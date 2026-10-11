"""Publish an exported Worker snapshot without exposing private Git history."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


class PublishError(RuntimeError):
    pass


def _failure_message(args: tuple[str, ...], stderr: str) -> str:
    lowered = str(stderr or "").lower()
    if args[:2] == ("git", "push") and (
        ".github/workflows" in lowered or "workflows permission" in lowered
    ):
        return (
            "Public push was rejected: the publisher App requires Contents and "
            "Workflows write permissions"
        )
    return f"Command failed without usable output: {args[0]}"


def _run(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(args), cwd=cwd, text=True, encoding="utf-8",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and completed.returncode != 0:
        raise PublishError(_failure_message(args, completed.stderr))
    return completed


def _copy_snapshot(snapshot: Path, destination: Path) -> None:
    for path in snapshot.rglob("*"):
        relative = path.relative_to(snapshot)
        target = destination / relative
        if path.is_symlink():
            raise PublishError("Snapshot contains a symbolic link")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def _clear_public_worktree(root: Path) -> None:
    resolved = root.resolve()
    for path in root.iterdir():
        if path.name == ".git":
            continue
        if path.resolve().parent != resolved:
            raise PublishError("Refusing to clear a path outside the public clone")
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def publish_snapshot(
    snapshot: Path,
    *,
    public_url: str,
    branch: str,
    source_commit: str,
    skip_pr: bool = False,
    public_repository: str = "",
    pr_title: str = "Update generated CourseLens Worker mirror",
) -> dict[str, str | bool]:
    snapshot = Path(snapshot).resolve()
    if not snapshot.is_dir() or not (snapshot / "worker-mirror.manifest.json").is_file():
        raise PublishError("Snapshot is missing the signed Worker manifest")
    if not branch or branch in {"main", "master"} or branch.startswith("-"):
        raise PublishError("Generated branch name is unsafe")
    if len(source_commit) != 40:
        raise PublishError("Private source commit is invalid")

    with tempfile.TemporaryDirectory(prefix="courselens-worker-publish-") as tmp:
        temporary = Path(tmp)
        orphan = temporary / "orphan"
        orphan.mkdir()
        _copy_snapshot(snapshot, orphan)
        _run(orphan, "git", "init", "--initial-branch=orphan")
        _run(orphan, "git", "config", "user.name", "CourseLens Worker Mirror")
        _run(orphan, "git", "config", "user.email", "worker-mirror@users.noreply.github.com")
        _run(orphan, "git", "add", "--all")
        _run(orphan, "git", "commit", "-m", f"generated Worker mirror {source_commit[:12]}")
        parents = _run(orphan, "git", "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
        if len(parents) != 1:
            raise PublishError("Generated snapshot commit is not an orphan")
        orphan_tree = _run(orphan, "git", "rev-parse", "HEAD^{tree}").stdout.strip()

        public = temporary / "public"
        _run(temporary, "git", "clone", "--no-tags", public_url, str(public))
        _run(public, "git", "config", "user.name", "CourseLens Worker Mirror")
        _run(public, "git", "config", "user.email", "worker-mirror@users.noreply.github.com")
        _run(public, "git", "checkout", "main")
        remote_branch = _run(
            public, "git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"
        ).stdout.strip()
        if remote_branch:
            generated_ref = "refs/courselens/generated"
            _run(public, "git", "fetch", "origin", f"refs/heads/{branch}:{generated_ref}")
            existing_tree = _run(public, "git", "rev-parse", f"{generated_ref}^{{tree}}").stdout.strip()
            if existing_tree != orphan_tree:
                raise PublishError("Generated branch already exists with a different tree")
            generated_commit = _run(public, "git", "rev-parse", generated_ref).stdout.strip()
            pushed = False
        else:
            _run(public, "git", "checkout", "-b", branch)
            _clear_public_worktree(public)
            _copy_snapshot(snapshot, public)
            _run(public, "git", "add", "--all")
            _run(public, "git", "commit", "-m", f"generated Worker mirror {source_commit[:12]}")
            generated_tree = _run(public, "git", "rev-parse", "HEAD^{tree}").stdout.strip()
            if generated_tree != orphan_tree:
                raise PublishError("Public branch tree differs from the orphan snapshot")
            private_object = _run(
                public, "git", "cat-file", "-e", f"{source_commit}^{{commit}}", check=False
            )
            if private_object.returncode == 0:
                raise PublishError("Private source commit unexpectedly exists in the public object database")
            _run(public, "git", "push", "--set-upstream", "origin", f"HEAD:refs/heads/{branch}")
            generated_commit = _run(public, "git", "rev-parse", "HEAD").stdout.strip()
            pushed = True

        pr_url = ""
        if not skip_pr:
            if not public_repository:
                raise PublishError("Public repository name is required to create a PR")
            existing = _run(
                public, "gh", "pr", "list", "--repo", public_repository,
                "--head", branch, "--base", "main", "--state", "open", "--json", "url",
            ).stdout
            rows = json.loads(existing or "[]")
            if rows:
                pr_url = str(rows[0].get("url") or "")
            else:
                body = (
                    "Generated from the private CourseLens monorepo by the protected Worker mirror "
                    "release workflow. Do not edit generated files manually."
                )
                pr_url = _run(
                    public, "gh", "pr", "create", "--repo", public_repository,
                    "--head", branch, "--base", "main", "--title", pr_title,
                    "--body", body,
                ).stdout.strip()
        return {
            "orphan_tree": orphan_tree,
            "generated_commit": generated_commit,
            "generated_tree": orphan_tree,
            "pushed": pushed,
            "pr_url": pr_url,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--public-url", required=True)
    parser.add_argument("--public-repository", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--pr-title", default="Update generated CourseLens Worker mirror")
    args = parser.parse_args()
    result = publish_snapshot(
        args.snapshot,
        public_url=args.public_url,
        public_repository=args.public_repository,
        branch=args.branch,
        source_commit=args.source_commit,
        pr_title=args.pr_title,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
