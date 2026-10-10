"""P11-CONTRACT-1 PKG-B 回归钉：质量抽检客户端链（job kind=quality_judge）。

覆盖判据（全部合成夹具，零真实账号、零外呼；合同=P11-CONTRACT-1 冻结面，
schema/采样算式/闭集不得就地改）：

1. 采样算式冻结：k=min(40,max(8,ceil(0.03*N)))、stride=max(1,N//k)、确定性
   同输入恒同样本、无 env 覆写面；
2. input_hash 冻结公式：canonical JSON 同族；同态同哈希、重字幕/总结换版变哈希；
3. preflight 五连闭集拒绝（顺序冻结，全拒路径零任务零外联）；
4. 幂等（同 hash 活跃复用 created=false）与 in_flight 409（异 hash 在飞）；
   failed 同 hash re-POST=复用任务行 retry 语义；
5. 分派形状：config_key=quality-judge:<sub>:<hash>、v3 metadata sealed、
   payload 冻结键、job 信封（kind/requested_outputs/pipeline/expires/secrets）；
6. import_result：报告落库 kind=quality_report/prompt_version=quality-judge-v1、
   content 冻结五键、闭集校验逐项（越集/越界/自由文本丢弃+计数）、warn 优先
   截断 24、双 mode 无效 fail-closed=judge_output_invalid 终态；
7. deterministic 现算六键（对 DB 段与总结章节）；
8. 消耗记账：import_result 落任务行绝对消耗、task_usage_month 自动覆盖；
9. ai_usage_month 加性键 quality_checks；
10. 错误闭集：云侧旧镜像 unsupported job kind=worker_kind_unsupported；
    RemoteTaskPaused=诚实终态失败；瞬态自动重试恰一次；
11. 任务抽屉标签闭集（quality_judge=质量抽检）且不进 LEARNING_PACK_KINDS。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from src.application import (
    CourseLensApplication,
    QualityJudgeRequestError,
    _normalize_quality_judge_report,
    _quality_input_hash,
    _quality_sample_indices,
    _quality_transcript_digest,
)
from tests.frontend_family import family_text
from src.remote.coordinator import RemoteTaskPaused
from src.runtime.catalog_repository import CatalogRepository
from src.runtime.course_memory import memory_path, sink_course_examples
from src.runtime.course_memory_feedback import dismiss_term_mapping
from src.runtime.learning_store import LearningStore, _quality_deterministic_counts
from src.runtime.task_store import TaskStore

COURSE = "crs-p11q"
SUB = "sub-p11q-1"
USAGE = {"deepseek_tokens": 90, "elapsed_seconds": 2.5}


def _segments() -> list[dict]:
    """18 段合成终稿（全部合法——replace_transcript_segments 写入端本有
    免疫纪律，脏行种不进 DB；脏数据的计数检验走纯函数测试面）。"""
    rows = []
    for index in range(18):
        start = 1_000 * (index + 1)
        rows.append({
            "index": index,
            "start_ms": start,
            "end_ms": start + 900,
            "text": f"第{index}段内容",
        })
    return rows


def _dirty_segments() -> list[dict]:
    """含 1 处非单调、1 处 duration≤0、1 处空 text 的段列表（纯函数面用）。"""
    rows = _segments()
    rows[2]["start_ms"] = 500          # 非单调（< 前一段 start）
    rows[5]["end_ms"] = rows[5]["start_ms"]  # duration = 0
    rows[7]["text"] = "   "            # 空 text
    return rows


def _finding(target="segment", position=0, dimension="term_fidelity",
             code="glossary_violation", severity="warn", **extra) -> dict:
    item = {"target": target, "position": position, "dimension": dimension,
            "code": code, "severity": severity}
    item.update(extra)
    return item


def _judge_report(subtitle_findings=None, summary_findings=None) -> dict:
    report: dict = {"schema_version": 1, "subtitle": None, "summary": None}
    if subtitle_findings is not None:
        report["subtitle"] = {"sample_size": 8, "targets_total": 8, "findings": subtitle_findings}
    if summary_findings is not None:
        report["summary"] = {"targets_total": 4, "findings": summary_findings}
    return report


class _FakeCoordinator:
    """替掉远端协调器：按次回放合成结果或抛合成错误，零网络。"""

    def __init__(self, outputs=None, *, metrics=None, failures=()):
        self.outputs = dict(outputs or {})
        self.metrics = dict(metrics or {})
        self.failures = list(failures)
        self.jobs: list[dict] = []

    def execute(self, *, task_id, build_job, import_result, cancel_requested=None, progress=None):
        self.jobs.append(build_job("synthetic-public-key"))
        if self.failures:
            raise self.failures.pop(0)
        result = {"outputs": dict(self.outputs), "metrics": dict(self.metrics)}
        import_result(result)
        return result


class _QualityHarness(unittest.TestCase):
    """合成 CourseLensApplication 壳：只填质量抽检链真正会碰到的运行时状态。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = LearningStore(self.root / "learning.db")
        self.tasks = TaskStore(self.root / "tasks.db")
        self.catalog = CatalogRepository(self.root / "catalog.db")
        self.app = CourseLensApplication.__new__(CourseLensApplication)
        self.app.learning_store = self.store
        self.app.task_store = self.tasks
        self.app.catalog_repository = self.catalog
        self.app.output_dir = self.root
        self.app._lock = threading.RLock()
        self.app._paused = False
        self.app._quality_queue = []
        self.app._quality_current = None
        self.app._generation_workers_stop = threading.Event()
        self.app._cloud_run_condition = threading.Condition()
        self.app._cloud_runs_active = 0
        self.app._deepseek_api_key = "sk-synthetic"
        self.catalog.upsert_course(COURSE, "概率论")
        self.catalog.upsert_lecture(COURSE, {"sub_id": SUB, "sub_title": "第五章 大数定律"})
        self.coordinator = _FakeCoordinator()
        self.captured_lease_workflows: list[str | None] = []
        self._patchers = [
            mock.patch.object(
                CourseLensApplication, "_ensure_quality_judge_worker", lambda self: None
            ),
            mock.patch.object(
                CourseLensApplication, "_leased_remote_coordinator", self._fake_coordinator_lease
            ),
            mock.patch.object(CourseLensApplication, "_cloud_run_slot", self._no_cloud_slot),
            mock.patch.object(
                CourseLensApplication, "remote_compute_snapshot",
                lambda self: {"configured": True, "verified": True},
            ),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.store.close)

    @contextlib.contextmanager
    def _fake_coordinator_lease(self, _task_id: str, *, workflow: str | None = None):
        self.captured_lease_workflows.append(workflow)
        yield self.coordinator

    @contextlib.contextmanager
    def _no_cloud_slot(self, _task_id: str, *, cancel_requested=None, on_wait=None):
        yield

    # -- 夹具 ---------------------------------------------------------------

    def seed_transcript(self, segments: list[dict] | None = None) -> list[dict]:
        rows = _segments() if segments is None else segments
        self.store.replace_transcript_segments(
            SUB,
            source_path="synthetic.srt", source_mtime_ns=111, source_size=22,
            segments=rows,
        )
        return rows

    def seed_summary(self, *, chapters=None, takeaways=None) -> dict:
        chapters = chapters if chapters is not None else [
            {"title": "开场", "summary": "引入大数定律", "start_ms": 0},
            {"title": "证明", "summary": "切比雪夫不等式", "start_ms": 5_000},
        ]
        takeaways = takeaways if takeaways is not None else ["大数定律需要独立同分布"]
        self.store.import_remote_summary(
            course_id=COURSE, sub_id=SUB, input_hash="b" * 64, model="deepseek-flash",
            markdown="合成概览：大数定律。",
            chapters=chapters, ppt_pages=[], key_takeaways=takeaways,
        )
        return {"chapters": chapters, "takeaways": takeaways}

    def request(self) -> dict:
        return self.app.request_quality_judge(SUB)

    def quality_tasks(self) -> list[dict]:
        return [t for t in self.tasks.list_tasks(limit=200) if t.get("kind") == "quality_judge"]

    def drain(self) -> None:
        """同步跑完队列（生产是后台线程，测试里就地跑）。"""
        self.app._run_quality_queue()
        # N1-ROUTING：质量抽检载荷媒体面缺席 → 一律 llm.yml 快路径。
        self.assertTrue(all(w == "llm.yml" for w in self.captured_lease_workflows))


class SamplingFormulaTests(_QualityHarness):
    """合同冻结采样算式（纯函数面，无 env 覆写）。"""

    def test_empty_and_tiny_inputs(self):
        self.assertEqual(_quality_sample_indices(0), [])
        self.assertEqual(_quality_sample_indices(5), [0, 1, 2, 3, 4])

    def test_floor_and_rate_and_cap(self):
        # N=100：ceil(3)=3 → 下限 8；stride=12
        self.assertEqual(_quality_sample_indices(100), [0, 12, 24, 36, 48, 60, 72, 84])
        # N=1000：ceil(30)=30；stride=33
        thousand = _quality_sample_indices(1000)
        self.assertEqual(len(thousand), 30)
        self.assertEqual(thousand, list(range(0, 1000, 33))[:30])
        # N=3000：ceil(90) → 上限 40；stride=75
        capped = _quality_sample_indices(3000)
        self.assertEqual(len(capped), 40)
        self.assertEqual(capped, list(range(0, 3000, 75))[:40])

    def test_deterministic_same_input_same_sample(self):
        self.assertEqual(_quality_sample_indices(777), _quality_sample_indices(777))


class InputHashTests(_QualityHarness):
    """合同冻结 input_hash 公式（canonical JSON 家族）。"""

    def test_frozen_formula(self):
        digest = hashlib.sha256(b"[]").hexdigest()
        expected = hashlib.sha256(json.dumps({
            "schema": "quality-judge-v1",
            "sub_id": SUB,
            "segment_indices": [0, 4],
            "transcript_digest": digest,
            "summary_input_hash": "",
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(_quality_input_hash(SUB, [0, 4], digest, ""), expected)

    def test_same_state_same_hash_and_material_changes_rehash(self):
        self.seed_transcript()
        self.seed_summary()
        rows = self.store.get_transcript_segments(SUB)
        indices = _quality_sample_indices(len(rows))
        digest = _quality_transcript_digest(rows)
        base = _quality_input_hash(SUB, indices, digest, "b" * 64)
        self.assertEqual(
            base,
            _quality_input_hash(SUB, list(indices), _quality_transcript_digest(rows), "b" * 64),
            "同态同哈希",
        )
        self.assertNotEqual(
            base, _quality_input_hash(SUB, indices, "f" * 64, "b" * 64), "重字幕变哈希",
        )
        self.assertNotEqual(
            base, _quality_input_hash(SUB, indices, digest, ""), "总结换版变哈希",
        )


class PreflightTests(_QualityHarness):
    """preflight 五连闭集拒绝（顺序冻结，零任务零外联）。"""

    def test_sub_id_required(self):
        with self.assertRaises(QualityJudgeRequestError) as caught:
            self.app.request_quality_judge("  ")
        self.assertEqual(caught.exception.code, "sub_id_required")
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.quality_tasks(), [])

    def test_lecture_not_in_catalog(self):
        with self.assertRaises(QualityJudgeRequestError) as caught:
            self.app.request_quality_judge("sub-unknown")
        self.assertEqual(caught.exception.code, "lecture_not_in_catalog")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.quality_tasks(), [])

    def test_no_material_rejected_before_key(self):
        self.catalog.upsert_lecture(COURSE, {"sub_id": "sub-empty", "sub_title": "空讲次"})
        with mock.patch.object(CourseLensApplication, "_deepseek_key", lambda self: ""):
            with self.assertRaises(QualityJudgeRequestError) as caught:
                self.app.request_quality_judge("sub-empty")
        self.assertEqual(caught.exception.code, "quality_no_material")
        self.assertEqual(self.quality_tasks(), [])

    def test_deepseek_key_required(self):
        self.seed_transcript()
        with mock.patch.object(CourseLensApplication, "_deepseek_key", lambda self: ""):
            with self.assertRaises(QualityJudgeRequestError) as caught:
                self.request()
        self.assertEqual(caught.exception.code, "deepseek_key_required")
        self.assertEqual(self.quality_tasks(), [])

    def test_remote_not_configured(self):
        self.seed_transcript()
        cases = (
            {"configured": False, "verified": True},
            {"configured": True, "verified": False},
        )
        for snapshot in cases:
            with mock.patch.object(CourseLensApplication, "remote_compute_snapshot", lambda self, s=snapshot: s):
                with self.assertRaises(QualityJudgeRequestError) as caught:
                    self.request()
            self.assertEqual(caught.exception.code, "remote_not_configured")
        self.assertEqual(self.quality_tasks(), [])


class DispatchAndIdempotencyTests(_QualityHarness):
    """分派形状 / 幂等 / in_flight 409 / failed re-POST retry 语义。"""

    def test_enqueue_shape_frozen(self):
        self.seed_transcript()
        self.seed_summary()
        receipt = self.request()
        self.assertTrue(receipt["created"])
        tasks = self.quality_tasks()
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["course_id"], COURSE)
        self.assertEqual(task["sub_id"], SUB)
        payload = task["payload"]
        self.assertTrue(str(task["config_key"]).startswith(f"quality-judge:{SUB}:"))
        self.assertEqual(payload["input_hash"], receipt["task"]["payload"]["input_hash"])
        self.assertEqual(payload["title"], "第五章 大数定律")
        self.assertEqual(payload["subtitle_sample"]["total_segments"], 18)
        self.assertEqual(len(payload["subtitle_sample"]["segments"]), 8)
        self.assertEqual(len(payload["summary_pack"]["transcript"]), 18)
        self.assertEqual(len(payload["summary_pack"]["chapters"]), 2)
        self.assertEqual(payload["summary_pack"]["key_takeaways"], ["大数定律需要独立同分布"])
        self.assertNotIn("glossary", payload, "空术语表省键（:6189 家规）")
        metadata = self.tasks.get_v3_metadata(task["task_id"])
        self.assertEqual(metadata["privacy_state"], "sealed")
        self.assertEqual(metadata["requested_outputs"], ["quality_judge"])
        self.assertEqual(metadata["input_hash"], payload["input_hash"])
        self.assertIn("质量抽检", str(task.get("progress", {}).get("label") or ""))
        self.assertEqual(len(self.app._quality_queue), 1, "队列持 task_id 载荷")

    def test_job_envelope_frozen(self):
        self.seed_transcript()
        self.seed_summary()
        self.request()
        self.coordinator.outputs = {"quality_judge": _judge_report(subtitle_findings=[])}
        self.coordinator.metrics = dict(USAGE)
        self.drain()
        self.assertEqual(len(self.coordinator.jobs), 1)
        job = self.coordinator.jobs[0]
        self.assertEqual(job["job_kind"], "quality_judge")
        self.assertEqual(job["requested_outputs"], ["quality_judge"])
        self.assertEqual(job["pipeline"], {"version": "quality-judge-v1", "llm": "deepseek-flash"})
        self.assertEqual(round(job["expires_at"] - job["created_at"]), 600)
        self.assertEqual(job["secrets"], {"deepseek_api_key": "sk-synthetic"})
        self.assertEqual(job["payload"]["title"], "第五章 大数定律")
        self.assertEqual(len(job["payload"]["subtitle_sample"]["segments"]), 8)
        self.assertNotIn("summary_pack", {}, "两键按需携带")

    def test_glossary_key_carries_course_memory_terms(self):
        self.seed_transcript()
        with mock.patch.object(
            CourseLensApplication, "_course_memory_terms", lambda self, _cid: ["大数定律"]
        ):
            self.request()
        payload = self.quality_tasks()[0]["payload"]
        self.assertEqual(payload["glossary"], ["大数定律"])

    def test_same_state_repost_reuses_active_task(self):
        self.seed_transcript()
        first = self.request()
        second = self.request()
        self.assertFalse(second["created"])
        self.assertEqual(second["task"]["task_id"], first["task"]["task_id"])
        self.assertEqual(len(self.quality_tasks()), 1)

    def test_different_material_in_flight_rejected_409(self):
        self.seed_transcript()
        self.request()
        rows = self.seed_transcript(_segments()[:10])  # 重字幕 → digest 变
        self.assertEqual(len(rows), 10)
        with self.assertRaises(QualityJudgeRequestError) as caught:
            self.request()
        self.assertEqual(caught.exception.code, "quality_task_in_flight")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.quality_tasks()), 1, "一讲同时只跑一份质检")

    def test_failed_same_hash_repost_retries_same_row(self):
        # judge_output_invalid=fail-closed 终态（不进瞬态自动重试）——re-POST
        # 复用同一任务行走 retry 语义。
        self.seed_transcript()
        self.coordinator.failures = [
            QualityJudgeRequestError("judge_output_invalid"),
        ]
        receipt = self.request()
        self.drain()
        failed = self.tasks.get_task(receipt["task"]["task_id"])
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["error"], "judge_output_invalid")
        self.assertEqual(
            self.tasks.get_app_state("quality_transient_retry.v1", []),
            [],
            "fail-closed 码不烧瞬态重试名额",
        )
        self.coordinator.failures = []
        self.coordinator.outputs = {"quality_judge": _judge_report(subtitle_findings=[])}
        self.coordinator.metrics = dict(USAGE)
        retry = self.request()
        self.assertFalse(retry["created"])
        self.assertEqual(retry["task"]["task_id"], receipt["task"]["task_id"], "复用任务行")
        self.assertEqual(retry["task"]["state"], "queued")
        self.drain()
        self.assertEqual(self.tasks.get_task(receipt["task"]["task_id"])["state"], "completed")

    def test_transient_failure_auto_retries_exactly_once(self):
        self.seed_transcript()
        self.coordinator.failures = [
            RuntimeError("deepseek upstream failed"),
            RuntimeError("deepseek upstream failed"),
        ]
        self.coordinator.outputs = {"quality_judge": _judge_report(subtitle_findings=[])}
        self.coordinator.metrics = dict(USAGE)
        receipt = self.request()
        self.drain()
        task = self.tasks.get_task(receipt["task"]["task_id"])
        self.assertEqual(task["state"], "failed", "两次瞬态后诚实终态")
        self.assertEqual(task["error"], "deepseek_answer_failed", "瞬态码=既有漏斗码原样")
        self.assertEqual(
            len(self.coordinator.jobs), 2,
            "自动重试恰一次（初跑+1 次重试），绝不连环重试",
        )
        self.assertEqual(
            self.tasks.get_app_state("quality_transient_retry.v1", []),
            [receipt["task"]["task_id"]],
            "有界标记恰一次",
        )


class ImportAndStorageTests(_QualityHarness):
    """报告落库形状 / 闭集校验 / fail-closed / deterministic 现算。"""

    def _dispatch_and_import(self, report: dict, *, metrics: dict | None = None) -> dict:
        self.seed_transcript()
        self.seed_summary()
        self.coordinator.outputs = {"quality_judge": report}
        self.coordinator.metrics = dict(USAGE) if metrics is None else dict(metrics)
        receipt = self.request()
        self.drain()
        return receipt

    def test_valid_report_lands_frozen_shape(self):
        receipt = self._dispatch_and_import(_judge_report(
            subtitle_findings=[_finding(position=1, code="homophone_suspect")],
            summary_findings=[_finding(target="chapter", position=1, dimension="alignment",
                                      code="chapter_mislabel")],
        ))
        task = self.tasks.get_task(receipt["task"]["task_id"])
        self.assertEqual(task["state"], "completed")
        artifact = self.store.find_ai_artifact(SUB, "quality_report")
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact["prompt_version"], "quality-judge-v1")
        self.assertEqual(artifact["model"], "deepseek-flash")
        content = artifact["content"]
        for key in ("schema_version", "subtitle", "summary", "deterministic", "generation"):
            self.assertIn(key, content)
        self.assertEqual(content["schema_version"], 1)
        self.assertEqual(content["subtitle"]["sample_size"], 8)
        self.assertEqual(len(content["subtitle"]["findings"]), 1)
        self.assertEqual(content["summary"]["targets_total"], 4)  # 2 章 + 1 takeaway + 1 正文
        deterministic = content["deterministic"]
        self.assertEqual(deterministic["segments_total"], 18)
        self.assertEqual(deterministic["non_monotonic"], 0, "写入端免疫纪律下 DB 行恒干净")
        self.assertEqual(deterministic["bad_duration"], 0)
        self.assertEqual(deterministic["empty_text"], 0)
        self.assertEqual(deterministic["chapters_total"], 2)
        self.assertEqual(deterministic["chapter_out_of_range"], 0)
        self.assertEqual(content["generation"]["input_hash"], task["payload"]["input_hash"])
        self.assertEqual(content["generation"]["prompt_version"], "quality-judge-v1")
        self.assertIn("字幕抽检 8 段", artifact["content_markdown"])
        self.assertIn("术语疑点 1", artifact["content_markdown"])
        self.assertNotIn("第", artifact["content_markdown"], "报告摘要零原文内容")

    def test_chapter_out_of_range_counts_against_transcript(self):
        self.seed_transcript()
        self.seed_summary(chapters=[
            {"title": "开场", "summary": "引入", "start_ms": 0},
            {"title": "越界", "summary": "超出字幕范围", "start_ms": 999_999_999},
        ])
        self.coordinator.outputs = {"quality_judge": _judge_report(subtitle_findings=[])}
        self.coordinator.metrics = dict(USAGE)
        self.request()
        self.drain()
        content = self.store.find_ai_artifact(SUB, "quality_report")["content"]
        self.assertEqual(content["deterministic"]["chapters_total"], 2)
        self.assertEqual(content["deterministic"]["chapter_out_of_range"], 1)

    def test_closed_set_drops_and_counts(self):
        dirty = [
            _finding(position=0),
            _finding(position=99),                                       # position 越界
            _finding(dimension="factuality"),                            # dimension 越集
            _finding(code="made_up_code"),                               # code 越集
            _finding(severity="critical"),                               # severity 越集
            _finding(target="chapter"),                                  # target 越集
            _finding(position=True),                                     # bool 冒充 int
            _finding(note="自由文本引用"),                                 # 闭集字段合法：保留但剥自由文本
            "not-a-dict",                                                # 非 dict 条目
        ]
        cleaned, counts = _normalize_quality_judge_report(
            _judge_report(subtitle_findings=dirty),
            sample_size=8, summary_chapter_count=2, summary_takeaway_count=1,
        )
        self.assertEqual(len(cleaned["subtitle"]["findings"]), 2)
        self.assertEqual(counts["findings_invalid_dropped"], 7)
        self.assertEqual(counts["judge_mode_invalid"], 0)
        for item in cleaned["subtitle"]["findings"]:
            self.assertEqual(item["code"], "glossary_violation")
            self.assertNotIn("note", item, "报告零自由文本——note/quote 一律不入形状")

    def test_summary_position_bounds_and_body_zero(self):
        cleaned, _ = _normalize_quality_judge_report(
            _judge_report(summary_findings=[
                _finding(target="chapter", position=1, dimension="alignment", code="chapter_mislabel"),
                _finding(target="chapter", position=2, dimension="alignment", code="chapter_mislabel"),
                _finding(target="takeaway", position=0, dimension="factuality", code="no_source_support"),
                _finding(target="takeaway", position=5, dimension="factuality", code="no_source_support"),
                _finding(target="body", position=0, dimension="factuality", code="contradicts_source"),
                _finding(target="body", position=1, dimension="factuality", code="contradicts_source"),
            ]),
            sample_size=8, summary_chapter_count=2, summary_takeaway_count=1,
        )
        self.assertEqual(
            [item["target"] for item in cleaned["summary"]["findings"]],
            ["chapter", "takeaway", "body"],
            "越界 chapter/takeaway/body 条目全部丢弃",
        )

    def test_cap_truncates_warn_first(self):
        dirty = [_finding(position=7, severity="info", code="garbled",
                          dimension="readability") for _ in range(20)]
        dirty += [_finding(position=6) for _ in range(10)]  # warn 后到，但优先保留
        cleaned, _ = _normalize_quality_judge_report(
            _judge_report(subtitle_findings=dirty),
            sample_size=8, summary_chapter_count=0, summary_takeaway_count=0,
        )
        findings = cleaned["subtitle"]["findings"]
        self.assertEqual(len(findings), 24)
        self.assertTrue(all(item["severity"] == "warn" for item in findings[:10]))
        self.assertTrue(all(item["severity"] == "info" for item in findings[10:]))

    def test_mode_invalid_goes_null_and_both_invalid_fails_closed(self):
        # 单 mode 失格：该 mode 置 null + 计数，另一 mode 合法则照常保留。
        cleaned, counts = _normalize_quality_judge_report(
            {
                "schema_version": 1,
                "subtitle": {"findings": "not-a-list"},
                "summary": {"findings": [_finding(target="body", position=0,
                                                  dimension="factuality", code="empty_section")]},
            },
            sample_size=8, summary_chapter_count=2, summary_takeaway_count=1,
        )
        self.assertIsNone(cleaned["subtitle"])
        self.assertIsNotNone(cleaned["summary"])
        self.assertEqual(counts["judge_mode_invalid"], 1)
        # 双 mode 均无效（或失格+缺席）→ 整体 fail-closed。
        for report in (
            {"schema_version": 1, "subtitle": None, "summary": None},
            {"schema_version": 1, "subtitle": {"findings": 3}, "summary": None},
        ):
            with self.assertRaises(QualityJudgeRequestError) as caught:
                _normalize_quality_judge_report(
                    report, sample_size=0, summary_chapter_count=0, summary_takeaway_count=0,
                )
            self.assertEqual(caught.exception.code, "judge_output_invalid")

    def test_both_modes_invalid_fails_task_without_row(self):
        receipt = self._dispatch_and_import(
            {"schema_version": 1, "subtitle": None, "summary": None}
        )
        task = self.tasks.get_task(receipt["task"]["task_id"])
        self.assertEqual(task["state"], "failed")
        self.assertEqual(task["error"], "judge_output_invalid")
        self.assertIsNone(self.store.find_ai_artifact(SUB, "quality_report"), "fail-closed 零落行")

    def test_worker_old_mirror_kind_unsupported(self):
        self.seed_transcript()
        self.coordinator.failures = [RuntimeError("unsupported job kind: quality_judge")]
        receipt = self.request()
        self.drain()
        task = self.tasks.get_task(receipt["task"]["task_id"])
        self.assertEqual(task["state"], "failed")
        self.assertEqual(task["error"], "worker_kind_unsupported")

    def test_remote_paused_fails_honestly(self):
        self.seed_transcript()
        self.coordinator.failures = [RemoteTaskPaused("remote task canceled")]
        receipt = self.request()
        self.drain()
        task = self.tasks.get_task(receipt["task"]["task_id"])
        self.assertEqual(task["state"], "failed")
        self.assertEqual(task["error"], "remote_answer_failed")

    def test_usage_recorded_and_month_lens_covers_quality(self):
        receipt = self._dispatch_and_import(
            _judge_report(subtitle_findings=[]), metrics=USAGE,
        )
        task = self.tasks.get_task(receipt["task"]["task_id"])
        self.assertEqual(task["deepseek_tokens"], 90, "消耗记账=绝对值落任务行")
        local_month = time.strftime("%Y-%m", time.localtime(time.time()))
        month_start = time.mktime(time.strptime(f"{local_month}-01", "%Y-%m-%d"))
        totals = self.tasks.usage_totals_since(month_start)
        self.assertGreaterEqual(float(totals["deepseek_tokens"]), 90.0, "消耗透镜 kind 无关自动覆盖")

    def test_artifact_read_via_existing_getter(self):
        self._dispatch_and_import(_judge_report(subtitle_findings=[]))
        artifact = self.app.ai_artifact(SUB, "quality_report")
        self.assertEqual(artifact["content"]["schema_version"], 1)


class DeterministicCountsTests(unittest.TestCase):
    """合同 §② 确定性完整性计数六键（纯函数面——写入端免疫纪律使 DB 恒干净，
    计数器对历史旧行与越界章节的观测价值在此钉住）。"""

    def test_dirty_segments_counts(self):
        counts = _quality_deterministic_counts(_dirty_segments(), [])
        self.assertEqual(counts["segments_total"], 18)
        self.assertEqual(counts["non_monotonic"], 1)
        self.assertEqual(counts["bad_duration"], 1)
        self.assertEqual(counts["empty_text"], 1)

    def test_chapter_counts_and_out_of_range(self):
        chapters = [
            {"title": "开场", "start_ms": 0},
            {"title": "正中", "start_ms": 10_000},
            {"title": "越界", "start_ms": 999_999_999},
            {"title": "负值", "start_ms": -5},
        ]
        counts = _quality_deterministic_counts(_segments(), chapters)
        self.assertEqual(counts["chapters_total"], 4)
        self.assertEqual(counts["chapter_out_of_range"], 2)

    def test_empty_inputs_are_zeroed(self):
        counts = _quality_deterministic_counts([], [])
        self.assertEqual(
            counts,
            {
                "segments_total": 0, "non_monotonic": 0, "bad_duration": 0,
                "empty_text": 0, "chapters_total": 0, "chapter_out_of_range": 0,
            },
        )


class MonthCounterAndFrontendPinTests(_QualityHarness):
    """ai_usage_month 加性键 + 任务抽屉标签静态钉。"""

    def test_ai_usage_month_additive_key(self):
        self.seed_transcript()
        before = self.app.ai_usage_month()
        self.assertEqual(before["quality_checks"], 0)
        self.request()
        after = self.app.ai_usage_month()
        self.assertEqual(after["quality_checks"], 1)
        self.assertEqual(after["schema"], before["schema"], "schema 不变，仅加键")

    def test_tasks_drawer_label_and_grouping_pin(self):
        source = family_text("tasks-drawer")
        self.assertIn('quality_judge: "质量抽检"', source, "TASK_KIND_LABELS 闭集一行")
        grouping = source.split("const LEARNING_PACK_KINDS", 1)[1].split(")", 1)[0]
        self.assertNotIn("quality_judge", grouping, "不参与学习材料组合分组")


class AutoJudgeHookTests(unittest.TestCase):
    """THINK-LADDER-1 自动抽检钩子：总结导入→同内容自动一次。

    幂等（同 hash 已有报告/该讲在途即跳过）、预算门（超日档诚实跳过）、
    绝不炸导入主链（缺材料/缺 key/缺远端=闭集日志零任务）。手动 POST
    入口不变（Preflight/Dispatch 两族用例照旧覆盖）。"""

    def setUp(self) -> None:
        from path_utils import PROJECT_ROOT

        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.output_dir = Path(self.temporary.name)
        self.app = CourseLensApplication(self.output_dir)
        self.app._deepseek_api_key = "sk-synthetic"
        self.app._run_assessment_radar = mock.Mock()
        self.app._rebuild_course_knowledge = mock.Mock(return_value={"state": "skipped"})
        self.app.catalog_repository.upsert_course("course", "概率论")
        self.app.catalog_repository.upsert_lecture(
            "course", {"sub_id": "lecture", "sub_title": "第一讲"}
        )
        for target in (
            mock.patch.object(
                CourseLensApplication, "remote_compute_snapshot",
                lambda self: {"configured": True, "verified": True},
            ),
            mock.patch.object(
                CourseLensApplication, "_ensure_quality_judge_worker", lambda self: None
            ),
        ):
            target.start()
            self.addCleanup(target.stop)

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def _result(self) -> dict:
        return {
            "status": "completed",
            "input_hash": "a" * 64,
            "outputs": {"summary": {"markdown": "## 第一讲\n\n要点甲。", "chapters": []}},
            "metrics": {"deepseek_tokens": 10},
        }

    def _quality_tasks(self) -> list[dict]:
        return [
            task for task in self.app.task_store.list_tasks(limit=200)
            if task.get("kind") == "quality_judge"
        ]

    def test_summary_import_auto_requests_exactly_once(self):
        self.app._import_remote_summary_result("course", "lecture", self._result())
        tasks = self._quality_tasks()
        self.assertEqual(len(tasks), 1, "总结落库后自动抽检恰一任务")
        self.assertEqual(tasks[0]["state"], "queued")
        self.assertEqual(
            tasks[0]["payload"]["summary_pack"]["markdown"], "## 第一讲\n\n要点甲。",
            "自动链与手动链同一请求面（payload 同形状）",
        )
        # 同内容重复导入：在途任务在飞 → 钩子静默跳过，不叠新任务
        self.app._import_remote_summary_result("course", "lecture", self._result())
        self.assertEqual(len(self._quality_tasks()), 1)

    def test_already_judged_content_not_retriggered(self):
        self.app._import_remote_summary_result("course", "lecture", self._result())
        task = self._quality_tasks()[0]
        # 报告落库（同 input_hash upsert 语义）后重放导入：已抽检 → 零新任务
        self.app.learning_store.import_quality_report(
            course_id="course", sub_id="lecture",
            input_hash=str(task["payload"]["input_hash"]),
            model="deepseek-flash",
            report=_judge_report(subtitle_findings=[]),
            metrics=dict(USAGE),
        )
        self.app._import_remote_summary_result("course", "lecture", self._result())
        self.assertEqual(len(self._quality_tasks()), 1)

    def test_budget_cap_skips_honestly(self):
        with mock.patch.object(
            CourseLensApplication, "max_deepseek_tokens_limit", lambda self: 100_000,
        ), mock.patch.object(
            CourseLensApplication, "task_usage_month",
            lambda self: {"deepseek_tokens": 100_000},
        ):
            self.app._import_remote_summary_result("course", "lecture", self._result())
        self.assertEqual(self._quality_tasks(), [], "超日档=闭集跳过零任务")

    def test_budget_sentinel_zero_is_unlimited(self):
        with mock.patch.object(
            CourseLensApplication, "max_deepseek_tokens_limit", lambda self: 0,
        ), mock.patch.object(
            CourseLensApplication, "task_usage_month",
            lambda self: {"deepseek_tokens": 10**9},
        ):
            self.app._import_remote_summary_result("course", "lecture", self._result())
        self.assertEqual(len(self._quality_tasks()), 1, "哨兵 0=不限档照常自动抽检")

    def test_never_raises_import_mainline(self):
        # 缺 key：preflight 闭集拒绝被钩子吞掉，导入主链照常落库
        self.app._deepseek_api_key = ""
        with mock.patch.object(
            CourseLensApplication, "_deepseek_key", lambda self: ""
        ):
            self.app._import_remote_summary_result("course", "lecture", self._result())
        stored = self.app.learning_store.find_ai_artifact("lecture", "timestamp_summary")
        self.assertIsNotNone(stored, "自动抽检绝不挡总结导入")
        self.assertEqual(self._quality_tasks(), [])
        # 直接调钩子：目录外讲=闭集日志零任务零异常
        self.app._auto_quality_judge_after_summary("no-such-lecture")
        self.assertEqual(self._quality_tasks(), [])


class ErrorCodeMapperPins(unittest.TestCase):
    """R3-11：question/quality 映射器共享尾部 `_transient_answer_error_code`
    收敛后的现行为逐键等值 + 已知疣点钉（authorization 宽松首支）。"""

    def _coded(self, code: str, message: str = "") -> Exception:
        exc = Exception(message or code)
        exc.code = code
        return exc

    def test_shared_tail_keyword_equivalence(self):
        from src.application import CourseLensApplication

        cases = {
            "HTTP 429 too many requests": "deepseek_rate_limited",
            "rate limit exceeded": "deepseek_rate_limited",
            "request timed out": "remote_answer_timeout",
            "the operation timed out after 30s": "remote_answer_timeout",
            "github is not ready": "remote_authorization_required",
            "deepseek unavailable": "deepseek_answer_failed",
            "llm exploded": "deepseek_answer_failed",
            "something else happened": "remote_answer_failed",
        }
        for message, expected in cases.items():
            self.assertEqual(
                CourseLensApplication._transient_answer_error_code(message),
                expected, message,
            )

    def test_authorization_word_beats_deepseek_branch(self):
        # 已知疣点现行为钉：任何含 authorization 字样的消息先于 deepseek
        # 分支命中授权码（R3-11 注释在案，闭集码通道成熟后整段退役）。
        from src.application import CourseLensApplication

        self.assertEqual(
            CourseLensApplication._transient_answer_error_code(
                "deepseek authorization header malformed"
            ),
            "remote_authorization_required",
        )

    def test_explicit_codes_win_over_keywords(self):
        from src.application import CourseLensApplication

        self.assertEqual(
            CourseLensApplication._question_error_code(
                self._coded("authorization_missing")
            ),
            "cloud_setup_required",
        )
        self.assertEqual(
            CourseLensApplication._quality_error_code(
                self._coded("judge_output_invalid")
            ),
            "judge_output_invalid",
        )
        self.assertEqual(
            CourseLensApplication._quality_error_code(
                self._coded("authorization_missing")
            ),
            "cloud_setup_required",
        )
        # 显式码不在闭集 → 落回关键词尾部（429 仍认得）
        self.assertEqual(
            CourseLensApplication._quality_error_code(
                self._coded("not_in_set", "HTTP 429 slow down")
            ),
            "deepseek_rate_limited",
        )


    def test_api_js_deep_answer_family_tracks_question_error_codes(self):
        # R3-10 词汇表等集钉：api.js ERROR_MESSAGES 的深度回答族键集必须覆盖
        # _question_error_code 可返回的全闭集（共享尾部派生+显式码），防后续
        # 单侧加码漂移让学生看到裸 code。
        import re

        api = (
            Path(__file__).resolve().parents[1] / "frontend" / "modules" / "api.js"
        ).read_text(encoding="utf-8")
        block = api[api.index("const ERROR_MESSAGES = Object.freeze({"):]
        block = block[:block.index("\n});")]
        keys = set(re.findall(r"([a-z0-9_]+): \"", block))
        from src.application import CourseLensApplication

        # 共享尾部闭集=按探测消息从实现派生（回退码含在内），再加显式授权码。
        shared = {
            CourseLensApplication._transient_answer_error_code(probe)
            for probe in ("429", "timed out", "authorization", "deepseek", "nothing")
        }
        question_set = shared | {"cloud_setup_required"}
        self.assertEqual(
            len(shared), 5, "共享尾部探测恰好覆盖五个码（实现漂移时先改这里）",
        )
        missing = sorted(question_set - keys)
        self.assertEqual(missing, [], f"api.js ERROR_MESSAGES 缺深度回答族键: {missing}")


class TermBoundaryPayloadTests(_QualityHarness):
    """THINK-LADDER-2 设计 B：quality_judge 载荷 ``term_boundary`` 组装/信封
    转发/裁决导入（user-adjudication-supreme 三路走完整队列漏斗）。"""

    def seed_boundary(self, *, signal: int = 0) -> list[dict]:
        """种一个真实候选词对（sink 挂账），可选偏差信号计数。"""
        sink_course_examples(self.app.output_dir, COURSE, [
            {"start_ms": 0, "end_ms": 1000,
             "before": "这个费米能及很重要", "after": "这个费米能级很重要，"},
        ], sub_id=SUB)
        if signal:
            path = memory_path(self.app.output_dir, COURSE)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["signals"] = [{
                "wrong": "费米能及", "right": "费米能级", "count": signal,
                "first_seen": 1.0, "last_seen": 2.0, "sources": {},
            }]
            path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return [{"wrong": "费米能及", "right": "费米能级", "signal_count": signal}]

    def test_payload_carries_below_threshold_pairs_with_signal(self):
        self.seed_transcript()
        self.seed_summary()
        expected = self.seed_boundary(signal=2)
        self.request()
        self.assertEqual(self.quality_tasks()[0]["payload"]["term_boundary"], expected)

    def test_threshold_pairs_omit_key(self):
        self.seed_transcript()
        self.seed_summary()
        self.seed_boundary(signal=3)  # signal≥3：合同筛选（正常路径已确定性晋升）
        self.request()
        self.assertNotIn("term_boundary", self.quality_tasks()[0]["payload"])

    def test_empty_candidates_omit_key(self):
        self.seed_transcript()
        self.seed_summary()
        self.request()
        self.assertNotIn("term_boundary", self.quality_tasks()[0]["payload"])

    def test_job_envelope_forwards_pairs(self):
        self.seed_transcript()
        self.seed_summary()
        expected = self.seed_boundary(signal=2)
        self.request()
        self.coordinator.outputs = {"quality_judge": _judge_report(subtitle_findings=[])}
        self.coordinator.metrics = dict(USAGE)
        self.drain()
        self.assertEqual(self.coordinator.jobs[0]["payload"]["term_boundary"], expected)

    def test_rulings_import_lands_judge_confirmed_and_revocable(self):
        self.seed_transcript()
        self.seed_summary()
        self.seed_boundary(signal=2)
        self.request()
        self.coordinator.outputs = {
            "quality_judge": _judge_report(subtitle_findings=[]),
            "term_boundary_rulings": {"rulings": [{
                "wrong": "费米能及", "right": "费米能级",
                "ruling": "valid", "reason_code": "glossary_match",
            }], "malformed_pairs": 0, "unruled_pairs": 0},
        }
        self.coordinator.metrics = dict(USAGE)
        self.drain()
        task = [t for t in self.quality_tasks()][0]
        self.assertEqual(task["state"], "completed", "裁决导入绝不拖垮质检任务本体")
        ledger = json.loads(
            memory_path(self.app.output_dir, COURSE).read_text(encoding="utf-8")
        )["confirmed_mappings"]
        self.assertTrue(ledger[0]["judge_confirmed"])
        self.assertEqual(ledger[0]["judge_signal_count"], 2)
        # 撤销面：judge 件与自动晋升件同规，dismiss 可撤销
        revoked = dismiss_term_mapping(
            self.app.output_dir, COURSE, wrong="费米能及", right="费米能级"
        )
        self.assertEqual(revoked["dismissed"], True)

    def test_missing_rulings_output_is_silent_no_op(self):
        self.seed_transcript()
        self.seed_summary()
        self.seed_boundary(signal=2)
        self.request()
        self.coordinator.outputs = {"quality_judge": _judge_report(subtitle_findings=[])}
        self.coordinator.metrics = dict(USAGE)
        self.drain()
        document = json.loads(
            memory_path(self.app.output_dir, COURSE).read_text(encoding="utf-8")
        )
        self.assertNotIn("confirmed_mappings", document, "旧 worker 无裁决输出=零台账写入")
        self.assertNotIn("judge_annotations", document)


class CoRunJudgeRequestTests(unittest.TestCase):
    """N2-2a judge 共位（客户端腿）：summary build_job 的 ``judge_request``
    组装+旧客户端兼容性自证（除新键外载荷逐键字节恒等）+共位报告导入身份
    与独立派发同域（自动抽检钩子幂等跳过，恰省一次独立派发）+缺席兜底。"""

    def setUp(self) -> None:
        from path_utils import PROJECT_ROOT

        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.output_dir = Path(self.temporary.name)
        self.app = CourseLensApplication(self.output_dir)
        self.app._deepseek_api_key = "sk-synthetic"
        self.app._run_assessment_radar = mock.Mock()
        self.app._rebuild_course_knowledge = mock.Mock(return_value={"state": "skipped"})
        self.app.catalog_repository.upsert_course("course", "概率论")
        self.app.catalog_repository.upsert_lecture(
            "course", {"sub_id": "lecture", "sub_title": "第一讲"}
        )
        self.app.learning_store.replace_transcript_segments(
            "lecture",
            source_path="synthetic.srt", source_mtime_ns=111, source_size=22,
            segments=_segments(),
        )
        # 预热读链：首读会触发 vtt 轨文件自愈回写（catalog vtt_path+store 源
        # 重挂），预热让后续每次 build_job 走同一条稳定文件链——兼容性自证
        # 比较的是「judge_request 键本身」的差，不混入首读自愈的一次性漂移。
        self.app.subtitle_segments("lecture")
        self.jobs: list[dict] = []
        self.captured: dict = {}
        self.app._active_summary_task_id = "task-cerun-1"
        for target in (
            mock.patch.object(
                CourseLensApplication, "_ensure_quality_judge_worker", lambda self: None
            ),
            mock.patch.object(
                CourseLensApplication, "_leased_remote_coordinator", self._fake_lease
            ),
            mock.patch.object(CourseLensApplication, "_cloud_run_slot", self._fake_slot),
            mock.patch.object(
                CourseLensApplication, "remote_compute_snapshot",
                lambda self: {"configured": True, "verified": True},
            ),
        ):
            target.start()
            self.addCleanup(target.stop)

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    @contextlib.contextmanager
    def _fake_slot(self, _task_id, *, cancel_requested=None, on_wait=None):
        yield

    @contextlib.contextmanager
    def _fake_lease(self, _task_id, *, workflow=None):
        self.captured["workflow"] = workflow

        class _Lease:
            def execute(self_inner, *, task_id, build_job, import_result,
                        cancel_requested=None, progress=None):
                self.jobs.append(build_job("synthetic-public-key"))
                if self.captured.get("result") is not None:
                    import_result(self.captured["result"])
                return self.captured.get("result") or {"input_hash": ""}

        yield _Lease()

    def run_remote(self, result: dict | None = None) -> dict:
        self.captured["result"] = result
        return self.app._run_summary_remote(
            {"course_id": "course", "sub_id": "lecture", "title": "第一讲",
             "include_ppt": False},
            on_progress=lambda *_: None,
        )

    def _quality_tasks(self) -> list[dict]:
        return [
            task for task in self.app.task_store.list_tasks(limit=200)
            if task.get("kind") == "quality_judge"
        ]

    def test_judge_request_frozen_shape(self):
        self.run_remote()
        self.assertEqual(len(self.jobs), 1)
        stored = self.app.learning_store.get_transcript_segments("lecture")
        self.assertEqual(self.jobs[0]["payload"]["judge_request"], {
            "segment_indices": _quality_sample_indices(len(stored)),
            "transcript_digest": _quality_transcript_digest(stored),
        })
        # N1-ROUTING：无 PPT 总结=媒体面缺席 → llm.yml 快路径
        self.assertEqual(self.captured["workflow"], "llm.yml")

    def test_old_client_shape_byte_identical_without_key(self):
        with mock.patch.object(
            CourseLensApplication, "_judge_request_extra", lambda self, _s, **_: {}
        ):
            self.run_remote()
        baseline_payload = self.jobs[0]["payload"]
        self.assertNotIn("judge_request", baseline_payload)
        self.run_remote()
        stripped = {
            key: value for key, value in self.jobs[1]["payload"].items()
            if key != "judge_request"
        }
        self.assertEqual(
            stripped, baseline_payload,
            "兼容性自证：除 judge_request 新键外，载荷逐键字节恒等",
        )

    def test_co_run_report_imports_and_skips_standalone_dispatch(self):
        result = {
            "status": "completed", "input_hash": "a" * 64,
            "outputs": {
                "summary": {"markdown": "## 第一讲\n\n要点甲。", "chapters": []},
                "quality_judge": _judge_report(subtitle_findings=[]),
            },
            "metrics": {"deepseek_tokens": 10},
        }
        self.run_remote(result)
        artifact = self.app.learning_store.find_ai_artifact("lecture", "quality_report")
        self.assertIsNotNone(artifact, "共位报告以派发时冻结身份落库")
        stored = self.app.learning_store.get_transcript_segments("lecture")
        self.assertEqual(
            artifact["content"]["generation"]["input_hash"],
            _quality_input_hash(
                "lecture",
                _quality_sample_indices(len(stored)),
                _quality_transcript_digest(stored),
                "a" * 64,
            ),
            "与 request_quality_judge/自动抽检钩子同一 input_hash 身份空间",
        )
        self.assertEqual(
            self._quality_tasks(), [],
            "共位报告已落库：自动抽检钩子同 hash 幂等跳过（省一次独立派发）",
        )

    def test_missing_co_run_report_falls_back_to_standalone(self):
        # worker 零材料/共位失败：无 quality_judge 输出 → 导入漏斗照旧独立派发
        result = {
            "status": "completed", "input_hash": "a" * 64,
            "outputs": {"summary": {"markdown": "## 第一讲\n\n要点甲。", "chapters": []}},
            "metrics": {"deepseek_tokens": 10},
        }
        self.run_remote(result)
        self.assertIsNone(self.app.learning_store.find_ai_artifact("lecture", "quality_report"))
        tasks = self._quality_tasks()
        self.assertEqual(len(tasks), 1, "兜底独立派发恰一任务")

    def test_invalid_co_run_report_never_blocks_summary_import(self):
        result = {
            "status": "completed", "input_hash": "a" * 64,
            "outputs": {
                "summary": {"markdown": "## 第一讲\n\n要点甲。", "chapters": []},
                "quality_judge": {"schema_version": 1, "subtitle": None, "summary": None},
            },
            "metrics": {"deepseek_tokens": 10},
        }
        self.run_remote(result)
        stored = self.app.learning_store.find_ai_artifact("lecture", "timestamp_summary")
        self.assertIsNotNone(stored, "共位报告无效（judge_output_invalid）绝不挡总结导入")
        self.assertEqual(
            str((stored.get("content") or {}).get("overview") or "").strip(),
            "## 第一讲\n\n要点甲。",
        )

    def test_import_without_capture_walks_standalone_funnel(self):
        # 启动对账恢复件：无派发时捕获（judge_request=None）→ 不落共位报告，
        # 走独立派发兜底（诚实重检）。
        self.app._import_remote_summary_result(
            "course", "lecture",
            {
                "status": "completed", "input_hash": "a" * 64,
                "outputs": {
                    "summary": {"markdown": "## 第一讲\n\n要点甲。", "chapters": []},
                    "quality_judge": _judge_report(subtitle_findings=[]),
                },
                "metrics": {"deepseek_tokens": 10},
            },
        )
        self.assertIsNone(
            self.app.learning_store.find_ai_artifact("lecture", "quality_report"),
            "无捕获身份不落共位报告",
        )
        self.assertEqual(len(self._quality_tasks()), 1)


if __name__ == "__main__":
    unittest.main()
