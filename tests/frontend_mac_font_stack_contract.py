"""MAC-FONT-1：三轨字体栈跨平台合同钉（Windows 名在前、mac 名在后）。

合同（frontend/styles/tokens.css 四 token 单栈跨平台写法）：各平台按序首中
即停——Windows 栈首名不变则 Windows 逐位渲染零变化；mac 首中即得等价排版
语言。三轨映射：宋体编辑轨 --font-editorial → Songti SC（STSong 兜底）；
楷体控件/大字轨 --font-ui / --font-display → Kaiti SC / STKaiti；
mono 读数轨 --font-mono → Menlo（Monaco 兜底）。

防回退点：重排/瘦身字体栈时既不得丢失 mac 名，也不得把 mac 名移到
Windows 等价名之前（否则 Windows 首中位改变=渲染变化）。
mac 名合法性引证：Apple macOS Sequoia 官方字型清单
https://support.apple.com/en-us/120414（Menlo/Monaco/Courier New/
Songti SC/STSong/Kaiti SC/STKaiti 全部在列，2026-10-09 核验）。
"""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOKENS_CSS = ROOT / "frontend" / "styles" / "tokens.css"
COMPONENTS_CSS = ROOT / "frontend" / "styles" / "components.css"


def token_value(css: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)}:\s*([^;]+);", css)
    assert match, f"token {name} 未在 tokens.css 声明"
    return match.group(1).strip()


class MacFontStackContractTests(unittest.TestCase):
    """字体栈合同：Windows 名在前、mac 名在后，逐轨 mac 等价在位。"""

    def setUp(self):
        self.css = TOKENS_CSS.read_text(encoding="utf-8")
        self.components = COMPONENTS_CSS.read_text(encoding="utf-8")

    def test_four_font_tokens_declared_exactly_once(self):
        for name in ("--font-display", "--font-editorial", "--font-ui", "--font-mono"):
            self.assertEqual(
                self.css.count(f"{name}:"),
                1,
                f"{name} 必须在 tokens.css 唯一声明（token 是唯一定义点）",
            )

    def test_editorial_track_keeps_windows_first_and_mac_songti(self):
        # 宋体编辑轨：Windows 首中 Georgia+SimSun 族，mac 首中 Songti SC/STSong。
        stack = token_value(self.css, "--font-editorial")
        self.assertTrue(stack.startswith("Georgia,"), "编辑轨栈首必须是 Georgia（Windows 零变化锚点）")
        self.assertLess(stack.index('"Songti SC"'), stack.index('"SimSun"'), "mac Songti SC 不得移到 Windows SimSun 之后")
        self.assertLess(stack.index('"STSong"'), stack.index('"SimSun"'), "mac STSong 兜底不得移到 Windows SimSun 之后")
        self.assertIn('"宋体"', stack, "Windows 本地化宋体名兜底必须在位")

    def test_ui_track_keeps_windows_first_and_mac_kaiti(self):
        # 楷体控件轨：Windows 首中 KaiTi，mac 首中 Kaiti SC（STKaiti 兜底）。
        stack = token_value(self.css, "--font-ui")
        self.assertTrue(stack.startswith("Georgia,"), "控件轨栈首必须是 Georgia（Windows 零变化锚点）")
        self.assertLess(stack.index('"KaiTi"'), stack.index('"Kaiti SC"'), "Windows KaiTi 必须先于 mac Kaiti SC")
        self.assertLess(stack.index('"Kaiti SC"'), stack.index("system-ui"), "mac Kaiti SC 必须先于系统无衬线兜底")
        self.assertIn('"STKaiti"', stack, "mac STKaiti 兜底必须在位")

    def test_display_track_keeps_windows_first_and_mac_kaiti(self):
        # 楷体大字轨（问候/诗联）：Windows 首中 KaiTi，mac 首中 STKaiti（Songti SC 兜底）。
        stack = token_value(self.css, "--font-display")
        self.assertTrue(stack.startswith("Georgia,"), "大字轨栈首必须是 Georgia（Windows 零变化锚点）")
        self.assertLess(stack.index('"KaiTi"'), stack.index('"STKaiti"'), "Windows KaiTi 必须先于 mac STKaiti")
        self.assertLess(stack.index('"STKaiti"'), stack.index('"Songti SC"'), "mac 楷体 STKaiti 必须先于宋体兜底 Songti SC")
        self.assertIn('"宋体"', stack, "Windows 本地化宋体名兜底必须在位")

    def test_mono_track_keeps_windows_first_and_mac_menlo(self):
        # mono 读数轨：Windows 首中 Consolas，mac 首中 Menlo（Monaco 兜底）。
        stack = token_value(self.css, "--font-mono")
        self.assertEqual(
            stack,
            'Consolas, "Cascadia Mono", "Menlo", "Monaco", "Courier New", monospace',
            "mono 轨合同栈=Consolas（Windows 首中）→ Menlo/Monaco（mac 首中）→ Courier New → monospace",
        )

    def test_cue_literal_copy_keeps_mac_songti(self):
        # video::cue 是唯一字面量副本（::cue 对 CSS 变量支持不可靠，设计系统例外）：
        # 必须与 --font-editorial 同步携带 mac 名。
        match = re.search(r"video::cue\s*\{[^}]+\}", self.components, re.S)
        self.assertIsNotNone(match, "video::cue 字面量声明必须在 components.css")
        cue = match.group(0)
        self.assertIn('"Songti SC"', cue, "::cue 字面量副本必须携带 mac Songti SC")
        self.assertIn('"STSong"', cue, "::cue 字面量副本必须携带 mac STSong 兜底")
        self.assertLess(cue.index('"Songti SC"'), cue.index('"SimSun"'), "::cue 内 mac 名不得移到 Windows SimSun 之后")


if __name__ == "__main__":
    unittest.main()
