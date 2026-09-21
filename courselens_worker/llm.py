"""DeepSeek-backed proofreading, grounded answers, and summaries."""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import requests

from .formats import normalize_segments
from .glossary import apply_glossary

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"  # N5A-P6：官方 2026-07-24 停用 deepseek-chat 别名
_USAGE_LOCK = threading.RLock()
_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


class LLMError(RuntimeError):
    pass


def reset_usage() -> None:
    with _USAGE_LOCK:
        for key in _USAGE:
            _USAGE[key] = 0


def usage_snapshot() -> dict[str, int]:
    with _USAGE_LOCK:
        return dict(_USAGE)


def _chat(api_key: str, messages: list[dict[str, str]], *, max_tokens: int = 8192) -> str:
    if not api_key:
        raise LLMError("the encrypted job does not contain an AI API key")
    payload = {"model": MODEL, "messages": messages, "temperature": 0.1, "max_tokens": max_tokens}
    last_status = 0
    for attempt in range(4):
        try:
            response = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=180,
            )
        except requests.RequestException as exc:
            if attempt == 3:
                raise LLMError(f"AI request failed: {type(exc).__name__}") from exc
            time.sleep(2 ** attempt)
            continue
        last_status = response.status_code
        if response.status_code == 200:
            try:
                value = response.json()
                usage = dict(value.get("usage") or {})
                with _USAGE_LOCK:
                    for key in _USAGE:
                        _USAGE[key] += max(0, int(usage.get(key) or 0))
                return str(value["choices"][0]["message"]["content"])
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMError("AI response shape is invalid") from exc
        if response.status_code not in {408, 409, 429, 500, 502, 503, 504}:
            break
        retry_after = response.headers.get("Retry-After")
        try:
            delay = min(30.0, float(retry_after)) if retry_after else float(2 ** attempt)
        except ValueError:
            delay = float(2 ** attempt)
        time.sleep(delay)
    raise LLMError(f"AI request returned HTTP {last_status or 'unknown'}")


def _json_content(text: str) -> Any:
    value = text.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise LLMError("AI response is not valid JSON") from exc


# Bounded correction contract for the proofread stage.  Candidates are paired
# by absolute anchors instead of array position, the model may only propose
# bounded replacement operations tied to known pair ids, and every proposal
# fails closed back to the primary (FireRed) text unless it validates
# deterministically.  The pairing marker inside checkpoints separates
# resumable state from legacy positional checkpoints, which are restarted.
PROOFREAD_PAIRING = "temporal-overlap"
_PROOFREAD_WINDOW = 20
_PAIR_NEAREST_GAP_MS = 2000
_MAX_OPS_PER_PAIR = 4
_MAX_OP_GROWTH = 16
_PROOFREAD_INSTRUCTIONS = (
    "你是严谨的中文课程字幕校对器。输入的每项包含 id、绝对毫秒锚 start_ms/end_ms、"
    "主要识别文本 text 和另一识别引擎的参考文本 alt（可为空）。"
    "有的输入项还包含 slide 字段，即该时段幻灯片的 OCR 文本，"
    "仅可作为术语与专有名词写法的参考证据，不是改写依据。"
    '只输出 JSON 数组，每项形如 {"id":"p0","old":"原文子串","new":"替换文本"}：'
    "old 必须原样出现在对应 text 中，new 只替换该子串。"
    "只修正明显的识别错误；禁止改写、扩写、删句或调整顺序；"
    "数字、单位、公式和否定词不得改动，除非 alt 支持相同写法；无法确定时不要输出该项。"
)
# Evidence fields copied from the chosen primary candidate.  tokens and
# segment_id describe the uncorrected text, so they are dropped whenever the
# text changes; token timing is never fabricated or re-aligned.
_PRIMARY_EVIDENCE_KEYS = ("segment_id", "evidence_id", "source_hash", "provenance", "tokens", "lang")
_CORRECTION_STATUSES = (
    "applied",
    "rejected-shape",
    "rejected-target",
    "rejected-ambiguous",
    "rejected-budget",
    "rejected-protected",
    "unpaired",
    "applied-glossary",  # N5A-P3：课程词表规则纠错（确定性后处理，第八态）
)
# N5A-P2：summary 合并调用顺带输出的考核事件类别闭集（与客户端台账同源）
ASSESSMENT_CATEGORIES = (
    "exam", "resit", "quiz", "assignment", "project", "lab", "computer_lab",
    "attendance", "rollcall", "schedule_change", "qa_session",
)
# Protected spans: formula-like tokens (must contain an operator), numbers
# with separators and optional unit suffixes, negation words, and runs of
# Chinese numerals.  A replacement may not alter the protected-form sequence
# unless the alternate ASR candidate supports the change.
_PROTECTED_FORM_RE = re.compile(
    "|".join((
        r"[A-Za-z0-9Α-Ωα-ωμ°²³√∑∫∞≠≤≥±×÷_=+*/%^.,:;()\[\]<>|&-]*"
        r"[=+*/%^×÷≠≤≥±-]"
        r"[A-Za-z0-9Α-Ωα-ωμ°²³√∑∫∞≠≤≥±×÷_=+*/%^.,:;()\[\]<>|&-]*",
        r"[0-9０-９]+(?:[.．,:：/][0-9０-９]+)*"
        r"(?:℃|％|°|万|亿|千米|公里|千克|公斤|毫克|厘米|毫米|毫升|立方米|摄氏度|华氏度"
        r"|[A-Za-zμ%]{1,6}|米|克|吨|升|元|页|章|节|年|月|日|天|时|分|秒|次|人|倍)?",
        r"不能|不会|没有|无法|不|没|未|无|非|别|勿|莫",
        r"[零〇一二两三四五六七八九十百千万亿]{2,}",
    ))
)


def _protected_forms(text: str) -> list[str]:
    return [match.group(0) for match in _PROTECTED_FORM_RE.finditer(text)]


def _protected_change(original: str, result: str, alternate: str) -> bool:
    """True when a replacement alters protected spans without alternate support.

    Added forms must already appear in the alternate candidate's protected
    forms; a removed form is rejected when the alternate corroborates the
    original hearing.  Without an alternate, every protected change fails.
    """
    before = _protected_forms(original)
    after = _protected_forms(result)
    if before == after:
        return False
    if not alternate:
        return True
    alt_forms = set(_protected_forms(alternate))
    added = Counter(after) - Counter(before)
    removed = Counter(before) - Counter(after)
    if not added and not removed:
        return True
    if any(form not in alt_forms for form in added):
        return True
    if any(form in alt_forms for form in removed):
        return True
    return False


def _pair_alternates(
    primaries: list[dict[str, Any]],
    alternates: list[dict[str, Any]],
    *,
    nearest_gap_ms: int = _PAIR_NEAREST_GAP_MS,
) -> list[dict[str, Any] | None]:
    """Bind each primary candidate to at most one alternate deterministically.

    Overlapping intervals win over nearest intervals; ties prefer the larger
    overlap, then the smaller boundary distance, then the earlier alternate.
    One alternate may support several primaries; a primary with no alternate
    inside the bounded gap stays unmatched (None).
    """
    alts = sorted(alternates, key=lambda item: int(item.get("start_ms") or 0))
    partners: list[dict[str, Any] | None] = [None] * len(primaries)
    low = 0
    for index, primary in enumerate(primaries):
        p_start = int(primary.get("start_ms") or 0)
        p_end = int(primary.get("end_ms") or p_start)
        while low < len(alts) and int(alts[low].get("end_ms") or 0) < p_start - nearest_gap_ms:
            low += 1
        best: dict[str, Any] | None = None
        best_key: tuple[int, int, int, int] | None = None
        for offset in range(low, len(alts)):
            alt = alts[offset]
            a_start = int(alt.get("start_ms") or 0)
            a_end = int(alt.get("end_ms") or a_start)
            if a_start > p_end + nearest_gap_ms:
                break
            overlap = min(p_end, a_end) - max(p_start, a_start)
            distance = abs(p_start - a_start) + abs(p_end - a_end)
            if overlap > 0:
                key = (1, overlap, -distance, -a_start)
            else:
                gap = max(p_start - a_end, a_start - p_end)
                if gap > nearest_gap_ms:
                    continue
                key = (0, -gap, -distance, -a_start)
            if best_key is None or key > best_key:
                best_key = key
                best = alt
        partners[index] = best
    return partners


def _correct_window(chunk: list[dict[str, Any]], ops: Any) -> list[dict[str, Any]]:
    """Apply bounded replacement ops to one window, failing closed per pair."""
    if not isinstance(ops, list):
        raise LLMError("proofreading response must be a JSON array")
    texts: dict[str, str] = {}
    lengths: dict[str, int] = {}
    alt_texts: dict[str, str] = {}
    for pair in chunk:
        pair_id = pair["wire"]["id"]
        texts[pair_id] = pair["wire"]["text"]
        lengths[pair_id] = len(pair["wire"]["text"])
        alt_texts[pair_id] = pair["alt_text"]
    seen_ops: set[tuple[str, str]] = set()
    applied_counts: dict[str, int] = {}
    rejected: dict[str, str] = {}
    for op in ops:
        if not isinstance(op, dict):
            continue
        pair_id = op.get("id")
        if not isinstance(pair_id, str) or pair_id not in texts:
            continue
        old = op.get("old")
        new = op.get("new")
        if not isinstance(old, str) or not old or not isinstance(new, str) or new == old:
            rejected.setdefault(pair_id, "rejected-shape")
            continue
        if (pair_id, old) in seen_ops:
            continue
        seen_ops.add((pair_id, old))
        if applied_counts.get(pair_id, 0) >= _MAX_OPS_PER_PAIR:
            continue
        text = texts[pair_id]
        occurrences = text.count(old)
        if occurrences != 1:
            rejected.setdefault(pair_id, "rejected-ambiguous" if occurrences > 1 else "rejected-target")
            continue
        result = " ".join(text.replace(old, new).split()).strip()
        budget = max(8, lengths[pair_id] // 4)
        if (
            not result
            or len(new) > len(old) + _MAX_OP_GROWTH
            or abs(len(result) - lengths[pair_id]) > budget
        ):
            rejected.setdefault(pair_id, "rejected-budget")
            continue
        if _protected_change(text, result, alt_texts[pair_id]):
            rejected.setdefault(pair_id, "rejected-protected")
            continue
        texts[pair_id] = result
        applied_counts[pair_id] = applied_counts.get(pair_id, 0) + 1
    segments: list[dict[str, Any]] = []
    for pair in chunk:
        wire = pair["wire"]
        pair_id = wire["id"]
        primary = pair["primary"]
        text = texts[pair_id]
        changed = text != " ".join(wire["text"].split()).strip()
        segment: dict[str, Any] = {
            "start_ms": wire["start_ms"],
            "end_ms": wire["end_ms"],
            "text": text,
        }
        for key in _PRIMARY_EVIDENCE_KEYS:
            if changed and key in {"tokens", "segment_id"}:
                continue
            value = primary.get(key)
            if value is not None:
                segment[key] = value
        if applied_counts.get(pair_id):
            segment["correction"] = "applied"
        elif pair_id in rejected:
            segment["correction"] = rejected[pair_id]
        elif not pair["paired"]:
            segment["correction"] = "unpaired"
        segments.append(segment)
    return segments


# Slide context is terminology evidence, not rewrite authority: one bounded,
# whitespace-normalized OCR text per primary segment.
_MAX_SLIDE_CONTEXT_CHARS = 200


def _active_slide_text(pages: list[dict[str, Any]] | None, midpoint_ms: int) -> str:
    """Bounded OCR text of the latest non-empty slide at or before the midpoint.

    Deterministic: pages are scanned in order and the first page with the
    greatest ``created_sec`` not after ``midpoint_ms`` wins.  Pages shown
    later than the segment midpoint, and empty OCR texts, never qualify.
    """
    best_sec = -1
    best_text = ""
    for page in pages or []:
        if not isinstance(page, dict):
            continue
        created_sec = int(page.get("created_sec") or 0)
        if created_sec * 1000 > midpoint_ms:
            continue
        text = " ".join(str(page.get("text") or "").split())
        if not text or created_sec <= best_sec:
            continue
        best_sec = created_sec
        best_text = text
    return best_text[:_MAX_SLIDE_CONTEXT_CHARS]


def proofread_segments(
    api_key: str,
    sensevoice: list[dict[str, Any]],
    firered: list[dict[str, Any]],
    *,
    prior_checkpoint: dict[str, Any] | None = None,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
    ppt_pages: list[dict[str, Any]] | None = None,
    glossary: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    primaries = normalize_segments(firered)
    alternates = normalize_segments(sensevoice)
    partners = _pair_alternates(primaries, alternates)
    prior = dict(prior_checkpoint or {})
    # Only checkpoints written under this pairing can be resumed window by
    # window.  A legacy positional checkpoint carries unvalidated whole-string
    # rewrites that cannot be aligned to anchor-based pairs, so the stage
    # restarts from window 0 instead of trusting its segment list.
    trusted = prior.get("proofread_pairing") == PROOFREAD_PAIRING
    output: list[dict[str, Any]] = list(prior.get("proofread_segments") or []) if trusted else []
    total_windows = (len(primaries) + _PROOFREAD_WINDOW - 1) // _PROOFREAD_WINDOW
    completed = (
        max(0, min(total_windows, int(prior.get("proofread_completed_windows") or 0)))
        if trusted
        else 0
    )

    def pairs_for(window_index: int) -> list[dict[str, Any]]:
        chunk: list[dict[str, Any]] = []
        base = window_index * _PROOFREAD_WINDOW
        for offset in range(base, min(len(primaries), base + _PROOFREAD_WINDOW)):
            primary = primaries[offset]
            partner = partners[offset]
            start_ms = int(primary.get("start_ms") or 0)
            end_ms = int(primary.get("end_ms") or 0)
            wire: dict[str, Any] = {
                "id": f"p{offset}",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": str(primary.get("text") or ""),
                "alt": str(partner.get("text") or "") if partner is not None else "",
            }
            slide_text = _active_slide_text(ppt_pages, (start_ms + end_ms) // 2)
            if slide_text:
                wire["slide"] = slide_text
            chunk.append({
                "primary": primary,
                "paired": partner is not None,
                "alt_text": str(partner.get("text") or "") if partner is not None else "",
                "wire": wire,
            })
        return chunk

    def request_window(chunk: list[dict[str, Any]]) -> Any:
        return _json_content(_chat(api_key, [
            {"role": "system", "content": _PROOFREAD_INSTRUCTIONS},
            {"role": "user", "content": json.dumps(
                [pair["wire"] for pair in chunk], ensure_ascii=False)},
        ]))

    for batch_start in range(completed, total_windows, 2):
        indices = list(range(batch_start, min(total_windows, batch_start + 2)))
        batch = {index: pairs_for(index) for index in indices}
        with ThreadPoolExecutor(max_workers=min(2, len(indices)), thread_name_prefix="llm-proofread") as executor:
            futures = {index: executor.submit(request_window, batch[index]) for index in indices}
            responses = {index: futures[index].result() for index in indices}
        for window_index in indices:
            output.extend(_correct_window(batch[window_index], responses[window_index]))
            if checkpoint is not None:
                checkpoint({
                    "stage": "proofread",
                    "proofread_pairing": PROOFREAD_PAIRING,
                    "proofread_completed_windows": window_index + 1,
                    "proofread_total_windows": total_windows,
                    "proofread_segments": normalize_segments(output),
                })
    return apply_glossary(normalize_segments(output), glossary)  # N5A-P3 一行挂点


_SUMMARY_MERGE_PROMPT = (
    "合并各窗口笔记为完整中文学习笔记，不得增加输入外事实。输出 JSON 对象，"
    "字段为 markdown 和 chapters；保留原有合法 start_ms，"
    "另输出 key_takeaways（≤6条、每条≤30字）"
    "和 assessment_events（仅当提到课程考核，每项含 "
    "category/title/due_hint/quote，category 仅限{"
    + ",".join(ASSESSMENT_CATEGORIES)
    + "}，quote 须为原文，无则空数组）"
)


def create_summary(
    api_key: str,
    *,
    title: str,
    transcript: list[dict[str, Any]],
    ppt_pages: list[dict[str, Any]],
    prior_checkpoint: dict[str, Any] | None = None,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    transcript_windows = [transcript[start:start + 120] for start in range(0, len(transcript), 120)]
    if not transcript_windows:
        transcript_windows = [[] for _ in range(max(1, (len(ppt_pages) + 19) // 20))]
    sources = []
    for index, transcript_window in enumerate(transcript_windows):
        if transcript_window:
            lower = int(transcript_window[0].get("start_ms") or 0)
            upper = int(transcript_window[-1].get("end_ms") or lower)
            pages = [page for page in ppt_pages if lower <= int(page.get("created_sec") or 0) * 1000 <= upper]
        else:
            pages = ppt_pages[index * 20:(index + 1) * 20]
        sources.append({"transcript": transcript_window, "ppt_pages": pages})

    prior = dict(prior_checkpoint or {})
    parts: list[dict[str, Any]] = list(prior.get("summary_parts") or [])
    completed = max(0, min(len(sources), int(prior.get("summary_completed_windows") or 0)))

    def summarize_window(index: int) -> dict[str, Any]:
        part = _json_content(_chat(api_key, [
            {"role": "system", "content": "你是严谨的课程学习助理。仅依据输入整理当前窗口，输出 JSON 对象，字段为 markdown 和 chapters；chapters 每项包含 title、start_ms、summary，start_ms 必须来自输入。"},
            {"role": "user", "content": json.dumps(sources[index], ensure_ascii=False)},
        ]))
        if not isinstance(part, dict) or not isinstance(part.get("markdown"), str) or not isinstance(part.get("chapters"), list):
            raise LLMError("summary window response has an invalid shape")
        return part

    for batch_start in range(completed, len(sources), 2):
        indices = list(range(batch_start, min(len(sources), batch_start + 2)))
        with ThreadPoolExecutor(max_workers=min(2, len(indices)), thread_name_prefix="llm-summary") as executor:
            futures = {index: executor.submit(summarize_window, index) for index in indices}
            values = {index: futures[index].result() for index in indices}
        for index in indices:
            parts.append(values[index])
            if checkpoint is not None:
                checkpoint({
                    "stage": "summary",
                    "completed_chunks": index + 1,
                    "total_chunks": len(sources) + 1,
                    "summary_completed_windows": index + 1,
                    "summary_parts": parts,
                })

    value = _json_content(_chat(api_key, [
        {"role": "system", "content": _SUMMARY_MERGE_PROMPT},
        {"role": "user", "content": json.dumps({"title": title, "parts": parts}, ensure_ascii=False)},
    ], max_tokens=12_000))
    if not isinstance(value, dict) or not isinstance(value.get("markdown"), str) or not isinstance(value.get("chapters"), list):
        raise LLMError("summary response has an invalid shape")
    valid_anchors = {int(item.get("start_ms") or 0) for item in transcript}
    valid_anchors.update(int(item.get("created_sec") or 0) * 1000 for item in ppt_pages)
    chapters = []
    for item in value["chapters"]:
        if not isinstance(item, dict):
            continue
        start_ms = int(item.get("start_ms") or 0)
        if start_ms not in valid_anchors:
            continue
        chapters.append({
            "title": str(item.get("title") or "").strip(),
            "start_ms": start_ms,
            "summary": str(item.get("summary") or "").strip(),
        })
    # N5A-P2 顺风车校验（fail-closed 丢项，不失败摘要）：events 闭集+quote
    # 必须是 parts 拼接原文子串；takeaways 截断到 6 条、每条≤60 字。
    parts_text = "".join(
        str(part.get("markdown") or "") for part in parts if isinstance(part, dict)
    )
    raw_events = value.get("assessment_events")
    events, rejected = [], 0
    if isinstance(raw_events, list):
        for item in raw_events[:10]:
            if not isinstance(item, dict):
                rejected += 1
                continue
            category = str(item.get("category") or "")
            quote = str(item.get("quote") or "").strip()[:80]
            title = str(item.get("title") or "").strip()[:30]
            if (
                category not in ASSESSMENT_CATEGORIES
                or not title
                or not quote
                or quote not in parts_text
            ):
                rejected += 1
                continue
            events.append({
                "category": category,
                "title": title,
                "due_hint": str(item.get("due_hint") or "").strip()[:20],
                "quote": quote,
            })
    raw_takeaways = value.get("key_takeaways")
    takeaways = []
    if isinstance(raw_takeaways, list):
        for item in raw_takeaways:
            if len(takeaways) >= 6:
                break
            text = str(item or "").strip()[:60]
            if text:
                takeaways.append(text)
    return {
        "model": MODEL,
        "markdown": value["markdown"].strip(),
        "chapters": chapters,
        "assessment_events": events,
        "assessment_events_rejected": rejected,
        "key_takeaways": takeaways,
    }


def answer_question(
    api_key: str,
    *,
    query: str,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Answer only from caller-provided evidence and preserve citation IDs."""
    allowed = [item for item in evidence if isinstance(item, dict) and item.get("citation_id") and item.get("text")]
    if not allowed:
        return {"answer": "资料不足，无法根据当前课程资料回答。", "citations": [], "grounded": False}
    raw = _json_content(_chat(api_key, [
        {
            "role": "system",
            "content": (
                "你是严谨的课程问答助手。只能依据用户提供的 evidence 回答，禁止补充外部事实。"
                "输出 JSON 对象，字段为 answer、grounded、citations。citations 只能填写输入中的 citation_id；"
                "证据不足时 answer 必须为‘资料不足，无法根据当前课程资料回答。’，grounded 为 false。"
            ),
        },
        {"role": "user", "content": json.dumps({"query": str(query), "evidence": allowed}, ensure_ascii=False)},
    ], max_tokens=4096))
    if not isinstance(raw, dict) or not isinstance(raw.get("answer"), str) or not isinstance(raw.get("citations"), list):
        raise LLMError("answer response has an invalid shape")
    allowed_ids = {str(item["citation_id"]) for item in allowed}
    citations = [str(value) for value in raw["citations"] if str(value) in allowed_ids]
    grounded = bool(raw.get("grounded")) and bool(citations)
    if not grounded:
        return {"answer": "资料不足，无法根据当前课程资料回答。", "citations": [], "grounded": False}
    return {"answer": raw["answer"].strip(), "citations": citations[:8], "grounded": True}
