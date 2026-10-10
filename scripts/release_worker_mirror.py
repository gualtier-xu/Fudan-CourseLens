"""One-command protected Worker mirror release.

Dispatches the public ``publish-mirror`` workflow, waits for its generated
pull request, squash-merges it once the required checks pass, updates the
bundled client pin in ``runtime-assets.json``, and opens (and by default
merges) the private pin pull request. Requires a clean local ``main`` that
matches ``origin/main``.

The protected publishing path itself is unchanged: the workflow still exports
and signs inside the ``worker-mirror-release`` environment as the publisher
App, and both repositories still require their CI checks.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.protocol.mirror import document_sha256  # noqa: E402
from src.distribution import DISTRIBUTION_REPOSITORY, SOURCE_REPOSITORY  # noqa: E402

PUBLIC_REPO = DISTRIBUTION_REPOSITORY
PRIVATE_REPO = SOURCE_REPOSITORY
WORKFLOW = "publish-mirror.yml"


class ReleaseError(RuntimeError):
    pass


def _proxy_env() -> dict[str, str]:
    env = dict(os.environ)
    try:
        from credentials import CredentialStore

        store = CredentialStore()
        if store.has_secret("network_github_proxy"):
            proxy = store.load_secret("network_github_proxy").strip()
            if proxy:
                for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                    env[name] = proxy
    except Exception:
        pass
    return env


def _run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None, check: bool = True) -> tuple[int, str]:
    completed = subprocess.run(
        list(args), cwd=str(cwd or ROOT), text=True, encoding="utf-8",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    output = (completed.stdout or "").strip()
    if check and completed.returncode != 0:
        detail = (completed.stderr or output or "no output").strip()[:400]
        raise ReleaseError(f"{args[0]} failed ({completed.returncode}): {detail}")
    return completed.returncode, output


def _gh(*args: str, check: bool = True) -> str:
    code, output = _run("gh", *args, env=_proxy_env(), check=check)
    return output


def _wait(
    description: str, predicate: Callable[[], Any],
    *, timeout_seconds: float, interval: float = 20.0,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise ReleaseError(f"Timed out waiting for {description}")
        time.sleep(interval)


def update_pin(assets: dict[str, Any], release: dict[str, Any]) -> dict[str, Any]:
    """Promote one signed release to active, moving the old one to previous."""
    mirror = assets["worker_mirror"]
    mirror["previous"] = dict(mirror["active"])
    mirror["active"] = {
        "repository": str(release["repository"]),
        "commit": str(release["commit"]).lower(),
        "tree": str(release["tree"]).lower(),
        "manifest_sha256": str(release["manifest_sha256"]).lower(),
        "signing_key_id": str(release["signing_key_id"]),
        "trust_epoch": int(release["trust_epoch"]),
        "protocol_versions": list(release["protocol_versions"]),
    }
    return assets


def _public_manifest_digest(commit: str) -> str:
    payload = json.loads(
        _gh("api", f"repos/{PUBLIC_REPO}/contents/worker-mirror.manifest.json?ref={commit}")
    )
    manifest = json.loads(base64.b64decode(payload["content"]))
    return document_sha256(manifest)


def _wait_checks(repo: str, pr_number: str, *, timeout_seconds: float) -> None:
    def failed_or_done() -> str:
        code, output = _run("gh", "pr", "checks", pr_number, "--repo", repo, env=_proxy_env(), check=False)
        # exit 8: no checks reported yet; empty output with checks pending is
        # also "not done". Any explicit fail row aborts immediately.
        if "fail" in output:
            raise ReleaseError(f"PR {pr_number} checks failed:\n{output}")
        return "" if (code != 0 or not output or "pending" in output) else "done"
    _wait(f"PR {pr_number} checks", failed_or_done, timeout_seconds=timeout_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="", help="Approved private commit (default: local main HEAD)")
    parser.add_argument("--no-private-merge", action="store_true", help="Leave the private pin PR open")
    parser.add_argument("--timeout-seconds", type=float, default=25 * 60.0)
    args = parser.parse_args(argv)

    if _run("git", "status", "--porcelain")[1]:
        raise ReleaseError("The private working tree must be clean")
    if _run("git", "rev-parse", "--abbrev-ref", "HEAD")[1] != "main":
        raise ReleaseError("Run this from the private main branch")
    _run("git", "fetch", "--quiet", "origin", "main", env=_proxy_env())
    source = args.source or _run("git", "rev-parse", "HEAD")[1]
    if source != _run("git", "rev-parse", "origin/main")[1]:
        raise ReleaseError("Local main is not pushed; commit and push first")

    branch = f"generated/worker-mirror-{source[:7]}"
    started = time.time()
    _gh(
        "workflow", "run", WORKFLOW, "--repo", PUBLIC_REPO, "--ref", "main",
        "-f", f"source_commit={source}", "-f", f"branch_name={branch}",
    )

    def finished_run() -> dict[str, Any]:
        rows = json.loads(
            _gh("run", "list", "--repo", PUBLIC_REPO, "--workflow", WORKFLOW, "--limit", "5",
                "--json", "databaseId,status,conclusion,createdAt")
        )
        for row in rows:
            if row["status"] != "completed":
                continue
            created = calendar.timegm(time.strptime(row["createdAt"], "%Y-%m-%dT%H:%M:%SZ"))
            if created >= started - 60:
                return row
        return {}
    run = _wait("the publish workflow", finished_run, timeout_seconds=args.timeout_seconds)
    if run["conclusion"] != "success":
        raise ReleaseError(f"Publish workflow {run['databaseId']} concluded with {run['conclusion']}")

    prs = json.loads(
        _gh("pr", "list", "--repo", PUBLIC_REPO, "--head", branch, "--state", "open", "--json", "number,url")
    )
    if not prs:
        raise ReleaseError("The publish workflow completed without an open mirror PR")
    public_pr = str(prs[0]["number"])
    _wait_checks(PUBLIC_REPO, public_pr, timeout_seconds=args.timeout_seconds)
    _gh("pr", "merge", public_pr, "--repo", PUBLIC_REPO, "--squash")

    new_commit = _gh("api", f"repos/{PUBLIC_REPO}/commits/main", "--jq", ".sha")
    new_tree = _gh("api", f"repos/{PUBLIC_REPO}/commits/main", "--jq", ".commit.tree.sha")

    assets_path = ROOT / "runtime-assets.json"
    assets = json.loads(assets_path.read_text(encoding="utf-8"))
    current = assets["worker_mirror"]["active"]
    update_pin(assets, {
        "repository": current["repository"],
        "commit": new_commit,
        "tree": new_tree,
        "manifest_sha256": _public_manifest_digest(new_commit),
        "signing_key_id": current["signing_key_id"],
        "trust_epoch": current["trust_epoch"],
        "protocol_versions": current["protocol_versions"],
    })
    assets_path.write_text(json.dumps(assets, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    pin_branch = f"agent/release-{new_commit[:7]}"
    _run("git", "checkout", "-b", pin_branch)
    _run("git", "add", "runtime-assets.json")
    _run("git", "commit", "-m", f"chore: pin the approved signed Worker mirror release {new_commit[:7]}")
    _run("git", "push", "-u", "origin", pin_branch, env=_proxy_env())
    pr_url = _gh(
        "pr", "create", "--repo", PRIVATE_REPO, "--base", "main", "--head", pin_branch,
        "--title", f"Pin approved signed Worker mirror release {new_commit[:7]}",
        "--body", "Automated pin update from scripts/release_worker_mirror.py after the protected publish workflow and public checks passed.",
    )
    if args.no_private_merge:
        _run("git", "checkout", "main")
        print(json.dumps({"status": "pin_pr_open", "pr_url": pr_url, "public_commit": new_commit}, sort_keys=True))
        return 0
    private_pr = pr_url.rsplit("/", 1)[-1]
    _wait_checks(PRIVATE_REPO, private_pr, timeout_seconds=args.timeout_seconds)
    _gh("pr", "merge", private_pr, "--repo", PRIVATE_REPO, "--merge")
    _run("git", "checkout", "main")
    _run("git", "pull", "--ff-only", env=_proxy_env())
    print(json.dumps({"status": "released", "public_commit": new_commit, "public_tree": new_tree}, sort_keys=True))
    print("Next: restart the CourseLens service, and optionally run the process canary.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReleaseError as exc:
        print(f"release failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
