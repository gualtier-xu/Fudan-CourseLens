"""DeepSeek-backed proofreading, summary, and chapter derivation."""

from __future__ import annotations

import json
import time
from typing import Any

import requests

from .formats import normalize_segments

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"


class LLMError(RuntimeError):
    pass


def _chat(api_key: str, messages: list[dict[str, str]], *, max_tokens: int = 8192) -> str:
    if not api_key:
        raise LLMError("the encrypted job does not contain an AI API key")
    payload = {"model": MODEL, "messages": messages, "temperature": 0.1, "max_tokens": max_tokens}
    last_status = 0
    for attempt in range(3):
        try:
            response = requests.post(
                API_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=180,
            )
        except requests.RequestException as exc:
            if attempt == 2:
                raise LLMError(f"AI request failed: {type(exc).__name__}") from exc
            time.sleep(2 ** attempt)
            continue
        last_status = response.status_code
        if response.status_code == 200:
            try:
                return str(response.json()["choices"][0]["message"]["content"])
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise LLMError("AI response shape is invalid") from exc
        if response.status_code not in {408, 409, 429, 500, 502, 503, 504}:
            break
        time.sleep(2 ** attempt)
    raise LLMError(f"AI request returned HTTP {last_status or 'unknown'}")


def _json_content(text: str) -> Any:
    value = text.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise LLMError("AI response is not valid JSON") from exc


def proofread_segments(
    api_key: str,
    sensevoice: list[dict[str, Any]],
    firered: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    count = max(len(sensevoice), len(firered))
    output: list[dict[str, Any]] = []
    for start in range(0, count, 20):
        pairs = []
        for index in range(start, min(count, start + 20)):
            sense = sensevoice[index] if index < len(sensevoice) else {}
            fire = firered[index] if index < len(firered) else {}
            pairs.append({
                "index": index,
                "start_ms": int(sense.get("start_ms") or fire.get("start_ms") or 0),
                "end_ms": int(sense.get("end_ms") or fire.get("end_ms") or 0),
                "sensevoice": str(sense.get("text") or ""),
                "firered": str(fire.get("text") or ""),
            })
        raw = _chat(api_key, [
            {"role": "system", "content": "你是严谨的中文课程字幕校对器。综合两个识别结果，只修正识别错误，不扩写，不总结。输出 JSON 数组，每项仅含 index 和 text。"},
            {"role": "user", "content": json.dumps(pairs, ensure_ascii=False)},
        ])
        corrected = _json_content(raw)
        if not isinstance(corrected, list):
            raise LLMError("proofreading response must be a JSON array")
        by_index = {int(item["index"]): str(item.get("text") or "") for item in corrected if isinstance(item, dict) and "index" in item}
        for pair in pairs:
            text = by_index.get(pair["index"]) or pair["firered"] or pair["sensevoice"]
            output.append({"start_ms": pair["start_ms"], "end_ms": pair["end_ms"], "text": text})
    return normalize_segments(output)


def create_summary(
    api_key: str,
    *,
    title: str,
    transcript: list[dict[str, Any]],
    ppt_pages: list[dict[str, Any]],
) -> dict[str, Any]:
    source = {
        "title": title,
        "transcript": transcript,
        "ppt_pages": ppt_pages,
    }
    raw = _chat(api_key, [
        {"role": "system", "content": "你是课程学习助理。仅依据输入内容生成中文学习笔记。输出 JSON 对象，字段为 markdown 和 chapters；chapters 是数组，每项含 title、start_ms、summary，start_ms 必须来自已有字幕或 PPT 时间。"},
        {"role": "user", "content": json.dumps(source, ensure_ascii=False)},
    ], max_tokens=12_000)
    value = _json_content(raw)
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
    return {"model": MODEL, "markdown": value["markdown"].strip(), "chapters": chapters}
