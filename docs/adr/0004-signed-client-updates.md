# ADR 0004: Signed, user-confirmed Windows client updates

Note: ADR numbers are duplicated — `0004-local-live-room-hls-gateway.md` shares the number 0004; both files are retained under their names to avoid breaking links (see A48-P3-1).

Status note (2026-09-07): ADR 0006 supersedes the distribution host below.
Client release assets are now distributed through the public GitHub Releases of
`gualtier-xu-co/Fudan-CourseLens-Worker` instead of the private
`gualtier-xu-co/Fudan-CourseLens-Releases` repository, downloaded anonymously.
The trust model — offline root key, online release key, signed manifest,
SHA-256/size binding, Authenticode, and anti-rollback — is unchanged. The body
of this decision is preserved as written for history.

Revision note (2026-09-15, user-authorized): the double confirmation below —
explicit user confirmation before download and again before install — is
revised to a single explicit confirmation. `POST /api/v3/client-update/actions`
accepts exactly one new orchestration value `update_now` (requires
`confirmed=true`), which serially reuses the existing check → download →
install paths inside the same mutex/process lock and returns the unchanged
snapshot schema. When active application work makes a restart unsafe, the
orchestration is refused with the one new closed-set error code
`update_restart_blocked` (HTTP 409, retriable); no work is drained. The HTTP
202 reply is flushed before the service requests its own shutdown
(`update_restart`), and the restart itself stays with the trusted managed
launcher — which applies the verified pending switch after the service exits
and restarts the client at most once automatically per launcher session; the
dying process never spawns detached helpers. A low-frequency background check
(persisted `update_background_checks` preference, default on for packaged
builds, never in source checkouts) checks for updates only — it never
downloads or installs anything. Production rollout remains gated exactly as
stated below; steps 5–6 of the lifecycle are preserved as originally decided
for history.

Status: accepted for implementation; production rollout remains disabled until
release infrastructure and a production signing key are provisioned.

## Context and audit

At `31e6008d34f5d87aad7714782d303fcffe47f09a`, CourseLens has a source-tree
Windows launcher, a pinned project-local CPython 3.10 runtime, a project-local
virtual environment, and a signed public Worker mirror. It does **not** have a
client installer, immutable client version slots, a client release workflow,
client signing keys, or an approved client asset host. GitHub Device Flow
credentials are stored through the existing credential store and are intended
for the student's private Worker/mailbox integration. They are not an
authorization grant for client release assets.

Windows is the only supported managed-update target. macOS CI proves selected
imports/tests and portable package generation only; it does not establish a
native launcher, credential-store, installer, code-signing, notarization, update,
or rollback claim.

## Decision

The client uses a backend-authoritative `UpdateService` and a stable,
already-installed helper. A managed installation has:

```text
<install>/
  launcher/                 stable trusted launcher and helper
  versions/<semver>/        immutable, verified version slots
  state/current.json        atomic active-version pointer
  state/pending.json        verified requested switch
  state/install-layout.json managed-install marker
```

Source checkouts may check and download a verified update, but installation is
blocked with `managed_install_required`. This prevents the updater from
overwriting a git checkout or guessing an installation layout.

The update lifecycle is:

1. Fetch a bounded manifest over HTTPS from an exact allowlisted host.
2. Verify the fixed schema and detached Ed25519 signature using a key embedded
   in the installed trust policy.
3. Verify release id, semantic version, stable channel, Windows/x86-64 target,
   publication/expiry times, minimum security version, key id/epoch, package
   size, SHA-256, format and release notes.
4. Record replay state only after the matching package has been verified.
5. Require explicit user confirmation before download and again before install.
6. Download with a byte ceiling into an isolated partial file, flush it, rename
   it, check exact size and SHA-256, and safely expand it to a new immutable slot.
7. On restart, the stable helper rechecks the slot identity and atomically
   replaces `current.json`; it never executes code directly from the archive.
8. The launcher starts the selected slot and confirms health. If health is not
   confirmed, it atomically restores the previous verified slot.

One previous verified version is retained. Cleanup may remove older,
non-current, non-previous slots only after a later release; cleanup is not part
of the first rollout.

## Trust and release model

HTTPS is necessary but not sufficient. The Ed25519 signature is the release
authority, while SHA-256 binds the package bytes. The signed manifest binds all
metadata to prevent mix-and-match. Redirects are rejected; URLs must be HTTPS,
port 443, free of credentials/fragments, and use an exact host allowlist.
Resolved private, loopback, link-local, reserved and otherwise non-global
addresses are rejected to limit SSRF and DNS-rebinding attacks.

`config/client-update-trust.json` is a deliberately disabled source template
and contains no production key or endpoint. The controlled installer provisions
the managed client's trust policy at the stable outer
`trust/client-update-trust.json`; ordinary version packages exclude the source
template and cannot replace that file through the updater. The template names
the approved private binary repository,
`gualtier-xu-co/Fudan-CourseLens-Releases`, which contains only generated release
assets, signed manifests, licenses, and minimal release metadata. The client
must receive `contents:read` for that repository only and no access to the
private source monorepo. Production enablement requires a separate reviewed
change that:

- generates the Ed25519 seed in an offline or managed signing system;
- stores the seed only in a protected release environment, never in git,
  artifacts, logs, screenshots, issue text, URLs or subprocess command lines;
- commits only the public key and exact asset hosts;
- builds from a private-monorepo commit and publishes only the client package,
  signed manifest and approved release notes to a private distribution channel;
- proves how a minimally scoped token is obtained from the existing protected
  credential facility if private GitHub Release assets are chosen.

The client does not reuse the iCourse session, Cookie, GitHub Worker signing key,
or Worker access token. The frontend never receives a source URL, authorization
header, token, sensitive local path, package hash, or signing key material.

The update trust hierarchy is separate from every Worker key:

1. An offline update root key signs a fixed-schema release-key authorization.
2. That authorization binds release key id/public key, monotonic epoch,
   validity interval, status, channels and platforms.
3. A protected online update release key signs each client manifest.
4. The client verifies root authorization before accepting a manifest
   signature. Invalid, expired, revoked, wrong-channel, wrong-platform and
   below-minimum-epoch authorizations yield no usable release key.

Key rotation is monotonic. The offline root signs an overlap authorization for
the next release key and an independently distributed Authenticode-signed
installer provisions it outside version slots. After adoption, a later
installer raises `minimum_key_epoch`; a root-signed
authorization with `status=revoked` removes a compromised release key. Root-key
compromise requires an independently distributed Authenticode-signed recovery
installer; remote arbitrary root replacement is not accepted.

The protected environment and secret *names* are declared in configuration:
`client-release-production`, `COURSELENS_UPDATE_RELEASE_SIGNING_KEY`,
`COURSELENS_RELEASE_REPOSITORY_APP_ID`,
`COURSELENS_RELEASE_REPOSITORY_APP_PRIVATE_KEY`,
`WINDOWS_AUTHENTICODE_CERTIFICATE_PFX`, and
`WINDOWS_AUTHENTICODE_CERTIFICATE_PASSWORD`. No value is committed or created
by this decision.

## Security policy

Normal releases cannot force installation. Background check may be enabled by a
future local preference; downloads and installation remain user-confirmed.
`minimum_security_version` blocks a client that is already below the declared
floor rather than silently forcing it. The UI reports `policy_blocked` and the
operator must provide an independently installed recovery build. This avoids
turning release metadata into remote arbitrary forced execution.

Manifest validity is bounded by publication and expiry timestamps. Clients keep
the highest accepted version and release id, reject downgrade, replay and same
version/different-release substitution, and enforce non-decreasing key epoch.
System clock repair is an explicit recovery action; weakening time validation is
not.

Archive extraction rejects absolute paths, parent traversal, backslashes,
symbolic links, excessive file counts, per-file/aggregate expansion limits,
Windows alternate-data-stream syntax, reserved device names, trailing dots or
spaces, non-canonical Unicode, and Unicode/case-insensitive collisions. The ZIP
file inventory must exactly equal the signed package metadata plus the metadata
file itself. Staging is on the installation volume, disk space is checked, and
verified data is never executed from the download directory. Version slots and
state paths are resolved beneath exact roots. The stable helper accepts a closed
command set, derives targets from verified state, and independently rejects any
slot file outside the hashed inventory.

The builder selects only the reviewed runtime allowlist, refuses a dirty Git
worktree or a source remote other than the private monorepo, and binds the exact
commit and tree into both package metadata and the signed manifest. Its built-in
outgoing scan fails closed on unreadable/non-UTF-8 content, known credential
forms, and structured course-data samples. This local gate does not replace the
independent protected-environment scan required before production publication.

The controlled installer hashes every file in the stable launcher directory
except the trust manifest itself and writes the complete inventory to
`launcher-trust.json`. The launcher checks that exact inventory before starting
any slot. A startup is healthy only when `/api/health` reports both the spawned
process id and the target version; a pre-existing service on the port cannot
confirm a new slot.

The download implementation uses fixed connect/read timeouts, a package byte
ceiling, exact final size and crash-safe partial naming. Production HTTP retry
and bounded Range resume remain rollout gates; the initial implementation fails
closed and retries from zero. No claim of resumable download is made.

## Machine-enforced production gates

`scripts/check_client_release_gates.py` validates the committed policy without
reading secret values. Production release and client enablement require the
private binary repository, protected monorepo CI, offline update root,
protected update release key, implemented private-asset authorization,
Authenticode-signed launcher/installer, and clean-machine, real-restart and
power-loss recovery acceptance.

Only the private binary repository gate is currently true. In addition,
`PRIVATE_ASSET_AUTH_IMPLEMENTED` is a compile-time false capability: changing
JSON cannot enable private GitHub downloads. Until a reviewed backend obtains a
short-lived GitHub App installation token scoped only to the binary repository
and stores it in the existing protected credential store, the updater returns
`private_asset_auth_unavailable`.

Tokens must never enter a URL, frontend response, subprocess command line, log,
report, trace or screenshot. Device Flow/App consent must request only
`contents:read` on the binary repository. It must not grant access to the source
repository, and existing Worker/iCourse credentials remain out of scope.

## Backend contract

`GET /api/v3/client-update` returns only:

- closed state: `idle`, `checking`, `offline`, `up_to_date`, `available`,
  `downloading`, `verifying`, `ready_to_restart`, `applying`, `healthy`,
  `rolled_back`, `failed`, or `policy_blocked`;
- closed error code, current/available version, channel, trusted notes, package
  size, timestamps, allowed actions and rollback summary.

`POST /api/v3/client-update/actions` accepts `check`, `download`, or `install`.
Download and install require `confirmed=true`. The frontend renders progress
immediately but treats only a subsequent backend response as success.

## Consequences and rollout gates

The design avoids in-place replacement and supports crash-safe idempotent
recovery. It adds disk usage for one previous release and requires a stable
launcher distribution step. The repository now includes build/signing and
helper primitives plus synthetic end-to-end tests, but production remains
disabled until all of the following pass:

- private asset permission and retention model approved;
- Windows managed installer/launcher built and code-signed;
- production public key and exact hosts reviewed;
- bounded retry/Range implementation and proxy policy reviewed;
- clean-machine, restart, health-failure and power-loss acceptance complete;
- release workflow proves no source, course data or credentials are published.

macOS remains explicitly unsupported/unknown for client auto-update.

## 2026-09-30 note (signing decision and release-face flip)

Two user decisions land as of 2026-09-30; the historical body above is
preserved as written.

1. Release-face flip: distribution URLs point at the official
   `gualtier-xu/Fudan-CourseLens-Worker`; the host referenced in the
   historical body as (旧称) `gualtier-xu-co/Fudan-CourseLens-Worker` is kept
   only as a read-only historical mirror.
2. Signing decision: CourseLens ships without code signing. Authenticode is
   no longer a mandatory production gate — the machine-enforced gate list
   now requires `release_artifact_sha256_published` in place of the retired
   Authenticode launcher gate — and the release trust base is the published
   package SHA-256 checksum sheet plus the unchanged Ed25519-signed update
   manifest. Unknown-publisher/SmartScreen warnings on first run are
   accepted and covered by the README guidance.
   Rationale: a code-signing certificate is a recurring cost a student
   project cannot responsibly carry, while SHA-256 binding plus the Ed25519
   manifest already give the update path its tamper evidence, and an honest,
   guided first-run warning preserves trust better than an unverifiable
   signing claim. The update chain's Ed25519 verification path is unchanged.
