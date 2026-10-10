# CourseLens project handoff

Last updated: 2026-10-02 (night14 tree-hygiene refresh; prior refresh was
2026-09-18 NIGHT-AUDIT-1). Current authority: `private/main`, branch `main`,
HEAD `6857bc6` (tree `4a4517cc`); remote `origin/main` currently `78c1bd59`.
Publication to the remote is a separate, explicitly
authorized step. This header supersedes the 2026-09-10 authority pointer at
the cleanroom checkout `private/courseLens-main-20260909` @ `8f2cb166`; that
root is lineage history now. Where this file and other documents disagree,
this file wins.

历史 handoff / closeout 群（16 个文件）已于 2026-10-02 归档至
`docs/archive/`（night14 R4 P-02）：群内互引随迁有效，指向 living 面的三条
出链已加 `../` 前缀；现状权威以本文件与根 README 为准。

## Batch lineage 2026-09-10 → 2026-09-18 (first-parent digest)

- 09-10 launcher hardening: relocation-safe launcher (896578c), runtime
  credential-boundary test lock (ea63e3f), OS-assigned launcher port
  fallback (51fa228), product batch 02 (bf81eb7), home overview redesign
  (e4ab40a).
- 09-11 connections/playback: live access and session UX (e0cc423), catalog
  refresh selection preservation (ab7b0f0), GitHub connection UX alignment
  (109e6ef), GitHub first-run setup (e88d720), connection and playback
  experience (d41c9d1), cloud learning intelligence + immersive playback
  (ce8ab5f), inactive player volume icons (ad8dcba).
- 09-12 evidence chain: subtitle evidence compatibility seam (627b8ed),
  subtitle/learning evidence pipelines (50db487), evidence-aware correction
  and navigation (a1f8296), standard-only subtitle automation with OCR
  context (909d16f), local backup pruning non-fatal (216d8d0), lecture IR +
  frontend polish (16ad06a), private CI publication gates restored
  (89f608b).
- 09-13 campus continuity: TUN media recovery guidance (cc3c13c), exam-aware
  AI learning workspace (bcf687b), campus connection recovery UX (505e943),
  campus recovery continuity hardening (f3d6fdd).
- 09-14 startup/cloud: lightweight client startup restore (1731794),
  unattended learning plan + local courseware assembly (34bafda), lecture
  slide PDF (a431ee4), actions quota UI removal (1915a7b).
- 09-15 usability/github: current usability bugs (987b723), timetable
  auto-refresh + quiet partial-source banner (27453bd), GitHub device grant
  persistence (a50788b), state-driven GitHub connection card (37ef2cf).
- 09-16 update/data/reset/visual: one-click update chain + course data
  workspace (ca8fa97), client reset with background receipt (c05ba3a), data
  workspace UX + update-chain hygiene + thread/harness repairs (e5a2008),
  visual tokens stage 1 + frontend repairs wave (5b0fda6), visual stage 2
  batches 1–3 (d7fcdd7, 6635d27, a72b956).
- 09-17 greeting closure: visual batch 4 greeting/verse empty state
  (acf1acf), timetable today-highlight contrast (d11f59a), context-tagged
  verse selection (95e8556), greeting hero polish + verse swap loading
  repair (b3127ff), CSP-externalized greeting boot (dcebbe0b).
- Post-milestone (09-17): milestone regression PASS at 506a1e9 (canonical
  py310 1631 passed / 6 skipped, mjs 16/16, clean tree). GitHub connection
  batch followed: stored-binding owner validation (a778d368), webvpn login
  route/transport memory (653d6934), proxy auto-detect + onboarding proxy
  card (4cd0a2ce), installation-step surfacing with trusted links
  (03f615f8), first-link authorization diagnostics hardening (4ebac0de),
  first-run install handoff with creation-time save (e88dd80). Baseline at
  e88dd80: **1686 passed / 6 skipped / 0 failed + mjs 16/16**.
- 09-18 overnight (NIGHT-FRONT-1): remote-action observability codes +
  endpoint-class routing (a5d40b4), leftover reuse-path denial downgrade to
  awaiting-installation with account-level install URL (f59fefd1), reset
  permission-class deletion refusal degraded to trusted manual-deletion
  links (8c1fd425), automation code-table completion + FakeRemote mirror
  alignment (9e58b350), user-token refresh once when installation appears
  (fa94ebe9), pre-install repo denial deep-classified into installation
  guidance (39da27d3), login dialog merged into one identity card
  (a5463391), visual contract probe (342ddd88), docs sync (3c77fab5).
  Baseline after the overnight batch: **1701 passed / 6 skipped / 0 failed
  + mjs 16/16**.

## Validation formula (canonical)

- Client suite: `.venv-client-py310\Scripts\python.exe -m pytest -q` →
  expect 1701 passed / 6 skipped at the 09-18 overnight head (1686 at
  e88dd80).
- Frontend behavior harnesses: `node --test` over the tests/*.mjs glob →
  16/16. (Run with a glob, not a directory.)
- Worker suite (unchanged): `.venv-client-py310\Scripts\python.exe -m pytest`
  with `PYTHONPATH=worker;.` over `worker/tests`.
- anaconda ≥3.12 interpreters are census-only, not canonical.

## Source and safety boundary

Unchanged from the 09-10 record: never place credentials, account data,
course URLs/titles/bodies, cookies, or other sensitive course data in
commands, logs, reports, screenshots, issues, or chat; do not edit managed
Worker or Mailbox state by hand; do not read, move, or modify
`runtime/data`. Untracked `docs/*prompt.md` one-off launch prompts are
user-retained: never commit or delete them.

## Topology (ADR 0006, unchanged in shape)

| Repository | Role |
| --- | --- |
| `Fudan-CourseLens-Private` | sole editable source (client, Worker, protocol, release control plane) |
| `gualtier-xu/Fudan-CourseLens-Worker-Release` (public) | official signed Worker template + public client Release portal (since 2026-09-30; renamed 2026-10-02 to the `-Release` suffix, WINIT-1 case 1 — GitHub redirects the old name until reclaim; the old co-account repo 旧称 `gualtier-xu-co/Fudan-CourseLens-Worker` is kept only as a read-only historical mirror); never executes real tasks |
| `OWNER/Fudan-CourseLens-Worker` (public) | per-account Actions executor |
| `OWNER/Fudan-CourseLens-Mailbox` (private) | per-account sealed mailbox |
| `gualtier-xu-co/Fudan-CourseLens-Releases` (private) | retired distribution host; deleted |

Mirror pin (runtime-assets.json): commit `d4f6ce5e…` / tree `a85ae5e6…`,
signing key `release-2026-09-cleanroom-r2`, trust epoch 1. The generated
public mirror is behind private main; it catches up only through
`scripts/release_worker_mirror.py` re-pins. Until the next re-pin, its
public README still shows the retired three-strategy ASR wording; the
canonical `worker/README.md` single-`automatic` policy text is
authoritative.

## Open gates

1. **First public client release** — three-stage trial packaging
   (trial → ceremony → flip), candidate version `0.1.0-trial.1`;
   `courselens-version.json` currently reads `0.1.0` / `stable`. Requires
   the exe installer, self-signed gate pass, dual-root USB set, and a 7-day
   observation window. The production updater stays machine-disabled until
   its own gates pass.
2. **Client reset remote-repo ceiling** — RESOLVED in the 09-18 night batch
   (T3 `8c1fd425`): permission-class deletion refusals degrade to a
   `repos_manual_deletion` receipt with per-repo trusted settings links and
   the local reset completes normally; transient failures still abort.
3. **Real-account validation window** — while a live validation period is
   active, frontend-only packages are held from dispatch unless the
   controller explicitly releases them.
4. **After 2026-10-01** — promote the pushed backup ref
   (`backup/main-20260915` = `ca8fa97`) to `main` on the remote via PR, then
   resume the normal push cadence.
5. **Parked** — release-packaging P0-1/P0-3, corpus v2, macOS stays
   CI-smoke only. The `Fudan-CourseLens-Releases` archive item is done: the
   repository has been deleted.

## Environment notes (for a fresh agent)

- Canonical venv directory is literally `.venv-client-py310` (recipe name
  "venv-py310" in ledgers refers to this directory).
- Serve: `.venv-client-py310\Scripts\python.exe -B -m src serve --port 8765
  --no-open` (loopback only; bare bundled `tools\python310` dies with
  ZoneInfoNotFoundError).
- Proxy handling is automatic: the client detects system proxy settings
  (Windows registry plus a closed 5-port fallback set); the historical
  manual `127.0.0.1:6268` note is legacy.
- After every mirror re-pin the first task dispatch is rejected until the
  encrypted channel test is re-run: POST `remote-connection/actions`
  `{action: "test-channel"}` with a fresh `operation_id`.
- Headless relogin can transiently fail with `fudan_login_failed`; back off
  ~90 s and retry once before treating it as an outage. Headless Fudan
  sessions expire after ~8–10 minutes without UI activity.
- Session-injection drivers live in `C:\Users\admin\.courselens-secrets\`
  (DPAPI store `store.json`; location only, never values).

## Stable records (do not redo)

- Remote-gate record: live-cancellation latch from run `33626274453` stays
  terminal `failed` (budgets 0/0); never replayed. Recovery snapshots
  `../taskstore-sidecar-recovery-20260902T043745Z` and
  `../state-sidecar-recovery-20260903T171407097` are retain-only.
  `private/runtime-convergence` is frozen.
- Mailbox reconciliation + process canary DONE 2026-09-07 (PR #113 record,
  run `34138886920`).
- Authorized catalog ladder 1 RESOLVED 2026-09-08 (PR #116 record); the next
  real login doubles as its end-to-end confirmation.
- Release runbook (Worker mirror): `python scripts/release_worker_mirror.py`
  from a clean, pushed main; restart the service and re-run the channel test
  afterwards.
- 2026-09-17 milestone: MSR-1 RUN2 commit `506a1e9` = merged wave closure
  (visual stage 1 + repair waves) with a zero-ignore baseline and clean
  tree.

Architecture: [project overview](project-overview.md); process gates:
[process canary](process-canary.md); migration decision:
[ADR 0006](adr/0006-uniform-personal-worker-and-public-releases.md).

## Retained diagnostic tools (no standing callers; keep, do not sweep)

- `scripts/catalog_session_probe.py` — closed-set catalog/session probe used by
  the MEDIAWEBVPN-3 media-chain investigations (SRC-CLEANUP-1 U6② kept it
  deliberately, 2026-09-22).
- `scripts/update_local_github_token.py` /
  `scripts/update_runner_github_token.py` — manual GitHub-token rotation aids
  landed on night9 (ede25f3); no automation calls them, they are run by hand.
