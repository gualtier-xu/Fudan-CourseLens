# Client update release, key rotation, and rollback operations

> **Repository identities:** `config/distribution.json` is the single source
> of truth for the distribution/source repository names used throughout this
> document; `scripts/check_distribution_references.py` enforces it.  The
> names below reflect the current registry values and follow it on a rebuild
> rename.


## Current production state

Production client updates are disabled. Since ADR 0006 the approved
distribution channel is the GitHub Releases of the **public** repository
`gualtier-xu/Fudan-CourseLens`, tagged `client-v<semver>`. Downloads are
anonymous and unauthenticated. Public download does not add trust: the offline
root key, the online release key, the signed manifest, SHA-256/size binding,
and the anti-rollback rules are unchanged. Since the 2026-09-30 signing
decision there is no code signing: the release trust base is the published
package SHA-256 checksum sheet plus the Ed25519-signed manifest, and Windows
SmartScreen/unknown-publisher warnings on first run are expected and covered
by the README guidance. Release
assets carry generated client packages, signed manifests, licenses, and
minimal release metadata only. Never push or mirror private monorepo source,
credentials, course URLs, course data, logs, traces, or screenshots.

The former private distribution repository
`gualtier-xu-co/Fudan-CourseLens-Releases` stays read-only during the observation
period. The updater never falls back to it automatically.

Run the non-secret gate audit:

```powershell
python scripts/check_client_release_gates.py
python scripts/check_client_release_gates.py --require-release-ready
```

The first command must pass the disabled-policy shape. The second must fail
closed (exit 3) until every remaining production gate is ready.

## Release tooling skeleton

- `scripts/sign_client_release.py` (Authenticode sign/verify) is retired from
  the release flow by the 2026-09-30 signing decision and stays in the tree
  unused; do not wire it back without a new decision.
- `scripts/build_client_update.py --authenticode-signed-file <name>` remains
  in the builder for history but the approved flow passes nothing; any
  deviation from the clean commit still fails the build.
- `.github/workflows/client-release.yml` is a dispatch-only skeleton. It stays
  inert until the repo variable `CLIENT_RELEASE_ENABLED=1` and the
  `client-release-production` environment secrets exist. Inputs are
  `release_tag` (`client-v<semver>`, validated fail closed), `package_url`
  (must sit under
  `https://github.com/gualtier-xu/Fudan-CourseLens/releases/download/<release_tag>/`),
  and `release_notes`. The final job creates a **draft** release on the public
  repository, uploads the built assets, and stops; publishing is a manual,
  independently verified step. There is no code signing (2026-09-30 decision):
  the job builds the package, signs the Ed25519 manifest with the release key,
  and uploads `SHA256SUMS.txt` with the draft assets.
- `scripts/bootstrap_asset_host.py` plans, checks, and (with `apply --yes`)
  verifies that the pre-existing public portal repository exists and is
  public; it never creates or edits anything. `generate-root-key` writes the
  offline update root seed outside the repository and prints its public key.
  It never handles secret values and never enables production.

## Student trial posture (2026-09-05 decision)

The first student-facing release is a trial distributed without a commercial
Authenticode certificate:

- Trial bits ship unsigned. Tamper-evidence for controlled installs comes from
  the SHA-256 launcher-trust inventory of `install_managed_client.ps1`, not
  from Authenticode. The launchers already run with
  `-ExecutionPolicy Bypass`, so unsigned PowerShell launchers need no script
  signing.
- No trust gate flips for the trial: `enabled` stays false, and the Ed25519
  update-manifest channel and the client release publisher identity are not
  part of trial distribution.
- Unknown-publisher/SmartScreen warnings are accepted for the trial.
- The 2026-09-30 decision makes this unsigned posture the standing posture for
  the production track too: no certificate (self-signed or CA) is planned;
  tamper-evidence comes from the published SHA-256 checksum sheet and the
  Ed25519-signed manifest. `scripts/sign_client_release.py` stays in the
  tree, retired from the flow.

## Protected release environment

Use the `client-release-production` environment in the private monorepo. The
environment must hold these secret names; this change does not create their
values:

- `COURSELENS_RELEASE_PUBLISHER_TOKEN`
- `COURSELENS_UPDATE_RELEASE_SIGNING_KEY`

The `WINDOWS_AUTHENTICODE_CERTIFICATE_*` names are retired by the 2026-09-30
signing decision; the committed trust template still lists them pending a
cleanup pass, and they must not be provisioned.

`COURSELENS_RELEASE_PUBLISHER_TOKEN` is the release publisher identity
consumed by the `publish-draft-release` job. Provision it as a dedicated
minimal GitHub App or a fine-grained personal access token with
`contents:write` on `gualtier-xu/Fudan-CourseLens` only — never the student
App, never an all-repositories grant, and never write access to the private
source repository. The earlier private-asset design's client App id/key secret
names are retired and must not be recreated. The student-facing GitHub App
(Worker/Mailbox) is untouched and keeps exactly its two-repository
installation.

Secret values stay in the protected environment/credential store. Do not pass
tokens or keys in URLs, process arguments, frontend JSON, logs, reports,
artifacts, issues, screenshots, or traces.

## Root and release-key ceremony

1. Generate the update root offline; escrow it independently. Do not add the
   root seed to GitHub.
2. Generate the update release key inside the protected signing environment.
3. Offline, sign `courselens.update-key-authorization.v1`, binding key id,
   public key, increasing epoch, validity window, `active` status, stable
   channel, and Windows platform.
4. Put only root public keys and root-signed release-key authorizations in the
   controlled installer's external `trust/client-update-trust.json`. Ordinary
   version packages explicitly exclude this file.
5. Protected CI signs each manifest with the authorized release key.
6. For rotation, use an independently distributed installer verified against
   its published SHA-256 checksum sheet to overlap old/new authorizations,
   ship adoption, then increase `minimum_key_epoch` with a later installer.
7. For release-key revocation, ship a root-signed `revoked` authorization and an
   independently safe recovery release. Root compromise requires a separately
   distributed recovery installer verified against its published SHA-256
   checksum sheet.

Worker root/release keys, iCourse credentials and client update keys are never
interchangeable.

U1 pilot installations do not yet have the stable external trust path. They
must be reprovisioned with the updated controlled installer before accepting a
U2-or-later package. Never copy trust metadata out of an ordinary release slot;
failing closed is the intended migration behavior.

## Release procedure after all gates pass

1. Verify a clean private-monorepo main commit and protected CI provenance. The
   builder independently rejects a dirty tree or a different source remote and
   records the exact commit/tree in signed release metadata.
2. Build the strict runtime-allowlisted package and exact per-file inventory
   without executing package content.
3. Generate the package/installer SHA-256 checksum sheet (`SHA256SUMS.txt`) for
   the release assets; there is no code signing (2026-09-30 decision).
4. Sign the manifest with the protected update release key.
   Set `--minimum-security-version` only when intentionally dropping support
   for older clients; otherwise the builder uses the trust policy's
   `minimum_version`. The floor must describe the oldest client allowed to
   install the release, not the new release version.
5. Treat the builder's fail-closed local scan as a first gate, then independently
   scan the exact outgoing file list in the protected environment for secrets,
   source-only material and course data.
6. Dispatch the client-release workflow with a fresh `client-v<semver>` tag and
   the matching package URL. The final job creates a **draft** release on
   `gualtier-xu/Fudan-CourseLens` and uploads the package and signed manifest —
   then stops.
7. Independently verify the draft before publishing: manifest signature against
   the authorized release key, SHA-256 recomputed against the manifest and the
   checksum sheet, exact package size, and a review of the release notes.
   Only then publish manually:
   `gh release edit <tag> --draft=false --repo gualtier-xu/Fudan-CourseLens`.
   Enable the repository's immutable-releases setting at or before the first
   publish, and never replace a published asset — fix forward with a new
   `client-v<semver>` release.
8. Canary on a clean Windows machine; test real restart and injected
   interruption at download, verification, extraction, pointer switch and
   health confirmation. Downloads from the public repository are anonymous; the
   client validates every download hop against its exact host allowlist and
   never sends an Authorization header.
9. Enable production only in a separately reviewed configuration change; the
   updater ships with `enabled=false` and stays disabled until that GO.

## One-click orchestration, managed restart loop, and background checks (2026-09-15)

`POST /api/v3/client-update/actions` accepts `update_now` with `confirmed=true`
as a single user confirmation: one action runs check → download → install
through the existing signed paths inside the same mutex/process lock and, when
a verified switch is staged, the HTTP 202 reply is sent first and only then the
service requests its own shutdown (`update_restart`). Active application work
blocks the restart with `update_restart_blocked` (HTTP 409, retriable); the
action stays retryable and no work is drained. Source checkouts cannot install
and gain no restart chain.

The managed launcher owns the restart: after the service process exits and
`state/pending.json` exists, it re-runs apply → Start-And-Probe →
confirm/rollback and reopens the fixed local URL. A session counter allows **at
most one automatic restart per launcher session**, so a broken switch cannot
loop; a second exit leaves the launcher (and the previous verified slot) as
today.

A low-frequency background check runs inside the service for packaged installs
only: the first attempt follows a randomized 5–15 minute delay, later attempts
follow a roughly 24-hour period throttled by the persisted `last_checked_at`,
with exponential backoff while offline. It is controlled by the
`update_background_checks` preference in the settings snapshot (default on,
user-toggleable), never downloads or installs anything, and is never gated by
active work.

Known residual risk (annotated deliberately, no lock added): when two
different data roots share one `install_root`, their services can race on the
shared `install_root/state` pointer files (`pending.json`, `current.json`).
The normal managed layout has exactly one data root per install root, so this
can only arise in a degenerate setup; sharing an install root between data
roots is not a supported configuration.

## Rollback

The stable trusted launcher retains the previous verified immutable slot. A new
slot becomes current through an atomic state-file replacement. Until the new
backend returns the expected version and the exact pid of the process spawned by
the launcher, `awaiting_health` remains true. A stale process already bound to
the port cannot confirm the slot. Timeout or launch failure stops the new process
and atomically restores the previous version. User data is outside version slots
and is not deleted or rolled back.

Distributing through immutable public Releases means a published version can
never be repaired in place: a bad release is fixed by publishing a new
`client-v<semver>` release, and clients reject the bad bytes by hash, signature,
or anti-rollback checks.

To disable updates operationally, restore the disabled trust configuration
(`enabled: false`); the updater fails closed with `update_not_configured`. The
former private repository `gualtier-xu-co/Fudan-CourseLens-Releases` remains
read-only during the observation period and is never used as an automatic
fallback source.

Do not manually edit `current.json`, execute code from staging, or copy files
over a running version. If automatic rollback is unavailable, use an
independently distributed recovery installer verified against its published
SHA-256 checksum sheet.
