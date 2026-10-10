# Signed-template-direct migration and rollback

> **RETIRED (2026-09-07):** signed-template-direct mode was retired; every
> account now uses its own personal Worker plus a private Mailbox, and the
> public template never executes real tasks. The preflight script is
> read-only, and the prepare/rollback/finalize scripts report the retirement
> and exit 3 without touching state. See ADR 0006
> (`docs/adr/0006-uniform-personal-worker-and-public-releases.md`). The
> original runbook is preserved below for history only.

This runbook reduces the active CourseLens repositories from five to four while
preserving the public-compute/private-Mailbox privacy boundary. It does not
merge Git histories and does not move Mailbox Issues into the public template.

## Target topology

- `Fudan-CourseLens-Private`: only source repository.
- `Fudan-CourseLens`: generated signed template and public Actions executor.
- `Fudan-CourseLens-Mailbox`: private sealed task/control transport.
- `Fudan-CourseLens-Releases`: private client release assets.

The old `Fudan-CourseLens-Worker` remains available for rollback until the
observation period and deterministic reconstruction check pass.

## Preflight

Run the read-only inventory from the installed client environment:

```powershell
python scripts/signed_template_direct_preflight.py
```

Exit code `0` and `ready: true` require all of the following from fresh local
and GitHub API evidence:

- the direct target passes owner, public, template, main, archive, signed
  commit/tree/manifest and exact trust-epoch checks;
- the live GitHub App installation repository set is exactly the public direct
  template, the old public Worker, and the private Mailbox; an unenumerable,
  missing, or additional repository is fail-closed;
- no active `process.yml` or `echo.yml` task run exists;
- no `courselens-result-` or `courselens-checkpoint-` Artifact exists;
- no managed Mailbox comment or unconsumed temporary task body exists;
- no local remote-token lease or `remote_result_private:*` key exists;
- no remote/import cleanup state or cleanup marker is pending;
- the old Worker has no `COURSELENS_JOB_TOKEN`.

The report contains counts and public repository pins only. It never emits an
Issue body, comment, OAuth token, course URL, task payload, result key, or
credential. Do not activate from a saved report: rerun immediately before the
change so the gate is fresh.

## Activation sequence

1. Save the old Worker repository identifier and its matching public encryption
   and signing keys as rollback metadata in local DPAPI storage.
2. Create or verify the signed template's `courselens-worker` environment without
   changing template source or `main`.
3. Generate a new Worker box/signing key pair, upload only the private halves as
   environment secrets, and store only the public halves locally.
4. Set the private Mailbox repository and bounded strategy variables on the
   template. Do not create a persistent job token.
5. Leave every old Worker secret and variable unchanged. Atomically point the
   client at the direct template and its new public key pair only after the
   template environment has passed read-back checks.
6. Recheck direct integrity. A mismatch restores the untouched old Worker tuple
   and must return
   `template_release_required`; `repair-worker` is not an allowed action.
7. Run the reusable encrypted echo and process canary, then re-run
   `python scripts/signed_template_direct_preflight.py` immediately before each
   canonical synthetic gate. Run replay and tamper in memory, cancellation only
   with its independent exact GO. Every live run must have the signed commit as
   `head_sha`; a cancellation success race fails the gate after cleanup.
8. The only acceptance order is: canonical process canary, cancellation,
   replay rejection, tamper rejection, reconstruction, then a fresh zero-state
   audit and observation decision. Run finalization successfully before
   archiving the old personal Worker. Deletion is separately authorized and is
   never implied by archival.

Steps 1 through 6 are performed by the resumable, fail-closed preparation
command:

```powershell
python scripts/prepare_signed_template_direct.py
```

The command first forces routine remote compute off and never restores it. It
uses a DPAPI-encrypted checkpoint for one newly generated Worker identity,
configures and read-back checks only the direct template environment, and
replaces the local repository and matching public keys in one credential-store
transaction. The old Worker retains its original private identity, while its
repository and matching old public keys remain in the encrypted checkpoint.
A successful preparation is not activation:
leave remote compute disabled until encrypted echo, independent cleanup review,
and the remaining canaries have passed.

If preparation is interrupted, rerun the same command. It reuses the encrypted
checkpoint and converges the direct environment idempotently. Do not start a second
key rotation, edit `credentials.json`, or dispatch a job while the checkpoint
is incomplete. New private key material is removed from the local checkpoint
after the signed direct repository passes its exact integrity check. The old
public rollback tuple remains until explicit finalization.

## Rollback

Rollback is a configuration switch, not a branch rewrite:

```powershell
python scripts/rollback_signed_template_direct.py
```

1. Stop new dispatch and repeat the same zero gate against the direct executor.
2. Require zero task runs and task Artifacts in both possible executor
   repositories, plus zero Mailbox, lease, result-key and cleanup residue.
3. Restore the saved personal Worker identifier and its matching original local public
   key pair.
4. Re-run personal-Worker tree verification. Its commit may differ, but its full
   tree must equal the still-approved signed tree.
5. Re-enable the archived Worker, run encrypted echo, and confirm complete
   Artifact, Mailbox, lease, result-key, and cleanup removal.
6. Leave the template release unchanged. Never force-push or have the client
   repair template `main`.

If the template itself drifted, rollback to the personal Worker first and then
publish a newly reviewed signed template release. Editing the template in place
or weakening the signed pin is not a recovery path.

## Finalize before archival

After the observation period, run the separate irreversible gate immediately
before archiving the old Worker:

```powershell
python scripts/finalize_signed_template_direct.py
```

Finalization requires the exact three-repository App installation set, zero
task state across both executors and the Mailbox/local stores, a trusted direct
template, and an old Worker that is still available and unarchived. Only then
does it atomically delete the DPAPI rollback checkpoint. It does not archive or
delete the repository itself and it never enables remote compute.
