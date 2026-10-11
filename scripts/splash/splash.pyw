"""CourseLens boot splash carrier (LAUNCH-FIX-1 C; two-phase since N15-W1).

pythonw.exe runs this file from the launcher tree before the full trust
walk: the launcher has already quick-checked this file and its splash.html
sibling against the hashes pinned in launcher-trust.json, so only the two
splash assets execute pre-walk; the walkthrough then proceeds unchanged.

Two phases, one process (N15-W1 双相方案):

- Phase 1 (native, <150ms target): a plain ctypes Win32 window paints a
  static brand mark (navy surface, gold ring, ivory wordmark) before any
  heavy import happens - the pythonnet/.NET WebView2 stack costs ~1.2s to
  load, which is exactly the dead time the splash exists to cover.
- Phase 2 (HTML continuation): a worker thread imports webview and shows
  the animated splash.html; the moment it reports shown it posts a
  takeover message and the native window destroys itself.  Every phase-2
  failure is a degradation, never a regression: the native static mark
  keeps serving until a normal dismissal path closes it.

The window is a frameless, top-most, non-focus-grabbing little shell that
fades out when the launcher signals readiness by creating the flag file
given as argv[1]. The carrier owns nothing else:

- no server, no lifecycle, no data access (WebView2 runs in private mode
  with a TEMP user-data folder, so nothing is ever written into the
  launcher tree that would trip the trust walk);
- dismissal sources: the flag file (service ready), the parent launcher
  process dying (failed/aborted launch), a load-timeout guard (only when
  webview imported but never showed), and a hard 45s lifetime cap - every
  path ends with the process gone, no residue;
- any failure (missing assets, no WebView2 runtime, webview import error,
  native window failure) logs one line to state\\splash-last.log next to
  the flag and exits 0: the boot continues exactly like a launch without
  splash ever could.

One splash at a time is enforced with a named mutex, so a second launcher
that loses the mutex race never stacks a second splash window.
"""

import sys

# The carrier runs BEFORE start_managed_courselens.ps1's own env hardening can
# matter for THIS interpreter: keep bytecode out of the trust-walked tree.
sys.dont_write_bytecode = True

import os
import threading
import time

MIN_DISPLAY_SECONDS = 0.8
LOAD_TIMEOUT_SECONDS = 5.0
MAX_LIFETIME_SECONDS = 45.0
POLL_SECONDS = 0.1
FADE_SECONDS = 0.3

_WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def _webview2_runtime_available() -> bool:
    """Standalone mirror of src.app's precheck (the carrier cannot import src).

    Same official lookup order: override folder, per-user Evergreen,
    per-machine Evergreen (WOW64 view); pv=0 counts as missing.
    """
    override = os.environ.get("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", "").strip()
    if override:
        return os.path.isfile(os.path.join(override, "msedewebview2.exe"))
    try:
        import winreg
    except ImportError:
        return True
    candidates = [
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients", 0),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients", 0),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients", 0),
    ]
    for hive, path, view in candidates:
        try:
            with winreg.OpenKey(
                hive, rf"{path}\{_WEBVIEW2_GUID}", 0, winreg.KEY_READ | view
            ) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if version and str(version) not in {"0", "0.0.0.0"}:
                    return True
        except OSError:
            continue
    return False


class _SplashLog:
    """One best-effort diagnostic line per run (never raises, never blocks)."""

    def __init__(self, flag_path: str) -> None:
        self._path = (
            os.path.join(os.path.dirname(flag_path), "splash-last.log")
            if flag_path
            else ""
        )
        self.started = time.perf_counter()

    def write(self, **fields) -> None:
        if not self._path:
            return
        elapsed_ms = int((time.perf_counter() - self.started) * 1000)
        parts = [f"total_ms={elapsed_ms}"]
        parts.extend(f"{key}={value}" for key, value in fields.items())
        try:
            with open(self._path, "w", encoding="utf-8") as handle:
                handle.write(" ".join(parts) + "\n")
        except OSError:
            pass


def _exclude_from_taskbar(window) -> None:
    """Best-effort WS_EX_TOOLWINDOW|WS_EX_NOACTIVATE so the HTML splash never
    owns a taskbar button; any failure keeps the window (cosmetic only)."""
    try:
        import ctypes

        form = getattr(window, "native", None)
        if form is None:
            return
        handle = form.Handle
        hwnd = int(handle.ToInt64()) if hasattr(handle, "ToInt64") else int(handle)
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        set_style = getattr(user32, "SetWindowLongPtrW", None)
        get_style = getattr(user32, "GetWindowLongPtrW", None)
        if set_style is None or get_style is None:
            set_style = user32.SetWindowLongW
            get_style = user32.GetWindowLongW
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        style = get_style(hwnd, GWL_EXSTYLE)
        set_style(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    except Exception:
        pass


def _another_splash_is_showing() -> bool:
    try:
        import ctypes

        ctypes.windll.kernel32.CreateMutexW(
            None, False, "Local\\FudanCourseLensSplash"
        )
        return ctypes.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return False


def _parent_is_gone(parent_pid: int) -> bool:
    if parent_pid <= 0:
        return False
    try:
        import ctypes

        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
        if not handle:
            return True
        ctypes.windll.kernel32.CloseHandle(handle)
        return False
    except Exception:
        return False


class _NativeSplash:
    """Phase-1 static brand mark: pure ctypes Win32, zero heavy imports.

    The window carries the extended styles from birth (toolwindow +
    no-activate + top-most) so it never owns a taskbar button and never
    steals focus.  Sizes stay in physical pixels (no process DPI-awareness
    change: the HTML phase manages its own scaling, and this phase lives
    for about a second).  Every failure inside show() returns False and
    the caller falls back to the legacy single-phase behavior.
    """

    WIDTH = 264
    HEIGHT = 264
    TAKEOVER_MSG = 0x8000 + 1  # WM_APP+1: HTML phase is visible, stand down
    # 色值与几何同 splash.html 的 mark（128px 盒 × 96 viewBox ≈ 1.333 缩放）：
    # 象牙环 r44/stroke7 + 藏青虹膜 r21 + 金 glare r6（偏 -15,-15）+ 字距字标。
    NAVY = 0x331D07        # #071D33 背景（COLORREF = 0x00BBGGRR）
    RING = 0xF1EBE8        # #E8EBF1 环
    IRIS = 0x9A693F        # #3F699A 虹膜
    GOLD = 0x3F9ED1        # #D19E3F glare
    WORD = 0xD9B38F        # #8FB3D9 字标
    MARK_CENTER_Y = 116
    WORD_TOP = 196

    def __init__(self) -> None:
        self.ctypes = None
        self.user32 = None
        self.gdi32 = None
        self.hwnd = 0
        self.shown_monotonic = 0.0
        self.show_error = ""
        self._proc_ref = None
        self._on_timer = None

    def show(self) -> bool:
        try:
            import ctypes
            from ctypes import wintypes

            self.ctypes = ctypes
            self.user32 = ctypes.windll.user32
            self.gdi32 = ctypes.windll.gdi32
            user32 = self.user32

            # 64 位句柄纪律：凡携句柄/指针的函数先声明 argtypes，禁默认 c_int
            # 截断（CreateWindowExW 的 hInstance 即因此 OverflowError）。
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
            ]
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.RegisterClassW.argtypes = [ctypes.c_void_p]
            user32.SetTimer.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.LPVOID]
            user32.UpdateWindow.argtypes = [wintypes.HWND]
            user32.KillTimer.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.DestroyWindow.argtypes = [wintypes.HWND]
            user32.PostQuitMessage.argtypes = [ctypes.c_int]
            user32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT]
            user32.TranslateMessage.argtypes = [ctypes.c_void_p]
            user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.c_void_p]
            user32.BeginPaint.restype = wintypes.HDC
            user32.EndPaint.argtypes = [wintypes.HWND, ctypes.c_void_p]
            user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.DefWindowProcW.restype = wintypes.LPARAM
            gdi32 = self.gdi32
            user32.FillRect.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.HBRUSH]
            gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
            gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
            gdi32.SelectObject.restype = wintypes.HGDIOBJ
            gdi32.GetStockObject.argtypes = [ctypes.c_int]
            gdi32.GetStockObject.restype = wintypes.HGDIOBJ
            gdi32.Ellipse.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
            gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
            gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
            gdi32.SetTextCharacterExtra.argtypes = [wintypes.HDC, ctypes.c_int]
            gdi32.GetTextExtentPoint32W.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.c_void_p]
            gdi32.TextOutW.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int]

            # wintypes 没有 LRESULT：LRESULT 与 LPARAM 同为指针尺寸有符号整型。
            WNDPROC = ctypes.WINFUNCTYPE(
                wintypes.LPARAM, wintypes.HWND, wintypes.UINT,
                wintypes.WPARAM, wintypes.LPARAM,
            )
            self._proc_ref = WNDPROC(self._wnd_proc)  # GC anchor for the callback

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            class MSG(ctypes.Structure):
                _fields_ = [
                    ("hwnd", wintypes.HWND),
                    ("message", wintypes.UINT),
                    ("wParam", wintypes.WPARAM),
                    ("lParam", wintypes.LPARAM),
                    ("time", wintypes.DWORD),
                    ("pt_x", wintypes.LONG),
                    ("pt_y", wintypes.LONG),
                ]

            self._msg = MSG()

            kernel32 = ctypes.windll.kernel32
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            hinst = kernel32.GetModuleHandleW(None)

            self.gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
            wnd_class = WNDCLASSW()
            wnd_class.lpfnWndProc = self._proc_ref
            wnd_class.hInstance = hinst
            wnd_class.lpszClassName = "FudanCourseLensSplashNative"
            wnd_class.hbrBackground = self.gdi32.CreateSolidBrush(self.NAVY)
            if not user32.RegisterClassW(ctypes.byref(wnd_class)):
                return False

            screen_w = user32.GetSystemMetrics(0)   # SM_CXSCREEN
            screen_h = user32.GetSystemMetrics(1)   # SM_CYSCREEN
            x = max(0, (screen_w - self.WIDTH) // 2)
            y = max(0, (screen_h - self.HEIGHT) // 2 - int(screen_h * 0.04))
            WS_POPUP = 0x80000000
            WS_VISIBLE = 0x10000000
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_TOPMOST = 0x00000008
            WS_EX_NOACTIVATE = 0x08000000
            user32.CreateWindowExW.restype = wintypes.HWND
            self.hwnd = user32.CreateWindowExW(
                WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
                wnd_class.lpszClassName, "CourseLens",
                WS_POPUP | WS_VISIBLE, x, y, self.WIDTH, self.HEIGHT,
                None, None, hinst, None,
            )
            if not self.hwnd:
                return False
            user32.SetTimer(self.hwnd, 1, int(POLL_SECONDS * 1000), None)
            user32.UpdateWindow(self.hwnd)
            self.shown_monotonic = time.perf_counter()
            return True
        except Exception as exc:
            self.show_error = f"{type(exc).__name__}: {exc}"
            self.hwnd = 0
            return False

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        user32 = self.user32
        WM_PAINT = 0x000F
        WM_TIMER = 0x0113
        WM_DESTROY = 0x0002
        if msg == WM_PAINT:
            self._paint(hwnd)
            return 0
        if msg == self.TAKEOVER_MSG:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_TIMER:
            callback = self._on_timer
            if callback:
                callback()
            return 0
        if msg == WM_DESTROY:
            user32.KillTimer(hwnd, 1)
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _paint(self, hwnd) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = self.user32
        gdi32 = self.gdi32

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG),
            ]

        class PAINTSTRUCT(ctypes.Structure):
            _fields_ = [
                ("hdc", wintypes.HDC), ("fErase", wintypes.BOOL),
                ("rcPaint", RECT), ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL),
                ("rgbReserved", wintypes.BYTE * 32),
            ]

        class SIZE(ctypes.Structure):
            _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        if not hdc:
            return
        try:
            brush = gdi32.CreateSolidBrush(self.NAVY)
            user32.FillRect(hdc, ctypes.byref(ps.rcPaint), brush)
            gdi32.DeleteObject(brush)

            NULL_BRUSH = 5
            PS_SOLID = 0
            cx, cy = self.WIDTH // 2, self.MARK_CENTER_Y
            gdi32.CreatePen.restype = wintypes.HPEN
            pen = gdi32.CreatePen(PS_SOLID, 7, self.RING)
            old_pen = gdi32.SelectObject(hdc, pen)
            old_brush = gdi32.SelectObject(hdc, gdi32.GetStockObject(NULL_BRUSH))
            gdi32.Ellipse(hdc, cx - 44, cy - 44, cx + 44, cy + 44)
            gdi32.DeleteObject(pen)
            gdi32.SelectObject(hdc, old_pen)
            gdi32.DeleteObject(old_brush)
            brush = gdi32.CreateSolidBrush(self.IRIS)
            old_brush = gdi32.SelectObject(hdc, brush)
            gdi32.Ellipse(hdc, cx - 21, cy - 21, cx + 21, cy + 21)
            gdi32.DeleteObject(brush)
            brush = gdi32.CreateSolidBrush(self.GOLD)
            gdi32.SelectObject(hdc, brush)
            gdi32.Ellipse(hdc, cx - 21, cy - 21, cx - 9, cy - 9)
            gdi32.SelectObject(hdc, old_brush)
            gdi32.DeleteObject(brush)

            gdi32.SetBkMode(hdc, 1)  # TRANSPARENT
            gdi32.SetTextColor(hdc, self.WORD)
            gdi32.CreateFontW.restype = wintypes.HFONT
            font = gdi32.CreateFontW(
                -13, 0, 0, 0, 600, 0, 0, 0, 0, 0, 0, 0, 0, "Segoe UI"
            )
            old_font = gdi32.SelectObject(hdc, font)
            gdi32.SetTextCharacterExtra(hdc, 5)  # ≈.46em 字距（12px 字面）
            text = "COURSELENS"
            extent = SIZE()
            gdi32.GetTextExtentPoint32W(hdc, text, len(text), ctypes.byref(extent))
            gdi32.TextOutW(
                hdc, cx - extent.cx // 2, self.WORD_TOP, text, len(text)
            )
            gdi32.SetTextCharacterExtra(hdc, 0)
            gdi32.SelectObject(hdc, old_font)
            gdi32.DeleteObject(font)
        except Exception:
            pass
        finally:
            user32.EndPaint(hwnd, ctypes.byref(ps))

    def pump(self, on_timer) -> None:
        """Run the phase-1 message loop; on_timer fires every poll tick."""
        self._on_timer = on_timer
        byref = self.ctypes.byref
        user32 = self.user32
        while user32.GetMessageW(byref(self._msg), None, 0, 0) > 0:
            user32.TranslateMessage(byref(self._msg))
            user32.DispatchMessageW(byref(self._msg))

    def request_close(self) -> None:
        try:
            if self.hwnd:
                self.user32.PostMessageW(self.hwnd, self.TAKEOVER_MSG, 0, 0)
        except Exception:
            pass


def main() -> int:
    process_start = time.perf_counter()
    args = sys.argv[1:]
    flag_path = args[0] if len(args) >= 1 else ""
    try:
        parent_pid = int(args[1]) if len(args) >= 2 else 0
    except ValueError:
        parent_pid = 0
    log = _SplashLog(flag_path)

    if _another_splash_is_showing():
        log.write(result="already-showing")
        return 0

    html_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "splash.html"
    )
    try:
        with open(html_path, "r", encoding="utf-8") as handle:
            html = handle.read()
    except OSError:
        log.write(result="html-missing")
        return 0

    # Phase 1 first: the static mark hits the screen before anything heavy.
    native = _NativeSplash()
    native_ok = native.show()
    if not native_ok:
        log.write(result="phase1-failed", detail=native.show_error or "api-false")

    started_at = time.perf_counter()
    shown_at = {"t": None}
    dismissed = threading.Event()
    dismiss_reason = {"why": "window-closed"}
    phase2 = {"state": "pending"}  # pending -> ready -> shown / missing / import-failed / start-failed
    window_holder = {"window": None}

    def _fade_out(reason: str) -> None:
        if dismissed.is_set():
            return
        dismissed.set()
        dismiss_reason["why"] = reason
        # 最短展示防闪烁：无论哪条收口路径，mark 至少完整亮相一拍。
        anchor = shown_at["t"] or (native.shown_monotonic if native_ok else started_at)
        remaining = MIN_DISPLAY_SECONDS - (time.perf_counter() - anchor)
        if remaining > 0:
            time.sleep(remaining)
        window = window_holder["window"]
        # HTML 淡出走守护线程且仅在 HTML 相确实显示过后触碰：evaluate_js 对
        # 尚未起跑的 GUI 循环会阻塞（沙箱实证 ~40s 挂），原生相绝不陪葬。
        if window is not None and phase2["state"] == "shown":
            def _html_fade() -> None:
                try:
                    window.evaluate_js(
                        "document.documentElement.classList.add('splash-bye');"
                    )
                except Exception:
                    pass
                time.sleep(FADE_SECONDS)
                try:
                    window.destroy()
                except Exception:
                    pass

            threading.Thread(target=_html_fade, name="courselens-splash-fade", daemon=True).start()
        native.request_close()

    def _watch() -> None:
        deadline = time.perf_counter() + MAX_LIFETIME_SECONDS
        load_deadline = time.perf_counter() + LOAD_TIMEOUT_SECONDS
        while not dismissed.is_set():
            if flag_path and os.path.isfile(flag_path):
                _fade_out("flag")
                return
            if _parent_is_gone(parent_pid):
                _fade_out("parent-gone")
                return
            if (
                shown_at["t"] is None
                and time.perf_counter() > load_deadline
                and (phase2["state"] == "ready" or not native_ok)
            ):
                _fade_out("load-timeout")
                return
            if time.perf_counter() > deadline:
                _fade_out("lifetime-cap")
                return
            time.sleep(POLL_SECONDS)

    threading.Thread(target=_watch, name="courselens-splash-watch", daemon=True).start()

    def _phase2() -> None:
        # WebView2 预检先行（运行时缺失绝不 import/建窗）；缺失=相 1 静态 mark
        # 继续服役，直至常规收口路径关门。
        if not _webview2_runtime_available():
            phase2["state"] = "missing"
            return
        try:
            import webview
        except Exception as exc:
            phase2["state"] = "import-failed"
            log.write(result="webview-import-failed", detail=type(exc).__name__)
            return

        def _on_shown(*_args) -> None:
            shown_at["t"] = time.perf_counter()
            phase2["state"] = "shown"
            window = window_holder["window"]
            if window is not None:
                _exclude_from_taskbar(window)
            # HTML 相可见：原生静态 mark 退场（相间交接零空档）。
            native.request_close()

        try:
            window = webview.create_window(
                "CourseLens",
                html=html,
                width=264,
                height=264,
                resizable=False,
                frameless=True,
                easy_drag=False,
                on_top=True,
                focus=False,
                background_color="#071D33",
            )
            window_holder["window"] = window
            window.events.shown += _on_shown
            phase2["state"] = "ready"
            webview.start()
            phase2["state"] = "closed"
        except Exception as exc:
            phase2["state"] = "start-failed"
            log.write(result="start-failed", detail=type(exc).__name__)

    worker = threading.Thread(target=_phase2, name="courselens-splash-html", daemon=True)
    worker.start()

    def _pump_timer() -> None:
        # 原生相自身也要响应收口：旗标/父进程/寿命帽。与 watcher 幂等。
        if dismissed.is_set():
            return
        if flag_path and os.path.isfile(flag_path):
            _fade_out("flag")
            return
        if _parent_is_gone(parent_pid):
            _fade_out("parent-gone")
            return
        if time.perf_counter() - process_start > MAX_LIFETIME_SECONDS:
            _fade_out("lifetime-cap")
            return

    if native_ok:
        # 泵退出=原生窗已收（HTML takeover 或任一收口路）。主线程随后等待
        # 统一收口完成再退出；HTML 相的淡出在守护线程里自 finishing。
        native.pump(_pump_timer)
        dismissed.wait(timeout=MAX_LIFETIME_SECONDS)
        worker.join(timeout=FADE_SECONDS + LOAD_TIMEOUT_SECONDS)
        phase1_ms = (
            int((native.shown_monotonic - process_start) * 1000)
            if native.shown_monotonic > process_start else 0
        )
        log.write(
            result="ok",
            phase1_ms=phase1_ms,
            shown_ms=int(((shown_at["t"] or started_at) - started_at) * 1000),
            dismissed_by=dismiss_reason["why"],
            phase2=phase2["state"],
        )
        if worker.is_alive():
            # webview 侧挂死（沙箱/异常环境实证存在）：窗口已全部关闭、日志
            # 已落，进程有界退场，不给非守护 GUI 线程陪葬机会。
            import os as _os

            _os._exit(0)
        return 0

    # 相 1 不可得（ctypes/窗口失败）：回退既有单相行为。
    dismissed.wait(timeout=MAX_LIFETIME_SECONDS)
    worker.join(timeout=FADE_SECONDS + LOAD_TIMEOUT_SECONDS)
    shown = shown_at["t"]
    log.write(
        result="ok",
        shown_ms=int(((shown or started_at) - started_at) * 1000),
        dismissed_by=dismiss_reason["why"],
        phase2=phase2["state"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
