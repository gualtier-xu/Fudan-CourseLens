"""RR-P4FSRS-1：本地 Minimal FSRS 边界 + 快照派生 + 调度闭环。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.runtime.flashcards import (
    DAY,
    MAX_CARDS_PER_COURSE,
    MINUTE,
    RATING_AGAIN,
    RATING_EASY,
    RATING_GOOD,
    RATING_HARD,
    STATE_LEARNING,
    STATE_NEW,
    STATE_RELEARNING,
    STATE_REVIEW,
    CARD_TYPE_RECALL,
    CARD_TYPE_CLOZE,
    CARD_TYPE_TOPIC,
    derive_flashcards,
    ensure_flashcard_schema,
    flashcard_deck,
    fsrs_interval_days,
    fsrs_retrievability,
    interval_text,
    last_derived_input_hash,
    mark_derived,
    review_flashcard,
    schedule_flashcard,
)

NOW = 1_800_000_000.0


def _fake_store(path: Path) -> SimpleNamespace:
    return SimpleNamespace(path=path)


def _snapshot_document() -> dict:
    """两讲快照：L1 有主题命中（填空）+ 共享 citation（主题线索）；L2 走时间锚兜底。"""
    return {
        "course_id": "c1",
        "input_hash": "a" * 32,
        "status": "ready",
        "topics": [
            {"title": "费米能级", "citation_ids": ["cite-1"], "lecture_ids": ["L1"]},
            {"title": "带隙", "citation_ids": ["cite-3"], "lecture_ids": ["L2"]},
        ],
        "lectures": [
            {
                "sub_id": "L1",
                "course_id": "c1",
                "topics": [
                    {"title": "费米能级", "citation_ids": ["cite-1"], "lecture_ids": ["L1"]},
                    {"title": "载流子浓度", "citation_ids": ["cite-1"], "lecture_ids": ["L1"]},
                ],
                "key_points": [
                    {
                        "text": "费米能级是电子在绝对零度时的最高占据能级。",
                        "citation_ids": ["cite-1"],
                        "anchor_ms": 61_000,
                    },
                    {
                        "text": "温度升高时，两种载流子的数量按指数规律上升。",
                        "citation_ids": ["cite-1"],
                    },
                ],
                "evidence_refs": [
                    {
                        "citation_id": "cite-1",
                        "kind": "transcript",
                        "source_id": "seg:aaaaaaaaaaaa",
                        "revision_id": "r1",
                        "content_hash": "h1",
                        "label": "同步字幕 01:00–01:30",
                        "locator": {"start_ms": 60_000, "end_ms": 90_000},
                    }
                ],
            },
            {
                "sub_id": "L2",
                "course_id": "c1",
                "topics": [],
                "key_points": [
                    {
                        "text": "带隙决定了材料对光的吸收阈值。",
                        "citation_ids": ["cite-3"],
                        "anchor_ms": 120_000,
                    }
                ],
                "evidence_refs": [
                    {
                        "citation_id": "cite-3",
                        "kind": "slide",
                        "source_id": "slevt:bbbbbbbbbbbb",
                        "revision_id": "r2",
                        "content_hash": "h2",
                        "label": "第 3 页",
                        "locator": {"page": 3},
                    }
                ],
            },
        ],
    }


def _transcript_ref(citation_id: str = "cite-1") -> dict:
    return {
        "citation_id": citation_id,
        "kind": "transcript",
        "source_id": "seg:cccccccccccc",
        "revision_id": "r1",
        "content_hash": "h1",
        "label": "同步字幕 00:20–00:40",
        "locator": {"start_ms": 30_000, "end_ms": 40_000},
    }


def _case_mismatch_document() -> dict:
    """QA-SWEEP P1-1：主题标题与要点文本大小写不一致——掩码必须真替换，失败须降级。"""
    return {
        "course_id": "c1",
        "input_hash": "b" * 32,
        "status": "ready",
        "lectures": [
            {
                "sub_id": "L1",
                "course_id": "c1",
                "topics": [{"title": "Minimal FSRS", "citation_ids": ["cite-1"], "lecture_ids": ["L1"]}],
                "key_points": [
                    {
                        "text": "the minimal fsrs scheduler runs locally.",
                        "citation_ids": ["cite-1"],
                        "anchor_ms": 30_000,
                    }
                ],
                "evidence_refs": [_transcript_ref()],
            }
        ],
    }


def _multi_anchorless_document() -> dict:
    """QA-SWEEP P1-2：同一讲多个无锚要点——全部成卡、卡面互异，不静默丢卡。"""
    return {
        "course_id": "c1",
        "input_hash": "d" * 32,
        "status": "ready",
        "lectures": [
            {
                "sub_id": "L1",
                "course_id": "c1",
                "topics": [],
                "key_points": [
                    {"text": f"无锚要点{index}：这一步只依赖局部信息。", "citation_ids": ["cite-1"]}
                    for index in range(1, 4)
                ],
                "evidence_refs": [_transcript_ref()],
            }
        ],
    }


def _bulk_document(seed: str, lecture_count: int) -> dict:
    """帽与读面验证矩阵：lecture_count 讲 × 12 张独立卡（无主题，全走时间锚）。"""
    return {
        "course_id": "c1",
        "input_hash": seed,
        "status": "ready",
        "lectures": [
            {
                "sub_id": f"BL-{seed}-{number:03d}",
                "course_id": "c1",
                "topics": [],
                "key_points": [
                    {
                        "text": f"{seed} 第{number:03d}讲要点{point}：独立结论，互不相同。",
                        "citation_ids": ["cite-1"],
                        "anchor_ms": point * 60_000,
                    }
                    for point in range(12)
                ],
                "evidence_refs": [_transcript_ref()],
            }
            for number in range(lecture_count)
        ],
    }


class FsrsSchedulerTests(unittest.TestCase):
    """到期/遗忘曲线边界（任务包要求的验证面）。"""

    def test_retrievability_boundaries_and_monotonic_decay(self):
        self.assertEqual(fsrs_retrievability(0.0, 5.0), 1.0)
        self.assertEqual(fsrs_retrievability(10.0, 0.0), 0.0)
        previous = 2.0
        for elapsed in (0.5, 1.0, 2.0, 5.0, 20.0):
            current = fsrs_retrievability(elapsed, 5.0)
            self.assertLess(current, previous)
            self.assertGreater(current, 0.0)
            self.assertLessEqual(current, 1.0)
            previous = current
        # 恰在稳定性处：R = 0.9^1 = 0.9（目标留存自洽）。
        self.assertAlmostEqual(fsrs_retrievability(5.0, 5.0), 0.9, places=6)

    def test_interval_matches_retention_target(self):
        stability = 4.0
        interval = fsrs_interval_days(stability, 0.9)
        # 间隔期满时遗忘曲线恰降到目标留存。
        self.assertAlmostEqual(fsrs_retrievability(interval, stability), 0.9, places=6)
        self.assertGreaterEqual(interval, 1.0)

    def test_new_card_rating_paths(self):
        again = schedule_flashcard(None, RATING_AGAIN, now=NOW)
        self.assertEqual(again["state"], STATE_LEARNING)
        self.assertAlmostEqual(again["due_at"] - NOW, 1 * MINUTE)
        good = schedule_flashcard(None, RATING_GOOD, now=NOW)
        self.assertEqual(good["state"], STATE_LEARNING)
        self.assertAlmostEqual(good["due_at"] - NOW, 10 * MINUTE)
        easy = schedule_flashcard(None, RATING_EASY, now=NOW)
        self.assertEqual(easy["state"], STATE_REVIEW)
        self.assertGreater(easy["due_at"] - NOW, 1 * DAY)
        hard = schedule_flashcard(None, RATING_HARD, now=NOW)
        self.assertEqual(hard["state"], STATE_LEARNING)
        self.assertAlmostEqual(hard["due_at"] - NOW, 1 * MINUTE)

    def test_review_interval_orders_hard_good_easy(self):
        base = {"state": STATE_REVIEW, "difficulty": 5.0, "stability": 5.0,
                "reps": 3, "lapses": 0, "learning_step": 0, "last_review_at": NOW - 5 * DAY}
        intervals = {}
        for rating in (RATING_HARD, RATING_GOOD, RATING_EASY):
            result = schedule_flashcard(base, rating, now=NOW)
            self.assertEqual(result["state"], STATE_REVIEW)
            self.assertGreaterEqual(result["due_at"] - NOW, 1 * DAY)
            intervals[rating] = result["interval_seconds"]
        self.assertLess(intervals[RATING_HARD], intervals[RATING_GOOD])
        self.assertLess(intervals[RATING_GOOD], intervals[RATING_EASY])

    def test_lapse_enters_relearning_with_short_interval_and_no_stability_gain(self):
        base = {"state": STATE_REVIEW, "difficulty": 5.0, "stability": 30.0,
                "reps": 5, "lapses": 0, "learning_step": 0, "last_review_at": NOW - 60 * DAY}
        result = schedule_flashcard(base, RATING_AGAIN, now=NOW)
        self.assertEqual(result["state"], STATE_RELEARNING)
        self.assertEqual(result["lapses"], 1)
        self.assertLessEqual(result["stability"], base["stability"])
        self.assertLess(result["due_at"] - NOW, 1 * DAY)

    def test_relearning_graduates_to_review(self):
        base = {"state": STATE_RELEARNING, "difficulty": 6.0, "stability": 2.0,
                "reps": 6, "lapses": 1, "learning_step": 0, "last_review_at": NOW - 10 * MINUTE / DAY}
        good = schedule_flashcard(base, RATING_GOOD, now=NOW)
        self.assertEqual(good["state"], STATE_REVIEW)
        self.assertGreater(good["due_at"] - NOW, 1 * DAY)
        again = schedule_flashcard(base, RATING_AGAIN, now=NOW)
        self.assertEqual(again["state"], STATE_RELEARNING)
        self.assertAlmostEqual(again["due_at"] - NOW, 10 * MINUTE)

    def test_due_boundary_and_invalid_rating(self):
        base = {"state": STATE_REVIEW, "difficulty": 5.0, "stability": 5.0,
                "reps": 3, "lapses": 0, "learning_step": 0, "last_review_at": NOW - 5 * DAY}
        result = schedule_flashcard(base, RATING_GOOD, now=NOW)
        self.assertGreater(result["due_at"], NOW)
        with self.assertRaises(ValueError):
            schedule_flashcard(base, 9, now=NOW)
        with self.assertRaises(ValueError):
            schedule_flashcard(None, 0, now=NOW)

    def test_unknown_state_degrades_to_new_card(self):
        result = schedule_flashcard({"state": "corrupted"}, RATING_GOOD, now=NOW)
        self.assertEqual(result["state"], STATE_LEARNING)

    def test_interval_text_is_human(self):
        self.assertEqual(interval_text(30 * 60), "30 分钟后")
        self.assertEqual(interval_text(5 * 3600), "5 小时后")
        self.assertEqual(interval_text(3 * DAY), "3 天后")
        self.assertEqual(interval_text(45 * DAY), "1.5 个月后")
        self.assertEqual(interval_text(400 * DAY), "一年以后")


class FlashcardDerivationTests(unittest.TestCase):
    def _derive(self, path: Path) -> int:
        store = _fake_store(path)
        ensure_flashcard_schema(path)
        return derive_flashcards(store, course_id="c1", document=_snapshot_document())

    def test_derivation_is_bounded_and_typed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            inserted = self._derive(path)
            self.assertEqual(inserted, 3)  # L1 填空 + L1 主题线索 / 时间锚 + L2 时间锚
            import sqlite3
            db = sqlite3.connect(path)
            try:
                rows = db.execute("SELECT card_type,front,back FROM flash_cards ORDER BY card_type").fetchall()
            finally:
                db.close()
            types = {row[0] for row in rows}
            self.assertIn(CARD_TYPE_CLOZE, types)
            self.assertIn(CARD_TYPE_TOPIC, types)
            self.assertIn(CARD_TYPE_RECALL, types)
            for _card_type, front, back in rows:
                self.assertNotIn(back[:12], front)  # 卡面绝不带答案文本开头

    def test_cloze_masks_topic_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            self._derive(path)
            import sqlite3
            db = sqlite3.connect(path)
            try:
                row = db.execute(
                    "SELECT front,back FROM flash_cards WHERE card_type=?", (CARD_TYPE_CLOZE,)
                ).fetchone()
            finally:
                db.close()
            self.assertIsNotNone(row)
            self.assertIn("____", row[0])
            self.assertNotIn("费米能级", row[0])
            self.assertIn("费米能级", row[1])

    def test_redrive_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            first = self._derive(path)
            second = self._derive(path)
            self.assertEqual(first, 3)
            self.assertEqual(second, 0)

    def test_evidence_stays_in_snapshot_closed_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            self._derive(path)
            import json
            import sqlite3
            db = sqlite3.connect(path)
            try:
                rows = db.execute("SELECT evidence_json FROM flash_cards").fetchall()
            finally:
                db.close()
            for (evidence_json,) in rows:
                for ref in json.loads(evidence_json):
                    self.assertIn(ref["citation_id"], {"cite-1", "cite-3"})

    def test_cloze_mask_survives_case_mismatch_or_degrades(self):
        """P1-1：掩码大小写不匹配时必须真替换或降级，卡面绝不带答案原文。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            store = _fake_store(path)
            ensure_flashcard_schema(path)
            inserted = derive_flashcards(store, course_id="c1", document=_case_mismatch_document())
            self.assertEqual(inserted, 1)
            import sqlite3
            db = sqlite3.connect(path)
            try:
                card_type, front, back = db.execute(
                    "SELECT card_type,front,back FROM flash_cards"
                ).fetchone()
            finally:
                db.close()
            self.assertNotEqual(front, back)  # 产品红线：卡面≠答案全文
            self.assertIn("minimal fsrs", back.lower())
            if card_type == CARD_TYPE_CLOZE:
                self.assertIn("____", front)
                self.assertNotIn("minimal fsrs", front.lower())
            else:
                self.assertTrue(front)  # 降级卡型：可读卡面，且上文已断言卡面≠答案全文

    def test_anchorless_points_all_get_distinct_cards(self):
        """P1-2：同讲多无锚要点全部成卡，front 带序数上下文互异、card_id 互异。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            store = _fake_store(path)
            ensure_flashcard_schema(path)
            inserted = derive_flashcards(store, course_id="c1", document=_multi_anchorless_document())
            self.assertEqual(inserted, 3)
            import sqlite3
            db = sqlite3.connect(path)
            try:
                rows = db.execute(
                    "SELECT card_id,front FROM flash_cards WHERE card_type=?", (CARD_TYPE_RECALL,)
                ).fetchall()
            finally:
                db.close()
            self.assertEqual(len(rows), 3)
            self.assertEqual(len({row[0] for row in rows}), 3, "card_id 互异（不再被通用 front 去重丢卡）")
            fronts = [row[1] for row in rows]
            self.assertEqual(len(set(fronts)), 3, "无锚卡面互异（序数上下文）")

    def test_course_cap_enforced_at_insert_and_read_face_not_truncated(self):
        """P1-4：总量帽在插入时按存量执行；读面不截断（>320 的存量也全可见）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            store = _fake_store(path)
            ensure_flashcard_schema(path)
            inserted = derive_flashcards(store, course_id="c1", document=_bulk_document("cap", 14))
            self.assertEqual(inserted, MAX_CARDS_PER_COURSE)  # 168 张只入库 160
            deck = flashcard_deck(store, course_id="c1", now=NOW)
            self.assertEqual(deck["counts"]["total"], MAX_CARDS_PER_COURSE)
            # 存量到帽后再派生新课容：帽不让步，一张也进不来。
            second = derive_flashcards(store, course_id="c1", document=_bulk_document("cap2", 2))
            self.assertEqual(second, 0)
            self.assertEqual(flashcard_deck(store, course_id="c1", now=NOW)["counts"]["total"], MAX_CARDS_PER_COURSE)
            # 历史存量 >320（旧行为读面 LIMIT 320 截断）：新读面必须全量计数。
            import sqlite3
            db = sqlite3.connect(path)
            try:
                base = db.execute("SELECT COUNT(*) FROM flash_cards").fetchone()[0]
                extra_rows = [
                    (f"legacy-{index:04d}", "c1", f"BL-legacy-{index:04d}", CARD_TYPE_RECALL,
                     f"legacy front {index}", f"legacy back {index}", 0.0)
                    for index in range(330 - base)
                ]
                db.executemany(
                    """INSERT INTO flash_cards(card_id,course_id,sub_id,card_type,front,back,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    extra_rows,
                )
                db.commit()
            finally:
                db.close()
            deck_all = flashcard_deck(store, course_id="c1", now=NOW)
            self.assertEqual(deck_all["counts"]["total"], 330)


class FlashcardDeckAndReviewTests(unittest.TestCase):
    def test_deck_counts_queue_and_graceful_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            store = _fake_store(path)
            # 无产物：诚实空态 + 行动指引。
            empty = flashcard_deck(store, course_id="c9", now=NOW)
            self.assertEqual(empty["counts"]["total"], 0)
            self.assertIn("empty_action", empty)
            ensure_flashcard_schema(path)
            derive_flashcards(store, course_id="c1", document=_snapshot_document())
            mark_derived(store, course_id="c1", input_hash="a" * 32, now=NOW)
            self.assertEqual(last_derived_input_hash(store, course_id="c1"), "a" * 32)
            deck = flashcard_deck(store, course_id="c1", lecture_labels={"L1": "第一讲"}, now=NOW)
            self.assertEqual(deck["counts"]["total"], 3)
            self.assertEqual(deck["counts"]["due"], 0)  # 全是新卡：不计到期
            self.assertEqual(len(deck["cards"]), 3)
            for card in deck["cards"]:
                self.assertIn(card["sub_id"], {"L1", "L2"})
            # 评分后：到期计数进入会话队列；全部清空出 all_caught_up。
            cards = list(deck["cards"])
            result = review_flashcard(store, card_id=cards[0]["card_id"], rating=RATING_GOOD, now=NOW)
            self.assertEqual(result["state"], STATE_LEARNING)
            self.assertTrue(result["interval_text"])
            deck2 = flashcard_deck(store, course_id="c1", now=NOW)
            self.assertEqual(deck2["counts"]["due"], 0)  # 10 分钟学习步未到点
            self.assertEqual(deck2["counts"]["learning"], 1)
            self.assertEqual(deck2["counts"]["reviewed_today"], 1)
            with self.assertRaises(KeyError):
                review_flashcard(store, card_id="missing", rating=RATING_GOOD, now=NOW)

    def test_learning_card_due_boundary_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            store = _fake_store(path)
            ensure_flashcard_schema(path)
            derive_flashcards(store, course_id="c1", document=_snapshot_document())
            deck = flashcard_deck(store, course_id="c1", now=NOW)
            card_id = deck["cards"][0]["card_id"]
            review_flashcard(store, card_id=card_id, rating=RATING_GOOD, now=NOW)
            # 10 分钟学习步内：学习态但未到调度时刻 → 不算到期、不进队列。
            within = flashcard_deck(store, course_id="c1", now=NOW + 5 * MINUTE)
            self.assertEqual(within["counts"]["due"], 0)
            self.assertNotIn(card_id, {card["card_id"] for card in within["cards"]})
            # 步长过后：重新入队。
            after = flashcard_deck(store, course_id="c1", now=NOW + 11 * MINUTE)
            self.assertEqual(after["counts"]["due"], 1)
            due_card = next(card for card in after["cards"] if card["card_id"] == card_id)
            self.assertTrue(due_card["due"])

    def test_reviewed_today_uses_local_calendar_day(self):
        """P2-8：「今日已复习」按学生本地日历日算，不按 UTC 日界。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            store = _fake_store(path)
            ensure_flashcard_schema(path)
            derive_flashcards(store, course_id="c1", document=_snapshot_document())
            deck = flashcard_deck(store, course_id="c1", now=NOW)
            today_card, yesterday_card = deck["cards"][0]["card_id"], deck["cards"][1]["card_id"]
            local_now = time.localtime(NOW)
            day_start = time.mktime(
                (local_now.tm_year, local_now.tm_mon, local_now.tm_mday, 0, 0, 0, 0, 0, -1)
            )
            # 本地今天零点后一分钟评的那次要计入「今日」；本地昨天那次不算。
            review_flashcard(store, card_id=today_card, rating=RATING_GOOD, now=day_start + 60)
            review_flashcard(store, card_id=yesterday_card, rating=RATING_GOOD, now=day_start - 3600)
            deck2 = flashcard_deck(store, course_id="c1", now=NOW)
            self.assertEqual(deck2["counts"]["reviewed_today"], 1)

    def test_session_queue_dedupes_same_back_cards(self):
        """P2-15：主题措辞变化产生的同要点双卡不进同一会话；deck 计数不动。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            store = _fake_store(path)
            ensure_flashcard_schema(path)
            derive_flashcards(store, course_id="c1", document=_snapshot_document())
            renamed = _snapshot_document()
            renamed["lectures"] = [renamed["lectures"][0]]  # 只保留 L1
            renamed["lectures"][0]["topics"][0]["title"] = "费米能级（Fermi Level）"
            derive_flashcards(store, course_id="c1", document=renamed)
            deck = flashcard_deck(store, course_id="c1", now=NOW)
            self.assertEqual(deck["counts"]["total"], 4, "双卡都真实存在，计数不动")
            backs = [card["back"] for card in deck["cards"]]
            self.assertEqual(len(backs), 3, "同一要点（同 back）本会话只出一张")
            self.assertEqual(len(backs), len(set(backs)), "会话内 back 互异")

    def test_empty_reason_typed_and_cleared(self):
        """P2-16：空态按「为什么做不出卡」分型；做出来后原因清空。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "learning.db"
            store = _fake_store(path)
            ensure_flashcard_schema(path)
            # 有要点但没带引用锚 → points_missing_anchors，别再让学生空点「更新」。
            missing_anchors = {
                "course_id": "c1", "input_hash": "e" * 32, "status": "ready",
                "lectures": [{
                    "sub_id": "L1", "course_id": "c1", "topics": [],
                    "key_points": [{"text": "有要点但没有引用锚，做不了卡。", "citation_ids": []}],
                    "evidence_refs": [],
                }],
            }
            self.assertEqual(derive_flashcards(store, course_id="c1", document=missing_anchors), 0)
            view = flashcard_deck(store, course_id="c1", now=NOW)
            self.assertEqual(view["empty_action"]["action"], "refresh_course_knowledge")
            self.assertIn("引用", view["empty_action"]["hint"])
            # 连要点都没有 → no_key_points。
            no_points = {
                "course_id": "c1", "input_hash": "f" * 32, "status": "ready",
                "lectures": [{"sub_id": "L1", "course_id": "c1", "topics": [], "key_points": [], "evidence_refs": []}],
            }
            self.assertEqual(derive_flashcards(store, course_id="c1", document=no_points), 0)
            view2 = flashcard_deck(store, course_id="c1", now=NOW)
            self.assertIn("要点", view2["empty_action"]["hint"])
            import sqlite3
            db = sqlite3.connect(path)
            try:
                reason = db.execute(
                    "SELECT empty_reason FROM flash_deck_meta WHERE course_id='c1'"
                ).fetchone()[0]
            finally:
                db.close()
            self.assertEqual(reason, "no_key_points")
            # 快照真做出卡后：原因清空，空态消失。
            inserted = derive_flashcards(store, course_id="c1", document=_snapshot_document())
            self.assertEqual(inserted, 3)
            db = sqlite3.connect(path)
            try:
                reason = db.execute(
                    "SELECT empty_reason FROM flash_deck_meta WHERE course_id='c1'"
                ).fetchone()[0]
            finally:
                db.close()
            self.assertEqual(reason, "")
            self.assertEqual(flashcard_deck(store, course_id="c1", now=NOW)["counts"]["total"], 3)


if __name__ == "__main__":
    unittest.main()
