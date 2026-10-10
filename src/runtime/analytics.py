"""Opt-in, local-only learning analytics."""

from __future__ import annotations

import re
import sqlite3
import time
import uuid
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from .sqlite_utils import connect_learning_db

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment,misc]


ANALYTICS_VERSION = "local-analytics-v1"

# AIRESEARCH 埋点（TELEMETRY-H64-1 计数面 + P3-CONTRACT-1 §⑤ kind 扩展）：
# 总结页引用点击 × 停留时长（H6）与深度回答引用点击（H1）。闭集事件种类——
# 只数事件、零内容落盘；palette 发射端已随 P3-PKGC 上线（本注册表即
# study-events 端点的校验闭集），summary 页发射仍属后续遥测合同。
STUDY_EVENT_KINDS = ("summary_citation_click", "summary_dwell", "palette_citation_click")
# click 类种类集合（P3 §⑤ 分诊集合化）：click 类 dwell_ms 一律归零，其余按
# 停留时长口径钳制。新增 click kind 必须同步进本集合，否则会被误计入驻留账。
_STUDY_CLICK_KINDS = frozenset({"summary_citation_click", "palette_citation_click"})
# 停留时长闭集帽：单次上报超过 4 小时按 4 小时收口（抗脏数据，不追真实上限）。
_STUDY_DWELL_MS_MAX = 14_400_000


def ensure_analytics_schema(path: str | Path) -> None:
    with closing(connect_learning_db(path)) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS analytics_settings_v3 (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                enabled INTEGER NOT NULL DEFAULT 0,
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                consented_at REAL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watch_events_v3 (
                event_id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL,
                sub_id TEXT NOT NULL,
                term TEXT NOT NULL DEFAULT '',
                position_ms INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                playback_rate REAL NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                occurred_at REAL NOT NULL,
                version TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_watch_events_v3_time ON watch_events_v3(occurred_at);
            CREATE INDEX IF NOT EXISTS idx_watch_events_v3_term ON watch_events_v3(term,course_id,sub_id,occurred_at);
            CREATE TABLE IF NOT EXISTS study_events_v3 (
                event_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('summary_citation_click','summary_dwell','palette_citation_click')),
                course_id TEXT NOT NULL,
                sub_id TEXT NOT NULL,
                dwell_ms INTEGER NOT NULL DEFAULT 0,
                occurred_at REAL NOT NULL,
                version TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_study_events_v3_kind ON study_events_v3(kind,course_id,sub_id,occurred_at);
            """
        )
        # P3 §⑤ kind 扩展的就地迁移：TELEMETRY-H64 时代建的存量库 CHECK 闭集
        # 没有 palette_citation_click，直接 INSERT 会被 CHECK 拒绝——按
        # sqlite_master 实检触发重建，行原样搬运（study_events 本就只计数零
        # 内容）。新库/已迁移库检测不命中=零动作；重复执行幂等。
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='study_events_v3'"
        ).fetchone()
        if row and "palette_citation_click" not in str(row[0] or ""):
            db.executescript(
                """
                CREATE TABLE study_events_v3_rebuild (
                    event_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK(kind IN ('summary_citation_click','summary_dwell','palette_citation_click')),
                    course_id TEXT NOT NULL,
                    sub_id TEXT NOT NULL,
                    dwell_ms INTEGER NOT NULL DEFAULT 0,
                    occurred_at REAL NOT NULL,
                    version TEXT NOT NULL
                );
                INSERT INTO study_events_v3_rebuild
                    SELECT event_id,kind,course_id,sub_id,dwell_ms,occurred_at,version
                    FROM study_events_v3;
                DROP TABLE study_events_v3;
                ALTER TABLE study_events_v3_rebuild RENAME TO study_events_v3;
                CREATE INDEX IF NOT EXISTS idx_study_events_v3_kind
                    ON study_events_v3(kind,course_id,sub_id,occurred_at);
                """
            )
        db.execute(
            """INSERT OR IGNORE INTO analytics_settings_v3(singleton,enabled,timezone,updated_at)
               VALUES(1,0,'Asia/Shanghai',?)""",
            (time.time(),),
        )
        db.commit()


def analytics_settings(path: str | Path) -> dict[str, Any]:
    with closing(connect_learning_db(path)) as db:
        db.row_factory = sqlite3.Row
        try:
            row = db.execute("SELECT * FROM analytics_settings_v3 WHERE singleton=1").fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            row = None
    if row is None:
        ensure_analytics_schema(path)
        with closing(connect_learning_db(path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM analytics_settings_v3 WHERE singleton=1").fetchone()
    if row is None:
        raise RuntimeError("analytics settings initialization failed")
    return {
        "enabled": bool(row["enabled"]), "timezone": str(row["timezone"]),
        "consented_at": row["consented_at"], "updated_at": float(row["updated_at"]),
        "version": ANALYTICS_VERSION,
    }


def set_analytics_enabled(path: str | Path, enabled: bool, *, timezone: str = "Asia/Shanghai") -> dict[str, Any]:
    ensure_analytics_schema(path)
    if ZoneInfo is not None:
        try:
            ZoneInfo(str(timezone))
        except Exception as exc:
            raise ValueError("analytics_timezone_invalid") from exc
    now = time.time()
    with closing(connect_learning_db(path)) as db:
        db.execute(
            """UPDATE analytics_settings_v3 SET enabled=?,timezone=?,
                       consented_at=CASE WHEN ?=1 THEN COALESCE(consented_at,?) ELSE consented_at END,
                       updated_at=? WHERE singleton=1""",
            (int(bool(enabled)), str(timezone), int(bool(enabled)), now, now),
        )
        db.commit()
    return analytics_settings(path)


def record_watch_event(
    path: str | Path,
    *,
    course_id: str,
    sub_id: str,
    term: str,
    position_seconds: float,
    duration_seconds: float,
    playback_rate: float,
    completed: bool,
    occurred_at: float | None = None,
) -> bool:
    settings = analytics_settings(path)
    if not settings["enabled"]:
        return False
    now = float(occurred_at or time.time())
    position_ms = max(0, int(float(position_seconds or 0) * 1000))
    duration_ms = max(position_ms, int(float(duration_seconds or 0) * 1000))
    event_id = uuid.uuid4().hex
    with closing(connect_learning_db(path)) as db:
        prior = db.execute(
            "SELECT position_ms,occurred_at FROM watch_events_v3 WHERE sub_id=? ORDER BY occurred_at DESC LIMIT 1",
            (str(sub_id),),
        ).fetchone()
        if prior and now - float(prior[1]) < 2.0 and abs(position_ms - int(prior[0])) < 2000:
            return False
        db.execute(
            """INSERT INTO watch_events_v3(
                event_id,course_id,sub_id,term,position_ms,duration_ms,playback_rate,
                completed,occurred_at,version
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id, str(course_id), str(sub_id), str(term), position_ms, duration_ms,
                max(0.25, min(4.0, float(playback_rate or 1))), int(bool(completed)), now,
                ANALYTICS_VERSION,
            ),
        )
        db.commit()
    return True


def delete_analytics_term(path: str | Path, term: str) -> int:
    ensure_analytics_schema(path)
    with closing(connect_learning_db(path)) as db:
        cursor = db.execute("DELETE FROM watch_events_v3 WHERE term=?", (str(term),))
        db.commit()
        return int(cursor.rowcount or 0)


def record_study_event(
    path: str | Path,
    *,
    kind: str,
    course_id: str,
    sub_id: str,
    dwell_ms: int = 0,
    occurred_at: float | None = None,
) -> bool:
    """AIRESEARCH H6 埋点：总结页引用点击/停留时长计数（闭集种类，零内容）。

    与 ``record_watch_event`` 同一口径：opt-in 门（analytics 未开启=不记，
    返回 False 不报错——遥测绝不挡学生主链）；本地 sqlite、可擦除、版本化。
    kind 必须落在 ``STUDY_EVENT_KINDS`` 闭集内，越界=契约错误抛
    ``ValueError("study_event_kind_invalid")``（调用方是自家闭集端点，内部
    误用要响）；click 类种类（``_STUDY_CLICK_KINDS``，P3 §⑤ 分诊集合化）的
    dwell_ms 一律归零，其余按停留时长口径钳制收口。
    """
    kind = str(kind or "").strip()
    if kind not in STUDY_EVENT_KINDS:
        raise ValueError("study_event_kind_invalid")
    settings = analytics_settings(path)
    if not settings["enabled"]:
        return False
    course_id = str(course_id or "").strip()
    sub_id = str(sub_id or "").strip()
    if not course_id or not sub_id:
        raise ValueError("study_event_field_invalid")
    if kind in _STUDY_CLICK_KINDS:
        dwell = 0
    else:
        try:
            dwell = max(0, min(_STUDY_DWELL_MS_MAX, int(dwell_ms)))
        except (TypeError, ValueError):
            dwell = 0
    now = float(occurred_at or time.time())
    with closing(connect_learning_db(path)) as db:
        db.execute(
            """INSERT INTO study_events_v3(
                event_id,kind,course_id,sub_id,dwell_ms,occurred_at,version
            ) VALUES(?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, kind, course_id, sub_id, dwell, now, ANALYTICS_VERSION),
        )
        db.commit()
    return True


def study_telemetry_summary(path: str | Path) -> dict[str, Any]:
    """H6 观测量读出：引用点击数 × 停留时长按讲次（sub_id）并置。

    相关性观测的join 基表——同一 sub_id 的 ``citation_clicks`` 与
    ``dwell_ms_total`` 并排可比。H6 既有键只统计 ``summary_*`` kind（口径
    不串：palette 面的行不进 H6 账）；加性 ``palette`` 节（P3 §⑤）单列
    深度回答引用点击。opt-in 门与 ``analytics_summary`` 一致：未开启时
    summary=None 且带闭集 reason，不泄任何计数。
    """
    settings = analytics_settings(path)
    if not settings["enabled"]:
        return {"settings": settings, "summary": None, "reason": "analytics_disabled"}
    with closing(connect_learning_db(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT kind,course_id,sub_id,dwell_ms,occurred_at FROM study_events_v3 ORDER BY occurred_at"
        ).fetchall()
    by_sub: dict[tuple[str, str], dict[str, int]] = {}
    palette_by_sub: dict[tuple[str, str], int] = {}
    citation_clicks = 0
    dwell_sessions = 0
    dwell_ms_total = 0
    palette_clicks = 0
    summary_event_count = 0
    latest = 0.0
    for row in rows:
        kind = str(row["kind"])
        key = (str(row["course_id"]), str(row["sub_id"]))
        if kind.startswith("summary_"):
            summary_event_count += 1
            latest = max(latest, float(row["occurred_at"]))
            bucket = by_sub.setdefault(
                key, {"citation_clicks": 0, "dwell_sessions": 0, "dwell_ms_total": 0}
            )
            if kind in _STUDY_CLICK_KINDS:
                citation_clicks += 1
                bucket["citation_clicks"] += 1
            else:
                dwell_sessions += 1
                dwell_ms_total += int(row["dwell_ms"] or 0)
                bucket["dwell_sessions"] += 1
                bucket["dwell_ms_total"] += int(row["dwell_ms"] or 0)
        elif kind in _STUDY_CLICK_KINDS:
            palette_clicks += 1
            palette_by_sub[key] = palette_by_sub.get(key, 0) + 1
    return {
        "settings": settings,
        "summary": {
            "event_count": summary_event_count,
            "citation_clicks": citation_clicks,
            "dwell_sessions": dwell_sessions,
            "dwell_ms_total": dwell_ms_total,
            "by_sub": [
                {"course_id": course, "sub_id": sub, **bucket}
                for (course, sub), bucket in sorted(
                    by_sub.items(),
                    key=lambda item: (
                        -(item[1]["citation_clicks"] + item[1]["dwell_sessions"]),
                        item[0],
                    ),
                )
            ],
            "latest_at": latest,
            "palette": {
                "citation_clicks": palette_clicks,
                "by_sub": [
                    {"course_id": course, "sub_id": sub, "citation_clicks": clicks}
                    for (course, sub), clicks in sorted(
                        palette_by_sub.items(), key=lambda item: (-item[1], item[0])
                    )
                ],
            },
        },
    }


def delete_study_events(path: str | Path) -> int:
    """H6 计数可擦除：清空 study_events_v3（与 watch 事件擦除同一隐私口径）。"""
    ensure_analytics_schema(path)
    with closing(connect_learning_db(path)) as db:
        cursor = db.execute("DELETE FROM study_events_v3")
        db.commit()
        return int(cursor.rowcount or 0)


def _watched_seconds(rows: list[sqlite3.Row]) -> tuple[float, dict[str, int]]:
    total = 0.0
    revisits: Counter[str] = Counter()
    last_by_sub: dict[str, sqlite3.Row] = {}
    last_bucket: dict[str, int] = {}
    for row in rows:
        sub_id = str(row["sub_id"])
        bucket = int(row["position_ms"]) // 300_000
        if sub_id in last_bucket and bucket < last_bucket[sub_id]:
            revisits[f"{sub_id}:{bucket}"] += 1
        last_bucket[sub_id] = bucket
        prior = last_by_sub.get(sub_id)
        last_by_sub[sub_id] = row
        if prior is None:
            continue
        elapsed = float(row["occurred_at"]) - float(prior["occurred_at"])
        position_delta = int(row["position_ms"]) - int(prior["position_ms"])
        if 0 < elapsed <= 90 and -3000 <= position_delta <= 240_000:
            total += min(elapsed, max(0.0, position_delta / max(0.25, float(row["playback_rate"])) / 1000.0 + 2.0))
    return total, dict(revisits)


def analytics_summary(path: str | Path, *, term: str = "") -> dict[str, Any]:
    settings = analytics_settings(path)
    if not settings["enabled"]:
        return {"settings": settings, "summary": None, "reason": "analytics_disabled"}
    where = " WHERE term=?" if term else ""
    params = [str(term)] if term else []
    with closing(connect_learning_db(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            f"SELECT * FROM watch_events_v3{where} ORDER BY occurred_at,event_id", params
        ).fetchall()
    watched, revisits = _watched_seconds(rows)
    timezone_name = str(settings["timezone"])
    tz = ZoneInfo(timezone_name) if ZoneInfo is not None else None
    hours: Counter[int] = Counter()
    speeds: Counter[str] = Counter()
    courses: Counter[str] = Counter()
    completed: set[str] = set()
    latest = 0.0
    for row in rows:
        dt = datetime.fromtimestamp(float(row["occurred_at"]), tz=tz)
        hours[dt.hour] += 1
        speeds[f"{float(row['playback_rate']):g}x"] += 1
        courses[str(row["course_id"])] += 1
        if bool(row["completed"]):
            completed.add(str(row["sub_id"]))
        latest = max(latest, float(row["occurred_at"]))
    top_revisits = [
        {"sub_id": key.split(":", 1)[0], "bucket": int(key.split(":", 1)[1]), "revisit_count": count}
        for key, count in sorted(revisits.items(), key=lambda item: (-item[1], item[0]))[:10]
    ]
    return {
        "settings": settings,
        "summary": {
            "term": str(term), "event_count": len(rows), "watch_seconds": round(watched, 1),
            "study_hours": [{"hour": hour, "samples": hours[hour]} for hour in sorted(hours)],
            "playback_rates": [{"rate": rate, "samples": count} for rate, count in speeds.most_common()],
            "course_samples": [{"course_id": course, "samples": count} for course, count in courses.most_common()],
            "completed_lectures": len(completed), "top_revisits": top_revisits,
            "latest_at": latest, "timezone": timezone_name,
        },
    }


def estimate_fun_metrics(lectures: list[dict[str, Any]]) -> dict[str, Any]:
    phrase_counts: Counter[str] = Counter()
    phrase_evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    roll_calls: list[dict[str, Any]] = []
    answers: list[dict[str, Any]] = []
    for lecture in lectures:
        course_id = str(lecture.get("course_id") or "")
        sub_id = str(lecture.get("sub_id") or "")
        for segment in lecture.get("segments") or []:
            text = re.sub(r"\s+", "", str(segment.get("text") or ""))
            start_ms = int(segment.get("start_ms") or 0)
            evidence = {"course_id": course_id, "sub_id": sub_id, "start_ms": start_ms, "end_ms": int(segment.get("end_ms") or start_ms)}
            if re.search(r"点名|签到|到没到|同学来了|请假", text):
                roll_calls.append(evidence)
            if re.search(r"请.*同学.*回答|哪位同学|同学回答|你来回答", text):
                answers.append(evidence)
            for match in re.finditer(r"(?:大家|同学们|我们)([\u4e00-\u9fff]{2,6})", text):
                phrase = match.group(0)
                phrase_counts[phrase] += 1
                phrase_evidence[phrase].append(evidence)
    phrase, count = phrase_counts.most_common(1)[0] if phrase_counts else ("", 0)
    scope = {
        "lecture_count": len(lectures),
        "course_count": len({str(item.get('course_id') or '') for item in lectures}),
    }
    return {
        "estimated": True,
        "label": "AI 估算",
        "scope": scope,
        "catchphrase": {
            "value": phrase, "count": count,
            "confidence": min(0.9, 0.35 + count * 0.08) if phrase else 0.0,
            "evidence": phrase_evidence.get(phrase, [])[:8],
        },
        "roll_call_count": len(roll_calls),
        "roll_call_confidence": min(0.88, 0.45 + len(roll_calls) * 0.06) if roll_calls else 0.0,
        "roll_call_evidence": roll_calls[:20],
        "student_answer_count": len(answers),
        "student_answer_confidence": min(0.85, 0.4 + len(answers) * 0.05) if answers else 0.0,
        "student_answer_evidence": answers[:20],
        "method": "keyword-estimate-v1",
    }


__all__ = [
    "ANALYTICS_VERSION", "STUDY_EVENT_KINDS", "analytics_settings", "analytics_summary",
    "delete_analytics_term", "delete_study_events", "ensure_analytics_schema",
    "estimate_fun_metrics", "record_study_event", "record_watch_event", "set_analytics_enabled",
    "study_telemetry_summary",
]
