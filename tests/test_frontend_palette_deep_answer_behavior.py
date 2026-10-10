from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "frontend_palette_deep_answer_behavior.mjs"


class FrontendPaletteDeepAnswerBehaviorTests(unittest.TestCase):
    def test_real_modules_handle_deep_answer_states_and_actions(self):
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
            f"frontend palette deep answer behavior failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("frontend palette deep answer behavior passed", result.stdout)

    def test_palette_surface_declares_dual_action_and_deep_states(self):
        """P3-CONTRACT-1 §④：双动作 IA 与深度状态机在 palette 节/模块内的静态钉。"""
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        module = (ROOT / "frontend" / "modules" / "search-palette.js").read_text(encoding="utf-8")
        css = (ROOT / "frontend" / "styles" / "components.css").read_text(encoding="utf-8")
        api = (ROOT / "frontend" / "modules" / "api.js").read_text(encoding="utf-8")
        # 双动作 IA（palette 节内）
        self.assertIn('id="palette-answer" type="button" class="btn-primary">快速回答', html)
        self.assertIn('id="palette-answer-deep" type="button" class="btn-primary">深度回答', html)
        # 卡片三件套：动态标签/取消钮/动态 caveat
        self.assertIn('id="palette-answer-label"', html)
        self.assertIn('id="palette-answer-cancel" type="button" class="btn-quiet" hidden', html)
        self.assertIn('id="palette-answer-caveat"', html)
        self.assertNotIn("证据回答（模型生成）", html)
        # 深度链关键形状（合同 §①/§④）
        self.assertIn('mode: "deep"', module)
        self.assertIn("search/answer?task_id=", module)
        self.assertIn("palette_citation_click", module)
        self.assertIn("任务中心", module)
        self.assertIn("基于检索证据的本地拼装", module)
        self.assertNotIn("基于检索证据的回答（模型生成）", module)
        # U3 通道就绪窗守卫（WAIT-UX-2）：派发前读 remote-connection，就绪中不受理不失败
        self.assertIn("remote-connection", module)
        self.assertIn("waitRemoteChannelReady", module)
        # U1/U3 数字闭集单源（WAIT-UX-2）：ETA 文案只从 wait-expectations.js 取
        self.assertIn("wait-expectations.js", module)
        # CSS 只在 answer-card 族内做行内扩展
        self.assertIn(".answer-card .btn-quiet { margin-top: 10px; }", css)
        # 审计钉：deep_answer_evidence_unavailable 补文案行 + friendlyError 出口
        self.assertIn("deep_answer_evidence_unavailable:", api)
        self.assertIn("export function friendlyError", api)

    def test_btn_primary_hover_keeps_contrast_color(self):
        """VA-P1-01：全局 button:hover 特异性 (0,2,1) 压过 .btn-primary 基态白字
        (0,1,0)，悬停不得被翻成 navy-ink——悬停块重申 --accent-contrast 保色
        （flashcards 15b 同款先例），:active 按压态保持 navy-hover 面。"""
        css = (ROOT / "frontend" / "styles" / "components.css").read_text(encoding="utf-8")
        self.assertIn(
            ".btn-primary:hover:not(:disabled) { background: var(--navy-hover); color: var(--accent-contrast); }",
            css,
        )
        self.assertIn(
            ".btn-primary:active:not(:disabled) { background: var(--navy-hover); border-color: var(--navy-hover); }",
            css,
        )


if __name__ == "__main__":
    unittest.main()
