"""Release gate: the pinned Worker mirror tree MUST contain the full metadata set.

RELEASE-CHAIN-010-R2 persistent gate (GITHUB-BIND-P0-1 §5.4 prescription).
R1 escaped because the pin was advanced onto a mirror tree that had lost all
five signed metadata files; this gate fails closed before any pin advance and
again after the mirror lands on the public repository.

Usage:
  python scripts/check_worker_mirror_pin_gate.py --repo <local-path-or-URL> --commit <sha>
      [--expect-tree <sha>]

Exit codes: 0 = gate PASS, 1 = gate FAIL (missing/drifted metadata),
2 = probe error (also fail-closed: treat as FAIL).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.protocol.mirror import METADATA_PATHS  # noqa: E402


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def _list_tree(repo: str, commit: str) -> tuple[str, list[str]]:
    """Return (tree_sha, sorted file paths) for `commit` in a local or remote repo."""
    if Path(repo).exists():
        tree = _git("-C", repo, "rev-parse", f"{commit}^{{tree}}")
        if tree.returncode != 0:
            raise RuntimeError(f"rev-parse failed: {tree.stderr.strip()[:200]}")
        listing = _git("-C", repo, "ls-tree", "-r", "--name-only", commit)
        if listing.returncode != 0:
            raise RuntimeError(f"ls-tree failed: {listing.stderr.strip()[:200]}")
        return tree.stdout.strip(), sorted(listing.stdout.splitlines())
    with tempfile.TemporaryDirectory(prefix="courselens-pin-gate-") as tmp:
        work = Path(tmp) / "probe"
        work.mkdir()
        for args in (
            ["init", "-q", str(work)],
            ["-C", str(work), "remote", "add", "origin", repo],
            ["-C", str(work), "config", "extensions.partialClone", "origin"],
        ):
            r = _git(*args)
            if r.returncode != 0:
                raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:200]}")
        fetched = _git("-C", str(work), "fetch", "--depth", "1", "--filter=blob:none",
                       "origin", commit)
        if fetched.returncode != 0:
            raise RuntimeError(
                f"fetch {commit[:12]} failed (unreachable on remote?): {fetched.stderr.strip()[:200]}")
        head = _git("-C", str(work), "rev-parse", "FETCH_HEAD")
        if head.stdout.strip() != commit.lower():
            raise RuntimeError(
                f"remote FETCH_HEAD {head.stdout.strip()[:12]} != requested {commit[:12]}")
        tree = _git("-C", str(work), "rev-parse", "FETCH_HEAD^{tree}")
        if tree.returncode != 0:
            raise RuntimeError(f"rev-parse failed: {tree.stderr.strip()[:200]}")
        listing = _git("-C", str(work), "ls-tree", "-r", "--name-only", "FETCH_HEAD")
        if listing.returncode != 0:
            raise RuntimeError(f"ls-tree failed: {listing.stderr.strip()[:200]}")
        return tree.stdout.strip(), sorted(listing.stdout.splitlines())


def run_gate(repo: str, commit: str, expect_tree: str | None) -> dict[str, object]:
    tree, files = _list_tree(repo, commit)
    present = {name for name in files if name in METADATA_PATHS}
    missing = sorted(METADATA_PATHS - present)
    result: dict[str, object] = {
        "gate": "worker-mirror-pin-tree-metadata-fullset",
        "repo": repo,
        "commit": commit,
        "tree": tree,
        "file_count": len(files),
        "metadata_present": sorted(present),
        "metadata_missing": missing,
        "expect_tree": expect_tree,
        "tree_match": (tree == expect_tree.lower()) if expect_tree else None,
        "passed": not missing and (expect_tree is None or tree == expect_tree.lower()),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="local repo path or remote URL")
    parser.add_argument("--commit", required=True, help="commit whose tree must carry metadata")
    parser.add_argument("--expect-tree", default=None, help="fail unless tree equals this sha")
    args = parser.parse_args()
    try:
        result = run_gate(args.repo, args.commit.lower(), args.expect_tree)
    except Exception as exc:  # noqa: BLE001 — probe errors are fail-closed
        result = {
            "gate": "worker-mirror-pin-tree-metadata-fullset",
            "repo": args.repo, "commit": args.commit,
            "passed": False, "error": f"{type(exc).__name__}: {exc}",
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["passed"] else (1 if "error" not in result else 2)


if __name__ == "__main__":
    raise SystemExit(main())
