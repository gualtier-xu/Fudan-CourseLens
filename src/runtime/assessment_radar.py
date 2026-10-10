"""考核雷达·规则版（N5A-P1）：从字幕段与总结 markdown 里用纯本地规则抽取
考核事件，落本地台账供学习桌与总结卡呈现。零 token、零网络调用。

设计要点（方案 PLAN-N5A-P1 附录为唯一权威，QA 修正四缺陷已并入）：
- K1-0 句级重组：字幕段均很短，考核句常跨段；先把相邻段按句末标点重组为
  句簇（≤120 字、时间跨度 ≤30s），规则只跑在句簇上，引句=句簇文本。
- 11 类闭集规则按优先级首中即取，负向卫命中跳过；同句两类取优先级高者。
- 日期两级：绝对日期直解；相对日期用讲次日期锚 + 7 天窗，解不出 due_at
  置空仍入库（引句证据必填）。
- due_bucket 用本地时区日期（+08:00 日界口径），恢复 UNIQUE 去重语义。
- 状态机 T1-T8：同键日期相等=追加证据；日期冲突>24h=不覆盖、降回
  unconfirmed（信任优先，北极星）；confirm/dismiss 用户主权； dismissed 不复活。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from typing import Any

SCHEMA = "courselens.assessment-radar.v1"

CATEGORIES = (
    "exam", "resit", "quiz", "assignment", "project", "lab", "computer_lab",
    "attendance", "rollcall", "schedule_change", "qa_session",
)

# 优先级自上而下首个命中；负向卫命中即跳过本类（含在正则命中时）。
_RULE_TABLE: tuple[tuple[str, re.Pattern[str], tuple[str, ...]], ...] = (
    ("exam", re.compile(r"(期末|期中|结课)\s*(考试|考核)|(考试|考核)\s*(时间|安排|范围)"),
     ("怎么办", "复习", "准备", "挂了", "怕挂")),
    ("resit", re.compile(r"(补考|重考|缓考)"), ("都?过了",)),
    ("quiz", re.compile(r"(小测|随堂测|课堂测|随堂练习)"), ("做了", "做完了", "好难")),
    ("project", re.compile(r"(大作业|课程设计|小组(作业|项目)|项目(展示|答辩)|期末(报告|展示))"),
     ("举例", "比如")),
    ("computer_lab", re.compile(r"(上机|机房)"), ("冷气", "空调", "好热")),
    ("lab", re.compile(r"(实验[一二三四五\d]*|实验报告|实验课)"),
     ("实验室", "实验值", "实验结果表明")),
    ("assignment",
     re.compile(r"(交|提交|收)?\s*(?<!大)(?<!的)(作业|习题集?|课后题)|\b(due|DDL|ddl)\b|截止"),
     ("批改", "讲评", "答案", "好难", "写不完", "太多")),
    ("attendance", re.compile(r"(考勤|签到|平时分?|平时成绩)"), ()),
    ("rollcall", re.compile(r"(点名|喊到|点到)"), ("没点名", "没点到", "为止", "不点名")),
    ("schedule_change",
     re.compile(r"(调课|停课|换(教室|时间)|补课|下次课(不上|暂停))"), ()),
    ("qa_session", re.compile(r"(答疑|office\s*hour|答疑(时间|安排))"), ("答疑群",)),
)

_LOCATION_RE = re.compile(r"[A-HF]\d{4}|[\u4e00-\u9fa5]{2,8}(?:教室|楼|厅|室|机房)")
_ABS_MD_RE = re.compile(r"(?:(?P<y>20\d{2})年?)?(?P<m>1[0-2]|0?[1-9])月(?P<d>\d{1,2})日?号?")
_ABS_ISO_RE = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")
_WEEK_RE = re.compile(r"第\s*(\d{1,2})\s*周")
_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_WEEKDAY_RE = re.compile(r"(?:下下|下|这|本)?周([一二三四五六日天])")
_REL_OFFSET = {"明天": 1, "后天": 2, "下周": 7}
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；])")
_MAX_CLUSTER_CHARS = 120
_MAX_CLUSTER_SPAN_MS = 30_000
_REL_WINDOW_DAYS = 7


def _sentence_units(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把字幕段切成句单元（时间按字符占比线性内插，保底段级时间）。"""
    units: list[dict[str, Any]] = []
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start_ms = int(segment.get("start_ms") or 0)
        end_ms = int(segment.get("end_ms") or start_ms)
        pieces = [piece.strip() for piece in _SENT_SPLIT_RE.split(text) if piece.strip()]
        if not pieces:
            continue
        total = sum(len(piece) for piece in pieces)
        cursor = start_ms
        span = max(0, end_ms - start_ms)
        for piece in pieces:
            piece_end = cursor + (span * len(piece) // total if total else 0)
            units.append({"text": piece, "start_ms": cursor, "end_ms": piece_end})
            cursor = piece_end
    return units


def sentence_clusters(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """K1-0 句级重组：相邻句拼成句簇（≤120 字、跨度 ≤30s）。"""
    clusters: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for unit in _sentence_units(segments):
        if current:
            head = current[0]
            chars = sum(len(item["text"]) for item in current) + len(unit["text"])
            span = int(unit["end_ms"]) - int(head["start_ms"])
            gap = int(unit["start_ms"]) - int(current[-1]["end_ms"])
            if chars > _MAX_CLUSTER_CHARS or span > _MAX_CLUSTER_SPAN_MS or gap > _MAX_CLUSTER_SPAN_MS:
                clusters.append(_close_cluster(current))
                current = []
        current.append(unit)
    if current:
        clusters.append(_close_cluster(current))
    return clusters


def _close_cluster(units: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "text": "".join(unit["text"] for unit in units),
        "start_ms": int(units[0]["start_ms"]),
        "end_ms": int(units[-1]["end_ms"]),
    }


def _strip_dates(text: str) -> str:
    """title_norm 用：去掉日期串与空白/全角差异，保留编号语义。"""
    cleaned = _ABS_ISO_RE.sub(" ", text)
    cleaned = _ABS_MD_RE.sub(" ", cleaned)
    cleaned = _WEEK_RE.sub(" ", cleaned)
    return cleaned


def _normalize_width(text: str) -> str:
    table = {ord(full): half for full, half in zip(
        "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
        "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")}
    return text.translate(table)


def _ordinal_key(text: str) -> str:
    """`第三次作业`→`作业#3`：序数词折叠成 #n，其余原样。"""
    match = re.search(r"第\s*([一二三四五六七八九十\d]{1,3})\s*(?:次|个|章)?", text)
    if not match:
        return text
    raw = match.group(1)
    if raw.isdigit():
        number = int(raw)
    else:
        number = _CN_NUM.get(raw[-1:], 0) or 0
    if not number:
        return text
    return text[:match.start()] + f"#{number}" + text[match.end():]


def title_norm(title: str) -> str:
    text = _normalize_width(title)
    text = _strip_dates(text)
    text = _ordinal_key(text)
    return re.sub(r"\s+", "", text)


def _classify(text: str) -> tuple[str, re.Match[str] | None]:
    for category, pattern, guards in _RULE_TABLE:
        match = pattern.search(text)
        if not match:
            continue
        # 卫语是正则片段（如 `都?过了`），按 re.search 判定而非子串
        if any(re.search(guard, text) for guard in guards):
            continue
        return category, match
    return "", None


def _parse_due(text: str, lecture_date: str | None, semester_first_monday: str | None) -> str:
    """返回 due_at 的 epoch 秒（当日 09:00 本地）或 ""（解不出）。"""
    today: date | None = None
    iso = _ABS_ISO_RE.search(text)
    month_day = _ABS_MD_RE.search(text)
    week = _WEEK_RE.search(text)
    if iso:
        today = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    elif month_day:
        year = int(month_day.group("y")) if month_day.group("y") else (
            date.today().year if lecture_date is None else _year_of(lecture_date))
        try:
            today = date(year, int(month_day.group("m")), int(month_day.group("d")))
        except ValueError:
            today = None
    elif week and semester_first_monday:
        try:
            anchor = date.fromisoformat(str(semester_first_monday))
            today = anchor + timedelta(weeks=int(week.group(1)) - 1)
        except ValueError:
            today = None
    if today is None:
        weekday = _WEEKDAY_RE.search(text)
        base = _parse_date(lecture_date)
        if weekday and base is not None:
            target = _CN_NUM[weekday.group(1)]
            offset = (target - base.weekday() - 1) % 7 + 1
            if "下下" in text:
                offset += 7
            today = base + timedelta(days=offset)
            if offset > _REL_WINDOW_DAYS:
                return ""
        elif base is not None:
            for token, offset_days in _REL_OFFSET.items():
                if token in text:
                    today = base + timedelta(days=offset_days)
                    break
    if today is None:
        return ""
    due = datetime(today.year, today.month, today.day, 9, 0, 0)
    return str(due.timestamp())


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _year_of(lecture_date: str) -> int:
    parsed = _parse_date(lecture_date)
    return parsed.year if parsed else date.today().year


def due_bucket(due_at: str | float | None) -> str:
    """本地时区 YYYY-MM-DD；无日期=''（UNIQUE 语义对 NULL 失效的修正）。"""
    try:
        stamp = float(due_at) if due_at not in (None, "") else None
    except (TypeError, ValueError):
        stamp = None
    if stamp is None:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(stamp))


def extract_events(
    segments: list[dict[str, Any]],
    *,
    course_id: str,
    sub_id: str,
    lecture_date: str | None = None,
    semester_first_monday: str | None = None,
    summary_text: str = "",
) -> list[dict[str, Any]]:
    """规则抽取：字幕段（+总结 markdown 逐行）→ 考核事件草稿列表。"""
    clusters = sentence_clusters(segments)
    for line in str(summary_text or "").splitlines():
        line = line.strip()
        if line:
            clusters.append({"text": line, "start_ms": None, "end_ms": None})
    events: list[dict[str, Any]] = []
    for cluster in clusters:
        category, match = _classify(cluster["text"])
        if not category or match is None:
            continue
        events.append({
            "course_id": str(course_id),
            "category": category,
            "title": match.group(0).strip(),
            "title_norm": title_norm(cluster["text"][:40]),
            "due_at": _parse_due(cluster["text"], lecture_date, semester_first_monday),
            "location": _LOCATION_RE.search(cluster["text"]).group(0)
            if _LOCATION_RE.search(cluster["text"]) else "",
            "source": "rule",
            "quote": cluster["text"][:80],
            "start_ms": cluster["start_ms"],
            "end_ms": cluster["end_ms"],
            "first_seen_sub_id": str(sub_id),
        })
    return events


# ---- 台账（learning.db assessment_events 表）--------------------------------

def upsert_events(
    learning_store, events: list[dict[str, Any]], *, default_status: str = "active",
) -> dict[str, int]:
    """T1-T6 状态机入库。返回 {inserted, evidence_merged, conflicts} 计数。

    default_status：规则来源=active；LLM 顺风车来源传 "unconfirmed"
    （N5A-P2：确认后才在学习桌默认展开）。"""
    counts = {"inserted": 0, "evidence_merged": 0, "conflicts": 0}
    now = time.time()
    with closing(sqlite3.connect(learning_store.path)) as db, db:
        for event in events:
            bucket = due_bucket(event.get("due_at"))
            # 键=(course_id, category, title_norm)：跨 due_bucket 也要比出日期
            # 冲突（T3），不能只在同桶内找。
            row = db.execute(
                """SELECT event_id, due_at, due_bucket, status, evidence_json, conflict_note
                     FROM assessment_events
                     WHERE course_id=? AND category=? AND title_norm=?
                     ORDER BY updated_at DESC LIMIT 1""",
                (str(event["course_id"]), str(event["category"]),
                 str(event["title_norm"])),
            ).fetchone()
            if row is None:
                course = str(event["course_id"])
                category = str(event["category"])
                norm = str(event["title_norm"])
                event_id = hashlib.sha256(
                    "|".join([course, category, norm, bucket]).encode("utf-8")
                ).hexdigest()[:32]
                db.execute(
                    """INSERT INTO assessment_events(
                        event_id, course_id, category, title, title_norm, due_at, due_bucket,
                        location, source, status, first_seen_sub_id, last_seen_sub_id,
                        evidence_json, conflict_note, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (event_id, course, category, str(event["title"]), norm,
                     _opt_float(event.get("due_at")), bucket,
                     str(event.get("location") or ""),
                     str(event.get("source") or "rule"),
                     str(event.get("status") or default_status),
                     str(event.get("first_seen_sub_id") or ""),
                     str(event.get("first_seen_sub_id") or ""),
                     json.dumps([event.get("quote")], ensure_ascii=False),
                     "", now, now),
                )
                counts["inserted"] += 1
                continue
            event_id, old_due, old_bucket, status, evidence_json, conflict_note = row
            evidence = json.loads(evidence_json or "[]")
            quote = str(event.get("quote") or "")
            if quote and quote not in evidence:
                evidence.append(quote)
                evidence = evidence[-3:]
            new_due = _opt_float(event.get("due_at"))
            old_val = _opt_float(old_due)
            if new_due is not None and old_val is not None and bucket != old_bucket:
                delta = abs(new_due - old_val)
                note = f"时间有出入：{time.strftime('%m月%d日', time.localtime(new_due))}"
                counts["conflicts"] += 1
                # T3：旧值保留不覆盖；>24h 硬冲突降回 unconfirmed，≤24h 软冲突只记注
                new_status = "unconfirmed" if (delta > 86400 and status == "active") else status
                db.execute(
                    """UPDATE assessment_events SET evidence_json=?, conflict_note=?,
                        status=?, last_seen_sub_id=?, updated_at=? WHERE event_id=?""",
                    (json.dumps(evidence, ensure_ascii=False),
                     "；".join(piece for piece in (conflict_note, note) if piece),
                     new_status,
                     str(event.get("first_seen_sub_id") or ""), now, event_id),
                )
                continue
            db.execute(
                """UPDATE assessment_events SET evidence_json=?, last_seen_sub_id=?, updated_at=?
                    WHERE event_id=?""",
                (json.dumps(evidence, ensure_ascii=False),
                 str(event.get("first_seen_sub_id") or ""), now, event_id),
            )
            counts["evidence_merged"] += 1
    return counts


def _opt_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def assessment_events_scan(
    learning_store, catalog_repository, *, course_id: str = "",
) -> dict[str, Any]:
    """GET assessment：近期未过期的 active/confirmed 事件 + 各课计数。

    night14-R2 Fix-D 防御补强：表缺席（旧库/建表前竞态）按「无台账」优雅
    降级为空 events——雷达特性绝不 500（boot 建表已补，此为纵深守卫）。"""
    now = time.time()
    where = ["status IN ('active','confirmed','unconfirmed')"]
    args: list[Any] = []
    if course_id:
        where.append("course_id=?")
        args.append(str(course_id))
    try:
        with closing(sqlite3.connect(learning_store.path)) as db, db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                f"""SELECT * FROM assessment_events WHERE {' AND '.join(where)}
                     ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at ASC LIMIT 200""",
                args,
            ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    events = []
    for row in rows:
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
        item["expired"] = bool(item.get("due_at") and float(item["due_at"]) < now)
        events.append(item)
    return {"schema": SCHEMA, "events": events, "count": len(events)}


def assessment_events_action(learning_store, *, event_id: str, action: str) -> dict[str, Any]:
    """POST assessment/actions：confirm→confirmed / dismiss→dismissed（T5/T6）。"""
    action = str(action or "").strip().casefold()
    if action not in ("confirm", "dismiss"):
        raise AssessmentActionError("assessment_action_invalid")
    status = "confirmed" if action == "confirm" else "dismissed"
    with closing(sqlite3.connect(learning_store.path)) as db, db:
        cursor = db.execute(
            "UPDATE assessment_events SET status=?, updated_at=? WHERE event_id=?",
            (status, time.time(), str(event_id)),
        )
        if cursor.rowcount != 1:
            raise KeyError("assessment_event_missing")
    return {"schema": SCHEMA, "event_id": str(event_id), "status": status}


def confirmed_schedule_events(
    learning_store, *, course_scope: list[str], until_epoch: float,
    now: float | None = None, limit: int = 6,
) -> list[dict[str, Any]]:
    """P13-B 联动冻结件1：窗口内已确认、带日期的考核事件（due_at 升序）。

    确认门硬闸：只联动 status='confirmed'——unconfirmed/active 是机器判定，
    未经用户点头永不自动进复习计划；dismissed/merged 永不联动。空 scope
    零联动。只读；调用点保证库已过 ensure_assessment_schema。
    """
    scope = [str(value) for value in (course_scope or []) if str(value)]
    if not scope:
        return []
    now_value = float(now) if now is not None else time.time()
    placeholders = ",".join("?" for _ in scope)
    with closing(sqlite3.connect(learning_store.path)) as db, db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            f"""SELECT * FROM assessment_events
                 WHERE status='confirmed' AND course_id IN ({placeholders})
                   AND due_at IS NOT NULL AND due_at >= ? AND due_at <= ?
                 ORDER BY due_at ASC LIMIT ?""",
            (*scope, now_value, float(until_epoch), max(0, int(limit))),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        evidence = json.loads(item.pop("evidence_json") or "[]")
        events.append({
            "event_id": str(item.get("event_id") or ""),
            "course_id": str(item.get("course_id") or ""),
            "category": str(item.get("category") or ""),
            "title": str(item.get("title") or ""),
            "due_at": float(item.get("due_at") or 0.0),
            "due_bucket": str(item.get("due_bucket") or ""),
            "location": str(item.get("location") or ""),
            "evidence": evidence if isinstance(evidence, list) else [],
            "conflict_note": str(item.get("conflict_note") or ""),
        })
    return events


def rescan_lecture(
    learning_store, *, course_id: str, sub_id: str,
    segments: list[dict[str, Any]], lecture_date: str | None = None,
    semester_first_monday: str | None = None, summary_text: str = "",
) -> dict[str, Any]:
    """触发入口：总结工件 ready 钩子或手动重扫时调用。"""
    events = extract_events(
        segments, course_id=course_id, sub_id=sub_id, lecture_date=lecture_date,
        semester_first_monday=semester_first_monday, summary_text=summary_text,
    )
    counts = upsert_events(learning_store, events)
    return {"schema": SCHEMA, "scanned": len(events), **counts}


class AssessmentActionError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
