from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "frontend_update_platform_entry_behavior.mjs"
HTML = ROOT / "frontend" / "index.html"
PANEL = ROOT / "frontend" / "modules" / "settings" / "update-panel.js"
SERVICE = ROOT / "src" / "update" / "service.py"

PAGES_DOWNLOAD_HREF = "https://gualtier-xu.github.io/Fudan-CourseLens/#download"
WIN_MISMATCH_GUIDANCE = "此设备不符合受管理 Windows 更新条件。请使用受支持的 Windows 客户端。"


class FrontendUpdatePlatformEntryBehaviorTests(unittest.TestCase):
    """UPDATE-PANEL-1（MAC-3 决策 C 落地）：更新面板平台分发双态钉。

    win=现行为逐位不变（Windows 面控件/文案原串原位，后端 fail-closed 语义
    零改动）；darwin=入口态（「macOS 测试版：请从下载页获取新版本。」+
    打开下载页锚 → Pages #download，自动检查控件随 Windows 面整组让位，
    不加设置项）。行为面真执行钉在 HARNESS（node），此处加源级结构钉。 """

    def test_behavior_harness_passes(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required for frontend behavior tests")
        result = subprocess.run(
            [str(node), str(HARNESS)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"update platform entry behavior failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("update platform entry pins", result.stdout)

    def test_source_pins_windows_face_bitwise_unchanged(self):
        html = HTML.read_text(encoding="utf-8")
        panel = PANEL.read_text(encoding="utf-8")
        service = SERVICE.read_text(encoding="utf-8")
        # Windows 面原控件原串原位（包进 update-windows-face 但逐位不动）
        self.assertIn('<div id="update-windows-face">', html)
        self.assertIn('<button id="check-update" type="button" class="btn-primary">检查更新</button>', html)
        self.assertIn('<button id="download-update" type="button" disabled>确认并下载</button>', html)
        self.assertIn('<button id="install-update" type="button" disabled>确认安装</button>', html)
        self.assertIn('<input id="update-background-checks" type="checkbox" role="switch">自动检查更新', html)
        # SIMPLIFY-AUDIT-1 S5：事实行收敛——一行人话 + 上次检查提示；开发行话
        # 「通道」不再上脸（三格 dl 与 update-channel/available-version/update-size
        # 旧 id 全族退役）。
        self.assertIn('<p id="update-facts-line" class="update-facts-line">', html)
        self.assertIn('<p id="update-last-check" class="hint"></p>', html)
        self.assertNotIn("update-channel", html)
        self.assertNotIn("<dl class=\"update-facts\">", html)
        # Windows 误导文案在本机宿主语境保留（mac 走入口态不再见到它）
        self.assertIn(WIN_MISMATCH_GUIDANCE, panel)
        # 后端 fail-closed 语义零改动（只读钉：host_platform_mismatch 仍在）
        self.assertIn('raise UpdateError("host_platform_mismatch")', service)

    def test_source_pins_mac_entry_state(self):
        html = HTML.read_text(encoding="utf-8")
        panel = PANEL.read_text(encoding="utf-8")
        mac_module = (ROOT / "frontend" / "modules" / "update-mac.js").read_text(encoding="utf-8")
        self.assertIn('<div id="update-mac-entry" hidden>', html)
        # UPDATE-UX-1：入口 idle 文案（点检查更新主动态）+ 三步安装闭集
        self.assertIn("macOS 测试版：点「检查更新」看看有没有新版本；安装包在下载页手动获取。", html)
        self.assertIn("把它拖进「应用程序」", html)
        self.assertIn("访达里右键点 CourseLens 选「打开」", html)
        self.assertIn(
            f'<a id="update-mac-download" class="button-link primary" href="{PAGES_DOWNLOAD_HREF}" rel="noreferrer">打开下载页</a>',
            html,
        )
        # 自动化缺省：mac 入口态不加设置项（入口块内不得出现 checkbox/switch）
        mac_block = html.split('<div id="update-mac-entry" hidden>', 1)[1].split("</section>", 1)[0]
        self.assertNotIn("checkbox", mac_block)
        self.assertNotIn('role="switch"', mac_block)
        # 分发早分支：darwin 宿主不看快照、不写 Windows 面
        self.assertIn("if (macUpdateEntry()) {", panel)
        # macUpdateEntry 移居 update-mac.js 共享叶模块（面板经导入消费，
        # 顶栏与面板共用同一探测与检查道）
        self.assertIn("from \"../update-mac.js\"", panel)
        self.assertIn("export function macUpdateEntry()", mac_module)
        self.assertIn("https://api.github.com/repos/gualtier-xu/Fudan-CourseLens/releases", mac_module)


if __name__ == "__main__":
    unittest.main()
