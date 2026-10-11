"""P10-CONTRACT-1 课园词汇候选流水行为钉（PKG-A 后端）。

合同冻结面唯一正源=archive/external-artifacts/top-model-results-20260930/
product-evalremain1-result-20261002.md 的 P10-CONTRACT-1 节。本文件钉住：
1. 冻结件 1：``sink_course_examples`` 的 mapping_stats 挂账——同批替换块
   计数、跨讲 subs 归并、帽 100 按 updated_at 最旧淘汰、sub_id 缺省
   byte-identical；
2. 冻结件 2：``term_candidates`` 归并（stats 主源 ∪ 推导补遗）排序帽、
   终态排除、signal_count 同对记账；``confirmed_term_list`` 确认序；
3. 冻结件 3：confirm/dismiss 资格门（``TermCandidateUnknown`` 闭集拒绝）+
   幂等终态 + 帽 200 + 写失败如实 False；
4. 冻结件 4：payload ``glossary`` 只在有确认词时出现；course_review 响应
   ``term_candidates`` 键形状（冻结行六字段）；
5. 旧文档兼容：三加性键缺席全链零行为差。
H4 讲次桶接线已由 1a14e00（STUDY-EVENT-1）先行落地，钉在
tests/test_telemetry_h64.py，此处不重复。HTTP 闭集路由与错误码翻译钉在
tests/test_course_review_api.py。
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
    course_memory_count,
    memory_path,
    sink_course_examples,
)
from src.runtime.course_memory_feedback import (
    TermCandidateUnknown,
    confirm_term_mapping,
    confirmed_memory_terms,
    confirmed_term_list,
    dismiss_term_mapping,
    import_boundary_rulings,
    judge_annotation_rows,
    judge_boundary_pairs,
    judge_confirmed_term_rows,
    term_candidates,
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


def _write_doc(output_dir: Path, course_id: str, document: dict) -> Path:
    path = memory_path(output_dir, course_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def _read_doc(output_dir: Path, course_id: str) -> dict:
    return json.loads(memory_path(output_dir, course_id).read_text(encoding="utf-8"))


def _stats_entry(wrong: str, right: str, subs: dict[str, int], updated_at: float) -> dict:
    return {
        "wrong": wrong, "right": right, "subs": subs,
        "first_seen": 1.0, "updated_at": updated_at,
    }


class TermSinkLedgerTests(unittest.TestCase):
    """冻结件 1：字幕导入 sink 的跨讲映射挂账。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.output_dir = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_sink_books_mapping_stats_in_same_write(self) -> None:
        added = sink_course_examples(self.output_dir, "course-1", [
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
            _audit("费米能及在物理里很常见", "费米能级在物理里很常见"),  # 同对第二块
        ], sub_id="sub-1")
        self.assertEqual(added, 2)
        document = _read_doc(self.output_dir, "course-1")
        self.assertEqual(len(document["examples"]), 2)  # 同一次文档写
        stats = document["mapping_stats"]
        self.assertEqual(len(stats), 1)
        entry = stats[0]
        self.assertEqual(set(entry), {"wrong", "right", "subs", "first_seen", "updated_at"})
        self.assertEqual(entry["wrong"], "费米能及")
        self.assertEqual(entry["right"], "费米能级")
        self.assertEqual(entry["subs"], {"sub-1": 2})  # 按替换块计数

    def test_sink_merges_subs_across_lectures(self) -> None:
        sink_course_examples(self.output_dir, "course-1", [
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
        ], sub_id="sub-1")
        # 挂账只随「本批新增示例」发生（合同冻结件 1）：第二讲带来新示例、
        # 同一对再被推导，讲次账归并、首见保留。
        sink_course_examples(self.output_dir, "course-1", [
            _audit("费米能及在物理里很常见", "费米能级在物理里很常见"),
        ], sub_id="sub-2")
        entry = _read_doc(self.output_dir, "course-1")["mapping_stats"][0]
        self.assertEqual(entry["subs"], {"sub-1": 1, "sub-2": 1})
        self.assertLessEqual(entry["first_seen"], entry["updated_at"])
        # 同一结果原样回放：零新增=零挂账，讲次账不被重复计数
        sink_course_examples(self.output_dir, "course-1", [
            _audit("费米能及在物理里很常见", "费米能级在物理里很常见"),
        ], sub_id="sub-2")
        entry = _read_doc(self.output_dir, "course-1")["mapping_stats"][0]
        self.assertEqual(entry["subs"], {"sub-1": 1, "sub-2": 1})

    def test_sink_without_sub_id_is_byte_identical_legacy(self) -> None:
        sink_course_examples(self.output_dir, "course-1", [
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
        ])
        document = _read_doc(self.output_dir, "course-1")
        self.assertEqual(
            sorted(document),
            ["course_id", "examples", "updated_at", "version"],
        )

    def test_sink_without_pairs_or_additions_writes_no_stats(self) -> None:
        # 零新增：整条短路，无文件无键
        self.assertEqual(sink_course_examples(
            self.output_dir, "course-1", [_audit("只是加个标点", "只是加个标点。")],
            sub_id="sub-1",
        ), 0)
        # 新增示例但替换块出词长窗：examples 落账、mapping_stats 缺席
        added = sink_course_examples(self.output_dir, "course-1", [
            _audit("这句话没有任何一点一点的改动空间", "这句话完全被换掉了呀"),
        ], sub_id="sub-1")
        self.assertEqual(added, 1)
        self.assertNotIn("mapping_stats", _read_doc(self.output_dir, "course-1"))

    def test_sink_stats_cap_evicts_oldest_by_updated_at(self) -> None:
        entries = [
            _stats_entry(f"词{i:03d}甲", f"词{i:03d}乙", {"sub-0": 1}, 1000.0 + i)
            for i in range(100)
        ]
        _write_doc(self.output_dir, "course-1", {
            "version": 1, "course_id": "course-1",
            "examples": [], "mapping_stats": entries,
        })
        sink_course_examples(self.output_dir, "course-1", [
            _audit("新词甲很重要", "新词乙很重要"),
        ], sub_id="sub-new")
        stats = _read_doc(self.output_dir, "course-1")["mapping_stats"]
        self.assertEqual(len(stats), 100)  # 帽 100
        keys = {f"{row['wrong']}\u0000{row['right']}" for row in stats}
        self.assertIn("新词甲\u0000新词乙", keys)
        self.assertNotIn("词000甲\u0000词000乙", keys)  # updated_at 最旧者出

    def test_sink_drops_corrupt_stats_rows(self) -> None:
        _write_doc(self.output_dir, "course-1", {
            "version": 1, "course_id": "course-1", "examples": [],
            "mapping_stats": [
                "garbage",
                {"wrong": "", "right": "空词对", "subs": {}},
                _stats_entry("旧词甲", "旧词乙", {"sub-0": 2}, 5.0),
            ],
        })
        sink_course_examples(self.output_dir, "course-1", [
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
        ], sub_id="sub-1")
        stats = _read_doc(self.output_dir, "course-1")["mapping_stats"]
        self.assertEqual(
            [(row["wrong"], row["right"], row["subs"]) for row in stats],
            [("费米能及", "费米能级", {"sub-1": 1}), ("旧词甲", "旧词乙", {"sub-0": 2})],
        )


class TermCandidateReadsTests(unittest.TestCase):
    """冻结件 2：候选清单归并/排序/帽与确认序读。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.output_dir = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_candidates_merge_stats_and_derivation(self) -> None:
        _write_doc(self.output_dir, "course-1", {
            "version": 1, "course_id": "course-1",
            # 旧文档形态：examples 有推导源，stats 只有一行（主源）
            "examples": [{
                "input": [{"id": "e0", "text": "这个费米能及很重要"}],
                "ops": [{"id": "e0", "old": "这个费米能及很重要", "new": "这个费米能级很重要，"}],
            }],
            "mapping_stats": [
                _stats_entry("直播生", "直博生", {"sub-1": 2, "sub-2": 1}, 20.0),
            ],
        })
        rows = term_candidates(self.output_dir, "course-1")
        self.assertEqual(
            [(row["wrong"], row["right"]) for row in rows],
            [("直播生", "直博生"), ("费米能及", "费米能级")],  # lecture_count 降序
        )
        self.assertEqual(rows[0]["lecture_count"], 2)
        self.assertEqual(rows[0]["total_count"], 3)
        self.assertEqual(rows[0]["signal_count"], 0)
        self.assertEqual(rows[0]["updated_at"], 20.0)
        # 推导补遗行：无账目时间与讲次，字段形状仍冻结齐
        self.assertEqual(
            set(rows[1]), {"wrong", "right", "lecture_count", "total_count", "signal_count", "updated_at"}
        )
        self.assertEqual(rows[1]["lecture_count"], 0)
        self.assertEqual(rows[1]["updated_at"], 0.0)

    def test_candidates_sort_and_limit(self) -> None:
        _write_doc(self.output_dir, "course-1", {
            "version": 1, "course_id": "course-1", "examples": [],
            "mapping_stats": [
                _stats_entry("词一甲", "词一乙", {"s1": 1}, 30.0),
                _stats_entry("词二甲", "词二乙", {"s1": 1, "s2": 1, "s3": 1}, 10.0),
                _stats_entry("词三甲", "词三乙", {"s1": 1, "s2": 1}, 40.0),
            ],
        })
        rows = term_candidates(self.output_dir, "course-1")
        self.assertEqual([row["lecture_count"] for row in rows], [3, 2, 1])
        self.assertEqual(
            [row["wrong"] for row in term_candidates(self.output_dir, "course-1", limit=2)],
            ["词二甲", "词三甲"],
        )

    def test_candidates_report_signals_and_exclude_terminal(self) -> None:
        _write_doc(self.output_dir, "course-1", {
            "version": 1, "course_id": "course-1", "examples": [],
            "mapping_stats": [
                _stats_entry("词一甲", "词一乙", {"s1": 1}, 30.0),
                _stats_entry("词二甲", "词二乙", {"s1": 1}, 20.0),
            ],
            "signals": [{
                "wrong": "词二甲", "right": "词二乙", "count": 7,
                "first_seen": 1.0, "last_seen": 2.0, "sources": {},
            }],
        })
        rows = term_candidates(self.output_dir, "course-1")
        # signal_count 是次级排序键：同讲次数下有信号者排前
        self.assertEqual(
            [(row["wrong"], row["signal_count"]) for row in rows],
            [("词二甲", 7), ("词一甲", 0)],
        )
        confirm_term_mapping(self.output_dir, "course-1", wrong="词二甲", right="词二乙")
        dismiss_term_mapping(self.output_dir, "course-1", wrong="词一甲", right="词一乙")
        self.assertEqual(term_candidates(self.output_dir, "course-1"), [])  # 终态出列

    def test_candidates_fail_closed(self) -> None:
        self.assertEqual(term_candidates(self.output_dir, "missing-course"), [])
        path = memory_path(self.output_dir, "course-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt", encoding="utf-8")
        self.assertEqual(term_candidates(self.output_dir, "course-1"), [])
        self.assertEqual(term_candidates(self.output_dir, "course-1", limit=0), [])

    def test_confirmed_term_list_keeps_confirmation_order(self) -> None:
        _write_doc(self.output_dir, "course-1", {
            "version": 1, "course_id": "course-1", "examples": [],
            "confirmed_mappings": [
                {"wrong": "后词甲", "right": "后词乙", "confirmed_at": 200.0},
                {"wrong": "先词甲", "right": "先词乙", "confirmed_at": 100.0},
                "garbage",
            ],
        })
        self.assertEqual(
            confirmed_term_list(self.output_dir, "course-1"),
            (("先词甲", "先词乙"), ("后词甲", "后词乙")),
        )
        self.assertEqual(confirmed_term_list(self.output_dir, "missing"), ())

    def test_confirmed_memory_terms_dedup_and_cap(self) -> None:
        rows = [
            {"wrong": f"词{i:03d}甲", "right": f"词{i:03d}乙", "confirmed_at": 1.0 + i}
            for i in range(201)
        ]
        rows.append({"wrong": "词000丙", "right": "词000乙", "confirmed_at": 500.0})
        _write_doc(self.output_dir, "course-1", {
            "version": 1, "course_id": "course-1", "examples": [],
            "confirmed_mappings": rows,
        })
        terms = confirmed_memory_terms(self.output_dir, "course-1")
        self.assertEqual(len(terms), 200)  # 帽 200
        self.assertEqual(len(set(terms)), len(terms))  # 去重
        self.assertEqual(terms[0], "词000乙")  # 确认序


class TermActionTests(unittest.TestCase):
    """冻结件 3：资格门/幂等终态/帽/写失败。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.output_dir = Path(self.temporary.name)
        sink_course_examples(self.output_dir, "course-1", [
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
        ], sub_id="sub-1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_confirm_books_ledger_and_returns_shape(self) -> None:
        result = confirm_term_mapping(
            self.output_dir, "course-1", wrong="费米能及", right="费米能级"
        )
        self.assertEqual(result, {"confirmed": True, "total_confirmed": 1})
        ledger = _read_doc(self.output_dir, "course-1")["confirmed_mappings"]
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["wrong"], "费米能及")
        self.assertEqual(ledger[0]["right"], "费米能级")
        self.assertIn("confirmed_at", ledger[0])

    def test_gate_rejects_pairs_outside_derived_candidates(self) -> None:
        with self.assertRaises(TermCandidateUnknown):
            confirm_term_mapping(
                self.output_dir, "course-1", wrong="不存在词", right="也没有词"
            )
        with self.assertRaises(TermCandidateUnknown):
            dismiss_term_mapping(self.output_dir, "course-1", wrong="", right="费米能级")
        document = _read_doc(self.output_dir, "course-1")
        self.assertNotIn("confirmed_mappings", document)
        self.assertNotIn("dismissed_mappings", document)

    def test_confirm_and_dismiss_are_idempotent_terminals(self) -> None:
        first = confirm_term_mapping(
            self.output_dir, "course-1", wrong="费米能及", right="费米能级"
        )
        self.assertEqual(first, {"confirmed": True, "total_confirmed": 1})
        repeat = confirm_term_mapping(
            self.output_dir, "course-1", wrong="费米能及", right="费米能级"
        )
        self.assertEqual(repeat, {"confirmed": False, "total_confirmed": 1})  # 幂等 no-op
        # 已确认的终态对再忽略=no-op 成功，两边台账语义不被改写
        cross = dismiss_term_mapping(
            self.output_dir, "course-1", wrong="费米能及", right="费米能级"
        )
        self.assertEqual(cross, {"dismissed": False, "total_dismissed": 0})
        document = _read_doc(self.output_dir, "course-1")
        self.assertNotIn("dismissed_mappings", document)

    def test_dismiss_terminal_removes_candidate(self) -> None:
        result = dismiss_term_mapping(
            self.output_dir, "course-1", wrong="费米能及", right="费米能级"
        )
        self.assertEqual(result, {"dismissed": True, "total_dismissed": 1})
        self.assertEqual(term_candidates(self.output_dir, "course-1"), [])
        self.assertEqual(confirmed_memory_terms(self.output_dir, "course-1"), ())

    def test_action_reports_false_on_write_failure(self) -> None:
        import src.runtime.course_memory_feedback as feedback

        original = feedback._write_document
        feedback._write_document = lambda path, document: False
        try:
            result = confirm_term_mapping(
                self.output_dir, "course-1", wrong="费米能及", right="费米能级"
            )
        finally:
            feedback._write_document = original
        self.assertEqual(result, {"confirmed": False, "total_confirmed": 1})  # 如实未生效
        self.assertNotIn("confirmed_mappings", _read_doc(self.output_dir, "course-1"))

    def test_ledger_cap_keeps_latest_200(self) -> None:
        rows = [
            {"wrong": f"词{i:03d}甲", "right": f"词{i:03d}乙", "confirmed_at": 1.0 + i}
            for i in range(200)
        ]
        _write_doc(self.output_dir, "course-1", {
            "version": 1, "course_id": "course-1", "examples": [],
            "mapping_stats": [_stats_entry("新词甲", "新词乙", {"s1": 1}, 9.0)],
            "confirmed_mappings": rows,
        })
        result = confirm_term_mapping(
            self.output_dir, "course-1", wrong="新词甲", right="新词乙"
        )
        self.assertEqual(result, {"confirmed": True, "total_confirmed": 200})
        ledger = _read_doc(self.output_dir, "course-1")["confirmed_mappings"]
        self.assertEqual(len(ledger), 200)  # 帽 200
        self.assertEqual(ledger[0]["wrong"], "词001甲")  # 最早的先出
        self.assertEqual(ledger[-1]["wrong"], "新词甲")  # 时间序 append

    def test_action_on_corrupt_memory_rejects_closed(self) -> None:
        path = memory_path(self.output_dir, "course-1")
        path.write_text("{corrupt", encoding="utf-8")
        with self.assertRaises(TermCandidateUnknown):
            confirm_term_mapping(
                self.output_dir, "course-1", wrong="费米能及", right="费米能级"
            )


class TermWiringTests(unittest.TestCase):
    """冻结件 4 + §②：应用层接线（导入漏斗/payload/复习读面）。"""

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
        captured: list[dict] = []

        class _Lease:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args: object):
                return False

            def execute(self_inner, *, task_id, build_job, import_result, cancel_requested, progress, **_: object):
                captured.append(build_job("test-public-key"))

        @contextmanager
        def fake_slot(task_id, **_: object):
            yield

        lease_workflows: list[str | None] = []

        @contextmanager
        def fake_lease(task_id: str, *, workflow: str | None = None):
            lease_workflows.append(workflow)
            yield _Lease()

        self.app._cloud_run_slot = fake_slot
        self.app._leased_remote_coordinator = fake_lease
        self.app._generate_subtitle_remote(
            "course", "lecture", task_id="task-test-1", proofread=False
        )
        self.assertEqual(len(captured), 1)
        # N1-ROUTING：字幕载荷带 media 媒体腿 → 派发面维持 process.yml。
        self.assertEqual(lease_workflows, ["process.yml"])
        return captured[0]

    def test_import_funnel_books_mapping_stats_with_sub_id(self) -> None:
        audit = [
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
            _audit("这里只是加个标点", "这里只是加个标点。"),  # 仅标点：不沉淀
        ]
        self.app._import_remote_subtitle("course", "lecture", _subtitle_result(audit))
        document = _read_doc(Path(self.app.output_dir), "course")
        self.assertEqual(document["mapping_stats"][0]["subs"], {"lecture": 1})
        self.assertEqual(course_memory_count(self.app.output_dir, "course"), 1)
        # 回放：示例幂等零新增，讲次账不重复计数
        self.app._import_remote_subtitle("course", "lecture", _subtitle_result(audit))
        document = _read_doc(Path(self.app.output_dir), "course")
        self.assertEqual(document["mapping_stats"][0]["subs"], {"lecture": 1})

    def test_dispatch_gains_glossary_only_with_confirmed_terms(self) -> None:
        sink_course_examples(Path(self.app.output_dir), "course", [
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
        ], sub_id="lecture")
        self.assertNotIn("glossary", self._dispatch_job()["payload"])  # 未确认=旧行为
        confirm_term_mapping(
            self.app.output_dir, "course", wrong="费米能及", right="费米能级"
        )
        self.assertEqual(
            self._dispatch_job()["payload"]["glossary"], ["费米能级"]
        )

    def test_dispatch_survives_corrupt_memory_without_glossary(self) -> None:
        path = memory_path(self.app.output_dir, "course")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{corrupt", encoding="utf-8")
        payload = self._dispatch_job()["payload"]
        self.assertNotIn("glossary", payload)
        self.assertNotIn("examples", payload)

    def test_course_review_term_candidates_shape(self) -> None:
        review = self.app.course_review("course")
        # THINK-LADDER-1/2 加性键：auto_confirmed_rows/judge_confirmed_rows
        # 空表也是形状冻结的一部分
        self.assertEqual(
            review["term_candidates"],
            {
                "rows": [], "confirmed_count": 0,
                "auto_confirmed_rows": [], "judge_confirmed_rows": [],
            },
        )
        sink_course_examples(Path(self.app.output_dir), "course", [
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
        ], sub_id="lecture")
        review = self.app.course_review("course")
        rows = review["term_candidates"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            set(rows[0]),
            {"wrong", "right", "lecture_count", "total_count", "signal_count", "updated_at"},
        )
        self.assertEqual(rows[0]["lecture_count"], 1)
        confirm_term_mapping(
            self.app.output_dir, "course", wrong="费米能及", right="费米能级"
        )
        review = self.app.course_review("course")
        self.assertEqual(review["term_candidates"]["rows"], [])
        self.assertEqual(review["term_candidates"]["confirmed_count"], 1)

    def test_legacy_document_keeps_full_chain_unchanged(self) -> None:
        # 旧文档（无三加性键）：推导补遗出候选、注入/计数面零行为差
        sink_course_examples(Path(self.app.output_dir), "course", [
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
        ])  # 旧式调用（无 sub_id）
        document = _read_doc(Path(self.app.output_dir), "course")
        self.assertNotIn("mapping_stats", document)
        review = self.app.course_review("course")
        self.assertEqual(review["term_candidates"]["confirmed_count"], 0)
        self.assertEqual(len(review["term_candidates"]["rows"]), 1)  # 推导补遗
        self.assertNotIn("glossary", self._dispatch_job()["payload"])


class JudgeBoundaryPairsTests(unittest.TestCase):
    """THINK-LADDER-2 设计 B：term_boundary 组装面（signal<3 筛选/帽/重问过滤）。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.output_dir = Path(self.temporary.name)
        sink_course_examples(self.output_dir, "course-1", [
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
        ], sub_id="sub-1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_pairs_carry_signal_count_and_shape(self) -> None:
        pairs = judge_boundary_pairs(self.output_dir, "course-1")
        self.assertEqual(pairs, [{"wrong": "费米能及", "right": "费米能级", "signal_count": 0}])

    def test_signal_threshold_filter_and_terminal_exclusion(self) -> None:
        document = _read_doc(self.output_dir, "course-1")
        document["signals"] = [{
            "wrong": "费米能及", "right": "费米能级", "count": 3,
            "first_seen": 1.0, "last_seen": 2.0, "sources": {},
        }]
        _write_doc(self.output_dir, "course-1", document)
        # signal≥3 合同筛选（正常路径该对已被确定性晋升出候选，此处双保险）
        self.assertEqual(judge_boundary_pairs(self.output_dir, "course-1"), [])
        # 人工确认终态对天然出列（候选视图不含终态）
        confirm_term_mapping(self.output_dir, "course-1", wrong="费米能及", right="费米能级")
        self.assertEqual(judge_boundary_pairs(self.output_dir, "course-1"), [])

    def test_already_judged_pairs_not_reshown(self) -> None:
        result = import_boundary_rulings(
            self.output_dir, "course-1",
            pairs=[{"wrong": "费米能及", "right": "费米能级", "signal_count": 1}],
            rulings={"rulings": [{
                "wrong": "费米能及", "right": "费米能级",
                "ruling": "invalid", "reason_code": "glossary_conflict",
            }]},
        )
        self.assertEqual(result, {"confirmed": 0, "annotated": 1, "no_op": 0, "skipped": 0})
        # invalid=标注折叠：对不重问、仍出候选（视图层带注记覆盖）、绝不写 dismissed
        self.assertEqual(judge_boundary_pairs(self.output_dir, "course-1"), [])
        rows = term_candidates(self.output_dir, "course-1")
        self.assertEqual([(row["wrong"], row["right"]) for row in rows],
                         [("费米能及", "费米能级")], "折叠注记不出列，人工动作不受限")
        self.assertNotIn("dismissed_mappings", _read_doc(self.output_dir, "course-1"))

    def test_cap_ten_pairs(self) -> None:
        entries = [
            _stats_entry(f"词{i:02d}甲", f"词{i:02d}乙", {"s1": 1}, 1.0 + i)
            for i in range(12)
        ]
        document = _read_doc(self.output_dir, "course-1")
        document["mapping_stats"] = entries
        _write_doc(self.output_dir, "course-1", document)
        pairs = judge_boundary_pairs(self.output_dir, "course-1")
        self.assertEqual(len(pairs), 10)
        self.assertEqual(len(judge_boundary_pairs(self.output_dir, "course-1", limit=3)), 3)

    def test_fail_closed_on_missing_or_corrupt_memory(self) -> None:
        self.assertEqual(judge_boundary_pairs(self.output_dir, "missing-course"), [])
        self.assertEqual(judge_boundary_pairs(self.output_dir, "course-1", limit=0), [])
        path = memory_path(self.output_dir, "course-1")
        path.write_text("{corrupt", encoding="utf-8")
        self.assertEqual(judge_boundary_pairs(self.output_dir, "course-1"), [])


class JudgeRulingImportTests(unittest.TestCase):
    """THINK-LADDER-2 设计 B：rulings 导入漏斗（user-adjudication-supreme 三路）。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.output_dir = Path(self.temporary.name)
        sink_course_examples(self.output_dir, "course-1", [
            _audit("这个费米能及很重要", "这个费米能级很重要，"),
        ], sub_id="sub-1")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _ruling(self, ruling: str, reason: str = "glossary_match") -> dict:
        return {"wrong": "费米能及", "right": "费米能级", "ruling": ruling, "reason_code": reason}

    def _pairs(self, signal: int = 2) -> list[dict]:
        return [{"wrong": "费米能及", "right": "费米能级", "signal_count": signal}]

    def test_valid_with_signal_confirms_with_judge_provenance(self) -> None:
        result = import_boundary_rulings(
            self.output_dir, "course-1", pairs=self._pairs(2),
            rulings={"rulings": [self._ruling("valid")]},
        )
        self.assertEqual(result, {"confirmed": 1, "annotated": 0, "no_op": 0, "skipped": 0})
        ledger = _read_doc(self.output_dir, "course-1")["confirmed_mappings"]
        self.assertTrue(ledger[0]["judge_confirmed"])
        self.assertEqual(ledger[0]["judge_reason_code"], "glossary_match")
        self.assertEqual(ledger[0]["judge_signal_count"], 2)
        # 确认词进字幕链注入面（与人工确认同落点）
        self.assertEqual(confirmed_memory_terms(self.output_dir, "course-1"), ("费米能级",))
        # 视图与撤销面：judge 件在 judge_confirmed_rows 出列，dismiss 可撤销
        rows = judge_confirmed_term_rows(self.output_dir, "course-1")
        self.assertEqual(
            rows,
            [{"wrong": "费米能及", "right": "费米能级",
              "signal_count": 2, "reason_code": "glossary_match",
              "confirmed_at": rows[0]["confirmed_at"]}],
        )
        revoke = dismiss_term_mapping(self.output_dir, "course-1", wrong="费米能及", right="费米能级")
        self.assertEqual(revoke["dismissed"], True)
        self.assertEqual(judge_confirmed_term_rows(self.output_dir, "course-1"), [])
        self.assertEqual(confirmed_memory_terms(self.output_dir, "course-1"), ())

    def test_valid_below_signal_floor_is_no_op(self) -> None:
        result = import_boundary_rulings(
            self.output_dir, "course-1", pairs=self._pairs(1),
            rulings={"rulings": [self._ruling("valid")]},
        )
        self.assertEqual(result, {"confirmed": 0, "annotated": 0, "no_op": 1, "skipped": 0})
        self.assertNotIn("confirmed_mappings", _read_doc(self.output_dir, "course-1"))

    def test_unsure_and_unruled_are_no_ops(self) -> None:
        result = import_boundary_rulings(
            self.output_dir, "course-1", pairs=self._pairs(2),
            rulings={"rulings": [self._ruling("unsure", "insufficient_context")]},
        )
        self.assertEqual(result, {"confirmed": 0, "annotated": 0, "no_op": 1, "skipped": 0})
        document = _read_doc(self.output_dir, "course-1")
        self.assertNotIn("confirmed_mappings", document)
        self.assertNotIn("judge_annotations", document)
        # unsure=no-op：候选原样保留（不折叠不出列）
        self.assertEqual(len(term_candidates(self.output_dir, "course-1")), 1)
        # unruled（worker 裁决缺位）：rulings 里没有该对 → 纯 no-op
        empty = import_boundary_rulings(
            self.output_dir, "course-1", pairs=self._pairs(2), rulings={"rulings": []},
        )
        self.assertEqual(empty, {"confirmed": 0, "annotated": 0, "no_op": 0, "skipped": 0})

    def test_invalid_folds_annotation_never_dismisses(self) -> None:
        result = import_boundary_rulings(
            self.output_dir, "course-1", pairs=self._pairs(0),
            rulings={"rulings": [self._ruling("invalid", "glossary_conflict")]},
        )
        self.assertEqual(result, {"confirmed": 0, "annotated": 1, "no_op": 0, "skipped": 0})
        document = _read_doc(self.output_dir, "course-1")
        self.assertNotIn("dismissed_mappings", document, "invalid 只折叠绝不忽略")
        note = document["judge_annotations"][0]
        self.assertEqual(note["ruling"], "invalid")
        self.assertEqual(note["reason_code"], "glossary_conflict")
        self.assertEqual(note["signal_count"], 0)
        # 幂等：同对重复裁决不再叠注记
        again = import_boundary_rulings(
            self.output_dir, "course-1", pairs=self._pairs(0),
            rulings={"rulings": [self._ruling("invalid", "glossary_conflict")]},
        )
        self.assertEqual(again["annotated"], 0)
        self.assertEqual(len(_read_doc(self.output_dir, "course-1")["judge_annotations"]), 1)
        # 已确认终态对不再注记（用户裁决至上）
        revoke_document = _read_doc(self.output_dir, "course-1")
        revoke_document["judge_annotations"] = []
        _write_doc(self.output_dir, "course-1", revoke_document)
        confirm_term_mapping(self.output_dir, "course-1", wrong="费米能及", right="费米能级")
        blocked = import_boundary_rulings(
            self.output_dir, "course-1", pairs=self._pairs(0),
            rulings={"rulings": [self._ruling("invalid", "glossary_conflict")]},
        )
        self.assertEqual(blocked["no_op"], 1)
        self.assertEqual(_read_doc(self.output_dir, "course-1")["judge_annotations"], [])

    def test_out_of_set_and_unrequested_entries_are_skipped(self) -> None:
        result = import_boundary_rulings(
            self.output_dir, "course-1", pairs=self._pairs(2),
            rulings={"rulings": [
                self._ruling("maybe"),                                   # ruling 越集
                self._ruling("valid", "because_i_said_so"),              # reason 越集
                {"wrong": "没请求", "right": "也没词", "ruling": "valid",
                 "reason_code": "glossary_match"},                       # 未请求对
                "garbage",                                               # 非 dict
            ]},
        )
        self.assertEqual(result, {"confirmed": 0, "annotated": 0, "no_op": 0, "skipped": 4})
        document = _read_doc(self.output_dir, "course-1")
        self.assertNotIn("confirmed_mappings", document)
        self.assertNotIn("judge_annotations", document)

    def test_gate_skip_when_pair_left_candidates_after_dispatch(self) -> None:
        # 派发后对已不在候选集（账目淘汰/示例蒸发）且无终态行：confirm 资格门
        # 闭集拒绝 → skipped，不写确认台账。
        result = import_boundary_rulings(
            self.output_dir, "course-1",
            pairs=[{"wrong": "凭空词", "right": "也凭空", "signal_count": 2}],
            rulings={"rulings": [{
                "wrong": "凭空词", "right": "也凭空",
                "ruling": "valid", "reason_code": "glossary_match",
            }]},
        )
        self.assertEqual(result, {"confirmed": 0, "annotated": 0, "no_op": 0, "skipped": 1})
        self.assertNotIn("confirmed_mappings", _read_doc(self.output_dir, "course-1"))

    def test_user_dismissed_pair_stays_dismissed_on_late_valid_ruling(self) -> None:
        # 派发后用户已手动忽略（终态=裁决至上）：晚到 valid 裁决=幂等 no-op，
        # 绝不翻案进确认台账。
        dismiss_term_mapping(self.output_dir, "course-1", wrong="费米能及", right="费米能级")
        result = import_boundary_rulings(
            self.output_dir, "course-1", pairs=self._pairs(2),
            rulings={"rulings": [self._ruling("valid")]},
        )
        self.assertEqual(result, {"confirmed": 0, "annotated": 0, "no_op": 1, "skipped": 0})
        document = _read_doc(self.output_dir, "course-1")
        self.assertNotIn("confirmed_mappings", document)
        self.assertEqual(len(document["dismissed_mappings"]), 1)

    def test_annotation_cap_keeps_latest_hundred(self) -> None:
        entries = [
            _stats_entry(f"词{i:03d}甲", f"词{i:03d}乙", {"s1": 1}, 1.0 + i)
            for i in range(101)
        ]
        document = _read_doc(self.output_dir, "course-1")
        document["mapping_stats"] = entries
        _write_doc(self.output_dir, "course-1", document)
        pairs = [
            {"wrong": f"词{i:03d}甲", "right": f"词{i:03d}乙", "signal_count": 0}
            for i in range(101)
        ]
        rulings = {"rulings": [
            {"wrong": pair["wrong"], "right": pair["right"],
             "ruling": "invalid", "reason_code": "not_in_glossary"}
            for pair in pairs
        ]}
        result = import_boundary_rulings(
            self.output_dir, "course-1", pairs=pairs, rulings=rulings,
        )
        self.assertEqual(result["annotated"], 101)
        notes = _read_doc(self.output_dir, "course-1")["judge_annotations"]
        self.assertEqual(len(notes), 100)  # 帽 100：最早先出
        self.assertEqual(notes[-1]["wrong"], "词100甲")
        self.assertEqual(notes[0]["wrong"], "词001甲")

    def test_malformed_inputs_fail_closed_to_zero_receipt(self) -> None:
        self.assertEqual(
            import_boundary_rulings(self.output_dir, "course-1", pairs=[], rulings={"rulings": []}),
            {"confirmed": 0, "annotated": 0, "no_op": 0, "skipped": 0},
        )
        self.assertEqual(
            import_boundary_rulings(
                self.output_dir, "missing", pairs=self._pairs(2), rulings={"rulings": [self._ruling("valid")]},
            ),
            {"confirmed": 0, "annotated": 0, "no_op": 0, "skipped": 1},
        )
        self.assertEqual(
            import_boundary_rulings(
                self.output_dir, "course-1", pairs=self._pairs(2), rulings={"rulings": "not-a-list"},
            ),
            {"confirmed": 0, "annotated": 0, "no_op": 0, "skipped": 0},
        )

    def test_annotation_rows_reader_shape_and_order(self) -> None:
        import_boundary_rulings(
            self.output_dir, "course-1", pairs=self._pairs(0),
            rulings={"rulings": [self._ruling("invalid", "glossary_conflict")]},
        )
        rows = judge_annotation_rows(self.output_dir, "course-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            set(rows[0]),
            {"wrong", "right", "ruling", "reason_code", "signal_count", "judged_at"},
        )
        self.assertEqual(rows[0]["ruling"], "invalid")
        self.assertEqual(judge_annotation_rows(self.output_dir, "missing"), [])

    def test_view_rows_carry_judge_annotation_overlay(self) -> None:
        app = CourseLensApplication(self.output_dir)
        try:
            app.catalog_repository.upsert_course("course-1", "概率论")
            app.catalog_repository.upsert_lecture(
                "course-1", {"sub_id": "lecture-1", "sub_title": "第1讲"}
            )
            app._credentials = {"student_id": "configured", "password": "configured"}
            review = app.course_review("course-1")
            self.assertEqual(review["term_candidates"]["judge_confirmed_rows"], [])
            import_boundary_rulings(
                self.output_dir, "course-1",
                pairs=[{"wrong": "费米能及", "right": "费米能级", "signal_count": 1}],
                rulings={"rulings": [self._ruling("invalid", "glossary_conflict")]},
            )
            review = app.course_review("course-1")
            rows = review["term_candidates"]["rows"]
            self.assertEqual(rows[0]["judge_ruling"], "invalid")
            self.assertEqual(rows[0]["judge_reason_code"], "glossary_conflict")
            self.assertIn("judge_judged_at", rows[0])
            # 无注记的行不带 judge 键（按需覆盖，旧行形状不膨胀）
            entries = [_stats_entry("另一对甲", "另一对乙", {"s1": 1}, 9.0)]
            document = _read_doc(self.output_dir, "course-1")
            document["mapping_stats"] = entries
            _write_doc(self.output_dir, "course-1", document)
            rows = app.course_review("course-1")["term_candidates"]["rows"]
            plain = next(row for row in rows if row["wrong"] == "另一对甲")
            self.assertNotIn("judge_ruling", plain)
        finally:
            app.close()
