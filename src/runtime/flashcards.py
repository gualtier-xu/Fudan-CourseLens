"""闪卡复习（RR-P4FSRS-1）：本地 Minimal FSRS 调度 + 既有产物派生。

职责边界（对齐 quiz-recall-v2 与 course_knowledge 家规）：

- **零模型、零外呼。** 卡片全部从本机已有的 course-knowledge.v1 快照
  派生（key_points = AI 总结/讲次 IR/章节的既有产物），调度是纯本地
  算法。不新增任何模型调用，不发明快照里没有的事实。
- **诚实卡面。** 卡背 = 被引要点原文 + 快照 citation 闭集内解析出的
  来源；卡面绝不包含答案文本。无法安全制卡的要点宁可不制（填空只在
  要点文本精确含主题标题时做，不猜变体）。
- **Minimal FSRS-4.5**（open-spaced-repetition 默认参数，纯函数）：
  遗忘曲线 R=(1+F·t/S)^DECAY；目标留存 0.9；学习步 [1,10] 分钟、
  重学步 [10] 分钟。参数不持久化调优——先给一个可解释、可测试的
  缺省调度，个体化是后续命题。
- **有界。** 每讲至多 FLASHCARDS_PER_LECTURE 张，课程总量有帽；
  会话队列 = 到期卡 + 少量新卡，单次不超过 SESSION_BATCH。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from contextlib import closing
from typing import Any

from .sqlite_utils import connect_learning_db

# ---- Minimal FSRS-4.5（默认权重，纯算法零依赖） -----------------------------
# 权重来自 open-spaced-repetition 公布的 FSRS-4.5 缺省参数（17 维）；
# DECAY=-0.5 对应 R=(1+FACTOR·t/S)^DECAY，FACTOR=0.9^(1/DECAY)-1=19/81。
FSRS_VERSION = "fsrs-4.5-default"
FSRS_WEIGHTS = (
    0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.031,
    1.6474, 0.1367, 1.0461, 2.1072, 0.0793, 0.3246, 1.587, 0.2272, 2.8755,
)
_FSRS_DECAY = -0.5
_FSRS_FACTOR = 0.9 ** (1 / _FSRS_DECAY) - 1.0

# 评分闭集：1=忘了 2=困难 3=良好 4=简单（前端按钮与文档同序）。
RATING_AGAIN, RATING_HARD, RATING_GOOD, RATING_EASY = 1, 2, 3, 4
RATINGS = (RATING_AGAIN, RATING_HARD, RATING_GOOD, RATING_EASY)

# 卡片状态闭集。
STATE_NEW, STATE_LEARNING, STATE_REVIEW, STATE_RELEARNING = "new", "learning", "review", "relearning"

# 目标留存率：与 review_plans 的期末特化一致取 0.9（不是可调项）。
RETENTION_TARGET = 0.9
# 复习态最小间隔（毕业/调度下限）：同日重复没有记忆价值。
MIN_REVIEW_DAYS = 1.0
# 学习/重学步长（分钟）。Anki 缺省族；again 回第 0 步，good/easy 前进。
LEARNING_STEPS_MIN = (1.0, 10.0)
RELEARNING_STEPS_MIN = (10.0,)

MINUTE = 60.0
DAY = 86400.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def fsrs_retrievability(elapsed_days: float, stability: float) -> float:
    """遗忘曲线：R = (1 + FACTOR·t/S)^DECAY。t=0 → R=1；t→∞ 单调递减。"""
    if stability <= 0:
        return 0.0
    elapsed = max(0.0, float(elapsed_days))
    return _clamp((1.0 + _FSRS_FACTOR * elapsed / stability) ** _FSRS_DECAY, 0.0, 1.0)


def fsrs_interval_days(stability: float, retention: float = RETENTION_TARGET) -> float:
    """给定目标留存率求间隔天数：I = (S/FACTOR)·(r^(1/DECAY) - 1)。"""
    if stability <= 0:
        return MIN_REVIEW_DAYS
    retention = _clamp(float(retention), 0.5, 0.99)
    return max(MIN_REVIEW_DAYS, stability / _FSRS_FACTOR * (retention ** (1 / _FSRS_DECAY) - 1.0))


def _fsrs_init_stability(rating: int) -> float:
    return FSRS_WEIGHTS[rating - 1]


def _fsrs_init_difficulty(rating: int) -> float:
    return _clamp(FSRS_WEIGHTS[4] - FSRS_WEIGHTS[5] * (rating - 3), 1.0, 10.0)


def _fsrs_next_difficulty(difficulty: float, rating: int) -> float:
    """FSRS-4.5：D' = D - w6·(G-3)，再向 D0(4) 均值回缩 w7。"""
    next_difficulty = difficulty - FSRS_WEIGHTS[6] * (rating - 3)
    reverted = FSRS_WEIGHTS[7] * _fsrs_init_difficulty(RATING_EASY) + (1 - FSRS_WEIGHTS[7]) * next_difficulty
    return _clamp(reverted, 1.0, 10.0)


def _fsrs_recall_stability(stability: float, difficulty: float, retrievability: float, rating: int) -> float:
    """FSRS-4.5 回忆后稳定性（含 hard 惩罚 w15 / easy 奖励 w16）。"""
    bonus = FSRS_WEIGHTS[15] if rating == RATING_HARD else (FSRS_WEIGHTS[16] if rating == RATING_EASY else 1.0)
    growth = (
        math.exp(FSRS_WEIGHTS[8]) * (11.0 - difficulty)
        * (stability ** -FSRS_WEIGHTS[9])
        * (math.exp(FSRS_WEIGHTS[10] * (1.0 - retrievability)) - 1.0)
        * bonus
    )
    return max(MIN_REVIEW_DAYS, stability * (1.0 + growth))


def _fsrs_lapse_stability(stability: float, difficulty: float, retrievability: float) -> float:
    """FSRS-4.5 遗忘后稳定性；不高于原稳定性（遗忘不该让卡更稳）。"""
    lapse = (
        FSRS_WEIGHTS[11] * (difficulty ** -FSRS_WEIGHTS[12])
        * ((stability + 1.0) ** FSRS_WEIGHTS[13] - 1.0)
        * math.exp(FSRS_WEIGHTS[14] * (1.0 - retrievability))
    )
    return max(0.01, min(stability, lapse))


def schedule_flashcard(state: dict[str, Any] | None, rating: int, *, now: float) -> dict[str, Any]:
    """对一张卡应用一次评分，返回新调度状态（纯函数，不触库）。

    输入 state 允许缺省（新卡）；输出含 state/difficulty/stability/due_at/
    reps/lapses/last_review_at/interval_seconds。rating 必须落在闭集内。
    """
    if rating not in RATINGS:
        raise ValueError("flashcard_rating_invalid")
    now = float(now)
    current = dict(state or {})
    card_state = str(current.get("state") or STATE_NEW)
    step = 0
    difficulty = float(current.get("difficulty") or 0.0)
    stability = float(current.get("stability") or 0.0)
    reps = int(current.get("reps") or 0)
    lapses = int(current.get("lapses") or 0)
    last_review_at = float(current.get("last_review_at") or 0.0)

    if card_state == STATE_NEW:
        difficulty = _fsrs_init_difficulty(rating)
        stability = _fsrs_init_stability(rating)
        reps = 1
        step = 0
        if rating == RATING_EASY:
            # 简单 = 一次即毕业，间隔直接按初始稳定性走天级。
            next_state = STATE_REVIEW
            due_at = now + fsrs_interval_days(stability) * DAY
        elif rating == RATING_GOOD:
            next_state, step = STATE_LEARNING, 1
            due_at = now + LEARNING_STEPS_MIN[1] * MINUTE
        else:
            next_state = STATE_LEARNING
            due_at = now + LEARNING_STEPS_MIN[0] * MINUTE
    elif card_state in (STATE_LEARNING, STATE_RELEARNING):
        steps = LEARNING_STEPS_MIN if card_state == STATE_LEARNING else RELEARNING_STEPS_MIN
        step = int(current.get("learning_step") or 0)
        if rating == RATING_AGAIN:
            step = 0
            next_state = card_state
            due_at = now + steps[0] * MINUTE
        else:
            step += 1
            if step >= len(steps) or rating == RATING_EASY:
                next_state = STATE_REVIEW
                if stability <= 0:
                    stability = _fsrs_init_stability(RATING_GOOD)
                due_at = now + fsrs_interval_days(stability) * DAY
            else:
                next_state = card_state
                due_at = now + steps[step] * MINUTE
        # 学习/重学步只推进步进：difficulty/stability 已在进入该状态前的
        # review 评分里更新过，这里再动会双重应用。
        reps += 1
    elif card_state == STATE_REVIEW:
        elapsed_days = max(0.0, (now - last_review_at) / DAY) if last_review_at else 0.0
        retrievability = fsrs_retrievability(elapsed_days, stability or 1.0)
        difficulty = _fsrs_next_difficulty(difficulty or _fsrs_init_difficulty(RATING_GOOD), rating)
        if rating == RATING_AGAIN:
            lapses += 1
            stability = _fsrs_lapse_stability(stability, difficulty, retrievability)
            next_state = STATE_RELEARNING
            due_at = now + RELEARNING_STEPS_MIN[0] * MINUTE
        else:
            stability = _fsrs_recall_stability(stability, difficulty, retrievability, rating)
            next_state = STATE_REVIEW
            due_at = now + fsrs_interval_days(stability) * DAY
        reps += 1
    else:
        # 未知状态不猜：按新卡重新起步（诚实降级，不崩）。
        return schedule_flashcard(None, rating, now=now)

    interval_seconds = max(0.0, due_at - now)
    return {
        "state": next_state,
        "difficulty": round(difficulty, 4),
        "stability": round(stability, 4),
        "due_at": due_at,
        "reps": reps,
        "lapses": lapses,
        "last_review_at": now,
        "interval_seconds": interval_seconds,
        "learning_step": step if next_state in (STATE_LEARNING, STATE_RELEARNING) else 0,
        "fsrs_version": FSRS_VERSION,
    }


def interval_text(seconds: float) -> str:
    """给学生的下一间隔人话：<1h 按分钟，<1d 按小时，否则按天（四舍五入）。"""
    seconds = max(0.0, float(seconds))
    if seconds < 1 * 60:
        return "1 分钟内"
    if seconds < 1 * 60 * 60:
        return f"{int(round(seconds / 60))} 分钟后"
    if seconds < 1 * DAY:
        return f"{int(round(seconds / 3600))} 小时后"
    days = int(round(seconds / DAY))
    if days <= 1:
        return "1 天后"
    if days < 30:
        return f"{days} 天后"
    months = days / 30.0
    return f"{months:.1f} 个月后" if months < 12 else "一年以后"


def _local_day_start(now_value: float) -> float:
    """P2-8：学生本地时区的当日零点。「今日已复习」按本地日历日算，不按 UTC。"""
    local = time.localtime(now_value)
    return time.mktime(
        (local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)
    )


# ---- 存储 ------------------------------------------------------------------

FLASHCARDS_PER_LECTURE = 12
MAX_CARDS_PER_COURSE = 160
SESSION_BATCH = 20
# 每次会话最多补入的新卡数：到期卡优先，新卡只补空隙。
SESSION_NEW_FILL = 8

CARD_TYPE_CLOZE = "cloze"
CARD_TYPE_TOPIC = "topic_cue"
CARD_TYPE_RECALL = "anchor_recall"
CARD_TYPES = (CARD_TYPE_CLOZE, CARD_TYPE_TOPIC, CARD_TYPE_RECALL)

# 卡面身份版本：改派生措辞时递增，旧卡按其身份原样保留（不重写历史）。
FLASHCARD_PROMPT_VERSION = "flashcards-v1"


def ensure_flashcard_schema(path: str | Any) -> None:
    """learning DB 里的两张表：卡定义与评分流水（状态列内嵌卡定义表）。"""
    with closing(connect_learning_db(path)) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS flash_cards (
                card_id TEXT PRIMARY KEY, course_id TEXT NOT NULL, sub_id TEXT NOT NULL,
                card_type TEXT NOT NULL, front TEXT NOT NULL, back TEXT NOT NULL,
                hint TEXT NOT NULL DEFAULT '', evidence_json TEXT NOT NULL DEFAULT '[]',
                prompt_version TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'new', difficulty REAL NOT NULL DEFAULT 0,
                stability REAL NOT NULL DEFAULT 0, due_at REAL NOT NULL DEFAULT 0,
                reps INTEGER NOT NULL DEFAULT 0, lapses INTEGER NOT NULL DEFAULT 0,
                learning_step INTEGER NOT NULL DEFAULT 0, last_review_at REAL NOT NULL DEFAULT 0,
                fsrs_version TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_flash_cards_course ON flash_cards(course_id,due_at);
            CREATE TABLE IF NOT EXISTS flash_card_reviews (
                review_id TEXT PRIMARY KEY, card_id TEXT NOT NULL, rating INTEGER NOT NULL,
                interval_seconds REAL NOT NULL DEFAULT 0, reviewed_at REAL NOT NULL,
                FOREIGN KEY(card_id) REFERENCES flash_cards(card_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_flash_card_reviews_card ON flash_card_reviews(card_id,reviewed_at);
            CREATE TABLE IF NOT EXISTS flash_deck_meta (
                course_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL DEFAULT '',
                derived_at REAL NOT NULL DEFAULT 0, empty_reason TEXT NOT NULL DEFAULT ''
            );
            """
        )
        # P2-16：既有库补 empty_reason 列（为什么一张卡都做不出来）。
        columns = {row[1] for row in db.execute("PRAGMA table_info(flash_deck_meta)")}
        if "empty_reason" not in columns:
            db.execute("ALTER TABLE flash_deck_meta ADD COLUMN empty_reason TEXT NOT NULL DEFAULT ''")
        db.commit()


def _card_id(course_id: str, sub_id: str, card_type: str, source_text: str) -> str:
    identity = {
        "course_id": str(course_id), "sub_id": str(sub_id), "card_type": str(card_type),
        "source": hashlib.sha256(str(source_text).encode("utf-8")).hexdigest(),
        "prompt_version": FLASHCARD_PROMPT_VERSION,
    }
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]


def _citation_map(lecture: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """快照 citation_id → 前端可渲染的最小来源（kind/label/locator）。"""
    refs: dict[str, dict[str, Any]] = {}
    for ref in lecture.get("evidence_refs") or []:
        if not isinstance(ref, dict):
            continue
        citation_id = str(ref.get("citation_id") or "")
        if not citation_id:
            continue
        locator = ref.get("locator") if isinstance(ref.get("locator"), dict) else {}
        refs[citation_id] = {
            "citation_id": citation_id,
            "kind": str(ref.get("kind") or ""),
            "label": str(ref.get("label") or ""),
            "start_ms": locator.get("start_ms"),
            "end_ms": locator.get("end_ms"),
            "page": locator.get("page"),
        }
    return refs


def _resolve_evidence(citation_ids: list[str], citations: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """闭集解析：快照里没有的 citation 直接丢弃（绝不发明来源）。"""
    resolved = []
    for citation_id in citation_ids[:6]:
        ref = citations.get(str(citation_id))
        if ref:
            resolved.append(ref)
    return resolved


def _mmss(milliseconds: object) -> str:
    try:
        total = max(0, int(milliseconds or 0)) // 1000
    except (TypeError, ValueError):
        return ""
    return f"{total // 60:02d}:{total % 60:02d}"


def _derive_cards_for_lecture(
    lecture: dict[str, Any], *, now: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """一讲 → 至多 FLASHCARDS_PER_LECTURE 张卡（填空 > 主题线索 > 时间锚）。

    主题匹配只做规范化后的精确包含（复用合同 normalize_topic_title 的
    归一），不猜变体；匹配不上就降级到下一卡型——宁可卡少，不做错卡。
    返回值附带要点消化统计（P2-16 空态记因用）：卡为 0 时能说清「为什么」。
    """
    sub_id = str(lecture.get("sub_id") or "")
    course_id = str(lecture.get("course_id") or "")
    citations = _citation_map(lecture)
    topics = [
        str(topic.get("title") or "").strip()
        for topic in lecture.get("topics") or []
        if isinstance(topic, dict) and str(topic.get("title") or "").strip()
    ]
    normalized_topics = {title.lower(): title for title in topics}

    # 主题 → 共享 citation 的要点集合（topic_cue 卡的依据）。
    topic_citations: dict[str, set[str]] = {}
    for topic in lecture.get("topics") or []:
        if isinstance(topic, dict) and str(topic.get("title") or "").strip():
            topic_citations[str(topic.get("title") or "").strip()] = {
                str(value) for value in topic.get("citation_ids") or []
            }

    stats = {"points_seen": 0, "points_with_text": 0, "points_with_citations": 0, "points_with_evidence": 0}
    cards: list[dict[str, Any]] = []
    seen_card_ids: set[str] = set()
    anchorless_seen = 0
    for point in lecture.get("key_points") or []:
        if len(cards) >= FLASHCARDS_PER_LECTURE:
            break
        if not isinstance(point, dict):
            continue
        stats["points_seen"] += 1
        text = str(point.get("text") or "").strip()
        citation_ids = [str(value) for value in point.get("citation_ids") or []]
        if not text:
            continue
        stats["points_with_text"] += 1
        if not citation_ids:
            continue
        stats["points_with_citations"] += 1
        anchor_ms = point.get("anchor_ms")
        anchor = anchor_ms if isinstance(anchor_ms, int) and not isinstance(anchor_ms, bool) and anchor_ms >= 0 else None
        evidence = _resolve_evidence(citation_ids, citations)
        if not evidence:
            continue
        stats["points_with_evidence"] += 1

        # 先定卡型（P1-1：掩码必须真的替换上才算填空——大小写不敏感匹配也要
        # 替换；re.subn 没替换就降级下一卡型，卡面绝不带答案原文出海）。
        card_type = ""
        masked_front = ""
        lowered = text.lower()
        masked_title = ""
        for lowered_title, title in normalized_topics.items():
            if lowered_title and lowered_title in lowered:
                masked_title = title
                break
        if masked_title and len(text) >= len(masked_title) + 8:
            masked_front, masked_count = re.subn(
                re.escape(masked_title), "____", text, count=1, flags=re.IGNORECASE
            )
            if masked_count:
                card_type = CARD_TYPE_CLOZE
        if not card_type:
            shared = None
            point_citations = set(citation_ids)
            for title, citation_set in topic_citations.items():
                if citation_set & point_citations:
                    shared = title
                    break
            if shared:
                card_type = CARD_TYPE_TOPIC
            else:
                card_type = CARD_TYPE_RECALL

        # 去重键 = card_id（P1-2）：无锚回忆卡不再共用通用 front 被静默丢卡。
        card_key = _card_id(course_id, sub_id, card_type, text)
        if card_key in seen_card_ids:
            continue
        seen_card_ids.add(card_key)

        # 再生成卡面文案（序数只在卡真正成立时消耗，编号不留空洞）。
        if card_type == CARD_TYPE_CLOZE:
            front, hint = masked_front, "填空：这个词是什么？"
        elif card_type == CARD_TYPE_TOPIC:
            front = f"关于「{shared}」，这一讲讲了什么要点？"
            hint = f"与「{shared}」有关的结论。"
        else:
            time_text = _mmss(anchor) if anchor is not None else ""
            if time_text:
                front = f"讲次进行到 {time_text} 附近时，老师强调的一个要点是什么？"
            else:
                anchorless_seen += 1
                front = f"这一讲里没标时间点的要点（第 {anchorless_seen} 个）：先回忆，再看答案。"
            hint = ""
        cards.append({
            "card_id": card_key,
            "course_id": course_id,
            "sub_id": sub_id,
            "card_type": card_type,
            "front": front[:600],
            "back": text[:1000],
            "hint": hint[:200],
            "evidence": evidence,
            "anchor_ms": anchor,
            "created_at": float(now),
        })
    return cards, stats


def _record_empty_reason(path: str, course_id: str, reason: str) -> None:
    """P2-16：把「为什么一张卡都做不出来」记入 flash_deck_meta（只动 reason 列，
    不碰 input_hash/derived_at——那是调用方的派生守卫面）。"""
    with closing(connect_learning_db(path)) as db:
        db.execute(
            """INSERT INTO flash_deck_meta(course_id,input_hash,derived_at,empty_reason)
               VALUES(?,'',0,?)
               ON CONFLICT(course_id) DO UPDATE SET empty_reason=excluded.empty_reason""",
            (str(course_id), str(reason)),
        )
        db.commit()


def derive_flashcards(store, *, course_id: str, document: dict[str, Any], now: float | None = None) -> int:
    """把快照里的新卡插入库（按 card_id 幂等）；返回本次新插入数。

    只插入快照里仍存在的卡；既有卡（含历史评分）绝不删除——旧卡是
    学生的真实复习记录。快照 input_hash 记入 app_state 由调用方守卫，
    这里只做幂等 UPSERT。课程总量帽（P1-4）在插入时按存量执行，读面
    因此无需截断。做不出卡时把原因记入 empty_reason，空态给人话分型。
    """
    now_value = float(time.time() if now is None else now)
    lectures = [
        lecture for lecture in document.get("lectures") or []
        if isinstance(lecture, dict) and str(lecture.get("sub_id") or "")
    ]
    stats = {"points_seen": 0, "points_with_text": 0, "points_with_citations": 0, "points_with_evidence": 0}
    derived: list[dict[str, Any]] = []
    for lecture in lectures:
        lecture_cards, lecture_stats = _derive_cards_for_lecture(lecture, now=now_value)
        derived.extend(lecture_cards)
        for key, value in lecture_stats.items():
            stats[key] = stats.get(key, 0) + value
    ensure_flashcard_schema(store.path)
    if not derived:
        # P2-16：诚实记录原因，别让学生对着空态反复点「更新课程知识」。
        if stats["points_with_text"] <= 0:
            reason = "no_key_points"
        elif stats["points_with_citations"] <= 0 or stats["points_with_evidence"] <= 0:
            reason = "points_missing_anchors"
        else:
            reason = ""
        _record_empty_reason(store.path, course_id, reason)
        return 0
    inserted = 0
    with closing(connect_learning_db(store.path)) as db:
        existing = int(
            db.execute(
                "SELECT COUNT(*) FROM flash_cards WHERE course_id=?", (str(course_id),)
            ).fetchone()[0] or 0
        )
        headroom = max(0, MAX_CARDS_PER_COURSE - existing)
        for card in derived:
            if inserted >= headroom:
                break
            cursor = db.execute(
                """INSERT INTO flash_cards(
                       card_id,course_id,sub_id,card_type,front,back,hint,evidence_json,
                       prompt_version,state,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(card_id) DO NOTHING""",
                (
                    card["card_id"], card["course_id"], card["sub_id"], card["card_type"],
                    card["front"], card["back"], card["hint"],
                    json.dumps(card["evidence"], ensure_ascii=False, separators=(",", ":")),
                    FLASHCARD_PROMPT_VERSION, STATE_NEW, card["created_at"],
                ),
            )
            inserted += cursor.rowcount > 0
        if inserted:
            # 帽内真进了新卡：清掉历史空态原因（有的话）。
            db.execute(
                "UPDATE flash_deck_meta SET empty_reason='' WHERE course_id=?", (str(course_id),)
            )
        db.commit()
    return inserted


def last_derived_input_hash(store, *, course_id: str) -> str:
    ensure_flashcard_schema(store.path)
    with closing(connect_learning_db(store.path)) as db:
        row = db.execute(
            "SELECT input_hash FROM flash_deck_meta WHERE course_id=?", (str(course_id),)
        ).fetchone()
    return str(row[0]) if row and len(row) > 0 else ""


def mark_derived(store, *, course_id: str, input_hash: str, now: float | None = None) -> None:
    ensure_flashcard_schema(store.path)
    with closing(connect_learning_db(store.path)) as db:
        db.execute(
            """INSERT INTO flash_deck_meta(course_id,input_hash,derived_at) VALUES(?,?,?)
               ON CONFLICT(course_id) DO UPDATE SET input_hash=excluded.input_hash,
                                                      derived_at=excluded.derived_at""",
            (str(course_id), str(input_hash), float(time.time() if now is None else now)),
        )
        db.commit()


def _empty_action(reason: str) -> dict[str, str]:
    """P2-16：空态分型人话——按「为什么做不出卡」给学生不同的解释与下一步。"""
    if reason == "points_missing_anchors":
        return {
            "action": "refresh_course_knowledge",
            "label": "更新课程知识",
            "hint": "这一课的要点都还没带可核对出处的引用，闪卡只做有出处的卡（答案要能翻回原文核对）。重新生成一次讲次总结、保留引用标注后，这里就能做好卡。",
        }
    if reason == "no_key_points":
        return {
            "action": "refresh_course_knowledge",
            "label": "更新课程知识",
            "hint": "这一课的总结里还没找到能做成卡的要点。先在讲次里生成一次总结，或点「更新课程知识」，做好后闪卡会自动出现。",
        }
    return {
        "action": "refresh_course_knowledge",
        "label": "更新课程知识",
        "hint": "这一课还没有可制卡的内容：先在讲次里生成一次总结，或点「更新课程知识」，闪卡会从要点里自动做好。",
    }


def flashcard_deck(store, *, course_id: str, lecture_labels: dict[str, str] | None = None,
                   now: float | None = None) -> dict[str, Any]:
    """课程级闪卡读面：计数 + 会话队列（到期优先、新卡补隙）。"""
    now_value = float(time.time() if now is None else now)
    labels = dict(lecture_labels or {})
    ensure_flashcard_schema(store.path)
    with closing(connect_learning_db(store.path)) as db:
        db.row_factory = sqlite3.Row
        # P1-4：读面不截断——总量帽已在插入时按存量执行；固定排序保证可复现。
        rows = db.execute(
            "SELECT * FROM flash_cards WHERE course_id=? ORDER BY created_at,card_id",
            (str(course_id),),
        ).fetchall()
        # P2-8：「今日」按学生本地日历日算，不按 UTC。
        reviewed_today = db.execute(
            """SELECT COUNT(DISTINCT r.card_id) FROM flash_card_reviews r
               JOIN flash_cards c ON c.card_id=r.card_id
              WHERE c.course_id=? AND r.reviewed_at>=?""",
            (str(course_id), _local_day_start(now_value)),
        ).fetchone()[0]
        meta_row = db.execute(
            "SELECT empty_reason FROM flash_deck_meta WHERE course_id=?", (str(course_id),)
        ).fetchone()
    empty_reason = str(meta_row["empty_reason"] or "") if meta_row is not None else ""

    counts = {"total": 0, "due": 0, "new": 0, "learning": 0, "reviewed_today": int(reviewed_today or 0)}
    due_rows: list[sqlite3.Row] = []
    new_rows: list[sqlite3.Row] = []
    for row in rows:
        state = str(row["state"] or STATE_NEW)
        counts["total"] += 1
        if state == STATE_NEW:
            counts["new"] += 1
            new_rows.append(row)
            continue
        if state in (STATE_LEARNING, STATE_RELEARNING):
            counts["learning"] += 1
        # 到期语义 = 调度时刻已过（due_at <= now）：学习步未到点不算到期。
        if float(row["due_at"] or 0) <= now_value:
            counts["due"] += 1
            due_rows.append(row)
    due_rows.sort(key=lambda row: (float(row["due_at"] or 0), str(row["card_id"])))
    queue = due_rows[:SESSION_BATCH] + new_rows[:max(0, SESSION_BATCH - len(due_rows[:SESSION_BATCH]))][:SESSION_NEW_FILL]
    # P2-15：同一要点的双卡（主题措辞变化→换 front 不换 back）不进同一会话，
    # 纯读面去重；deck 计数不动——两张卡都真实存在、只是这次只见一张。
    seen_backs: set[str] = set()
    session_rows: list[sqlite3.Row] = []
    for row in queue:
        back_key = hashlib.sha256(str(row["back"] or "").strip().encode("utf-8")).hexdigest()
        if back_key in seen_backs:
            continue
        seen_backs.add(back_key)
        session_rows.append(row)

    cards = []
    for row in session_rows:
        value = dict(row)
        value["evidence"] = json.loads(value.pop("evidence_json", "[]") or "[]")
        value["lecture_label"] = labels.get(str(value.get("sub_id") or ""), "") or "这一讲"
        value["due"] = str(value.get("state") or STATE_NEW) != STATE_NEW and float(value.get("due_at") or 0) <= now_value
        cards.append(value)
    view: dict[str, Any] = {
        "view": "course_flashcards",
        "label": "闪卡复习",
        "course_id": str(course_id),
        "source_label": "来自本课总结与知识要点（非官方）",
        "fsrs_version": FSRS_VERSION,
        "counts": counts,
        "cards": cards,
    }
    if not counts["total"]:
        view["empty_action"] = _empty_action(empty_reason)
    elif not cards:
        next_due = min(
            (float(row["due_at"] or 0) for row in rows if str(row["state"] or "") != STATE_NEW and float(row["due_at"] or 0) > now_value),
            default=0.0,
        )
        view["all_caught_up"] = {
            "next_due_at": next_due,
            "next_due_text": interval_text(next_due - now_value) if next_due else "",
        }
    return view


def review_flashcard(
    store, *, card_id: str, rating: int, course_id: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    """对一张卡记一次评分：写评分流水 + 更新调度状态；返回下一间隔。

    ``course_id`` 是归属校验面（QA-SWEEP-1 P1-3）：给了就不允许动他课的卡，
    不符按「卡不存在」同一 KeyError 闭集拒绝，不向他课授权面泄露卡片存在性。
    """
    now_value = float(time.time() if now is None else now)
    if rating not in RATINGS:
        raise ValueError("flashcard_rating_invalid")
    ensure_flashcard_schema(store.path)
    with closing(connect_learning_db(store.path)) as db:
        db.row_factory = sqlite3.Row
        # P2-14：读改写包显式事务（家规同 catalog_repository：BEGIN IMMEDIATE +
        # commit/rollback），评分流水与调度状态要么都落、要么都不落。
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute("SELECT * FROM flash_cards WHERE card_id=?", (str(card_id),)).fetchone()
            if row is None:
                raise KeyError("flashcard_not_found")
            if course_id and str(row["course_id"] or "") != str(course_id):
                raise KeyError("flashcard_course_mismatch")
            current = {
                "state": str(row["state"] or STATE_NEW),
                "difficulty": float(row["difficulty"] or 0),
                "stability": float(row["stability"] or 0),
                "due_at": float(row["due_at"] or 0),
                "reps": int(row["reps"] or 0),
                "lapses": int(row["lapses"] or 0),
                "learning_step": int(row["learning_step"] or 0),
                "last_review_at": float(row["last_review_at"] or 0),
            }
            scheduled = schedule_flashcard(current, rating, now=now_value)
            db.execute(
                """UPDATE flash_cards SET state=?,difficulty=?,stability=?,due_at=?,reps=?,lapses=?,
                           learning_step=?,last_review_at=?,fsrs_version=? WHERE card_id=?""",
                (
                    scheduled["state"], scheduled["difficulty"], scheduled["stability"], scheduled["due_at"],
                    scheduled["reps"], scheduled["lapses"], int(scheduled["learning_step"]),
                    scheduled["last_review_at"], scheduled["fsrs_version"], str(card_id),
                ),
            )
            review_id = hashlib.sha256(f"{card_id}:{rating}:{now_value:.6f}".encode("utf-8")).hexdigest()[:32]
            db.execute(
                """INSERT INTO flash_card_reviews(review_id,card_id,rating,interval_seconds,reviewed_at)
                   VALUES(?,?,?,?,?)""",
                (review_id, str(card_id), int(rating), scheduled["interval_seconds"], now_value),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {
        "card_id": str(card_id),
        "rating": int(rating),
        "state": scheduled["state"],
        "due_at": scheduled["due_at"],
        "interval_seconds": scheduled["interval_seconds"],
        "interval_text": interval_text(scheduled["interval_seconds"]),
        "stability": scheduled["stability"],
        "difficulty": scheduled["difficulty"],
        "lapses": scheduled["lapses"],
    }


__all__ = [
    "FLASHCARD_PROMPT_VERSION", "FLASHCARDS_PER_LECTURE", "MAX_CARDS_PER_COURSE",
    "RATINGS", "RATING_AGAIN", "RATING_HARD", "RATING_GOOD", "RATING_EASY",
    "RETENTION_TARGET", "SESSION_BATCH", "SESSION_NEW_FILL", "FSRS_VERSION",
    "STATE_NEW", "STATE_LEARNING", "STATE_REVIEW", "STATE_RELEARNING",
    "CARD_TYPE_CLOZE", "CARD_TYPE_TOPIC", "CARD_TYPE_RECALL",
    "derive_flashcards", "ensure_flashcard_schema", "flashcard_deck",
    "fsrs_interval_days", "fsrs_retrievability", "interval_text",
    "last_derived_input_hash", "mark_derived", "review_flashcard", "schedule_flashcard",
]
