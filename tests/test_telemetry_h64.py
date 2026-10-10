"""TELEMETRY-H64-1 AIRESEARCH H6/H4 遥测埋点行为钉。

两条「立即可做」假设的计数器（正源 ABDOCS-1 → docs/airesearch-decisions.md §②）：

1. **H6**——总结页引用点击 × 停留时长相关性观测（``study_events_v3`` 闭集
   事件）：opt-in 门、零内容落盘、可擦除、读出按 sub_id 并置。palette 引用
   点击发射已随 P3-PKGC 上线、注册表解锁随 STUDY-EVENT-1（P3-CONTRACT-1
   §⑤：kind 扩三元组、click 分诊集合化、H6 键只认 summary_*、加性 palette
   节、旧 CHECK 库就地重建）；summary 页发射仍属后续遥测合同。
2. **H4**——偏差信号按讲次闭集分桶（first/second/later）：与 count 同口径
   只记 recorded（回放幂等不重复计）；调用点透传随 STUDY-EVENT-1 落地
   （sub_id → 目录讲次正典序 → 桶，读不出=不记零行为差异）。

反哺闭环本体由 tests/test_course_memory_feedback.py 钉住；观看分析本体由
tests/test_analytics.py 钉住——本文件只钉新增埋点面。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from path_utils import PROJECT_ROOT
from src.application import CourseLensApplication
from src.runtime.analytics import (
    ANALYTICS_VERSION,
    STUDY_EVENT_KINDS,
    ensure_analytics_schema,
    record_study_event,
    set_analytics_enabled,
    study_telemetry_summary,
    delete_study_events,
)
from src.runtime.course_memory import memory_path, sink_course_examples
from src.runtime.course_memory_feedback import (
    LECTURE_BUCKETS,
    record_product_deviations,
)
from src.runtime.http_api import FrontendSessionRegistry, make_handler
from tests.http_services import http_services


_FERM = ("这个费米能及很重要", "这个费米能级很重要，")


def _audit(before: str, after: str) -> dict:
    return {"start_ms": 0, "end_ms": 1000, "before": before, "after": after}


class H6StudyEventTests(unittest.TestCase):
    """H6 计数器本体：opt-in 门、闭集种类、口径帽、读出并置、可擦除。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.db = Path(self.temporary.name) / "learning.db"
        ensure_analytics_schema(self.db)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_recording_is_opt_in_and_honest_noop_when_disabled(self) -> None:
        self.assertIs(record_study_event(
            self.db, kind="summary_citation_click", course_id="c1", sub_id="s1"
        ), False)
        self.assertEqual(study_telemetry_summary(self.db)["summary"], None)
        self.assertEqual(
            study_telemetry_summary(self.db)["reason"], "analytics_disabled"
        )

    def test_enabled_recording_writes_closed_set_row_with_version(self) -> None:
        set_analytics_enabled(self.db, True)
        self.assertIs(record_study_event(
            self.db, kind="summary_citation_click", course_id="c1", sub_id="s1"
        ), True)
        self.assertIs(record_study_event(
            self.db, kind="summary_dwell", course_id="c1", sub_id="s1", dwell_ms=90_000
        ), True)
        import sqlite3

        with __import__("contextlib").closing(sqlite3.connect(self.db)) as db:
            rows = db.execute(
                "SELECT kind,course_id,sub_id,dwell_ms,version FROM study_events_v3 ORDER BY kind"
            ).fetchall()
        self.assertEqual(
            rows,
            [
                ("summary_citation_click", "c1", "s1", 0, ANALYTICS_VERSION),
                ("summary_dwell", "c1", "s1", 90_000, ANALYTICS_VERSION),
            ],
        )

    def test_kind_is_closed_set(self) -> None:
        set_analytics_enabled(self.db, True)
        self.assertEqual(
            STUDY_EVENT_KINDS,
            ("summary_citation_click", "summary_dwell", "palette_citation_click"),
        )
        # P3 §⑤ 解锁：palette 引用点击是注册表成员，真实落账
        self.assertIs(record_study_event(
            self.db, kind="palette_citation_click", course_id="c1", sub_id="s1"
        ), True)
        with self.assertRaises(ValueError) as caught:
            record_study_event(self.db, kind="page_view", course_id="c1", sub_id="s1")
        self.assertEqual(str(caught.exception), "study_event_kind_invalid")

    def test_palette_click_triage_forces_zero_dwell(self) -> None:
        # 分诊集合化：palette click 走 click 类口径，传了 dwell 也归零
        set_analytics_enabled(self.db, True)
        record_study_event(
            self.db, kind="palette_citation_click", course_id="c1", sub_id="s1", dwell_ms=5_000
        )
        with closing(sqlite3.connect(self.db)) as db:
            dwell = db.execute(
                "SELECT dwell_ms FROM study_events_v3 WHERE kind='palette_citation_click'"
            ).fetchone()[0]
        self.assertEqual(int(dwell), 0)

    def test_summary_h6_keys_stay_summary_scoped_and_gain_palette_section(self) -> None:
        set_analytics_enabled(self.db, True)
        record_study_event(self.db, kind="summary_citation_click", course_id="c1", sub_id="s1")
        record_study_event(self.db, kind="summary_dwell", course_id="c1", sub_id="s1", dwell_ms=60_000)
        record_study_event(self.db, kind="palette_citation_click", course_id="c1", sub_id="s2")
        record_study_event(self.db, kind="palette_citation_click", course_id="c1", sub_id="s2")
        record_study_event(self.db, kind="palette_citation_click", course_id="c2", sub_id="s9")
        summary = study_telemetry_summary(self.db)["summary"]
        # H6 既有键只统计 summary_*（口径不串）：palette 行不进点击/停留/计数
        self.assertEqual(summary["event_count"], 2)
        self.assertEqual(summary["citation_clicks"], 1)
        self.assertEqual(summary["dwell_sessions"], 1)
        self.assertEqual(summary["dwell_ms_total"], 60_000)
        self.assertEqual(len(summary["by_sub"]), 1)
        # P3 §⑤ 加性 palette 节：H1 深度引用点击率观测基表（按点击数降序）
        self.assertEqual(
            summary["palette"],
            {
                "citation_clicks": 3,
                "by_sub": [
                    {"course_id": "c1", "sub_id": "s2", "citation_clicks": 2},
                    {"course_id": "c2", "sub_id": "s9", "citation_clicks": 1},
                ],
            },
        )

    def test_legacy_check_constraint_is_rebuilt_in_place(self) -> None:
        #存量库形状：TELEMETRY-H64 时代的双 kind CHECK，palette INSERT 被 CHECK 拒
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript(
                """
                DROP TABLE study_events_v3;
                CREATE TABLE study_events_v3 (
                    event_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('summary_citation_click','summary_dwell')),
                    course_id TEXT NOT NULL,
                    sub_id TEXT NOT NULL,
                    dwell_ms INTEGER NOT NULL DEFAULT 0,
                    occurred_at REAL NOT NULL,
                    version TEXT NOT NULL
                );
                INSERT INTO study_events_v3
                    VALUES('legacy1','summary_dwell','c1','s1',100,1.0,'older');
                """
            )
            db.commit()
        with closing(sqlite3.connect(self.db)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO study_events_v3"
                    " VALUES('blocked','palette_citation_click','c1','s2',0,2.0,'x')"
                )
                db.commit()
        ensure_analytics_schema(self.db)  # 就地重建：行原样搬运，palette 可落
        set_analytics_enabled(self.db, True)
        self.assertIs(record_study_event(
            self.db, kind="palette_citation_click", course_id="c1", sub_id="s2"
        ), True)
        self.assertIs(record_study_event(
            self.db, kind="summary_citation_click", course_id="c1", sub_id="s3"
        ), True)
        with closing(sqlite3.connect(self.db)) as db:
            kinds = [row[0] for row in db.execute(
                "SELECT kind FROM study_events_v3 ORDER BY kind"
            ).fetchall()]
            legacy_dwell = db.execute(
                "SELECT dwell_ms FROM study_events_v3 WHERE kind='summary_dwell'"
            ).fetchone()[0]
        self.assertEqual(
            kinds, ["palette_citation_click", "summary_citation_click", "summary_dwell"]
        )
        self.assertEqual(int(legacy_dwell), 100)
        ensure_analytics_schema(self.db)  # 重建后幂等：再跑零动作不炸

    def test_required_fields_are_enforced(self) -> None:
        set_analytics_enabled(self.db, True)
        for kwargs in (
            {"kind": "summary_dwell", "course_id": "", "sub_id": "s1"},
            {"kind": "summary_dwell", "course_id": "c1", "sub_id": "  "},
        ):
            with self.assertRaises(ValueError) as caught:
                record_study_event(self.db, **kwargs)
            self.assertEqual(str(caught.exception), "study_event_field_invalid")

    def test_dwell_scope_is_clamped_and_click_kind_forces_zero(self) -> None:
        set_analytics_enabled(self.db, True)
        # 点击事件没有停留口径：传了也归零
        record_study_event(
            self.db, kind="summary_citation_click", course_id="c1", sub_id="s1", dwell_ms=5_000
        )
        # 停留帽 4 小时：超帽收口、负值归零
        record_study_event(
            self.db, kind="summary_dwell", course_id="c1", sub_id="s1", dwell_ms=10**9
        )
        record_study_event(
            self.db, kind="summary_dwell", course_id="c1", sub_id="s1", dwell_ms=-7
        )
        import sqlite3

        with __import__("contextlib").closing(sqlite3.connect(self.db)) as db:
            dwell = [
                int(row[0])
                for row in db.execute(
                    "SELECT dwell_ms FROM study_events_v3 WHERE kind='summary_dwell' ORDER BY dwell_ms"
                ).fetchall()
            ]
        self.assertEqual(dwell, [0, 14_400_000])
        with __import__("contextlib").closing(sqlite3.connect(self.db)) as db:
            click_dwell = db.execute(
                "SELECT dwell_ms FROM study_events_v3 WHERE kind='summary_citation_click'"
            ).fetchone()[0]
        self.assertEqual(int(click_dwell), 0)

    def test_summary_joins_clicks_and_dwell_per_sub(self) -> None:
        set_analytics_enabled(self.db, True)
        record_study_event(self.db, kind="summary_citation_click", course_id="c1", sub_id="s1")
        record_study_event(self.db, kind="summary_citation_click", course_id="c1", sub_id="s1")
        record_study_event(self.db, kind="summary_dwell", course_id="c1", sub_id="s1", dwell_ms=60_000)
        record_study_event(self.db, kind="summary_dwell", course_id="c2", sub_id="s9", dwell_ms=30_000)
        summary = study_telemetry_summary(self.db)["summary"]
        # H6 观测量：同一 sub_id 的点击数与停留时长并排可比
        self.assertEqual(summary["citation_clicks"], 2)
        self.assertEqual(summary["dwell_sessions"], 2)
        self.assertEqual(summary["dwell_ms_total"], 90_000)
        self.assertEqual(summary["event_count"], 4)
        self.assertEqual(
            summary["by_sub"][0],
            {
                "course_id": "c1", "sub_id": "s1",
                "citation_clicks": 2, "dwell_sessions": 1, "dwell_ms_total": 60_000,
            },
        )
        self.assertEqual(len(summary["by_sub"]), 2)

    def test_study_events_are_erasable_and_schema_idempotent(self) -> None:
        set_analytics_enabled(self.db, True)
        record_study_event(self.db, kind="summary_dwell", course_id="c1", sub_id="s1", dwell_ms=1)
        record_study_event(self.db, kind="palette_citation_click", course_id="c1", sub_id="s2")
        ensure_analytics_schema(self.db)  # 重复迁移不炸不加噪
        self.assertEqual(delete_study_events(self.db), 2)  # 擦除面天然覆盖新 kind
        self.assertEqual(study_telemetry_summary(self.db)["summary"]["event_count"], 0)


class H4LectureBucketTests(unittest.TestCase):
    """H4 讲次桶：与 count 同口径记 recorded、幂等不重复、闭集过滤。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.output_dir = Path(self.temporary.name)
        sink_course_examples(self.output_dir, "course-1", [_audit(*_FERM)])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _signal(self) -> dict:
        document = json.loads(
            memory_path(self.output_dir, "course-1").read_text(encoding="utf-8")
        )
        return document["signals"][0]

    def test_buckets_track_recorded_count_per_closed_set_lecture(self) -> None:
        self.assertEqual(LECTURE_BUCKETS, ("first", "second", "later"))
        hits, recorded = record_product_deviations(
            self.output_dir, "course-1", "费米能及与费米能及",
            source="summary", lecture_bucket="first",
        )
        self.assertEqual((hits, recorded), (2, 2))
        record_product_deviations(
            self.output_dir, "course-1", "又见费米能及",
            source="answer", lecture_bucket="second",
        )
        signal = self._signal()
        self.assertEqual(signal["count"], 3)
        # H4 观测量：首讲基线 vs 第二讲的偏差处数直接可比
        self.assertEqual(signal["lecture_buckets"], {"first": 2, "second": 1})

    def test_replay_is_idempotent_and_does_not_double_count_bucket(self) -> None:
        record_product_deviations(
            self.output_dir, "course-1", "费米能及",
            source="summary", lecture_bucket="second",
        )
        hits, recorded = record_product_deviations(
            self.output_dir, "course-1", "费米能及",
            source="summary", lecture_bucket="later",
        )
        self.assertEqual((hits, recorded), (1, 0))
        self.assertEqual(self._signal()["lecture_buckets"], {"second": 1})

    def test_invalid_bucket_is_ignored_without_throwing(self) -> None:
        hits, recorded = record_product_deviations(
            self.output_dir, "course-1", "费米能及",
            source="answer", lecture_bucket="lecture-99",
        )
        self.assertEqual((hits, recorded), (1, 1))
        self.assertEqual(self._signal()["lecture_buckets"], {})

    def test_reload_keeps_only_closed_set_nonzero_buckets(self) -> None:
        record_product_deviations(
            self.output_dir, "course-1", "费米能及",
            source="summary", lecture_bucket="first",
        )
        document = json.loads(
            memory_path(self.output_dir, "course-1").read_text(encoding="utf-8")
        )
        document["signals"][0]["lecture_buckets"] = {
            "first": 3, "hacker": 9, "second": -5, "later": "x",
        }
        memory_path(self.output_dir, "course-1").write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )
        record_product_deviations(
            self.output_dir, "course-1", "新文本费米能及",
            source="summary", lecture_bucket="later",
        )
        self.assertEqual(
            self._signal()["lecture_buckets"], {"first": 3, "later": 1}
        )

    def test_legacy_call_without_bucket_keeps_buckets_untouched(self) -> None:
        record_product_deviations(
            self.output_dir, "course-1", "费米能及",
            source="summary", lecture_bucket="first",
        )
        record_product_deviations(self.output_dir, "course-1", "再看费米能及", source="answer")
        self.assertEqual(self._signal()["lecture_buckets"], {"first": 1})


class H4SinkPassthroughTests(unittest.TestCase):
    """H4 调用点透传（STUDY-EVENT-1）：sub_id → 目录讲次正典序 → 闭集桶。

    序号来源=``catalog_repository.lectures_for_course`` 的 date,sub_title 序
    （产品既有讲次排序读侧）；读不出/讲次缺席=空桶不记，反哺零行为差异。
    """

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.output_dir = Path(self.temporary.name)
        sink_course_examples(self.output_dir, "course-1", [_audit(*_FERM)])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _service(self, lectures) -> CourseLensApplication:
        service = CourseLensApplication.__new__(CourseLensApplication)
        service.output_dir = self.output_dir
        service.catalog_repository = SimpleNamespace(
            lectures_for_course=lambda course_id: lectures
        )
        return service

    def _signal(self) -> dict:
        document = json.loads(
            memory_path(self.output_dir, "course-1").read_text(encoding="utf-8")
        )
        return document["signals"][0]

    def test_bucket_follows_catalog_lecture_order(self) -> None:
        lectures = [
            {"sub_id": "s-first", "date": "2026-09-01", "sub_title": "a"},
            {"sub_id": "s-second", "date": "2026-09-08", "sub_title": "b"},
            {"sub_id": "s-third", "date": "2026-09-15", "sub_title": "c"},
        ]
        service = self._service(lectures)
        self.assertEqual(service._lecture_bucket_for("course-1", "s-first"), "first")
        self.assertEqual(service._lecture_bucket_for("course-1", "s-second"), "second")
        self.assertEqual(service._lecture_bucket_for("course-1", "s-third"), "later")
        # 讲次不在目录内/缺席 sub_id：空桶=不记账（fail-closed 不抛）
        self.assertEqual(service._lecture_bucket_for("course-1", "s-unknown"), "")
        self.assertEqual(service._lecture_bucket_for("course-1", ""), "")

    def test_bucket_read_failure_degrades_to_no_bucket(self) -> None:
        def _boom(course_id):
            raise RuntimeError("catalog unavailable")

        service = self._service(_boom)
        self.assertEqual(service._lecture_bucket_for("course-1", "s1"), "")

    def test_sink_passes_sub_id_bucket_into_signal_account(self) -> None:
        lectures = [
            {"sub_id": "s1", "date": "2026-09-01", "sub_title": "a"},
            {"sub_id": "s2", "date": "2026-09-08", "sub_title": "b"},
        ]
        service = self._service(lectures)
        service._memory_feedback_sink("course-1", "费米能及", source="summary", sub_id="s1")
        service._memory_feedback_sink("course-1", "又见费米能及", source="summary", sub_id="s2")
        self.assertEqual(self._signal()["count"], 2)
        self.assertEqual(self._signal()["lecture_buckets"], {"first": 1, "second": 1})
        # 旧式调用（不带 sub_id）：桶账零行为差异
        service._memory_feedback_sink("course-1", "再看费米能及", source="answer")
        self.assertEqual(self._signal()["count"], 3)
        self.assertEqual(self._signal()["lecture_buckets"], {"first": 1, "second": 1})


class _TelemetryStubService:
    """只承载 H6 三方法的最小服务壳（http_services 白名单透传）。"""

    def __init__(self, db_path: Path) -> None:
        ensure_analytics_schema(db_path)
        self.learning_store = SimpleNamespace(path=db_path)

    def authentication_snapshot(self) -> dict:
        # analytics/actions 走既有动作族会话门；遥测擦除与学校会话无关，
        # stub 直接给 ready 让门放行（被测的是擦除行为，不是门）。
        return {"state": "ready"}

    def record_study_event(self, *, kind: str, course_id: str, sub_id: str, dwell_ms: int = 0) -> dict:
        recorded = record_study_event(
            self.learning_store.path, kind=kind, course_id=course_id,
            sub_id=sub_id, dwell_ms=dwell_ms,
        )
        return {"recorded": bool(recorded), "kind": str(kind or "")}

    def study_telemetry_summary(self) -> dict:
        return study_telemetry_summary(self.learning_store.path)

    def delete_study_events(self) -> dict:
        return {"deleted_events": delete_study_events(self.learning_store.path)}


class StudyEventApiTests(unittest.TestCase):
    """H6 端点闭集契约：越界 400 闭集码、未开启诚实 recorded=False、可擦除。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.service = _TelemetryStubService(Path(self.temporary.name) / "learning.db")
        self.sessions = FrontendSessionRegistry(
            lease_seconds=30, shutdown_grace_seconds=30, poll_seconds=1,
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(http_services(self.service), PROJECT_ROOT / "frontend", frontend_sessions=self.sessions),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.sessions.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def _post(self, route: str, body: dict, expected: int = 200):
        request = Request(
            f"{self.base}{route}", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            response = urlopen(request)
        except HTTPError as exc:
            if exc.code != expected:
                raise
            return exc.code, json.loads(exc.read())
        with response:
            self.assertEqual(response.status, expected)
            return response.status, json.loads(response.read())

    def _get(self, route: str):
        with urlopen(f"{self.base}{route}") as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read())

    def test_study_event_endpoint_rejects_non_closed_set_kind(self) -> None:
        status, payload = self._post(
            "/api/v3/analytics/study-events",
            {"kind": "page_view", "course_id": "c1", "sub_id": "s1"}, expected=400,
        )
        self.assertEqual(payload["error_code"], "study_event_kind_invalid")

    def test_study_event_endpoint_rejects_bad_fields(self) -> None:
        for body in (
            {"kind": "summary_dwell", "course_id": "", "sub_id": "s1"},
            {"kind": "summary_dwell", "course_id": "c1", "sub_id": "s1", "dwell_ms": "90"},
            {"kind": "summary_dwell", "course_id": "c1", "sub_id": "s1", "dwell_ms": -1},
            {"kind": "summary_citation_click", "course_id": "c" * 191, "sub_id": "s1"},
        ):
            status, payload = self._post(
                "/api/v3/analytics/study-events", body, expected=400,
            )
            self.assertEqual(payload["error_code"], "study_event_field_invalid", body)

    def test_study_event_roundtrip_is_honest_when_disabled_then_records(self) -> None:
        # 未开启：200 + recorded=False（诚实回执，不落行、不挡前端主链）
        _, payload = self._post(
            "/api/v3/analytics/study-events",
            {"kind": "summary_citation_click", "course_id": "c1", "sub_id": "s1"},
        )
        self.assertEqual(payload["data"]["recorded"], False)
        set_analytics_enabled(self.service.learning_store.path, True)
        _, payload = self._post(
            "/api/v3/analytics/study-events",
            {"kind": "summary_dwell", "course_id": "c1", "sub_id": "s1", "dwell_ms": 45_000},
        )
        self.assertEqual(payload["data"]["recorded"], True)
        summary = self._get("/api/v3/analytics/study-summary")["data"]["summary"]
        self.assertEqual(summary["citation_clicks"], 0)
        self.assertEqual(summary["dwell_ms_total"], 45_000)

    def test_palette_click_endpoint_unlock_three_states(self) -> None:
        # 状态一：未开启=200 recorded=False（诚实回执，前端 fire-and-forget 不挡主链）
        _, payload = self._post(
            "/api/v3/analytics/study-events",
            {"kind": "palette_citation_click", "course_id": "c1", "sub_id": "s1", "dwell_ms": 0},
        )
        self.assertEqual(payload["data"]["recorded"], False)
        # 状态二：开启=注册表放行真实落账（P3-PKGC 静默拒绝就此解除）
        set_analytics_enabled(self.service.learning_store.path, True)
        _, payload = self._post(
            "/api/v3/analytics/study-events",
            {"kind": "palette_citation_click", "course_id": "c1", "sub_id": "s1", "dwell_ms": 0},
        )
        self.assertEqual(payload["data"]["recorded"], True)
        # 状态三：越界种类仍被注册表闭集拒绝（解锁≠闭集放开）
        status, payload = self._post(
            "/api/v3/analytics/study-events",
            {"kind": "palette_dwell", "course_id": "c1", "sub_id": "s1"}, expected=400,
        )
        self.assertEqual(payload["error_code"], "study_event_kind_invalid")
        # 读出面：palette 节真实计数；palette 行不进 H6 键（口径不串）
        summary = self._get("/api/v3/analytics/study-summary")["data"]["summary"]
        self.assertEqual(summary["palette"]["citation_clicks"], 1)
        self.assertEqual(summary["event_count"], 0)

    def test_delete_study_events_action_erasable(self) -> None:
        set_analytics_enabled(self.service.learning_store.path, True)
        self._post(
            "/api/v3/analytics/study-events",
            {"kind": "summary_dwell", "course_id": "c1", "sub_id": "s1", "dwell_ms": 1},
        )
        _, payload = self._post(
            "/api/v3/analytics/actions", {"action": "delete_study_events"},
        )
        self.assertEqual(payload["data"]["deleted_events"], 1)
        summary = self._get("/api/v3/analytics/study-summary")["data"]["summary"]
        self.assertEqual(summary["event_count"], 0)


if __name__ == "__main__":
    unittest.main()
