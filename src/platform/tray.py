"""macOS 窗口/托盘层（MAC-NIGHT-1）：NSStatusItem 托盘 + 单实例聚焦等价。

Windows 对应物：``src.runtime.tray_manager``（Shell_NotifyIcon）与
``src.runtime.window_shell.focus_running_instance_window``（EnumWindows/
SetForegroundWindow/FlashWindowEx）。本模块是同一职责的 macOS 面：

- 托盘 = AppKit ``NSStatusItem``；点击 = 拉回主窗，双职责菜单给「打开/退出」。
- 聚焦等价 = ``NSRunningApplication(processIdentifier=owner_pid).activate``；
  身份闭集与 Windows 完全一致（``server-instance.json`` 的属主 pid，
  由调用方读取证据文件后传入，本模块绝不自行全桌搜索）。

依赖纪律：**零新第三方依赖**——pyobjc(AppKit) 是 pywebview 在 macOS 的
既有依赖标记，打包链自带。AppKit 以注入承载（生产=真实模块对象，测试=桩）；
缺席/非 macOS 一律 ``available=False`` 优雅降级（Dock 激活语义兜底），
绝不阻塞窗口主链，也绝不静默假装可用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from src.platform._core import MACOS, PlatformNotSupportedError, current_platform

MACOS_TRAY_BACKEND = "macos-nsstatusitem"

# NSModalResponse / NSApplicationActivateOptions 只取用到的常量值（与
# AppKit 头文件一致，钉进桩合同；不在导入期触碰 AppKit）。
_NS_APPLICATION_ACTIVATE_IGNORING_OTHER_APPS = 1 << 1  # NSApplicationActivateIgnoringOtherApps


def _load_appkit():
    """Lazily resolve AppKit (pyobjc); None = 本机不可用（非 macOS/未捆绑）。"""
    if current_platform() != MACOS:
        return None
    try:
        import AppKit  # noqa: PLC0415 - 调用时懒加载，保持模块处处可导入
        import Foundation  # noqa: PLC0415

        return AppKit
    except Exception:
        return None


class MacOSStatusItemTray:
    """NSStatusItem 托盘控制器：形状与 ``tray_manager.TrayIcon`` 对齐。

    ``start`` 幂等、``stop`` 收口自有菜单项；一切 AppKit 句柄经
    ``appkit`` 注入承载（生产传 None = 懒加载真实 AppKit；测试注入桩对象）。
    ``available`` 为 False 时 ``start`` 诚实返回 False（托盘缺席=Dock 激活
    语义兜底），绝不抛异常阻塞窗口主链。
    """

    name = MACOS_TRAY_BACKEND

    def __init__(
        self,
        *,
        tooltip: str = "CourseLens",
        status_provider: Callable[[], str] | None = None,
        appkit=None,
    ):
        self._tooltip = str(tooltip or "CourseLens")
        self._status_provider = status_provider
        self._appkit = appkit
        self._item = None
        self._button = None
        self._handler = None
        self._icon_path: str | None = None
        self._on_open: Callable[[], None] | None = None
        self._on_exit: Callable[[], None] | None = None

    @property
    def available(self) -> bool:
        if self._appkit is not None:
            return True
        return _load_appkit() is not None

    @property
    def running(self) -> bool:
        """菜单项真挂上了才算在跑：学生必须有可见入口。"""
        return self._item is not None

    def start(
        self,
        icon_path: str | None,
        *,
        on_open: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> bool:
        if self.running:
            return True
        injected = self._appkit is not None
        appkit = self._appkit if injected else _load_appkit()
        if appkit is None:
            return False
        if not injected and current_platform() != MACOS:
            # 平台守卫只归生产懒加载路径；注入桩=形状合同测试，双平台可跑。
            raise PlatformNotSupportedError(
                "macos-nsstatusitem tray backend requires macOS"
            )
        self._icon_path = str(icon_path) if icon_path else None
        self._on_open = on_open
        self._on_exit = on_exit
        # 挂载序列（生产 AppKit 恒为真实现；桩合同逐形状钉在测试里）：
        # systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        #   → button() → setToolTip_ → 菜单两固定项（打开 / 退出）。
        bar = appkit.NSStatusBar.systemStatusBar()
        item = bar.statusItemWithLength_(appkit.NSVariableStatusItemLength)
        button = item.button()
        button.setToolTip_(self._tooltip)
        menu = appkit.NSMenu.new()
        open_item = appkit.NSMenuItem.new()
        open_item.setTitle_("打开 CourseLens")
        open_item.setTarget_(self._menu_target())
        open_item.setAction_("courselensOpen:")
        menu.addItem_(open_item)
        menu.addItem_(appkit.NSMenuItem.separatorItem())
        exit_item = appkit.NSMenuItem.new()
        exit_item.setTitle_("退出 CourseLens")
        exit_item.setTarget_(self._menu_target())
        exit_item.setAction_("courselensExit:")
        menu.addItem_(exit_item)
        item.setMenu_(menu)
        self._item = item
        self._button = button
        return True

    def _menu_target(self):
        """Click handler；AppKit 桩下允许省略（仅钉 argv/标题形状）。"""
        appkit = self._appkit
        if appkit is not None and hasattr(appkit, "Foundation"):
            foundation = appkit.Foundation
            owner = self

            class _Handler(foundation.NSObject):
                def courselensOpen_(self, _sender) -> None:  # noqa: N802
                    if owner._on_open is not None:
                        owner._on_open()

                def courselensExit_(self, _sender) -> None:  # noqa: N802
                    if owner._on_exit is not None:
                        owner._on_exit()

            self._handler = _Handler.new()
            return self._handler
        return None

    def stop(self) -> None:
        item, self._item = self._item, None
        self._button = None
        self._handler = None
        if item is None:
            return
        appkit = self._appkit if self._appkit is not None else _load_appkit()
        if appkit is not None:
            appkit.NSStatusBar.systemStatusBar().removeStatusItem_(item)

    def set_tooltip(self, text: str) -> bool:
        """更新悬停提示；未在跑=False 且零副作用（与 Windows 同口径）。"""
        if not self.running:
            return False
        self._tooltip = (text or "CourseLens")[:127]
        self._button.setToolTip_(self._tooltip)
        return True

    def notify(self, title: str, message: str, *, kind: str = "info") -> bool:
        """用户通知（媒体完成/失败族）；用户未配通知或 AppKit 缺席=False。

        v1 走 NSUserNotification 兼容面（macOS 12+ 系统自动桥接 UNUserNotification）；
        投递失败诚实返回 False——托盘/通知缺席绝不影响主流程。
        """
        del kind  # 视觉分级在 Windows 侧；mac v1 同一文案闭集，不做样式分叉
        appkit = self._appkit if self._appkit is not None else _load_appkit()
        if appkit is None or not hasattr(appkit, "NSUserNotification"):
            return False
        try:
            notification = appkit.NSUserNotification.new()
            notification.setTitle_(str(title or "CourseLens")[:127])
            notification.setInformativeText_(str(message or "")[:255])
            center = appkit.NSUserNotificationCenter.defaultUserNotificationCenter()
            if center is None:
                return False
            center.deliverNotification_(notification)
            return True
        except Exception:
            return False


def focus_owner_process(pid: int) -> bool:
    """把属主 pid 的进程拉回前台（focus_running_instance_window 的 macOS 等价腿）。

    只按证据文件给出的属主 pid 激活（身份闭集与 Windows 一致，绝不按名字
    全桌搜索）。AppKit 缺席/非 macOS/pid 无效=诚实 False（调用方降级为
    「已在运行」提示后安静退出）。
    """
    pid = int(pid or 0)
    if pid <= 0:
        return False
    if current_platform() != MACOS:
        return False
    try:
        import AppKit  # noqa: PLC0415

        application = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if application is None:
            return False
        return bool(
            application.activateWithOptions_(_NS_APPLICATION_ACTIVATE_IGNORING_OTHER_APPS)
        )
    except Exception:
        return False


def status_item_script_pins(source: str) -> dict[str, bool]:
    """源钉辅助：挂载序列关键形状是否在位（供测试与 CI 双端复用）。"""
    return {
        "statusItemWithLength": "statusItemWithLength_" in source,
        "variable_length": "NSVariableStatusItemLength" in source,
        "tooltip_before_menu": source.index("setToolTip_") < source.index("setMenu_"),
        "open_and_exit_actions": "courselensOpen:" in source and "courselensExit:" in source,
    }


__all__ = [
    "MACOS_TRAY_BACKEND",
    "MacOSStatusItemTray",
    "focus_owner_process",
    "status_item_script_pins",
]
