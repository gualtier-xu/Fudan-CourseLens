"""本地学习统计 v2 默认层（STUDY-STATS-M1）：播放时长聚合 + 读侧概览。

全模块零内容、零外呼、零遥测：唯一新增采集点 ``study_daily_seconds``——
一行/日/讲、30s 播放心跳累加 wall-clock 秒数（无讲次名/无位置/无文本），
采集门在客户端沿用既有 ``courselens:insight`` 开关（同一隐私级：默认开、
显式写 off 才关；服务端与 watch-events 默认流同型，不做第二道门）。
其余一切观测量从既有表读侧聚合派生，不做历史回填。

查询帽（设计稿 §4.3 风险对策）：本模块只发聚合查询（GROUP BY/COUNT），
``watch_events`` 互动面走 ensure 时补建的 ``occurred_at`` 索引范围扫描；
``study_overview`` 结果按 (库路径, 本地日) 做 60s 内存缓存，心跳与擦除
写路径即时失效。擦除同族：行随数据页 delete-records（per sub_id）可删，
``clear_study_daily_seconds`` 提供全清通道。

掌握度（M2 完整三路诚实合成）：五档闭集
``薄弱 / 待巩固 / 一般 / 扎实 / 数据不足``，纯读侧、只引用计数不引用内容。
三路证据各自独立归一到 [0,1]、各自带样本量 n：
①测验路=按时间衰减加权正确率（权重 2^(-Δ天/14)，越新权重越高）；
②书签路=未解决书签密度（open / 有学习信号的讲数，取 1-密度下限钳 0）；
③FSRS 路=已复习卡遗忘曲线均值 ×(1-到期积压率)（``fsrs_retrievability`` 纯函数复用）。
合成诚实优先级：三路全无=数据不足灰档；仅一路过最小样本=出档但标注
``仅基于 X``（basis+partial 随 payload 带出）；多路=权重加权（权重写死在
常量、payload 带出可调不可隐）；矛盾证据不平均（测验高分×书签堆积→
保守档待巩固+矛盾证据句）。阈值与权重改任何一处必须同步 bump
STUDY_STATS_VERSION。
"""

from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from src.runtime.flashcards import fsrs_retrievability

# ---- 常量（合同面：改任何阈值/权重必须同步 bump STUDY_STATS_VERSION） ----

STUDY_STATS_VERSION = "study-overview-m2"

# 单次心跳秒数闭集：客户端 30s 一跳；服务端收 1..120s 容忍节拍漂移，
# 畸形值整跳丢弃（不记 0 行）。行级日累计帽=一天（抗脏数据，不追真实上限）。
HEARTBEAT_SECONDS_DEFAULT = 30
HEARTBEAT_SECONDS_MIN = 1
HEARTBEAT_SECONDS_MAX = 120
DAILY_SECONDS_CAP = 86_400

OVERVIEW_CACHE_TTL_SECONDS = 60
OVERVIEW_DAYS = 7

# 展开层（M2-b）：FSRS 未来 7 天到期预测窗口（今天..+6 天）。
DETAIL_FORECAST_DAYS = 7
# 回看热点闭集（watch_events event 值）：回放/回退=「这里值得再看」的困惑信号。
REPLAY_EVENTS = ("replay", "seek_back")

# 导出（M3）：挂 course-data action 家族（export-study-stats）的文件面。
# 设计稿 §3.4：JSON 结构化全量 + CSV×2（学生可直开 Excel，UTF-8 BOM）。
# 脱敏纪律照 course-data 既有导出：只含计数/档位/目录标签，绝不含有
# 书签备注、检索词等任何内容文本（书签 note 列永不进导出）。
STUDY_STATS_EXPORT_SCHEMA = "courselens.study-stats-export.v1"
STUDY_STATS_EXPORT_DIRNAME = "study-stats-exports"
STUDY_STATS_EXPORT_JSON = "courselens-study-stats-{date}.json"
STUDY_STATS_EXPORT_DAILY_CSV = "courselens-study-stats-{date}-daily.csv"
STUDY_STATS_EXPORT_LECTURES_CSV = "courselens-study-stats-{date}-lectures.csv"
STUDY_STATS_EXPORT_RETENTION_SECONDS = 24 * 3_600  # 过期导出清剪（迁移包同法）

# 掌握度三路权重与最小样本（设计稿 §3.3；导出 JSON 随 mastery.weights 带出）。
MASTERY_WEIGHTS = {"quiz": 0.45, "fsrs": 0.35, "bookmark": 0.20}
MASTERY_MIN_SAMPLES = {"quiz": 3, "fsrs": 5, "bookmark": 1}
MASTERY_TIERS = ("薄弱", "待巩固", "一般", "扎实", "数据不足")
MASTERY_ACTION_TIERS = ("薄弱", "待巩固")
MASTERY_TOP_MAX = 3
MASTERY_CONTRADICTION_BOOKMARKS = 3
MASTERY_DUE_BACKLOG_TIER = 10  # 到期积压张数到达即「待巩固」信号线
# 测验路时间衰减半衰期（天）：权重=2^(-Δ天/14)，两周前的作答权重减半。
MASTERY_QUIZ_HALF_LIFE_DAYS = 14.0
# 单路数据标注（basis → 学生可读名；前端「仅基于 X」同源，禁两处漂移）。
MASTERY_PATH_LABELS = {"quiz": "测验", "fsrs": "闪卡复习", "bookmark": "没听懂标记"}
# U1（STUDY-STATS-GATES-1）：行级帽不约束日合计——日显示层按一天封顶。
DAY_DISPLAY_SECONDS_CAP = 86_400

# 到期待办：只看未来 30 天内的最近一次考核/复习计划（再远不构成「到期待办」）。
DUE_EXAM_HORIZON_DAYS = 30

_OVERVIEW_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


# ---- 本地日历日（学生本地时区；flashcards._local_day_start 同法的数据面） ----

def local_date_text(now_value: float) -> str:
    return date.fromtimestamp(now_value).isoformat()


def _week_dates(now_value: float) -> list[str]:
    """本周（周一起）7 个本地日历日，ISO YYYY-MM-DD。"""
    today = date.fromtimestamp(now_value)
    monday = today - timedelta(days=today.weekday())
    return [(monday + timedelta(days=offset)).isoformat() for offset in range(OVERVIEW_DAYS)]


# ---- schema（幂等；旧库零行=诚实空态，零 backfill 零破坏性迁移） ----

def ensure_study_stats_schema(path: str | Path) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS study_daily_seconds (
                local_date TEXT NOT NULL,
                course_id TEXT NOT NULL,
                sub_id TEXT NOT NULL,
                active_seconds INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL,
                PRIMARY KEY(local_date, sub_id)
            )
            """
        )
        # 查询帽索引：watch_events 互动面的 occurred_at 范围扫描。表由
        # student_features ensure 族建——boot/导入修复链里它先于本 ensure 运行；
        # 单独先跑本 ensure 的新库（测试壳）下次随族补建，零破坏。
        if _table_exists(db, "watch_events"):
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_watch_events_time ON watch_events(occurred_at)"
            )
        db.commit()


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return bool(row)


# ---- 采集端：30s 播放心跳聚合（零内容，UPSERT 累加） ----

def record_study_heartbeat(
    path: str | Path,
    *,
    course_id: str,
    sub_id: str,
    seconds: int | float | None,
    now: float | None = None,
) -> dict[str, Any]:
    """累加一跳播放秒数到 (本地日, 讲次) 行；返回诚实回执。

    与 watch-events 默认流同型：畸形输入=``recorded=False`` 闭集原因，
    绝不报错不落行；合法跳=UPSERT 累加（同键一行，绝不产生重复行），
    行级 ``DAILY_SECONDS_CAP`` 封顶。写路径即时失效 overview 缓存。
    """
    course = str(course_id or "").strip()
    sub = str(sub_id or "").strip()
    try:
        step = int(seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return {"recorded": False, "reason": "study_heartbeat_invalid"}
    if not course or not sub or not (HEARTBEAT_SECONDS_MIN <= step <= HEARTBEAT_SECONDS_MAX):
        return {"recorded": False, "reason": "study_heartbeat_invalid"}
    now_value = float(time.time() if now is None else now)
    local_date = local_date_text(now_value)
    ensure_study_stats_schema(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            """INSERT INTO study_daily_seconds(local_date,course_id,sub_id,active_seconds,updated_at)
                   VALUES(?,?,?,?,?)
               ON CONFLICT(local_date,sub_id) DO UPDATE SET
                   course_id=excluded.course_id,
                   active_seconds=MIN(study_daily_seconds.active_seconds+excluded.active_seconds,?),
                   updated_at=excluded.updated_at""",
            (local_date, course, sub, step, now_value, DAILY_SECONDS_CAP),
        )
        row = db.execute(
            "SELECT active_seconds FROM study_daily_seconds WHERE local_date=? AND sub_id=?",
            (local_date, sub),
        ).fetchone()
        db.commit()
    _invalidate_overview_cache(path)
    return {
        "recorded": True,
        "local_date": local_date,
        "sub_id": sub,
        "active_seconds": int(row[0]) if row else step,
    }


def clear_study_daily_seconds(path: str | Path, *, course_id: str = "") -> int:
    """擦除通道（同族 delete）：空串清全部，带 course_id 只清该课程。"""
    ensure_study_stats_schema(path)
    with closing(sqlite3.connect(path)) as db:
        if str(course_id or "").strip():
            cursor = db.execute(
                "DELETE FROM study_daily_seconds WHERE course_id=?", (str(course_id),)
            )
        else:
            cursor = db.execute("DELETE FROM study_daily_seconds")
        db.commit()
        deleted = int(cursor.rowcount or 0)
    _invalidate_overview_cache(path)
    return deleted


def _invalidate_overview_cache(path: str | Path) -> None:
    prefix = str(Path(path))
    for key in [key for key in _OVERVIEW_CACHE if key[0] == prefix]:
        _OVERVIEW_CACHE.pop(key, None)


# ---- 读侧概览：周面貌 + 到期待办 + 掌握度 top3（全部聚合查询） ----

def study_overview(path: str | Path, *, now: float | None = None) -> dict[str, Any]:
    """默认层读面（60s 缓存；注入 now 的钉测路径绕过缓存保证确定性）。"""
    now_value = float(time.time() if now is None else now)
    if now is None:
        key = (str(Path(path)), local_date_text(now_value))
        hit = _OVERVIEW_CACHE.get(key)
        if hit and now_value - hit[0] < OVERVIEW_CACHE_TTL_SECONDS:
            return hit[1]
        payload = _study_overview_uncached(path, now_value)
        _OVERVIEW_CACHE[key] = (now_value, payload)
        for stale in [k for k, v in _OVERVIEW_CACHE.items() if now_value - v[0] >= OVERVIEW_CACHE_TTL_SECONDS]:
            _OVERVIEW_CACHE.pop(stale, None)
        return payload
    return _study_overview_uncached(path, now_value)


def _study_overview_uncached(path: str | Path, now_value: float) -> dict[str, Any]:
    ensure_study_stats_schema(path)
    week = _week_dates(now_value)
    # STUDY-STATS-GFIX-1 F2：today 取真实本地日。旧写法 `week[-1] if 本地日 in week else 本地日`
    # 恒真（week 本就由 today 构造）→ payload.today 恒=本周日，today 高亮恒落周日格、
    # 前端 future 态永不可渲染。week 由 today 派生，无需成员判断。
    today_text = local_date_text(now_value)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        seconds_by_day = _seconds_by_day(db, week)
        interactions_by_day = _interactions_by_day(db, week)
        course_count = _week_course_count(db, week)
        avg_completion, lectures_touched = _completion_stats(db)
        first_run = _is_first_run(db, seconds_by_day, interactions_by_day)
        flashcards_due, flashcards_top_course = _flashcards_due(db, now_value)
        next_exam = _next_exam(db, now_value)
        mastery = _mastery_top(db, now_value)
    days = []
    for day_text in week:
        seconds = int(seconds_by_day.get(day_text, 0))
        interactions = int(interactions_by_day.get(day_text, 0))
        days.append({
            "date": day_text,
            "seconds": seconds,
            "interactions": interactions,
            "active": seconds > 0 or interactions > 0,
        })
    study_days = sum(1 for day in days if day["active"])
    total_seconds = int(sum(seconds_by_day.values()))
    minutes_total = max(1, round(total_seconds / 60)) if total_seconds > 0 else None
    return {
        "view": "study_overview",
        "method": STUDY_STATS_VERSION,
        "generated_at": now_value,
        "today": today_text,
        "first_run": first_run,
        "week": {
            "days": days,
            "study_days": study_days,
            "course_count": course_count,
            "avg_completion_percent": avg_completion,
            "minutes_total": minutes_total,
            "lectures_touched": lectures_touched,
        },
        "due": {
            "flashcards_due": flashcards_due,
            "flashcards_top_course_id": flashcards_top_course,
            "next_exam": next_exam,
        },
        "mastery": mastery,
    }


def _seconds_by_day(db: sqlite3.Connection, week: list[str]) -> dict[str, int]:
    rows = db.execute(
        """SELECT local_date, SUM(active_seconds) AS total
             FROM study_daily_seconds WHERE local_date>=? GROUP BY local_date""",
        (week[0],),
    ).fetchall()
    # U1（STUDY-STATS-GATES-1）：行级日帽不约束同日多讲合计，脏数据可现
    # 「26 小时」——日显示层按一天封顶（真实一天不可能超过 86400 秒）。
    return {
        str(row["local_date"]): min(int(row["total"] or 0), DAY_DISPLAY_SECONDS_CAP)
        for row in rows
    }


def _interactions_by_day(db: sqlite3.Connection, week: list[str]) -> dict[str, int]:
    if not _table_exists(db, "watch_events"):
        return {}
    try:
        rows = db.execute(
            """SELECT strftime('%Y-%m-%d', occurred_at, 'unixepoch', 'localtime') AS day,
                      COUNT(*) AS total
                 FROM watch_events WHERE occurred_at>=? GROUP BY day""",
            (time.mktime(time.strptime(week[0], "%Y-%m-%d")),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {
        str(row["day"]): int(row["total"] or 0)
        for row in rows if str(row["day"]) >= week[0]
    }


def _week_course_count(db: sqlite3.Connection, week: list[str]) -> int:
    row = db.execute(
        "SELECT COUNT(DISTINCT course_id) FROM study_daily_seconds WHERE local_date>=?",
        (week[0],),
    ).fetchone()
    return int(row[0] or 0)


def _completion_stats(db: sqlite3.Connection) -> tuple[int | None, int]:
    """平均完成度（看过的讲，percent 与 learning_store 同法，简单平均）。"""
    if not _table_exists(db, "watch_progress"):
        return None, 0
    row = db.execute(
        """SELECT COUNT(*) AS lectures,
                  AVG(CASE WHEN duration_ms>0
                           THEN MIN(100.0, position_ms*100.0/duration_ms)
                           ELSE NULL END) AS avg_percent
             FROM watch_progress"""
    ).fetchone()
    lectures = int(row["lectures"] or 0)
    avg = row["avg_percent"]
    return (round(float(avg)) if avg is not None else None), lectures


def _is_first_run(
    db: sqlite3.Connection,
    seconds_by_day: dict[str, int],
    interactions_by_day: dict[str, int],
) -> bool:
    """首跑零数据：没看过讲、没累计过秒数、没有任何互动事件。"""
    if seconds_by_day or interactions_by_day:
        return False
    for table in ("watch_progress", "watch_events"):
        if _table_exists(db, table):
            row = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            if int(row[0] or 0) > 0:
                return False
    return True


def _flashcards_due(db: sqlite3.Connection, now_value: float) -> tuple[int, str]:
    """跨课程到期闪卡（FSRS 口径同 flashcard_deck：state!='new' 且 due_at<=now）。

    返回 (到期总数, 到期最多的课程 id)——后者供默认层「去复习」一键直达。"""
    if not _table_exists(db, "flash_cards"):
        return 0, ""
    row = db.execute(
        "SELECT COUNT(*) FROM flash_cards WHERE state<>'new' AND due_at<=?",
        (float(now_value),),
    ).fetchone()
    top = db.execute(
        """SELECT course_id FROM flash_cards WHERE state<>'new' AND due_at<=?
           GROUP BY course_id ORDER BY COUNT(*) DESC, course_id LIMIT 1""",
        (float(now_value),),
    ).fetchone()
    return int(row[0] or 0), (str(top[0]) if top else "")


def _next_exam(db: sqlite3.Connection, now_value: float) -> dict[str, Any] | None:
    """最近一个到期待办：考核台账（active/confirmed）与复习计划取更近者。"""
    horizon = now_value + DUE_EXAM_HORIZON_DAYS * 86_400
    candidates: list[dict[str, Any]] = []
    if _table_exists(db, "assessment_events"):
        rows = db.execute(
            """SELECT title, course_id, due_at FROM assessment_events
                WHERE status IN ('active','confirmed') AND due_at IS NOT NULL
                  AND due_at>? AND due_at<=? ORDER BY due_at LIMIT 1""",
            (now_value - 3_600, horizon),
        ).fetchall()
        candidates.extend(
            {"title": str(row["title"]), "course_id": str(row["course_id"]),
             "days_left": max(0, int((float(row["due_at"]) - now_value) // 86_400)),
             "source": "assessment"}
            for row in rows
        )
    if _table_exists(db, "review_plans"):
        rows = db.execute(
            """SELECT title, exam_at FROM review_plans
                WHERE exam_at>? AND exam_at<=? ORDER BY exam_at LIMIT 1""",
            (now_value - 3_600, horizon),
        ).fetchall()
        candidates.extend(
            {"title": str(row["title"]), "course_id": "",
             "days_left": max(0, int((float(row["exam_at"]) - now_value) // 86_400)),
             "source": "review_plan"}
            for row in rows
        )
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item["days_left"], item["title"]))


# ---- 展开层（M2-b）：全部课程掌握度 + 逐讲明细 + FSRS 7 日到期预测 ----

def study_detail(path: str | Path, *, now: float | None = None) -> dict[str, Any]:
    """展开层读面（默认层「看全部课程明细」单入口消费，dialog 一次载入）。

    全聚合查询；讲次集合=学习信号（进度/时长/热点/书签/测验/闪卡）出现过的
    讲——目录里有但还没学过的讲由服务层并目录补充（诚实零行「这讲还没看过」）。
    掌握度条目与默认层 top3 同源（_mastery_courses），另把只有观看进度的课程
    以灰档条目补入（默认层 top3 语义不变：纯观看无测评信号不占行动行）。"""
    now_value = float(time.time() if now is None else now)
    ensure_study_stats_schema(path)
    week_start = _week_dates(now_value)[0]
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        mastery = _mastery_courses(db, now_value)
        forecast = _due_forecast(db, now_value)
        lecture_rows = _lecture_signal_rows(db, now_value)
        week_interactions = _week_interactions_by_course(db, week_start)
    courses: dict[str, dict[str, Any]] = {}
    for entry in mastery:
        courses[entry["course_id"]] = {**entry, "lectures": []}
    for row in lecture_rows:
        course_id = str(row.pop("course_id"))
        course = courses.get(course_id)
        if course is None:
            # 只有观看进度、无任何测评信号的课程：完整面补灰档条目（诚实「数据不足」）。
            course = {
                **_mastery_entry(
                    course_id, now_value=now_value, quiz={}, bookmark={},
                    signal_lectures=0, flash={}, fsrs={},
                ),
                "lectures": [],
            }
            courses[course_id] = course
        course["lectures"].append(row)
    for course in courses.values():
        lectures = course["lectures"]
        percents = [row["percent"] for row in lectures if row["percent"] is not None]
        course["completion_percent"] = round(sum(percents) / len(percents)) if percents else None
        course["week_interactions"] = int(week_interactions.get(course["course_id"], 0))
        lectures.sort(key=lambda row: (str(row["sub_id"]),))
    return {
        "view": "study_detail",
        "method": STUDY_STATS_VERSION,
        "generated_at": now_value,
        "forecast": forecast,
        "courses": sorted(courses.values(), key=lambda item: item["course_id"]),
        "weights": dict(MASTERY_WEIGHTS),
    }


def _due_forecast(db: sqlite3.Connection, now_value: float) -> list[dict[str, Any]]:
    """FSRS 未来 7 天到期预测（今天..+6 天；今天桶含今天之前已积压的到期卡）。"""
    buckets: dict[str, int] = {}
    if _table_exists(db, "flash_cards"):
        horizon_end = _local_midnight_epoch(now_value) + DETAIL_FORECAST_DAYS * 86_400
        rows = db.execute(
            """SELECT date(due_at, 'unixepoch', 'localtime') AS day, COUNT(*) AS total
                 FROM flash_cards WHERE state<>'new' AND due_at<? GROUP BY day""",
            (float(horizon_end),),
        ).fetchall()
        for row in rows:
            day_text = str(row["day"])
            if day_text < _local_day_text(now_value):
                day_text = _local_day_text(now_value)  # 积压并入今天（今天必须先清）
            buckets[day_text] = buckets.get(day_text, 0) + int(row["total"] or 0)
    base_day = date.fromtimestamp(now_value)
    return [
        {
            "date": (base_day + timedelta(days=offset)).isoformat(),
            "due": int(buckets.get((base_day + timedelta(days=offset)).isoformat(), 0)),
        }
        for offset in range(DETAIL_FORECAST_DAYS)
    ]


def _local_day_text(now_value: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now_value))


def _local_midnight_epoch(now_value: float) -> float:
    day = date.fromtimestamp(now_value)
    return time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1))


def _lecture_signal_rows(db: sqlite3.Connection, now_value: float) -> list[dict[str, Any]]:
    """逐讲学习信号行（全聚合查询；无信号的讲不出现在本函数输出里）。"""
    aggregate: dict[tuple[str, str], dict[str, Any]] = {}

    def _row(course_id: str, sub_id: str) -> dict[str, Any]:
        return aggregate.setdefault(
            (str(course_id), str(sub_id)),
            {
                "course_id": str(course_id), "sub_id": str(sub_id),
                "percent": None, "completed": False, "seconds": 0, "replays": 0,
                "open_bookmarks": 0, "quiz_graded": 0, "quiz_correct": 0,
                "quiz_ungraded": 0, "last_correct": None,
                "flashcards_total": 0, "flashcards_due": 0,
            },
        )

    if _table_exists(db, "watch_progress"):
        for row in db.execute(
            """SELECT course_id, sub_id, position_ms, duration_ms, completed FROM watch_progress"""
        ).fetchall():
            entry = _row(row["course_id"], row["sub_id"])
            duration = int(row["duration_ms"] or 0)
            if duration > 0:
                entry["percent"] = min(100, round(int(row["position_ms"] or 0) * 100.0 / duration))
            entry["completed"] = bool(row["completed"])
    if _table_exists(db, "study_daily_seconds"):
        for row in db.execute(
            """SELECT course_id, sub_id, SUM(active_seconds) AS total
                 FROM study_daily_seconds GROUP BY course_id, sub_id"""
        ).fetchall():
            _row(row["course_id"], row["sub_id"])["seconds"] = int(row["total"] or 0)
    if _table_exists(db, "watch_events"):
        placeholders = ",".join("?" for _ in REPLAY_EVENTS)
        for row in db.execute(
            f"""SELECT course_id, sub_id, COUNT(*) AS total FROM watch_events
                 WHERE event IN ({placeholders}) GROUP BY course_id, sub_id""",
            REPLAY_EVENTS,
        ).fetchall():
            _row(row["course_id"], row["sub_id"])["replays"] = int(row["total"] or 0)
    if _table_exists(db, "bookmarks"):
        for row in db.execute(
            """SELECT course_id, sub_id,
                      SUM(CASE WHEN COALESCE(resolution_status,'open')='open' THEN 1 ELSE 0 END) AS open_count
                 FROM bookmarks GROUP BY course_id, sub_id"""
        ).fetchall():
            _row(row["course_id"], row["sub_id"])["open_bookmarks"] = int(row["open_count"] or 0)
    if _table_exists(db, "quiz_attempts") and _table_exists(db, "quiz_items"):
        for row in db.execute(
            """SELECT qi.course_id AS course_id, qi.sub_id AS sub_id,
                      a.correct AS correct, a.created_at AS created_at
                 FROM quiz_attempts a JOIN quiz_items qi ON qi.quiz_id=a.quiz_id"""
        ).fetchall():
            entry = _row(row["course_id"], row["sub_id"])
            if row["correct"] is None:
                entry["quiz_ungraded"] += 1
                continue
            entry["quiz_graded"] += 1
            entry["quiz_correct"] += 1 if int(row["correct"]) == 1 else 0
            created = float(row["created_at"] or 0.0)
            last = entry["_last_graded_at"] if isinstance(entry.get("_last_graded_at"), float) else -1.0
            if created >= last:
                entry["_last_graded_at"] = created
                entry["last_correct"] = bool(int(row["correct"]) == 1)
    if _table_exists(db, "flash_cards"):
        for row in db.execute(
            """SELECT course_id, sub_id, COUNT(*) AS total,
                      SUM(CASE WHEN state<>'new' AND due_at<=? THEN 1 ELSE 0 END) AS due
                 FROM flash_cards GROUP BY course_id, sub_id""",
            (float(now_value),),
        ).fetchall():
            entry = _row(row["course_id"], row["sub_id"])
            entry["flashcards_total"] = int(row["total"] or 0)
            entry["flashcards_due"] = int(row["due"] or 0)
    for entry in aggregate.values():
        entry.pop("_last_graded_at", None)
    return list(aggregate.values())


def _week_interactions_by_course(db: sqlite3.Connection, week_start: str) -> dict[str, int]:
    if not _table_exists(db, "watch_events"):
        return {}
    try:
        rows = db.execute(
            """SELECT course_id, COUNT(*) AS total FROM watch_events
                WHERE occurred_at>=? GROUP BY course_id""",
            (time.mktime(time.strptime(week_start, "%Y-%m-%d")),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(row["course_id"]): int(row["total"] or 0) for row in rows}


# ---- 掌握度（M2 完整三路诚实合成：五档闭集，只引用计数） ----

def _mastery_top(db: sqlite3.Connection, now_value: float) -> dict[str, Any]:
    """默认层 top3 摘要（行动行优先 + 灰档补位；完整课程面归 study_detail）。"""
    courses = _mastery_courses(db, now_value)
    action_rows = sorted(
        (item for item in courses if item["tier"] in MASTERY_ACTION_TIERS),
        key=lambda item: (round(item["score"], 4), item["course_id"]),
    )
    gray_rows = sorted(
        (item for item in courses if item["tier"] == "数据不足"),
        key=lambda item: item["course_id"],
    )
    top = (action_rows + gray_rows)[:MASTERY_TOP_MAX]
    solid_rows = [item for item in courses if item["tier"] in ("一般", "扎实")]
    all_clear = bool(solid_rows) and not action_rows and not gray_rows
    return {
        "all_clear": all_clear,
        "course_count": len(courses),
        "courses": top,
        "weights": dict(MASTERY_WEIGHTS),
    }


def _mastery_courses(db: sqlite3.Connection, now_value: float) -> list[dict[str, Any]]:
    """全部有信号课程的完整五档合成条目（默认层 top3 与展开层共用一次计算）。"""
    quiz_by_course = _quiz_by_course(db, now_value)
    bookmark_by_course = _bookmark_by_course(db)
    signal_by_course = _signal_lectures_by_course(db)
    flash_by_course, fsrs_by_course = _flash_by_course(db, now_value)
    touched = sorted(
        set(quiz_by_course) | set(bookmark_by_course) | set(flash_by_course)
    )
    return [
        _mastery_entry(
            str(course_id),
            now_value=now_value,
            quiz=quiz_by_course.get(str(course_id), {}),
            bookmark=bookmark_by_course.get(str(course_id), {}),
            signal_lectures=int(signal_by_course.get(str(course_id), 0)),
            flash=flash_by_course.get(str(course_id), {}),
            fsrs=fsrs_by_course.get(str(course_id), {}),
        )
        for course_id in touched
    ]


def _quiz_by_course(db: sqlite3.Connection, now_value: float) -> dict[str, dict[str, Any]]:
    """测验路原始量：逐条已判作答（correct, created_at）按课程聚合。

    未判对错（correct IS NULL）不入分子分母，只单报 attempts-graded 差值。"""
    if not _table_exists(db, "quiz_attempts") or not _table_exists(db, "quiz_items"):
        return {}
    rows = db.execute(
        """SELECT qi.course_id AS course_id, a.correct AS correct, a.created_at AS created_at
             FROM quiz_attempts a JOIN quiz_items qi ON qi.quiz_id=a.quiz_id"""
    ).fetchall()
    by_course: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_course.setdefault(str(row["course_id"]), {"graded_rows": [], "attempts": 0})
        entry["attempts"] += 1
        if row["correct"] is not None:
            entry["graded_rows"].append((int(row["correct"]), float(row["created_at"] or 0.0)))
    return by_course


def _bookmark_by_course(db: sqlite3.Connection) -> dict[str, dict[str, int]]:
    if not _table_exists(db, "bookmarks"):
        return {}
    rows = db.execute(
        """SELECT course_id,
                  COUNT(*) AS total,
                  SUM(CASE WHEN COALESCE(resolution_status,'open')='open' THEN 1 ELSE 0 END) AS open_count
             FROM bookmarks GROUP BY course_id"""
    ).fetchall()
    return {
        str(row["course_id"]): {
            "total": int(row["total"] or 0),
            "open": int(row["open_count"] or 0),
        }
        for row in rows
    }


def _signal_lectures_by_course(db: sqlite3.Connection) -> dict[str, int]:
    """书签路分母：有学习信号（观看进度或闪卡）的讲数。"""
    if not _table_exists(db, "watch_progress") and not _table_exists(db, "flash_cards"):
        return {}
    counts: dict[str, int] = {}
    if _table_exists(db, "watch_progress"):
        for row in db.execute(
            "SELECT course_id, COUNT(DISTINCT sub_id) AS lectures FROM watch_progress GROUP BY course_id"
        ).fetchall():
            counts[str(row["course_id"])] = int(row["lectures"] or 0)
    if _table_exists(db, "flash_cards"):
        for row in db.execute(
            "SELECT course_id, COUNT(DISTINCT sub_id) AS lectures FROM flash_cards GROUP BY course_id"
        ).fetchall():
            course_id = str(row["course_id"])
            counts[course_id] = max(counts.get(course_id, 0), int(row["lectures"] or 0))
    return counts


def _flash_by_course(
    db: sqlite3.Connection, now_value: float
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, Any]]]:
    """闪卡计数面 + FSRS 路原始量（已复习卡的 stability/last_review_at）。

    FSRS 路只吃真实记忆态卡（state<>'new' 且 last_review_at>0 且 stability>0）：
    稳定度非正=调度尚未产生有效记忆估计，按该路无数据处理（诚实 abstain，
    绝不拿 0 稳定度硬算出「全忘了」的假信号）。"""
    if not _table_exists(db, "flash_cards"):
        return {}, {}
    rows = db.execute(
        """SELECT course_id,
                  COUNT(*) AS total,
                  SUM(CASE WHEN state<>'new' THEN 1 ELSE 0 END) AS scheduled,
                  SUM(CASE WHEN state<>'new' AND due_at<=? THEN 1 ELSE 0 END) AS due
             FROM flash_cards GROUP BY course_id""",
        (float(now_value),),
    ).fetchall()
    by_course = {
        str(row["course_id"]): {
            "total": int(row["total"] or 0),
            "scheduled": int(row["scheduled"] or 0),
            "due": int(row["due"] or 0),
        }
        for row in rows
    }
    fsrs_rows = db.execute(
        """SELECT course_id, stability, last_review_at FROM flash_cards
            WHERE state<>'new' AND last_review_at>0 AND stability>0"""
    ).fetchall()
    fsrs_by_course: dict[str, dict[str, Any]] = {}
    for row in fsrs_rows:
        entry = fsrs_by_course.setdefault(str(row["course_id"]), {"cards": []})
        entry["cards"].append((float(row["stability"]), float(row["last_review_at"])))
    return by_course, fsrs_by_course


def _quiz_decay_score(graded_rows: list[tuple[int, float]], now_value: float) -> float:
    """时间衰减加权正确率：权重=2^(-Δ天/14)。越新的作答越能代表现在。"""
    weight_sum = 0.0
    correct_sum = 0.0
    for correct, created_at in graded_rows:
        age_days = max(0.0, (now_value - float(created_at)) / 86_400.0)
        weight = math.pow(2.0, -age_days / MASTERY_QUIZ_HALF_LIFE_DAYS)
        weight_sum += weight
        correct_sum += weight * (1.0 if correct == 1 else 0.0)
    if weight_sum <= 0.0:
        return 0.0
    return correct_sum / weight_sum


def _fsrs_score(fsrs: dict[str, Any], flash: dict[str, int], now_value: float) -> float:
    """FSRS 路：已复习卡遗忘曲线均值 ×(1-到期积压率)。

    积压率=到期卡/已调度卡（到期 ⊆ 已调度，天然 [0,1]）。"""
    cards = fsrs.get("cards") or []
    if not cards:
        return 0.0
    retrievability_sum = 0.0
    for stability, last_review_at in cards:
        elapsed_days = max(0.0, (now_value - last_review_at) / 86_400.0)
        retrievability_sum += fsrs_retrievability(elapsed_days, stability)
    mean_retrievability = retrievability_sum / len(cards)
    scheduled = int(flash.get("scheduled", 0))
    backlog_rate = 0.0
    if scheduled > 0:
        backlog_rate = min(1.0, int(flash.get("due", 0)) / scheduled)
    return mean_retrievability * (1.0 - backlog_rate)


def _mastery_entry(
    course_id: str,
    *,
    now_value: float,
    quiz: dict[str, Any],
    bookmark: dict[str, int],
    signal_lectures: int,
    flash: dict[str, int],
    fsrs: dict[str, Any],
) -> dict[str, Any]:
    """单课程五档诚实合成（证据句只引用计数，绝不引用内容文本）。

    诚实优先级：全无数据=灰档；单路=出档但 basis/partial 标注「仅基于 X」；
    多路=权重加权；矛盾证据（测验高分×书签堆积）不平均=保守档待巩固。"""
    graded_rows = quiz.get("graded_rows") or []
    ungraded = int(quiz.get("attempts", 0)) - len(graded_rows)
    quiz_score: float | None = None
    if len(graded_rows) >= MASTERY_MIN_SAMPLES["quiz"]:
        quiz_score = _quiz_decay_score(graded_rows, now_value)
    bookmark_score: float | None = None
    open_count = int(bookmark.get("open", 0))
    if int(bookmark.get("total", 0)) >= MASTERY_MIN_SAMPLES["bookmark"]:
        # 密度=未解决书签/有学习信号的讲数（分母 0 时按 1 讲兜底），取 1-密度钳 0。
        density = open_count / max(1, signal_lectures)
        bookmark_score = max(0.0, 1.0 - density)
    fsrs_score: float | None = None
    if len(fsrs.get("cards") or []) >= MASTERY_MIN_SAMPLES["fsrs"]:
        fsrs_score = _fsrs_score(fsrs, flash, now_value)

    present = {
        "quiz": quiz_score,
        "bookmark": bookmark_score,
        "fsrs": fsrs_score,
    }
    active = {path: score for path, score in present.items() if score is not None}
    due_count = int(flash.get("due", 0))
    contradiction = (
        quiz_score is not None and quiz_score >= 0.8
        and open_count >= MASTERY_CONTRADICTION_BOOKMARKS
    )

    if not active:
        return {
            "course_id": course_id,
            "tier": "数据不足",
            "gray": True,
            "score": None,
            "basis": [],
            "partial": False,
            "paths": {},
            "evidence": "学习信号还太少，暂时看不出掌握情况",
        }

    basis = sorted(active, key=lambda path: (-MASTERY_WEIGHTS[path], path))
    weight_total = sum(MASTERY_WEIGHTS[path] for path in active)
    score = sum(MASTERY_WEIGHTS[path] * value for path, value in active.items()) / weight_total

    if contradiction:
        tier = "待巩固"
        evidence = f"测得不错，但有 {open_count} 处没听懂标记还没解决"
    elif score >= 0.9:
        tier = "扎实"
        evidence = _weakest_evidence(active, ungraded, open_count, due_count)
    elif score >= 0.7:
        tier = "一般"
        evidence = _weakest_evidence(active, ungraded, open_count, due_count)
    elif score >= 0.4:
        tier = "待巩固"
        evidence = _weakest_evidence(active, ungraded, open_count, due_count)
    else:
        tier = "薄弱"
        evidence = _weakest_evidence(active, ungraded, open_count, due_count)
    # 到期积压是直接的行动信号：任何有数据课程积压过线即不低于「待巩固」。
    if tier in ("一般", "扎实") and due_count >= MASTERY_DUE_BACKLOG_TIER:
        tier = "待巩固"
        evidence = f"到期闪卡积压 {due_count} 张，先清一批再学新的"
    return {
        "course_id": course_id,
        "tier": tier,
        "gray": False,
        "score": round(score, 4),
        "basis": basis,
        "partial": len(basis) == 1,
        "paths": {
            path: {"n": _path_sample_size(path, quiz, bookmark, fsrs), "score": round(value, 4)}
            for path, value in active.items()
        },
        "evidence": evidence,
    }


def _path_sample_size(
    path: str, quiz: dict[str, Any], bookmark: dict[str, int], fsrs: dict[str, Any]
) -> int:
    if path == "quiz":
        return len(quiz.get("graded_rows") or [])
    if path == "bookmark":
        return int(bookmark.get("total", 0))
    return len(fsrs.get("cards") or [])


def _weakest_evidence(
    active: dict[str, float], ungraded: int, open_count: int, due_count: int
) -> str:
    """取最弱一路给一句行动建议（同分按 quiz→bookmark→fsrs 行动优先序）。

    只引用计数/档位事实，不引用内容文本；措辞随强弱分级，扎实档不说教。
    P1（STUDY-STATS-GATES-1）：书签路全解决（open=0）不构成弱点——顺延次弱路，
    无路可说时给中性收束句，绝不输出「0 处没听懂还没解决」空建议。"""
    if not active:
        return ""
    order = {"quiz": 0, "bookmark": 1, "fsrs": 2}
    candidates = {
        path: score for path, score in active.items()
        if not (path == "bookmark" and open_count <= 0)
    }
    if not candidates:
        return "已有的学习记录都解决了，保持这个节奏"
    weakest, score = min(candidates.items(), key=lambda item: (round(item[1], 4), order[item[0]]))
    if weakest == "quiz":
        if score >= 0.8:
            text = "最近测验对题率不错，保持这个节奏"
        else:
            text = "最近测验对题率偏低，错题值得回看一遍"
        if ungraded > 0:
            text += f"，另有 {ungraded} 题还没判对错"
        return text
    if weakest == "bookmark":
        return f"{open_count} 处没听懂标记还没解决，趁记得去弄懂"
    return f"到期闪卡积压 {due_count} 张，先清一批再学新的"


# ---- 导出（M3）：JSON 结构化全量 + CSV×2（全部读侧聚合，零内容文本） ----

def study_daily_export_rows(db: sqlite3.Connection, now_value: float) -> list[dict[str, Any]]:
    """逐日×课程聚合行（全历史；行数有界=天数×课程数）。"""
    del now_value  # 全历史口径；保留形参稳定合同
    seconds: dict[tuple[str, str], int] = {}
    if _table_exists(db, "study_daily_seconds"):
        for row in db.execute(
            """SELECT local_date, course_id, SUM(active_seconds) AS total
                 FROM study_daily_seconds GROUP BY local_date, course_id"""
        ).fetchall():
            seconds[(str(row["local_date"]), str(row["course_id"]))] = min(
                int(row["total"] or 0), DAY_DISPLAY_SECONDS_CAP
            )
    interactions: dict[tuple[str, str], int] = {}
    if _table_exists(db, "watch_events"):
        try:
            for row in db.execute(
                """SELECT strftime('%Y-%m-%d', occurred_at, 'unixepoch', 'localtime') AS day,
                          course_id, COUNT(*) AS total
                     FROM watch_events GROUP BY day, course_id"""
            ).fetchall():
                key = (str(row["day"]), str(row["course_id"]))
                interactions[key] = interactions.get(key, 0) + int(row["total"] or 0)
        except sqlite3.OperationalError:
            pass
    merged = sorted(set(seconds) | set(interactions))
    return [
        {
            "date": day,
            "course_id": course_id,
            "seconds": seconds.get((day, course_id), 0),
            "interactions": interactions.get((day, course_id), 0),
        }
        for day, course_id in merged
    ]


def _csv_bytes(rows: list[dict[str, Any]], header: list[str]) -> str:
    """CSV 文本（CRLF 行距 Excel 直开友好；外层落盘用 utf-8-sig 带 BOM）。"""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def build_study_stats_export(
    path: str | Path,
    *,
    now: float | None = None,
    course_titles: dict[str, str] | None = None,
    lecture_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """一次导出=三个文件文本（JSON 全量 + daily.csv + lectures.csv）。

    脱敏合同（course-data 既有导出同族）：课程/讲次只用目录标签与计数，
    书签 note、检索词、字幕/总结文本等任何内容字段永不进导出——由本函数
    只构造白名单列保证（钉测以敏感种子串不存在为证）。课程标题与讲次标签
    （目录 date+sub_title 元数据）由调用方供给，目录缺失时诚实留空。"""
    now_value = float(time.time() if now is None else now)
    titles = {str(key): str(value) for key, value in (course_titles or {}).items()}
    labels = {str(key): str(value) for key, value in (lecture_labels or {}).items()}
    date_stamp = time.strftime("%Y%m%d", time.localtime(now_value))
    ensure_study_stats_schema(path)
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        daily_rows = study_daily_export_rows(db, now_value)
    overview = study_overview(path, now=now_value)
    detail = study_detail(path, now=now_value)

    json_payload = {
        "schema": STUDY_STATS_EXPORT_SCHEMA,
        "method": STUDY_STATS_VERSION,
        "generated_at": now_value,
        "method_constants": {
            "weights": dict(MASTERY_WEIGHTS),
            "min_samples": dict(MASTERY_MIN_SAMPLES),
            "tiers": list(MASTERY_TIERS),
            "quiz_half_life_days": MASTERY_QUIZ_HALF_LIFE_DAYS,
            "forecast_days": DETAIL_FORECAST_DAYS,
        },
        "overview": overview,
        "detail": detail,
        "daily": daily_rows,
    }

    daily_csv_rows = [
        {
            "date": row["date"],
            "course_id": row["course_id"],
            "course_title": titles.get(row["course_id"], ""),
            "seconds": row["seconds"],
            "interactions": row["interactions"],
        }
        for row in daily_rows
    ]
    lectures_csv_rows = []
    tier_by_course = {entry["course_id"]: entry["tier"] for entry in detail.get("courses") or []}
    for course in detail.get("courses") or []:
        course_id = str(course.get("course_id") or "")
        for row in course.get("lectures") or []:
            lectures_csv_rows.append({
                "course_id": course_id,
                "course_title": titles.get(course_id, ""),
                "sub_id": row.get("sub_id", ""),
                "lecture_label": row.get("label", "") or labels.get(str(row.get("sub_id") or ""), ""),
                "percent": "" if row.get("percent") is None else row.get("percent"),
                "completed": 1 if row.get("completed") else 0,
                "seconds": row.get("seconds", 0),
                "replays": row.get("replays", 0),
                "open_bookmarks": row.get("open_bookmarks", 0),
                "quiz_graded": row.get("quiz_graded", 0),
                "quiz_correct": row.get("quiz_correct", 0),
                "quiz_ungraded": row.get("quiz_ungraded", 0),
                "last_correct": "" if row.get("last_correct") is None else (1 if row.get("last_correct") else 0),
                "flashcards_due": row.get("flashcards_due", 0),
                "mastery_tier": tier_by_course.get(course_id, ""),
            })
    daily_header = ["date", "course_id", "course_title", "seconds", "interactions"]
    lectures_header = [
        "course_id", "course_title", "sub_id", "lecture_label", "percent", "completed",
        "seconds", "replays", "open_bookmarks", "quiz_graded", "quiz_correct",
        "quiz_ungraded", "last_correct", "flashcards_due", "mastery_tier",
    ]
    files = [
        {
            "filename": STUDY_STATS_EXPORT_JSON.format(date=date_stamp),
            "content": json.dumps(json_payload, ensure_ascii=False, indent=2),
        },
        {
            "filename": STUDY_STATS_EXPORT_DAILY_CSV.format(date=date_stamp),
            "content": _csv_bytes(daily_csv_rows, daily_header),
        },
        {
            "filename": STUDY_STATS_EXPORT_LECTURES_CSV.format(date=date_stamp),
            "content": _csv_bytes(lectures_csv_rows, lectures_header),
        },
    ]
    return {
        "schema": STUDY_STATS_EXPORT_SCHEMA,
        "method": STUDY_STATS_VERSION,
        "directory": STUDY_STATS_EXPORT_DIRNAME,
        "files": files,
    }


__all__ = [
    "DAILY_SECONDS_CAP", "DAY_DISPLAY_SECONDS_CAP", "HEARTBEAT_SECONDS_DEFAULT",
    "HEARTBEAT_SECONDS_MAX", "HEARTBEAT_SECONDS_MIN", "MASTERY_MIN_SAMPLES",
    "MASTERY_PATH_LABELS", "MASTERY_TIERS", "MASTERY_WEIGHTS",
    "OVERVIEW_CACHE_TTL_SECONDS", "STUDY_STATS_EXPORT_DIRNAME",
    "STUDY_STATS_EXPORT_SCHEMA", "STUDY_STATS_VERSION",
    "build_study_stats_export", "clear_study_daily_seconds",
    "ensure_study_stats_schema", "local_date_text", "record_study_heartbeat",
    "study_detail", "study_overview",
]
