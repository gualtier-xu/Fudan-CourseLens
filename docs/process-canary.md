# Canonical process canary

## Gate status boundary

Commit pins, run IDs, and mirror state are dynamic. The sole current status
entry is [the handoff](handoff.md); this document keeps only the stable canary
protocol and its failure semantics. Any new process canary needs its own,
separately authorized GO.

The process canary and live-cancellation gates are distinct. The
live-cancellation latch from run `33626274453` is terminal (`failed`, dispatch
and cancellation budgets `0/0`): it must not be replayed, resumed, or replaced
by a second workflow dispatch. Its local recovery and global-zero verification
passed.

The process canary proves the production remote-task transport without using a
Fudan account, course URL, course body, upstream media, cookie, credential, user
file, or arbitrary operator text. It is not a quality test and it cannot be
used to submit general synthetic content.

## Protocol boundary

The job kind is `process_canary` under the unchanged wire protocol v2. Its
top-level fields are exact. `payload` and `secrets` are empty objects,
`requested_outputs` is an empty list, and the pipeline is the constant
`actions-process-canary-v1`. Null payloads, additional fields, other outputs,
source sessions, media, titles, queries, accounts, URLs, and credentials fail
schema validation before sealing or processing.

`process.yml` supplies the constant `process-v1` workflow profile. The runner
creates a fixed numeric fixture internally; task ID, clock, repository input,
and operator input cannot change it. The fixture is never returned or logged.
The signed encrypted result contains only the closed schema, fixed fixture
digest and counts, the process profile, and `GITHUB_SHA`. The client requires
that commit to equal the signed dispatch pin.

## Operator sequence

Normal remote compute must be explicitly disabled. The default command is a
read-only preflight:

```powershell
python scripts/verify_github_process_canary.py
```

Preflight requires the personal Worker pin (a `main` commit whose tree equals
the bundled signed tree), a dispatch target bound to the configured personal
Worker repository, a quiescent public template, and a global zero state across
that single executor. It prints count-only evidence and the exact
confirmation token. It does not dispatch.

After an independent verifier accepts that fresh report, run exactly:

```powershell
python scripts/verify_github_process_canary.py `
  --dispatch-once `
  --verifier-go PROCESS-CANARY-GO
```

The command persists a protected latch before dispatch. A crash or cancellation
reuses the same task ID and coordinator record; it must never create a second
run. Once a signed worker commit passes, the latch rejects another canary for
that same commit. Remote compute remains disabled throughout; only the
in-memory settings for this one coordinator use `enabled=True`.

## Required postflight

Success requires all of the following from fresh API and local evidence:

- the stored workflow is `process.yml` and the run completed successfully;
- every dispatch and poll observed the signed `head_sha`;
- the decrypted signed result reports the same commit and `process-v1` profile;
- import is an in-memory proof and writes no course or learning database;
- the task and checkpoint Artifacts are deleted;
- the Mailbox Issue is closed and consumed with zero comments;
- the personal Worker executor has zero active process/echo runs and task
  Artifacts, and the public template gained no new run and has zero
  non-completed runs since the canary window opened;
- token leases, all temporary result keys, cleanup markers, and the temporary
  environment job-token secret are zero;
- a bounded public-log scan finds no fixture literal or sensitive field marker;
- `remote_enabled` is still false within the canary's own credential scope;
  the canary requires remote compute disabled throughout (a canary-run
  requirement enforced by the script, not a deployment status claim).

On failure, do not delete the latch or edit local state manually. Repair the
reported cleanup or signed-pin gate and rerun the same command so it resumes the
same task. Do not use this command before the new signed mirror containing the
canary protocol has passed its protected publication sequence.

A terminal canceled/paused run or a failed run without a resumable signed
Artifact is latched as failed and can never dispatch again for that signed
Worker commit. This is intentional one-shot behavior; publish and pin a newly
reviewed Worker commit before another process canary attempt.

## Migration synthetic gates

After the normal canary has reached its signed, zero-state postflight, use the
canonical synthetic verifier. Its default is always read-only; no argument can
silently dispatch a workflow:

```powershell
python scripts/verify_migration_synthetic.py cancellation
python scripts/verify_migration_synthetic.py replay --run --go REPLAY-REJECTION-GO
python scripts/verify_migration_synthetic.py tamper --run --go TAMPER-REJECTION-GO
```

Cancellation is separately high risk and requires
`PROCESS-CANARY-CANCEL-GO` plus the reviewed live binding. It has one signed
dispatch and one cancellation budget, must cancel before Mailbox publication,
and never converts an unknown POST outcome into a second dispatch. A success
during the cancellation race is a failed gate even if cleanup reaches zero.
All gate reports use `courselens.migration-gate-evidence.v1` and contain only
public pins, hashes, counts, state, and timestamps.
