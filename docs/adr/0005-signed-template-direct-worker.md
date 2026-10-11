# ADR 0005: Direct execution from the signed public template

- Status: superseded by ADR 0006
- Date: 2026-08-10

## Context

The generated public mirror and a personal public Worker currently contain the
same complete tree. The second public repository preserves free GitHub Actions
capacity but does not add a privacy boundary: sealed jobs remain in the private
Mailbox and encrypted results remain short-lived Artifacts. For the authorized
owner of the canonical template, the duplicate Worker can therefore be removed
without moving the Mailbox or compute into a private Actions repository.

## Decision

CourseLens supports two dispatch modes:

1. `personal-worker` remains the compatible default for users who do not own
   the signed template. Its `main` commit may differ, but its complete tree must
   equal the approved signed tree. Client repair may rebuild that managed tree.
2. `signed-template-direct` is allowed only when the configured Worker
   repository is exactly the repository named by the bundled signed release,
   the authorized GitHub identity owns it, and live repository metadata says it
   is public, an unarchived template, enabled, and uses `main` as its default
   branch. The live `main` commit and tree, signed manifest digest, and trust
   epoch must exactly equal the bundled release pin.

Direct mode never repairs or advances template `main`. Drift yields the closed
error `template_release_required`; recovery is a new generated, reviewed,
signed template release. Every discovered and subsequently polled workflow run
must report a `head_sha` equal to the signed commit pin. A missing SHA is a
failure, not unknown success.

The private Mailbox, sealed protocol, one-time result key, short-lived
cross-repository user-token lease, Artifact import, and cleanup order do not
change. Environment secrets remain GitHub-encrypted values scoped to the Worker
environment; no credential, course URL, task body, or result enters the public
tree or logs.

## Migration boundary

Migration requires a fresh read-only preflight. It counts only active
`process.yml` and `echo.yml` workflow runs and Artifacts beginning with
`courselens-result-` or `courselens-checkpoint-`. It also requires zero managed
Mailbox comments and temporary bodies, local token leases, one-time result
keys, cleanup-pending records or marker, and `COURSELENS_JOB_TOKEN` in the old
Worker. Cloud automation runs and cloud Artifacts are outside this retired
personal-Worker migration and must not be accidentally treated as task data.
The GitHub App installation must enumerate exactly the direct template, old
Worker, and private Mailbox repositories. Additional repositories or an
unenumerable selection are not accepted. The migration writes a new private
identity only to the direct template; the old Worker and its original identity
remain untouched until the observation period is explicitly finalized.

## Consequences

- The canonical public repository can provide both the signed audit surface and
  free public Actions compute for its authorized owner.
- Other users retain the existing personal Worker plus private Mailbox design.
- Template publication and runtime execution share a repository but not a
  mutation path: the desktop client can write environment configuration and
  dispatch workflows, never template source or `main`.
- The old personal Worker is archived only after migration, round-trip tests,
  cleanup evidence, and an observation window; deletion is a later decision.
