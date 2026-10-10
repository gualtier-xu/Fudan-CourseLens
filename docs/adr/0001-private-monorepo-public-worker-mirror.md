# ADR 0001: Private monorepo and generated public Worker mirror

- Status: accepted
- Date: 2026-07-20

## Context

CourseLens currently maintains the desktop client in the private repository and
the generic Actions Worker in a separate public template. The encrypted wire
protocol is implemented independently on both sides, while the desktop client
pins a public commit and tree and repairs each managed student Worker to that
tree. This protects dispatches, but it leaves two source repositories that can
drift and permits manual maintenance of files that should be generated.

The client launcher, packaging scripts, imports and tests depend heavily on the
current repository root. Moving the complete client into a new subdirectory in
the same migration would add unrelated startup and packaging risk.

## Decision

`Fudan-CourseLens-Private` is the only source-of-truth repository.

- Existing client directories remain at the repository root for compatibility.
- Public Worker source is maintained under `worker/`.
- Only protocol constants, canonical serialization, encrypted envelope logic,
  closed error codes and compatibility metadata that are genuinely shared live
  under `shared/protocol/`.
- `Fudan-CourseLens` becomes a generated, review-only mirror. A protected
  workflow exports an explicit allowlist, signs its manifest, constructs an
  orphan snapshot in an empty repository, and creates a PR whose parent history
  contains only the existing public repository.
- No private commit or object is pushed to the public repository.
- Personal Workers remain supported public repositories managed by the desktop
  client. Their commits may differ, but their complete tree must equal an
  approved public mirror tree. ADR 0005 additionally permits the authorized
  template owner to dispatch directly from the exact signed template release;
  this does not change the private Mailbox boundary.
- Student Mailboxes remain separate private repositories with their current
  encrypted transport and cleanup responsibilities.
- Wire protocol v2 remains active. Mirror manifest v1 adds provenance and
  version negotiation without enabling protocol v3.

## Trust and release model

The public mirror contains a canonical manifest and detached Ed25519 signature.
A root-signed trust document identifies active, retired and revoked release
keys and revoked manifest digests. The offline root private key never enters
GitHub. The release signing key and the private key of a dedicated minimal
GitHub App are available only to the protected `worker-mirror-release`
Environment on private `main`.

The release App is installed only on the public mirror and has metadata read,
contents write and pull-request write permissions. Pull requests and untrusted
code receive no release secret. All Actions are pinned to full commit SHAs.

The exporter fails closed for unknown files, symlinks or reparse points, path
escape, private imports, invalid file modes, non-UTF-8 text and build caches.
The same source commit and signing key must produce byte-identical output.

## Compatibility and rollback

The initial rollout used a temporary dual-track bootstrap. After the generated
mirror and signed round-trip were accepted, that transitional path was removed
by ADR 0003. The supported client now accepts only approved signed mirrors.

Rollback republishes and pins a previously approved, non-revoked source through
the generated release workflow. A student Worker is never repaired by hand.
Credentials, logs, Mailbox data, sealed jobs, Artifacts, and token leases retain
their existing security and cleanup boundaries.

## Consequences

- Worker and protocol changes originate in one private repository.
- Public changes are auditable generated PRs instead of hand-edited source.
- A protected private-repository Environment and a dedicated release App are
  prerequisites for real publication.
- Public history is preserved and scanned; only future snapshots are generated.
- Client directory cleanup is deliberately deferred to a separate migration.
