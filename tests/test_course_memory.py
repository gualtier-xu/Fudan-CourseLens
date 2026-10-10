"""COURSEMEM-1 课程字幕记忆行为钉。

现场：SUBTITLE-DEEP-1 包B 的客户端域实现——字幕结果里 worker 已透传的
``deep_audit`` 审计账此前零消费。本文件钉住加性行为：
1. 沉淀幂等（重复回放同一结果零新增）与 fail-closed（审计账缺失/损坏/
   条目不成形一律跳过，绝不抛）；
2. 标点-only 修正是通用职责不占课程名额，超长段跳过，封顶 200 与 worker
   ``resolve_course_examples`` 的 ``raw[:200]`` 切片语义一致；
3. 字幕 job payload 注入面：``course_context``（course_title/teacher_names/
   term_label 平铺标量，缺省省键）+ ``examples``（课程示例，worker 检索侧
   恒排通用示例前）；两者缺席=旧行为零变化；
4. 学习页轻量入口的真实计数（course_review 响应 course_memory.examples）。

worker 读侧合同本身（resolve_course_examples/_select_examples）由 worker
tests/test_term_proofread.py 钉住，不在此重复。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

from path_utils import PROJECT_ROOT
from src.application import CourseLensApplication
from src.runtime.course_memory import (
    COURSE_MEMORY_MAX_EXAMPLES,
    course_memory_count,
    load_course_examples,
    memory_path,
    sink_course_examples,
)


def _audit(before: str, after: str) -> dict:
    return {"start_ms": 0, "end_ms": 1000, "before": before, "after": after}


def _subtitle_result(audit: list[dict] | None) -> dict:
    subtitle = {
        "mode": "automatic",
        "segments": [{"start_ms": 0, "end_ms": 1000, "text": "费米能级"}],
        "srt": "1\n00:00:00,000 --> 00:00:01,000\n费米能级\n",
        "vtt": "WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.000\n费米能级\n",
    }
    if audit:
        subtitle["deep_audit"] = audit
    return {"outputs": {"subtitle": subtitle}, "metrics": {}}


class CourseMemorySinkTests(unittest.TestCase):
    """沉淀/读取的纯模块行为。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.output_dir = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_sink_builds_worker_contract_shape(self) -> None:
        added = sink_course_examples(
            self.output_dir, "course-1",
            [_audit("这个费米能及很重要", "这个费米能级很重要，")],
        )
        self.assertEqual(added, 1)
        examples = load_course_examples(self.output_dir, "course-1")
        self.assertEqual(examples, [{
            "input": [{"id": "e0", "text": "这个费米能及很重要"}],
            "ops": [{"id": "e0", "old": "这个费米能及很重要", "new": "这个费米能级很重要，"}],
        }])
        self.assertEqual(course_memory_count(self.output_dir, "course-1"), 1)

    def test_sink_idempotent_on_replay(self) -> None:
        audit = [_audit("这个费米能及很重要", "这个费米能级很重要，")]
        self.assertEqual(sink_course_examples(self.output_dir, "course-1", audit), 1)
        # 同一结果重复回放：审计账逐条相同 → 零新增
        self.assertEqual(sink_course_examples(self.output_dir, "course-1", audit), 0)
        self.assertEqual(course_memory_count(self.output_dir, "course-1"), 1)

    def test_sink_skips_punctuation_only_and_noop_entries(self) -> None:
        added = sink_course_examples(self.output_dir, "course-1", [
            _audit("这个器件的电阻", "这个器件的电阻。"),  # 仅补标点：通用职责
            _audit("完全相同", "完全相同"),  # 无变化
            _audit("", ""),  # 空
            "not-a-dict",  # 不成形
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
        ])
        self.assertEqual(added, 1)
        self.assertEqual(course_memory_count(self.output_dir, "course-1"), 1)

    def test_sink_fail_closed_on_missing_or_corrupt(self) -> None:
        # 审计账缺席/类型不对/为空：零新增，不抛
        self.assertEqual(sink_course_examples(self.output_dir, "course-1", None), 0)
        self.assertEqual(sink_course_examples(self.output_dir, "course-1", "nope"), 0)
        self.assertEqual(sink_course_examples(self.output_dir, "course-1", []), 0)
        self.assertEqual(course_memory_count(self.output_dir, "course-1"), 0)
        # 记忆文件损坏：读侧退空、写侧照常重建
        path = memory_path(self.output_dir, "course-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt json", encoding="utf-8")
        self.assertEqual(load_course_examples(self.output_dir, "course-1"), [])
        self.assertEqual(sink_course_examples(
            self.output_dir, "course-1",
            [_audit("这个费米能及很重要", "这个费米能级很重要，")],
        ), 1)
        self.assertEqual(course_memory_count(self.output_dir, "course-1"), 1)

    def test_sink_skips_oversized_segments(self) -> None:
        long_text = "很" * 500
        added = sink_course_examples(self.output_dir, "course-1", [
            _audit(long_text, long_text.replace("很", "狠", 1)),
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
        ])
        self.assertEqual(added, 1)

    def test_sink_caps_at_worker_limit(self) -> None:
        audit = [
            _audit(f"第{i}个费米能及的例子", f"第{i}个费米能级的例子，")
            for i in range(COURSE_MEMORY_MAX_EXAMPLES + 10)
        ]
        added = sink_course_examples(self.output_dir, "course-1", audit)
        self.assertEqual(added, COURSE_MEMORY_MAX_EXAMPLES)
        self.assertEqual(course_memory_count(self.output_dir, "course-1"), COURSE_MEMORY_MAX_EXAMPLES)
        # 已满后再沉淀：FIFO 封顶语义下零新增（与 worker raw[:200] 一致）
        self.assertEqual(sink_course_examples(
            self.output_dir, "course-1",
            [_audit("新的费米能及", "新的费米能级，")],
        ), 0)
        self.assertEqual(course_memory_count(self.output_dir, "course-1"), COURSE_MEMORY_MAX_EXAMPLES)

    def test_course_isolation(self) -> None:
        sink_course_examples(self.output_dir, "course-1", [
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
        ])
        sink_course_examples(self.output_dir, "course-2", [
            _audit("小波基的选取原里", "小波基的选取原理，"),
        ])
        self.assertEqual(course_memory_count(self.output_dir, "course-1"), 1)
        self.assertEqual(course_memory_count(self.output_dir, "course-2"), 1)
        self.assertNotIn(
            "费米", json.dumps(load_course_examples(self.output_dir, "course-2"), ensure_ascii=False)
        )
        self.assertNotEqual(memory_path(self.output_dir, "course-1"), memory_path(self.output_dir, "course-2"))

    def test_load_filters_malformed_examples(self) -> None:
        path = memory_path(self.output_dir, "course-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "version": 1, "course_id": "course-1",
            "examples": [
                {"input": [{"id": "e0", "text": "好的示例"}], "ops": [{"id": "e0", "old": "好的示例", "new": "好的示例，"}]},
                {"input": [], "ops": []},  # 空 input：整条丢
                {"input": [{"id": "e0", "text": "缺 ops"}]},  # 缺 ops：整条丢
                {"input": [{"id": "", "text": "无 id"}], "ops": [{"id": "e0", "old": "a", "new": "b"}]},
                "not-a-dict",
            ],
        }, ensure_ascii=False), encoding="utf-8")
        examples = load_course_examples(self.output_dir, "course-1")
        self.assertEqual(len(examples), 1)
        self.assertEqual(examples[0]["input"][0]["text"], "好的示例")

    def test_memory_path_scoped_by_course_id(self) -> None:
        self.assertEqual(
            memory_path(self.output_dir, "course-1"), memory_path(self.output_dir, "course-1")
        )
        self.assertTrue(str(memory_path(self.output_dir, "course/x?1")).endswith(".json"))
        self.assertEqual(load_course_examples(self.output_dir, ""), [])
        self.assertEqual(course_memory_count(self.output_dir, ""), 0)


class _CaptureCoordinator:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def execute(self, *, task_id, build_job, import_result, cancel_requested, progress, **_: object) -> None:
        self.jobs.append(build_job("test-public-key"))


class CourseMemoryApplicationTests(unittest.TestCase):
    """应用层接线：payload 注入、导入沉淀、course_review 计数。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.app.catalog_repository.upsert_course(
            "course", "高等数学", teacher="张三", term="2026-2027·第1学期"
        )
        self.app.catalog_repository.upsert_lecture(
            "course",
            {
                "sub_id": "lecture",
                "sub_title": "第1讲",
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

    def _dispatch_job(self) -> dict:
        coordinator = _CaptureCoordinator()
        lease_workflows: list[str | None] = []

        @contextmanager
        def fake_slot(task_id, **_: object):
            yield

        @contextmanager
        def fake_lease(task_id: str, *, workflow: str | None = None):
            lease_workflows.append(workflow)
            yield coordinator

        self.app._cloud_run_slot = fake_slot
        self.app._leased_remote_coordinator = fake_lease
        self.app._generate_subtitle_remote(
            "course", "lecture", task_id="task-test-1", proofread=False
        )
        self.assertEqual(len(coordinator.jobs), 1)
        # N1-ROUTING：字幕载荷带 media 媒体腿 → 派发面维持 process.yml。
        self.assertEqual(lease_workflows, ["process.yml"])
        return coordinator.jobs[0]

    def test_dispatch_injects_metadata_and_examples(self) -> None:
        sink_course_examples(self.output_dir_holder(), "course", [
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
        ])
        job = self._dispatch_job()
        payload = job["payload"]
        self.assertEqual(payload["course_context"], {
            "course_title": "高等数学",
            "teacher_names": "张三",
            "term_label": "2026-2027·第1学期",
        })
        self.assertEqual(len(payload["examples"]), 1)
        self.assertEqual(payload["examples"][0]["ops"][0]["new"], "这个费米能级很重要，")

    def test_dispatch_omits_absent_metadata_and_examples(self) -> None:
        # 只有课程名（无教师/学期）：course_context 只带 course_title
        self.app.catalog_repository.upsert_course("course", "高等数学")
        job = self._dispatch_job()
        self.assertEqual(job["payload"].get("course_context"), {"course_title": "高等数学"})
        # 零积累：examples 键缺席 = 旧行为零变化
        self.assertNotIn("examples", job["payload"])

    def test_dispatch_survives_memory_read_failure(self) -> None:
        # 记忆文件损坏：注入面 fail-closed，派发照常（examples 缺席）
        path = memory_path(self.app.output_dir, "course")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt", encoding="utf-8")
        job = self._dispatch_job()
        self.assertNotIn("examples", job["payload"])
        self.assertEqual(job["payload"]["course_context"]["course_title"], "高等数学")

    def test_import_sinks_deep_audit_idempotently(self) -> None:
        audit = [
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
            _audit("这里只是加个标点", "这里只是加个标点。"),  # 仅标点：不沉淀
        ]
        self.app._import_remote_subtitle("course", "lecture", _subtitle_result(audit))
        self.assertEqual(course_memory_count(self.app.output_dir, "course"), 1)
        # 同一结果再次导入（恢复链回放）：零新增
        self.app._import_remote_subtitle("course", "lecture", _subtitle_result(audit))
        self.assertEqual(course_memory_count(self.app.output_dir, "course"), 1)

    def test_import_without_audit_leaves_no_memory(self) -> None:
        self.app._import_remote_subtitle("course", "lecture", _subtitle_result(None))
        self.assertEqual(course_memory_count(self.app.output_dir, "course"), 0)

    def test_course_review_reports_memory_count(self) -> None:
        review = self.app.course_review("course")
        self.assertEqual(review["course_memory"], {"examples": 0})
        sink_course_examples(self.app.output_dir, "course", [
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
            _audit("极限的存在性定理怎么瘦", "极限的存在性定理怎么叙述，"),
        ])
        review = self.app.course_review("course")
        self.assertEqual(review["course_memory"], {"examples": 2})

    def output_dir_holder(self) -> Path:
        return self.app.output_dir


if __name__ == "__main__":
    unittest.main()
