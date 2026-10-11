"""P13-B 联动（P13B-CONTRACT-1 PKG-A）合成钉：确认门聚合读、planner 考核步
注入、impl fail-open 接线。全部合成夹具，不入任何真实课程/考核数据。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.runtime.assessment_radar import confirmed_schedule_events, upsert_events
from src.runtime.learning_schema import ensure_assessment_schema
from src.runtime.learning_store import LearningStore
from src.runtime.student_features import (
    build_review_steps,
    ensure_student_feature_schema,
    list_review_plans,
)

DAY = 86400.0
FAR_FUTURE_EXAM = 4_000_000_000.0


def _seed_event(db: Path, *, course_id: str, status: str, due_at, title: str,
                category: str = "exam") -> None:
    upsert_events(SimpleNamespace(path=str(db)), [{
        "course_id": course_id, "category": category, "title": title,
        "title_norm": title, "due_at": due_at, "location": "",
        "source": "rule", "status": status, "first_seen_sub_id": "sub-1",
        "quote": f"老师提到{title}的安排",
    }])


def _assessment_events(*titles: str, course_id: str = "c-1") -> list[dict[str, object]]:
    return [
        {
            "event_id": f"ev-{index}", "course_id": course_id,
            "category": "exam", "title": title,
            "due_at": FAR_FUTURE_EXAM + index * DAY, "due_bucket": "synthetic",
            "location": "", "evidence": [f"{title}的安排"], "conflict_note": "",
        }
        for index, title in enumerate(titles, start=1)
    ]


class ConfirmedScheduleEventsTests(unittest.TestCase):
    """验收①：聚合函数闭集过滤（确认门/scope/日期/窗口/limit/升序/空 scope）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "learning.db"
        ensure_assessment_schema(self.db)
        self.now = time.time()
        _seed_event(self.db, course_id="c1", status="confirmed", due_at=self.now + 2 * DAY, title="期末考试")
        _seed_event(self.db, course_id="c1", status="active", due_at=self.now + 1 * DAY, title="随堂测")
        _seed_event(self.db, course_id="c1", status="unconfirmed", due_at=self.now + 1.5 * DAY, title="大作业")
        _seed_event(self.db, course_id="c1", status="dismissed", due_at=self.now + 3 * DAY, title="实验报告")
        _seed_event(self.db, course_id="c2", status="confirmed", due_at=self.now + 1 * DAY, title="他课考核")
        _seed_event(self.db, course_id="c1", status="confirmed", due_at="", title="答疑时间")
        _seed_event(self.db, course_id="c1", status="confirmed", due_at=self.now - DAY, title="已过考核")
        _seed_event(self.db, course_id="c1", status="confirmed", due_at=self.now + 40 * DAY, title="远期考核")

    def _scan(self, **kwargs):
        params = {"course_scope": ["c1"], "until_epoch": self.now + 10 * DAY, "now": self.now}
        params.update(kwargs)
        return confirmed_schedule_events(SimpleNamespace(path=str(self.db)), **params)

    def test_confirmed_gate_and_scope_filter(self):
        events = self._scan()
        self.assertEqual([event["title"] for event in events], ["期末考试"],
                         "仅 confirmed+scope 内+带日期+窗口内的事件联动")

    def test_second_in_window_event_enters_ascending(self):
        _seed_event(self.db, course_id="c1", status="confirmed",
                    due_at=self.now + 2 * DAY + 3600, title="补考")
        events = self._scan()
        self.assertEqual([event["title"] for event in events], ["期末考试", "补考"], "due_at 升序")

    def test_window_boundaries_are_inclusive(self):
        _seed_event(self.db, course_id="c1", status="confirmed",
                    due_at=self.now + 10 * DAY, title="压哨考核")
        events = self._scan()
        self.assertIn("压哨考核", [event["title"] for event in events], "due_at==until_epoch 含边界")
        self.assertIn("期末考试", [event["title"] for event in events], "due_at==now 含边界")

    def test_now_override_lets_past_event_reenter(self):
        events = self._scan(now=self.now - 2 * DAY)
        self.assertIn("已过考核", [event["title"] for event in events])

    def test_limit_defaults_to_six_and_caps_seed_overflow(self):
        for index in range(8):
            _seed_event(self.db, course_id="c1", status="confirmed",
                        due_at=self.now + (4 + index * 0.5) * DAY, title=f"批量考核{index}")
        self.assertEqual(len(self._scan()), 6, "默认 limit 6")
        self.assertEqual(len(self._scan(limit=2)), 2)

    def test_empty_scope_never_links(self):
        self.assertEqual(self._scan(course_scope=[]), [])
        self.assertEqual(self._scan(course_scope=["", "  "]), [])

    def test_row_shape_is_the_frozen_nine_fields(self):
        events = self._scan()
        self.assertEqual(sorted(events[0]), sorted([
            "event_id", "course_id", "category", "title", "due_at",
            "due_bucket", "location", "evidence", "conflict_note",
        ]))
        self.assertIsInstance(events[0]["due_at"], float)
        self.assertIsInstance(events[0]["evidence"], list)
        self.assertIsInstance(events[0]["due_bucket"], str)


class AssessmentStepInjectionTests(unittest.TestCase):
    """验收②③④：步子形状逐字段对冻结件2、恒最前+预算保底、无事件零增量。"""

    def test_step_shape_matches_frozen_contract_field_by_field(self):
        events = _assessment_events("期末考试")
        steps = build_review_steps(
            chapters=[{"title": "第一章", "start_seconds": 0}],
            quiz_items=[], exam_at=FAR_FUTURE_EXAM + 10 * DAY, available_minutes=60,
            course_id="c-1", sub_id="sub-9", assessment_events=events,
        )
        self.assertEqual(steps[0], {
            "order": 1, "kind": "assessment", "title": "期末考试",
            "minutes": 15, "estimated_minutes": 15, "status": "planned",
            "reason": "考核临近", "event_id": "ev-1",
            "evidence": {
                "source": "assessment_event", "event_id": "ev-1", "category": "exam",
                "due_at": FAR_FUTURE_EXAM + DAY, "due_bucket": "synthetic", "course_id": "c-1",
            },
            "evidence_id": None, "course_id": "c-1", "sub_id": "",
        }, "与 P13B-CONTRACT-1 冻结件2 逐字段一致（前端 fixture 同形状）")
        self.assertEqual(steps[1]["sub_id"], "sub-9", "其余步照走既有讲次回落")
        self.assertEqual(steps[1]["kind"], "watch")

    def test_assessment_steps_precede_every_strategy_branch(self):
        events = _assessment_events("第一门", "第二门")
        steps = build_review_steps(
            chapters=[{"title": "考试重点章", "start_seconds": 0}],
            quiz_items=[{"quiz_id": "q1", "question": "问题", "course_id": "c-1", "sub_id": "s1"}],
            key_moments=[{"id": "unit:0123456789ab", "title": "关键时点",
                          "time": {"start_ms": 0, "end_ms": 1000}}],
            exam_at=FAR_FUTURE_EXAM + 10 * DAY, available_minutes=120,
            strategy="mixed", course_id="c-1", sub_id="s1",
            assessment_events=events,
        )
        self.assertEqual([step["kind"] for step in steps[:2]], ["assessment", "assessment"])
        self.assertEqual([step["order"] for step in steps[:2]], [1, 2], "按传入序恒最前")
        self.assertTrue(all(step["kind"] != "assessment" for step in steps[2:]))
        self.assertEqual([step["event_id"] for step in steps[:2]], ["ev-1", "ev-2"])

    def test_budget_floor_keeps_nearest_single_step(self):
        events = _assessment_events("最近的考核", "第二近", "第三近")
        steps = build_review_steps(
            chapters=[], quiz_items=[], exam_at=FAR_FUTURE_EXAM, available_minutes=15,
            assessment_events=events,
        )
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["kind"], "assessment")
        self.assertEqual(steps[0]["title"], "最近的考核", "预算耗尽仍保底最临近 1 步")
        self.assertEqual(steps[0]["minutes"], 15)

    def test_minutes_follow_min_15_budget(self):
        events = _assessment_events("期末考试")
        now = time.time()
        steps = build_review_steps(
            chapters=[], quiz_items=[], exam_at=now + 2 * DAY + 100, available_minutes=60,
            daily_minutes=1, assessment_events=events,
        )
        self.assertEqual(steps[0]["minutes"], 3, "daily_minutes 压缩预算后 minutes=min(15,budget)")
        self.assertEqual(len(steps), 1)

    def test_no_events_is_zero_increment(self):
        def _plan(**extra):
            params = dict(
                chapters=[{"title": "普通章", "start_seconds": 0}, {"title": "考试重点", "start_seconds": 10}],
                quiz_items=[{"quiz_id": "q1", "question": "问题", "course_id": "c", "sub_id": "s"}],
                exam_at=FAR_FUTURE_EXAM, available_minutes=45, watched_seconds=5.0,
                wrong_quiz_ids={"q1"}, strategy="wrong_first", course_id="c", sub_id="s",
                key_moments=[{"id": "unit:0123456789ab", "title": "关键时点",
                              "time": {"start_ms": 0, "end_ms": 1000}}],
                attempt_stats={"q1": {"attempts": 2, "wrong": 1, "last_correct": 0}},
            )
            params.update(extra)
            return build_review_steps(**params)

        self.assertEqual(_plan(), _plan(assessment_events=None), "不传=零增量")
        self.assertEqual(_plan(), _plan(assessment_events=[]), "空列表=零增量")


class ReviewPlanLinkageImplTests(unittest.TestCase):
    """验收⑤⑥：impl 接线（fail-open / scope 空不联动）与 steps_json 落库往返。"""

    def _make_app(self, directory: Path, *, with_assessment_schema: bool = True):
        db = directory / "learning.db"
        store = LearningStore(db)
        ensure_student_feature_schema(db)
        if with_assessment_schema:
            ensure_assessment_schema(db)
        return SimpleNamespace(learning_store=store), db

    def _create(self, app, **kwargs):
        from src.application import CourseLensApplication
        params = dict(title="联动计划", exam_at=time.time() + 10 * DAY,
                      available_minutes=60, course_id="course-1", strategy="coverage")
        params.update(kwargs)
        return CourseLensApplication._create_review_plan_impl(app, **params)

    def test_impl_injects_confirmed_events_with_closed_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, db = self._make_app(Path(tmp))
            now = time.time()
            _seed_event(db, course_id="course-1", status="confirmed", due_at=now + 2 * DAY, title="期末考试")
            _seed_event(db, course_id="course-1", status="active", due_at=now + DAY, title="随堂测")
            _seed_event(db, course_id="course-2", status="confirmed", due_at=now + DAY, title="他课考核")
            plan = self._create(app)
            assessment_steps = [step for step in plan["steps"] if step["kind"] == "assessment"]
            expected = confirmed_schedule_events(
                app.learning_store, course_scope=["course-1"], until_epoch=plan["exam_at"])
            self.assertEqual([step["event_id"] for step in assessment_steps],
                             [event["event_id"] for event in expected],
                             "注入集合与聚合读逐 id 对拍（active/他课被双闸排除）")
            self.assertTrue(assessment_steps, "确认考核已进计划步骤")
            self.assertEqual(assessment_steps[0], plan["steps"][0], "考核步恒最前")
            self.assertTrue(all(step["sub_id"] == "" for step in assessment_steps))
            stored = list_review_plans(db)[0]
            stored_assessments = [step for step in stored["steps"] if step["kind"] == "assessment"]
            self.assertEqual(stored_assessments, assessment_steps, "steps_json 落库往返保形")

    def test_impl_fail_open_when_aggregator_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, db = self._make_app(Path(tmp))
            _seed_event(db, course_id="course-1", status="confirmed",
                        due_at=time.time() + 2 * DAY, title="期末考试")
            with mock.patch("src.application.confirmed_schedule_events",
                            side_effect=RuntimeError("boom")):
                plan = self._create(app)
            self.assertTrue(plan.get("plan_id"), "联动失败不阻断建计划")
            self.assertEqual([step["kind"] for step in plan["steps"]].count("assessment"), 0)

    def test_impl_fail_open_when_assessment_schema_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, _db = self._make_app(Path(tmp), with_assessment_schema=False)
            plan = self._create(app)
            self.assertTrue(plan.get("plan_id"), "缺 assessment 表时照常建计划")
            self.assertEqual([step["kind"] for step in plan["steps"]].count("assessment"), 0)

    def test_impl_empty_scope_never_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, db = self._make_app(Path(tmp))
            _seed_event(db, course_id="course-1", status="confirmed",
                        due_at=time.time() + 2 * DAY, title="期末考试")
            from src.runtime.exam_schedule import ACTIVE_WINDOW_HOURS
            far_future = time.time() + (ACTIVE_WINDOW_HOURS + 48) * 3600
            plan = self._create(app, course_id="", exam_at=far_future)
            self.assertEqual(plan["exam_context"]["exam_state"], "outside_window")
            self.assertEqual([step["kind"] for step in plan["steps"]].count("assessment"), 0,
                             "scope 空=零联动（course_id 缺省的桌面计划不挂考核步）")


if __name__ == "__main__":
    unittest.main()
