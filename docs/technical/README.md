# CourseLens technical reference

This document describes the private online-only client monorepo. Student usage
starts in the repository [README](../../README.md).

## Live-room API and media boundary

- `GET /api/v3/live-room/status?course_id=...` returns `live`, `upcoming`,
  `ended`, `denied`, `offline`, `stale`, or `unknown`.
- `POST /api/v3/live-room/grants` creates a short-lived one-time grant after a
  fresh `live` observation.
- `POST /api/v3/live-room/sessions` consumes it, sets a path-restricted
  `HttpOnly; SameSite=Strict` cookie, and returns only a local manifest path.
- `GET /api/v3/live-room/play/{session}/...` serves bounded rewritten HLS
  resources by opaque IDs.

The gateway validates HTTPS/443, DNS and connected peer IP, TLS hostname/SNI,
same-origin redirects, request header allowlists, content type, response size,
and Range bounds. Master, variant, segment, initialization-map, and key URIs
are rewritten. FLV and unknown formats are rejected.

Chromium playback uses local `hls.js` 1.6.13 under Apache-2.0; its license,
version, and SHA-256 are retained under `frontend/vendor/hlsjs/`.

The media trust boundary (what may reach disk/network, and what never leaves
the machine) is specified in [media-protection-boundary](../media-protection-boundary.md).

## Source boundaries

The end-to-end dataflow across these boundaries is drawn in
[architecture-dataflow](../architecture-dataflow.md).

```text
frontend/                 Browser shell and domain ES modules
src/app.py                Composition root and server lifecycle
src/application.py        Online client and remote-task orchestration
src/api/                  iCourse and WebVPN integrations
src/remote/               GitHub App, protocol, Worker, and connection control
src/runtime/              Repositories, v3 HTTP, media, search, tasks, timetable
src/services/             Constructor-injected domain boundaries
worker/                   Canonical public Worker source
shared/protocol/          Authentication-free shared wire protocol
scripts/                  Release, audit, setup, cleanup, and acceptance tools
runtime/                  Private local data; never part of a mirror export
```

The client source tree contains no local ASR/OCR pipeline, media downloader,
FFmpeg wrapper, desktop player integration, or legacy API implementation.

`src/app.py` is the only production module allowed to construct
`CourseLensApplication`. It immediately projects the repositories and
integrations into lifecycle, auth/catalog, media, learning, task, remote,
automation, timetable, and settings services. The HTTP adapter receives only
that immutable service container; it cannot reach the application aggregate or
private fields. Narrow legacy-shaped HTTP doubles are adapted only inside the
test package.

## API contract

The server binds only to `127.0.0.1`. Its supported routes are
`/api/health` and `/api/v3/*`. Media, subtitle files, and SSE are versioned too:

- `/api/v3/media`
- `/api/v3/subtitles/file`
- `/api/v3/subtitles/segments`
- `/api/v3/events`

Responses use `courselens.api.v3` envelopes. Error codes are closed and
machine-readable. Authentication and remote operations are idempotent through
operation IDs. The UI does not mutate task state optimistically; it reloads
backend-confirmed state after an accepted operation.

Media uses a bounded single range, `private, no-store`, session refresh, and
strict authorization. The response surface excludes upstream URL, cookie,
origin, referer, authorization header, and filesystem path values.

Two frozen contracts pin the evidence and connection faces: the closed-set
evidence ID space lives in
[evidence-contract-v1](../evidence-contract-v1.md), and the campus connection
snapshot (`courselens.vpn-connection.v1`) validator contract lives in
[vpn-connection-contract-v1](../vpn-connection-contract-v1.md).

## Data and repositories

`CatalogRepository`, `TaskStore`, and `LearningStore` use short-lived SQLite
connections. Catalog schema v2 contains no local download path, byte count, or
local processing status columns. Schema upgrades are idempotent and do not read
`manifest.json`. The data-management store-layer contract (export, cleanup,
delete semantics) is frozen in
[course-data-management](../course-data-management.md).

Every account uses the same uniform topology (ADR 0006): a personal public
`Fudan-CourseLens-Worker` executes real tasks and a personal private
`Fudan-CourseLens-Mailbox` carries sealed transport. Personal-worker integrity
is tree-based: `main` may point at any commit whose tree equals the bundled
signed tree. The public template repository is the signed release pin (and the
public client release portal, ADR 0006) and never executes tasks; a configured
Worker
equal to it fails closed with `personal_worker_migration_required`, and the
GitHub App installation must select exactly the Worker and Mailbox
repositories (`installation_scope_not_exact` otherwise). The former
`signed-template-direct` mode (ADR 0005) is retired; see
`docs/signed-template-direct-migration.md` for the preserved history and the
read-only retirement audit.

The canonical no-content `process_canary` uses the production `process.yml`
path while accepting no payload, secrets, requested outputs, account, URL,
media, or user text. It binds the decrypted signed proof to the personal
Worker commit, audits the executor and the public template's quiescence, and
keeps normal remote compute disabled. See `docs/process-canary.md` for the
one-shot and crash-resume runbook.

`state.db` owns catalog, operation, run, lease, automation, and timetable state.
`learning.db` owns derived learning evidence and the local text index. Secrets
are held by the platform credential store and are never returned by API
snapshots or persisted in browser storage.

## Worker and protocol

Wire protocol v2 remains stable. The active Worker must pass all of these
checks before bootstrap, repair, task lease, or dispatch:

1. Pinned public commit and Git tree.
2. Root-signed trust metadata and monotonic epoch.
3. Active, non-revoked release key.
4. Manifest signature and manifest digest.
5. Per-file digest, payload tree, and protocol compatibility.
6. Personal Worker tree equality with the approved public mirror.

The private exporter is allowlist-only and fail-closed. Publication creates a
generated public PR; it never pushes private Git objects or history. Personal
Worker and Mailbox changes are made only through the client.

The unattended cloud automation contract (opt-in disclosure, daily windows,
circuit breakers) is documented in
[cloud-unattended-automation](../cloud-unattended-automation.md). The
accepted/rejected trade-offs of AI research proposals are tracked in
[airesearch-decisions](../airesearch-decisions.md).

## Runtime and launch

The Windows launcher validates `tools/python310`, repairs only
`.venv-client-py310`, starts `python -m src serve`, waits for `/api/health`, and
opens the browser. There is no system Python fallback and no local media
toolchain. `runtime-assets.json` contains the signed mirror pin, Catalog schema,
GitHub App public identity, Python base, and client environment only.

Client update logic is isolated in `src/update/`. The fixed manifest binds a
Windows/x86-64 package to a signed release; verified packages expand only to
immutable managed-install slots. `GET /api/v3/client-update` and
`POST /api/v3/client-update/actions` expose a redacted authoritative state
model. The trust policy is disabled by default because no production client
release service or signing key has been approved.

The v2 trust policy separates an offline update root from protected manifest
release keys and machine-enforces production gates. The approved distribution
target is the public GitHub Releases of `gualtier-xu/Fudan-CourseLens-Worker-Release`
(ADR 0006), tagged `client-v<semver>` and downloaded anonymously. Public
download adds no trust: downloads are restricted to an exact host allowlist
(`github.com`, `release-assets.githubusercontent.com`,
`objects.githubusercontent.com`), every redirect hop is re-validated with a
three-hop ceiling, and no Authorization header is ever attached; production
therefore remains fail-closed even if the JSON enabled flag is changed. The
release controller's original design record is
[ADR 0002](../adr/0002-public-release-controller.md).

For a managed installation, the controlled installer copies the trust policy to
the stable outer `trust/client-update-trust.json`. Ordinary version packages
exclude the source template, and a managed client refuses to start its updater
without the external file. U1 pilot layouts must therefore be reprovisioned by
the installer rather than trusting metadata copied from a release slot.

Release packages contain only the reviewed runtime allowlist and bind a clean
private-monorepo commit/tree. ZIP entries must exactly match the hashed metadata
inventory and obey the Windows namespace; ambiguous or extra names fail before
extraction. The stable helper repeats exact slot-inventory validation. The
installer also records every stable launcher asset in `launcher-trust.json`, and
the launcher confirms a selected slot only when health reports both its target
version and the pid it just spawned. Production remains blocked on independent
release scanning, Authenticode, ACL, and real Windows acceptance.

## Validation

```powershell
python -m pytest -q
$env:PYTHONPATH = "worker;."; python -m pytest worker/tests -q
python -m compileall -q src worker shared scripts tests
Get-ChildItem frontend -Recurse -Filter *.js | ForEach-Object { node --check $_.FullName }
python scripts/check_online_only_residue.py
python scripts/check_text_encoding.py
python scripts/check_markdown_links.py
python scripts/final_acceptance_audit.py
.\scripts\audit_dependencies.ps1
git diff --check
```

The public mirror separately runs unittest, compileall, protocol compatibility,
manifest verification, boundary/history scans, Gitleaks, and mirror policy.

### Test collection surface

Where a test must live to be run automatically: pytest collects
`tests/test_*.py` (`pytest.ini`); the routine CI loads every
`tests/frontend_*_behavior.mjs` by glob; worker tests are discovered from
`worker/tests/test_*.py` via unittest. Helpers without the `test_` prefix
(`tests/http_services.py` and friends) are intentionally not collected — import
them, or run them by hand.

## Recovery and cleanup

Worker rollback means publishing and pinning a previously approved signed
snapshot. It never re-enables a template or local compute path. Local recovery
uses an ACL-restricted backup of `state.db` and platform-protected credential
ciphertext. See [online-only cleanup](../online-only-cleanup.md) and
[signed Worker rollback](../worker-mirror-rollback.md).

Physical asset deletion is fail-closed. The approved cleanup is complete; its
current state is recorded in the [project handoff](../handoff.md). Current work
must start there to avoid repeating completed Worker,
echo, migration, and cleanup operations.
