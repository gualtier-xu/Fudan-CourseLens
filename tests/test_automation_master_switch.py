# AS11（第五十一案）自动整理唯一总开关钉面：视觉（hover 边框退役+焦点环）/
# 唯一编辑面（目录勾选链）/任务区状态行源码钉。后端 automation 合同零改动。
from __future__ import annotations

import unittest
from pathlib import Path

from tests.frontend_family import family_text

ROOT = Path(__file__).resolve().parents[1]


class AutomationMasterSwitchSourcePins(unittest.TestCase):
    def setUp(self) -> None:
        self.css = (ROOT / "frontend" / "styles" / "components.css").read_text(encoding="utf-8")
        self.study = family_text("study")
        self.drawer = family_text("tasks-drawer")
        self.settings = family_text("settings")

    def test_hover_border_is_retired_with_scoped_exemption(self) -> None:
        self.assertNotIn(
            ".course-automation-toggle:hover:not(:disabled) { border-color: var(--line-strong); }",
            self.css,
            "旧的悬停显边框规则必须删除",
        )
        self.assertIn(
            ".course-automation-toggle:hover:not(:disabled) { border-color: transparent; }",
            self.css,
            "控件级豁免全局 hover 边框翻转（全局政策本身不动）",
        )
        # 全局 button:hover 政策保持原样（纪律：不动全局）
        self.assertIn(
            "button:hover:not(:disabled), .button-link:hover, .file-button:hover",
            self.css,
        )

    def test_keyboard_focus_ring_present(self) -> None:
        self.assertIn(
            ".course-automation-toggle:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }",
            self.css,
        )

    def test_catalog_toggle_is_the_only_automation_edit_surface(self) -> None:
        # 目录勾选链语义完整（陈旧响应保护 + 串行队列 + 空规则=disable-cloud）
        self.assertIn("automationSaveSeq", self.study)
        self.assertIn("async function saveCourseAutomation", self.study)
        self.assertIn('action: "disable-cloud"', self.study)
        # 设置页不再有自动化管理面板（CLOUD-CONSENT-AUTO-1 U2 既有事实保持）
        self.assertNotIn("runAutomationAction", self.settings)
        self.assertNotIn("AUTOMATION_STATE_TEXT", self.settings)
        # 设置页残留的 automation 写面只允许撤销云端授权（安全阀，非开关）
        self.assertIn('"revoke-cloud-credentials"', self.settings)
        self.assertEqual(self.settings.count("automation/actions"), 1)

    def test_task_area_status_line_source_pins(self) -> None:
        self.assertIn("automation-run-status", self.drawer)
        self.assertIn("未勾选课程", self.drawer)
        self.assertIn("已勾选", self.drawer)
        self.assertIn("schedule?.times", self.drawer, "时刻来自快照，不硬编码")


if __name__ == "__main__":
    unittest.main()
