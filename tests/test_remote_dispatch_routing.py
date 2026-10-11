"""N1-ROUTING：按任务种类与载荷媒体面派发工作流（f08b6c0 llm.yml 快路径）。

钉三件事：
1. 判别式——载荷无 media 键=纯 LLM 任务面（总结/章节、质量抽检，
   含带 PPT 的总结：其 OCR 只需 rapidocr，f08b6c0 口径）→ llm.yml；
   载荷带 media 键=真实媒体腿（现役唯一=字幕）→ process.yml 字节不变；
   learning_pack 白名单（D-20261009-13）→ 恒 process.yml：worker N20
   profile 合同（runner ``_MEDIA_PROFILE_REQUIRED_KINDS``）要求其必须跑
   process-v1，误派 llm.yml 会被 fail-closed 拒跑（任务 06b42931 实证）。
2. 汇总站真接线——_run_summary_remote 实际派发 llm.yml，且实发 payload
   确无 media 键（PPT 变体照样，PPT 腿=source_session.slides）；问答链
   真接线钉在 test_p3_deep_mode / test_ai_assessment_answer 的
   drain_question_queue（learning_pack → process.yml）。
3. fail-closed 边界——双向误派均有既有语义兜底（媒体入 llm.yml 由 worker
   无模型闭集拒跑；纯 LLM 留 process.yml 仅多付环境税），本文件钉的
   正是"路由正确使兜底永不触发"。
"""

from __future__ import annotations

import contextlib
import tempfile
import unittest
from pathlib import Path

from path_utils import PROJECT_ROOT
from src.application import CourseLensApplication, _remote_dispatch_workflow


def _summary_payload_shape(*, include_ppt: bool) -> dict:
    """汇总站实发载荷的媒体面相关形状（f08b6c0 口径的路由输入）。"""
    payload = {"transcript": [{"start": 0.0, "text": "第一段"}], "slides": []}
    if include_ppt:
        payload["source_session"] = {"provider": "runner-session-v1", "slides": True}
    return payload


class RemoteDispatchWorkflowPredicateTests(unittest.TestCase):
    """判别式单元面：以真实载荷形状钉四站点路由结果。"""

    def test_subtitle_media_leg_routes_to_process(self) -> None:
        self.assertEqual(
            _remote_dispatch_workflow({"media": {"start_seconds": 0.0}}),
            "process.yml",
        )

    def test_learning_pack_whitelist_routes_to_process(self) -> None:
        # D-20261009-13 防回归钉：learning_pack 在 worker N20 profile 合同
        # 闭集内（必须 process-v1），载荷形状（query/evidence/glossary 无
        # media）不再决定归属——恒 process.yml。
        self.assertEqual(
            _remote_dispatch_workflow(
                {"query": "", "evidence": [], "glossary": {}}, job_kind="learning_pack",
            ),
            "process.yml",
        )

    def test_pure_llm_shapes_route_to_llm(self) -> None:
        self.assertEqual(_remote_dispatch_workflow({"subtitle_sample": {}}), "llm.yml")
        self.assertEqual(
            _remote_dispatch_workflow({"title": "", "transcript": []}), "llm.yml"
        )

    def test_ppt_summary_shape_routes_to_llm(self) -> None:
        # PPT 归属钉：带 PPT 的总结 OCR 只需 rapidocr（f08b6c0），仍属 llm.yml。
        self.assertEqual(
            _remote_dispatch_workflow(_summary_payload_shape(include_ppt=True)),
            "llm.yml",
        )

    def test_media_absent_empty_payload_routes_to_llm(self) -> None:
        self.assertEqual(_remote_dispatch_workflow({}), "llm.yml")


class _CaptureWorkflowCoordinator:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def execute(
        self, *, task_id, build_job, import_result, cancel_requested, progress, **_: object
    ) -> dict:
        self.jobs.append(build_job("test-public-key"))
        return {"input_hash": "test-input-hash"}


class SummaryDispatchRoutingTests(unittest.TestCase):
    """汇总站真接线：实派 llm.yml 且实发 payload 确无 media 键。"""

    def setUp(self) -> None:
        # 管理路径守卫要求应用根在项目/数据目录内：锚 runtime/cache（course_memory 同款）。
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.app._credentials = {"student_id": "configured", "password": "configured"}
        self.app._deepseek_key = lambda: ""
        self.app.subtitle_segments = lambda sub_id: {
            "segments": [{"start": 0.0, "text": "测试段"}]
        }
        self.coordinator = _CaptureWorkflowCoordinator()
        self.lease_workflows: list[str | None] = []

        @contextlib.contextmanager
        def fake_lease(task_id: str, *, workflow: str | None = None):
            self.lease_workflows.append(workflow)
            yield self.coordinator

        @contextlib.contextmanager
        def fake_slot(task_id, **_: object):
            yield

        self.app._leased_remote_coordinator = fake_lease
        self.app._cloud_run_slot = fake_slot

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def _run(self, *, include_ppt: bool) -> dict:
        return self.app._run_summary_remote(
            {
                "course_id": "course",
                "sub_id": "lecture",
                "task_id": "task-routing-1",
                "include_ppt": include_ppt,
            },
            on_progress=lambda progress: None,
        )

    def test_summary_without_ppt_dispatches_llm(self) -> None:
        self._run(include_ppt=False)
        self.assertEqual(self.lease_workflows, ["llm.yml"])
        job = self.coordinator.jobs[0]
        self.assertEqual(job["job_kind"], "summary")
        self.assertNotIn("media", job["payload"])

    def test_ppt_summary_still_dispatches_llm_with_slides_session(self) -> None:
        self._run(include_ppt=True)
        self.assertEqual(self.lease_workflows, ["llm.yml"])
        job = self.coordinator.jobs[0]
        # PPT 腿在 source_session，不在载荷媒体面：media 键缺席=llm.yml 归属不变。
        self.assertNotIn("media", job["payload"])
        self.assertTrue(job["payload"]["source_session"]["slides"])


if __name__ == "__main__":
    unittest.main()
