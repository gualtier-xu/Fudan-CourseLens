"""Build (but never upload) a signed CourseLens Windows update."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import stat
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nacl.signing import SigningKey

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.distribution import MANIFEST_SOURCE_ID  # noqa: E402
RUNTIME_PREFIXES = ("frontend/", "src/", "shared/", "docs/repository-readmes/")
RUNTIME_FILES = {
    "credentials.py",
    "path_utils.py",
    "runtime-assets.json",
    "courselens-version.json",
    "start_fudan_courselens.ps1",
}
RUNTIME_TEXT_SUFFIXES = {"", ".css", ".html", ".js", ".md", ".py", ".ps1", ".svg", ".txt"}
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:EC |OPENSSH |RSA )?PRIVATE KEY-----"),
    re.compile(rb"\bgh[opsu]_[A-Za-z0-9]{36,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9]{32,}\b"),
)
COURSE_DATA_PATTERNS = (
    re.compile(rb"https://icourse\.fudan\.edu\.cn/[^\s\"']*(?:course_id|sub_id)=\d+", re.I),
    re.compile(rb"[\"'](?:course_id|sub_id)[\"']\s*:\s*[\"']?\d{4,}", re.I),
)
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit("release source provenance is unavailable") from exc


def _repository_name(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    if normalized.startswith("git@github.com:"):
        normalized = normalized.removeprefix("git@github.com:")
    elif normalized.startswith("ssh://git@github.com/"):
        normalized = normalized.removeprefix("ssh://git@github.com/")
    elif normalized.startswith("https://github.com/"):
        normalized = normalized.removeprefix("https://github.com/")
    else:
        return ""
    return normalized.removesuffix(".git").strip("/")


def _private_source_repository() -> str:
    """Authorized release-source repository, from the checkout-only sidecar.

    CO-NEUTRAL-1: the published manifest carries the neutral manifest source
    id, so the real private repository name lives only in
    ``config/ops-private.json`` next to this tool (never packaged, never
    published).  A missing or invalid sidecar fails closed — the builder
    must never guess which repository is authorized.
    """
    path = PROJECT_ROOT / "config" / "ops-private.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit("release source authorization is missing config/ops-private.json") from exc
    except (OSError, ValueError) as exc:
        raise SystemExit("release source authorization config is unreadable") from exc
    value = raw.get("source_repository") if isinstance(raw, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise SystemExit("release source authorization config is invalid")
    return value.strip()


def source_provenance(root: Path, allow_modified: frozenset[str] = frozenset()) -> dict[str, str]:
    status_entries = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    for entry in status_entries.decode("utf-8").split("\0"):
        if not entry:
            continue
        if set(entry[:2]) <= {"M", " "} and entry[3:] in allow_modified:
            continue
        if allow_modified:
            raise SystemExit(f"release source has an unauthorized modification: {entry[3:] or entry}")
        raise SystemExit("release source must be a clean commit")
    commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    tree = _git(root, "rev-parse", "--verify", "HEAD^{tree}").decode("ascii").strip()
    remote = _git(root, "config", "--get", "remote.origin.url").decode("utf-8").strip()
    if not HEX40_RE.fullmatch(commit) or not HEX40_RE.fullmatch(tree):
        raise SystemExit("release source provenance is invalid")
    authorized = _repository_name(f"https://github.com/{_private_source_repository()}.git")
    if _repository_name(remote) != authorized:
        raise SystemExit("release source repository is not authorized")
    return {"repository": MANIFEST_SOURCE_ID, "commit": commit, "tree": tree}


def _runtime_path_allowed(name: str) -> bool:
    suffix = Path(name).suffix.casefold()
    if name in RUNTIME_FILES:
        return suffix in {*RUNTIME_TEXT_SUFFIXES, ".json"}
    return (
        any(name.startswith(prefix) for prefix in RUNTIME_PREFIXES)
        and suffix in RUNTIME_TEXT_SUFFIXES
    )


def _is_link(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(
            path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
        )
    except AttributeError:
        return path.is_symlink()


def tracked_files(root: Path) -> list[Path]:
    result = _git(root, "ls-files", "-z").decode("utf-8").split("\0")
    files = [root / name for name in result if name and _runtime_path_allowed(name)]
    if not files:
        raise SystemExit("release runtime allowlist is empty")
    for path in files:
        resolved = path.resolve(strict=True)
        if not path.is_file() or _is_link(path) or root not in resolved.parents:
            raise SystemExit("release runtime contains an invalid file")
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def scan_outgoing_files(root: Path, files: list[Path]) -> None:
    """Fail closed before archive creation; never print matched content."""
    for path in files:
        name = path.relative_to(root).as_posix()
        try:
            content = path.read_bytes()
            content.decode("utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            raise SystemExit(f"outgoing content scan unavailable for {name}") from exc
        if b"\0" in content or any(pattern.search(content) for pattern in SECRET_PATTERNS):
            raise SystemExit(f"outgoing secret scan rejected {name}")
        if any(pattern.search(content) for pattern in COURSE_DATA_PATTERNS):
            raise SystemExit(f"outgoing course-data scan rejected {name}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--package-url", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--key-epoch", type=int, required=True)
    parser.add_argument("--signing-key-file", type=Path, required=True)
    # PKG-MIGRATION-1 (N5K §43 clock-drift trial variant): 168h proved tight for
    # student machines with skewed clocks (manifest_expired_or_future); the
    # trial configuration settles on the 14-day window.
    parser.add_argument("--expires-hours", type=int, default=336)
    parser.add_argument("--notes-file", type=Path, required=True)
    parser.add_argument(
        "--authenticode-signed-file", action="append", default=[],
        help=(
            "tracked packaged file that was Authenticode-signed after the recorded "
            "commit, so its clean-tree deviation is accepted and recorded in metadata"
        ),
    )
    parser.add_argument(
        "--minimum-security-version",
        help="Oldest client allowed to install this update; defaults to the trust policy minimum_version",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    files = tracked_files(root)
    tracked_names = {path.relative_to(root).as_posix() for path in files}
    authenticode_signed = frozenset(args.authenticode_signed_file)
    if (authenticode_signed - tracked_names):
        raise SystemExit("authenticode allowance is not a packaged runtime file")
    provenance = source_provenance(root, authenticode_signed)
    version_info = json.loads((root / "courselens-version.json").read_text(encoding="utf-8"))
    version = str(version_info["version"])
    if args.minimum_security_version:
        minimum_security_version = str(args.minimum_security_version)
    else:
        trust = json.loads((root / "config" / "client-update-trust.json").read_text(encoding="utf-8"))
        if trust.get("schema") != "courselens.client-update-trust.v2":
            raise SystemExit("client update trust policy is invalid")
        minimum_security_version = str(trust.get("minimum_version") or "")
    if not minimum_security_version:
        raise SystemExit("minimum security version is required")
    output = args.output.resolve()
    if output == root or root in output.parents:
        raise SystemExit("release output must be outside the source worktree")
    output.mkdir(parents=True, exist_ok=True)
    package_path = output / f"courselens-{version}-windows-x86_64.zip"
    scan_outgoing_files(root, files)
    file_hashes = {}
    for path in files:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        file_hashes[path.relative_to(root).as_posix()] = digest.hexdigest()
    metadata = {
        "schema": "courselens.client-package.v1", "version": version,
        "release_id": args.release_id, "platform": "windows", "architecture": "x86_64",
        "source": provenance, "files": file_hashes,
    }
    if authenticode_signed:
        metadata["authenticode_signed_files"] = sorted(authenticode_signed)
    with zipfile.ZipFile(package_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("courselens-package.json", json.dumps(metadata, sort_keys=True, indent=2))
        for path in files:
            archive.write(path, path.relative_to(root).as_posix())
    digest = hashlib.sha256()
    with package_path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    manifest = {
        "release_id": args.release_id, "version": version, "channel": str(version_info["channel"]),
        "platform": "windows", "architecture": "x86_64", "published_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=args.expires_hours)).isoformat(),
        "minimum_security_version": minimum_security_version,
        "source": provenance,
        "package": {
            "url": args.package_url, "size": package_path.stat().st_size,
            "sha256": digest.hexdigest(), "format": "zip-v1",
        },
        "release_notes": args.notes_file.read_text(encoding="utf-8")[:8000],
    }
    raw_key = base64.b64decode(args.signing_key_file.read_text(encoding="ascii").strip(), validate=True)
    if len(raw_key) != 32:
        raise SystemExit("signing key file must contain one base64 Ed25519 seed")
    signature = SigningKey(raw_key).sign(canonical(manifest)).signature
    envelope = {
        "schema": "courselens.client-update.v1", "manifest": manifest,
        "signature": {
            "key_id": args.key_id, "key_epoch": args.key_epoch,
            "value": base64.b64encode(signature).decode("ascii"),
        },
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"package": str(package_path), "manifest": str(manifest_path), "sha256": digest.hexdigest()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
