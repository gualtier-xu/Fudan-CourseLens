"""macOS 平台抽象层地基（MAC-PLATFORM）：核心类型与分发原语。

本包是 CourseLens 的平台抽象层：把 Windows 特定依赖（DPAPI、winreg、
schtasks、Win32 托盘/任务栏、命名互斥）收拢为「按 sys.platform 分发的
后端」，并为 macOS 提供平级后端（Keychain、flock、launchd），使 CI 在
无真机的情况下也能验证 macOS 后端逻辑（桩测接口与行为）。

纪律（2026-10-06 MAC-PLATFORM 包）：
- 现调用点零改动——抽象层就绪可用，接线留给后续 UI/托盘/打包车道。
- 本包不引入任何第三方依赖；macOS 后端走系统自带命令行（security /
  launchctl）与标准库（fcntl）。
"""

from __future__ import annotations

import sys

WINDOWS = "windows"
MACOS = "macos"
OTHER = "other"


class PlatformNotSupportedError(RuntimeError):
    """当前平台没有可用的后端实现（fail-closed，绝不静默降级）。"""


_SYS_PLATFORM_ALIASES = {"win32": WINDOWS, "darwin": MACOS}
_CANONICAL = (WINDOWS, MACOS, OTHER)


def current_platform(platform: str | None = None) -> str:
    """sys.platform（或显式覆盖）→ 规范平台名。

    覆盖值接受 raw ``sys.platform`` 值（``win32``/``darwin``）或规范名
    （``windows``/``macos``/``other``）；未知值归入 ``other``，工厂层
    fail-closed。运行时一律读 ``sys.platform``（调用时而非导入时），
    测试才能安全打桩。
    """
    value = sys.platform if platform is None else platform
    if value in _SYS_PLATFORM_ALIASES:
        return _SYS_PLATFORM_ALIASES[value]
    if value in _CANONICAL:
        return value
    return OTHER


__all__ = [
    "MACOS",
    "OTHER",
    "WINDOWS",
    "PlatformNotSupportedError",
    "current_platform",
]
