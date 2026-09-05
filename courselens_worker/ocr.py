"""Bounded, single-threaded OCR for generic slide images."""

from __future__ import annotations

import hashlib
import io
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import numpy as np
from PIL import Image, UnidentifiedImageError
from rapidocr_onnxruntime import RapidOCR

from .source import fetch_bytes, safe_source_error_code

_OCR_LOCAL = threading.local()


def _dhash(image: Image.Image) -> str:
    gray = image.convert("L").resize((9, 8))
    pixels = np.asarray(gray)
    bits = pixels[:, 1:] > pixels[:, :-1]
    value = 0
    for bit in bits.flatten():
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def _engine() -> RapidOCR:
    value = getattr(_OCR_LOCAL, "engine", None)
    if value is None:
        value = RapidOCR()
        _OCR_LOCAL.engine = value
    return value


def _fetch_page(source: dict[str, Any]) -> tuple[bytes, str]:
    """Fetch one slide; a failure is a closed-set skip reason, never fatal.

    Slide hosts intermittently reject the session's primary transport while
    serving the other one, so a source may carry an ``_alternate_source``
    used for one bounded retry — for connection failures and for text
    bodies (an authorization page is a transport failure in disguise).
    When both attempts fail, the primary closed-set reason is kept.
    """
    primary = dict(source or {})
    alternate = primary.pop("_alternate_source", None)
    has_alternate = isinstance(alternate, dict) and bool(alternate)
    try:
        raw = fetch_bytes(primary)
    except Exception as exc:  # the URL, headers, and body must stay private
        if not has_alternate:
            return b"", safe_source_error_code(exc) or "fetch_failed"
        try:
            return fetch_bytes(dict(alternate)), ""
        except Exception:
            return b"", safe_source_error_code(exc) or "fetch_failed"
    if has_alternate and _text_body_kind(raw):
        try:
            alternate_raw = fetch_bytes(dict(alternate))
        except Exception:
            return raw, ""
        if not _text_body_kind(alternate_raw):
            return alternate_raw, ""
    return raw, ""


def _text_body_kind(raw: bytes) -> str:
    """Classify a short in-memory prefix as a non-image text body.

    The check is deliberately conservative: only an explicit HTML doctype or
    ``<html`` tag, or a leading JSON object/array marker, is classified; any
    other body stays ``None`` so Pillow decides, and no upstream error kind is
    ever guessed from the content.
    """
    prefix = raw[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html"):
        return "html_body"
    if prefix[:1] in (b"{", b"["):
        return "json_body"
    return ""


def _ocr_page(index: int, item: dict[str, Any], raw: bytes) -> tuple[dict[str, Any] | None, str]:
    """Recognize one slide; a bad page degrades to a closed-set skip reason.

    A platform slide URL can serve a non-image body (for example an
    authorization page) or a format this Pillow build cannot identify.  One
    such page must skip quietly instead of failing the whole summary job.
    """
    if not raw:
        return None, "empty"
    text_kind = _text_body_kind(raw)
    if text_kind:
        return None, text_kind
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            image = opened.convert("RGB")
    except UnidentifiedImageError:
        return None, "unidentified_image"
    except Exception:
        return None, "decode_failed"
    try:
        fingerprint = _dhash(image)
        result, _elapsed = _engine()(np.asarray(image))
    except Exception:
        return None, "ocr_failed"
    lines = []
    for row in result or []:
        if len(row) >= 2 and str(row[1]).strip():
            lines.append(str(row[1]).strip())
    return {
        "page_num": int(item.get("page_num") or index + 1),
        "created_sec": int(item.get("created_sec") or 0),
        "text": "\n".join(lines),
        "dhash": fingerprint,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
    }, ""


def process_slides(
    slides: list[dict[str, Any]],
    *,
    progress: Callable[[str, int, int], None],
    prior_checkpoint: dict[str, Any] | None = None,
    checkpoint: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Return (recognized pages, skip counts by closed-set reason)."""
    prior = dict(prior_checkpoint or {})
    output: list[dict[str, Any]] = list(prior.get("ppt_pages") or [])
    skipped: dict[str, int] = dict(prior.get("ppt_skipped") or {})
    seen: set[str] = {
        str(item.get("dhash") or "") for item in output if item.get("dhash")
    }
    total = len(slides)
    completed = max(0, min(total, int(prior.get("ocr_completed_items") or 0)))
    prefetch = max(1, min(20, int(os.environ.get("COURSELENS_IMAGE_PREFETCH") or 16)))
    concurrency = max(1, min(2, int(os.environ.get("COURSELENS_OCR_CONCURRENCY") or 1)))
    for batch_start in range(completed, total, prefetch):
        batch_end = min(total, batch_start + prefetch)
        indices = list(range(batch_start, batch_end))
        with ThreadPoolExecutor(max_workers=min(prefetch, len(indices)), thread_name_prefix="image-fetch") as fetch_pool:
            fetched = {
                index: fetch_pool.submit(_fetch_page, dict(slides[index].get("source") or {}))
                for index in indices
            }
            raw_pages = {index: fetched[index].result() for index in indices}
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="ocr") as ocr_pool:
            recognized = {
                index: ocr_pool.submit(_ocr_page, index, slides[index], raw_pages[index][0])
                for index in indices
            }
            for index in indices:
                fetch_reason = raw_pages[index][1]
                page, ocr_reason = recognized[index].result()
                reason = fetch_reason or ocr_reason
                if page is None:
                    if reason:
                        skipped[reason] = int(skipped.get(reason) or 0) + 1
                elif str(page.get("dhash") or "") in seen:
                    skipped["duplicate"] = int(skipped.get("duplicate") or 0) + 1
                else:
                    seen.add(str(page["dhash"]))
                    output.append(page)
                progress("ocr", index + 1, total)
                should_checkpoint = (index + 1) % 5 == 0 or index + 1 == total
                if checkpoint is not None and should_checkpoint:
                    checkpoint({
                        "stage": "ocr",
                        "completed_chunks": index + 1,
                        "total_chunks": total,
                        "ocr_completed_items": index + 1,
                        "ppt_pages": output,
                        "ppt_skipped": skipped,
                    })
        raw_pages.clear()
    return output, {name: count for name, count in skipped.items() if count > 0}
