from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from path_utils import PROJECT_ROOT
from src.application import CourseLensApplication, _summary_block_code


class SubtitleDurationTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.app.catalog_repository.upsert_course("course", "Course")
        self.app.catalog_repository.upsert_lecture(
            "course",
            {
                "sub_id": "lecture",
                "sub_title": "Lecture",
                "has_playback": True,
                "duration_seconds": 1200,
            },
        )
        self.app._credentials = {"student_id": "configured", "password": "configured"}
        self.app._prepare_remote_coordinator = Mock()
        self.app._ensure_subtitle_worker = Mock()

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def test_automatic_policy_label_regardless_of_caller_payload(self) -> None:
        """唯一 automatic 策略：任务载荷只带 automatic，请求侧无模式选择参数。"""
        with_key = self.app
        with_key._deepseek_api_key = "sk-synthetic"
        task = with_key.enqueue_subtitle("course", "lecture", start_seconds=300)

        self.assertEqual(task["payload"]["subtitle_mode"], "automatic")
        self.assertTrue(task["payload"]["subtitle_proofread"])
        self.assertTrue(str(task["config_key"]).startswith("automatic"))
        self.assertEqual(task["payload"]["duration_seconds"], 900)
        self.assertEqual(task["payload"]["media_duration_seconds"], 900)

    def test_without_deepseek_key_automatic_still_enqueues(self) -> None:
        """无 Key：同一 automatic 策略入队，行为位如实记录为非 AI 回退。"""
        task = self.app.enqueue_subtitle("course", "lecture")

        self.assertEqual(task["payload"]["subtitle_mode"], "automatic")
        self.assertFalse(task["payload"]["subtitle_proofread"])
        self.assertTrue(str(task["config_key"]).startswith("automatic"))

    def test_catalog_guard_rejects_with_closed_set_runtime_error(self) -> None:
        """讲次不在授权目录：闭集 RuntimeError+显式码拒绝，绝不让
        FileNotFoundError 逃过 tasks/enqueue 出口闭集（实证曾逃逸 500
        runtime_failed）。"""
        with self.assertRaises(RuntimeError) as subtitle:
            self.app.enqueue_subtitle("course", "missing-lecture")
        self.assertEqual(subtitle.exception.code, "lecture_not_found")
        self.assertNotIsInstance(subtitle.exception, FileNotFoundError)

        with self.assertRaises(RuntimeError) as summary:
            self.app.enqueue_summary("course", "missing-lecture")
        self.assertEqual(summary.exception.code, "lecture_not_found")
        self.assertNotIsInstance(summary.exception, FileNotFoundError)

    def test_summary_block_code_maps_not_in_catalog_from_explicit_code(self) -> None:
        """课程整理分诊保持 lecture_not_in_catalog 语义：显式码与
        FileNotFoundError 两种形态同归一类人话原因。"""
        explicit = RuntimeError("Lecture is not in the authorized catalog")
        explicit.code = "lecture_not_found"
        self.assertEqual(_summary_block_code(explicit), "lecture_not_in_catalog")
        self.assertEqual(
            _summary_block_code(FileNotFoundError("Lecture is not in the authorized catalog")),
            "lecture_not_in_catalog",
        )

    def test_summary_enqueue_guards_raise_explicit_codes(self) -> None:
        """R3-12 收尾：摘要入队三守卫改抛显式码异常，分诊映射器优先读
        code——措辞漂移不再静默降级，关键词嗅探仅作兼容回退。"""
        with self.assertRaises(RuntimeError) as no_subtitles:
            self.app.enqueue_summary("course", "lecture")
        self.assertEqual(no_subtitles.exception.code, "transcript_missing")
        self.assertEqual(_summary_block_code(no_subtitles.exception), "transcript_missing")

        self.app._deepseek_api_key = "sk-synthetic"
        self.app.subtitle_segments = Mock(return_value={"segments": [{"start_ms": 0}]})
        self.app._credentials = {}
        with self.assertRaises(RuntimeError) as no_credentials:
            self.app.enqueue_summary("course", "lecture", include_ppt=True)
        self.assertEqual(no_credentials.exception.code, "fudan_login_required")
        self.assertEqual(_summary_block_code(no_credentials.exception), "fudan_login_required")

        hostile = RuntimeError("措辞已完全漂移的消息")
        hostile.code = "ai_key_missing"
        self.assertEqual(_summary_block_code(hostile), "ai_key_missing",
            "explicit code must win regardless of message wording")
        legacy = RuntimeError("DeepSeek API key is required for AI timestamp summaries")
        self.assertEqual(_summary_block_code(legacy), "ai_key_missing",
            "keyword sniffing stays as the compatibility fallback")


if __name__ == "__main__":
    unittest.main()
