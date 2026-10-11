# Client update chain: map, local rehearsal, and gaps

This manual maps the whole signed client-update chain with exact anchors,
documents how to reproduce the full chain locally end to end, and lists the
remaining gaps between the local rehearsal and a production release.  The
operations side (keys, publishing, rotation) lives in
`client-update-operations.md`; this file is the structural map plus the local
rehearsal.

## Chain map

### Channel pointing

> **Single source of truth:** repository identities live in
> `config/distribution.json` (loaded via `src/distribution.py`); run
> `scripts/check_distribution_references.py` after any rename.  Names below
> reflect the current registry values.

- `src/update/service.py` — `SOURCE_REPOSITORY` (source-identity pin, never
  channel-related), `DISTRIBUTION_REPOSITORY` (pinned default channel), and
  `UPDATE_CHANNEL_ENV = "COURSELENS_UPDATE_CHANNEL"`, whose blank/unset value
  keeps the default verbatim via `_distribution_repository()`.
- The effective repository flows into exactly three places, which must stay
  coherent: `_stable_manifest_url()` (manifest fetch target),
  `_validate_package_url()` (package start-URL prefix), and
  `UpdateService._policy()` (the trust policy must pin the same repository).
  A channel the installed policy does not pin fails closed with
  `distribution_policy_invalid`.
- `config/client-update-trust.json` is the committed production GO policy
  since GO-FLIP-1 (`enabled: true`, root `root-2026-10`, release key
  `release-2026-10-go1`, all nine gates true; `40e2d96`).  A managed install
  reads its live policy from `<install_root>/trust/client-update-trust.json`
  (`src/app.py::_client_update_trust_path`); a source checkout reads this
  config template as its policy, but the install action still fails closed
  with `managed_install_required` because it has no managed-install layout.

### Trust anchors

- Root keys live only inside the installed trust policy; every
  `courselens.update-key-authorization.v1` envelope inside it is verified by a
  root key (`TrustPolicy.load`).  Authorized release keys carry epoch,
  validity window, channel, and platform; `minimum_key_epoch` blocks
  rollback.
- `_policy()` fail-closes unless: enabled, platform/architecture match the
  host, all nine production gates are boolean-true, the distribution block
  matches the effective channel exactly, and `manifest_url` equals
  `_stable_manifest_url()`.
- The managed layout itself is validated by
  `src/app.py::_client_install_root` (`COURSELENS_INSTALL_ROOT` may redirect
  it) and `install-layout.json` gates installs and background checks.

### The five hops

1. **Check** — `UpdateService.check` fetches the manifest from the pinned
   URL and `_verify_manifest` enforces: Ed25519 signature by an authorized
   release key, canonical-JSON binding, publish/expiry window, security
   floor (`max(manifest minimum, policy minimum)`), anti-rollback (key epoch
   and accepted-version replay/mix-and-match against
   `<state_root>/accepted.json`).
2. **Download** — re-fetches and canonically re-verifies the manifest,
   checks disk headroom, downloads the package with per-hop revalidation
   (https, 443, exact host allowlist, globally-routable resolved addresses,
   ≤3 https redirects, no Authorization header ever), then pins size and
   SHA-256.
3. **Apply (staging)** — `_extract` opens the zip defensively (no links,
   traversal, collisions, or extra files; per-file SHA-256; exact inventory;
   `courselens-package.json` identity) into `versions/<version>`, then
   records `accepted.json`.  `install` requires the managed layout and
   writes `state/pending.json`.
4. **Restart** — `update_now` runs check→download→install inside one lock,
   then `complete_restart` requests shutdown only after the HTTP 202 is
   written (`http_api.py` client-update actions route).  The trusted
   launcher owns the restart: apply pending → start-and-probe →
   confirm/rollback, with at most one automatic restart per launcher
   session.
5. **Health / rollback** — the new slot stays `awaiting_health` until the
   backend confirms the expected version; more than 120 s (`recover`) or a
   failed probe triggers `rollback_if_unhealthy`, an atomic pointer switch
   back to the previous verified slot.  User data lives outside slots and is
   never rolled back.

### Version source and background checks

- The version label is `courselens-version.json` at the project root (read
  by the composition root and by `build_client_update.py`).
- `start_background_checks` runs only for managed installs
  (`install-layout.json` present): first check after a random 5–15 minutes,
  then ~24-hour period, exponential offline backoff, never gated by active
  work, controlled by the `update_background_checks` preference.
  `src/app.py::_stop_client_update_background_checks` stops it during serve
  teardown.
- HTTP surface: `GET /api/v3/client-update` (snapshot),
  `POST /api/v3/client-update/actions` (`check|download|install|update_now`
  with `confirmed`; 409 for `update_busy`/`update_restart_blocked`).  UI:
  `frontend/modules/update-widget.js` and `settings.js`.

### Build side

- `scripts/build_client_update.py` (fail-closed, never uploads): runtime
  allowlist (`frontend/`, `src/`, `shared/`, `docs/repository-readmes/` plus
  fixed root files), outgoing secret/course-data scan, provenance (clean
  tree, remote must normalize to the private source repository), version
  from `courselens-version.json`, Ed25519 manifest signature, package zip +
  `manifest.json` written outside the worktree.
- Production signing keys live only in the protected release environment
  (`client-update-operations.md`); there is deliberately no local key.

## Local end-to-end rehearsal

`scripts/rehearse_client_update_local.py` reproduces the entire chain
locally in seconds and writes nothing outside one stage root:

1. Clean throwaway clone at the current HEAD with the private source
   repository as `origin` (provenance passes without touching the dirty
   working tree).
2. Throwaway Ed25519 root + release keys (generated in memory; the seed file
   is deleted immediately after the build).  The verification code paths are
   the production paths — only the key material is rehearsal-local.
3. `build_client_update.py` invoked unmodified to produce the package zip +
   signed manifest for the current version.
4. A one-shot `127.0.0.1` HTTP channel (port 0) serves the canonical GitHub
   release paths.  Because the real transport validates every hop against
   https/443/global-IP by design (anti-SSRF — loopback can never pass), the
   rehearsal maps canonical URLs onto the local channel through the
   service's injected-transport seam; every other line runs unmodified.
5. A managed layout under the stage root
   (`state/install-layout.json`, external `trust/`, `versions/0.0.9` old
   slot) with an isolated updates state root; the driver walks
   check → download → install, then a real child process runs from inside
   the extracted `versions/0.1.0` slot, calls `apply_pending` +
   `confirm_health`, and reports `healthy` at the new version.
6. The same run repeats the pass with `COURSELENS_UPDATE_CHANNEL` pointing
   at a rehearsal repository (policy pinned to match), then exercises three
   negative branches through the real local channel: manifest 404 →
   `policy_blocked/download_http_error`; corrupted signature →
   `policy_blocked/manifest_signature_invalid`; same-version manifest →
   `up_to_date`.

Reproduce with: `python scripts/rehearse_client_update_local.py --keep`
(stage kept for inspection; without `--keep` a green run removes itself).
Fast synthetic equivalents of the negative branches live in
`tests/test_update_local_channel_branches.py`; the channel-override pins in
`tests/test_update_channel_override.py`.

## Rollback semantics (summary)

- A published release is immutable: bad bytes are rejected by hash,
  signature, or anti-rollback checks; fixes are new `client-v<semver>`
  releases.
- After a staged switch, health confirmation is the only way to keep the new
  slot; timeout or probe failure atomically restores the previous slot.
- Operationally disabling updates = restoring the disabled trust policy;
  the updater fails closed with `update_not_configured`.

## Gap list: local rehearsal vs production release

1. **Production keys**: the real root/release keys live in the protected
   release environment; the rehearsal proves the verification paths with
   throwaway keys.  Nothing local needs to change for GO.
2. **Real channel end-to-end**: publishing a draft release and checking
   against `github.com` requires the publisher token and a real release —
   user-authorized territory (`client-update-operations.md` steps 6–7).
3. **Transport realism**: the rehearsal's byte movement uses the injected
   transport seam because loopback is intentionally unreachable for the real
   hop validator.  A production-grade rehearsal against the real network
   would need a published (draft) release.
4. **Installer signing**: by the 2026-09-30 decision, release installers ship
   without Authenticode code signing (`client-update-operations.md`);
   pre-install integrity is the published SHA-256 receipt, enforced by the
   `release_artifact_sha256_published` production gate. If a signing
   certificate is purchased later, an Authenticode gate returns with it.
5. **Production GO**: closed 2026-10-02 — the user-authorized flip landed
   (`40e2d96`, GO-FLIP-1): the committed policy is the production policy
   (enabled, all nine gates true) and the live `client-v0.1.0` face carries
   the signed production manifest on the master-repo portal (2026-10-08
   same-name repo rebuild R2: current generation `rebuild-r2b-20261008`;
   the pre-rebuild `rebuild9d-20261005` face was the one live-verified
   2026-10-07); the update chain is
   live for provisioned installs.  Current values:
   [update-chain-values.md](update-chain-values.md).

## Glossary

- **Channel** — the distribution repository the client is pinned to
  (`DISTRIBUTION_REPOSITORY` default, `COURSELENS_UPDATE_CHANNEL` override);
  not the release maturity label.
- **Trust policy** — `client-update-trust.json`: root keys plus root-signed
  release-key authorizations; installed outside version slots.
- **Release key** — the Ed25519 key that signs manifests; authorized by a
  root key with epoch, window, channel, and platform.
- **Slot** — `versions/<semver>` under the install root; immutable after
  extraction; `current.json` points at the running one.
- **Pending switch** — `state/pending.json`: a staged slot waiting for the
  launcher restart; consumed by `apply_pending`.
- **Health window** — the 120 s after a switch during which the new slot
  must confirm; otherwise `recover()` rolls back.
- **Accepted history** — `accepted.json` in the updates state root: the last
  version/key-epoch accepted, backing anti-rollback and mix-and-match
  checks.

## Data contracts (summary)

- Update envelope: `courselens.client-update.v1` =
  `{schema, manifest, signature{key_id, key_epoch, value}}`; the signature
  covers the canonical (sorted-key, compact) JSON of `manifest`.
- Manifest required keys: `release_id, version, channel, platform,
  architecture, published_at, expires_at, minimum_security_version, package,
  release_notes, source`; `package = {url, size, sha256, format:"zip-v1"}`;
  `source = {repository (= neutral source id `Fudan-CourseLens-Source` since
  CO-NEUTRAL-1; the real private name lives only in the checkout-only
  sidecar), commit, tree}` (commit and tree 40-hex each).
- Package inventory: `courselens-package.json` =
  `courselens.client-package.v1` with the same identity fields plus
  `files: {path: sha256}`; the zip must contain exactly that inventory.
- Trust policy: `courselens.client-update-trust.v2` (keys, authorizations,
  `allowed_hosts`, `manifest_url`, `distribution`, boolean
  `production_gates`).
- Version strings: `MAJOR.MINOR.PATCH[-prerelease]` (semver subset; the
  stable channel rejects prereleases).

## Audit trail format

Every state transition appends one line to
`<data_root>/updates/transition-audit.jsonl`
(`src/update/durability.py::append_audit`):
`{"event": "state_transition", "previous": <state>, "state": <state>,
"observed_at": <unix seconds>}` — redacted by construction: no URLs, no
package names, no error text.  Error details travel only in the closed-set
`error_code` of the state file/snapshot, never in logs or the audit trail.

## Channel inventory (operations reference)

> Per-value current sheet with `file:line` anchors and change history: [update-chain-values.md](update-chain-values.md).

| Item | Value | Where pinned |
| --- | --- | --- |
| Distribution repository | `gualtier-xu/Fudan-CourseLens`（总仓门户；2026-10-04 REBUILD-9 自旧发布门户重指，旧门户已退役、旧名不载公开版） | `service.py` default + trust policy `distribution.repository` + `manifest_url` |
| Override | `COURSELENS_UPDATE_CHANNEL` env | effective repository; policy must pin the same value |
| Manifest asset | `courselens-windows-manifest.json` | trust policy `distribution.manifest_asset` |
| Tag namespace | `client-v<semver>` | trust policy `distribution.tag_namespace`; package-URL validator |
| Allowed hosts | `github.com`, `release-assets.githubusercontent.com`, `objects.githubusercontent.com` | trust policy `allowed_hosts` |
| Source repository | `Fudan-CourseLens-Source`（中性标识，2026-10-05 CO-NEUTRAL-1 起；真实私仓名仅存 checkout-only 侧车 `config/ops-private.json`） | manifest `source` pin（`MANIFEST_SOURCE_ID`, `service.py:61`）; `SOURCE_REPOSITORY` reads the sidecar for private-channel tooling |
| Signing keys | root (offline) → release (protected environment) | trust policy + GitHub environment secrets |

Key rotation runbook distillation (full text in
`client-update-operations.md`): generate new release key → root-sign a new
`active` authorization with `key_epoch + 1` and an overlap window → ship via
an independently distributed installer → after adoption, raise
`minimum_key_epoch` in a later installer; revocation ships a root-signed
`revoked` authorization.

## Network environment note

The update transport connects directly to the pinned, resolved address and
deliberately ignores `HTTP(S)_PROXY` environment variables (the pinned-IP
anti-DNS-rebinding design and proxying are mutually exclusive).  On a
network that forces a proxy for outbound HTTPS, updates fail honestly with
`network_unavailable`; use a direct connection or an intranet channel
pinned via the trust policy.  The launcher's startup probe bypasses proxies
the same way (`Invoke-RestMethod -Proxy $null` in
`scripts/start_managed_courselens.ps1`), so the whole chain is
proxy-free by design, not just the download transport.
