"""P2-CONTRACT-1 第一批（客户端侧）行为钉：review_views 存储合同。

覆盖（合同 §④ 冻结口径）：
1. 落库形状：kind=review_views / prompt_version=review-views-v1，content_json
   = {schema_version, views, generation}，content_markdown = 速览+must_know 行；
2. shape fail-closed：非 dict / 四档全缺 / 单档畸形整档丢弃，绝不落坏行，
   也绝不挡总结导入；
3. 老结果无键幂等：review_views 缺席 = 零行，summary/chapters 照常；
4. 同 input_hash 重跑 upsert 覆盖（随总结整体再生成）；
5. 中断元组双 kind：summary 任务失败时 review_views 与 timestamp_summary
   同批收口（四处 `("timestamp_summary", "review_views")` 循环的漏斗级钉）；
6. 两条导入漏斗同权：任务漏斗与 automation 漏斗都把 summary.review_views
   传入存储面。

LLM 派生调用本体属第二批（worker llm.py，PKG-A），本文件不做任何模型调用。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from path_utils import PROJECT_ROOT
from src.application import CourseLensApplication
from src.runtime.learning_store import LearningStore


def _views() -> dict:
    """合同 §① 冻结 schema 的合法四档（合成内容，零真实课程信息）。"""
    return {
        "study_guide": {
            "items": [
                {"question": "内建电场如何形成？", "hint": "从扩散与漂移的平衡想起",
                 "anchor_ms": 1200000, "citation_ids": ["seg:a1"]},
                {"question": "无锚无引用的问句", "hint": "", "anchor_ms": None,
                 "citation_ids": []},
            ],
        },
        "faq": {
            "items": [
                {"question": "扩散电容和什么有关？", "answer": "正比于直流电流与渡越时间。",
                 "anchor_ms": 1200500, "citation_ids": ["seg:a2"]},
            ],
        },
        "timeline": {
            "events": [
                {"start_ms": 1200000, "title": "小信号模型", "detail": "导纳与渡越时间"},
                {"start_ms": 0, "title": "PN 结形成", "detail": ""},
            ],
        },
        "briefing": {
            "speed_read": "本讲从平衡 PN 结讲到交流小信号模型。",
            "must_know": ["扩散电容由渡越时间决定", "内建电场阻止进一步扩散"],
            "exam_alerts": [
                {"category": "quiz", "title": "第三讲后小测", "due_hint": "下周"},
            ],
        },
    }


class ReviewViewsStoreTests(unittest.TestCase):
    """存储合同（learning_store.import_remote_summary）。"""

    def _import(self, store: LearningStore, *, review_views=None, input_hash="a" * 64):
        store.import_remote_summary(
            course_id="course",
            sub_id="sub-1",
            input_hash=input_hash,
            model="deepseek-flash",
            markdown="# 总结",
            chapters=[{"start_ms": 0, "title": "章", "summary": "s"}],
            ppt_pages=[],
            review_views=review_views,
        )

    def test_lands_with_frozen_shape_and_readable_markdown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LearningStore(Path(directory) / "learning.db")
            self._import(store, review_views=_views())
            artifact = store.find_ai_artifact("sub-1", "review_views")
        self.assertIsNotNone(artifact, "合法四档必须落行")
        self.assertEqual(artifact["status"], "ready")
        self.assertEqual(artifact["prompt_version"], "review-views-v1")
        content = artifact["content"]
        self.assertEqual(content["schema_version"], 1)
        self.assertEqual(content["views"], _views(), "views 原样透传（深校验在派生调用收口）")
        self.assertEqual(content["generation"], {
            "input_hash": "a" * 64,
            "prompt_version": "review-views-v1",
            "model": "deepseek-flash",
        })
        markdown = artifact["content_markdown"]
        self.assertIn("本讲从平衡 PN 结讲到交流小信号模型。", markdown)
        self.assertIn("- 扩散电容由渡越时间决定", markdown)
        self.assertIn("- 内建电场阻止进一步扩散", markdown)

    def test_summary_and_chapters_still_land_alongside(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LearningStore(Path(directory) / "learning.db")
            self._import(store, review_views=_views())
            summary = store.find_ai_artifact("sub-1", "timestamp_summary")
            chapters = store.find_ai_artifact("sub-1", "lecture_chapters")
        self.assertIsNotNone(summary, "视图入库不得影响总结本体")
        self.assertIsNotNone(chapters, "视图入库不得影响章节本体")

    def test_missing_key_stores_nothing_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LearningStore(Path(directory) / "learning.db")
            self._import(store)
            self._import(store)
            self.assertIsNone(
                store.find_ai_artifact("sub-1", "review_views"),
                "老结果无键 = 零行（重跑同样零行）",
            )
            self.assertIsNotNone(store.find_ai_artifact("sub-1", "timestamp_summary"))

    def test_malformed_shapes_fail_closed(self):
        cases = [
            ("non-dict", "not-a-dict"),
            ("empty", {}),
            ("all-tiers-malformed", {
                "study_guide": {"no_items": []},
                "faq": {"items": "nope"},
                "timeline": {"events": None},
                "briefing": {"speed_read": "   "},
            }),
            ("empty-item-lists", {
                "study_guide": {"items": []},
                "faq": {"items": []},
                "timeline": {"events": []},
                "briefing": {"speed_read": "", "must_know": [], "exam_alerts": []},
            }),
        ]
        for label, payload in cases:
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as directory:
                    store = LearningStore(Path(directory) / "learning.db")
                    self._import(store, review_views=payload)
                    artifact = store.find_ai_artifact("sub-1", "review_views")
                self.assertIsNone(artifact, f"{label}: 畸形必须拒落")
                with tempfile.TemporaryDirectory() as directory:
                    store = LearningStore(Path(directory) / "learning.db")
                    self._import(store, review_views=payload)
                    self.assertIsNotNone(
                        store.find_ai_artifact("sub-1", "timestamp_summary"),
                        f"{label}: 拒落不得挡总结导入",
                    )

    def test_malformed_tier_dropped_valid_tiers_kept(self):
        payload = _views()
        payload["study_guide"] = {"items": "broken"}
        del payload["briefing"]["speed_read"]
        with tempfile.TemporaryDirectory() as directory:
            store = LearningStore(Path(directory) / "learning.db")
            self._import(store, review_views=payload)
            artifact = store.find_ai_artifact("sub-1", "review_views")
        self.assertIsNotNone(artifact, "单档畸形=整档丢弃，其余档照落")
        self.assertNotIn("study_guide", artifact["content"]["views"])
        self.assertNotIn("briefing", artifact["content"]["views"])
        self.assertIn("faq", artifact["content"]["views"])
        self.assertIn("timeline", artifact["content"]["views"])

    def test_same_input_hash_rerun_upserts_over_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LearningStore(Path(directory) / "learning.db")
            self._import(store, review_views=_views())
            updated = _views()
            updated["briefing"]["speed_read"] = "重跑后的速览。"
            self._import(store, review_views=updated)
            rows = store.find_ai_artifact("sub-1", "review_views")
        self.assertEqual(rows["content"]["views"]["briefing"]["speed_read"], "重跑后的速览。")


class ReviewViewsFunnelTests(unittest.TestCase):
    """两条导入漏斗同权 + 中断元组双 kind（application.py）。"""

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

    def _summary_outputs(self) -> dict:
        return {
            "summary": {
                "model": "deepseek-flash",
                "markdown": "# 远端总结",
                "chapters": [],
                "key_takeaways": [],
                "assessment_events": [],
                "review_views": _views(),
            },
        }

    def test_task_funnel_passes_review_views(self):
        self.app._import_remote_summary_result("course", "sub-1", {
            "input_hash": "b" * 64,
            "status": "completed",
            "outputs": self._summary_outputs(),
            "metrics": {},
            "warnings": [],
        }, task_id="task-p2")
        stored = self.app.learning_store.find_ai_artifact("sub-1", "review_views")
        self.assertIsNotNone(stored, "任务漏斗须把 review_views 同批入库")
        self.assertEqual(stored["prompt_version"], "review-views-v1")

    def test_llm_pending_local_completion_path_same_rights(self):
        """llm_pending 领回共用任务漏斗：create_summary 返回带 review_views
        键时同样落库（合同 §② 派生收口在 create_summary 内部→三路同权）。"""
        from unittest.mock import patch

        local_summary = {
            "model": "deepseek-flash", "markdown": "# 本地领回", "chapters": [],
            "key_takeaways": [], "assessment_events": [],
            "review_views": _views(),
        }
        worker_llm = self.app._worker_llm_module()
        with patch.object(worker_llm, "create_summary", return_value=local_summary), \
                patch.object(worker_llm, "usage_snapshot", return_value={"total_tokens": 3}), \
                patch.object(self.app, "_deepseek_key", return_value="test-key"):
            self.app._import_remote_summary_result("course", "sub-1", {
                "input_hash": "c" * 64,
                "status": "llm_pending",
                "outputs": {"llm_pending": {
                    "title": "Lecture",
                    "transcript": [{"start_ms": 0, "end_ms": 1000, "text": "段落"}],
                    "ppt_pages": [],
                    "evidence_packet": None,
                    "course_context": None,
                    "reason_code": "llm_remote_failed",
                }},
                "metrics": {},
                "warnings": [],
            }, task_id="task-p2-pending")
        stored = self.app.learning_store.find_ai_artifact("sub-1", "review_views")
        self.assertIsNotNone(stored, "领回路径与远端导入零特判同权")

    def test_automation_funnel_passes_review_views(self):
        self.app._import_automation_result({"outputs": {
            "cloud_catalog": {"course_id": "course", "lecture": {"sub_id": "sub-1"}},
            **self._summary_outputs(),
        }})
        stored = self.app.learning_store.find_ai_artifact("sub-1", "review_views")
        self.assertIsNotNone(stored, "automation 漏斗与任务漏斗同权")

    def test_summary_failure_interrupts_both_kinds(self):
        store = self.app.learning_store
        store.begin_ai_artifact(
            course_id="course", sub_id="sub-1", kind="timestamp_summary",
            input_hash="d" * 64, prompt_version="actions-summary-v1",
        )
        store.begin_ai_artifact(
            course_id="course", sub_id="sub-1", kind="review_views",
            input_hash="d" * 64, prompt_version="review-views-v1",
        )
        self.app._fail_remote_result_import(
            {"task_id": "task-gone", "kind": "summary", "sub_id": "sub-1"},
            RuntimeError("boom"),
        )
        for kind in ("timestamp_summary", "review_views"):
            row = store.find_ai_artifact("sub-1", kind, input_hash="d" * 64)
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed", f"{kind} 须与总结同批中断收口")


if __name__ == "__main__":
    unittest.main()
