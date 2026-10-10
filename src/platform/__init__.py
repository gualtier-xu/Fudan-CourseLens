"""CourseLens 平台抽象层（macOS 启动地基）。

后端矩阵与接线指引见同目录 ``README.md``。分发一律调用时读
``sys.platform``（见 ``current_platform``），测试可安全打桩。

MAC-NIGHT-1 接线纪律：包导出改为 PEP 562 惰性解析——``import
src.platform.paths`` 这类叶子导入不再连带加载全部后端（autostart 的
xml.sax/urllib 链、credentials 的 subprocess 面等），客户端最早期路径
（根 ``path_utils`` → 数据目录）只付出叶子模块的成本；``from
src.platform import X`` 的既有用法（含平台 7 文件测试钉面）经
``__getattr__`` 保持逐字面兼容。
"""

from __future__ import annotations

from importlib import import_module

__all__ = [
    "MACOS",
    "OTHER",
    "WINDOWS",
    "MacOSFlockSingleInstanceLock",
    "MacOSKeychainCredentialsBackend",
    "MacOSLaunchdAutostartBackend",
    "PlatformNotSupportedError",
    "PlatformPaths",
    "WindowsDPAPICredentialsBackend",
    "WindowsMutexSingleInstanceLock",
    "WindowsSchtasksAutostartBackend",
    "acquire_single_instance",
    "current_platform",
    "data_dir",
    "get_autostart_backend",
    "get_credentials_backend",
    "platform_paths",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    # 公共符号 → (来源模块, 符号名)；首次访问时才 import 对应叶子。
    "MACOS": ("src.platform._core", "MACOS"),
    "OTHER": ("src.platform._core", "OTHER"),
    "WINDOWS": ("src.platform._core", "WINDOWS"),
    "PlatformNotSupportedError": ("src.platform._core", "PlatformNotSupportedError"),
    "current_platform": ("src.platform._core", "current_platform"),
    "MacOSLaunchdAutostartBackend": ("src.platform.autostart", "MacOSLaunchdAutostartBackend"),
    "WindowsSchtasksAutostartBackend": ("src.platform.autostart", "WindowsSchtasksAutostartBackend"),
    "get_autostart_backend": ("src.platform.autostart", "get_autostart_backend"),
    "MacOSKeychainCredentialsBackend": ("src.platform.credentials", "MacOSKeychainCredentialsBackend"),
    "WindowsDPAPICredentialsBackend": ("src.platform.credentials", "WindowsDPAPICredentialsBackend"),
    "get_credentials_backend": ("src.platform.credentials", "get_credentials_backend"),
    "PlatformPaths": ("src.platform.paths", "PlatformPaths"),
    "data_dir": ("src.platform.paths", "data_dir"),
    "platform_paths": ("src.platform.paths", "platform_paths"),
    "MacOSFlockSingleInstanceLock": ("src.platform.single_instance", "MacOSFlockSingleInstanceLock"),
    "WindowsMutexSingleInstanceLock": ("src.platform.single_instance", "WindowsMutexSingleInstanceLock"),
    "acquire_single_instance": ("src.platform.single_instance", "acquire_single_instance"),
}


def __getattr__(name: str):
    source = _EXPORTS.get(name)
    if source is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = source
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value  # 缓存：后续访问零查找成本
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
