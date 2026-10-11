"""Evidence-backed transcript classification for focused playback modes."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .sqlite_utils import connect_learning_db


CLASSIFIER_VERSION = "evidence-rules-zh-v1"
TIMELINE_LABELS = (
    "knowledge", "exam", "example", "homework",
    "roll_call", "chat", "administrative", "slide_transition",
)

LABEL_TERMS: dict[str, tuple[str, ...]] = {
    "exam": ("考试重点", "必考", "考点", "期中", "期末", "会考", "考试", "容易错", "重点掌握"),
    "homework": ("作业", "习题", "课后练习", "提交", "截止", "练习题", "作业题"),
    "example": ("例题", "举个例子", "例如", "解题", "这道题", "计算一下", "证明", "案例"),
    "roll_call": ("点名", "签到", "学号", "哪位同学", "同学回答", "来了没有", "请举手"),
    "administrative": ("调课", "停课", "课程通知", "上课安排", "教室", "助教", "分组", "请假", "下周安排"),
    "chat": ("闲聊", "天气", "吃饭", "哈哈", "开个玩笑", "听得到吗", "直播卡", "网络卡", "声音大吗"),
    "slide_transition": ("下一页", "上一页", "看课件", "看ppt", "切到课件", "这张幻灯片", "翻页", "课件切换"),
    "knowledge": ("定义", "定理", "性质", "原理", "概念", "公式", "推导", "结论", "方法", "原因"),
}

LABEL_PRIORITY = {
    "exam": 80,
    "homework": 70,
    "example": 60,
    "roll_call": 50,
    "administrative": 40,
    "chat": 30,
    "slide_transition": 20,
    "knowledge": 10,
}


def ensure_smart_playback_schema(path: str | Path) -> None:
    with closing(connect_learning_db(path)) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS smart_timeline_meta (
                sub_id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                classifier_version TEXT NOT NULL,
                segment_count INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS smart_timeline_segments (
                segment_id TEXT PRIMARY KEY,
                course_id TEXT NOT NULL,
                sub_id TEXT NOT NULL,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL,
                label TEXT NOT NULL,
                confidence REAL NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                classifier_version TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_smart_timeline_lecture_time
                ON smart_timeline_segments(sub_id,start_ms,end_ms);
            """
        )
        db.commit()


def _normal_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def timeline_input_hash(segments: list[dict[str, Any]]) -> str:
    values = [
        [
            max(0, int(item.get("start_ms") or 0)),
            max(0, int(item.get("end_ms") or item.get("start_ms") or 0)),
            hashlib.sha256(_normal_text(item.get("text")).encode("utf-8")).hexdigest(),
        ]
        for item in segments
        if _normal_text(item.get("text"))
    ]
    raw = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def classify_text(text: str) -> dict[str, Any]:
    normalized = _normal_text(text).casefold()
    matches: dict[str, list[str]] = {}
    for label, terms in LABEL_TERMS.items():
        hits = [term for term in terms if term.casefold() in normalized]
        if hits:
            matches[label] = hits
    if not matches:
        return {
            "label": "knowledge",
            "confidence": 0.68 if len(normalized) >= 12 else 0.55,
            "matched_terms": [],
            "reason": "substantive_default" if len(normalized) >= 12 else "short_uncertain",
        }
    label = max(
        matches,
        key=lambda value: (len(matches[value]), LABEL_PRIORITY[value]),
    )
    hits = matches[label]
    base = 0.78 + min(0.16, 0.07 * len(hits))
    if label == "exam" and any(term in normalized for term in ("考试重点", "必考", "考点")):
        base = max(base, 0.93)
    return {
        "label": label,
        "confidence": min(0.97, base),
        "matched_terms": hits,
        "reason": "keyword_evidence",
    }


def classify_segments(
    *, course_id: str, sub_id: str, segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for item in sorted(segments, key=lambda value: int(value.get("start_ms") or 0)):
        text = _normal_text(item.get("text"))
        if not text:
            continue
        start_ms = max(0, int(item.get("start_ms") or 0))
        end_ms = max(start_ms + 1, int(item.get("end_ms") or start_ms + 1))
        classification = classify_text(text)
        source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        value = {
            "course_id": str(course_id),
            "sub_id": str(sub_id),
            "start_ms": start_ms,
            "end_ms": end_ms,
            "label": classification["label"],
            "confidence": float(classification["confidence"]),
            "evidence": {
                "matched_terms": list(classification["matched_terms"]),
                "reason": str(classification["reason"]),
                "source_hash": source_hash,
            },
        }
        value["segment_id"] = hashlib.sha256(
            f"{sub_id}:{start_ms}:{end_ms}:{source_hash}:{value['label']}".encode("utf-8")
        ).hexdigest()[:32]
        output.append(value)
    return coalesce_segments(output)


def coalesce_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for item in segments:
        current = dict(item)
        current["evidence"] = dict(item.get("evidence") or {})
        if (
            merged
            and merged[-1]["label"] == current["label"]
            and current["start_ms"] - merged[-1]["end_ms"] <= 3000
        ):
            prior = merged[-1]
            prior_duration = max(1, int(prior["end_ms"]) - int(prior["start_ms"]))
            current_duration = max(1, int(current["end_ms"]) - int(current["start_ms"]))
            prior["confidence"] = (
                float(prior["confidence"]) * prior_duration
                + float(current["confidence"]) * current_duration
            ) / (prior_duration + current_duration)
            prior["end_ms"] = max(int(prior["end_ms"]), int(current["end_ms"]))
            terms = list(dict.fromkeys([
                *list(prior["evidence"].get("matched_terms") or []),
                *list(current["evidence"].get("matched_terms") or []),
            ]))
            hashes = list(dict.fromkeys([
                *list(prior["evidence"].get("source_hashes") or [prior["evidence"].get("source_hash")]),
                current["evidence"].get("source_hash"),
            ]))
            prior["evidence"] = {
                "matched_terms": [term for term in terms if term],
                "reason": "coalesced_evidence",
                "source_hashes": [value for value in hashes if value],
            }
            prior["evidence"]["source_hash"] = hashlib.sha256(
                "|".join(prior["evidence"]["source_hashes"]).encode("ascii")
            ).hexdigest()
            prior["segment_id"] = hashlib.sha256(
                f"{prior['sub_id']}:{prior['start_ms']}:{prior['end_ms']}:{prior['label']}:{'|'.join(prior['evidence']['source_hashes'])}".encode("utf-8")
            ).hexdigest()[:32]
        else:
            merged.append(current)
    return merged


def save_timeline(
    path: str | Path, *, course_id: str, sub_id: str,
    transcript_segments: list[dict[str, Any]], classified: list[dict[str, Any]],
) -> dict[str, Any]:
    ensure_smart_playback_schema(path)
    input_hash = timeline_input_hash(transcript_segments)
    now = time.time()
    with closing(connect_learning_db(path)) as db:
        db.execute("DELETE FROM smart_timeline_segments WHERE sub_id=?", (str(sub_id),))
        for item in classified:
            label = str(item.get("label") or "")
            if label not in TIMELINE_LABELS:
                continue
            db.execute(
                """INSERT INTO smart_timeline_segments(
                       segment_id,course_id,sub_id,start_ms,end_ms,label,confidence,
                       evidence_json,classifier_version,input_hash,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(item["segment_id"]), str(course_id), str(sub_id),
                    max(0, int(item.get("start_ms") or 0)),
                    max(1, int(item.get("end_ms") or 1)), label,
                    max(0.0, min(1.0, float(item.get("confidence") or 0))),
                    json.dumps(item.get("evidence") or {}, ensure_ascii=False, separators=(",", ":")),
                    CLASSIFIER_VERSION, input_hash, now,
                ),
            )
        db.execute(
            """INSERT INTO smart_timeline_meta(
                   sub_id,course_id,input_hash,classifier_version,segment_count,updated_at
               ) VALUES(?,?,?,?,?,?)
               ON CONFLICT(sub_id) DO UPDATE SET course_id=excluded.course_id,
                   input_hash=excluded.input_hash,classifier_version=excluded.classifier_version,
                   segment_count=excluded.segment_count,updated_at=excluded.updated_at""",
            (str(sub_id), str(course_id), input_hash, CLASSIFIER_VERSION, len(classified), now),
        )
        db.commit()
    return list_timeline(path, sub_id=sub_id, current_input_hash=input_hash)


def list_timeline(
    path: str | Path, *, sub_id: str, current_input_hash: str = "",
) -> dict[str, Any]:
    ensure_smart_playback_schema(path)
    with closing(connect_learning_db(path)) as db:
        db.row_factory = sqlite3.Row
        meta_row = db.execute("SELECT * FROM smart_timeline_meta WHERE sub_id=?", (str(sub_id),)).fetchone()
        rows = db.execute(
            "SELECT * FROM smart_timeline_segments WHERE sub_id=? ORDER BY start_ms,end_ms",
            (str(sub_id),),
        ).fetchall()
    meta = dict(meta_row) if meta_row else {
        "sub_id": str(sub_id), "course_id": "", "input_hash": "",
        "classifier_version": CLASSIFIER_VERSION, "segment_count": 0, "updated_at": 0,
    }
    stale = bool(current_input_hash and meta.get("input_hash") != current_input_hash)
    segments = []
    if not stale:
        for row in rows:
            value = dict(row)
            value["evidence"] = json.loads(value.pop("evidence_json", "{}") or "{}")
            segments.append(value)
    return {"meta": meta, "segments": segments, "stale": stale}


def macro_f1(expected: list[str], predicted: list[str]) -> float:
    labels = sorted(set(expected) | set(predicted))
    if not labels or len(expected) != len(predicted):
        return 0.0
    scores = []
    for label in labels:
        true_positive = sum(1 for left, right in zip(expected, predicted) if left == label and right == label)
        false_positive = sum(1 for left, right in zip(expected, predicted) if left != label and right == label)
        false_negative = sum(1 for left, right in zip(expected, predicted) if left == label and right != label)
        precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
        recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
        scores.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return sum(scores) / len(scores)


__all__ = [
    "CLASSIFIER_VERSION", "TIMELINE_LABELS", "classify_segments", "classify_text",
    "coalesce_segments", "ensure_smart_playback_schema", "list_timeline", "macro_f1",
    "save_timeline", "timeline_input_hash",
]
