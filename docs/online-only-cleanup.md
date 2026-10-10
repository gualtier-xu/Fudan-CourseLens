# Online-only cleanup and recovery

The online client is the only supported implementation. Catalog state lives in
`runtime/data/state.db`; there is no runtime import from `manifest.json` and no
feature flag that restores local media or compute.

## Supported boundary

- Launch: `python -m src serve` through the project-local client environment.
- API: `/api/health` and `/api/v3/*` only.
- Media: bounded no-store relay; no original media persistence.
- Compute: approved signed Worker only.
- Recovery: signed Worker pin or local state/credential backup only.

## Destructive cleanup gate

`scripts/cleanup_legacy.py` may be applied only after all of these are true:

1. Current authorized catalog and all three bounded subtitle modes pass against
   real media, with complete remote cleanup.
2. Personal Worker is repaired to the approved signed public tree.
3. A real encrypted task completes and Artifact, Mailbox temporary content,
   token leases, temporary token, and cleanup-pending counts are zero.
4. The synthetic browser boundary matrix passes on the current frontend.
5. `state.db` integrity, catalog counts, and identity hashes are recorded.
6. No CourseLens task, remote run, import, lease, FFmpeg, ASR, or OCR process is
   active, and all deletion paths are ordinary paths inside an approved root.

Before deletion, save an ACL-restricted backup of `state.db` and credential
ciphertext under `runtime/data`. Do not back up old media, models, or local
compute environments.

The cleanup report contains byte counts and hashed path identifiers, never
course titles, URLs, account names, cookies, transcript text, or credentials.

The accelerated beta policy does not require a 14-day observation window,
complete-lecture compute, manual content-quality scoring, or clean Windows
Sandbox onboarding before
legacy asset deletion. Those are release-quality limitations, not deletion
safety boundaries. It does require hash-bound evidence for the automated
signed Worker repair, encrypted echo cleanup, and browser matrix. The real
three-mode smoke results remain bound in the status report. Cloud unattended
automation is not a release gate while its product
entry points are absent. Generate the fail-closed release report before any
deletion:

Use the resumable orchestrator as the single entry point during acceptance. It
caches local checks by source-content digest, invalidates real evidence when its
client/catalog/Worker binding changes, and writes only hashes and bounded
status data under the ignored `runtime/reports` directory:

```powershell
python scripts/final_acceptance.py run-local
python scripts/final_acceptance.py status
```

Each real gate must produce UTF-8 JSON with schema
`courselens.acceptance-gate-evidence.v1`, the gate name, `status: passed`, and
the binding printed by the acceptance harness for that exact context. Record it
with `final_acceptance.py record --gate NAME --artifact PATH`. Re-running
`run-local` skips unchanged passing checks; `--force` performs a complete rerun.

Credential retirement is a separate fail-closed operation. First use
`migrate-credentials --source PATH` to copy only DPAPI-protected accounts,
DeepSeek, proxy, repository and Worker verification state from one discovered
legacy store. OAuth access/refresh tokens, transient job tokens and enabled
state are never migrated. Then authorize the
migration worktree through `final_acceptance.py authorize` and run
`verify-credentials`. When direct GitHub access is unavailable, pass the same
explicit `--proxy http://127.0.0.1:PORT` to both commands; the acceptance tool
does not inherit ambient proxy variables. Live verification refreshes and persists the OAuth pair,
checks the GitHub identity without recording it, and verifies the approved
Worker tree. Only during the following 15 minutes may explicit legacy
credential candidates be passed to `retire-credentials`. Reports contain path
and ciphertext hashes, byte counts, and removal status, never account IDs,
secret names, tokens, or plaintext. The active credential store cannot be a
retirement candidate.

```powershell
python scripts/final_acceptance_audit.py `
  --evidence runtime/reports/final-acceptance-evidence.json `
  --package-root D:\path\to\the\installed\CourseLens `
  --require-installed-package `
  --output runtime/reports/final-acceptance.json
python scripts/cleanup_legacy.py `
  --root D:\path\to\the\installed\CourseLens `
  --acceptance-report runtime/reports/final-acceptance.json `
  --report runtime/reports/legacy-cleanup-preflight.json
```

Apply mode additionally requires `--apply --confirm-online-only`. The cleanup
tool refuses a missing or non-ready acceptance report, an invalid CourseLens
root, active processes, unsafe links, live code dependencies, catalog mismatch,
or nonzero task/run/import/lease state. The two project-external targets are an
exact allowlist: `CourseLens-Rollback-Media-20260720-151657` and
`CourseLens-Reset-Audit`.

## Recovery

Restore the state and credential backup with its original ACL, validate SQLite
integrity, start the client, and run Worker verification. Recovery does not
restore downloads, FFmpeg, desktop-player integration, local models, or local
compute environments.
