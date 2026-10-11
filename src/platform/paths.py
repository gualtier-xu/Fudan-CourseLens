"""数据目录后端：Windows %LOCALAPPDATA% vs macOS ~/Library/Application Support。

与根 ``path_utils`` 的既有口径对齐：``COURSELENS_DATA_DIR`` 环境变量永远
最高优先（测试/集成注入临时目录的既有通道不变）；平台默认目录只在无覆盖
时生效。接线（后续车道）时只需让平台入口改用本模块取数据目录。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from src.platform._core import MACOS, WINDOWS, PlatformNotSupportedError, current_platform

APP_DIR_NAME = "CourseLens"
DATA_DIR_ENV = "COURSELENS_DATA_DIR"


@dataclass(frozen=True)
class PlatformPaths:
    """一个平台的数据面路径集合（只描述，不做任何 IO）。"""

    data_dir: Path

    @property
    def credentials_file(self) -> Path:
        return self.data_dir / "credentials.json"

    @property
    def instance_lock_file(self) -> Path:
        return self.data_dir / "instance.lock"

    @property
    def instance_evidence_file(self) -> Path:
        return self.data_dir / "server-instance.json"

    def subdir(self, name: str) -> Path:
        return self.data_dir / name


def _windows_default_dir(environ) -> Path:
    local_app_data = str(environ.get("LOCALAPPDATA") or "").strip()
    if local_app_data:
        return Path(local_app_data) / APP_DIR_NAME
    # 无 LOCALAPPDATA 的极端环境：按同构路径回退到用户主目录。
    return Path.home() / "AppData" / "Local" / APP_DIR_NAME


def _macos_default_dir(environ) -> Path:
    home = str(environ.get("HOME") or "").strip()
    base = Path(home) if home else Path.home()
    return base / "Library" / "Application Support" / APP_DIR_NAME


def platform_paths(platform: str | None = None, *, environ=None) -> PlatformPaths:
    """返回平台数据面路径；COURSELENS_DATA_DIR 覆盖优先于一切平台默认。"""
    environ = os.environ if environ is None else environ
    configured = str(environ.get(DATA_DIR_ENV) or "").strip()
    if configured:
        return PlatformPaths(data_dir=Path(configured).expanduser().resolve())
    resolved = current_platform(platform)
    if resolved == WINDOWS:
        return PlatformPaths(data_dir=_windows_default_dir(environ))
    if resolved == MACOS:
        return PlatformPaths(data_dir=_macos_default_dir(environ))
    raise PlatformNotSupportedError(
        f"no default data directory for platform: {resolved}"
    )


def data_dir(platform: str | None = None, *, environ=None) -> Path:
    """便捷入口：平台数据目录。"""
    return platform_paths(platform, environ=environ).data_dir


__all__ = [
    "APP_DIR_NAME",
    "DATA_DIR_ENV",
    "PlatformPaths",
    "data_dir",
    "platform_paths",
]
