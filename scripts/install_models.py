"""Install pinned upstream ASR models with SHA-256 verification."""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import requests


MODELS = {
    "sensevoice": {
        "archive": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
        "sha256": "7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e",
    },
    # M4 Paraformer（ASRBENCH-1 A5 立项，M4-ENABLE-1 U1 实测钉，234 MB tar.bz2）。
    "paraformer": {
        "archive": "sherpa-onnx-paraformer-zh-2023-09-14.tar.bz2",
        "sha256": "9c49fd9c6fb63de8e18c1054cf3d100f804741b7e608e187923cd8ff09fa9f03",
    },
}
BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"


def _model_directories(root: Path, name: str) -> list[Path]:
    candidates = [
        path for path in root.iterdir()
        if path.is_dir() and (path / "tokens.txt").is_file()
    ] if root.is_dir() else []
    markers = {
        "sensevoice": "sense-voice",
        "paraformer": "paraformer",
    }
    marker = markers[name]
    return sorted(path for path in candidates if marker in path.name)


def _safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:bz2") as handle:
        members: list[tuple[tarfile.TarInfo, Path]] = []
        for member in handle.getmembers():
            relative = PurePosixPath(member.name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or not (member.isdir() or member.isfile())
            ):
                raise RuntimeError("model archive contains an unsafe member")
            target = destination.joinpath(*relative.parts).resolve()
            if destination not in target.parents and target != destination:
                raise RuntimeError("model archive contains an unsafe path")
            members.append((member, target))
        for member, target in members:
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = handle.extractfile(member)
            if source is None:
                raise RuntimeError("model archive member could not be read")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)


def _install(name: str, spec: dict[str, str], root: Path) -> Path:
    marker = root / f".{name}-{spec['sha256']}.ready"
    if marker.is_file():
        directories = _model_directories(root, name)
        if directories:
            return directories[0]
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="model-install-") as temporary:
        archive = Path(temporary) / spec["archive"]
        digest = hashlib.sha256()
        with requests.get(f"{BASE}/{spec['archive']}", stream=True, timeout=60) as response:
            response.raise_for_status()
            with archive.open("wb") as output:
                for block in response.iter_content(1024 * 1024):
                    digest.update(block)
                    output.write(block)
        if digest.hexdigest() != spec["sha256"]:
            raise RuntimeError(f"{name} model checksum mismatch")
        _safe_extract(archive, root)
    marker.write_text(spec["sha256"], encoding="ascii")
    directories = _model_directories(root, name)
    if not directories:
        raise RuntimeError(f"{name} model directory was not extracted")
    return directories[0]


def main() -> None:
    root = Path(os.environ.get("COURSELENS_MODEL_ROOT", ".models")).resolve()
    # 空 sha256 = 条目尚未实测钉，整条跳过。
    installed = {
        name: _install(name, spec, root)
        for name, spec in MODELS.items()
        if spec["sha256"]
    }
    environment = Path(os.environ.get("GITHUB_ENV", root / "models.env"))
    with environment.open("a", encoding="utf-8") as output:
        for name, directory in installed.items():
            output.write(f"{name.upper()}_MODEL_DIR={directory}\n")


if __name__ == "__main__":
    main()
