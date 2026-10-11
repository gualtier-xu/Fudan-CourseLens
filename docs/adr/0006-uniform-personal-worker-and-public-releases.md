# ADR 0006: Uniform personal Worker and public releases

- Status: accepted
- Date: 2026-09-07
- Supersedes: ADR 0005

## Context

ADR 0005 introduced an owner-only `signed-template-direct` dispatch mode: when
the configured Worker repository equaled the public template
`gualtier-xu-co/Fudan-CourseLens-Worker`, the owner could dispatch real tasks straight to
the public template. Operating two dispatch modes cost more than it saved: the
client carried a second state machine (prepare / rollback / finalize with
DPAPI checkpoints), every integrity, probe, and canary path had to branch on
`is_template` versus `managed`, and the public repository's role was ambiguous
between "signed release pin" and "personal executor".

The mode is now retired. Every account, including the canonical template
owner, uses the same topology: a personal public
`Fudan-CourseLens-Worker` for compute and a personal private
`Fudan-CourseLens-Mailbox` for sealed transport. Dispatch is driven entirely
by the `github_worker_repo` credential secret, so removing the mode special
case requires no dispatch-call changes. A stale legacy binding (configured
Worker equal to the bundled template repository) must still be recognized and
fail closed instead of being silently reinterpreted.

## Decision

1. The public repository `gualtier-xu-co/Fudan-CourseLens-Worker` is the signed template
   and, going forward, the public release portal. It is never an executor.
   `check_worker_integrity` and `repair_worker` fail closed with the code
   `personal_worker_migration_required` when the configured Worker repository
   equals the bundled template repository, and the personal-worker integrity
   path is the only validation path.
2. The GitHub App installation contract shrinks from three repositories to
   exactly two: the personal Worker and the personal Mailbox
   (`REQUIRED_INSTALLATION_REPOSITORIES`). Authorization (device flow) only
   requires that an installation exists; bootstrap requires the exact
   two-repository selection after the repositories have been created. Because
   GitHub rejects App user tokens for add/remove installation repository APIs
   (and classic PATs are out of scope), correcting the selection is a manual
   step in GitHub's installation settings. This is the shortest recoverable
   path: bootstrap is idempotent and re-runs converge once the user adjusts
   the selection, and the connection probe surfaces the exact missing and
   unexpected repositories plus the settings URL under the closed code
   `installation_scope_not_exact`. One limit remains: if a foreign repository
   already occupies the preferred `Fudan-CourseLens-Worker` name,
   `_available_name` creates a suffixed repository instead, and the exact
   two-repository selection cannot converge until the user removes the
   foreign repository from the installation selection. *(2026-10-02 drift
   note: this described the historical suffixed-fallback behavior; the
   current implementation deliberately has no suffixed fallback name and
   fails closed with `managed_repository_name_conflict` instead — see
   `_ensure_managed_repository` in `src/remote/github_app.py`.)*
3. The migrate / rollback / finalize state machine and its checkpoint secrets
   are removed. The remaining migration surface is read-only: a retirement
   audit (`build_signed_template_retirement_audit`) reports zero-state
   observations and whether a legacy direct binding is still configured.
   The former prepare/rollback/finalize scripts print a closed retirement
   notice and exit 3 without touching state.
4. Client release distribution moves to the public repository's GitHub
   Releases. The updater stays disabled until the version and channel gates
   issue a separate GO; nothing in this decision enables it. As shipped, the
   implementation works as follows:
   - The policy names exactly `github.com`,
     `release-assets.githubusercontent.com`, and
     `objects.githubusercontent.com` as allowed download hosts, and the stable
     manifest URL is policy-pinned to
     `https://github.com/gualtier-xu-co/Fudan-CourseLens-Worker/releases/latest/download/courselens-windows-manifest.json`.
   - Every request hop — including the first — is re-validated (HTTPS, port
     443, exact allowlisted host, no credentials or fragment, every resolved
     address globally routable, IP re-resolved per hop) with a ceiling of
     three redirect hops; anything else fails closed with
     `download_redirect_untrusted`. Package URLs must be
     `https://github.com/gualtier-xu-co/Fudan-CourseLens-Worker/releases/download/client-v<semver>/<asset>`.
     No Authorization header is ever attached, so anonymous cross-host
     redirect following cannot leak credentials.
   - Publishing is draft-first: the gated `publish-draft-release` job creates
     a draft release on the public repository, uploads the built assets, and
     stops; a human verifies signature, SHA-256, size, Authenticode and notes,
     then publishes manually. Released assets are immutable; bad releases are
     fixed by a new `client-v<semver>` release, never by replacing assets.
   - Draft creation authenticates with a dedicated minimal publisher identity
     (a GitHub App or fine-grained PAT with `contents:write` on the public
     repository only) stored as `COURSELENS_RELEASE_PUBLISHER_TOKEN` in the
     `client-release-production` environment. It is never the student App and
     never an all-repositories grant.

## Consequences

- One dispatch topology and one integrity contract; the `is_template`,
  `worker_dispatch_sha`, and `template_release_required` branches disappear.
  Personal-worker integrity is tree-based only: the personal Worker `main`
  commit may differ from the pin as long as its tree equals the signed tree.
- The process canary validates the personal Worker pin from
  `actual_commit` (whose tree equals the bundled pin) and, in preflight and
  postflight, audits the public template's `process.yml`/`echo.yml` runs to
  prove the template stays quiescent.
- Legacy direct state converges by re-running bootstrap, which recreates any
  missing personal repositories and then demands the exact installation
  selection; until then every probe reports `bootstrap` as the remediation.
- The signed release pipeline (manifest, signatures, trust epoch) is
  unchanged: it now feeds only tree verification and the public release
  portal.

## 2026-09-30 note (release-face namespace flip)

The public release face has moved to the official release account: the
repository referenced throughout the historical body above by its old name
(旧称) `gualtier-xu-co/Fudan-CourseLens-Worker` is now
`gualtier-xu/Fudan-CourseLens-Worker-Release`. The release workflow, the ops manual,
the frontend help link, and the registry pins point at the official
repository; the old-name repository is retained only as a read-only
historical mirror and receives no further releases. The historical body is
preserved as written and still carries the old name deliberately.

## 2026-10-02 note (template/portal rename to the `-Release` suffix)

The release repository itself is renamed (WINIT-1 case 1, user-approved):
`gualtier-xu/Fudan-CourseLens-Worker` becomes
`gualtier-xu/Fudan-CourseLens-Worker-Release`, freeing the preferred
`Fudan-CourseLens-Worker` name for the account owner's personal executor
repository. All registry pins (distribution registry, update service,
shipped trust template, bundled mirror pin) already point at the new
name; the GitHub-side rename is a separate gated step, and GitHub
redirects the old name until the owner reclaims it. The 2026-09-30 note
above names the current (post-rename) repository.
