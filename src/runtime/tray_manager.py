"""系统托盘图标（夜10-A APP-SHAPE-2 T1）：Win32 Shell_NotifyIcon 的最小实现。

pywebview 没有托盘能力，这里用 ctypes 自建一个隐藏消息窗承载托盘图标：
双击/左键 → 打开主窗口；右键 → 状态行 + 「打开 CourseLens / 退出 CourseLens」
菜单；长任务终态可经 notify() 发一枚气球通知（SYSTRAY-IMPL-2 P2-7）；
Explorer 重启（TaskbarCreated 广播）后自动重挂；任务栏深浅色切换
（WM_SETTINGCHANGE / ImmersiveColorSet）按注册表口径热换深底变体图标。
全部失败路径降级为「没有托盘图标」，绝不影响主窗口或服务。线程自管：
start/stop 幂等，stop 按 PostThreadMessage(WM_QUIT) 收口自己的线程与窗口，
不碰任何别人创建的东西。
"""

from __future__ import annotations

import ctypes
import threading
from pathlib import Path
from typing import Callable

user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") and hasattr(ctypes.windll, "user32") else None
shell32 = ctypes.windll.shell32 if user32 is not None else None
kernel32 = ctypes.windll.kernel32 if user32 is not None else None

WM_APP_TRAY = 0x8000  # WM_APP
NIM_ADD = 0x0
NIM_MODIFY = 0x1
NIM_DELETE = 0x2
NIF_MESSAGE = 0x1
NIF_ICON = 0x2
NIF_TIP = 0x4
NIF_INFO = 0x10
NIIF_INFO = 0x1
NIIF_ERROR = 0x3
NIN_SELECT = 0x400
NIN_BALLOONUSERCLICK = 0x405
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
WM_CLOSE = 0x0010
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_QUIT = 0x0012
WM_USER = 0x0400
WM_SETTINGCHANGE = 0x001A
SM_CXSMICON = 49
SM_CYSMICON = 50
IDM_TRAY_OPEN = 1
IDM_TRAY_EXIT = 2
MF_STRING = 0x0
MF_GRAYED = 0x1
MF_SEPARATOR = 0x800
# 深浅色自适应口径（SYSTRAY-IMPL-3）：跟随系统，零设置项。
THEME_REG_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
THEME_REG_VALUE = "AppsUseLightTheme"
DARK_TASKBAR_VARIANT_SUFFIX = "-darktaskbar"
IMMERSIVE_COLOR_SET = "ImmersiveColorSet"


def apps_use_light_theme() -> bool | None:
    """AppsUseLightTheme 注册表口径：True=浅色 / False=深色 / None=读不到。

    只读不改；无 winreg、键缺失、值类型意外一律 None，调用方回退现役单一
    图标（SYSTRAY-IMPL-3 的回退语义：选择失败绝不丢托盘入口）。
    """
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, THEME_REG_SUBKEY) as key:
            value, _reg_type = winreg.QueryValueEx(key, THEME_REG_VALUE)
    except OSError:
        return None
    return bool(value) if value in (0, 1) else None


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_ulong),
        ("dwStateMask", ctypes.c_ulong),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_ulong),
        ("guidItem", ctypes.c_ubyte * 16),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


# WNDCLASSW 的回调字段在类体导入期求值 ctypes.WINFUNCTYPE（仅 Windows
# 提供），旧无条件顶层定义曾让整个客户端在非 Windows 上一 importing 就崩
# （与 MAC-CRED 修 credentials.py 顶层 wintypes 同族病灶）。定义以同款
# hasattr 谓词收窄：Windows 上字段/布局逐位同旧（ABI 形状钉
# tests/test_tray_struct_shapes.py 在 Windows CI 继续全量执行）；非 Windows
# 不定义本类——唯一使用点 _run() 在 available=False 时不可达，托盘按既有
# 口径降级为「没有托盘图标」。
if hasattr(ctypes, "WINFUNCTYPE"):

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", ctypes.c_uint),
            ("lpfnWndProc", ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
                ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_longlong
            )),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", ctypes.c_void_p),
            ("hIcon", ctypes.c_void_p),
            ("hCursor", ctypes.c_void_p),
            ("hbrBackground", ctypes.c_void_p),
            ("lpszMenuName", ctypes.c_wchar_p),
            ("lpszClassName", ctypes.c_wchar_p),
        ]


class TrayIcon:
    """单实例托盘控制器：start 幂等、stop 收口自有线程/窗口/图标。"""

    def __init__(
        self, *, tooltip: str = "CourseLens",
        status_provider: Callable[[], str] | None = None,
    ) -> None:
        self._tooltip = tooltip
        self._status_provider = status_provider
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._hwnd = 0
        self._hicon = 0
        self._on_open: Callable[[], None] | None = None
        self._on_exit: Callable[[], None] | None = None
        self._stopped = threading.Event()
        self._wndproc_ref = None  # 保住回调引用防 GC
        self._wm_taskbar_created = 0
        self._icon_added = False
        self._tip_lock = threading.Lock()
        self._icon_path: str | None = None  # 调用方给的基准图标（选择口径的锚）
        self._active_icon_path: str | None = None  # 当前实际装载的图标路径
        self._hicon_is_shared = False  # IDI_APPLICATION 兜底图标不可 DestroyIcon

    @property
    def available(self) -> bool:
        return user32 is not None and shell32 is not None

    @property
    def running(self) -> bool:
        """图标真挂上了才算在跑：线程活着但图标失败=学生没有入口，不算。"""
        return self._thread is not None and self._icon_added

    def set_tooltip(self, text: str) -> bool:
        """更新托盘悬停提示（夜10-A #9：在飞任务数被动呈现）。

        NIM_MODIFY 只带 NIF_TIP；未在跑（图标没挂上）=False 且零副作用，
        绝不因此触碰 shell。Shell_NotifyIconW 由 shell 自行封送，允许从轮询
        线程调用；提示文本变更在此串行化。
        """
        if not (self.available and self._icon_added and self._hwnd):
            return False
        with self._tip_lock:
            self._tooltip = (text or "CourseLens")[:127]
            data = self._icon_data()
            data.uFlags = NIF_TIP
            return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)))

    def notify(self, title: str, text: str, *, kind: str = "info") -> bool:
        """长任务终态托盘通知（SYSTRAY-IMPL-2 P2-7）：NIM_MODIFY+NIF_INFO 气球。

        只发终态单枚，过程零播报（被动呈现原则的限定面例外）。托盘未挂=
        False 静默跳过，绝不因此触碰 shell；kind="error" 用错误图钉。现代
        Windows 上气球自动进通知中心，归属=显式 AUMID（P1-3 已落）。与
        set_tooltip 共用结构体，串行化在同把锁里。
        """
        if not (self.available and self._icon_added and self._hwnd):
            return False
        with self._tip_lock:
            data = self._icon_data()
            data.uFlags = NIF_INFO
            data.szInfo = (text or "")[:255]
            data.szInfoTitle = (title or "CourseLens")[:63]
            data.dwInfoFlags = NIIF_ERROR if kind == "error" else NIIF_INFO
            return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)))

    def start(
        self, icon_path: str | Path | None, *,
        on_open: Callable[[], None], on_exit: Callable[[], None],
        status_provider: Callable[[], str] | None = None,
    ) -> bool:
        if not self.available or self._thread is not None:
            return False
        self._on_open = on_open
        self._on_exit = on_exit
        self._status_provider = status_provider
        self._ready.clear()
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._run, args=(str(icon_path) if icon_path else None,),
            name="courselens-tray", daemon=True,
        )
        self._thread.start()
        return self._ready.wait(5.0)

    def stop(self) -> None:
        self._stopped.set()
        thread, self._thread = self._thread, None
        if thread is not None and self._thread_id:
            if user32 is not None:
                # WM_QUIT 必须发给线程本身（GetMessage 循环收口），不是发给窗口。
                # PostThreadMessageW 在 user32（kernel32 只有线程句柄/ID 族）。
                user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            thread.join(timeout=5.0)

    # --- 托盘线程 -----------------------------------------------------------

    def _run(self, icon_path: str | None) -> None:
        import ctypes.wintypes as wintypes

        try:
            self._thread_id = kernel32.GetCurrentThreadId()
            user32.DefWindowProcW.restype = ctypes.c_longlong
            # lParam 在 64 位进程里是指针：不声明 argtypes 时 ctypes 默认按
            # 32 位 c_int 收参，凡携指针的广播（WM_GETMINMAXINFO/SettingChange
            # 族）在兜底调用处 OverflowError（回调异常被吞、默认处理失守）。
            user32.DefWindowProcW.argtypes = [
                ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_longlong,
            ]
            class_name = "CourseLensTrayHost"
            hinstance = kernel32.GetModuleHandleW(None)
            wndproc = ctypes.WINFUNCTYPE(
                ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_longlong
            )(self._wndproc)
            self._wndproc_ref = wndproc
            wc = WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = wndproc
            wc.hInstance = hinstance
            wc.lpszClassName = class_name
            if not user32.RegisterClassW(ctypes.byref(wc)):
                return
            # 顶层隐形窗（不用 HWND_MESSAGE：message-only 窗收不到
            # TaskbarCreated 广播，Explorer 重启后图标会丢）。
            hwnd = user32.CreateWindowExW(
                0, class_name, "CourseLens Tray Host", 0, 0, 0, 0, 0,
                None, None, hinstance, None,
            )
            if not hwnd:
                return
            self._hwnd = hwnd
            self._wm_taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
            self._icon_path = icon_path
            resolved = self._resolve_icon_path(icon_path)
            self._active_icon_path = resolved
            self._hicon = self._load_icon(resolved)
            if not self._add_icon():
                self._cleanup_window()
                return
            self._icon_added = True
            self._ready.set()
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            if self._hwnd:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._icon_data()))
            self._cleanup_window()
        except Exception:
            self._ready.set()  # 别让 start 卡满超时
        finally:
            self._ready.set()

    def _cleanup_window(self) -> None:
        hwnd, self._hwnd = self._hwnd, 0
        if hwnd and user32 is not None:
            user32.DestroyWindow(hwnd)

    def _resolve_icon_path(self, icon_path: str | None) -> str | None:
        """深浅色变体选择（SYSTRAY-IMPL-3）：深色任务栏换兄弟变体，浅色沿用现役。

        浅色形态=现役图标本身（浅底观感已取证达标），深色形态=同目录
        `-darktaskbar` 变体。口径读不到 / 变体文件不在 / 未给图标路径一律
        原样返回——回退语义=现役单一 ICO，绝不因选择失败丢托盘入口。
        """
        if not icon_path:
            return None
        if apps_use_light_theme() is not False:
            return icon_path
        base = Path(icon_path)
        candidate = base.with_name(f"{base.stem}{DARK_TASKBAR_VARIANT_SUFFIX}{base.suffix or '.ico'}")
        return str(candidate) if candidate.is_file() else icon_path

    def _is_theme_change(self, lparam: int) -> bool:
        """WM_SETTINGCHANGE 的 lParam 指向变更类名；主题切换="ImmersiveColorSet"。"""
        if not lparam:
            return False
        try:
            return ctypes.c_wchar_p(lparam).value == IMMERSIVE_COLOR_SET
        except Exception:
            return False

    def _refresh_theme_icon(self) -> None:
        """主题切换热换图标（SYSTRAY-IMPL-3）：重选口径→装载→NIM_MODIFY。

        口径结果与在挂一致=零动作；新图标装载失败=保留旧图标（绝不因主题
        切换丢托盘入口）；Shell 侧修改失败不回滚——句柄已换代，真正的
        Explorer 重建由 TaskbarCreated 兜底重挂。仅在托盘线程调用。
        """
        if not self._icon_path:
            return
        resolved = self._resolve_icon_path(self._icon_path)
        if resolved == self._active_icon_path:
            return
        old_shared = self._hicon_is_shared
        handle = self._load_icon(resolved)
        if not handle or self._hicon_is_shared:
            self._hicon_is_shared = old_shared
            return
        old, self._hicon = self._hicon, handle
        self._active_icon_path = resolved
        if self._icon_added and self._hwnd:
            with self._tip_lock:
                data = self._icon_data()
                data.uFlags = NIF_ICON
                shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
        if old and not old_shared and user32 is not None:
            user32.DestroyIcon(old)

    def _load_icon(self, icon_path: str | None) -> int:
        if icon_path and Path(icon_path).is_file():
            # 托盘按系统小图标尺寸显式装载（SYSTRAY-IMPL-1 P1-2）：shell 直接
            # 取 ICO 里的 16/20/24px 原生变体，高 DPI 缩放下不再由 32px 大图
            # 缩小发糊；尺寸读不到（0）回退原 cx=cy=0 行为。
            cx = user32.GetSystemMetrics(SM_CXSMICON)
            cy = user32.GetSystemMetrics(SM_CYSMICON)
            if cx > 0 and cy > 0:
                handle = user32.LoadImageW(
                    None, icon_path, IMAGE_ICON, cx, cy, LR_LOADFROMFILE
                )
            else:
                handle = user32.LoadImageW(
                    None, icon_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE
                )
            if handle:
                self._hicon_is_shared = False
                return handle
        self._hicon_is_shared = True
        return user32.LoadIconW(None, 32512)  # IDI_APPLICATION 兜底

    def _icon_data(self) -> NOTIFYICONDATAW:
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = ctypes.c_void_p(self._hwnd)
        data.uID = 1
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        data.uCallbackMessage = WM_APP_TRAY
        data.hIcon = ctypes.c_void_p(self._hicon)
        data.szTip = self._tooltip[:127]
        return data

    def _add_icon(self) -> bool:
        return bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._icon_data())))

    def _wndproc(self, hwnd, message: int, wparam: int, lparam: int) -> int:
        if message == WM_APP_TRAY:
            event = lparam & 0xFFFF
            if event == WM_LBUTTONDBLCLK and self._on_open is not None:
                self._on_open()
            # 左键单击同样打开（SYSTRAY-IMPL-1 P1-1）：单击是托盘图标的主
            # 操作惯例，双击保留（show/restore 幂等，多触发一次无害）。
            elif event == WM_LBUTTONUP and self._on_open is not None:
                self._on_open()
            # 气球通知被点（P2-7）：点开=回窗口看任务，通知不给死胡同。
            elif event in (NIN_SELECT, NIN_BALLOONUSERCLICK) and self._on_open is not None:
                self._on_open()
            elif event == WM_RBUTTONUP:
                self._show_context_menu()
            return 0
        if message == getattr(self, "_wm_taskbar_created", None):
            # Explorer 重启后任务栏重建：原图标句柄已失效，重挂一次；重挂前
            # 按当前主题口径重选变体（Explorer 重启与主题切换可能同期发生）。
            if self._hwnd:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._icon_data()))
                self._refresh_theme_icon()
                self._add_icon()
            return 0
        if message == WM_SETTINGCHANGE:
            # 任务栏深浅色切换（SYSTRAY-IMPL-3）：只响应 ImmersiveColorSet
            # 广播热换变体图标，其余 SettingChange 零动作。隐形宿主窗吞掉即可。
            if self._is_theme_change(lparam):
                self._refresh_theme_icon()
            return 0
        if message == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _build_context_menu(self) -> int:
        """构建右键菜单句柄（SYSTRAY-IMPL-2 P1-4）：首组=灰显状态行。

        状态行文本来自可选的 status_provider（在托盘线程同步调用，内部
        自带 1s 查询超时上限）：provider 未接/抛错/返回空串 = 状态行整个
        不显示，菜单照常打开，绝不因状态查询失焦。
        """
        menu = user32.CreatePopupMenu()
        if not menu:
            return 0
        if self._status_provider is not None:
            try:
                text = str(self._status_provider() or "").strip()
            except Exception:
                text = ""
            if text:
                user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, text[:127])
                user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, IDM_TRAY_OPEN, "打开 CourseLens")
        user32.AppendMenuW(menu, MF_STRING, IDM_TRAY_EXIT, "退出 CourseLens")
        return menu

    def _show_context_menu(self) -> None:
        if self._hwnd == 0:
            return
        # 托盘菜单焦点规则：先 SetForegroundWindow，否则菜单不收首击。
        user32.SetForegroundWindow(self._hwnd)
        menu = self._build_context_menu()
        if not menu:
            return
        import ctypes.wintypes as wintypes

        point = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        # TPM_RETURNCMD(0x100)|TPM_RIGHTBUTTON(0x2)：直接返回所选项。
        chosen = user32.TrackPopupMenuEx(
            menu, 0x100 | 0x2, point.x, point.y, self._hwnd, None
        )
        user32.DestroyMenu(menu)
        if chosen == IDM_TRAY_OPEN and self._on_open is not None:
            self._on_open()
        elif chosen == IDM_TRAY_EXIT and self._on_exit is not None:
            self._on_exit()
