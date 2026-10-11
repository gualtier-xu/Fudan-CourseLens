"""Build the publishable orphan commit from an exported Worker mirror snapshot.

RELEASE-CHAIN-010-R2 canonical orphan builder (GITHUB-BIND-P0-1 §5.4).
R1 escaped because a hand-rolled orphan build copied only the allowlist
payload and silently dropped the five signed metadata files; this builder is
the one sanctioned path from export snapshot to orphan commit.  It fails
closed unless the snapshot carries the full metadata set, verifies the whole
signature chain, cross-checks every payload byte and the payload git tree,
then emits the orphan with the same identity form as every prior generation
(``generated Worker mirror <source12>``, zero parents).

Usage:
  python scripts/build_worker_mirror_orphan.py --snapshot <export-dir> --output <dir>
      [--source-repo <path>]     # read the source commit date for the orphan

Output JSON on stdout: orphan_commit / orphan_tree / manifest_sha256 /
payload_file_count / metadata checks.  Exit 0 only when every gate passed.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_worker_mirror_pin_gate import run_gate  # noqa: E402
from scripts.export_worker_mirror import REPARSE_POINT, _git_tree, _root_keys  # noqa: E402
from shared.protocol.mirror import (  # noqa: E402
    METADATA_PATHS,
    document_sha256,
    load_json_bytes,
    verify_manifest_document,
    verify_snapshot_files,
)


class OrphanBuildError(RuntimeError):
    pass


def _run(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    if completed.returncode != 0:
        raise OrphanBuildError(f"git {' '.join(args[:3])} failed: {completed.stderr.strip()[:300]}")
    return completed.stdout.strip()


def _assert_regular_file(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or (int(getattr(info, "st_file_attributes", 0)) & REPARSE_POINT):
        raise OrphanBuildError(f"metadata file is a link or reparse point: {path.name}")


def build_orphan(snapshot: Path, output: Path, source_repo: Path | None) -> dict[str, object]:
    snapshot = Path(snapshot).resolve()
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise OrphanBuildError("output directory must be empty")

    # 1. Metadata full set must exist (the R1 escape point — fail closed).
    missing_meta = sorted(name for name in METADATA_PATHS if not (snapshot / name).is_file())
    if missing_meta:
        raise OrphanBuildError(f"snapshot is missing mirror metadata {missing_meta}; "
                               "refusing to build a metadata-less mirror")
    for name in METADATA_PATHS:
        _assert_regular_file(snapshot / name)

    root_document = load_json_bytes((snapshot / "worker-mirror-root.json").read_bytes(), label="root")
    trust = load_json_bytes((snapshot / "worker-mirror-trust.json").read_bytes(), label="trust")
    trust_signature = load_json_bytes((snapshot / "worker-mirror-trust.sig").read_bytes(), label="trust signature")
    manifest = load_json_bytes((snapshot / "worker-mirror.manifest.json").read_bytes(), label="manifest")
    manifest_signature = load_json_bytes((snapshot / "worker-mirror.manifest.sig").read_bytes(), label="manifest signature")

    # 2. Full signature chain (trust -> release key -> manifest) + payload bytes.
    verified = verify_manifest_document(
        manifest, manifest_signature, trust, trust_signature,
        root_keys=_root_keys(root_document), supported_protocol_versions=("2",),
    )
    verify_snapshot_files(snapshot, verified["files"])
    manifest_sha256 = document_sha256(manifest)
    if manifest_sha256 != verified["manifest_sha256"]:
        raise OrphanBuildError("manifest digest drift between document and verification")

    # 3. Cross-check the payload git tree against the actual snapshot bytes.
    exported: list[tuple[str, str, bytes]] = []
    for record in verified["files"]:
        content = (snapshot / record["path"]).read_bytes()
        if len(content) != int(record["size"]):
            raise OrphanBuildError(f"payload size drift: {record['path']}")
        exported.append((record["path"], record["mode"], content))
    recomputed_tree = _git_tree(exported)
    if recomputed_tree != verified["payload_git_tree"]:
        raise OrphanBuildError("payload git tree mismatch between snapshot and manifest")

    source_commit = str(manifest["source"]["commit"]).lower()

    # 4. Materialize the orphan commit.
    shutil.copytree(snapshot, output)
    git_env = dict(os.environ)
    if source_repo is not None:
        date = _run(Path(source_repo).resolve(), "show", "-s", "--format=%cI", source_commit)
        git_env["GIT_AUTHOR_DATE"] = date
        git_env["GIT_COMMITTER_DATE"] = date
    _run(output, "init", "-q", "--initial-branch=main")
    _run(output, "config", "user.name", "CourseLens Worker Mirror")
    _run(output, "config", "user.email", "worker-mirror@users.noreply.github.com")
    _run(output, "add", "--all")
    _run(output, "commit", "-q", "-m", f"generated Worker mirror {source_commit[:12]}", env=git_env)

    parents = _run(output, "rev-list", "--parents", "-n", "1", "HEAD").split()
    if len(parents) != 1:
        raise OrphanBuildError(f"orphan commit has {len(parents) - 1} parents; refusing to publish")
    orphan_commit = parents[0]
    orphan_tree = _run(output, "rev-parse", "HEAD^{tree}")

    # 5. Post-build gates on the freshly created commit.
    gate = run_gate(str(output), orphan_commit, orphan_tree)
    if not gate["passed"]:
        raise OrphanBuildError(f"post-build metadata gate failed: {gate}")
    probe = subprocess.run(
        ["git", "-C", str(output), "cat-file", "-e", f"{source_commit}^{{commit}}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if probe.returncode == 0:
        raise OrphanBuildError("private source commit leaked into the public object database")

    return {
        "orphan_commit": orphan_commit,
        "orphan_tree": orphan_tree,
        "manifest_sha256": manifest_sha256,
        "payload_git_tree": verified["payload_git_tree"],
        "payload_file_count": len(verified["files"]),
        "metadata_file_count": len(METADATA_PATHS),
        "source_commit": source_commit,
        "signing_key_id": verified["key_id"],
        "trust_epoch": verified["trust_epoch"],
        "protocol_versions": verified["protocol_versions"],
        "gate": gate,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-repo", default=None, type=Path,
                        help="private repo path to inherit the source commit date")
    args = parser.parse_args()
    try:
        result = build_orphan(args.snapshot, args.output, args.source_repo)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
