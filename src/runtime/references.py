"""Canonical, validated references shared by search and learning features."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any


_HEX_RE = re.compile(r"^[0-9a-f]{16,128}$")


@dataclass(frozen=True)
class ContentReference:
    course_id: str
    sub_id: str
    start_ms: int
    end_ms: int
    source_hash: str
    source: str = "transcript"
    source_ref: str = ""

    def public(self) -> dict[str, Any]:
        return asdict(self)


def source_hash(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return hashlib.sha256(data).hexdigest()


def normalize_reference(value: dict[str, Any]) -> ContentReference:
    if not isinstance(value, dict):
        raise ValueError("reference must be an object")
    course_id = str(value.get("course_id") or "").strip()
    sub_id = str(value.get("sub_id") or "").strip()
    source = str(value.get("source") or "transcript").strip().lower()
    source_ref = str(value.get("source_ref") or "").strip()
    if not course_id or not sub_id or not course_id.isdigit() or not sub_id.isdigit():
        raise ValueError("reference course_id and sub_id must be numeric")
    try:
        start_ms = max(0, int(value.get("start_ms", 0)))
        end_ms = max(start_ms, int(value.get("end_ms", start_ms)))
    except (TypeError, ValueError) as exc:
        raise ValueError("reference time range is invalid") from exc
    digest = str(value.get("source_hash") or "").strip().lower()
    if not _HEX_RE.fullmatch(digest):
        raise ValueError("reference source_hash is invalid")
    if source not in {"transcript", "ppt", "document", "summary", "quiz", "bookmark"}:
        raise ValueError("reference source is unsupported")
    if len(source_ref) > 512:
        raise ValueError("reference source_ref is too long")
    return ContentReference(course_id, sub_id, start_ms, end_ms, digest, source, source_ref)


def reference_from_text(
    *, course_id: str, sub_id: str, start_ms: int, end_ms: int, text: str,
    source: str = "transcript", source_ref: str = "",
) -> ContentReference:
    return normalize_reference({
        "course_id": course_id,
        "sub_id": sub_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "source_hash": source_hash(text),
        "source": source,
        "source_ref": source_ref,
    })


__all__ = ["ContentReference", "normalize_reference", "reference_from_text", "source_hash"]
