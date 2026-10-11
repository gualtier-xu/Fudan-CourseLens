"""CourseLens course-knowledge contract v1 (``courselens.course-knowledge.v1``).

Deterministic, standard-library-only JSON contract for the course-review
batch.  It wraps — and never replaces — the accepted ``evidence.v1`` contract:
transcript and slide references reuse the existing ``seg:``/``slevt:`` IDs,
while document pages, assessment items and bookmarks get their own citation
namespace so a citation stays addressable even when the underlying artifact is
regenerable (OCR text, exam split, user bookmark).

Two rules drive the whole module:

- **Traceable or absent.**  Every published knowledge claim (key point, topic,
  assessment reference) carries ``citation_ids`` that must resolve to a
  declared :class:`EvidenceRef` in the same document, and that ref must
  resolve to a declared source with a matching kind.  A dangling citation is
  rejected rather than silently dropped.
- **Reference views never invent answers.**  An ``AssessmentItem`` is a
  pointer into the imported paper (question number, label, content hash); it
  must not carry an answer key, because the local pipeline has no adjudicated
  answer source and a fabricated "official answer" is worse than no answer.

Validation is fail-closed: unknown fields, unknown enum values, wrong hash
shapes, cross-course references and answer-bearing assessment items all raise
:class:`CourseKnowledgeContractError` with a stable ``code``.

The frozen consumer fixture is ``tests/fixtures/course_review_v1.json``; it
holds three course-knowledge documents (complete / partial / stale), the three
API view models, and six rejection samples with their expected codes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import unicodedata

from shared.evidence_contract import SECRET_VALUE_PATTERNS

CONTRACT_ID = "courselens.course-knowledge.v1"
FIXTURE_SCHEMA = "courselens.course-knowledge.fixture.v1"

NAMESPACE_CITATION = "ckc"
NAMESPACE_TOPIC = "ckt"
NAMESPACE_ASSESSMENT = "cka"
NAMESPACE_SOURCE = "cks"

_ID_RE = re.compile(r"^(ckc|ckt|cka|cks):[0-9a-f]{12}$")
_SHA_RE = re.compile(r"^[0-9a-f]{32,64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{12,64}$")
_SEGMENT_ID_RE = re.compile(r"^seg:[0-9a-f]{12}$")
_SLIDE_ID_RE = re.compile(r"^(slevt|slent):[0-9a-f]{12}$")

EVIDENCE_KINDS = ("transcript", "slide", "document_page", "assessment_item", "bookmark")
KNOWLEDGE_STATUSES = ("ready", "partial", "stale", "error")
TOPIC_STATUSES = ("ready", "partial")
SOURCE_SCOPES = ("lecture", "course")

# Why a snapshot is not simply "ready".  A closed set keeps the UI honest: it
# can say "这一讲的 PPT 换了，需要重建" without inventing prose per caller.
STALE_REASONS = (
    "never_built",
    "input_changed",
    "transcript_missing",
    "summary_missing",
    "document_changed",
    "assessment_changed",
    "snapshot_invalid",
)

# citation kind -> locator field set.  Anchors stay integers so a viewer can
# jump back to the exact subtitle window / page / question.
LOCATOR_FIELDS = {
    "transcript": ("start_ms", "end_ms"),
    "bookmark": ("start_ms", "end_ms"),
    "slide": ("page",),
    "document_page": ("page",),
    "assessment_item": ("question_no",),
}
LOCATOR_INT_FIELDS = {
    "transcript": ("start_ms", "end_ms"),
    "bookmark": ("start_ms", "end_ms"),
    "slide": ("page",),
    "document_page": ("page",),
    "assessment_item": ("question_no",),
}

COVERAGE_KEYS = (
    "transcript_segments",
    "slide_pages",
    "document_pages",
    "course_document_pages",
    "assessment_items",
    "bookmarks",
)
LECTURE_COVERAGE_KEYS = COVERAGE_KEYS + ("lecture_ir", "summary")
LECTURE_COVERAGE_BOOL_KEYS = ("lecture_ir", "summary")
COURSE_COVERAGE_KEYS = (
    "lectures_total",
    "lectures_ready",
    "lectures_partial",
    "lectures_stale",
) + COVERAGE_KEYS

# A reference view must never pretend to know the answer.  These keys are
# rejected outright on an AssessmentItem (and anywhere inside one).
ANSWER_KEY_FIELDS = (
    "answer",
    "answers",
    "official_answer",
    "answer_key",
    "correct_option",
    "correct_answer",
    "reference_answer",
    "model_answer",
    "solution",
    "标准答案",
    "参考答案",
    "答案",
)

_FIELD_ORDER = (
    "citation_id", "kind", "source_id", "revision_id", "content_hash", "locator", "label",
)


class CourseKnowledgeContractError(ValueError):
    """Closed validation failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, path: str = "") -> None:
        super().__init__(f"{code}: {message}" + (f" (at {path})" if path else ""))
        self.code = str(code)
        self.message = str(message)
        self.path = str(path)


def _fail(code: str, message: str, path: str) -> None:
    raise CourseKnowledgeContractError(code, message, path)


def _check(condition: bool, code: str, message: str, path: str) -> None:
    if not condition:
        raise CourseKnowledgeContractError(code, message, path)


def canonical_json(value) -> str:
    """Canonical JSON text: sorted keys, compact separators, finite numbers."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def compute_id(namespace: str, identity: dict) -> str:
    """Deterministic, position-independent ID over canonical identity fields."""
    payload = canonical_json(identity).encode("utf-8")
    return f"{namespace}:{hashlib.sha256(payload).hexdigest()[:12]}"


def citation_id_for(ref: dict) -> str:
    """Stable citation ID over the fields that make a citation resolvable."""
    return compute_id(
        NAMESPACE_CITATION,
        {
            "kind": ref.get("kind"),
            "source_id": ref.get("source_id"),
            "revision_id": ref.get("revision_id"),
            "content_hash": ref.get("content_hash"),
            "locator": ref.get("locator"),
        },
    )


def topic_id_for(title: str, aliases) -> str:
    """Stable topic ID over the normalized title plus its sorted aliases."""
    return compute_id(
        NAMESPACE_TOPIC,
        {
            "title_norm": normalize_topic_title(title),
            "aliases": sorted(normalize_topic_title(value) for value in (aliases or []) if str(value or "").strip()),
        },
    )


def assessment_item_id_for(item: dict) -> str:
    return compute_id(
        NAMESPACE_ASSESSMENT,
        {
            "course_id": item.get("course_id"),
            "document_id": item.get("document_id"),
            "question_no": item.get("question_no"),
            "content_hash": item.get("content_hash"),
        },
    )


def source_id_for(source: dict) -> str:
    return compute_id(
        NAMESPACE_SOURCE,
        {
            "kind": source.get("kind"),
            "scope": source.get("scope"),
            "sub_id": source.get("sub_id"),
            "external_id": source.get("external_id"),
            "revision_id": source.get("revision_id"),
        },
    )


def normalize_topic_title(value: object) -> str:
    """Normalized topic key used for alias merging (no embeddings, no guessing).

    NFKC + casefold + whitespace collapse + trailing chapter punctuation
    removal.  Two topics merge only when this key matches exactly or one lists
    the other as an alias; anything less certain stays as two topics.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[\s\u3000]+", "", text)
    text = re.sub(r"[、。，,.;；:：!！?？\-—_·•()（）\[\]【】《》\"'“”‘’]+$", "", text)
    text = re.sub(r"^[\s第]+(?=[0-9一二三四五六七八九十]+[章节講讲])", "", text)
    return text.strip()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _scan_secrets(value, path: str) -> None:
    """Reject credential-shaped strings and non-finite numbers anywhere."""
    if isinstance(value, str):
        if _ID_RE.match(value) or _SHA_RE.match(value):
            return
        for pattern, label in SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                _fail("secret_like", f"string looks like {label}", path)
    elif isinstance(value, bool):
        return
    elif isinstance(value, float):
        if not math.isfinite(value):
            _fail("value_invalid", "non-finite number is not valid JSON data", path)
    elif isinstance(value, dict):
        for key, item in value.items():
            _scan_secrets(item, path + "." + str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_secrets(item, f"{path}[{index}]")


def _reject_unknown_keys(obj: dict, allowed, path: str) -> None:
    for key in obj:
        _check(str(key) in allowed, "field_unknown", f"unknown field {key!r}", path + "." + str(key))


def _require_text(value, path: str, *, allow_empty: bool = False) -> str:
    _check(isinstance(value, str), "type_invalid", "expected a string", path)
    if not allow_empty:
        _check(value.strip() != "", "value_invalid", "expected a non-empty string", path)
    return value


def _require_anchor(value, path: str) -> int:
    _check(_is_int(value), "type_invalid", "expected an integer millisecond anchor", path)
    _check(value >= 0, "anchor_negative", "millisecond anchor must be >= 0", path)
    return value


def _require_hash(value, path: str, *, pattern, message: str) -> str:
    """A missing digest is `field_required`; a present but wrong one is malformed."""
    _check(value is not None, "field_required", "a digest is required here", path)
    _check(
        isinstance(value, str) and bool(pattern.match(value)),
        "hash_malformed",
        message,
        path,
    )
    return value


def _sorted_key(obj, fields):
    """Total, type-tolerant sort key so array order is canonical."""
    parts = []
    for field in fields:
        value = obj.get(field) if isinstance(obj, dict) else None
        if _is_int(value):
            parts.append((0, value, ""))
        elif isinstance(value, str):
            parts.append((1, 0, value))
        elif value is None:
            parts.append((2, 0, ""))
        else:
            parts.append((3, 0, canonical_json(value) if isinstance(value, dict) else repr(value)))
    return tuple(parts)


# ---------------------------------------------------------------------------
# Evidence references


def _validate_locator(ref: dict, path: str) -> dict:
    kind = ref.get("kind")
    locator = ref.get("locator")
    _check(isinstance(locator, dict), "type_invalid", "locator must be an object", path + ".locator")
    allowed = LOCATOR_FIELDS[kind]
    _reject_unknown_keys(locator, allowed, path + ".locator")
    for field in allowed:
        _check(field in locator, "field_required", f"locator.{field} is required", path + ".locator")
    values = {}
    for field in allowed:
        if field in LOCATOR_INT_FIELDS[kind]:
            values[field] = _require_anchor(locator.get(field), f"{path}.locator.{field}")
        else:  # pragma: no cover - every current field is an integer anchor
            values[field] = _require_text(locator.get(field), f"{path}.locator.{field}", allow_empty=True)
    if "start_ms" in values:
        _check(
            values["start_ms"] <= values["end_ms"],
            "anchor_descending",
            "start_ms must be <= end_ms",
            path + ".locator",
        )
    return values


def validate_evidence_ref(ref, course_id: str, path: str = "ref") -> dict:
    """Validate one citation; returns its canonical form."""
    _check(isinstance(ref, dict), "type_invalid", "evidence reference must be an object", path)
    allowed = set(_FIELD_ORDER) | {"course_id"}
    _reject_unknown_keys(ref, allowed, path)
    kind = ref.get("kind")
    _check(
        kind in EVIDENCE_KINDS,
        "kind_unsupported",
        f"kind must be one of {EVIDENCE_KINDS}",
        path + ".kind",
    )
    ref_course = ref.get("course_id")
    if ref_course is not None:
        _check(
            str(ref_course) == str(course_id),
            "cross_course_reference",
            "evidence reference belongs to a different course",
            path + ".course_id",
        )
    source_id = _require_text(ref.get("source_id"), path + ".source_id")
    if kind == "transcript":
        _check(
            bool(_SEGMENT_ID_RE.match(source_id)),
            "reference_missing",
            "transcript citations must reuse an evidence.v1 segment id (seg:<12 hex>)",
            path + ".source_id",
        )
    elif kind == "slide":
        _check(
            bool(_SLIDE_ID_RE.match(source_id)),
            "reference_missing",
            "slide citations must reuse an evidence.v1 slide id (slevt:<12 hex>|slent:<12 hex>)",
            path + ".source_id",
        )
    else:
        _check(len(source_id) <= 128, "value_invalid", "source_id is too long", path + ".source_id")
    revision_id = _require_hash(
        ref.get("revision_id"), path + ".revision_id",
        pattern=_REVISION_RE, message="revision_id must be 12-64 lowercase hex",
    )
    content_hash = _require_hash(
        ref.get("content_hash"), path + ".content_hash",
        pattern=_SHA_RE, message="content_hash must be 32-64 lowercase hex",
    )
    label = ref.get("label")
    _check(
        label is None or isinstance(label, str),
        "type_invalid",
        "label must be a string or null",
        path + ".label",
    )
    locator = _validate_locator(ref, path)
    canonical = {
        "citation_id": citation_id_for({**ref, "locator": locator}),
        "kind": kind,
        "source_id": source_id,
        "revision_id": revision_id,
        "content_hash": content_hash,
        "locator": locator,
        "label": label,
    }
    provided = ref.get("citation_id")
    if provided is not None:
        _require_text(provided, path + ".citation_id")
        _check(
            bool(_ID_RE.match(provided)) and provided.split(":")[0] == NAMESPACE_CITATION,
            "id_malformed",
            f"citation_id must match {NAMESPACE_CITATION}:<12 lowercase hex>",
            path + ".citation_id",
        )
        if provided != canonical["citation_id"]:
            _fail(
                "id_mismatch",
                f"citation_id does not match its citation fields (expected {canonical['citation_id']})",
                path + ".citation_id",
            )
    return canonical


def _validate_source(source, course_id: str, path: str) -> dict:
    _check(isinstance(source, dict), "type_invalid", "source must be an object", path)
    allowed = {"source_id", "kind", "scope", "sub_id", "external_id", "revision_id", "label", "course_id"}
    _reject_unknown_keys(source, allowed, path)
    source_course = source.get("course_id")
    if source_course is not None:
        _check(
            str(source_course) == str(course_id),
            "cross_course_reference",
            "source belongs to a different course",
            path + ".course_id",
        )
    kind = source.get("kind")
    _check(kind in EVIDENCE_KINDS, "kind_unsupported", f"kind must be one of {EVIDENCE_KINDS}", path + ".kind")
    scope = source.get("scope")
    _check(
        scope in SOURCE_SCOPES,
        "status_unsupported",
        f"scope must be one of {SOURCE_SCOPES}",
        path + ".scope",
    )
    sub_id = source.get("sub_id")
    _check(isinstance(sub_id, str), "type_invalid", "sub_id must be a string", path + ".sub_id")
    if scope == "course":
        _check(sub_id == "", "value_invalid", "course-scope sources carry an empty sub_id", path + ".sub_id")
    else:
        _check(sub_id.strip() != "", "field_required", "lecture-scope sources need a sub_id", path + ".sub_id")
    external_id = _require_text(source.get("external_id"), path + ".external_id")
    if kind == "transcript":
        _check(bool(_SEGMENT_ID_RE.match(external_id)), "reference_missing", "transcript sources reuse seg:<12 hex> ids", path + ".external_id")
    elif kind == "slide":
        _check(bool(_SLIDE_ID_RE.match(external_id)), "reference_missing", "slide sources reuse slide ids", path + ".external_id")
    revision_id = _require_hash(
        source.get("revision_id"), path + ".revision_id",
        pattern=_REVISION_RE, message="revision_id must be 12-64 lowercase hex",
    )
    label = source.get("label")
    _check(label is None or isinstance(label, str), "type_invalid", "label must be a string or null", path + ".label")
    canonical = {
        "source_id": source_id_for({**source, "external_id": external_id}),
        "kind": kind,
        "scope": scope,
        "sub_id": sub_id,
        "external_id": external_id,
        "revision_id": revision_id,
        "label": label,
    }
    provided = source.get("source_id")
    if provided is not None:
        _require_text(provided, path + ".source_id")
        _check(
            bool(_ID_RE.match(provided)) and provided.split(":")[0] == NAMESPACE_SOURCE,
            "id_malformed",
            f"source_id must match {NAMESPACE_SOURCE}:<12 lowercase hex>",
            path + ".source_id",
        )
        if provided != canonical["source_id"]:
            _fail(
                "id_mismatch",
                f"source_id does not match its source fields (expected {canonical['source_id']})",
                path + ".source_id",
            )
    # Keep the real external identity visible to consumers without leaking a
    # second ID namespace into the reference view.
    canonical["course_id"] = str(course_id)
    return canonical


# ---------------------------------------------------------------------------
# Knowledge objects


def _validate_stale_reasons(value, status: str, path: str) -> list[str]:
    field = path + ".stale_reasons"
    _check(isinstance(value, list), "type_invalid", "stale_reasons must be an array", field)
    seen: list[str] = []
    for index, reason in enumerate(value):
        _check(
            reason in STALE_REASONS,
            "status_unsupported",
            f"stale reason must be one of {STALE_REASONS}",
            f"{field}[{index}]",
        )
        if reason not in seen:
            seen.append(reason)
    if status == "ready":
        _check(not seen, "state_conflict", "a ready snapshot must not carry stale reasons", field)
    else:
        _check(
            bool(seen),
            "field_required",
            "a non-ready snapshot must say why (stale_reasons)",
            field,
        )
    return sorted(seen)


def _validate_key_points(points, path: str, citations: set[str]) -> list[dict]:
    _check(isinstance(points, list), "type_invalid", "key_points must be an array", path)
    values = []
    for index, point in enumerate(points):
        ppath = f"{path}[{index}]"
        _check(isinstance(point, dict), "type_invalid", "key point must be an object", ppath)
        # RR-ANCHORFE-1：anchor_ms 是加性可选字段（takeaway 时间戳锚，非负整数
        # 毫秒）。旧快照没有该字段照常通过；出现时必须合法，fail-closed 不放行
        # 畸形锚——锚错比锚缺更伤「AI 说的话可信吗」的信任闭环。
        _reject_unknown_keys(point, {"text", "citation_ids", "anchor_ms"}, ppath)
        text = _require_text(point.get("text"), ppath + ".text")
        _check(len(text) <= 600, "value_invalid", "key point text is too long", ppath + ".text")
        validated = {
            "text": text,
            "citation_ids": _citation_list(point.get("citation_ids"), ppath, citations),
        }
        anchor_ms = point.get("anchor_ms")
        if anchor_ms is not None:
            _check(
                isinstance(anchor_ms, int) and not isinstance(anchor_ms, bool) and anchor_ms >= 0,
                "type_invalid",
                "key point anchor_ms must be a non-negative integer millisecond",
                ppath + ".anchor_ms",
            )
            validated["anchor_ms"] = int(anchor_ms)
        values.append(validated)
    return values


def _citation_list(value, path: str, citations: set[str]) -> list[str]:
    field = path + ".citation_ids"
    _check(
        isinstance(value, list) and len(value) >= 1,
        "field_required",
        "at least one citation_id is required (uncited claims are not publishable)",
        field,
    )
    seen: list[str] = []
    for index, entry in enumerate(value):
        _require_text(entry, f"{field}[{index}]")
        _check(bool(_ID_RE.match(entry)), "id_malformed", "citation_id must match ckc:<12 lowercase hex>", f"{field}[{index}]")
        _check(
            entry in citations,
            "reference_missing",
            "citation_id does not resolve to an evidence reference in this document",
            f"{field}[{index}]",
        )
        if entry not in seen:
            seen.append(entry)
    return sorted(seen)


def _validate_topics(topics, path: str, citations: set[str], lecture_ids: set[str]) -> list[dict]:
    _check(isinstance(topics, list), "type_invalid", "topics must be an array", path)
    values = []
    for index, topic in enumerate(topics):
        tpath = f"{path}[{index}]"
        _check(isinstance(topic, dict), "type_invalid", "topic must be an object", tpath)
        _reject_unknown_keys(topic, {"topic_id", "title", "aliases", "lecture_ids", "citation_ids", "status"}, tpath)
        title = _require_text(topic.get("title"), tpath + ".title")
        status = topic.get("status")
        _check(status in TOPIC_STATUSES, "status_unsupported", f"status must be one of {TOPIC_STATUSES}", tpath + ".status")
        aliases = topic.get("aliases")
        _check(isinstance(aliases, list), "type_invalid", "aliases must be an array", tpath + ".aliases")
        clean_aliases = []
        for alias_index, alias in enumerate(aliases):
            value = _require_text(alias, f"{tpath}.aliases[{alias_index}]")
            _check(len(value) <= 200, "value_invalid", "alias is too long", f"{tpath}.aliases[{alias_index}]")
            if value not in clean_aliases:
                clean_aliases.append(value)
        own_lectures = topic.get("lecture_ids")
        _check(
            isinstance(own_lectures, list) and len(own_lectures) >= 1,
            "field_required",
            "topic.lecture_ids must name at least one lecture",
            tpath + ".lecture_ids",
        )
        for lecture_index, lecture_id in enumerate(own_lectures):
            _require_text(lecture_id, f"{tpath}.lecture_ids[{lecture_index}]")
            _check(
                str(lecture_id) in lecture_ids,
                "reference_missing",
                "topic references a lecture that is not declared in this document",
                f"{tpath}.lecture_ids[{lecture_index}]",
            )
        canonical = {
            "topic_id": topic_id_for(title, clean_aliases),
            "title": title,
            "aliases": sorted(clean_aliases),
            "lecture_ids": sorted({str(value) for value in own_lectures}),
            "citation_ids": _citation_list(topic.get("citation_ids"), tpath, citations),
            "status": status,
        }
        provided = topic.get("topic_id")
        if provided is not None:
            _require_text(provided, tpath + ".topic_id")
            _check(
                bool(_ID_RE.match(provided)) and provided.split(":")[0] == NAMESPACE_TOPIC,
                "id_malformed",
                f"topic_id must match {NAMESPACE_TOPIC}:<12 lowercase hex>",
                tpath + ".topic_id",
            )
            if provided != canonical["topic_id"]:
                _fail(
                    "id_mismatch",
                    f"topic_id does not match its topic fields (expected {canonical['topic_id']})",
                    tpath + ".topic_id",
                )
        values.append(canonical)
    return sorted(values, key=lambda topic: topic["topic_id"])


def _validate_coverage(coverage, path: str, *, keys, bool_keys=()) -> dict:
    _check(isinstance(coverage, dict), "type_invalid", "coverage must be an object", path)
    _reject_unknown_keys(coverage, set(keys), path)
    for key in keys:
        _check(key in coverage, "field_required", f"coverage.{key} is required", path + "." + key)
        value = coverage[key]
        if key in bool_keys:
            _check(isinstance(value, bool), "type_invalid", f"coverage.{key} must be a boolean", path + "." + key)
        else:
            _check(
                _is_int(value) and value >= 0,
                "type_invalid",
                f"coverage.{key} must be a non-negative integer",
                path + "." + key,
            )
    return {key: coverage[key] for key in keys}


def _validate_assessment_item(item, course_id: str, path: str, citations: set[str], lecture_ids: set[str]) -> dict:
    _check(isinstance(item, dict), "type_invalid", "assessment item must be an object", path)
    for key in item:
        _check(
            str(key).casefold() not in {value.casefold() for value in ANSWER_KEY_FIELDS},
            "assessment_answer_unsupported",
            f"assessment items are reference views and must not carry an answer field ({key!r})",
            path + "." + str(key),
        )
    allowed = {
        "item_id", "course_id", "sub_id", "document_id", "question_no",
        "label", "content_hash", "citation_ids",
    }
    _reject_unknown_keys(item, allowed, path)
    item_course = item.get("course_id")
    _check(isinstance(item_course, str), "type_invalid", "course_id is required", path + ".course_id")
    _check(
        str(item_course) == str(course_id),
        "cross_course_reference",
        "assessment item belongs to a different course",
        path + ".course_id",
    )
    sub_id = item.get("sub_id")
    _check(isinstance(sub_id, str), "type_invalid", "sub_id must be a string", path + ".sub_id")
    if sub_id:
        _check(
            sub_id in lecture_ids,
            "reference_missing",
            "assessment item references a lecture that is not declared in this document",
            path + ".sub_id",
        )
    document_id = _require_text(item.get("document_id"), path + ".document_id")
    _check(len(document_id) <= 128, "value_invalid", "document_id is too long", path + ".document_id")
    question_no = item.get("question_no")
    _check(
        _is_int(question_no) and question_no >= 1,
        "value_invalid",
        "question_no must be an integer >= 1",
        path + ".question_no",
    )
    content_hash = _require_hash(
        item.get("content_hash"), path + ".content_hash",
        pattern=_SHA_RE, message="content_hash must be 32-64 lowercase hex",
    )
    label = item.get("label")
    _check(label is None or isinstance(label, str), "type_invalid", "label must be a string or null", path + ".label")
    canonical = {
        "item_id": assessment_item_id_for({"course_id": course_id, "document_id": document_id, "question_no": question_no, "content_hash": content_hash}),
        "course_id": str(course_id),
        "sub_id": sub_id,
        "document_id": document_id,
        "question_no": question_no,
        "label": label,
        "content_hash": content_hash,
        "citation_ids": _citation_list(item.get("citation_ids"), path, citations),
    }
    provided = item.get("item_id")
    if provided is not None:
        _require_text(provided, path + ".item_id")
        _check(
            bool(_ID_RE.match(provided)) and provided.split(":")[0] == NAMESPACE_ASSESSMENT,
            "id_malformed",
            f"item_id must match {NAMESPACE_ASSESSMENT}:<12 lowercase hex>",
            path + ".item_id",
        )
        if provided != canonical["item_id"]:
            _fail(
                "id_mismatch",
                f"item_id does not match its item fields (expected {canonical['item_id']})",
                path + ".item_id",
            )
    return canonical


def _validate_lecture(lecture, course_id: str, path: str) -> dict:
    _check(isinstance(lecture, dict), "type_invalid", "lecture must be an object", path)
    allowed = {
        "sub_id", "course_id", "input_hash", "status", "stale_reasons", "key_points",
        "topics", "source_coverage", "evidence_refs", "updated_at",
    }
    _reject_unknown_keys(lecture, allowed, path)
    sub_id = _require_text(lecture.get("sub_id"), path + ".sub_id")
    lecture_course = lecture.get("course_id")
    _check(isinstance(lecture_course, str), "type_invalid", "course_id is required", path + ".course_id")
    _check(
        str(lecture_course) == str(course_id),
        "cross_course_reference",
        "lecture knowledge belongs to a different course",
        path + ".course_id",
    )
    status = lecture.get("status")
    _check(
        status in KNOWLEDGE_STATUSES,
        "status_unsupported",
        f"status must be one of {KNOWLEDGE_STATUSES}",
        path + ".status",
    )
    input_hash = _require_hash(
        lecture.get("input_hash"), path + ".input_hash",
        pattern=_REVISION_RE, message="input_hash must be 12-64 lowercase hex",
    )
    updated_at = lecture.get("updated_at")
    _check(
        isinstance(updated_at, (int, float)) and not isinstance(updated_at, bool) and math.isfinite(updated_at) and float(updated_at) >= 0,
        "type_invalid",
        "updated_at must be a finite non-negative number",
        path + ".updated_at",
    )
    refs = lecture.get("evidence_refs")
    _check(isinstance(refs, list), "type_invalid", "evidence_refs must be an array", path + ".evidence_refs")
    validated_refs = [
        validate_evidence_ref(ref, course_id, f"{path}.evidence_refs[{index}]")
        for index, ref in enumerate(refs)
    ]
    citations = {ref["citation_id"] for ref in validated_refs}
    _check(
        len(citations) == len(validated_refs),
        "duplicate_id",
        "duplicate citation within a lecture",
        path + ".evidence_refs",
    )
    canonical = {
        "sub_id": sub_id,
        "course_id": str(course_id),
        "input_hash": input_hash,
        "status": status,
        "stale_reasons": _validate_stale_reasons(lecture.get("stale_reasons"), status, path),
        "key_points": _validate_key_points(lecture.get("key_points"), path + ".key_points", citations),
        "topics": _validate_topics(lecture.get("topics"), path + ".topics", citations, {sub_id}),
        "source_coverage": _validate_coverage(
            lecture.get("source_coverage"),
            path + ".source_coverage",
            keys=LECTURE_COVERAGE_KEYS,
            bool_keys=LECTURE_COVERAGE_BOOL_KEYS,
        ),
        "evidence_refs": sorted(validated_refs, key=lambda ref: ref["citation_id"]),
        "updated_at": float(updated_at),
    }
    return canonical


def _validate_course(doc) -> dict:
    allowed = {
        "contract", "course_id", "input_hash", "status", "stale_reasons", "coverage",
        "topics", "lectures", "assessment_items", "sources", "evidence_refs", "updated_at",
    }
    _reject_unknown_keys(doc, allowed, "")
    _check(doc.get("contract") == CONTRACT_ID, "contract_unsupported", f"contract must be {CONTRACT_ID!r}", "contract")
    course_id = _require_text(doc.get("course_id"), "course_id")
    status = doc.get("status")
    _check(status in KNOWLEDGE_STATUSES, "status_unsupported", f"status must be one of {KNOWLEDGE_STATUSES}", "status")
    input_hash = _require_hash(
        doc.get("input_hash"), "input_hash",
        pattern=_REVISION_RE, message="input_hash must be 12-64 lowercase hex",
    )
    updated_at = doc.get("updated_at")
    _check(
        isinstance(updated_at, (int, float)) and not isinstance(updated_at, bool) and math.isfinite(updated_at) and float(updated_at) >= 0,
        "type_invalid",
        "updated_at must be a finite non-negative number",
        "updated_at",
    )
    sources = doc.get("sources")
    _check(isinstance(sources, list), "type_invalid", "sources must be an array", "sources")
    validated_sources = [
        _validate_source(source, course_id, f"sources[{index}]") for index, source in enumerate(sources)
    ]
    by_external = {(source["kind"], source["external_id"]): source for source in validated_sources}
    _check(
        len(by_external) == len(validated_sources),
        "duplicate_id",
        "duplicate source for the same kind/external id",
        "sources",
    )
    lectures = doc.get("lectures")
    _check(isinstance(lectures, list) and len(lectures) >= 1, "field_required", "at least one lecture is required", "lectures")
    validated_lectures = [
        _validate_lecture(lecture, course_id, f"lectures[{index}]") for index, lecture in enumerate(lectures)
    ]
    lecture_ids = [lecture["sub_id"] for lecture in validated_lectures]
    _check(len(set(lecture_ids)) == len(lecture_ids), "duplicate_id", "duplicate lecture sub_id", "lectures")
    lecture_id_set = set(lecture_ids)

    course_topics = doc.get("topics")
    all_refs: list[dict] = []
    for lecture in validated_lectures:
        all_refs.extend(lecture["evidence_refs"])
    course_refs = [
        validate_evidence_ref(ref, course_id, f"evidence_refs[{index}]")
        for index, ref in enumerate(_course_refs(doc))
    ]
    all_refs.extend(course_refs)
    citations = {ref["citation_id"] for ref in all_refs}
    for ref in all_refs:
        key = (ref["kind"], ref["source_id"])
        _check(
            key in by_external,
            "reference_missing",
            "citation source_id does not resolve to a declared source of the same kind",
            "sources",
        )
    coverage = _validate_coverage(doc.get("coverage"), "coverage", keys=COURSE_COVERAGE_KEYS)
    _check(
        coverage["lectures_total"] == len(validated_lectures),
        "coverage_mismatch",
        "coverage.lectures_total must equal the declared lecture count",
        "coverage.lectures_total",
    )
    for key, expected in (
        ("lectures_ready", sum(1 for lecture in validated_lectures if lecture["status"] == "ready")),
        ("lectures_partial", sum(1 for lecture in validated_lectures if lecture["status"] == "partial")),
        ("lectures_stale", sum(1 for lecture in validated_lectures if lecture["status"] == "stale")),
    ):
        _check(
            coverage[key] == expected,
            "coverage_mismatch",
            f"coverage.{key} must equal {expected}",
            "coverage." + key,
        )
    items = doc.get("assessment_items")
    _check(isinstance(items, list), "type_invalid", "assessment_items must be an array", "assessment_items")
    validated_items = [
        _validate_assessment_item(item, course_id, f"assessment_items[{index}]", citations, lecture_id_set)
        for index, item in enumerate(items)
    ]
    _check(
        coverage["assessment_items"] == len(validated_items),
        "coverage_mismatch",
        "coverage.assessment_items must equal the declared item count",
        "coverage.assessment_items",
    )
    # 课程级文档页是全课共享资源：同一页会被多讲各自引用一次，这里数的是
    # 去重后的「全课可用页数」= (文档, 页) 去重。早先版本拿它对照「课程级
    # source 条数」，在两讲以上同时引用同一份教材时必然误判——由
    # tests/test_course_knowledge_store.py::CourseDocumentSearchTests 一族的
    # 多讲用例钉住。
    _check(
        coverage["course_document_pages"] == len({
            (ref["source_id"], (ref.get("locator") or {}).get("page"))
            for ref in all_refs
            if ref["kind"] == "document_page"
            and (by_external.get((ref["kind"], ref["source_id"])) or {}).get("scope") == "course"
        }),
        "coverage_mismatch",
        "coverage.course_document_pages must equal the distinct course-scope document pages",
        "coverage.course_document_pages",
    )
    return {
        "contract": CONTRACT_ID,
        "course_id": course_id,
        "input_hash": input_hash,
        "status": status,
        "stale_reasons": _validate_stale_reasons(doc.get("stale_reasons"), status, ""),
        "coverage": coverage,
        "topics": _validate_topics(
            course_topics, "topics", citations, lecture_id_set
        ),
        "lectures": sorted(validated_lectures, key=lambda lecture: lecture["sub_id"]),
        "assessment_items": sorted(
            validated_items, key=lambda item: _sorted_key(item, ("sub_id", "question_no", "document_id"))
        ),
        "sources": sorted(validated_sources, key=lambda source: source["source_id"]),
        "evidence_refs": sorted(course_refs, key=lambda ref: ref["citation_id"]),
        "updated_at": float(updated_at),
    }


def _course_refs(doc) -> list:
    """Course-level evidence references, if the document declares any."""
    refs = doc.get("evidence_refs")
    return refs if isinstance(refs, list) else []


def validate_course_knowledge(document) -> dict:
    """Validate a course-knowledge document and return its canonical form."""
    _check(isinstance(document, dict), "not_json_object", "document must be a JSON object", "")
    doc = copy.deepcopy(document)
    _scan_secrets(doc, "$")
    return _validate_course(doc)


def documents_match(left, right) -> bool:
    """Canonical-equality of two validated documents."""
    return canonical_json(validate_course_knowledge(left)) == canonical_json(
        validate_course_knowledge(right)
    )


def to_json(document) -> str:
    return canonical_json(validate_course_knowledge(document))


def from_json(text) -> dict:
    def _reject_constant(name):
        _fail("value_invalid", f"{name} is not valid JSON data", "")

    def _no_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail("duplicate_key", f"duplicate JSON key {key!r}", "")
            result[key] = value
        return result

    try:
        document = json.loads(text, parse_constant=_reject_constant, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as error:
        _fail("value_invalid", f"invalid JSON: {error.msg}", "")
    return validate_course_knowledge(document)


# ---------------------------------------------------------------------------
# API view models.  Exactly three shapes are supported: one course overview,
# one lecture detail, one assessment workspace.  More endpoints would be more
# surface for a consumer to drift against, and none of them is needed.


def course_overview_view(document) -> dict:
    doc = validate_course_knowledge(document)
    return {
        "view": "course_overview",
        "contract": CONTRACT_ID,
        "course_id": doc["course_id"],
        "status": doc["status"],
        "stale_reasons": doc["stale_reasons"],
        "coverage": doc["coverage"],
        "topics": [
            {
                "topic_id": topic["topic_id"],
                "title": topic["title"],
                "aliases": topic["aliases"],
                "lecture_ids": topic["lecture_ids"],
                "status": topic["status"],
                "citation_count": len(topic["citation_ids"]),
            }
            for topic in doc["topics"]
        ],
        "lectures": [
            {
                "sub_id": lecture["sub_id"],
                "status": lecture["status"],
                "stale_reasons": lecture["stale_reasons"],
                "input_hash": lecture["input_hash"],
                "key_point_count": len(lecture["key_points"]),
                "topic_ids": sorted(topic["topic_id"] for topic in lecture["topics"]),
                "source_coverage": lecture["source_coverage"],
                "updated_at": lecture["updated_at"],
            }
            for lecture in doc["lectures"]
        ],
        "assessment": {
            "total": len(doc["assessment_items"]),
            "lectures_with_items": len({item["sub_id"] for item in doc["assessment_items"] if item["sub_id"]}),
        },
        "sources": [
            {
                "source_id": source["source_id"],
                "kind": source["kind"],
                "scope": source["scope"],
                "sub_id": source["sub_id"],
                "external_id": source["external_id"],
                "revision_id": source["revision_id"],
                "label": source["label"],
            }
            for source in doc["sources"]
        ],
        "updated_at": doc["updated_at"],
    }


def lecture_detail_view(document, sub_id: str) -> dict:
    doc = validate_course_knowledge(document)
    wanted = str(sub_id)
    lecture = next((item for item in doc["lectures"] if item["sub_id"] == wanted), None)
    if lecture is None:
        raise CourseKnowledgeContractError(
            "lecture_not_found", "no such lecture in this course knowledge snapshot", "sub_id"
        )
    return {
        "view": "lecture_detail",
        "contract": CONTRACT_ID,
        "course_id": doc["course_id"],
        "sub_id": lecture["sub_id"],
        "status": lecture["status"],
        "stale_reasons": lecture["stale_reasons"],
        "input_hash": lecture["input_hash"],
        "key_points": [
            {
                "text": point["text"],
                "citation_ids": point["citation_ids"],
                **({"anchor_ms": point["anchor_ms"]} if "anchor_ms" in point else {}),
            }
            for point in lecture["key_points"]
        ],
        "topics": [
            {
                "topic_id": topic["topic_id"],
                "title": topic["title"],
                "aliases": topic["aliases"],
                "status": topic["status"],
                "citation_ids": topic["citation_ids"],
            }
            for topic in lecture["topics"]
        ],
        "source_coverage": lecture["source_coverage"],
        "evidence_refs": [
            {
                "citation_id": ref["citation_id"],
                "kind": ref["kind"],
                "source_id": ref["source_id"],
                "revision_id": ref["revision_id"],
                "content_hash": ref["content_hash"],
                "locator": ref["locator"],
                "label": ref["label"],
            }
            for ref in lecture["evidence_refs"]
        ],
        "updated_at": lecture["updated_at"],
    }


def assessment_workspace_view(document) -> dict:
    doc = validate_course_knowledge(document)
    items = [
        {
            "item_id": item["item_id"],
            "sub_id": item["sub_id"],
            "document_id": item["document_id"],
            "question_no": item["question_no"],
            "label": item["label"],
            "content_hash": item["content_hash"],
            "citation_ids": item["citation_ids"],
        }
        for item in doc["assessment_items"]
    ]
    return {
        "view": "assessment_workspace",
        "contract": CONTRACT_ID,
        "course_id": doc["course_id"],
        "items": items,
        "counts": {
            "total": len(items),
            "lectures": len({item["sub_id"] for item in items if item["sub_id"]}),
            "course_level": sum(1 for item in items if not item["sub_id"]),
        },
        "updated_at": doc["updated_at"],
    }


VIEW_BUILDERS = {
    "course_overview": course_overview_view,
    "lecture_detail": lecture_detail_view,
    "assessment_workspace": assessment_workspace_view,
}


__all__ = [
    "CONTRACT_ID",
    "FIXTURE_SCHEMA",
    "NAMESPACE_CITATION",
    "NAMESPACE_TOPIC",
    "NAMESPACE_ASSESSMENT",
    "NAMESPACE_SOURCE",
    "EVIDENCE_KINDS",
    "KNOWLEDGE_STATUSES",
    "TOPIC_STATUSES",
    "SOURCE_SCOPES",
    "STALE_REASONS",
    "COVERAGE_KEYS",
    "LECTURE_COVERAGE_KEYS",
    "COURSE_COVERAGE_KEYS",
    "ANSWER_KEY_FIELDS",
    "LOCATOR_FIELDS",
    "CourseKnowledgeContractError",
    "canonical_json",
    "compute_id",
    "citation_id_for",
    "topic_id_for",
    "assessment_item_id_for",
    "source_id_for",
    "normalize_topic_title",
    "validate_evidence_ref",
    "validate_course_knowledge",
    "documents_match",
    "to_json",
    "from_json",
    "course_overview_view",
    "lecture_detail_view",
    "assessment_workspace_view",
    "VIEW_BUILDERS",
]
