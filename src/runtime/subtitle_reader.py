"""Parse generated SRT/WebVTT files into safe, timestamped reading segments.

Also owns the additive ``evidence.v1`` seam for subtitle segments: bounded
metadata sanitization, deterministic fallback identity for legacy rows, and
split-child display-cue identities.  The ID grammar and identity fields are
frozen in ``docs/evidence-contract-v1.md``; IDs are minted only through
``shared.evidence_contract.compute_id``.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path

from shared.evidence_contract import (
    NAMESPACE_CUE,
    NAMESPACE_SOURCE,
    NAMESPACE_SEGMENT,
    SECRET_VALUE_PATTERNS,
    canonical_json,
    compute_id,
)


_TIMING_RE = re.compile(r"^\s*(\S+)\s*-->\s*(\S+)(?:\s+.*)?$")
_TAG_RE = re.compile(r"<[^>]*>")
_SRT_POSITION_RE = re.compile(r"\{\\[^}]+\}")
# 长句切分闭集：中文句读标点，切点在标点之后
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[。！？；，、])")

# evidence.v1 ID grammar and identity scope, frozen in docs/evidence-contract-v1.md §3/§8.
_EVIDENCE_ID_RE = re.compile(r"^(src|seg|cor|cue|slent|slevt|unit|chk|mtr):[0-9a-f]{12}$")
_SECRET_KEY_RE = re.compile(r"(?i)(secret|passwd|password|api[_-]?key|apikey|credential)")
_SOURCE_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

_METADATA_MAX_KEYS = 24
_METADATA_MAX_STRING = 2000
_METADATA_MAX_JSON = 4096
_METADATA_MAX_DEPTH = 3
_RESERVED_SEGMENT_KEYS = {"index", "start_ms", "end_ms", "text"}
_CHILD_PRESERVED_KEYS = _RESERVED_SEGMENT_KEYS | {"evidence_id", "parent_evidence_id"}


def is_evidence_id(value: object) -> bool:
    """Whether the value matches the frozen evidence.v1 ID grammar."""
    return isinstance(value, str) and bool(_EVIDENCE_ID_RE.match(value))


def segment_evidence_id(segment: object) -> str | None:
    """Contract-shaped identity of one incoming segment.

    ``evidence_id`` wins; a contract-shaped ``segment_id`` alias is promoted
    when ``evidence_id`` is absent or malformed.  Anything else is not an
    identity and never gets minted into one here.
    """
    raw = segment if isinstance(segment, dict) else {}
    value = raw.get("evidence_id")
    if not is_evidence_id(value):
        value = raw.get("segment_id")
    return value if is_evidence_id(value) else None


def _safe_metadata_value(value: object, depth: int = 0) -> object | None:
    """JSON-safe bounded copy of a metadata value, or None when unsafe."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if len(value) > _METADATA_MAX_STRING:
            return None
        return value if not any(p.search(value) for p, _ in SECRET_VALUE_PATTERNS) else None
    if depth >= _METADATA_MAX_DEPTH:
        return None
    if isinstance(value, dict):
        safe: dict = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 64
                or _SECRET_KEY_RE.search(key)
            ):
                continue
            nested = _safe_metadata_value(item, depth + 1)
            if nested is not None:
                safe[key] = nested
        return safe or None
    if isinstance(value, list):
        items = []
        for item in value[:32]:
            nested = _safe_metadata_value(item, depth + 1)
            if nested is not None:
                items.append(nested)
        return items or None
    return None


def sanitize_evidence_metadata(segment: object) -> dict:
    """Bounded, secret-safe metadata retained from one incoming segment.

    Valid contract identity fields survive verbatim; unknown safe additive
    keys are retained in the same bounded object; credential-shaped strings,
    credential-store key names, oversized values, and malformed identity
    fields are dropped so legacy reads can never crash or leak on them.
    """
    raw = segment if isinstance(segment, dict) else {}
    metadata: dict = {}
    evidence_id = segment_evidence_id(raw)
    for key, value in raw.items():
        if len(metadata) >= _METADATA_MAX_KEYS:
            break
        if not isinstance(key, str) or key in _RESERVED_SEGMENT_KEYS:
            continue
        if not key or len(key) > 64 or _SECRET_KEY_RE.search(key):
            continue
        if key == "evidence_id" or key == "source_hash":
            continue  # identity fields are normalized below, never kept raw
        if key == "segment_id" and evidence_id is not None and value == evidence_id:
            continue  # promoted into evidence_id below; no duplicate entry
        safe = _safe_metadata_value(value)
        if safe is not None:
            metadata[key] = safe
    source_hash = raw.get("source_hash")
    if isinstance(source_hash, str) and _SOURCE_HASH_RE.match(source_hash):
        metadata["source_hash"] = source_hash
    if evidence_id is not None:
        metadata["evidence_id"] = evidence_id
    return metadata


def _fallback_source_sha256(rows: list[dict]) -> str:
    """Content hash over the cached transcript itself (never the media)."""
    content = [
        {
            "start_ms": int(row.get("start_ms") or 0),
            "end_ms": int(row.get("end_ms") or 0),
            "text": str(row.get("text") or ""),
        }
        for row in rows
    ]
    content.sort(key=lambda item: (item["start_ms"], item["end_ms"], item["text"]))
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def assign_fallback_evidence_ids(rows: list[dict]) -> list[dict]:
    """Fill contract-shaped evidence IDs for cached rows that carry none.

    Identity is position-independent: the source scope is a canonically
    ordered content hash of the transcript rows, and segment IDs use the
    frozen evidence.v1 identity fields.  Unchanged content re-reads to
    identical IDs; changed text or anchors mint a different identity.  Rows
    already carrying a valid ID are returned untouched.
    """
    if not rows or not any(not is_evidence_id(row.get("evidence_id")) for row in rows):
        return rows
    source_id = compute_id(NAMESPACE_SOURCE, {
        "kind": "document",
        "origin": "external_import",
        "title": None,
        "duration_ms": None,
        "source_sha256": _fallback_source_sha256(rows),
    })
    result: list[dict] = []
    for row in rows:
        if is_evidence_id(row.get("evidence_id")):
            result.append(row)
            continue
        identity = {
            "source_id": source_id,
            "start_ms": int(row.get("start_ms") or 0),
            "end_ms": int(row.get("end_ms") or 0),
            "text": str(row.get("text") or ""),
            "lang": row.get("lang"),
            "no_speech": False,
            "producer": None,
            "model": None,
            "config_hash": None,
        }
        updated = dict(row)
        updated["evidence_id"] = compute_id(NAMESPACE_SEGMENT, identity)
        result.append(updated)
    return result


def parse_timestamp_ms(value: str) -> int:
    value = str(value or "").strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 2:
        hours = 0
        minutes_text, seconds_text = parts
    elif len(parts) == 3:
        hours = int(parts[0])
        minutes_text, seconds_text = parts[1:]
    else:
        raise ValueError("invalid subtitle timestamp")
    seconds_parts = seconds_text.split(".", 1)
    seconds = int(seconds_parts[0])
    fraction = (seconds_parts[1] if len(seconds_parts) > 1 else "0")[:3].ljust(3, "0")
    minutes = int(minutes_text)
    if min(hours, minutes, seconds) < 0 or minutes >= 60 or seconds >= 60:
        raise ValueError("subtitle timestamp is out of range")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(fraction)


def _clean_cue_text(lines: list[str]) -> str:
    text = " ".join(line.strip() for line in lines if line.strip())
    text = _SRT_POSITION_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    return " ".join(html.unescape(text).split())


def parse_subtitle_text(text: str) -> list[dict[str, object]]:
    """Return normalized cues from SRT or WebVTT source text."""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    segments: list[dict[str, object]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip().lstrip("\ufeff")
        if not line or line == "WEBVTT":
            index += 1
            continue
        if line.startswith(("NOTE", "STYLE", "REGION")):
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            continue
        timing = _TIMING_RE.match(line)
        if timing is None and index + 1 < len(lines):
            timing = _TIMING_RE.match(lines[index + 1].strip())
            if timing is not None:
                index += 1
        if timing is None:
            index += 1
            continue
        try:
            start_ms = parse_timestamp_ms(timing.group(1))
            end_ms = parse_timestamp_ms(timing.group(2))
        except ValueError:
            index += 1
            continue
        index += 1
        cue_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            cue_lines.append(lines[index])
            index += 1
        cue_text = _clean_cue_text(cue_lines)
        if cue_text and end_ms > start_ms:
            segments.append({
                "index": len(segments) + 1,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": cue_text,
            })
    return segments


def parse_subtitle_file(path: str | Path) -> list[dict[str, object]]:
    source = Path(path)
    return parse_subtitle_text(source.read_text(encoding="utf-8-sig", errors="replace"))


def _split_cue_text(text: str, max_chars: int) -> list[str]:
    """Split one cue body at Chinese clause punctuation, hard-splitting run-ons."""
    clauses = [part for part in _CLAUSE_SPLIT_RE.split(text) if part]
    chunks: list[str] = []
    buffer = ""
    for clause in clauses:
        if len(clause) > max_chars:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.extend(clause[index:index + max_chars] for index in range(0, len(clause), max_chars))
        elif buffer and len(buffer) + len(clause) > max_chars:
            chunks.append(buffer)
            buffer = clause
        else:
            buffer += clause
    if buffer:
        chunks.append(buffer)
    return chunks


def _split_timing(
    start_ms: int, end_ms: int, chunks: list[str], min_duration_ms: int,
) -> list[int]:
    """Proportional child boundaries: rounded ms, >= min duration, exact parent end."""
    span = max(0, end_ms - start_ms)
    total = sum(len(chunk) for chunk in chunks) or len(chunks)
    consumed = 0
    boundaries = [start_ms]
    for position, chunk in enumerate(chunks):
        consumed += len(chunk)
        if position == len(chunks) - 1:
            boundaries.append(end_ms)
        else:
            boundaries.append(start_ms + round(span * consumed / total))
    durations = [boundaries[i + 1] - boundaries[i] for i in range(len(chunks))]
    # 不足最小时长的子段从最长的兄弟段借时间；借不到时保持诚实比例
    for index, duration in enumerate(durations):
        if duration >= min_duration_ms:
            continue
        donors = [i for i, value in enumerate(durations) if i != index and value > min_duration_ms]
        if not donors:
            continue
        donor = max(donors, key=lambda i: durations[i])
        take = min(min_duration_ms - duration, durations[donor] - min_duration_ms)
        durations[donor] -= take
        durations[index] += take
    result = [boundaries[0]]
    for duration in durations:
        result.append(result[-1] + max(0, duration))
    result[-1] = end_ms
    return result


def split_long_cues(
    segments: list[dict[str, object]],
    *,
    max_chars: int = 44,
    min_duration_ms: int = 200,
) -> list[dict[str, object]]:
    """Split over-long display cues into clause-sized children on the same timeline.

    纯函数：≤ max_chars 的 cue 原样通过；> max_chars 的 cue 按中文标点（必要时硬切）
    拆分为子 cue，时长按字符数比例分配（毫秒四舍五入），保证单调、不重叠且末段
    恰好落在父段结束时间上，并对每段执行最小时长约束。

    携带有效 evidence_id 的父段拆分后，子 cue 绝不复用父段 ID：每个子 cue 获得
    由 evidence.v1 cue 身份字段（锚点/文本/derived_from/lang）计算的稳定独立 ID，
    并保留不可变的 ``parent_evidence_id`` 父证据引用；无 ID 的父段不为子 cue
    凭空造 ID。父段其余元数据原样传给子 cue。
    """
    max_chars = max(1, int(max_chars))
    min_duration_ms = max(0, int(min_duration_ms))
    if not any(len(str(item.get("text") or "")) > max_chars for item in segments):
        return list(segments)
    result: list[dict[str, object]] = []
    for segment in segments:
        text = str(segment.get("text") or "")
        if len(text) <= max_chars:
            result.append(segment)
            continue
        start_ms = int(segment.get("start_ms") or 0)
        end_ms = int(segment.get("end_ms") or 0)
        if end_ms <= start_ms:
            result.append(segment)
            continue
        chunks = _split_cue_text(text, max_chars)
        boundaries = _split_timing(start_ms, end_ms, chunks, min_duration_ms)
        parent_id = segment_evidence_id(segment)
        parent_identified = parent_id is not None
        child_metadata = {
            key: value for key, value in segment.items()
            if key not in _CHILD_PRESERVED_KEYS
        }
        for position, chunk in enumerate(chunks):
            child: dict[str, object] = {
                "index": 0,
                "start_ms": boundaries[position],
                "end_ms": boundaries[position + 1],
                "text": chunk,
            }
            child.update(child_metadata)
            if parent_identified:
                child["parent_evidence_id"] = parent_id
                child["evidence_id"] = compute_id(NAMESPACE_CUE, {
                    "start_ms": child["start_ms"],
                    "end_ms": child["end_ms"],
                    "lines": [chunk],
                    "derived_from": {"kind": "segment", "id": parent_id},
                    "lang": child.get("lang"),
                })
            result.append(child)
    for index, segment in enumerate(result, 1):
        segment["index"] = index
    return result


# ---- AS13（第五十三案）字幕 cue 确定性整形 --------------------------------
# 纯服务/展示层变换：证据层一字不动、零标点新增、零改写、零 LLM。全部阈值
# 顶部具名；用户拍板口径=单句 ≤25-30 字、句中填充词保留、本轮不加标点、
# 纯语气词独占 cue 删。

# 合并目标单句上限（拍板 25-30 区间上限；夜10-C 数据判定=真实库 -139 条碎句）
SHAPE_MERGE_MAX_CHARS = 30
# 相邻 cue 间隔 ≤2.0s 视为可贴并（夜10-C 数据判定=微碎片残余 -1.2pp）
SHAPE_MERGE_GAP_MS = 2000
# ≤6 字的碎片 cue 允许以更近的间隔（<1s）贴并
SHAPE_SHORT_CUE_CHARS = 6
SHAPE_SHORT_CUE_GAP_MS = 1000
# 合并单元显示时长下限；不足时向静音方向延到该时长
SHAPE_MIN_DISPLAY_MS = 1200
# 合并单元末段拖尾 0.5s，但绝不越过下一条 start
SHAPE_TAIL_MS = 500
# 拖尾/补时不得把字幕悬挂进超过 1.5s 的静音
SHAPE_MAX_SILENCE_HANG_MS = 1500
# >30 字才拆分（25-30 区间内保留整句）；无安全边界则保留并计数，禁硬截断
SHAPE_SPLIT_MAX_CHARS = 30
# SUBTITLE-DEEP-1 Phase C（总控补充行 2026-09-29）：行长均衡软目标带。标点
# 句读拆分装箱时提前收口，使 cue 行长尽量落在 [12, 22]（用户口径），硬帽仍
# =30；单句无标点超带时原样保留（禁硬截断），只影响多句读长句的装箱选择。
SHAPE_BALANCE_MIN_CHARS = 12
SHAPE_BALANCE_MAX_CHARS = 22
# 夜10-C 软边界拆分：真实语料零标点，标点句读路径在真实库上 split=0 失活；
# >30 字无标点边界时改在话轮标记词起点拆（标记词随其后分句，零标点零改写），
# 每块须落在 [10, 30] 字防拆出新微碎片；任一块越界则整句保留（禁硬截断）。
SHAPE_SOFT_BOUNDARY_MIN_CHUNK = 10
SHAPE_SOFT_BOUNDARY_MARKERS = (
    "然后", "那么", "所以", "但是", "不过", "接着", "另外", "其实", "还有",
    "而且", "可是", "因此", "后来", "最后", "首先", "其次", "当然", "总之",
    "也就是说", "就是说", "于是", "同时", "此外", "比如", "例如", "这样的话",
    "我们看到", "大家看", "这个问题", "这道题", "接下来", "一般来说", "换句话说",
)
_SHAPE_SOFT_BOUNDARY_RE = re.compile(
    "|".join(re.escape(marker) for marker in SHAPE_SOFT_BOUNDARY_MARKERS)
)
# 纯语气词字母表：cue 全文仅由这些字构成（≤4 字）即删（含 嗯行/嗯好/啊对 组合）。
# 夜10-C 扩展（审计口径）：欸诶嘿哟唷咦哇喔咯嘞呣唔 = 同族纯拟声/语气字；
# 嘛啦呗 = 独占 cue 时不承载实义的尾助词字。句首剥离范围不扩（拍板边界=仅呃/嗯）。
SHAPE_FILLER_CHARS = "呃嗯啊哦噢哎唉欸诶嘿哟唷咦哇喔咯嘞呣唔嘛啦呗"
# 应答词独占 cue：并入下一条开头作句头前缀，不直删。夜10-C 扩展=已裁定应答词
# 的重叠体（好的/是的/对的）与带尾助词变体（是啊/行啊/好嘛/行吧/好呀/好啦）。
SHAPE_ANSWER_WORDS = (
    "对", "好", "行", "是", "没有", "可以",
    "好的", "是的", "对的", "是啊", "行啊", "好嘛", "行吧", "好呀", "好啦",
)
# 句首语气词剥离：仅剥连续的 呃/嗯（含其后的空白与紧随标点），绝不剥实词。
# 紧随标点属于被剥语气词自身的停顿符（「嗯，这个」→「这个」而非「，这个」）；
# 剥离在第一个实词处停止，标点集与 _SHAPE_EDGE_PUNCT 同表。
_SHAPE_LEADING_FILLER_RE = re.compile(
    r"^[呃嗯](?:[ \u00a0]*[呃嗯])*[ \u00a0]*[。！？，、；：,.!?;:~～…—··]*[ \u00a0]*"
)
# 边缘标点（分类净体用）：判定纯度/应答词时剥去 cue 首尾的空白与标点；
# 仅用于分类，输出文本的标点零增零改（用户拍板=本轮不加标点）。
_SHAPE_EDGE_PUNCT = "。！？，、；：,.!?;:~～…—··\u00a0\t "
# 应答词并入上限：与下一条起点间隔超过 3s 不再并入（语义上已不属同一回应，
# 并入会把显示起点拉长悬挂进长静音）；超限走独立 cue+通用贴并规则。
SHAPE_ANSWER_ATTACH_MAX_GAP_MS = 3000


def _shape_class_body(text: str) -> str:
    """分类净体：去首尾空白与边缘标点（仅用于纯度/应答词判定，不改输出文本）。"""
    return text.strip().strip(_SHAPE_EDGE_PUNCT)


def _shape_is_pure_filler(text: str) -> bool:
    body = _shape_class_body(text)
    if not body:
        return True  # 无实义壳（纯标点/空白壳 cue）：按纯语气词处理
    if len(body) > 4:
        return False
    if all(ch in SHAPE_FILLER_CHARS for ch in body):
        return True
    # 语气词+应答词组合（嗯行/嗯好/啊对 一族，用户拍板删除）：剥去前导语气词
    # 后恰为一个应答词即判纯语气；纯应答词本身不在此判（走并入）。
    index = 0
    while index < len(body) and body[index] in SHAPE_FILLER_CHARS:
        index += 1
    stripped = body[index:].strip()
    return index > 0 and stripped in SHAPE_ANSWER_WORDS


def _shape_is_answer_word(text: str) -> bool:
    return _shape_class_body(text) in SHAPE_ANSWER_WORDS


def _shape_prefix_text(item: dict[str, object]) -> str:
    """应答词并入句头的前缀体：去首尾空白与边缘标点，避免并处产生句中标点。"""
    return _shape_class_body(str(item.get("text") or ""))


def _shape_pack_balanced(clauses: list[str], max_chars: int) -> list[str] | None:
    """句读装箱（Phase C 行长均衡）：软目标带 [12,22]，硬帽 max_chars。

    贪心累积句读；当加入下一句读会超出软目标上限且当前块已达软目标下限时
    提前收口（行更均衡），否则继续累积至硬帽。任一句读超硬帽→None（调用方
    走软边界/保留路径）。产出块数 <2 → None（无拆分意义）。
    """
    chunks: list[str] = []
    buffer = ""
    for clause in clauses:
        if len(clause) > max_chars:
            return None
        if buffer and len(buffer) + len(clause) > SHAPE_BALANCE_MAX_CHARS and (
            len(buffer) >= SHAPE_BALANCE_MIN_CHARS
        ):
            chunks.append(buffer)
            buffer = clause
        elif buffer and len(buffer) + len(clause) > max_chars:
            chunks.append(buffer)
            buffer = clause
        else:
            buffer += clause
    if buffer:
        chunks.append(buffer)
    return chunks if len(chunks) > 1 else None


def _shape_soft_chunks(text: str, max_chars: int, min_chunk: int) -> list[str] | None:
    """话轮标记词软拆分：标记词起点切块（标记词随其后块），贪心装箱。

    约束：每块 ∈ [min_chunk, max_chars]（防拆出新微碎片/超上限）；
    任一块越界或有效块数 <2 → 返回 None（整句保留，禁硬截断）。
    """
    parts: list[str] = []
    last = 0
    for match in _SHAPE_SOFT_BOUNDARY_RE.finditer(text):
        position = match.start()
        if position > last:
            parts.append(text[last:position])
            last = position
    parts.append(text[last:])
    chunks: list[str] = []
    buffer = ""
    for part in parts:
        if not part:
            continue
        if len(part) > max_chars:
            return None
        if buffer and len(buffer) + len(part) > max_chars:
            if len(buffer) < min_chunk:
                return None
            chunks.append(buffer)
            buffer = part
        else:
            buffer += part
    if buffer:
        if len(buffer) < min_chunk:
            if chunks and len(chunks[-1]) + len(buffer) <= max_chars:
                chunks[-1] += buffer
            else:
                return None
        else:
            chunks.append(buffer)
    return chunks if len(chunks) > 1 else None


# 夜10-C 第九波：邻接重复折叠——合法叠词白名单（单字 run==2 保留）与
# 相邻同文合并窗口；折叠确定性、零增字，与 worker 侧同一算法两处落地。
_SHAPE_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9]+")
_SHAPE_LEGIT_REDUP = frozenset("""慢慢 刚刚 天天 人人 常常 往往 渐渐 仅仅 统统 恰恰 微微 轻轻 深深 久久 缓缓 悄悄 匆匆 淡淡 默默 徐徐 频频 高高 远远 好好 多多 早早 爸爸 妈妈 哥哥 姐姐 弟弟 妹妹 爷爷 奶奶 叔叔 星星""".split())
_SHAPE_FOLD_WINDOW_MS = 5000
# SUBTITLE-DEEP-1 Phase C：折叠删掉叠用词后，其各自尾标点在接缝处连用
# （所以，所以，→折叠→，，，）——挤压同族标点串并剥句首标点；无标点输入逐位不变。
_SHAPE_FOLD_PUNCT_RUN = re.compile(r"([，。？！、；：,.!?;:])[，。？！、；：,.!?;:]+")
_SHAPE_FOLD_LEADING_PUNCT = re.compile(r"^[，。？！、；：,.!?;:]+")


# 折叠纯函数文本级 memo（VTT-PERF）：同文 cue 重复极多（口吃语料/合成讲次），
# memo 把每行全量扫描摊薄为一次。GIL 下 dict 读写原子；并发未命中重复计算无害。
# FIFO 封顶防无界增长（8192 条 ≈ 1MB 文本级）。
_COLLAPSE_MEMO: "OrderedDict[str, tuple[str, int]]" = OrderedDict()
_COLLAPSE_MEMO_LIMIT = 8192
_COLLAPSE_MEMO_LOCK = threading.Lock()


def _collapse_repeated_tokens(text: str) -> tuple[str, int]:
    """折叠文本内 ASR 连续重复字词（R3 残叠/R1 立即串/R2 块级重复），零增字。

    与 worker 侧同一算法两处落地（client/worker 分域无共享模块）。
    R2=块级重复——连续两个及以上的等长块（2/3/4/6/8 字，词级口吃在字面
    即 4 字块 ABCDABCD）→保留首块。

    VTT-PERF：本函数纯（输出只依赖 text），文本级 memo 语义零变化；无重复
    token 且无 R3 相邻对的文本走必要条件快径，跳过 O(n²) 块级扫描。
    """
    with _COLLAPSE_MEMO_LOCK:
        hit = _COLLAPSE_MEMO.get(text)
        if hit is not None:
            _COLLAPSE_MEMO.move_to_end(text)
            return hit
    result = _collapse_repeated_tokens_uncached(text)
    with _COLLAPSE_MEMO_LOCK:
        _COLLAPSE_MEMO[text] = result
        _COLLAPSE_MEMO.move_to_end(text)
        while len(_COLLAPSE_MEMO) > _COLLAPSE_MEMO_LIMIT:
            _COLLAPSE_MEMO.popitem(last=False)
    return result


def _collapse_repeated_tokens_uncached(text: str) -> tuple[str, int]:
    matches = list(_SHAPE_TOKEN_RE.finditer(text))
    if len(matches) < 2:
        return text, 0
    values = [m.group(0) for m in matches]
    # 必要条件快径（VTT-PERF，语义等价）：R1/R2 折叠都需要存在重复 token，
    # R3 需要相邻「单字 token + 以该字开头的 token」对。两者皆无时任何折叠
    # 都不可能触发，直接原样返回——无口吃语料的主路径从全量扫描降为一次去重。
    if len(set(values)) == len(values) and not any(
        len(values[k]) == 1 and len(values[k + 1]) >= 2
        and values[k + 1].startswith(values[k])
        for k in range(len(values) - 1)
    ):
        return text, 0
    drop = [False] * len(values)
    folds = 0
    changed = True
    while changed:
        changed = False
        live = [i for i, d in enumerate(drop) if not d]
        vals = [values[i] for i in live]
        # R3：单字残叠
        for k in range(len(vals) - 1):
            a, b = vals[k], vals[k + 1]
            if len(a) == 1 and len(b) >= 2 and b.startswith(a):
                drop[live[k]] = True
                folds += 1
                changed = True
                break
        if changed:
            continue
        # R1：立即同字串
        for k in range(len(vals)):
            run = 1
            while k + run < len(vals) and vals[k + run] == vals[k]:
                run += 1
            if run >= 2:
                tok = vals[k]
                if not (len(tok) == 1 and run == 2 and (tok + tok) in _SHAPE_LEGIT_REDUP):
                    for j in range(k + 1, k + run):
                        drop[live[j]] = True
                    folds += 1
                    changed = True
                    break
        if changed:
            continue
        # R2：块级重复（块长 2/3/4/6/8 字）
        # C2-3FIX-1 等价重写（VTT-PERF，语义逐位一致）：旧扫描对每个 k 切片
        # 建集合作全同检查，单轮 O(5n²) 且每次折叠迭代重付——真实讲次语料
        # （逐 cue 文本唯一、句内重复词常见）下是冷开 6s 级的主导项。等值位表
        # 重写：B_k==B_{k+1} ⟺ eq[k..k+length-1] 全真；同一 True 段内链式块
        # 彼此相等（传递性），段首块全同⇒整段皆单字重复串（归 R1）整体跳过，
        # 段首块非全同即为该块长下旧扫描会选中的首个折叠点；reps/残叠尾规则
        # 按原步进语义逐位保持。
        if len(set(vals)) == len(vals):
            break
        folded_by_r2 = False
        for length in (2, 3, 4, 6, 8):
            n_vals = len(vals)
            if n_vals < 2 * length:
                continue
            eq = [vals[i] == vals[i + length] for i in range(n_vals - length)]
            pos = 0
            eq_limit = len(eq)
            while pos < eq_limit:
                if not eq[pos]:
                    pos += 1
                    continue
                run_start = pos
                while pos < eq_limit and eq[pos]:
                    pos += 1
                run_end = pos
                k = run_start
                block = vals[k:k + length]
                if block.count(block[0]) == length:
                    continue  # 单字重复串归 R1 管（链式块全同，整段跳过）
                j = k + length
                reps = 1
                # 首个扩展窗口=eq[k:k+length]（即 run 本身，B_k==B_{k+1}）；
                # 其后逐块前移，与旧扫描的 while 切片比较逐窗口对应
                while j + length <= n_vals and all(eq[j - length:j]):
                    reps += 1
                    j += length
                if reps < 2:
                    continue
                # 尾部残叠仅当其为块前缀时收敛（我我也我也→我也）；
                # 非前缀的后续正文（这个这个这样）原样保留。
                partial = vals[j:n_vals]
                if partial and len(partial) < length and partial == block[:len(partial)]:
                    j += len(partial)
                for t in range(k + length, j):
                    drop[live[t]] = True
                folds += reps - 1
                changed = True
                folded_by_r2 = True
                break
            if folded_by_r2:
                break
            if changed:
                break
    pieces = []
    last = 0
    for m, d in zip(matches, drop):
        if d:
            pieces.append(text[last:m.start()])
            last = m.end()
    pieces.append(text[last:])
    joined = "".join(pieces)
    joined = _SHAPE_FOLD_PUNCT_RUN.sub(r"\1", joined)
    joined = _SHAPE_FOLD_LEADING_PUNCT.sub("", joined)
    return joined, folds

def shape_display_cues_with_stats(
    segments: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, int]]:
    """确定性整形（带统计）：合并碎片/删纯语气词/应答词并入/句首剥离/安全拆分。

    纯函数：不改输入行。输出行携源段锚=``source_ids``（原始行号列表，从 1
    起）与 ``source_evidence_ids``（源行带证据 ID 时）；单源行原样透传全部
    字段（含 evidence_id），合并行绝不携带单段 evidence_id（身份属源段，
    绝不冒领）。``stats["residual_overlong"]``=无安全边界的超长保留句计数。
    """
    stats = {
        "input": len(segments), "output": 0, "deleted_fillers": 0,
        "answer_attached": 0, "merged_units": 0, "split_units": 0,
        "soft_split_units": 0, "residual_overlong": 0,
        "folded_tokens": 0, "merged_adjacent_dups": 0,
    }
    rows = [dict(row) for row in segments]
    rows.sort(key=lambda row: (int(row.get("start_ms") or 0), int(row.get("end_ms") or 0)))

    # ⓪ 夜10-C 第九波：邻接重复折叠——口齿不清的 ASR 连续重复字词（段内
    # token 串折叠+相邻同文段合并）。存量讲次的展示面即时受益；确定性、
    # 零增字、合法叠词保留。
    folded_rows: list[dict[str, object]] = []
    for row in rows:
        text, folds = _collapse_repeated_tokens(str(row.get("text") or ""))
        if folds:
            row["text"] = text
            stats["folded_tokens"] += folds
        if folded_rows:
            prev = folded_rows[-1]
            gap = int(row.get("start_ms") or 0) - int(prev.get("end_ms") or 0)
            if (
                str(prev.get("text") or "").strip()
                and prev.get("text") == row.get("text")
                and 0 <= gap <= _SHAPE_FOLD_WINDOW_MS
            ):
                prev["end_ms"] = max(
                    int(prev.get("end_ms") or 0), int(row.get("end_ms") or 0)
                )
                stats["merged_adjacent_dups"] += 1
                continue
        folded_rows.append(row)
    rows = folded_rows

    # ① 句首语气词剥离 + 纯语气词删除 + 应答词暂挂（单源行原字段原样透传）
    candidates: list[dict[str, object]] = []
    pending_answers: list[dict[str, object]] = []
    for position, row in enumerate(rows, 1):
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        if _shape_is_pure_filler(text):
            stats["deleted_fillers"] += 1
            continue
        stripped = _SHAPE_LEADING_FILLER_RE.sub("", text).strip()
        unit = dict(row)
        unit["text"] = stripped or text
        unit["start_ms"] = int(row.get("start_ms") or 0)
        unit["end_ms"] = int(row.get("end_ms") or 0)
        unit["source_ids"] = [position]
        evidence_id = row.get("evidence_id")
        if is_evidence_id(evidence_id):
            unit["source_evidence_ids"] = [str(evidence_id)]
        if _shape_is_answer_word(stripped or text):
            pending_answers.append(unit)
            continue
        if pending_answers:
            # 应答词并入下一条开头作句头前缀（零标点、零改写）；夜10-C 边界：
            # ①与下一条间隔超 3s 不并入（防显示起点悬挂进长静音/语义漂移）；
            # ②并入后总长超拆分上限（30 字）不并入（防制造新的无边界超长句）；
            # ③前缀体去边缘标点（防句中标点伪影）。未并入的应答词按序独立保留，
            # 交由通用贴并规则按其间隔与字数预算自行处置。
            prefix = "".join(_shape_prefix_text(item) for item in pending_answers)
            gap_ok = (
                int(unit["start_ms"]) - int(pending_answers[-1]["end_ms"])
                <= SHAPE_ANSWER_ATTACH_MAX_GAP_MS
            )
            budget_ok = len(prefix) + len(str(unit["text"])) <= SHAPE_SPLIT_MAX_CHARS
            if gap_ok and budget_ok:
                unit["text"] = f"{prefix}{unit['text']}"
                unit["source_ids"] = (
                    [item["source_ids"][0] for item in pending_answers] + unit["source_ids"]
                )
                unit["start_ms"] = int(pending_answers[0]["start_ms"])
                prior_ids = [
                    item["source_evidence_ids"][0]
                    for item in pending_answers if item.get("source_evidence_ids")
                ]
                if prior_ids or unit.get("source_evidence_ids"):
                    unit["source_evidence_ids"] = prior_ids + list(
                        unit.get("source_evidence_ids") or []
                    )
                pending_answers = []
                stats["answer_attached"] += 1
            else:
                candidates.extend(pending_answers)
                pending_answers = []
        candidates.append(unit)
    # 流末尾悬空的应答词：保留为独立 cue（不直删）
    for item in pending_answers:
        candidates.append(item)

    # ② 贴并：间隔 ≤1.5s，或 ≤6 字碎片 <1s；拼后 ≤28 字即停
    units: list[dict[str, object]] = []
    for unit in candidates:
        if units:
            previous = units[-1]
            gap = int(unit["start_ms"]) - int(previous["end_ms"])
            char_total = len(str(previous["text"])) + len(str(unit["text"]))
            mergeable = (
                gap <= SHAPE_MERGE_GAP_MS
                or (
                    len(str(previous["text"])) <= SHAPE_SHORT_CUE_CHARS
                    and gap < SHAPE_SHORT_CUE_GAP_MS
                )
            ) and char_total <= SHAPE_MERGE_MAX_CHARS
            if mergeable:
                previous["text"] = f"{previous['text']}{unit['text']}"
                previous["end_ms"] = max(int(previous["end_ms"]), int(unit["end_ms"]))
                previous["source_ids"] = previous["source_ids"] + unit["source_ids"]
                merged_ids = list(previous.get("source_evidence_ids") or [])
                merged_ids += list(unit.get("source_evidence_ids") or [])
                if merged_ids:
                    previous["source_evidence_ids"] = merged_ids
                previous.pop("evidence_id", None)  # 合并行不冒领单段身份
                previous["_merged"] = True
                continue
        units.append(unit)
    stats["merged_units"] = sum(1 for unit in units if unit.pop("_merged", False))

    # ③ >30 字安全拆分：仅中文句读边界；无边界保留整句并计数，禁硬截断
    shaped: list[dict[str, object]] = []
    for unit in units:
        text = str(unit["text"])
        if len(text) <= SHAPE_SPLIT_MAX_CHARS:
            shaped.append(unit)
            continue
        clauses = [part for part in _CLAUSE_SPLIT_RE.split(text) if part]
        # Phase C 行长均衡：句读装箱先走 [12,22] 软目标带；单句读超硬帽或
        # 产不出 ≥2 块时退回旧硬帽贪心（任一句读 >30 仍走软边界/保留路径）。
        balanced = _shape_pack_balanced(clauses, SHAPE_SPLIT_MAX_CHARS)
        if balanced is not None:
            chunks = balanced
            soft = False
        else:
            chunks = []
            buffer = ""
            overflow = False
            for clause in clauses:
                if len(clause) > SHAPE_SPLIT_MAX_CHARS:
                    overflow = True
                    break
                if buffer and len(buffer) + len(clause) > SHAPE_SPLIT_MAX_CHARS:
                    chunks.append(buffer)
                    buffer = clause
                else:
                    buffer += clause
            if buffer:
                chunks.append(buffer)
            soft = False
            if overflow or len(chunks) <= 1:
                # 无标点句读边界：退到话轮标记词软拆分（夜10-C）；仍无安全边界则
                # 保留整句（绝不硬截断），计数上报
                soft_chunks = _shape_soft_chunks(
                    text, SHAPE_SPLIT_MAX_CHARS, SHAPE_SOFT_BOUNDARY_MIN_CHUNK,
                )
                if soft_chunks is None:
                    stats["residual_overlong"] += 1
                    shaped.append(unit)
                    continue
                chunks = soft_chunks
                soft = True
        if soft:
            stats["soft_split_units"] += 1
        else:
            stats["split_units"] += 1
        boundaries = _split_timing(
            int(unit["start_ms"]), int(unit["end_ms"]), chunks, min_duration_ms=200,
        )
        for position, chunk in enumerate(chunks):
            child = dict(unit)
            child["text"] = chunk
            child["start_ms"] = boundaries[position]
            child["end_ms"] = boundaries[position + 1]
            child["source_ids"] = list(unit["source_ids"])
            if unit.get("source_evidence_ids"):
                child["source_evidence_ids"] = list(unit["source_evidence_ids"])
            shaped.append(child)

    # ④ 合并单元（源段 ≥2）拖尾/补时：+0.5s、不越过下一条 start、不悬挂 >1.5s 静音
    for position, unit in enumerate(shaped):
        if len(unit.get("source_ids") or []) < 2:
            continue
        next_start = (
            int(shaped[position + 1]["start_ms"]) if position + 1 < len(shaped) else None
        )
        end_ms = int(unit["end_ms"])
        target = end_ms + SHAPE_TAIL_MS
        target = max(target, min(
            int(unit["start_ms"]) + SHAPE_MIN_DISPLAY_MS,
            end_ms + SHAPE_MAX_SILENCE_HANG_MS,
        ))
        if next_start is not None:
            target = min(target, max(end_ms, next_start))
        unit["end_ms"] = max(end_ms, target)

    for index, unit in enumerate(shaped, 1):
        unit["index"] = index
    stats["output"] = len(shaped)
    return shaped, stats


def shape_display_cues(segments: list[dict[str, object]]) -> list[dict[str, object]]:
    """确定性整形（展示面入口）：细节与统计口径见 shape_display_cues_with_stats。"""
    shaped, _stats = shape_display_cues_with_stats(segments)
    return shaped


# 进程内整形缓存（AS13）：键=调用方给的源段指纹（路径/mtimes/行数等）。
# 小容量 FIFO、零落盘零持久化；命中行共享只读——消费方不得改写行内容。
_SHAPE_CACHE_LOCK = threading.Lock()
_SHAPE_CACHE: "OrderedDict[tuple, list[dict[str, object]]]" = OrderedDict()
_SHAPE_CACHE_LIMIT = 12


def shape_display_cues_cached(
    key: tuple, segments: list[dict[str, object]],
) -> list[dict[str, object]]:
    """带进程内缓存的确定性整形入口（字幕文件路由与阅读面板共用）。"""
    with _SHAPE_CACHE_LOCK:
        hit = _SHAPE_CACHE.get(key)
        if hit is not None:
            _SHAPE_CACHE.move_to_end(key)
            return hit
    shaped = shape_display_cues(segments)
    with _SHAPE_CACHE_LOCK:
        _SHAPE_CACHE[key] = shaped
        while len(_SHAPE_CACHE) > _SHAPE_CACHE_LIMIT:
            _SHAPE_CACHE.popitem(last=False)
    return shaped


# ---- VTT-PERF 展示层 cue 落盘缓存 ------------------------------------------
# 解析一次、多开复用：subtitle_segments 的最终展示行（整形后）按源指纹落盘。
# 纯优化层：任何 miss/损坏/口径不符都回落原链路重算，绝不影响正确性；
# 证据层（learning_store transcript_segments）与 storage schema 一字不动。
# 失效=文件名内嵌整形口径 tag + 文件体内嵌源指纹（sub_id/path/mtime/size），
# 源文件或整形口径任一变化即换新文件，旧文件由写入方收敛删除。

DISPLAY_CACHE_FORMAT = 1


def display_cache_tag() -> str:
    """整形口径指纹：展示配置常量任一变化 → tag 变 → 旧缓存自然失效。

    改折叠/整形算法逻辑（而非常量）时必须手动递增 DISPLAY_CACHE_FORMAT。
    """
    config = {
        "format": DISPLAY_CACHE_FORMAT,
        "split_defaults": (44, 200),
        "merge": [
            SHAPE_MERGE_MAX_CHARS, SHAPE_MERGE_GAP_MS, SHAPE_SHORT_CUE_CHARS,
            SHAPE_SHORT_CUE_GAP_MS, SHAPE_MIN_DISPLAY_MS, SHAPE_TAIL_MS,
            SHAPE_MAX_SILENCE_HANG_MS, SHAPE_SPLIT_MAX_CHARS,
            SHAPE_BALANCE_MIN_CHARS, SHAPE_BALANCE_MAX_CHARS,
            SHAPE_SOFT_BOUNDARY_MIN_CHUNK, _SHAPE_FOLD_WINDOW_MS,
        ],
        "lexicon": [
            SHAPE_FILLER_CHARS, list(SHAPE_ANSWER_WORDS),
            sorted(_SHAPE_LEGIT_REDUP), list(SHAPE_SOFT_BOUNDARY_MARKERS),
        ],
    }
    return hashlib.sha256(canonical_json(config).encode("utf-8")).hexdigest()[:16]


def display_cache_path(
    cache_dir: Path, sub_id: object, *, source_path: str, source_mtime_ns: int, source_size: int,
) -> Path:
    """单讲次缓存文件路径：文件名即失效键（sub 哈希 + 源指纹 + 口径 tag）。"""
    digest = hashlib.sha256(str(sub_id).encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{digest}-{int(source_mtime_ns)}-{int(source_size)}-{display_cache_tag()}.json"


def load_display_cache(path: Path, key: list) -> list[dict[str, object]] | None:
    """读缓存：格式/口径/指纹任一不符或文件损坏一律按 miss 处理（返回 None）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("cache_format") != DISPLAY_CACHE_FORMAT:
        return None
    if payload.get("tag") != display_cache_tag() or payload.get("key") != key:
        return None
    segments = payload.get("segments")
    if not isinstance(segments, list) or not all(isinstance(row, dict) for row in segments):
        return None
    return segments


def store_display_cache(path: Path, key: list, segments: list[dict[str, object]]) -> None:
    """原子落盘展示行并收敛同讲次旧指纹文件（尽力而为；失败静默不影响读链）。"""
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cache_format": DISPLAY_CACHE_FORMAT,
            "tag": display_cache_tag(),
            "key": key,
            "segments": segments,
        }
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, path)
        prefix = path.name.split("-", 1)[0]
        for stale in path.parent.glob(f"{prefix}-*.json"):
            if stale.name != path.name:
                try:
                    stale.unlink()
                except OSError:
                    pass
    except OSError:
        return
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = [
    "assign_fallback_evidence_ids",
    "display_cache_path",
    "display_cache_tag",
    "is_evidence_id",
    "load_display_cache",
    "parse_subtitle_file",
    "parse_subtitle_text",
    "parse_timestamp_ms",
    "sanitize_evidence_metadata",
    "segment_evidence_id",
    "shape_display_cues",
    "shape_display_cues_cached",
    "shape_display_cues_with_stats",
    "split_long_cues",
    "store_display_cache",
]
