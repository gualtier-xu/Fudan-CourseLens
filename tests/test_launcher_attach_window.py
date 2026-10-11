r"""LAUNCH-FIX-1 A/B 钉面（2026-10-03）：无窗口挂接 + 先探活再走查。

产品语义：既有服务在跑但找不到它的原生窗时，不再直接降级浏览器——先聚焦
（现状保留），聚焦不中则经 src/app.py 的 --attach-window 薄模式新开一枚挂
接原生窗（零服务、零生命周期、零业务逻辑，关窗只关窗），浏览器仍是最后兜
底但须在 state\last-launch.host.log 留痕一行降级原因。执行顺序：先探活
（Find-ManagedAttachTarget：ok+service+安装槽版本+属主 pid 闭集指纹）命中
即挂接并跳过全量信任走查（挂接对象=上一个已验证启动器拉起的在跑实例）；
未命中才走与既往逐字节相同的全量走查。C 面（splash 载体/资产/清单行）钉在
tests/test_launcher_splash.py。
"""

from __future__ import annotations

import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

MANAGED_LAUNCHER = ROOT / "scripts" / "start_managed_courselens.ps1"
APP_SOURCE = ROOT / "src" / "app.py"


class FakeEvent:
    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, *args) -> None:
        for handler in list(self.handlers):
            handler(*args)


class FakeWindow:
    def __init__(self) -> None:
        self.events = types.SimpleNamespace(
            closing=FakeEvent(), closed=FakeEvent(), loaded=FakeEvent(), shown=FakeEvent()
        )
        self._destroyed = False
        self.native = None

    def destroy(self) -> None:
        self._destroyed = True


class FakeWebviewModule(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("webview")
        self.created: list[dict] = []
        self.started: list[dict] = []
        self.windows: list[FakeWindow] = []

    def create_window(self, title, url=None, **kwargs):
        window = FakeWindow()
        self.created.append({"title": title, "url": url, **kwargs})
        self.windows.append(window)
        return window

    def start(self, **kwargs):
        self.started.append(kwargs)
        for window in self.windows:
            window.events.loaded.fire()
            window.events.closed.fire()


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class AttachWindowCliTests(unittest.TestCase):
    """--attach-window：薄壳 CLI 面（零服务/零生命周期，复用 P68 窗口代码）。"""

    def setUp(self) -> None:
        self.webview = FakeWebviewModule()
        patcher = mock.patch.dict(sys.modules, {"webview": self.webview})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_attach_creates_a_thin_shell_window(self) -> None:
        from src.app import main

        with mock.patch("src.app.serve") as serve:
            self.assertEqual(main(["--attach-window", "http://127.0.0.1:6268/"]), 0)
        serve.assert_not_called()
        self.assertEqual(len(self.webview.created), 1)
        created = self.webview.created[0]
        self.assertEqual(created["title"], "CourseLens")
        self.assertEqual(created["url"], "http://127.0.0.1:6268/")
        self.assertIn("js_api", created)
        self.assertEqual(created["min_size"], (900, 600))

    def test_attach_never_wires_the_service_lifecycle(self) -> None:
        from src.app import main

        main(["--attach-window", "http://127.0.0.1:6268/"])
        window = self.webview.windows[0]
        # 关窗只关窗：attach 进程没有前端会话注册表——closed 事件（服务侧
        # 确定性离场信号）必须零处理器；关窗策略（托盘/工作保护确认）照旧。
        self.assertEqual(window.events.closed.handlers, [])
        self.assertEqual(len(window.events.closing.handlers), 1)

    def test_attach_rejects_non_loopback_targets(self) -> None:
        from src.app import main

        self.assertEqual(main(["--attach-window", "http://example.com/"]), 2)
        self.assertEqual(main(["--attach-window", "https://127.0.0.1:6268/"]), 2)
        self.assertEqual(self.webview.created, [])

    def test_attach_requires_a_value(self) -> None:
        from src.app import main

        with self.assertRaises(SystemExit):
            main(["--attach-window"])

    def test_usage_surface_mentions_attach_window(self) -> None:
        from src.app import main

        self.assertEqual(main([]), 2)
        source = _text(APP_SOURCE)
        self.assertIn("python -m src --attach-window URL", source)

    def test_attach_fallback_open_prints_manual_url_when_browser_fails(self) -> None:
        # OV-1a（夜15-R4 覆盖审计）：WebView2 缺失走 _fallback_open，浏览器
        # 再打不开时唯一出口=把 URL 打到 stdout 让学生手动复制——零打印即
        # 学生彻底失路。URL 逐字符=attach 目标，禁漂移。
        import contextlib
        import io

        from src.app import main

        with (
            mock.patch("src.app._webview2_runtime_available", return_value=False),
            mock.patch("src.app.webbrowser.open", return_value=False) as browser,
            contextlib.redirect_stdout(io.StringIO()) as captured,
        ):
            self.assertEqual(main(["--attach-window", "http://127.0.0.1:6268/"]), 0)
        browser.assert_called_once_with("http://127.0.0.1:6268/")
        self.assertIn(
            "[FudanCourseLens] Open the UI manually: http://127.0.0.1:6268/",
            captured.getvalue(),
        )
        self.assertEqual(self.webview.created, [], "兜底路径不创建原生窗")

    def test_attach_fallback_open_is_silent_when_browser_succeeds(self) -> None:
        import contextlib
        import io

        from src.app import main

        with (
            mock.patch("src.app._webview2_runtime_available", return_value=False),
            mock.patch("src.app.webbrowser.open", return_value=True),
            contextlib.redirect_stdout(io.StringIO()) as captured,
        ):
            self.assertEqual(main(["--attach-window", "http://127.0.0.1:6268/"]), 0)
        self.assertNotIn("Open the UI manually", captured.getvalue())


class LauncherOrderPins(unittest.TestCase):
    """ps1 顺序与闭集钉：探活在前、走查照旧、挂接优先于浏览器兜底。"""

    def setUp(self) -> None:
        self.content = _text(MANAGED_LAUNCHER)

    def test_probe_precedes_the_trust_walk(self) -> None:
        probe = self.content.index("Find-ManagedAttachTarget -PreferredPort $Port")
        walk = self.content.index(
            "$trustVerifyStopwatch = [System.Diagnostics.Stopwatch]::StartNew()"
        )
        self.assertLess(probe, walk, "探活必须在全量信任走查之前")

    def test_attach_path_exits_before_splash_launch(self) -> None:
        # 探活命中即挂接路径 exit 0（挂接不显示 splash）；splash 拉起在其后。
        probe_block = self.content[
            self.content.index("if (-not $NoOpen) {"):
            self.content.index("Start-ManagedSplash\n")
        ]
        self.assertIn("exit 0", probe_block)
        self.assertIn("Start-ManagedAttachWindow", probe_block)
        self.assertIn("Find-ManagedWindowHandle", probe_block)

    def test_probe_prefilters_dead_ports_with_a_short_tcp_connect(self) -> None:
        # 真机矩阵实测：裸死端口被安全软件丢包而非拒绝，逐口 1s HTTP 超时把
        # 冷启动探活拖成 11-14s；必须先以 250ms TCP 连接预过滤，只有接受连
        # 接的端口才发闭集健康请求。
        probe = re.search(
            r"function Find-ManagedServiceUrl \{(?P<body>.*?)\n\}", self.content, re.DOTALL
        )
        assert probe
        body = probe.group("body")
        self.assertIn("ConnectAsync", body)
        self.assertIn(".Wait(250)", body)
        # 闭集身份核对保留在 HTTP 面之后
        self.assertIn('$health.service -eq "fudan-courselens"', body)

    def test_probe_fingerprint_is_the_triple_identity(self) -> None:
        body = re.search(
            r"function Find-ManagedAttachTarget \{(?P<body>.*?)\n\}", self.content, re.DOTALL
        )
        assert body
        text = body.group("body")
        self.assertIn('Resolve-ManagedActiveRoot', text)
        self.assertIn("Find-ManagedServiceUrl -PreferredPort $PreferredPort", text)
        # 闭集三元组：service 名 / 安装槽版本 / 属主 pid 属本安装根 python312
        self.assertIn('$health.service -ne "fudan-courselens"', text)
        self.assertIn('[string]$health.version, $expectedVersion', text)
        self.assertIn('StartsWith("$LauncherRoot\\python312\\")', text)

    def test_active_root_resolution_never_runs_the_helper(self) -> None:
        body = re.search(
            r"function Resolve-ManagedActiveRoot \{(?P<body>.*?)\n\}", self.content, re.DOTALL
        )
        assert body
        text = body.group("body")
        self.assertIn('Join-Path $StateRoot "current.json"', text)
        self.assertIn('Join-Path $InstallRoot "versions\\$version"', text)
        self.assertNotIn("$Helper", text, "探活路径不得预执行受信 helper")

    def test_attach_window_spawn_is_the_thin_pythonw_mode(self) -> None:
        body = re.search(
            r"function Start-ManagedAttachWindow \{(?P<body>.*?)\n\}", self.content, re.DOTALL
        )
        assert body
        text = body.group("body")
        self.assertIn('Join-Path $LauncherRoot "python312\\pythonw.exe"', text)
        self.assertIn('@("-m", "src", "--attach-window", $ServiceUrl)', text)
        self.assertIn("-WorkingDirectory $ActiveRoot", text)
        self.assertIn('SetEnvironmentVariable("COURSELENS_DATA_DIR", $DataRoot, "Process")', text)

    def test_windowless_fallback_attaches_before_the_browser_in_repull(self) -> None:
        repull = self.content[
            self.content.index("if (-not $owns) {"):
            self.content.index("function Get-ActiveRoot")
        ]
        attach = repull.index("Start-ManagedAttachWindow -ServiceUrl $existingUrl")
        browser = repull.index("Start-Process $existingUrl")
        self.assertLess(attach, browser, "挂接窗必须先于浏览器兜底")
        self.assertIn("Write-ManagedHostLog", repull)
        # 聚焦路径保留（现状）：闭集句柄 + 前台化
        self.assertIn("Find-ManagedWindowHandle", repull)
        self.assertIn("SetForegroundWindow", repull)

    def test_browser_last_resort_logs_the_degraded_reason(self) -> None:
        body = re.search(
            r"function Write-ManagedHostLog \{(?P<body>.*?)\n\}", self.content, re.DOTALL
        )
        assert body
        text = body.group("body")
        self.assertIn('Join-Path $StateRoot "last-launch.host.log"', text)
        self.assertIn("Add-Content", text)

    def test_splash_flag_is_set_when_the_service_is_ready(self) -> None:
        probe_start = self.content.index("function Start-And-Probe")
        probe_end = self.content.index("# A verified pending switch restarts")
        body = self.content[probe_start:probe_end]
        flag = body.index("Complete-ManagedSplash")
        ret = body.index("return $process")
        self.assertLess(flag, ret, "health 三元组通过即收口 splash")


if __name__ == "__main__":
    unittest.main()
