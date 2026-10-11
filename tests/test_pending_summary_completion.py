"""夜10-C 第七波②：远端 LLM 段降级领回钉。

worker 以 status=llm_pending 回执交还非 LLM 产物后，客户端本地执行同一
create_summary 并按同一导入面落库：summary 落 store、消耗计数（DeepSeek
tokens）入账、警告携带 summary_completed_locally；缺 key 时闭集失败。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.application import CourseLensApplication

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PendingSummaryCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        # addCleanup 后进先出：先关应用（收池化句柄）再清临时目录
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.app.close)
        self.app.catalog_repository.upsert_course("course", "Course")
        self.app.catalog_repository.upsert_lecture(
            "course", {"sub_id": "sub-1", "sub_title": "Lecture", "has_playback": True},
        )

    def _pending_result(self) -> dict:
        return {
            "schema": "result.v2",
            "protocol_version": "2",
            "task_id": "task-pending",
            "job_kind": "summary",
            "input_hash": "0" * 64,
            "pipeline_fingerprint": "test-v2",
            "status": "llm_pending",
            "outputs": {"llm_pending": {
                "title": "Lecture",
                "transcript": [{"start_ms": 0, "end_ms": 1000, "text": "段落"}],
                "ppt_pages": [],
                "evidence_packet": None,
                "course_context": None,
                "reason_code": "llm_remote_failed",
            }},
            "metrics": {"elapsed_seconds": 1.0},
            "warnings": ["llm_pending_remote_failed"],
        }

    def test_pending_result_completes_locally_and_lands_in_store(self):
        local_summary = {
            "model": "deepseek-flash",
            "markdown": "# 本地完成的笔记",
            "chapters": [{"title": "章", "start_ms": 0, "summary": "s"}],
            "key_takeaways": ["要点"],
            # RR-ANCHORFE-1：本地领回与远端导入同一漏斗，锚随总结落库
            "takeaway_anchors": [123000],
            "assessment_events": [],
            "knowledge_points": [],
            "topic_candidates": [],
            "citations_rejected": 0,
        }
        # 领回走 _worker_llm_module 的守卫导入（会先把 worker/ 挂上 sys.path），
        # 因此先触发一次导入再以模块属性为 patch 目标。
        worker_llm = self.app._worker_llm_module()
        with patch.object(worker_llm, "create_summary", return_value=local_summary) as create,                 patch.object(worker_llm, "usage_snapshot", return_value={
                    "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                }), patch.object(self.app, "_deepseek_key", return_value="test-key"):
            self.app._import_remote_summary_result(
                "course", "sub-1", self._pending_result(), task_id="task-pending",
            )
        self.assertEqual(create.call_count, 1)
        stored = self.app.learning_store.find_ai_artifact("sub-1", "timestamp_summary")
        self.assertIsNotNone(stored, "本地完成的总结按同一导入面落库")
        self.assertIn("本地完成的笔记", str(stored))
        self.assertEqual(stored["content"]["takeaway_anchors"], [123000], "领回路径锚随总结落库")

    def test_learning_pack_pending_receipt_completes_locally(self):
        """夜10-C 第九波任务2：55fbf220 形状（learning_pack 回执）同样领回。"""
        local_summary = {
            "model": "deepseek-flash", "markdown": "# 领回笔记", "chapters": [],
            "key_takeaways": [], "assessment_events": [], "knowledge_points": [],
            "topic_candidates": [], "citations_rejected": 0,
        }
        worker_llm = self.app._worker_llm_module()
        receipt = self._pending_result()
        receipt["outputs"]["llm_pending"]["job_kind"] = "learning_pack"
        receipt["outputs"]["llm_pending"]["ppt_pages"] = [{"page_num": 1}]
        with patch.object(worker_llm, "create_summary", return_value=local_summary) as create,                 patch.object(worker_llm, "usage_snapshot", return_value={"total_tokens": 7}),                 patch.object(self.app, "_deepseek_key", return_value="test-key"):
            self.app._import_remote_summary_result(
                "course", "sub-1", receipt, task_id="task-pending",
            )
        self.assertEqual(create.call_count, 1)
        stored = self.app.learning_store.find_ai_artifact("sub-1", "timestamp_summary")
        self.assertIsNotNone(stored)

    def test_pending_without_key_fails_closed(self):
        with patch.object(self.app, "_deepseek_key", return_value=""):
            with self.assertRaises(RuntimeError) as caught:
                self.app._import_remote_summary_result(
                    "course", "sub-1", self._pending_result(), task_id="task-pending",
                )
        self.assertIn("DeepSeek API key is required", str(caught.exception))

    def test_regular_result_path_untouched(self):
        regular = {
            "schema": "result.v2", "protocol_version": "2",
            "task_id": "task-regular", "job_kind": "summary",
            "input_hash": "0" * 64, "pipeline_fingerprint": "test-v2",
            "status": "completed",
            "outputs": {"summary": {"model": "deepseek-flash", "markdown": "# 远端",
                                    "chapters": [], "key_takeaways": [],
                                    "assessment_events": []}},
            "metrics": {},
            "warnings": [],
        }
        self.app._import_remote_summary_result("course", "sub-1", regular, task_id="task-regular")
        stored = self.app.learning_store.find_ai_artifact("sub-1", "timestamp_summary")
        self.assertIsNotNone(stored)
        self.assertIn("远端", str(stored))


if __name__ == "__main__":
    unittest.main()
