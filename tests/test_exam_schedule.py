"""Sanitized-fixture and contract tests for the Fudan exam-arrangement context.

All fixture values are synthetic.  Raw HTML, student ids, and notes must
never reach the persisted exam context.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import requests

from src.runtime.exam_schedule import (
    ACTIVE_WINDOW_HOURS,
    EXAM_ARRANGE_URL,
    PLAN_STRATEGIES,
    ReviewPlanValidationError,
    context_from_exam,
    exam_state_for_window,
    fetch_exam_rows,
    match_exam_row,
    parse_exam_arrangement,
    refresh_context_state,
    semester_range_from_start,
    unavailable_context,
    user_confirmed_context,
)
from src.runtime.learning_store import LearningStore
from src.runtime.student_features import (
    build_review_steps,
    ensure_student_feature_schema,
    list_review_plans,
    quiz_attempt_stats,
    save_review_plan,
    save_quiz_items,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")

_STUDENT_ID = "20301080001"

_EXAM_ROW_TEMPLATE = """
<tr class="unfinished" data-finished="false"><td><div class="time">{date} {time}<span>HGXK</span><span>2</span><span>H3208</span></div></td><td><div><span>人工智能基础</span><span>ICSE30021h.01</span><span>（闭卷）</span></div><div><span>期末</span></div></td><td>SECRET-NOTE-VALUE</td><td>未结束</td></tr>
"""

_FINISHED_ROW = """
<tr data-finished="true" class="finished hide"><td><div class="time">2026-01-04 08:30~10:30<span>HGXK</span><span>1</span><span>H1101</span></div></td><td><div><span>已结束课程</span><span>OLD1000.01</span><span>（闭卷）</span></div><div><span>期末</span></div></td><td>旧备注</td><td>已结束</td></tr>
"""

_DATE_ONLY_ROW = """
<tr class="unfinished"><td><div class="time">{date}</div></td><td><div><span>仅日期课程</span><span>DATE1000.01</span><span>（闭卷）</span></div><div><span>期末</span></div></td><td></td><td>未结束</td></tr>
"""

_MALFORMED_ROW = """
<tr class="unfinished"><td><div class="time">坏日期 25:99~26:00</div></td><td><div><span>坏行课程</span><span>BAD1000.01</span><span>（闭卷）</span></div><div><span>期末</span></div></td><td></td><td>未结束</td></tr>
"""


def _page(*rows: str) -> str:
    body = "".join(rows)
    return (
        '<html><body><table class="layout"><tr><td>noise</td></tr></table>'
        f'<table class="exam-table"><tbody>{body}</tbody></table></body></html>'
    )


def _upcoming_date(days: int) -> str:
    return (datetime.now(SHANGHAI) + timedelta(days=days)).strftime("%Y-%m-%d")


class _FakeResponse:
    def __init__(self, text: str, status: int = 200):
        self.text = text
        self.status_code = status


class _FakeVpn:
    def __init__(self, text: str = "", status: int = 200, error: Exception | None = None):
        self._text = text
        self._status = status
        self._error = error
        self.last_url = ""

    def get_allowed(self, url: str, **kwargs):
        self.last_url = str(url)
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._text, self._status)


class ExamArrangementParserTests(unittest.TestCase):
    def test_rows_parse_course_identity_and_ignore_finished_and_malformed(self):
        page = _page(
            _FINISHED_ROW,
            _EXAM_ROW_TEMPLATE.format(date="2026-09-20", time="08:30~10:30"),
            _DATE_ONLY_ROW.format(date="2026-09-30"),
            _MALFORMED_ROW,
        )
        rows = parse_exam_arrangement(page)
        self.assertEqual(len(rows), 2)
        active = rows[0]
        self.assertEqual(active["course_code"], "ICSE30021h.01")
        self.assertEqual(active["course_name"], "人工智能基础")
        self.assertEqual(active["exam_type"], "期末")
        self.assertEqual(active["category"], "闭卷")
        self.assertEqual(active["location"], "H3208")
        self.assertEqual(active["start_time"], "08:30")
        self.assertEqual(active["end_time"], "10:30")
        date_only = rows[1]
        self.assertEqual(date_only["start_time"], "")
        self.assertEqual(date_only["course_code"], "DATE1000.01")

    def test_finished_rows_are_never_candidates_even_for_matching_codes(self):
        page = _page(_FINISHED_ROW, _EXAM_ROW_TEMPLATE.format(date="2026-09-20", time="08:30~10:30"))
        row, detail = match_exam_row(
            parse_exam_arrangement(page), course_codes=["OLD1000.01"], course_name="", semester_range=None,
        )
        self.assertIsNone(row)
        self.assertEqual(detail, "no_match")

    def test_garbage_input_yields_no_rows_and_never_raises(self):
        self.assertEqual(parse_exam_arrangement(""), [])
        self.assertEqual(parse_exam_arrangement("<html><body>broken"), [])

    def test_note_is_parsed_but_never_persisted_in_context(self):
        rows = parse_exam_arrangement(_page(_EXAM_ROW_TEMPLATE.format(date="2026-09-20", time="08:30~10:30")))
        self.assertEqual(rows[0]["note"], "SECRET-NOTE-VALUE")
        context = context_from_exam(
            rows[0], now=datetime.now(SHANGHAI),
        )
        self.assertIsNotNone(context)
        self.assertNotIn("SECRET-NOTE-VALUE", json.dumps(context, ensure_ascii=False))


class ExamWindowTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 13, 12, 0, 0, tzinfo=SHANGHAI)

    def test_active_within_720_hours_before_exam(self):
        start = self.now + timedelta(hours=72)
        end = start + timedelta(hours=2)
        self.assertEqual(exam_state_for_window(start, end, self.now), "active")
        boundary = self.now + timedelta(hours=ACTIVE_WINDOW_HOURS)
        self.assertEqual(exam_state_for_window(boundary, boundary + timedelta(hours=1), self.now), "active")

    def test_outside_window_beyond_720_hours(self):
        start = self.now + timedelta(hours=ACTIVE_WINDOW_HOURS + 1)
        self.assertEqual(exam_state_for_window(start, start + timedelta(hours=2), self.now), "outside_window")

    def test_in_progress_exam_stays_active_until_passed(self):
        start = self.now - timedelta(minutes=30)
        end = self.now + timedelta(minutes=90)
        self.assertEqual(exam_state_for_window(start, end, self.now), "active")
        after = self.now + timedelta(minutes=120)
        self.assertEqual(exam_state_for_window(start, end, after), "passed")

    def test_date_precision_covers_exam_day_and_then_passes(self):
        context = context_from_exam({"date": "2027-09-20", "start_time": "", "end_time": ""}, now=self.now)
        self.assertIsNotNone(context)
        self.assertEqual(context["exam_precision"], "date")
        self.assertEqual(context["exam_at"], "2027-09-20")
        self.assertEqual(context["exam_state"], "outside_window")
        during = context_from_exam({"date": "2026-09-13", "start_time": "", "end_time": ""}, now=self.now)
        self.assertEqual(during["exam_state"], "active")
        passed = context_from_exam({"date": "2026-09-06", "start_time": "", "end_time": ""}, now=self.now)
        self.assertEqual(passed["exam_state"], "passed")

    def test_datetime_context_uses_shanghai_offset(self):
        context = context_from_exam(
            {"date": "2026-09-20", "start_time": "08:30", "end_time": "10:30"}, now=self.now,
        )
        self.assertEqual(context["exam_at"], "2026-09-20T08:30:00+08:00")
        self.assertEqual(context["exam_precision"], "datetime")
        self.assertEqual(context["exam_state"], "active")
        self.assertEqual(context["deadline_context"], "active")

    def test_user_confirmed_context_states(self):
        now = self.now.timestamp()
        active = user_confirmed_context(now + 72 * 3600, now=now)
        self.assertEqual(active["exam_source"], "user_confirmed")
        self.assertEqual(active["exam_state"], "active")
        outside = user_confirmed_context(now + (ACTIVE_WINDOW_HOURS + 5) * 3600, now=now)
        self.assertEqual(outside["exam_state"], "outside_window")
        passed = user_confirmed_context(now - 3600, now=now)
        self.assertEqual(passed["exam_state"], "passed")
        self.assertEqual(user_confirmed_context(0, now=now)["exam_state"], "unavailable")

    def test_unavailable_context_uses_closed_sets(self):
        context = unavailable_context("session_unavailable")
        self.assertEqual(context["exam_source"], "none")
        self.assertEqual(context["exam_precision"], "none")
        self.assertEqual(context["exam_state"], "unavailable")
        self.assertEqual(context["deadline_context"], "none")
        self.assertEqual(context["exam_at"], None)

    def test_refresh_context_state_reevaluates_stored_contexts(self):
        stored = {
            "exam_at": "2026-01-04T08:30:00+08:00",
            "exam_source": "fudan_jwgl",
            "exam_precision": "datetime",
            "exam_state": "active",
            "deadline_context": "active",
        }
        refreshed = refresh_context_state(stored, now=self.now.timestamp())
        self.assertEqual(refreshed["exam_state"], "passed")
        self.assertEqual(refreshed["deadline_context"], "none")
        missing = refresh_context_state(None, now=self.now.timestamp())
        self.assertEqual(missing["exam_state"], "unavailable")

    def test_semester_range_gates_name_matching(self):
        self.assertIsNone(semester_range_from_start("not-a-date"))
        start, end = semester_range_from_start("2026-09-01")
        self.assertEqual(start, date(2026, 9, 1))
        self.assertEqual(end, date(2027, 3, 30))


class ExamMatchingTests(unittest.TestCase):
    def setUp(self):
        self.rows = parse_exam_arrangement(
            _page(
                _EXAM_ROW_TEMPLATE.format(date="2026-09-20", time="08:30~10:30"),
                _DATE_ONLY_ROW.format(date="2026-09-30"),
            )
        )
        self.semester = semester_range_from_start("2026-09-01")

    def test_exact_code_match_wins(self):
        row, detail = match_exam_row(self.rows, course_codes=["icse30021h.01"], course_name="", semester_range=None)
        self.assertEqual(detail, "matched")
        self.assertEqual(row["course_code"], "ICSE30021h.01")

    def test_ambiguous_code_match_never_picks_first(self):
        row, detail = match_exam_row(self.rows, course_codes=["ICSE30021h.01", "DATE1000.01"], course_name="", semester_range=None)
        self.assertIsNone(row)
        self.assertEqual(detail, "ambiguous")

    def test_unique_name_and_semester_match_is_accepted(self):
        row, detail = match_exam_row(self.rows, course_codes=[], course_name="人工智能基础", semester_range=self.semester)
        self.assertEqual(detail, "matched")
        self.assertEqual(row["course_code"], "ICSE30021h.01")

    def test_name_without_semester_range_is_rejected(self):
        row, detail = match_exam_row(self.rows, course_codes=[], course_name="人工智能基础", semester_range=None)
        self.assertIsNone(row)
        self.assertEqual(detail, "no_match")

    def test_name_outside_semester_range_is_rejected(self):
        rows = parse_exam_arrangement(_page(_EXAM_ROW_TEMPLATE.format(date="2027-09-20", time="08:30~10:30")))
        row, detail = match_exam_row(rows, course_codes=[], course_name="人工智能基础", semester_range=self.semester)
        self.assertIsNone(row)
        self.assertEqual(detail, "no_match")


class ExamFetchTests(unittest.TestCase):
    def test_fetch_parses_rows_and_hits_reference_endpoint(self):
        vpn = _FakeVpn(_page(_EXAM_ROW_TEMPLATE.format(date=_upcoming_date(3), time="08:30~10:30")))
        rows = fetch_exam_rows(vpn, _STUDENT_ID)
        self.assertEqual(len(rows or []), 1)
        self.assertEqual(vpn.last_url, EXAM_ARRANGE_URL.format(student_id=_STUDENT_ID))
        self.assertNotIn(_STUDENT_ID, json.dumps(rows))

    def test_fetch_fails_closed_without_session_or_payload(self):
        self.assertIsNone(fetch_exam_rows(_FakeVpn("", status=401), _STUDENT_ID))
        self.assertIsNone(fetch_exam_rows(_FakeVpn(_page(_EXAM_ROW_TEMPLATE.format(date="x", time="y")), status=500), _STUDENT_ID))
        self.assertIsNone(fetch_exam_rows(_FakeVpn(error=RuntimeError("boom")), _STUDENT_ID))
        self.assertIsNone(fetch_exam_rows(_FakeVpn(), ""))
        self.assertIsNone(fetch_exam_rows(None, _STUDENT_ID))


class _ScriptedSession:
    """WebVPN 会话替身：按剧本回放响应/异常并记录调用（零真实网络）。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if not self.script:
            raise AssertionError("unexpected extra request")
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class _SessionResponse:
    def __init__(self, text="", *, status=200, url=""):
        self.text = text
        self.status_code = status
        self.url = url
        self.headers = {}


class SafeGetRecoveryTests(unittest.TestCase):
    """P3.1 附件：client().vpn 的只读安全 GET 在共享边界上获得恰一次
    已验证恢复；终止性/无钩子路径保持既有 fail-closed。"""

    PORTAL_LOGIN_URL = "https://webvpn.fudan.edu.cn/login"

    def _vpn(self, script):
        from src.api.webvpn import WebVPNSession

        vpn = WebVPNSession()
        vpn.session = _ScriptedSession(script)
        vpn.logged_in = True
        return vpn

    def _exam_response(self):
        page = _page(_EXAM_ROW_TEMPLATE.format(date=_upcoming_date(3), time="08:30~10:30"))
        return _SessionResponse(page, url="https://webvpn.fudan.edu.cn/https/7764wrapped")

    def test_transport_failure_retries_once_on_verified_epoch(self):
        vpn = self._vpn([requests.ConnectionError("synthetic flap"), self._exam_response()])
        recovery_calls = []
        vpn.safe_get_recovery = lambda: (recovery_calls.append(1), vpn)[1]
        rows = fetch_exam_rows(vpn, _STUDENT_ID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(vpn.session.calls), 2, "恰一次重试")
        self.assertEqual(len(recovery_calls), 1, "恢复钩子恰调用一次")

    def test_portal_login_response_rebuilds_and_retries_on_new_vpn(self):
        stale = self._vpn([_SessionResponse("expired", url=self.PORTAL_LOGIN_URL)])
        fresh = self._vpn([self._exam_response()])
        stale.safe_get_recovery = lambda: fresh
        rows = fetch_exam_rows(stale, _STUDENT_ID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(stale.session.calls), 1, "过期会话本身不空转重试")
        self.assertEqual(len(fresh.session.calls), 1, "重建后的会话恰重试一次")

    def test_same_epoch_stale_response_degrades_without_pointless_retry(self):
        stale = self._vpn([_SessionResponse("expired", url=self.PORTAL_LOGIN_URL)])
        stale.safe_get_recovery = lambda: stale
        rows = fetch_exam_rows(stale, _STUDENT_ID)
        self.assertEqual(rows, [])
        self.assertEqual(len(stale.session.calls), 1, "纪元未重建绝不空转重试")

    def test_missing_or_failed_hook_keeps_fail_closed(self):
        vpn = self._vpn([requests.ConnectionError("synthetic outage")])
        self.assertIsNone(fetch_exam_rows(vpn, _STUDENT_ID))
        self.assertEqual(len(vpn.session.calls), 1, "无钩子绝不重试")
        failed = self._vpn([requests.ConnectionError("synthetic outage")])
        failed.safe_get_recovery = lambda: None
        self.assertIsNone(fetch_exam_rows(failed, _STUDENT_ID))
        self.assertEqual(len(failed.session.calls), 1, "恢复失败绝不重试")

    def test_stale_response_without_hook_is_returned_as_is(self):
        vpn = self._vpn([_SessionResponse("expired", url=self.PORTAL_LOGIN_URL)])
        rows = fetch_exam_rows(vpn, _STUDENT_ID)
        self.assertEqual(rows, [])
        self.assertEqual(len(vpn.session.calls), 1)

    def test_allowlist_unchanged_no_request_or_recovery_for_foreign_hosts(self):
        vpn = self._vpn([])
        vpn.safe_get_recovery = lambda: vpn
        with self.assertRaisesRegex(ValueError, "not allowed"):
            vpn.get_allowed("https://example.com/exam")
        self.assertEqual(vpn.session.calls, [], "非许可主机零请求零恢复")

    def test_exam_rows_never_carry_the_student_id(self):
        vpn = self._vpn([requests.ConnectionError("x"), self._exam_response()])
        vpn.safe_get_recovery = lambda: vpn
        rows = fetch_exam_rows(vpn, _STUDENT_ID)
        self.assertTrue(rows)
        self.assertNotIn(_STUDENT_ID, json.dumps(rows, ensure_ascii=False))


class ReviewPlanCompatTests(unittest.TestCase):
    def test_legacy_rows_gain_honest_additive_fields_and_keep_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            ensure_student_feature_schema(path)
            future = datetime.now(SHANGHAI).timestamp() + 72 * 3600
            plan = save_review_plan(
                path, title="Legacy", exam_at=future, available_minutes=60,
                scope={"course_id": "course-1", "sub_id": "sub-1"},
                steps=[{"kind": "watch", "title": "章节", "minutes": 20, "reason": "章节重点"}],
            )
            listed = list_review_plans(path)[0]
            self.assertEqual(listed["plan_id"], plan["plan_id"])
            self.assertEqual(listed["exam_at"], future)
            self.assertEqual(listed["scope"]["course_id"], "course-1")
            context = listed["exam_context"]
            self.assertEqual(context["exam_source"], "user_confirmed")
            self.assertEqual(context["exam_precision"], "datetime")
            self.assertEqual(context["exam_state"], "active")
            self.assertEqual(listed["strategy"], "coverage")
            self.assertIsNone(listed["daily_minutes"])
            self.assertEqual(listed["course_scope"], ["course-1"])

    def test_stored_fudan_context_round_trips_and_refreshes_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            ensure_student_feature_schema(path)
            stored = {
                "exam_at": "2020-01-04T08:30:00+08:00",
                "exam_source": "fudan_jwgl",
                "exam_precision": "datetime",
                "exam_state": "active",
                "deadline_context": "active",
                "detail": "matched",
            }
            save_review_plan(
                path, title="Exam", exam_at=0, available_minutes=60,
                scope={"course_id": "course-1", "sub_id": "sub-1"},
                steps=[], exam_context=stored, strategy="wrong_first",
                daily_minutes=45, course_scope=["course-1"],
            )
            listed = list_review_plans(path)[0]
            self.assertEqual(listed["exam_context"]["exam_source"], "fudan_jwgl")
            self.assertEqual(listed["exam_context"]["exam_state"], "passed")
            self.assertEqual(listed["strategy"], "wrong_first")
            self.assertEqual(listed["daily_minutes"], 45)
            self.assertEqual(listed["course_scope"], ["course-1"])


class PlannerStrategyTests(unittest.TestCase):
    def _steps(self, **kwargs):
        base = dict(
            chapters=[{"chapter_id": "C1", "title": "重点章节", "start_ms": 10_000, "end_ms": 20_000}],
            quiz_items=[],
            # 同源纪律：产品 build_review_steps 内部读 time.time()，参考“现在”必须
            # 用同一时钟源构造，且落在 3 天桶内部（71h）而非 72h 整点，抗双钟路径
            # 读差（约 1 ulp ≈ 0.24μs，可将 exam_lead 顶过 72h 整点使 ceil 翻 4）。
            exam_at=time.time() + 71 * 3600,
            available_minutes=60,
        )
        base.update(kwargs)
        return build_review_steps(**base)

    def test_every_step_carries_frozen_additive_shape(self):
        quiz_text = "被引原文内容用于测验。"
        quiz = {
            "quiz_id": "quiz-1", "course_id": "course-1", "sub_id": "sub-1", "question": "Q",
            "evidence": {"start_ms": 5_000, "end_ms": 8_000, "text": quiz_text},
        }
        steps = self._steps(
            quiz_items=[quiz], course_id="course-1", sub_id="sub-1",
            key_moments=[{"id": "unit:abc123def456", "title": "关键页", "time": {"start_ms": 1_000, "end_ms": 4_000}}],
        )
        self.assertTrue(steps)
        allowed_reasons = {
            "考试临近的章节重点", "未观看章节", "章节重点", "错题优先", "考点复习",
            "未作答题目", "关键时点回顾",
        }
        for step in steps:
            self.assertIn(step["reason"], allowed_reasons)
            self.assertEqual(step["status"], "planned")
            self.assertEqual(step["estimated_minutes"], step["minutes"])
            self.assertEqual(step["course_id"], "course-1")
            self.assertEqual(step["sub_id"], "sub-1")
            self.assertIn("evidence_id", step)
        self.assertNotIn("必考", json.dumps(steps, ensure_ascii=False))
        self.assertNotIn("押题", json.dumps(steps, ensure_ascii=False))

    def test_wrong_first_puts_wrong_quizzes_ahead_of_chapters(self):
        quiz = {"quiz_id": "quiz-9", "question": "Q", "evidence": {"start_ms": 5_000}}
        stats = {"quiz-9": {"attempts": 2, "wrong": 2, "last_correct": 0}}
        steps = self._steps(quiz_items=[quiz], attempt_stats=stats, strategy="wrong_first")
        self.assertEqual(steps[0]["kind"], "quiz")
        self.assertEqual(steps[0]["reason"], "错题优先")

    def test_weak_first_prioritizes_unanswered_quizzes(self):
        answered = {"quiz-id-ok": {"attempts": 1, "wrong": 0, "last_correct": 1}}
        quiz = {"quiz_id": "quiz-new", "question": "Q", "evidence": {"start_ms": 5_000}}
        steps = self._steps(quiz_items=[quiz], attempt_stats=answered, strategy="weak_first")
        self.assertEqual(steps[0]["kind"], "quiz")
        self.assertEqual(steps[0]["reason"], "未作答题目")

    def test_mixed_interleaves_chapter_and_weak_quiz(self):
        chapters = [
            {"chapter_id": f"C{i}", "title": f"章节{i}", "start_ms": i * 10_000, "end_ms": i * 10_000 + 9_000}
            for i in range(1, 4)
        ]
        quiz = {"quiz_id": "quiz-w", "question": "Q", "evidence": {"start_ms": 1_000}}
        stats = {"quiz-w": {"attempts": 1, "wrong": 1, "last_correct": 0}}
        steps = self._steps(chapters=chapters, quiz_items=[quiz], attempt_stats=stats, strategy="mixed")
        self.assertEqual([step["kind"] for step in steps[:2]], ["watch", "quiz"])

    def test_key_moments_require_contract_unit_identity(self):
        good = [{"id": "unit:abc123def456", "title": "关键页", "time": {"start_ms": 1_000, "end_ms": 4_000}}]
        bad = [{"id": "unit:ZZZZ", "title": "坏身份", "time": {"start_ms": 9_000, "end_ms": 9_500}}]
        steps = self._steps(chapters=[], key_moments=good + bad)
        moments = [step for step in steps if step["kind"] == "key_moment"]
        self.assertEqual(len(moments), 1)
        self.assertEqual(moments[0]["evidence_id"], "unit:abc123def456")
        self.assertEqual(moments[0]["evidence"]["source"], "lecture_ir")

    def test_daily_minutes_clamps_budget_by_days_remaining(self):
        chapters = [
            {"chapter_id": f"C{i}", "title": f"章节{i}", "start_ms": i * 3_600_000, "end_ms": i * 3_600_000 + 3_600_000}
            for i in range(1, 7)
        ]
        unclamped = self._steps(chapters=chapters, available_minutes=600)
        clamped = self._steps(chapters=chapters, available_minutes=600, daily_minutes=30)
        total = lambda steps: sum(step["minutes"] for step in steps)
        # 六章 × 25 分钟 = 150；exam_at 锚定 71h → ceil = 3 天 × 30 分钟 = 90 的预算上限。
        self.assertEqual(total(unclamped), 150)
        self.assertEqual(total(clamped), 90)

    def test_unknown_strategy_falls_back_to_coverage_order(self):
        steps = self._steps(strategy="押题冲刺")
        default = self._steps()
        self.assertEqual([step["kind"] for step in steps], [step["kind"] for step in default])

    def test_plan_strategies_are_the_frozen_closed_set(self):
        self.assertEqual(PLAN_STRATEGIES, ("coverage", "weak_first", "wrong_first", "mixed"))


class LectureIrImportTests(unittest.TestCase):
    def _view(self) -> dict:
        return {
            "contract": "evidence.v1",
            "sections": [{"id": "unit:aaaaaaaa0001", "kind": "section", "title": "第一章",
                          "time": {"start_ms": 0, "end_ms": 1000}, "spans": [], "content": None}],
            "knowledge_units": [],
            "key_moments": [{"id": "unit:bbbbbbbb0002", "kind": "key_moment", "title": None,
                             "time": {"start_ms": 500, "end_ms": 900}, "spans": [], "content": None}],
        }

    def test_valid_view_imports_and_is_findable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LearningStore(Path(tmp) / "learning.db")
            self.assertTrue(store.import_lecture_ir(
                course_id="course-1", sub_id="sub-1", input_hash="a" * 64, view=self._view(),
            ))
            artifact = store.find_ai_artifact("sub-1", "lecture_ir")
            self.assertIsNotNone(artifact)
            self.assertEqual(artifact["status"], "ready")
            self.assertEqual(artifact["content"]["contract"], "evidence.v1")
            self.assertEqual(len(artifact["content"]["key_moments"]), 1)

    def test_malformed_views_store_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LearningStore(Path(tmp) / "learning.db")
            cases = [
                None,
                {"contract": "evidence.v2", "sections": [], "knowledge_units": [], "key_moments": []},
                {"contract": "evidence.v1", "sections": "no", "knowledge_units": [], "key_moments": []},
            ]
            for view in cases:
                self.assertFalse(store.import_lecture_ir(
                    course_id="course-1", sub_id="sub-1", input_hash="a" * 64, view=view,
                ))
            self.assertIsNone(store.find_ai_artifact("sub-1", "lecture_ir"))


class _StubApplication:
    """Minimal adapter exercising the real review-plan implementation."""

    def __init__(self, directory: Path, vpn=None, auth_state: str = "action_required",
                 student_id: str = "", timetable_snapshot: dict | None = None):
        self.learning_store = LearningStore(directory / "learning.db")
        ensure_student_feature_schema(directory / "learning.db")
        self.catalog_repository = SimpleNamespace(courses_for_ids=lambda ids: [
            {"course_id": "course-1", "title": "人工智能基础"}])
        self.timetable = SimpleNamespace(snapshot=lambda: timetable_snapshot or {
            "selected_semester": {"start_date": "2026-09-01"},
            "courses": [{"catalog_link": {"state": "linked", "course_id": "course-1"},
                         "course_code": "ICSE30021h.01"}],
        })
        self.authentication_snapshot = lambda: {"state": auth_state}
        self._credentials = {"student_id": student_id}
        self._timetable_vpn = lambda: vpn

    def create_impl(self, **kwargs):
        from src.application import CourseLensApplication
        return CourseLensApplication._create_review_plan_impl(self, **kwargs)

    def resolve_context(self, course_ids: list[str]):
        from src.application import CourseLensApplication
        return CourseLensApplication._resolve_exam_context(self, scope_course_ids=course_ids)

    def _resolve_course_identity(self, course_ids: list[str]):
        from src.application import CourseLensApplication
        return CourseLensApplication._resolve_course_identity(self, course_ids)

    def _resolve_exam_context(self, *, scope_course_ids: list[str]):
        return self.resolve_context(scope_course_ids)


class ReviewPlanFailClosedTests(unittest.TestCase):
    def _vpn_with_active_exam(self) -> _FakeVpn:
        page = _page(_EXAM_ROW_TEMPLATE.format(date=_upcoming_date(3), time="08:30~10:30"))
        return _FakeVpn(page)

    def test_active_user_confirmed_exam_requires_explicit_course_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _StubApplication(Path(tmp), auth_state="action_required")
            soon = datetime.now(SHANGHAI).timestamp() + 72 * 3600
            with self.assertRaises(ReviewPlanValidationError) as caught:
                app.create_impl(title="Exam", exam_at=soon, available_minutes=60,
                                daily_minutes=45, strategy="weak_first")
            self.assertEqual(caught.exception.code, "review_scope_required")

    def test_empty_scope_keeps_fetch_skipped_and_ordinary_plan_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _StubApplication(
                Path(tmp), vpn=self._vpn_with_active_exam(), auth_state="ready", student_id=_STUDENT_ID,
            )
            plan = app.create_impl(title="Ordinary", exam_at=0, available_minutes=30)
            self.assertEqual(plan["exam_context"]["exam_state"], "unavailable")
            self.assertEqual(plan["exam_context"]["detail"], "no_match")

    def test_active_fudan_context_plans_with_scope_and_additive_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _StubApplication(
                Path(tmp), vpn=self._vpn_with_active_exam(), auth_state="ready", student_id=_STUDENT_ID,
            )
            plan = app.create_impl(
                title="Exam", exam_at=0, available_minutes=60, course_id="course-1", sub_id="sub-1",
                daily_minutes=45, strategy="weak_first", course_scope=["course-1"],
            )
            context = plan["exam_context"]
            self.assertEqual(context["exam_source"], "fudan_jwgl")
            self.assertEqual(context["exam_state"], "active")
            self.assertEqual(context["deadline_context"], "active")
            self.assertEqual(plan["strategy"], "weak_first")
            self.assertEqual(plan["daily_minutes"], 45)
            self.assertEqual(plan["course_scope"], ["course-1"])
            self.assertNotIn(_STUDENT_ID, json.dumps(plan, ensure_ascii=False, default=str))

    def test_no_session_degrades_to_unavailable_and_ordinary_review_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _StubApplication(Path(tmp), vpn=None, auth_state="action_required")
            context = app.resolve_context(["course-1"])
            self.assertEqual(context["exam_state"], "unavailable")
            self.assertEqual(context["detail"], "session_unavailable")
            plan = app.create_impl(title="Ordinary", exam_at=0, available_minutes=30)
            self.assertEqual(plan["exam_context"]["exam_state"], "unavailable")
            self.assertIsNone(plan["course_scope"])

    def test_fetch_failure_and_ambiguity_fail_to_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _StubApplication(Path(tmp), vpn=_FakeVpn(status=503), auth_state="ready", student_id=_STUDENT_ID)
            self.assertEqual(app.resolve_context(["course-1"])["detail"], "fetch_failed")
            broken = _StubApplication(
                Path(tmp), vpn=_FakeVpn(error=RuntimeError("boom")), auth_state="ready", student_id=_STUDENT_ID,
            )
            self.assertEqual(broken.resolve_context(["course-1"])["detail"], "fetch_failed")

    def test_non_positive_minutes_and_invalid_strategy_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _StubApplication(Path(tmp), auth_state="action_required")
            with self.assertRaises(ReviewPlanValidationError) as minutes:
                app.create_impl(title="T", exam_at=0, available_minutes=60, course_id="course-1",
                                daily_minutes=0)
            self.assertEqual(minutes.exception.code, "review_minutes_invalid")
            with self.assertRaises(ReviewPlanValidationError) as strategy:
                app.create_impl(title="T", exam_at=0, available_minutes=60, course_id="course-1",
                                strategy="押题模式")
            self.assertEqual(strategy.exception.code, "review_strategy_invalid")

    def test_user_confirmed_exam_outside_window_keeps_ordinary_review_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = _StubApplication(Path(tmp), auth_state="action_required")
            far_future = datetime.now(SHANGHAI).timestamp() + (ACTIVE_WINDOW_HOURS + 48) * 3600
            plan = app.create_impl(title="远期", exam_at=far_future, available_minutes=60)
            self.assertEqual(plan["exam_context"]["exam_state"], "outside_window")
            self.assertEqual(plan["exam_context"]["exam_source"], "user_confirmed")


class _HttpSource:
    """Narrow source for the http_services adapter: review-plan routes only."""

    def __init__(self, app: _StubApplication):
        self._app = app

    def authentication_snapshot(self):
        return {"state": "ready"}

    def create_review_plan(self, **kwargs):
        return self._app.create_impl(**kwargs)

    def list_review_plans(self):
        return list_review_plans(self._app.learning_store.path)


class ReviewPlanHttpTests(unittest.TestCase):
    def setUp(self):
        from src.runtime.http_api import FrontendSessionRegistry, make_handler
        from tests.http_services import http_services
        from http.server import ThreadingHTTPServer

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = _StubApplication(Path(self.tmp.name), auth_state="action_required")
        self.sessions = FrontendSessionRegistry(lease_seconds=30, shutdown_grace_seconds=30, poll_seconds=1)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(http_services(_HttpSource(self.app)), Path(self.tmp.name) / "frontend", frontend_sessions=self.sessions),
        )
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.sessions.stop)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _post(self, body):
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        request = Request(
            f"{self.base}/api/v3/review-plans", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            response = urlopen(request)
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())
        with response:
            return response.status, json.loads(response.read())

    def test_post_review_plan_keeps_envelope_and_gains_additive_fields(self):
        future = datetime.now(SHANGHAI).timestamp() + 72 * 3600
        status, value = self._post({
            "title": "HTTP计划", "exam_at": future, "available_minutes": 60,
            "course_id": "course-1", "sub_id": "sub-1", "daily_minutes": 45,
            "strategy": "weak_first",
        })
        self.assertEqual(status, 200)
        plan = value["data"]["plan"]
        self.assertEqual(plan["title"], "HTTP计划")
        self.assertEqual(plan["exam_context"]["exam_source"], "user_confirmed")
        self.assertEqual(plan["exam_context"]["exam_state"], "active")
        self.assertEqual(plan["strategy"], "weak_first")
        self.assertEqual(plan["daily_minutes"], 45)
        self.assertEqual(plan["course_scope"], ["course-1"])

    def test_post_review_plan_maps_fail_closed_codes(self):
        # 无考试上下文的普通计划：exam_at=0 走来源解析，无课程范围仍可创建。
        status, value = self._post({"title": "T", "exam_at": 0, "available_minutes": 60})
        self.assertEqual(status, 200)
        self.assertEqual(value["data"]["plan"]["exam_context"]["exam_state"], "unavailable")

        status, value = self._post({
            "title": "T", "exam_at": datetime.now(SHANGHAI).timestamp() + 3600,
            "available_minutes": 60, "strategy": "押题",
        })
        self.assertEqual(status, 400)
        self.assertEqual(value["error_code"], "review_strategy_invalid")

        status, value = self._post({
            "title": "T", "exam_at": datetime.now(SHANGHAI).timestamp() + 3600,
            "available_minutes": 60, "daily_minutes": -5,
        })
        self.assertEqual(status, 400)
        self.assertEqual(value["error_code"], "review_minutes_invalid")

        status, value = self._post({
            "title": "T", "exam_at": datetime.now(SHANGHAI).timestamp() + 3600,
            "available_minutes": 60,
        })
        self.assertEqual(status, 400)
        self.assertEqual(value["error_code"], "review_scope_required")

    def test_get_review_plans_returns_additive_plan_fields(self):
        from urllib.request import urlopen
        future = datetime.now(SHANGHAI).timestamp() + 72 * 3600
        status, _ = self._post({
            "title": "HTTP计划", "exam_at": future, "available_minutes": 60, "course_id": "course-1",
        })
        self.assertEqual(status, 200)
        with urlopen(f"{self.base}/api/v3/review-plans") as response:
            value = json.loads(response.read())
        plans = value["data"]["plans"]
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["exam_context"]["exam_source"], "user_confirmed")
        self.assertEqual(plans[0]["course_scope"], ["course-1"])


class QuizAttemptStatsTests(unittest.TestCase):
    def test_stats_reflect_latest_attempt(self):
        from src.runtime.student_features import ensure_student_feature_schema
        import sqlite3
        from contextlib import closing
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            ensure_student_feature_schema(path)
            with closing(sqlite3.connect(path)) as db:
                db.execute(
                    "INSERT INTO quiz_items(quiz_id,course_id,sub_id,question_type,question,answer,created_at)"
                    " VALUES('quiz-1','c','s','short_answer','Q','A',1)"
                )
                for quiz_id, correct in (("quiz-1", 0), ("quiz-1", 1)):
                    db.execute(
                        "INSERT INTO quiz_attempts(attempt_id,quiz_id,answer,correct,confidence,created_at)"
                        " VALUES(?,?,?,?,0,?)",
                        (f"att-{quiz_id}-{correct}", quiz_id, "", correct, 2),
                    )
                db.commit()
            stats = quiz_attempt_stats(path)
            self.assertEqual(stats["quiz-1"]["attempts"], 2)
            self.assertEqual(stats["quiz-1"]["last_correct"], 1)


if __name__ == "__main__":
    unittest.main()
