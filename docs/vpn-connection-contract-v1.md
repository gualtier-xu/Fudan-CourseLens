# Campus connection snapshot contract — `courselens.vpn-connection.v1`

Status: **FROZEN v1** (2026-09-13, package VPN-CONTRACT-1).

Companion artifacts:

- Fixture: `tests/fixtures/vpn_connection_contract_v1.json`
- Reference validator + contract gate: `tests/test_vpn_connection_contract.py`
- UX source of truth: `product-vpn-user-experience-optimization-plan-20260913.md` (P0.1 "Unified connection state contract and card")

## 1. Purpose

This contract freezes one honest, closed-set campus-connection snapshot shared by the backend (producer) and the frontend (consumer). It is the wire-level shape behind the single "校园连接" status card: one status, one reason, one prioritized list of user actions, and per-service detail. Every field, enum member, cross-field rule, size limit, and rejection code is enumerated here; anything outside the closed set is rejected.

Scope note (from the UX plan): "VPN" here means the application HTTP path (WebVPN and the optional local proxy). This contract never describes or controls a system VPN, TUN, route table, or system proxy.

## 2. Scope and non-goals

- v1 is a **contract gate only**. No product runtime code imports it in v1; the reference validator is embedded in the test file (standard library only, deterministic) and a later backend package may lift `validate_snapshot` verbatim into product code as the runtime validator.
- It **changes no existing shared protocol, evidence contract, worker schema, authentication snapshot, or API surface**. It is an independent, additive schema with its own schema string. Producers map their internal states onto this closed set at the boundary.
- The snapshot contains **no free-form strings** — there is no field that can carry URLs, credentials, cookies, tokens, `lck` values, or proxy addresses. Negative fixture cases prove that secret-looking fields are rejected with `unknown_field`.
- The plan's P0.1 minimal field list (`state`, `network_path`, `school_route`, `reason`, `observed_at`, `expires_at`, `retry_after`, `actions`) is extended in this dispatch contract with `generation` (route-generation semantics, plan P0.3) and `services` (per-service path detail, plan P0.2). This extension is part of frozen v1.

## 3. Wire shape

A snapshot is a JSON object with **exactly 11 top-level fields, all required**:

| # | field | type | rules |
|---|-------|------|-------|
| 1 | `schema` | string (const) | exactly `courselens.vpn-connection.v1` |
| 2 | `state` | enum(9) | §4.1 |
| 3 | `network_path` | enum(3) | §4.2 |
| 4 | `school_route` | enum(4) | §4.3 |
| 5 | `reason` | enum(9) | §4.4 |
| 6 | `observed_at` | epoch seconds | integer, `1..4102444800` (§7) |
| 7 | `expires_at` | null \| epoch seconds | state-coupled, §5.1 |
| 8 | `retry_after` | null \| epoch seconds | coupling, §5.2 |
| 9 | `actions` | ordered enum list | duplicate-free members of §4.5; may be empty; ≤ 6 |
| 10 | `generation` | integer | `0..2^53−1` |
| 11 | `services` | object(2) | exactly `webvpn` + `icourse`, §4.6 |

## 4. Enum semantics

### 4.1 `state` — aggregate campus-connection state (9 members)

| member | meaning | typical producer condition |
|---|---|---|
| `off` | connection feature idle; no probe attempted yet | before first probe, or feature disabled |
| `checking` | a no-credential probe or login flow is in flight | path probe, verification, or single-flight auth running |
| `ready` | campus services are usable | required service reads verified this snapshot |
| `login_required` | a path exists but credentials are missing or rejected | protected action without a usable session |
| `reauthenticating` | a bounded silent refresh attempt is in flight | session stale, recovery single-flight started |
| `network_unavailable` | no campus path reachable (off-campus, no proxy, dead path) | both allowed paths failed before ticket use |
| `challenge_required` | an interactive challenge (CAPTCHA / 2FA / consent) was met | the UI must pause and hand off to the user; never auto-bypassed |
| `expired` | a previously valid session has expired | server rejected a once-valid session |
| `degraded` | partially usable: at least one service verified, at least one not | e.g. WebVPN ready, iCourse unreachable |

### 4.2 `network_path` (3)

- `direct` — campus services reached directly (campus network / direct route).
- `local_proxy` — reached via the explicitly configured local proxy client on the user's machine.
- `unknown` — path not determined (e.g. cold start).

### 4.3 `school_route` (4)

- `webvpn` — the school WebVPN route is in use.
- `icourse_direct` — the direct school-service route is in use.
- `mixed` — different services used different routes in this snapshot.
- `unknown` — route not determined.

### 4.4 `reason` (9)

| member | meaning |
|---|---|
| `cold_start` | first observation; nothing probed yet |
| `direct_ok` | direct campus path confirmed |
| `proxy_fallback` | fell back to the local proxy path |
| `session_expired` | a previously valid session expired |
| `possible_tun_interference` | local TUN/system proxy *may* be interfering; only when evidence supports it (plan principle 4: no false certainty); pairs with `close-tun-and-retry` |
| `credentials_rejected` | login refused or credentials missing/invalid |
| `challenge` | an interactive challenge was encountered |
| `service_unavailable` | school service unreachable or erroring |
| `unknown` | no specific cause |

### 4.5 `actions` (6, ordered)

`login`, `reauthenticate`, `check-network`, `retry`, `open-settings`, `close-tun-and-retry`

- The list is **priority-ordered**: producers emit actions in the order the UI should offer them; consumers must preserve order and must not sort, deduplicate, or extend it. The list may be empty (nothing to ask of the user, e.g. `ready`).
- Membership is closed; duplicates are rejected (`duplicate_action`), so the list is bounded at 6 entries by construction.

### 4.6 `services` — per-service detail (exactly `webvpn` and `icourse`)

Each service object has **exactly three fields**:

- `state`: `unknown | checking | ready | unavailable` — probe state of that service. Note this is a *different, smaller enum* from the top-level `state`.
- `route`: `direct | local_proxy | unknown` — transport used for that service.
- `verified`: boolean — `true` only when the producer actually confirmed usability in this snapshot (e.g. an authenticated read succeeded). Never inferred from optimism; a successful portal probe is not proof of catalog identity (plan: "never treat a successful portal probe as proof of iCourse identity").

The top-level `state` is the aggregate UX state; per-service states may differ from it (e.g. aggregate `expired` with one service still `ready`).

## 5. Cross-field rules (normative)

### 5.1 `expires_at` coupling — recorded decision

- For `state` ∈ {`ready`, `degraded`}: `expires_at` must be a valid epoch integer (`> 0`, `≥ observed_at`, `≤ 4102444800`). A producer must know or estimate its session expiry to claim a usable state.
- For **every other state**: `expires_at` must be `null`.
- **Zero is never valid** — neither for non-ready states nor anywhere else. `0` is rejected (`bad_time`) so that "no expiry" has exactly one representation (`null`) and sentinels cannot be confused with real times. Negative values are rejected (`negative_number`).

### 5.2 `retry_after` coupling

- `null` by default. Non-null is allowed **only when `actions` contains `retry`** (`retry_after_without_retry` otherwise).
- When non-null: a valid epoch integer (`> 0`, `≤ 4102444800`) and `≥ observed_at` (a retry time in the past is meaningless → `retry_before_observed`).
- Any state may set it; there is no further state coupling.

### 5.3 `reason` is advisory

No `state` × `reason` coupling matrix is enforced in v1 (recorded decision: keeps the closed set small; producers choose the most specific honest reason).

### 5.4 `generation`

Monotonic non-negative counter per producer session (`0` at cold start). When two snapshots meet, consumers must prefer the higher `generation`. Producers increment it on route-generation events (network-setting changes, plan P0.3).

## 6. Unknown, stale, and missing data (normative for consumers)

- Unknown values must be expressed with the `unknown` enum members (`network_path=unknown`, `school_route=unknown`, `reason=unknown`, `services.*.state=unknown`, `services.*.route=unknown`). Field omission is never a substitute; all fields are always required.
- **Staleness is a consumer rule, not a validator rule** (the validator is deterministic and never reads the wall clock):
  - A snapshot with `now − observed_at > 300` seconds is **stale**: consumers must treat it as unknown and re-probe before acting on it.
  - A snapshot with `expires_at < now` must be treated as expired regardless of its `state`.
- Producers must not fabricate: a service not probed in this snapshot is `unknown`/`unavailable` with `verified: false`.

## 7. Size limits (closed)

- Top level: exactly 11 fields. `services`: exactly 2 keys. Each service: exactly 3 keys.
- `actions`: at most 6 entries (bounded by the closed enum plus the uniqueness rule).
- Integers: `generation` in `0..2^53−1` (JSON/JavaScript `Number.MAX_SAFE_INTEGER`, so any JS consumer can compare generations exactly); time fields in `1..4102444800` (`2100-01-01T00:00:00Z`).
- No free-form strings exist: the longest legal string is the longest enum member, so unbounded data cannot hide anywhere in a valid snapshot.

## 8. Rejection codes (closed set of 18)

| code | meaning |
|---|---|
| `not_an_object` | snapshot is not a JSON object |
| `missing_field` | a required top-level field (or required service) is absent |
| `unknown_field` | field outside the closed set — this is how secret-looking fields are rejected |
| `unknown_service` | service name outside {`webvpn`, `icourse`} |
| `bad_schema` | `schema` string ≠ `courselens.vpn-connection.v1` |
| `bad_type` | value has the wrong JSON type (booleans are not numbers) |
| `bad_enum` | string outside a closed enum |
| `duplicate_action` | repeated entry in `actions` |
| `non_integer_number` | fractional value where an integer epoch/generation is required |
| `non_finite_number` | NaN / ±Infinity time value (cannot appear in strict JSON; exercised at code level) |
| `negative_number` | negative value where non-negative is required |
| `number_too_large` | beyond the documented maximum (e.g. generation > 2^53−1) |
| `bad_time` | time value of `0` or beyond the epoch ceiling |
| `expires_at_forbidden` | non-null `expires_at` in a non-usable state |
| `expires_at_required` | null `expires_at` in `ready`/`degraded` |
| `expires_before_observed` | `expires_at` < `observed_at` |
| `retry_after_without_retry` | non-null `retry_after` without `retry` in `actions` |
| `retry_before_observed` | `retry_after` < `observed_at` |

## 9. Deterministic validation order

The reference validator returns **all** violations as (`code`, JSON-path) pairs — paths look like `$`, `$.state`, `$.actions.1`, `$.services.webvpn.state` — in a fixed order; identical input always yields an identical list:

1. root must be a JSON object (`not_an_object`, stop);
2. missing required top-level fields (canonical field order), then unknown top-level fields (sorted);
3. per-field checks in canonical field order (type → enum → value rules);
4. cross-field rules (§5), guarded so that an invalid prerequisite field never adds cascade errors.

## 10. Compatibility and versioning

- v1 is **additive and frozen**: no field, enum member, or rule may change within `courselens.vpn-connection.v1`.
- The closed set means unknown fields and unknown enum values are **rejected, not ignored** (fail-closed; it is also what keeps secrets out). Forward compatibility is achieved only by minting `courselens.vpn-connection.v2` with a new contract document; v1 consumers must reject v2 snapshots (`bad_schema`).
- No existing shared protocol, evidence contract, worker schema, auth snapshot, or API surface is modified by v1. The existing authentication snapshot stays compatible and untouched (plan P0.1).

## 11. Fixture and gate

- `tests/fixtures/vpn_connection_contract_v1.json` is **strict JSON** (no `NaN`/`Infinity` literals). Layout:
  - `contract`: the schema name this fixture serves;
  - `fixture_version`: `1`;
  - `note`: synthetic-data reminder;
  - `valid`: named valid snapshots — `ready_direct`, `checking_cold_start`, `degraded_proxy_fallback` (the required trio), plus `login_required` and `expired_session`;
  - `invalid`: 42 mutation entries `{name, base | explicit_snapshot, set?, remove?, expect_code, expect_path}` that the test applies to a named valid base (dotted `set` paths reach nested fields).
- `tests/test_vpn_connection_contract.py` embeds the reference validator (standard library only, deterministic, no product imports) and gates:
  - every `valid` snapshot validates clean;
  - every `invalid` case is rejected with its expected code at the expected path;
  - every rejection code except `non_finite_number` is exercised by the fixture (the non-finite guard is exercised at code level because strict JSON cannot carry NaN/Infinity);
  - the fixture file contains no URL-scheme, `lck=`, JWT-shaped, or long hex/base64 string (static hygiene, also run as an explicit grep);
  - the validator is deterministic and the closed-set enum sizes are pinned.
- Fixtures are fully synthetic: round epoch numbers, no real course names, URLs, accounts, cookies, tokens, or proxy values.

## 12. Security notes

- No free-form strings ⇒ no place to carry credentials, cookies, tokens, `lck` values, URLs, or proxy addresses.
- Unknown/secret-looking fields are rejected with `unknown_field` (fixture cases `secret_field_rejected`, `secret_url_field_rejected`, `unknown_top_field_rejected`).
- `challenge_required` snapshots exist so the UI pauses for the user (plan P1.1); nothing in this contract automates or bypasses a challenge.
- The contract never asks producers to expose proxy URLs or account identifiers; per-service detail is limited to the closed enums and one boolean.
