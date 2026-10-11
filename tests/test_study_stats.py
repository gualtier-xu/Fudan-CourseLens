"""STUDY-STATS-M1 钉测：study_daily_seconds 聚合往返/心跳幂等/概览读面/
掌握度五档诚实合成（含数据不足灰档与矛盾证据）/首跑引导态/查询帽耗时/擦除同族/
study 路由合同。

设计稿 §4.2-M1 验收判据：合成壳聚合往返绿；心跳幂等；10 万行级历史下概览
<50ms（查询帽=聚合查询+occurred_at 索引范围扫描）；off 开关零采集由客户端
insight 开关门（player-core 同源）保证——服务端只收合法跳，畸形跳
recorded=False 不落行（本文件钉死）。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from src.runtime.flashcards import ensure_flashcard_schema
from src.runtime.http_api import make_handler
from src.runtime.learning_schema import ensure_assessment_schema, initialize_learning_schema
from src.runtime.student_features import ensure_student_feature_schema
from src.runtime.study_stats import (
    DAILY_SECONDS_CAP,
    STUDY_STATS_VERSION,
    clear_study_daily_seconds,
    ensure_study_stats_schema,
    record_study_heartbeat,
    study_overview,
)
from tests.http_services import http_services

# 固定钟：2026-10-07（周三）14:00 本地时间。
NOW = time.mktime((2026, 10, 7, 14, 0, 0, 0, 0, -1))
TODAY = date.fromtimestamp(NOW)
MONDAY = TODAY - timedelta(days=TODAY.weekday())


def _iso(day: date) -> str:
    return day.isoformat()


def _local_midnight(day: date) -> float:
    return time.mktime((day.year, day.month, day.day, 0, 30, 0, 0, 0, -1))


class StudyStatsTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "learning.db"
        # 与 boot ensure 链同序：student_features 先建 watch_events，统计索引随后补建。
        ensure_student_feature_schema(self.db)
        ensure_study_stats_schema(self.db)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        return conn

    def _heartbeat(self, seconds=30, course_id="course-1", sub_id="lecture-1", now=NOW):
        return record_study_heartbeat(
            self.db, course_id=course_id, sub_id=sub_id, seconds=seconds, now=now,
        )


class SchemaAndHeartbeatTests(StudyStatsTestBase):
    def test_schema_ensure_is_idempotent(self):
        ensure_study_stats_schema(self.db)
        ensure_study_stats_schema(self.db)
        with self._conn() as conn:
            rows = conn.execute("SELECT COUNT(*) FROM study_daily_seconds").fetchone()
        self.assertEqual(int(rows[0]), 0)

    def test_heartbeat_accumulates_same_key_without_duplicate_rows(self):
        self._heartbeat(seconds=30)
        self._heartbeat(seconds=30)
        self._heartbeat(seconds=30, now=NOW + 60)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT local_date,course_id,sub_id,active_seconds FROM study_daily_seconds"
            ).fetchall()
        self.assertEqual(len(rows), 1, "同 (日,讲) 键必须只有一行（UPSERT 累加，绝不重复行）")
        self.assertEqual(rows[0][0], _iso(TODAY))
        self.assertEqual(rows[0][2], "lecture-1")
        self.assertEqual(int(rows[0][3]), 90)

    def test_heartbeat_separates_days_and_lectures(self):
        self._heartbeat(seconds=30)
        self._heartbeat(seconds=30, sub_id="lecture-2")
        self._heartbeat(seconds=30, now=NOW + 86_400)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT sub_id,active_seconds,local_date FROM study_daily_seconds"
            ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(int(row[1]) for row in rows), 90)

    def test_heartbeat_rejects_invalid_jumps_without_rows(self):
        for seconds in (0, -5, 500, "abc", None):
            value = self._heartbeat(seconds=seconds)
            self.assertFalse(value["recorded"], f"seconds={seconds!r} 应被闭集拒绝")
            self.assertEqual(value["reason"], "study_heartbeat_invalid")
        for kwargs in ({"sub_id": ""}, {"course_id": ""}):
            self.assertFalse(self._heartbeat(**kwargs)["recorded"])
        with self._conn() as conn:
            rows = conn.execute("SELECT COUNT(*) FROM study_daily_seconds").fetchone()
        self.assertEqual(int(rows[0]), 0)

    def test_heartbeat_daily_cap_bounds_dirty_data(self):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO study_daily_seconds(local_date,course_id,sub_id,active_seconds,updated_at)"
                " VALUES(?,?,?,?,?)",
                (_iso(TODAY), "course-1", "lecture-1", DAILY_SECONDS_CAP - 10, NOW),
            )
            conn.commit()
        value = self._heartbeat(seconds=30)
        self.assertTrue(value["recorded"])
        self.assertEqual(int(value["active_seconds"]), DAILY_SECONDS_CAP)

    def test_clear_erasable_full_and_by_course(self):
        self._heartbeat(course_id="course-1", sub_id="lecture-1")
        self._heartbeat(course_id="course-2", sub_id="lecture-9")
        self.assertEqual(clear_study_daily_seconds(self.db, course_id="course-1"), 1)
        self.assertEqual(clear_study_daily_seconds(self.db), 1)
        self.assertEqual(clear_study_daily_seconds(self.db), 0)


class OverviewTests(StudyStatsTestBase):
    def test_first_run_guidance_on_fresh_db(self):
        payload = study_overview(self.db, now=NOW)
        self.assertTrue(payload["first_run"])
        self.assertEqual(payload["view"], "study_overview")
        self.assertEqual(payload["method"], STUDY_STATS_VERSION)
        self.assertEqual(len(payload["week"]["days"]), 7)
        self.assertEqual(payload["week"]["days"][0]["date"], _iso(MONDAY), "周条形从周一起")

    def test_week_buckets_days_and_completion(self):
        monday_morning = _local_midnight(MONDAY)
        # 秒数种子直插（心跳闭集 ≤120s 是采集合同，本钉测的是聚合面）。
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO study_daily_seconds(local_date,course_id,sub_id,active_seconds,updated_at)"
                " VALUES(?,?,?,?,?)",
                [
                    (_iso(MONDAY), "course-1", "lecture-1", 600, monday_morning),
                    (_iso(MONDAY), "course-1", "lecture-2", 300, monday_morning),
                    (_iso(TODAY), "course-1", "lecture-1", 120, NOW),
                ],
            )
            conn.executemany(
                "INSERT INTO watch_events(event_id,course_id,sub_id,event,position_ms,playback_rate,occurred_at)"
                " VALUES(?,?,?,?,?,?,?)",
                [
                    ("e1", "course-1", "lecture-1", "pause", 1000, 1.0, monday_morning + 60),
                    ("e2", "course-1", "lecture-1", "replay", 2000, 1.0, monday_morning + 120),
                ],
            )
            initialize_learning_schema(conn)
            conn.executemany(
                "INSERT OR REPLACE INTO watch_progress(sub_id,course_id,position_ms,duration_ms,completed,playback_rate,updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                [
                    ("lecture-1", "course-1", 600_000, 1_000_000, 0, 1.0, monday_morning),
                    ("lecture-2", "course-1", 750_000, 1_000_000, 0, 1.0, monday_morning),
                ],
            )
            conn.commit()
        payload = study_overview(self.db, now=NOW)
        week = payload["week"]
        self.assertFalse(payload["first_run"])
        days = {day["date"]: day for day in week["days"]}
        self.assertEqual(days[_iso(MONDAY)]["seconds"], 900)
        self.assertEqual(days[_iso(MONDAY)]["interactions"], 2)
        self.assertTrue(days[_iso(MONDAY)]["active"])
        self.assertEqual(days[_iso(TODAY)]["seconds"], 120)
        self.assertFalse(days[_iso(MONDAY + timedelta(days=5))]["active"])
        self.assertEqual(week["study_days"], 2)
        self.assertEqual(week["course_count"], 1)
        self.assertEqual(week["avg_completion_percent"], 68)  # (60%+75%)/2=67.5
        self.assertEqual(week["minutes_total"], 17)  # (900+120)s

    def test_today_is_real_local_date_not_week_tail(self):
        # STUDY-STATS-GFIX-1 F2：payload.today 必须是真实本地日（NOW=周三），
        # 不得恒落本周日（week 末位）——旧恒真条件 `week[-1] if 本地日 in week`
        # 使 today 恒=周日：高亮错格、前端 future 态永不可渲染。
        payload = study_overview(self.db, now=NOW)
        self.assertEqual(payload["today"], _iso(TODAY))
        self.assertEqual(payload["today"], "2026-10-07")
        self.assertNotEqual(payload["today"], _iso(MONDAY + timedelta(days=6)))

    def test_overview_cache_invalidated_by_heartbeat(self):
        self.assertTrue(study_overview(self.db)["first_run"])
        self._heartbeat(seconds=30)
        second = study_overview(self.db)
        self.assertFalse(second["first_run"])
        self.assertEqual(second["week"]["study_days"], 1)

    def test_due_rows_flashcards_and_top_course(self):
        ensure_flashcard_schema(self.db)
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO flash_cards(card_id,course_id,sub_id,card_type,front,back,state,due_at,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    ("c1", "course-1", "lecture-1", "short_answer", "f", "b", "review", NOW - 100, NOW),
                    ("c2", "course-1", "lecture-1", "short_answer", "f", "b", "review", NOW - 90, NOW),
                    ("c3", "course-2", "lecture-2", "short_answer", "f", "b", "review", NOW - 50, NOW),
                    ("c4", "course-1", "lecture-1", "short_answer", "f", "b", "new", 0.0, NOW),
                ],
            )
            conn.commit()
        self._heartbeat(seconds=30)
        payload = study_overview(self.db, now=NOW)
        self.assertEqual(payload["due"]["flashcards_due"], 3, "new 卡不计到期（同 flashcard_deck 口径）")
        self.assertEqual(payload["due"]["flashcards_top_course_id"], "course-1")

    def test_next_exam_prefers_nearest_of_assessment_and_plan(self):
        ensure_assessment_schema(self.db)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO assessment_events(event_id,course_id,category,title,title_norm,due_at,source,status,"
                "first_seen_sub_id,last_seen_sub_id,created_at,updated_at)"
                " VALUES('a1','course-1','exam','期中考试','期中考试',?,'rule','active','lecture-1','lecture-1',?,?)",
                (NOW + 5 * 86_400, NOW, NOW),
            )
            conn.execute(
                "INSERT INTO review_plans(plan_id,title,exam_at,available_minutes,scope_json,steps_json,updated_at)"
                " VALUES('p1','期末复习计划',?,?,?,?,?)",
                (NOW + 2 * 86_400, 60, "{}", "[]", NOW),
            )
            conn.commit()
        exam = study_overview(self.db, now=NOW)["due"]["next_exam"]
        self.assertIsNotNone(exam)
        self.assertEqual(exam["title"], "期末复习计划")
        self.assertEqual(exam["days_left"], 2)

    def test_next_exam_none_when_beyond_horizon_or_dismissed(self):
        ensure_assessment_schema(self.db)
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO assessment_events(event_id,course_id,category,title,title_norm,due_at,source,status,"
                "first_seen_sub_id,last_seen_sub_id,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("a1", "course-1", "exam", "远期考试", "远期考试", NOW + 40 * 86_400, "rule", "active", "lecture-1", "lecture-1", NOW, NOW),
                    ("a2", "course-1", "quiz", "已忽略测验", "已忽略测验", NOW + 86_400, "rule", "dismissed", "lecture-1", "lecture-1", NOW, NOW),
                ],
            )
            conn.commit()
        self.assertIsNone(study_overview(self.db, now=NOW)["due"]["next_exam"])


class MasteryTests(StudyStatsTestBase):
    def _seed_quiz(self, course_id, graded_correct, graded_wrong, ungraded=0):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO quiz_items(quiz_id,course_id,sub_id,question_type,question,answer,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (f"q-{course_id}-1", course_id, "lecture-1", "short_answer", "题干", "答案", NOW),
            )
            rows = [
                (f"a-{course_id}-c{index}", f"q-{course_id}-1", "", 1, 0, NOW)
                for index in range(graded_correct)
            ]
            rows += [
                (f"a-{course_id}-w{index}", f"q-{course_id}-1", "", 0, 0, NOW)
                for index in range(graded_wrong)
            ]
            rows += [
                (f"a-{course_id}-u{index}", f"q-{course_id}-1", "", None, 0, NOW)
                for index in range(ungraded)
            ]
            conn.executemany(
                "INSERT INTO quiz_attempts(attempt_id,quiz_id,answer,correct,confidence,created_at)"
                " VALUES(?,?,?,?,?,?)",
                rows,
            )
            conn.commit()

    def _seed_bookmarks(self, course_id, open_count):
        ensure_student_feature_schema(self.db)
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO bookmarks(bookmark_id,course_id,sub_id,start_ms,end_ms,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                [
                    (f"b-{course_id}-o{index}", course_id, "lecture-1", 0, 1000, NOW, NOW)
                    for index in range(open_count)
                ],
            )
            conn.commit()

    def _seed_flashcards(self, course_id, scheduled, due, reviewed=0):
        ensure_flashcard_schema(self.db)
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO flash_cards(card_id,course_id,sub_id,card_type,front,back,state,due_at,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        f"f-{course_id}-{index}", course_id, "lecture-1", "short_answer", "f", "b",
                        "review", NOW - 100 if index < due else NOW + 86_400, NOW,
                    )
                    for index in range(scheduled)
                ],
            )
            conn.executemany(
                "INSERT INTO flash_card_reviews(review_id,card_id,rating,reviewed_at) VALUES(?,?,?,?)",
                [
                    (f"r-{course_id}-{index}", f"f-{course_id}-{index}", 3, NOW - 200)
                    for index in range(reviewed)
                ],
            )
            conn.commit()

    def _mastery_by_course(self, payload):
        return {row["course_id"]: row for row in payload["mastery"]["courses"]}

    def test_weak_quiz_course_is_flagged_with_honest_counts(self):
        self._seed_quiz("course-1", graded_correct=1, graded_wrong=3)
        row = self._mastery_by_course(study_overview(self.db, now=NOW))["course-1"]
        self.assertEqual(row["tier"], "薄弱")
        self.assertFalse(row["gray"])
        self.assertIn("测验", row["evidence"])

    def test_contradiction_does_not_average(self):
        self._seed_quiz("course-1", graded_correct=10, graded_wrong=0)
        self._seed_bookmarks("course-1", open_count=3)
        row = self._mastery_by_course(study_overview(self.db, now=NOW))["course-1"]
        self.assertEqual(row["tier"], "待巩固", "测验高分×书签堆积=矛盾证据不平均")
        self.assertIn("测得不错", row["evidence"])
        self.assertIn("3 处", row["evidence"])

    def test_insufficient_data_is_gray_and_honest(self):
        self._seed_flashcards("course-1", scheduled=3, due=1, reviewed=1)  # 低于 FSRS 最小样本 5
        row = self._mastery_by_course(study_overview(self.db, now=NOW))["course-1"]
        self.assertEqual(row["tier"], "数据不足")
        self.assertTrue(row["gray"])
        self.assertIn("暂时看不出", row["evidence"])

    def test_solid_course_all_clear(self):
        self._seed_quiz("course-1", graded_correct=5, graded_wrong=0)
        self._seed_flashcards("course-1", scheduled=6, due=0, reviewed=6)
        payload = study_overview(self.db, now=NOW)
        self.assertTrue(payload["mastery"]["all_clear"])
        self.assertEqual(payload["mastery"]["courses"], [])

    def test_top3_cap_on_action_rows(self):
        for index in range(4):
            self._seed_quiz(f"course-{index}", graded_correct=0, graded_wrong=4)
        courses = study_overview(self.db, now=NOW)["mastery"]["courses"]
        self.assertEqual(len(courses), 3, "行动行最多 3 行")
        self.assertTrue(all(row["tier"] in ("薄弱", "待巩固") for row in courses))

    def test_gray_rows_fill_remaining_slots(self):
        self._seed_quiz("course-weak", graded_correct=0, graded_wrong=4)
        self._seed_flashcards("course-gray", scheduled=2, due=0, reviewed=1)
        courses = study_overview(self.db, now=NOW)["mastery"]["courses"]
        self.assertEqual([row["tier"] for row in courses], ["薄弱", "数据不足"])
        self.assertTrue(courses[1]["gray"])

    def test_ungraded_attempts_reported_not_mixed_into_accuracy(self):
        # 未判对错的题不入分子分母、只单报；该课仍凭弱分进 top3（扎实课走 all_clear 不列行）。
        self._seed_quiz("course-1", graded_correct=1, graded_wrong=2, ungraded=2)
        self._seed_bookmarks("course-1", open_count=1)
        with self._conn() as conn:
            conn.execute("UPDATE bookmarks SET resolution_status='resolved' WHERE course_id='course-1'")
            conn.commit()
        row = self._mastery_by_course(study_overview(self.db, now=NOW))["course-1"]
        self.assertEqual(row["tier"], "待巩固")
        self.assertIn("2 题还没判对错", row["evidence"])
        self.assertIn("偏低", row["evidence"])

    def test_due_backlog_lifts_solid_course_to_action_tier(self):
        self._seed_quiz("course-1", graded_correct=5, graded_wrong=0)
        self._seed_bookmarks("course-1", open_count=1)
        with self._conn() as conn:
            conn.execute(
                "UPDATE bookmarks SET resolution_status='resolved' WHERE course_id='course-1'"
            )
            conn.commit()
        self._seed_flashcards("course-1", scheduled=12, due=10, reviewed=12)
        payload = study_overview(self.db, now=NOW)
        row = self._mastery_by_course(payload)["course-1"]
        # 合成分 ≈0.708（本档常规=一般）；到期积压 10 张过线 → 抬到待巩固+积压证据句。
        self.assertGreaterEqual(row["score"], 0.7)
        self.assertEqual(row["tier"], "待巩固", "到期积压过线=直接行动信号，不低于待巩固")
        self.assertTrue(row["evidence"].startswith("到期闪卡积压"))


class MasteryFullModelTests(StudyStatsTestBase):
    """STUDY-STATS-M2-a 完整三路诚实合成钉测：时间衰减/单路标注/书签密度/
    FSRS 遗忘曲线×积压/日级显示帽（U1）/空建议句根除（P1）。"""

    def _mastery_rows(self):
        from src.runtime.study_stats import _mastery_courses

        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return {row["course_id"]: row for row in _mastery_courses(conn, NOW)}

    def _seed_attempt(self, attempt_id, course_id, correct, created_at):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO quiz_items(quiz_id,course_id,sub_id,question_type,question,answer,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (f"q-{course_id}", course_id, "lecture-1", "short_answer", "题干", "答案", NOW),
            )
            conn.execute(
                "INSERT INTO quiz_attempts(attempt_id,quiz_id,answer,correct,confidence,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (attempt_id, f"q-{course_id}", "", correct, 0, created_at),
            )
            conn.commit()

    def test_quiz_time_decay_weights_recent_attempts(self):
        # 旧错（28 天前，权重 0.25）+ 三新对（现在，权重 1.0×3）→ 3/3.25≈0.923「扎实」；
        # 平权旧口径会算出 0.75「一般」——时间衰减必须让近期作答更有发言权。
        self._seed_attempt("a-old", "course-1", 0, NOW - 28 * 86_400)
        self._seed_attempt("a-new1", "course-1", 1, NOW)
        self._seed_attempt("a-new2", "course-1", 1, NOW)
        self._seed_attempt("a-new3", "course-1", 1, NOW)
        row = self._mastery_rows()["course-1"]
        self.assertEqual(row["basis"], ["quiz"])
        self.assertTrue(row["partial"], "单路数据必须带 partial 标注")
        self.assertAlmostEqual(row["score"], 3.0 / 3.25, places=3)
        self.assertEqual(row["tier"], "扎实")
        # 镜像排列（旧对+三新错）必须显著更差：衰减方向性钉（平权两排列同为 0.75）。
        self._seed_attempt("a-old2", "course-2", 1, NOW - 28 * 86_400)
        self._seed_attempt("a-new4", "course-2", 0, NOW)
        self._seed_attempt("a-new5", "course-2", 0, NOW)
        self._seed_attempt("a-new6", "course-2", 0, NOW)
        mirrored = self._mastery_rows()["course-2"]
        self.assertAlmostEqual(mirrored["score"], 0.25 / 3.25, places=3)
        self.assertEqual(mirrored["tier"], "薄弱")

    def test_single_path_annotation_and_paths_n(self):
        self._seed_attempt("a-1", "course-1", 1, NOW)
        self._seed_attempt("a-2", "course-1", 1, NOW)
        self._seed_attempt("a-3", "course-1", 0, NOW)
        row = self._mastery_rows()["course-1"]
        self.assertEqual(row["basis"], ["quiz"])
        self.assertTrue(row["partial"])
        self.assertEqual(row["paths"]["quiz"]["n"], 3)
        self.assertAlmostEqual(row["paths"]["quiz"]["score"], 2 / 3, places=3)

    def test_bookmark_density_uses_signal_lectures(self):
        # 2 处未解决书签摊在 4 讲有观看信号 → 密度 0.5 → 得分 0.5；
        # M1 旧口径（open/total=2/2）会算出 0——分母必须是有信号的讲数。
        with self._conn() as conn:
            initialize_learning_schema(conn)
            conn.executemany(
                "INSERT OR REPLACE INTO watch_progress(sub_id,course_id,position_ms,duration_ms,completed,playback_rate,updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                [
                    (f"lecture-{index}", "course-1", 1, 1000, 0, 1.0, NOW)
                    for index in range(4)
                ],
            )
            conn.executemany(
                "INSERT INTO bookmarks(bookmark_id,course_id,sub_id,start_ms,end_ms,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                [
                    (f"b-{index}", "course-1", "lecture-1", 0, 1000, NOW, NOW)
                    for index in range(2)
                ],
            )
            conn.commit()
        row = self._mastery_rows()["course-1"]
        self.assertEqual(row["basis"], ["bookmark"])
        self.assertTrue(row["partial"])
        self.assertAlmostEqual(row["score"], 0.5, places=3)
        self.assertEqual(row["tier"], "待巩固")

    def test_fsrs_retrievability_times_backlog(self):
        # 6 张真实记忆态卡（stability=10，1 天前复习）：R≈0.9885/张；
        # 一半到期 → 积压率 0.5 → 路分≈0.494 → 待巩固。
        ensure_flashcard_schema(self.db)
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO flash_cards(card_id,course_id,sub_id,card_type,front,back,state,difficulty,"
                "stability,due_at,reps,last_review_at,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        f"f-{index}", "course-1", "lecture-1", "short_answer", "f", "b",
                        "review", 5.0, 10.0,
                        NOW - 100 if index < 3 else NOW + 5 * 86_400,
                        1, NOW - 86_400, NOW,
                    )
                    for index in range(6)
                ],
            )
            conn.commit()
        row = self._mastery_rows()["course-1"]
        self.assertIn("fsrs", row["basis"])
        self.assertTrue(row["partial"])
        self.assertGreater(row["paths"]["fsrs"]["score"], 0.45)
        self.assertLess(row["paths"]["fsrs"]["score"], 0.55)
        self.assertEqual(row["tier"], "待巩固")

    def test_fsrs_degenerate_cards_abstain_not_fabricated(self):
        # stability/last_review_at 全零的「已复习」卡=无真实记忆态（合成种子态），
        # FSRS 路必须 abstain（绝不拿 0 稳定度硬算「全忘了」假信号）。
        ensure_flashcard_schema(self.db)
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO flash_cards(card_id,course_id,sub_id,card_type,front,back,state,due_at,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (f"f-{index}", "course-1", "lecture-1", "short_answer", "f", "b", "review", NOW + 86_400, NOW)
                    for index in range(6)
                ],
            )
            conn.commit()
        row = self._mastery_rows()["course-1"]
        self.assertEqual(row["tier"], "数据不足")
        self.assertTrue(row["gray"])
        self.assertEqual(row["basis"], [])

    def test_multi_path_basis_sorted_by_weight(self):
        self._seed_attempt("a-m", "course-1", 1, NOW)
        self._seed_attempt("a-m2", "course-1", 1, NOW)
        self._seed_attempt("a-m3", "course-1", 1, NOW)
        ensure_flashcard_schema(self.db)
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO flash_cards(card_id,course_id,sub_id,card_type,front,back,state,difficulty,"
                "stability,due_at,reps,last_review_at,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (f"f-{index}", "course-1", "lecture-1", "short_answer", "f", "b", "review", 5.0, 10.0,
                     NOW + 86_400, 1, NOW - 86_400, NOW)
                    for index in range(5)
                ],
            )
            conn.commit()
        row = self._mastery_rows()["course-1"]
        self.assertEqual(row["basis"], ["quiz", "fsrs"], "basis 按权重降序（quiz 0.45 > fsrs 0.35）")
        self.assertFalse(row["partial"], "多路数据不算单路")
        self.assertEqual(set(row["paths"]), {"quiz", "fsrs"})

    def test_weights_constants_carried_in_payload(self):
        self._seed_attempt("a-w", "course-1", 1, NOW)
        payload = study_overview(self.db, now=NOW)
        self.assertEqual(payload["mastery"]["weights"], {"quiz": 0.45, "fsrs": 0.35, "bookmark": 0.20})

    def test_day_display_seconds_capped_at_one_day(self):
        # U1（GATES-1）：行级帽不约束同日合计——两行各 86400/600 → 日显示 86400 封顶，
        # 绝不出现「26 小时」。写入直插（绕过采集行帽）模拟历史脏数据。
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO study_daily_seconds(local_date,course_id,sub_id,active_seconds,updated_at)"
                " VALUES(?,?,?,?,?)",
                [
                    (_iso(TODAY), "course-1", "lecture-1", 86_400, NOW),
                    (_iso(TODAY), "course-1", "lecture-2", 600, NOW),
                ],
            )
            conn.commit()
        payload = study_overview(self.db, now=NOW)
        days = {day["date"]: day for day in payload["week"]["days"]}
        self.assertEqual(days[_iso(TODAY)]["seconds"], 86_400)
        self.assertEqual(payload["week"]["minutes_total"], 1_440)

    def test_no_empty_suggestion_when_bookmarks_all_resolved(self):
        # P1（GATES-1）：全解决书签不构成弱点——顺延次弱路/中性收束，
        # 绝不输出「0 处没听懂标记还没解决」空建议。
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO bookmarks(bookmark_id,course_id,sub_id,start_ms,end_ms,created_at,updated_at)"
                " VALUES('b-1','course-1','lecture-1',0,1000,?,?)",
                (NOW, NOW),
            )
            conn.execute("UPDATE bookmarks SET resolution_status='resolved' WHERE course_id='course-1'")
            conn.commit()
        row = self._mastery_rows()["course-1"]
        self.assertNotIn("0 处", row["evidence"])
        self.assertNotIn("没听懂标记还没解决", row["evidence"])
        self.assertEqual(row["tier"], "扎实")
        self.assertTrue(row["evidence"], "扎实档也必须有一句人话证据")


class DetailTests(StudyStatsTestBase):
    """STUDY-STATS-M2-b 展开层钉测：逐讲五列聚合对账/FSRS 7 日预测桶/
    纯观看课程灰档补入/课程完成度与周互动。"""

    def _detail(self):
        from src.runtime.study_stats import study_detail

        return study_detail(self.db, now=NOW)

    def _seed_signal_mix(self, course_id="course-1"):
        with self._conn() as conn:
            initialize_learning_schema(conn)
            # 讲 lecture-1：进度 60% + 2 次回放热点 + 1 处未解决书签 + 1 对 1 错测验
            # + 2 张卡（1 到期）；讲 lecture-2：仅 300 秒时长。
            conn.execute(
                "INSERT OR REPLACE INTO watch_progress(sub_id,course_id,position_ms,duration_ms,completed,playback_rate,updated_at)"
                " VALUES('lecture-1',?,600000,1000000,0,1.0,?)",
                (course_id, NOW),
            )
            conn.executemany(
                "INSERT INTO study_daily_seconds(local_date,course_id,sub_id,active_seconds,updated_at)"
                " VALUES(?,?,?,?,?)",
                [
                    (_iso(TODAY), course_id, "lecture-1", 600, NOW),
                    (_iso(TODAY), course_id, "lecture-2", 300, NOW),
                ],
            )
            conn.executemany(
                "INSERT INTO watch_events(event_id,course_id,sub_id,event,position_ms,playback_rate,occurred_at)"
                " VALUES(?,?,?,?,?,?,?)",
                [
                    ("e1", course_id, "lecture-1", "replay", 1000, 1.0, NOW - 60),
                    ("e2", course_id, "lecture-1", "seek_back", 2000, 1.0, NOW - 30),
                    ("e3", course_id, "lecture-1", "pause", 1500, 1.0, NOW - 10),
                ],
            )
            conn.execute(
                "INSERT INTO bookmarks(bookmark_id,course_id,sub_id,start_ms,end_ms,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (f"b-{course_id}", course_id, "lecture-1", 0, 1000, NOW, NOW),
            )
            conn.execute(
                "INSERT INTO quiz_items(quiz_id,course_id,sub_id,question_type,question,answer,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (f"q-{course_id}", course_id, "lecture-1", "short_answer", "题干", "答案", NOW),
            )
            conn.executemany(
                "INSERT INTO quiz_attempts(attempt_id,quiz_id,answer,correct,confidence,created_at)"
                " VALUES(?,?,?,?,?,?)",
                [
                    (f"a-{course_id}-1", f"q-{course_id}", "", 1, 0, NOW - 100),
                    (f"a-{course_id}-2", f"q-{course_id}", "", 0, 0, NOW - 50),
                    (f"a-{course_id}-3", f"q-{course_id}", "", None, 0, NOW - 10),
                ],
            )
            conn.commit()
        ensure_flashcard_schema(self.db)
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO flash_cards(card_id,course_id,sub_id,card_type,front,back,state,due_at,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (f"f-{course_id}-1", course_id, "lecture-1", "short_answer", "f", "b", "review", NOW - 100, NOW),
                    (f"f-{course_id}-2", course_id, "lecture-1", "short_answer", "f", "b", "new", 0.0, NOW),
                ],
            )
            conn.commit()

    def test_detail_is_empty_honest_on_fresh_db(self):
        detail = self._detail()
        self.assertEqual(detail["view"], "study_detail")
        self.assertEqual(detail["courses"], [])
        self.assertEqual(len(detail["forecast"]), 7)

    def test_detail_lecture_rows_aggregate_union(self):
        self._seed_signal_mix()
        detail = self._detail()
        course = next(item for item in detail["courses"] if item["course_id"] == "course-1")
        rows = {row["sub_id"]: row for row in course["lectures"]}
        first = rows["lecture-1"]
        self.assertEqual(first["percent"], 60)
        self.assertFalse(first["completed"])
        self.assertEqual(first["seconds"], 600)
        self.assertEqual(first["replays"], 2, "热点闭集=replay+seek_back，pause 不计")
        self.assertEqual(first["open_bookmarks"], 1)
        self.assertEqual(first["quiz_graded"], 2)
        self.assertEqual(first["quiz_correct"], 1)
        self.assertEqual(first["quiz_ungraded"], 1)
        self.assertIs(first["last_correct"], False, "last_correct=最近一条已判作答（错）")
        self.assertEqual(first["flashcards_total"], 2)
        self.assertEqual(first["flashcards_due"], 1, "new 卡不计到期")
        second = rows["lecture-2"]
        self.assertIsNone(second["percent"], "无进度讲=诚实 None（前端显示「还没看过」）")
        self.assertEqual(second["seconds"], 300)

    def test_detail_forecast_buckets_overdue_folds_into_today(self):
        ensure_flashcard_schema(self.db)
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO flash_cards(card_id,course_id,sub_id,card_type,front,back,state,due_at,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    ("f-over", "course-1", "l1", "short_answer", "f", "b", "review", NOW - 86_400, NOW),
                    ("f-plus2", "course-1", "l1", "short_answer", "f", "b", "review", NOW + 2 * 86_400, NOW),
                    ("f-far", "course-1", "l1", "short_answer", "f", "b", "review", NOW + 9 * 86_400, NOW),
                    ("f-new", "course-1", "l1", "short_answer", "f", "b", "new", NOW - 86_400, NOW),
                ],
            )
            conn.commit()
        forecast = self._detail()["forecast"]
        self.assertEqual([day["date"] for day in forecast][0], _iso(TODAY))
        self.assertEqual(forecast[0]["due"], 1, "积压卡并入今天（今天必须先清）")
        self.assertEqual(forecast[2]["due"], 1)
        self.assertEqual(sum(day["due"] for day in forecast), 2, "窗外卡与 new 卡不进预测")

    def test_detail_watch_only_course_is_gray_entry(self):
        with self._conn() as conn:
            initialize_learning_schema(conn)
            conn.execute(
                "INSERT OR REPLACE INTO watch_progress(sub_id,course_id,position_ms,duration_ms,completed,playback_rate,updated_at)"
                " VALUES('lecture-1','course-watch',0,1000000,0,1.0,?)",
                (NOW,),
            )
            conn.commit()
        detail = self._detail()
        course = next(item for item in detail["courses"] if item["course_id"] == "course-watch")
        self.assertEqual(course["tier"], "数据不足")
        self.assertTrue(course["gray"])
        self.assertEqual(course["completion_percent"], 0)
        self.assertEqual(len(course["lectures"]), 1)
        # overview top3 语义不变：纯观看课程不占默认层行动行/灰档位。
        overview = study_overview(self.db, now=NOW)
        self.assertEqual(overview["mastery"]["courses"], [])

    def test_detail_course_completion_average(self):
        self._seed_signal_mix()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO watch_progress(sub_id,course_id,position_ms,duration_ms,completed,playback_rate,updated_at)"
                " VALUES('lecture-2','course-1',900000,1000000,0,1.0,?)",
                (NOW,),
            )
            conn.commit()
        course = next(item for item in self._detail()["courses"] if item["course_id"] == "course-1")
        self.assertEqual(course["completion_percent"], 75)  # (60%+90%)/2
        self.assertEqual(course["week_interactions"], 3)


class TimingAndEraseTests(StudyStatsTestBase):
    def test_overview_100k_history_rows_within_budget(self):
        """查询帽钉：10 万行历史 + 小当前窗 → 索引范围扫描聚合 <50ms。"""
        with self._conn() as conn:
            initialize_learning_schema(conn)
            old_base = _local_midnight(MONDAY - timedelta(days=365))
            conn.executemany(
                "INSERT INTO watch_events(event_id,course_id,sub_id,event,position_ms,playback_rate,occurred_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    (
                        f"bulk-{index}", f"course-{index % 20}", f"lecture-{index % 400}",
                        "replay", (index % 3600) * 1000, 1.0,
                        old_base + (index % 360) * 86_400 + (index % 3_600),
                    )
                    for index in range(100_000)
                ),
            )
            conn.executemany(
                "INSERT INTO watch_events(event_id,course_id,sub_id,event,position_ms,playback_rate,occurred_at)"
                " VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        f"week-{day}-{index}", "course-1", f"lecture-{day}",
                        "replay", 1000, 1.0, _local_midnight(MONDAY + timedelta(days=day)) + index * 60,
                    )
                    for day in range(7) for index in range(5)
                ],
            )
            conn.commit()
        started = time.perf_counter()
        payload = study_overview(self.db, now=NOW)
        elapsed = time.perf_counter() - started
        self.assertFalse(payload["first_run"])
        self.assertEqual(payload["week"]["study_days"], 7)
        self.assertLess(elapsed, 0.05, f"概览聚合须吃查询帽（实测 {elapsed * 1000:.1f}ms）")

    def test_delete_records_family_includes_new_table(self):
        # 同族 delete（设计稿 §4.3）：数据页 delete-records 表清单必须含本道新表；
        # 钉文本合同面，免全量 application 导入（该模块由 http/回归族另行覆盖）。
        source = (Path(__file__).resolve().parents[1] / "src" / "application.py").read_text(encoding="utf-8")
        marker = source.index("_COURSE_DATA_RECORD_TABLES")
        block = source[marker:marker + 400]
        self.assertIn('"study_daily_seconds"', block)


class _RouteService:
    """路由合同桩：study/overview 与 study/heartbeat 走 service.learning 白名单。"""

    def __init__(self):
        self.overview = {"view": "study_overview", "first_run": True}

    def study_overview(self):
        return dict(self.overview)

    def study_detail(self):
        return {
            "view": "study_detail",
            "forecast": [{"date": "2026-10-07", "due": 3}],
            "courses": [],
        }

    def record_study_heartbeat(self, course_id, sub_id, seconds):
        return {"recorded": True, "active_seconds": 30, "sub_id": str(sub_id), "seconds": seconds}

    def authentication_snapshot(self):
        return {"state": "ready"}


class StudyStatsRouteTests(unittest.TestCase):
    def _serve(self, service) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(http_services(service), Path(tmp.name)),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def _shutdown():
            server.shutdown()  # 先停 serve_forever，再关套接字（win select 撕裂防）
            server.server_close()
            thread.join(timeout=2)

        self.addCleanup(_shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_overview_route_returns_envelope(self):
        base = self._serve(_RouteService())
        with urlopen(f"{base}/api/v3/study/overview") as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["schema"], "courselens.api.v3")
        self.assertEqual(payload["data"]["view"], "study_overview")

    def test_detail_route_returns_envelope(self):
        base = self._serve(_RouteService())
        with urlopen(f"{base}/api/v3/study/detail") as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["schema"], "courselens.api.v3")
        self.assertEqual(payload["data"]["view"], "study_detail")
        self.assertEqual(payload["data"]["forecast"][0]["due"], 3)

    def test_heartbeat_route_round_trip(self):
        base = self._serve(_RouteService())
        body = json.dumps({"course_id": "course-1", "sub_id": "lecture-1", "seconds": 30}).encode()
        request = Request(
            f"{base}/api/v3/study/heartbeat", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request) as response:
            payload = json.loads(response.read())
        self.assertTrue(payload["data"]["recorded"])
        self.assertEqual(payload["data"]["active_seconds"], 30)
        self.assertEqual(payload["data"]["sub_id"], "lecture-1")


if __name__ == "__main__":
    unittest.main()
