# Namespace migration ledger

Status: active. Authority after the 2026-09-09 closeout cycle is
`gualtier-xu-co/Fudan-CourseLens-Private` at `main` =
`0eefaa43a7a022403df2c36fff96190197975151` (tree
`a9500f59aafae6a28668a394fc02616056e06cde`; same-day lineage
`b7fec619` → `4617aacc` → `0eefaa43`). The old primary namespace
`gualtier-xu-co/Fudan-CourseLens-Private` is frozen for ordinary
development; its PR #118 is closed as superseded. The in-repo copy of
this ledger (`docs/namespace-migration-ledger.md`) is current through
the `authority-0eefaa4-20260909` entry (PRs #4/#5 closeout); later entries
accumulate here until the next docs PR. The current authority bundle is
`courseLens-authority-main-0eefaa4-20260909.bundle`
(SHA-256 `1F8DF26C8D82808B7669E2974383289B7E287DE46C0ABA751DF1E2F4AA6F0090`).

This ledger records only nonsecret authority transitions for CourseLens source development. Each entry must identify the exact source and destination refs, the validated tree, the CI result, and the backup evidence. Do not store credentials, cookies, bearer values, API keys, private keys, course data, or runtime state here.

## Entries

### 2026-09-09 — full-workspace handoff prepared

```yaml
id: handoff-20260909-full-workspace
date: 2026-09-09
mode: full-workspace-plus-git-bundle
source: gualtier-xu-co/Fudan-CourseLens-Private
source_sha: 13cbc83ebe8ff4ebfaf79c3ee1d3dedea2255c1d
destination: pending-aleksandrlisikov5
destination_sha: not-created
tree: 1d5b1fb981d4692d4fbe76394205bd8dab134513
ci: not-run-in-destination
review: accepted-for-source-diff
bundle_sha256: DBB9E2460DC63CCEB4212FFC94445A163906B2B916C2F893ACBA5A44F3AC3C9A
authority_bundle_sha256: 94159C98FA265B90A8AAAEA64887837C63EC210220D483BA8BE3A88A13A5B3D5
authority_after: unchanged
notes: Full workspace, all-refs bundle, and authority-only bundle prepared; no destination repository created or accepted yet.
```

### 2026-09-09 — independent-student relay accepted

```yaml
id: relay-20260909-gualtier-xu-co
date: 2026-09-09
mode: G1-fresh-push
source: gualtier-xu-co/Fudan-CourseLens-Private
source_sha: 13cbc83ebe8ff4ebfaf79c3ee1d3dedea2255c1d
destination: gualtier-xu-co/Fudan-CourseLens-Private
destination_sha: b7fec6193229ce0f49d8bb92d3231de80fb46659
tree: 1d5b1fb981d4692d4fbe76394205bd8dab134513
ci: five-of-five green on PR #1
review: prior final-RC review accepted; PR #1 merged after green CI
bundle_sha256: CDE2B9BE40C70CE47A3D1D08740E9101842A094696F6D7584A8FC2FE731374C6
authority_after: gualtier-xu-co/Fudan-CourseLens-Private
notes: Destination repository created, baseline and final-RC branch pushed, PR #1 opened, CI passed, and merge completed. Old primary namespace is superseded for ordinary development; old primary PR #118 is closed.
```

### 2026-09-09 — post-merge restore drill and authority re-verification

```yaml
id: postmerge-restore-validation-20260909
date: 2026-09-09
mode: post-merge-restore-drill (validation only; no refs pushed or merged)
source: gualtier-xu-co/Fudan-CourseLens-Private
source_sha: b7fec6193229ce0f49d8bb92d3231de80fb46659
destination: none (authority unchanged)
destination_sha: not-applicable
tree: 1d5b1fb981d4692d4fbe76394205bd8dab134513
ci: five-of-five green on post-merge main run 34269517241; PR #1 run 34269109714 and baseline run 34268934952 also green
review: prior final-RC review accepted (2026-09-08); tree byte-identical, no new product diff
bundle_sha256: CDE2B9BE40C70CE47A3D1D08740E9101842A094696F6D7584A8FC2FE731374C6
authority_after: gualtier-xu-co/Fudan-CourseLens-Private
notes: >
  Overnight Task 3 continuation fresh-verified the authority (worktree clean,
  no active writer, no locks) and reran the restore drill from the authority
  bundle in a sibling-layout clone (drill-20260909/private/repo +
  public/worker-mirror pinned at 4e8f1614/e34f82ae). Full local validation
  reproduced exactly: client 964 passed / 6 skipped / 0 failed (138 subtests),
  worker 104 passed (42 subtests), six mjs harnesses passed, git diff --check
  clean, default release gate exit 0, --require-release-ready exit 3
  fail-closed. A bare restore clone without the sibling public template fails
  the reconstruction gate by design (layout-dependent, documented). Old
  primary PR #118 confirmed still CLOSED; old namespace frozen (last push
  2026-09-08T08:35:51Z). The 2026-09-08 CI billing blocker is resolved in the
  new namespace. Destructive phase remains NO-GO on eight gates (see
  post-merge-validation-manifest-20260909.md). Evidence:
  archive/external-artifacts/CourseLens-cleanroom-restore-set-20260909/post-merge-validation-manifest-20260909.md
```

### 2026-09-09 — in-repo ledger and routine-CI increments merged

```yaml
id: routine-increments-20260909-ledger-and-routine-ci
date: 2026-09-09
mode: ordinary-development (authority unchanged; observation-period commits recorded)
source: gualtier-xu-co/Fudan-CourseLens-Private
source_sha: b7fec6193229ce0f49d8bb92d3231de80fb46659
destination: gualtier-xu-co/Fudan-CourseLens-Private
destination_sha: 4617aacc9c923ec11b944c76efa40476697027b3
tree: 1612d13c9583f50d9f1a9195a6af1790e1068251
ci: >
  PR#2 full matrix 5/5 (run 34309498374); PR#3 final head routine run
  34310050775 + milestone dispatch 34310072876 green; merged-main push
  runs 34310101095 (c584134e) and 34310405462 (4617aacc) green
review: candidate validation recorded in task3-branch-preparation-manifest-20260909; hosted CI green on each exact merged head
bundle_sha256: CDE2B9BE40C70CE47A3D1D08740E9101842A094696F6D7584A8FC2FE731374C6
authority_after: gualtier-xu-co/Fudan-CourseLens-Private
notes: >
  User-authorized closeout merged sequentially: (1) PR#2 added the in-repo
  docs/namespace-migration-ledger.md (merge c584134e); (2) PR#3 split
  routine PR CI from the milestone matrix and fixed a latent masked
  hosted-CI failure — the reconstruction-gate test had been failing on
  hosted runners (no sibling public template) while private-ci.yml's
  combined pwsh step swallowed pytest's exit code; commit 129830c6 added
  an explicit fixture-absent skip proven in both layouts (merge
  4617aacc). Active worktree fast-forwarded to 4617aacc. Follow-ups
  recorded: private-ci.yml's Windows step still structurally masks pytest
  exit codes (split into separate steps in a future reviewed PR); a fresh
  authority bundle for 4617aacc should be generated at the next natural
  checkpoint.
```

### 2026-09-09 — authority bundle regenerated for 4617aacc with full restore drill

```yaml
id: authority-bundle-4617aacc-20260909
date: 2026-09-09
mode: authority-snapshot (bundle + restore drill; validation only, no refs changed)
source: gualtier-xu-co/Fudan-CourseLens-Private
source_sha: 4617aacc9c923ec11b944c76efa40476697027b3
destination: none (authority unchanged)
destination_sha: not-applicable
tree: 1612d13c9583f50d9f1a9195a6af1790e1068251
ci: not-run-this-session (hosted CI already green on this exact head: runs 34310050775, 34310072876, 34310405462)
review: not-applicable (no product diff; candidates recorded separately)
bundle_sha256: 324A8DA7FF0C180455C7005317995DB999EDCA554CFD60F9FE9243FB10795C7D
authority_after: gualtier-xu-co/Fudan-CourseLens-Private
notes: >
  New authority bundle (3 refs, complete history, 3388713 bytes) replaces
  the b7fec619-generation bundle for restore purposes; the old bundle is
  retained as history. Restore drill in a sibling layout (private/repo at
  4617aacc + public/worker-mirror at 4e8f1614/e34f82ae) reproduced the
  full local baseline: client 964/6/0 (138 subtests), worker 104 (42
  subtests), six mjs harnesses, git diff --check clean, default release
  gate exit 0, --require-release-ready exit 3 fail-closed. The
  reconstruction gate ran for real (2 passed) against the pinned signed
  template. Two unpushed local candidates prepared for the next
  authorized PR cycle: ledger sync 85c4fa3f and milestone test-step split
  180bc218 (see
  archive/external-artifacts/CourseLens-cleanroom-restore-set-20260909/authority-4617aacc-restore-manifest-20260909.md).
```

### 2026-09-09 — authorized closeout cycle: PRs #4/#5 merged, authority 0eefaa4

```yaml
id: authority-0eefaa4-20260909
date: 2026-09-09
mode: ordinary-development (authority advanced by authorized PR cycle)
source: gualtier-xu-co/Fudan-CourseLens-Private
source_sha: 4617aacc9c923ec11b944c76efa40476697027b3
destination: gualtier-xu-co/Fudan-CourseLens-Private
destination_sha: 0eefaa43a7a022403df2c36fff96190197975151
tree: a9500f59aafae6a28668a394fc02616056e06cde
ci: >
  PR#4 routine run 34312074885 (first attempt failed on a transient
  process-probe flake; identical-commit rerun success); PR#5 routine run
  34312077984 success; merged-main push runs 34313056609/34313066152
  (full five-job matrix) green
review: candidates pre-validated per authority-4617aacc-restore-manifest-20260909; hosted CI green on each exact head
bundle_sha256: 1F8DF26C8D82808B7669E2974383289B7E287DE46C0ABA751DF1E2F4AA6F0090
authority_after: gualtier-xu-co/Fudan-CourseLens-Private
notes: >
  Authorized closeout merged sequentially: (1) PR#4 synced the in-repo
  ledger with the planning ledger (merge 26719bb8); (2) PR#5 split the
  milestone Windows test step so pytest failures can no longer be masked
  (merge 0eefaa4). New authority bundle generated and drilled in a
  sibling layout: full local validation green (client 964/6/0, worker
  104, six mjs, gates 0/3 fail-closed, reconstruction gate real 2
  passed). Prepared for the next cycle (local, unpushed): macOS portable
  step split 046c209f and the following ledger-sync candidate.
  Follow-up recorded: acceptance-audit process probe is timeout-sensitive
  on loaded runners (PR#4 flake) — consider probe retry and
  report-in-assertion diagnosability in a reviewed PR. Evidence:
  archive/external-artifacts/CourseLens-cleanroom-restore-set-20260909/authority-0eefaa4-closeout-manifest-20260909.md
```

### 2026-10-02 — release-face repository rename (WINIT-1 case 1, PKG-WINIT1-A)

```yaml
id: release-face-rename-winit1-20261002
date: 2026-10-02
mode: pin-update-only (name literals; no source-tree authority change)
source: gualtier-xu/Fudan-CourseLens-Worker
destination: gualtier-xu/Fudan-CourseLens-Worker-Release
tree: n/a (mirror pins dc5cce70/c8ae0eaf/0ae496aa and trust trio r3/epoch1 unchanged)
ci: check_distribution_references exit 0 + targeted namespace/update/bootstrap tests + canonical venv-py310 full suite
review: WINIT1-DESIGN-1 frozen contract (product-winit1design1-result-20261002.md); controller acceptance pending
authority_after: unchanged (source authority stays Fudan-CourseLens-Private main)
notes: >
  User-approved rename of the official template + release-portal
  repository to free the preferred personal-executor name for the
  account owner. Pins updated in one commit (PKG-WINIT1-A); the
  GitHub-side rename and old-name reclaim are later gated steps
  (PKG-WINIT1-C) and old-name links redirect until reclaim; the bridge
  release (client-v0.1.1, legacy-shaped URLs) is PKG-WINIT1-B.
  Evidence: archive/external-artifacts/top-model-results-20260930/product-winit1a-result-20261002.md
```
