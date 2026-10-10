r"""LAUNCH-FIX-1 C 钉面（2026-10-03）：开机 splash。

时序与信任：ps1 在探活未命中（真冷启动）后才做单点快核（launcher-trust.json
预置哈希 × 仅 splash 壳+HTML 两件）并 pythonw 拉起载体，全量信任走查照旧在
后；任何缺失/异常一律静默跳过，绝不阻断或拖慢启动。载体自守：字节码禁写、
WebView2 预检、命名互斥防叠窗、父进程退场监视、45s 兜底、最短展示 0.8s、
private mode（TEMP 用户数据目录，launcher 树零写入）。HTML 单文件零外链、
动画循环 ≤2s、prefers-reduced-motion 命中即静态（即静态图标过渡版）。
挂接路径与 -NoOpen 恒无 splash。收口旗标落 state\ 根（绝不落 launcher 树）。
"""

import py_compile
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MANAGED_LAUNCHER = ROOT / "scripts" / "start_managed_courselens.ps1"
CARRIER = ROOT / "scripts" / "splash" / "splash.pyw"
ASSET = ROOT / "scripts" / "splash" / "splash.html"
INSTALLER = ROOT / "scripts" / "install_managed_client.ps1"


def _launcher_text() -> str:
    return MANAGED_LAUNCHER.read_text(encoding="utf-8")


class SplashOrderPins(unittest.TestCase):
    def setUp(self) -> None:
        self.content = _launcher_text()

    def _splash_body(self) -> str:
        match = re.search(
            r"function Start-ManagedSplash \{(?P<body>.*?)\n\}", self.content, re.DOTALL
        )
        assert match, "Start-ManagedSplash function missing"
        return match.group("body")

    def test_splash_launches_after_probe_miss_and_before_the_trust_walk(self) -> None:
        attach_probe = self.content.index("if (-not $NoOpen) {")
        splash_call = self.content.index("Start-ManagedSplash\n")
        walk = self.content.index(
            "$trustVerifyStopwatch = [System.Diagnostics.Stopwatch]::StartNew()"
        )
        self.assertLess(attach_probe, splash_call, "探活命中即挂接路径，先于 splash")
        self.assertLess(splash_call, walk, "splash 载体在全量走查之前拉起")

    def test_noopen_never_splashes(self) -> None:
        self.assertTrue(self._splash_body().lstrip().startswith("if ($NoOpen) { return }"))

    def test_quick_check_hashes_exactly_two_assets_against_the_trust_manifest(self) -> None:
        body = self._splash_body()
        self.assertIn('"splash/splash.pyw"', body)
        self.assertIn('"splash/splash.html"', body)
        self.assertIn('Join-Path $LauncherRoot "launcher-trust.json"', body)
        # 任何快核失败都是静默跳过（return），绝不阻断启动（零 throw）。
        self.assertNotIn("throw ", body)

    def test_flag_file_lives_in_the_state_root_not_the_launcher_tree(self) -> None:
        self.assertIn('Join-Path $StateRoot "splash-dismiss.flag"', self.content)

    def test_flag_written_only_when_a_splash_was_launched(self) -> None:
        match = re.search(
            r"function Complete-ManagedSplash \{(?P<body>.*?)\n\}", self.content, re.DOTALL
        )
        assert match
        body = match.group("body")
        self.assertIn("if (-not $script:SplashLaunched) { return }", body)
        self.assertIn("Set-Content -LiteralPath $script:SplashFlagPath", body)


class SplashCarrierTests(unittest.TestCase):
    def test_carrier_compiles_as_python(self) -> None:
        py_compile.compile(str(CARRIER), doraise=True)

    def test_carrier_never_blocks_the_boot(self) -> None:
        text = CARRIER.read_text(encoding="utf-8")
        # launcher 树零字节码（信任走查安全）
        self.assertIn("sys.dont_write_bytecode = True", text)
        # 兜底退出与最短展示
        self.assertIn("MAX_LIFETIME_SECONDS = 45.0", text)
        self.assertIn("MIN_DISPLAY_SECONDS = 0.8", text)
        # 单实例互斥：第二个启动器绝不叠窗
        self.assertIn("FudanCourseLensSplash", text)
        self.assertIn("ERROR_ALREADY_EXISTS", text)
        # 收口四路：旗标 / 父进程退场 / 加载超时 / 寿命兜底
        # （B33v2：第四路 load-timeout 此前漏钉——087f9d7 证据面可静默回退
        # 而全量仍绿，注释「收口三路」与代码四路已漂移过一次）
        for marker in (
            '_fade_out("flag")', '_fade_out("parent-gone")',
            '_fade_out("load-timeout")', '_fade_out("lifetime-cap")',
        ):
            self.assertIn(marker, text)
        # WebView2 预检在 import webview 之前（运行时缺失绝不 import/建窗）
        self.assertLess(
            text.index("_webview2_runtime_available"), text.index("import webview\n")
        )
        # private mode 缺省（TEMP 用户数据目录）：载体绝不传 storage_path
        self.assertNotIn("storage_path", text)
        self.assertNotIn("private_mode", text)

    def test_dual_phase_close_paths_record_dismiss_reason(self) -> None:
        """B33v2（N15-R4/OV-2a + 7940bac 双相回声）：原生相泵三路收口与
        文本相四路并存；收口理由经 dismiss_reason 记录制在两处 ended 日志
        如实落账（防「日志误报家长死亡」类证据面静默回退）。"""
        text = CARRIER.read_text(encoding="utf-8")
        # 原生相 _pump_timer 内的三路（与文本相幂等，HTML takeover 前兜底）。
        pump = text[text.index("def _pump_timer"):]
        pump = pump[:pump.index("\n    if native_ok:")]
        for marker in (
            '_fade_out("flag")', '_fade_out("parent-gone")', '_fade_out("lifetime-cap")',
        ):
            self.assertIn(marker, pump)
        # 记录制：初始化缺省 + _fade_out 内记录 + 两处 dismissed_by 落账。
        self.assertIn('dismiss_reason = {"why": "window-closed"}', text)
        self.assertIn('dismiss_reason["why"] = reason', text)
        self.assertEqual(
            text.count('dismissed_by=dismiss_reason["why"]'), 2,
            "原生相与回退相两条 ended 日志都必须带收口理由",
        )
        # 理由值闭集：四路各一 + 缺省值（观测面靠它归因，改名即红）。
        for reason in ("flag", "parent-gone", "load-timeout", "lifetime-cap"):
            self.assertIn(f'_fade_out("{reason}")', text)
        self.assertIn('"why": "window-closed"', text)

    def test_carrier_asset_is_a_closed_set_single_file(self) -> None:
        text = ASSET.read_text(encoding="utf-8")
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)
        self.assertNotIn("<script", text)
        self.assertNotIn("src=", text)

    def test_animation_is_short_and_respects_reduced_motion(self) -> None:
        text = ASSET.read_text(encoding="utf-8")
        self.assertIn("@media (prefers-reduced-motion: reduce)", text)
        durations = [
            float(value)
            for value in re.findall(r"animation:[^;]*?([0-9]+(?:\.[0-9]+)?)s", text)
        ]
        self.assertTrue(durations, "至少一条动画定义")
        self.assertLessEqual(max(durations), 2.0, "动画循环 ≤2s")
        # 淡出由载体驱动（html.splash-bye），页面自身不计时
        self.assertIn("html.splash-bye body { opacity: 0; }", text)
        # 品牌深墨蓝底（既有设计语言 tokens.css navy-deep）
        self.assertIn("#071D33", text)


class InstallerPayloadPins(unittest.TestCase):
    def test_splash_assets_ship_inside_the_trust_boundary(self) -> None:
        content = INSTALLER.read_text(encoding="utf-8")
        for name in ("splash\\splash.pyw", "splash\\splash.html"):
            self.assertIn(name, content)
        first = content.index("splash\\splash.pyw")
        trust_build = content.index("$launcherFiles = [ordered]@{")
        self.assertLess(first, trust_build, "splash 资产必须先入树再被信任清单哈希")


if __name__ == "__main__":
    unittest.main()
