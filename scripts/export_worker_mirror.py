"""Export and sign a deterministic public Worker snapshot from the monorepo."""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.protocol.mirror import (
    MANIFEST_SCHEMA,
    METADATA_PATHS,
    document_sha256,
    load_json_bytes,
    pretty_json,
    sha256_hex,
    sign_document,
    verify_trust_document,
)
from shared.protocol.wire import SUPPORTED_PROTOCOL_VERSIONS
# CO-NEUTRAL: the neutral manifest source id is registry truth — import it
# instead of carrying a literal (check_distribution_references.py enforces).
from src.distribution import MANIFEST_SOURCE_ID as NEUTRAL_SOURCE_ID


ALLOWLIST_PATH = ROOT / "scripts" / "worker_mirror_allowlist.json"
RELEASE_ROOT = ROOT / "release" / "worker-mirror"
EXPORTER_VERSION = "1.0.0"
ROOT_SCHEMA = "courselens.worker-mirror.root.v1"
# CO-NEUTRAL: the real private repository name lives only in the
# checkout-only sidecar (same pattern as src/distribution.py); standalone
# contexts fall back to the neutral manifest source id.
OPS_PRIVATE_PATH = ROOT / "config" / "ops-private.json"
PRIVATE_IMPORT_ROOTS = {"archive", "credentials", "manifest", "path_utils", "src"}
REPARSE_POINT = 0x400


class ExportError(RuntimeError):
    pass


def _export_source_repository() -> str:
    """Resolve the manifest source repository identity (CO-NEUTRAL).

    Mirrors the ``config/ops-private.json`` sidecar pattern from
    ``src/distribution.py``: the real private repository name exists only in
    the checkout-only sidecar.  A missing sidecar (standalone/packaged
    context) falls back to the neutral manifest source id with a stderr
    hint; a sidecar that exists but is unreadable or malformed fails closed.
    """
    try:
        raw = json.loads(OPS_PRIVATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(
            "export_worker_mirror: config/ops-private.json sidecar missing; "
            f"falling back to neutral source id {NEUTRAL_SOURCE_ID}",
            file=sys.stderr,
        )
        return NEUTRAL_SOURCE_ID
    except (OSError, ValueError) as exc:
        raise ExportError("config/ops-private.json sidecar is unreadable or invalid") from exc
    if not isinstance(raw, dict) or set(raw) != {"source_repository"}:
        raise ExportError("config/ops-private.json sidecar is malformed")
    value = raw["source_repository"]
    if not isinstance(value, str) or not value.strip():
        raise ExportError("config/ops-private.json sidecar is malformed")
    return value.strip()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _is_reparse(stat_result: os.stat_result) -> bool:
    return bool(int(getattr(stat_result, "st_file_attributes", 0)) & REPARSE_POINT)


def _load_allowlist(path: Path = ALLOWLIST_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportError("Worker mirror allowlist is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != "courselens.worker-mirror.allowlist.v1":
        raise ExportError("Worker mirror allowlist schema is unsupported")
    if str(value.get("exporter_version") or "") != EXPORTER_VERSION:
        raise ExportError("Worker mirror allowlist requires a different exporter version")
    files = value.get("files")
    if not isinstance(files, list) or not files:
        raise ExportError("Worker mirror allowlist is empty")
    return value


def _normalized_mapping(value: dict[str, Any]) -> dict[str, str]:
    source = str(value.get("source") or "").replace("\\", "/")
    destination = str(value.get("destination") or "").replace("\\", "/")
    mode = str(value.get("mode") or "")
    for label, path in (("source", source), ("destination", destination)):
        pure = PurePosixPath(path)
        if not path or pure.is_absolute() or ".." in pure.parts:
            raise ExportError(f"Allowlist {label} path is unsafe: {path}")
    if not (source.startswith("worker/") or source.startswith("shared/protocol/") or source in {"shared/__init__.py", "shared/evidence_contract.py"}):
        raise ExportError(f"Allowlist source is outside the public roots: {source}")
    if destination in METADATA_PATHS:
        raise ExportError(f"Allowlist destination collides with mirror metadata: {destination}")
    if mode not in {"100644", "100755"}:
        raise ExportError(f"Allowlist mode is unsupported: {mode}")
    return {"source": source, "destination": destination, "mode": mode}


def _enumerate_source_files(repo_root: Path) -> set[str]:
    actual: set[str] = set()
    roots = (repo_root / "worker", repo_root / "shared" / "protocol")
    for root in roots:
        if not root.is_dir():
            raise ExportError(f"Required Worker source root is missing: {root.relative_to(repo_root)}")
        for path in root.rglob("*"):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                raise ExportError(f"Worker source contains a link or reparse point: {path.relative_to(repo_root)}")
            if path.is_dir():
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ExportError(f"Worker source contains a non-regular file: {path.relative_to(repo_root)}")
            actual.add(path.relative_to(repo_root).as_posix())
    for shared_source in ("shared/__init__.py", "shared/evidence_contract.py"):
        shared_path = repo_root / shared_source
        if shared_path.is_symlink() or _is_reparse(shared_path.lstat()) or not shared_path.is_file():
            raise ExportError(f"{shared_source} is missing or unsafe")
        actual.add(shared_source)
    return actual


def _check_python_imports(source: str, raw: bytes) -> None:
    if not source.endswith(".py"):
        return
    try:
        tree = ast.parse(raw.decode("utf-8"), filename=source)
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise ExportError(f"Exported Python is invalid: {source}") from exc
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.append(node.module)
        for module in modules:
            if module.split(".", 1)[0] in PRIVATE_IMPORT_ROOTS:
                raise ExportError(f"Exported Python depends on private module {module}: {source}")


def _normalized_content(source: str, raw: bytes, text_extensions: set[str]) -> bytes:
    suffix = Path(source).suffix.lower()
    is_text = suffix in text_extensions or Path(source).name in {
        "LICENSE", "README", ".editorconfig", ".gitattributes", ".gitignore",
    }
    if not is_text:
        raise ExportError(f"Binary or unknown file type is not allowed: {source}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExportError(f"Exported text is not UTF-8: {source}") from exc
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    normalized = text.encode("utf-8")
    _check_python_imports(source, normalized)
    return normalized


def _git_object_id(kind: str, content: bytes) -> bytes:
    header = f"{kind} {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).digest()


def _git_tree(files: list[tuple[str, str, bytes]]) -> str:
    root: dict[str, Any] = {}
    for path, mode, content in files:
        parts = PurePosixPath(path).parts
        node = root
        for part in parts[:-1]:
            existing = node.setdefault(part, {})
            if not isinstance(existing, dict):
                raise ExportError(f"File/directory collision in export: {path}")
            node = existing
        if parts[-1] in node:
            raise ExportError(f"Duplicate export destination: {path}")
        node[parts[-1]] = (mode, content)

    def build(node: dict[str, Any]) -> bytes:
        entries: list[tuple[bytes, bytes]] = []
        for name, value in node.items():
            encoded = name.encode("utf-8")
            if isinstance(value, dict):
                oid = build(value)
                sort_key = encoded + b"/"
                entry = b"40000 " + encoded + b"\0" + oid
            else:
                mode, content = value
                oid = _git_object_id("blob", content)
                sort_key = encoded
                entry = mode.encode("ascii") + b" " + encoded + b"\0" + oid
            entries.append((sort_key, entry))
        body = b"".join(entry for _, entry in sorted(entries, key=lambda item: item[0]))
        return _git_object_id("tree", body)

    return build(root).hex()


def _root_keys(root_document: dict[str, Any]) -> dict[str, str]:
    if root_document.get("schema") != ROOT_SCHEMA:
        raise ExportError("Worker mirror root schema is unsupported")
    values = root_document.get("root_keys")
    if not isinstance(values, list) or not values:
        raise ExportError("Worker mirror root contains no public keys")
    result: dict[str, str] = {}
    for item in values:
        if not isinstance(item, dict):
            raise ExportError("Worker mirror root key entry is invalid")
        key_id = str(item.get("key_id") or "").strip().lower()
        public_key = str(item.get("public_key") or "")
        try:
            decoded = base64.b64decode(public_key.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise ExportError("Worker mirror root public key is invalid") from exc
        if not key_id or len(decoded) != 32 or key_id in result:
            raise ExportError("Worker mirror root key is invalid or duplicated")
        result[key_id] = public_key
    return result


def export_snapshot(
    repo_root: Path,
    output: Path,
    *,
    signing_private_key: str,
    signing_key_id: str,
    source_commit: str,
    worker_tree: str,
    allowlist_path: Path = ALLOWLIST_PATH,
    release_root: Path = RELEASE_ROOT,
) -> dict[str, Any]:
    repo_root = Path(repo_root).resolve()
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ExportError("Worker mirror output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    if not _inside(output, output):
        raise ExportError("Worker mirror output path is invalid")

    allowlist = _load_allowlist(allowlist_path)
    mappings = [_normalized_mapping(dict(item)) for item in allowlist["files"]]
    sources = [item["source"] for item in mappings]
    destinations = [item["destination"] for item in mappings]
    if len(sources) != len(set(sources)) or len(destinations) != len(set(destinations)):
        raise ExportError("Worker mirror allowlist contains duplicate paths")
    actual = _enumerate_source_files(repo_root)
    expected = set(sources)
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unknown:
        raise ExportError(f"Unknown Worker source file is not allowlisted: {unknown[0]}")
    if missing:
        raise ExportError(f"Allowlisted Worker source file is missing: {missing[0]}")

    text_extensions = {str(value).lower() for value in allowlist.get("text_extensions") or []}
    exported: list[tuple[str, str, bytes]] = []
    records: list[dict[str, Any]] = []
    for item in sorted(mappings, key=lambda value: value["destination"].encode("utf-8")):
        source_path = (repo_root / item["source"]).resolve()
        if not _inside(source_path, repo_root) or source_path.is_symlink() or _is_reparse(source_path.lstat()):
            raise ExportError(f"Allowlisted source escapes the repository or is linked: {item['source']}")
        content = _normalized_content(item["source"], source_path.read_bytes(), text_extensions)
        target = (output / item["destination"]).resolve()
        if not _inside(target, output):
            raise ExportError(f"Export destination escapes the output root: {item['destination']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if item["mode"] == "100755":
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        exported.append((item["destination"], item["mode"], content))
        records.append({
            "path": item["destination"],
            "mode": item["mode"],
            "size": len(content),
            "sha256": sha256_hex(content),
        })

    root_document = load_json_bytes((release_root / "worker-mirror-root.json").read_bytes(), label="root")
    trust = load_json_bytes((release_root / "worker-mirror-trust.json").read_bytes(), label="trust")
    trust_signature = load_json_bytes((release_root / "worker-mirror-trust.sig").read_bytes(), label="trust signature")
    release_keys = verify_trust_document(
        trust, trust_signature, root_keys=_root_keys(root_document)
    )
    release_key = release_keys.get(str(signing_key_id).strip().lower())
    if not release_key or release_key["status"] != "active":
        raise ExportError("Configured release signing key is not active in trust metadata")

    allowlist_sha = sha256_hex(pretty_json(allowlist))
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "exporter": {"version": EXPORTER_VERSION, "allowlist_sha256": allowlist_sha},
        "source": {
            "repository": _export_source_repository(),
            "commit": str(source_commit).lower(),
            "worker_tree": str(worker_tree).lower(),
        },
        "protocol": {"supported_versions": list(SUPPORTED_PROTOCOL_VERSIONS)},
        "payload": {
            "files": records,
            "tree_sha256": sha256_hex(json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")),
            "git_tree": _git_tree(exported),
        },
    }
    signature = sign_document(
        manifest, key_id=str(signing_key_id).strip().lower(), private_key=signing_private_key
    )
    metadata = {
        "worker-mirror-root.json": root_document,
        "worker-mirror-trust.json": trust,
        "worker-mirror-trust.sig": trust_signature,
        "worker-mirror.manifest.json": manifest,
        "worker-mirror.manifest.sig": signature,
    }
    for name, value in metadata.items():
        (output / name).write_bytes(pretty_json(value))
    return {
        "manifest": manifest,
        "manifest_sha256": document_sha256(manifest),
        "payload_git_tree": manifest["payload"]["git_tree"],
        "files": records,
    }


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo_root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    return completed.stdout.strip()


def _assert_clean_release_source(repo_root: Path) -> None:
    status = _git(
        repo_root, "status", "--porcelain", "--", "worker", "shared/protocol",
        "shared/__init__.py", "shared/evidence_contract.py",
        "scripts/worker_mirror_allowlist.json", "release/worker-mirror",
    )
    if status:
        raise ExportError("Worker mirror release source must be committed and clean")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--signing-key-id", default=os.environ.get("WORKER_MIRROR_SIGNING_KEY_ID", ""))
    parser.add_argument(
        "--signing-private-key",
        default=os.environ.get("WORKER_MIRROR_SIGNING_PRIVATE_KEY", ""),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--local-dpapi-key-id",
        help="Load a locally generated release key from CredentialStore without printing it",
    )
    args = parser.parse_args()
    if args.local_dpapi_key_id:
        from credentials import CredentialStore
        args.signing_key_id = args.local_dpapi_key_id
        args.signing_private_key = CredentialStore().load_secret(
            "worker_mirror_release_signing_private:" + args.local_dpapi_key_id
        )
    if not args.signing_key_id or not args.signing_private_key:
        raise SystemExit("Release signing key id/private key are required")
    _assert_clean_release_source(ROOT)
    result = export_snapshot(
        ROOT,
        args.output,
        signing_private_key=args.signing_private_key,
        signing_key_id=args.signing_key_id,
        source_commit=_git(ROOT, "rev-parse", "HEAD"),
        worker_tree=_git(ROOT, "rev-parse", "HEAD:worker"),
    )
    print(json.dumps({
        "manifest_sha256": result["manifest_sha256"],
        "payload_git_tree": result["payload_git_tree"],
        "file_count": len(result["files"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
