"""Offline, verify-only reconstruction from pinned Git objects (never checkouts)."""
from __future__ import annotations
import argparse, hashlib, json, subprocess, sys, tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ROOT = ROOT.parents[1] / "public" / "worker-mirror"
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from scripts.export_worker_mirror import EXPORTER_VERSION, _git_tree, _load_allowlist, _normalized_content
from scripts.migration_gate_evidence import EVIDENCE_SCHEMA, build_evidence
from shared.protocol.mirror import METADATA_PATHS, document_sha256, load_json_bytes, pretty_json, verify_manifest_document, verify_snapshot_files

GO = "WORKER-RECONSTRUCTION-GO"
SCHEMA = EVIDENCE_SCHEMA
HEX40 = set("0123456789abcdef")

def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

def _blob(root: Path, commit: str, path: str) -> bytes:
    return _git(root, "show", f"{commit}:{path}")

def _tracked(root: Path, commit: str) -> dict[str, tuple[str, str]]:
    result = {}
    for line in _git(root, "ls-tree", "-r", "--full-tree", commit).decode().splitlines():
        left, path = line.split("\t", 1); mode, kind, oid = left.split()
        if kind != "blob" or path in result: raise RuntimeError("pinned public tree is malformed")
        result[path] = (mode, oid)
    return result

def _runtime_root_keys(assets: dict[str, Any]) -> dict[str, str]:
    values = dict(assets.get("worker_mirror") or {}).get("root_keys")
    if not isinstance(values, dict) or not values: raise RuntimeError("production runtime root keys are unavailable")
    keys = {str(k).strip().lower(): str(v) for k, v in values.items()}
    if not all(keys.values()): raise RuntimeError("production runtime root keys are invalid")
    return keys

def _metadata_paths(manifest: dict[str, Any]) -> set[str]:
    result = set(METADATA_PATHS)
    declared = manifest.get("metadata", [])
    if declared is None: declared = []
    if not isinstance(declared, list) or any(not isinstance(p, str) or p not in METADATA_PATHS for p in declared):
        raise RuntimeError("manifest metadata declaration is invalid")
    return result | set(declared)

def _source_allowlist(source_commit: str, manifest: dict[str, Any]) -> dict[str, Any]:
    raw = _blob(ROOT, source_commit, "scripts/worker_mirror_allowlist.json")
    exporter = dict(manifest.get("exporter") or {})
    with tempfile.TemporaryDirectory(prefix="courselens-allowlist-") as tmp:
        path = Path(tmp) / "allowlist.json"; path.write_bytes(raw)
        allowlist = _load_allowlist(path)
    if str(exporter.get("version") or "") != EXPORTER_VERSION or hashlib.sha256(pretty_json(allowlist)).hexdigest() != str(exporter.get("allowlist_sha256") or "").lower():
        raise RuntimeError("manifest allowlist pin drifted")
    return allowlist

def _assert_private_absent(public_commit: str, source_commit: str) -> None:
    present = subprocess.run(["git", "cat-file", "-e", f"{source_commit}^{{commit}}"], cwd=PUBLIC_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if present: raise RuntimeError("public Worker object database contains private source commit")
    if source_commit in _git(PUBLIC_ROOT, "rev-list", public_commit).decode().splitlines():
        raise RuntimeError("public Worker history contains private source ancestry")

def reconstruct() -> dict[str, Any]:
    assets = load_json_bytes((ROOT / "runtime-assets.json").read_bytes(), label="runtime assets")
    worker = dict(assets.get("worker_mirror") or {}); active, paths = dict(worker.get("active") or {}), dict(worker.get("paths") or {})
    public_commit, public_tree = str(active.get("commit") or "").lower(), str(active.get("tree") or "").lower()
    if not PUBLIC_ROOT.is_dir() or any((len(v) != 40 or set(v) - HEX40) for v in (public_commit, public_tree)): raise RuntimeError("production public Worker pin is unavailable")
    tracked = _tracked(PUBLIC_ROOT, public_commit)
    if _git(PUBLIC_ROOT, "rev-parse", f"{public_commit}^{{tree}}").decode().strip() != public_tree: raise RuntimeError("runtime-assets full public tree pin drifted")
    names = {key: str(paths.get(key) or "") for key in ("manifest", "manifest_signature", "trust", "trust_signature")}
    if not all(names.values()) or not all(name in tracked for name in (*names.values(), "worker-mirror-root.json")): raise RuntimeError("pinned public metadata is unavailable")
    manifest = load_json_bytes(_blob(PUBLIC_ROOT, public_commit, names["manifest"]), label="manifest")
    signature = load_json_bytes(_blob(PUBLIC_ROOT, public_commit, names["manifest_signature"]), label="manifest signature")
    trust = load_json_bytes(_blob(PUBLIC_ROOT, public_commit, names["trust"]), label="trust")
    trust_signature = load_json_bytes(_blob(PUBLIC_ROOT, public_commit, names["trust_signature"]), label="trust signature")
    roots = _runtime_root_keys(assets)
    public_root = load_json_bytes(_blob(PUBLIC_ROOT, public_commit, "worker-mirror-root.json"), label="public root")
    root_file_keys = {str(x.get("key_id") or "").lower(): str(x.get("public_key") or "") for x in list(public_root.get("root_keys") or []) if isinstance(x, dict)}
    if root_file_keys != roots: raise RuntimeError("public root differs from production runtime root")
    verified = verify_manifest_document(manifest, signature, trust, trust_signature, root_keys=roots, supported_protocol_versions=active.get("protocol_versions") or [])
    if document_sha256(manifest) != str(active.get("manifest_sha256") or "").lower() or verified["key_id"] != str(active.get("signing_key_id") or "").lower() or verified["trust_epoch"] != int(active.get("trust_epoch") or 0): raise RuntimeError("production trust, manifest, key, epoch, or protocol pin drifted")
    source_commit = str(dict(manifest.get("source") or {}).get("commit") or "").lower()
    if len(source_commit) != 40 or set(source_commit) - HEX40: raise RuntimeError("manifest source commit is invalid")
    _assert_private_absent(public_commit, source_commit)
    allowlist = _source_allowlist(source_commit, manifest)
    mappings = {str(x["destination"]): dict(x) for x in allowlist["files"]}; files = list(verified["files"])
    if set(mappings) != {str(x["path"]) for x in files}: raise RuntimeError("signed file set does not match source-commit allowlist")
    if set(tracked) != {str(x["path"]) for x in files} | _metadata_paths(manifest): raise RuntimeError("pinned public tracked set differs from signed payload and metadata")
    extensions = {str(x).lower() for x in allowlist.get("text_extensions") or []}; materialized = []
    with tempfile.TemporaryDirectory(prefix="courselens-reconstruct-") as tmp:
        for name in ("one", "two"):
            output = Path(tmp) / name; output.mkdir(); records = []
            for entry in files:
                mapping = mappings[str(entry["path"])]
                raw = _normalized_content(str(mapping["source"]), _blob(ROOT, source_commit, str(mapping["source"])), extensions)
                target = output / str(entry["path"]); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw)
                records.append((str(entry["path"]), str(entry["mode"]), raw))
            verify_snapshot_files(output, files); materialized.append(records)
    first, second = materialized; payload_tree = _git_tree(first)
    if first != second or payload_tree != verified["payload_git_tree"]: raise RuntimeError("reconstruction is not deterministic or payload tree pin drifted")
    for path, mode, raw in first:
        if tracked[path][0] != mode or _blob(PUBLIC_ROOT, public_commit, path) != raw: raise RuntimeError("pinned public payload file mode or bytes drifted")
    return build_evidence("worker_reconstruction", "passed", source_commit=source_commit, public_commit=public_commit, public_tree=public_tree, payload_git_tree=payload_tree, file_count=len(files))

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--go", default=""); args = parser.parse_args(argv)
    if not args.go:
        print(json.dumps(build_evidence("worker_reconstruction", "preflight", required_go_sha256=hashlib.sha256(GO.encode()).hexdigest()), sort_keys=True)); return 0
    if args.go != GO: raise SystemExit("exact verifier GO token is required")
    print(json.dumps(reconstruct(), sort_keys=True, separators=(",", ":"))); return 0
if __name__ == "__main__": raise SystemExit(main())
