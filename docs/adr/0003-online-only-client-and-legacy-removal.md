# ADR 0003: Online-only client and legacy removal

- Status: accepted
- Date: 2026-07-21

> 2026-09-14 注记：文中历史性提及的"三模式"字幕计算已被取代——live 契约只有
> 唯一 `automatic` 策略（DeepSeek Key 配置时 AI 校对，否则非 AI 回退）。
> ADR 正文按当时决策原样保留。

## Context

The signed Worker mirror is now the approved compute path, but the private
client still contains a second local-download and local-compute product path.
That path includes downloads, PotPlayer, FFmpeg, SenseVoice,
RapidOCR, local subtitle workers, old unversioned routes and a large facade in
`archive.py`. It is no longer part of the supported online product and keeps
several incompatible state models alive.

## Decision

The supported client is online-only:

- Course media is streamed through the bounded, no-store media session.
- ASR/OCR/summary/chapter/quiz work runs through the signed Worker mirror and
  encrypted Artifact protocol.
- The public client API is `/api/v3/*` plus `/api/health`; old unversioned
  download, local subtitle, summary, merge and file-operation routes are
  removed rather than retained as compatibility tombstones.
- Catalog state moves from `manifest.json` to a versioned CatalogRepository in
  `state.db`; the one-shot migration runs before legacy files are deleted.
- `archive.py` is replaced by `src/app.py` and `python -m src`; services,
  repositories, routers and frontend workspaces own their respective domains.
- Windows is the real acceptance platform. macOS receives CI import, test,
  compile and packaging compatibility through a platform credential adapter.

## Destructive-cleanup gate

Before deleting any legacy code, data or environment, all of the following
must be observed from backend evidence:

1. The current signed Worker tree and manifest verify.
2. A complete lecture and two additional real authorized lectures complete all
   selected outputs with valid citations and hashes.
3. The real quality-set thresholds in the acceptance matrix pass.
4. Clean Windows onboarding and browser checks pass, using Hyper-V only as the
   documented fallback when Windows Sandbox is unavailable.
5. Network interruption, signature/tree/protocol failure, cancellation,
   restart recovery and duplicate operations fail closed and recover correctly.
6. Legacy rollback and signed restore have both completed, with no active run,
   Artifact, Mailbox content, token lease or temporary secret.

The 14-day/20-lecture observation remains a product-release gate but is not a
prerequisite for this accelerated cleanup. Git history and the final hashed
deletion report are retained as audit evidence; no media cold backup is kept.

## Consequences

Existing users with legacy local media will receive a one-time state-only
migration. Media and local compute artifacts are deliberately not copied. The
legacy path cannot be re-enabled after this release; recovery uses the signed
Worker mirror and the database/credential backups created during the gate.

## 2026-07-28 accelerated beta amendment

The destructive-cleanup safety gate is narrowed to the approved signed Worker
tree, encrypted echo zero-residue audit, real three-mode bounded compute,
browser boundary matrix, database integrity, protected state/credential backup,
zero active work, and allowlisted path checks.

Complete-lecture compute and manual transcript/OCR/chapter/smart-playback
scoring remain optional and are not represented as passed. Clean Windows
Sandbox onboarding remains a
known release limitation because the host's signed
`WindowsSandboxRemoteSession.exe` fails before CourseLens starts when loading
`WinRT.Runtime 2.2.0.0`. CourseLens does not modify that system package. These
two release-quality limitations do not weaken credential, signature, Git tree,
Mailbox, Artifact, database, process, or deletion-path safety checks.
