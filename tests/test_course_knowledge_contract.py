"""Contract tests for ``courselens.course-knowledge.v1`` (N7K, K0 freeze).

The same module owns the frozen consumer fixture
``tests/fixtures/course_review_v1.json``.  Regenerate it (only when the
contract itself is deliberately re-frozen) with::

    python tests/test_course_knowledge_contract.py --write-fixture

Everything here is synthetic: no real course, account or runtime data.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import course_knowledge_contract as ck  # noqa: E402

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "course_review_v1.json"

COURSE_ID = "crs-synthetic-2026a"
SUB_L1 = "sub-syn-0001"
SUB_L2 = "sub-syn-0002"

SEG_L1 = "seg:111111111111"
SEG_L2 = "seg:222222222222"
SLIDE_L1 = "slevt:333333333333"
DOC_LECTURE = "a" * 32
DOC_COURSE = "b" * 32
QUESTION_ID = "c" * 32
BOOKMARK_ID = "bm-synthetic-0001"


def _digest(seed: str, length: int = 64) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:length]


def _ref(kind: str, source_id: str, revision_id: str, content_hash: str, locator: dict, label: str) -> dict:
    return {
        "kind": kind,
        "source_id": source_id,
        "revision_id": revision_id,
        "content_hash": content_hash,
        "locator": locator,
        "label": label,
    }


def _sources() -> list[dict]:
    return [
        {"kind": "transcript", "scope": "lecture", "sub_id": SUB_L1, "external_id": SEG_L1,
         "revision_id": _digest("rev-transcript-l1"), "label": "第 1 讲同步字幕"},
        {"kind": "transcript", "scope": "lecture", "sub_id": SUB_L2, "external_id": SEG_L2,
         "revision_id": _digest("rev-transcript-l2"), "label": "第 2 讲同步字幕"},
        {"kind": "slide", "scope": "lecture", "sub_id": SUB_L1, "external_id": SLIDE_L1,
         "revision_id": _digest("rev-slide-l1"), "label": "第 1 讲课间 PPT"},
        {"kind": "document_page", "scope": "lecture", "sub_id": SUB_L1, "external_id": DOC_LECTURE,
         "revision_id": _digest("rev-doc-lecture"), "label": "第 1 讲讲义"},
        {"kind": "document_page", "scope": "course", "sub_id": "", "external_id": DOC_COURSE,
         "revision_id": _digest("rev-doc-course"), "label": "课程级教材"},
        {"kind": "assessment_item", "scope": "course", "sub_id": "", "external_id": QUESTION_ID,
         "revision_id": _digest("rev-exam-paper"), "label": "期末真题"},
        {"kind": "bookmark", "scope": "lecture", "sub_id": SUB_L1, "external_id": BOOKMARK_ID,
         "revision_id": _digest("rev-bookmark-l1"), "label": "我的书签"},
    ]


def _refs() -> dict[str, dict]:
    return {
        "transcript_l1": _ref(
            "transcript", SEG_L1, _digest("rev-transcript-l1"), _digest("小波变换的定义"),
            {"start_ms": 125000, "end_ms": 168000}, "小波变换的定义",
        ),
        "transcript_l2": _ref(
            "transcript", SEG_L2, _digest("rev-transcript-l2"), _digest("滤波器组与重构"),
            {"start_ms": 40000, "end_ms": 92000}, "滤波器组与重构",
        ),
        "slide_l1": _ref(
            "slide", SLIDE_L1, _digest("rev-slide-l1"), _digest("ppt-page-7"),
            {"page": 7}, "第 7 页：多分辨率分析",
        ),
        "document_lecture": _ref(
            "document_page", DOC_LECTURE, _digest("rev-doc-lecture"), _digest("doc-page-3"),
            {"page": 3}, "讲义第 3 页",
        ),
        "document_course": _ref(
            "document_page", DOC_COURSE, _digest("rev-doc-course"), _digest("course-doc-page-1"),
            {"page": 1}, "教材第 1 章",
        ),
        "assessment": _ref(
            "assessment_item", QUESTION_ID, _digest("rev-exam-paper"), _digest("question-2")[:32],
            {"question_no": 2}, "第 2 题",
        ),
        "bookmark": _ref(
            "bookmark", BOOKMARK_ID, _digest("rev-bookmark-l1"), _digest("bookmark-note"),
            {"start_ms": 300000, "end_ms": 302000}, "我的书签：重点",
        ),
    }


def _point(text: str, *citation_keys: str) -> dict:
    return {"text": text, "citation_ids": [REF_MAP[key]["citation_id"] for key in citation_keys]}


def _topic(title: str, aliases: list[str], lecture_ids: list[str], citation_keys: list[str], status: str = "ready") -> dict:
    return {
        "title": title,
        "aliases": aliases,
        "lecture_ids": lecture_ids,
        "citation_ids": [REF_MAP[key]["citation_id"] for key in citation_keys],
        "status": status,
    }


_RAW_REFS = _refs()
REF_MAP = {
    key: ck.validate_evidence_ref(value, COURSE_ID, key) for key, value in _RAW_REFS.items()
}


def _lecture(
    sub_id: str,
    *,
    status: str,
    stale_reasons: list[str],
    ref_keys: list[str],
    key_points: list[dict],
    topics: list[dict],
    coverage: dict,
    input_hash: str,
    updated_at: float,
) -> dict:
    return {
        "sub_id": sub_id,
        "course_id": COURSE_ID,
        "input_hash": input_hash,
        "status": status,
        "stale_reasons": stale_reasons,
        "key_points": key_points,
        "topics": topics,
        "source_coverage": {
            "transcript_segments": 0,
            "slide_pages": 0,
            "document_pages": 0,
            "course_document_pages": 0,
            "assessment_items": 0,
            "bookmarks": 0,
            "lecture_ir": False,
            "summary": False,
            **coverage,
        },
        "evidence_refs": [copy.deepcopy(REF_MAP[key]) for key in ref_keys],
        "updated_at": updated_at,
    }


def build_complete() -> dict:
    """Two ready lectures with full AI coverage."""
    return {
        "contract": ck.CONTRACT_ID,
        "course_id": COURSE_ID,
        "input_hash": _digest("course-input-complete", 32),
        "status": "ready",
        "stale_reasons": [],
        "coverage": {
            "lectures_total": 2,
            "lectures_ready": 2,
            "lectures_partial": 0,
            "lectures_stale": 0,
            "transcript_segments": 2,
            "slide_pages": 1,
            "document_pages": 1,
            "course_document_pages": 1,
            "assessment_items": 1,
            "bookmarks": 1,
        },
        "topics": [
            _topic("小波变换", ["Wavelet Transform"], [SUB_L1, SUB_L2], ["transcript_l1", "transcript_l2"]),
            _topic("多分辨率分析", [], [SUB_L1], ["slide_l1"]),
            _topic("滤波器组", [], [SUB_L2], ["transcript_l2"]),
        ],
        "lectures": [
            _lecture(
                SUB_L1,
                status="ready",
                stale_reasons=[],
                ref_keys=["transcript_l1", "slide_l1", "document_lecture", "document_course", "bookmark"],
                key_points=[
                    _point("小波变换把信号分解到时频平面", "transcript_l1", "slide_l1"),
                    _point("多分辨率分析用尺度函数构造逼近", "slide_l1"),
                    _point("讲义与板书给出的滤波器条件一致", "document_lecture", "document_course"),
                ],
                topics=[
                    _topic("小波变换", ["Wavelet Transform"], [SUB_L1], ["transcript_l1"]),
                    _topic("多分辨率分析", [], [SUB_L1], ["slide_l1"]),
                ],
                coverage={
                    "transcript_segments": 1,
                    "slide_pages": 1,
                    "document_pages": 1,
                    "course_document_pages": 1,
                    "bookmarks": 1,
                    "lecture_ir": True,
                    "summary": True,
                },
                input_hash=_digest("lecture-l1-input", 32),
                updated_at=1787000000.0,
            ),
            _lecture(
                SUB_L2,
                status="ready",
                stale_reasons=[],
                ref_keys=["transcript_l2", "document_course"],
                key_points=[
                    _point("滤波器组给出可逆重构条件", "transcript_l2"),
                    _point("课程级教材补充了正交性证明", "document_course"),
                ],
                topics=[
                    _topic("小波变换", ["小波分析"], [SUB_L2], ["transcript_l2"]),
                    _topic("滤波器组", [], [SUB_L2], ["transcript_l2"]),
                ],
                coverage={
                    "transcript_segments": 1,
                    "course_document_pages": 1,
                    "lecture_ir": True,
                    "summary": True,
                },
                input_hash=_digest("lecture-l2-input", 32),
                updated_at=1787000100.0,
            ),
        ],
        "assessment_items": [
            {
                "course_id": COURSE_ID,
                "sub_id": "",
                "document_id": QUESTION_ID,
                "question_no": 2,
                "label": "第 2 题",
                "content_hash": _digest("question-2")[:32],
                "citation_ids": [REF_MAP["assessment"]["citation_id"]],
            }
        ],
        "sources": _sources(),
        "evidence_refs": [
            copy.deepcopy(REF_MAP["document_course"]),
            copy.deepcopy(REF_MAP["assessment"]),
        ],
        "updated_at": 1787000200.0,
    }


def build_partial() -> dict:
    """One lecture with PPT + document but no AI summary; the other has no transcript yet."""
    return {
        "contract": ck.CONTRACT_ID,
        "course_id": COURSE_ID,
        "input_hash": _digest("course-input-partial", 32),
        "status": "partial",
        "stale_reasons": ["summary_missing"],
        "coverage": {
            "lectures_total": 2,
            "lectures_ready": 0,
            "lectures_partial": 2,
            "lectures_stale": 0,
            "transcript_segments": 1,
            "slide_pages": 1,
            "document_pages": 1,
            "course_document_pages": 1,
            "assessment_items": 1,
            "bookmarks": 0,
        },
        "topics": [_topic("小波变换", [], [SUB_L1], ["transcript_l1"], status="partial")],
        "lectures": [
            _lecture(
                SUB_L1,
                status="partial",
                stale_reasons=["summary_missing"],
                ref_keys=["transcript_l1", "slide_l1", "document_lecture"],
                key_points=[_point("小波变换把信号分解到时频平面", "transcript_l1", "slide_l1")],
                topics=[_topic("小波变换", [], [SUB_L1], ["transcript_l1"], status="partial")],
                coverage={
                    "transcript_segments": 1,
                    "slide_pages": 1,
                    "document_pages": 1,
                    "lecture_ir": False,
                    "summary": False,
                },
                input_hash=_digest("lecture-l1-partial", 32),
                updated_at=1787000300.0,
            ),
            _lecture(
                SUB_L2,
                status="partial",
                stale_reasons=["transcript_missing", "summary_missing"],
                ref_keys=[],
                key_points=[],
                topics=[],
                coverage={},
                input_hash=_digest("lecture-l2-empty", 32),
                updated_at=1787000300.0,
            ),
        ],
        "assessment_items": [
            {
                "course_id": COURSE_ID,
                "sub_id": "",
                "document_id": QUESTION_ID,
                "question_no": 2,
                "label": "第 2 题",
                "content_hash": _digest("question-2")[:32],
                "citation_ids": [REF_MAP["assessment"]["citation_id"]],
            }
        ],
        "sources": _sources(),
        "evidence_refs": [copy.deepcopy(REF_MAP["document_course"]), copy.deepcopy(REF_MAP["assessment"])],
        "updated_at": 1787000400.0,
    }


def build_stale() -> dict:
    """A previously-good lecture whose source document changed; the other is still ready."""
    return {
        "contract": ck.CONTRACT_ID,
        "course_id": COURSE_ID,
        "input_hash": _digest("course-input-stale", 32),
        "status": "stale",
        "stale_reasons": ["input_changed"],
        "coverage": {
            "lectures_total": 2,
            "lectures_ready": 1,
            "lectures_partial": 0,
            "lectures_stale": 1,
            "transcript_segments": 2,
            "slide_pages": 1,
            "document_pages": 1,
            "course_document_pages": 1,
            "assessment_items": 1,
            "bookmarks": 0,
        },
        "topics": [_topic("小波变换", ["Wavelet Transform"], [SUB_L1, SUB_L2], ["transcript_l1", "transcript_l2"])],
        "lectures": [
            _lecture(
                SUB_L1,
                status="stale",
                stale_reasons=["document_changed"],
                ref_keys=["transcript_l1", "slide_l1", "document_lecture"],
                key_points=[_point("小波变换把信号分解到时频平面", "transcript_l1", "slide_l1")],
                topics=[_topic("小波变换", [], [SUB_L1], ["transcript_l1"])],
                coverage={
                    "transcript_segments": 1,
                    "slide_pages": 1,
                    "document_pages": 1,
                    "lecture_ir": True,
                    "summary": True,
                },
                input_hash=_digest("lecture-l1-stale", 32),
                updated_at=1787000500.0,
            ),
            _lecture(
                SUB_L2,
                status="ready",
                stale_reasons=[],
                ref_keys=["transcript_l2", "document_course"],
                key_points=[_point("滤波器组给出可逆重构条件", "transcript_l2")],
                topics=[_topic("小波变换", ["小波分析"], [SUB_L2], ["transcript_l2"])],
                coverage={
                    "transcript_segments": 1,
                    "course_document_pages": 1,
                    "lecture_ir": True,
                    "summary": True,
                },
                input_hash=_digest("lecture-l2-input", 32),
                updated_at=1787000600.0,
            ),
        ],
        "assessment_items": [
            {
                "course_id": COURSE_ID,
                "sub_id": "",
                "document_id": QUESTION_ID,
                "question_no": 2,
                "label": "第 2 题",
                "content_hash": _digest("question-2")[:32],
                "citation_ids": [REF_MAP["assessment"]["citation_id"]],
            }
        ],
        "sources": _sources(),
        "evidence_refs": [copy.deepcopy(REF_MAP["document_course"]), copy.deepcopy(REF_MAP["assessment"])],
        "updated_at": 1787000700.0,
    }


BUILDERS = {
    "complete": build_complete,
    "partial": build_partial,
    "stale": build_stale,
}

# Six rejection classes required by the task package, plus four more that cost
# nothing to freeze and catch drifts the six would miss.
REJECTIONS = [
    {"name": "dangling_citation", "base": "complete", "expected_code": "reference_missing",
     "ops": [{"op": "set", "path": "lectures[0].key_points[0].citation_ids[0]", "value": "ckc:000000000000"}]},
    {"name": "hash_malformed", "base": "complete", "expected_code": "hash_malformed",
     "ops": [{"op": "set", "path": "lectures[0].evidence_refs[0].revision_id", "value": "ZZZZ"}]},
    {"name": "unknown_evidence_kind", "base": "complete", "expected_code": "kind_unsupported",
     "ops": [{"op": "set", "path": "lectures[0].evidence_refs[0].kind", "value": "video"}]},
    {"name": "unknown_status", "base": "complete", "expected_code": "status_unsupported",
     "ops": [{"op": "set", "path": "lectures[0].status", "value": "done"}]},
    {"name": "assessment_pretends_official_answer", "base": "complete",
     "expected_code": "assessment_answer_unsupported",
     "ops": [{"op": "set", "path": "assessment_items[0].official_answer", "value": "B"}]},
    {"name": "cross_course_reference", "base": "complete", "expected_code": "cross_course_reference",
     "ops": [{"op": "set", "path": "lectures[0].course_id", "value": "crs-other-0002"}]},
    {"name": "missing_required_field", "base": "complete", "expected_code": "field_required",
     "ops": [{"op": "del", "path": "lectures[0].evidence_refs[0].content_hash"}]},
    {"name": "unknown_field", "base": "complete", "expected_code": "field_unknown",
     "ops": [{"op": "set", "path": "lectures[0].embedding_vector", "value": [0.1, 0.2]}]},
    {"name": "coverage_mismatch", "base": "complete", "expected_code": "coverage_mismatch",
     "ops": [{"op": "set", "path": "coverage.lectures_stale", "value": 1}]},
    {"name": "ready_must_not_be_stale", "base": "complete", "expected_code": "state_conflict",
     "ops": [{"op": "set", "path": "lectures[0].stale_reasons", "value": ["input_changed"]}]},
    {"name": "key_point_anchor_negative", "base": "complete", "expected_code": "type_invalid",
     "ops": [{"op": "set", "path": "lectures[0].key_points[0].anchor_ms", "value": -5}]},
    {"name": "key_point_anchor_wrong_type", "base": "complete", "expected_code": "type_invalid",
     "ops": [{"op": "set", "path": "lectures[0].key_points[0].anchor_ms", "value": "125000"}]},
]


def _apply_ops(document: dict, ops: list[dict]) -> dict:
    doc = copy.deepcopy(document)
    for op in ops:
        parts = []
        for chunk in str(op["path"]).split("."):
            while "[" in chunk:
                head, _, tail = chunk.partition("[")
                if head:
                    parts.append(head)
                index, _, rest = tail.partition("]")
                parts.append(int(index))
                chunk = rest
            if chunk:
                parts.append(chunk)
        target = doc
        for part in parts[:-1]:
            target = target[part]
        if op["op"] == "set":
            target[parts[-1]] = copy.deepcopy(op["value"])
        elif op["op"] == "del":
            del target[parts[-1]]
        else:  # pragma: no cover - fixture authoring error
            raise AssertionError(f"unknown op {op['op']!r}")
    return doc


def build_fixture() -> dict:
    documents = {name: ck.validate_course_knowledge(builder()) for name, builder in BUILDERS.items()}
    return {
        "fixture_schema": ck.FIXTURE_SCHEMA,
        "contract": ck.CONTRACT_ID,
        "documents": documents,
        "views": {
            "course_overview": ck.course_overview_view(documents["complete"]),
            "lecture_detail": ck.lecture_detail_view(documents["complete"], SUB_L1),
            "assessment_workspace": ck.assessment_workspace_view(documents["complete"]),
        },
        "rejections": REJECTIONS,
    }


# ---------------------------------------------------------------------------
# Tests


def test_complete_document_validates_and_is_canonically_stable():
    doc = ck.validate_course_knowledge(build_complete())
    assert doc["status"] == "ready"
    assert [lecture["sub_id"] for lecture in doc["lectures"]] == [SUB_L1, SUB_L2]
    assert doc["coverage"]["lectures_ready"] == 2
    # Re-validating the canonical form is a fixed point.
    assert ck.canonical_json(ck.validate_course_knowledge(doc)) == ck.canonical_json(doc)


def test_partial_and_stale_documents_validate_with_reasons():
    partial = ck.validate_course_knowledge(build_partial())
    assert partial["status"] == "partial"
    assert partial["lectures"][1]["stale_reasons"] == ["summary_missing", "transcript_missing"]
    assert partial["lectures"][1]["evidence_refs"] == []
    stale = ck.validate_course_knowledge(build_stale())
    assert stale["status"] == "stale"
    assert stale["lectures"][0]["stale_reasons"] == ["document_changed"]


def test_array_order_does_not_change_canonical_form():
    doc = build_complete()
    shuffled = copy.deepcopy(doc)
    shuffled["lectures"].reverse()
    shuffled["sources"] = list(reversed(shuffled["sources"]))
    shuffled["topics"] = list(reversed(shuffled["topics"]))
    shuffled["assessment_items"] = list(reversed(shuffled["assessment_items"]))
    for lecture in shuffled["lectures"]:
        lecture["evidence_refs"].reverse()
        lecture["topics"].reverse()
    assert ck.canonical_json(ck.validate_course_knowledge(shuffled)) == ck.canonical_json(
        ck.validate_course_knowledge(doc)
    )


def test_key_point_order_is_meaningful_and_preserved():
    """Key points are prose in intended reading order, so they are not sorted."""
    doc = build_complete()
    flipped = copy.deepcopy(doc)
    flipped["lectures"][0]["key_points"].reverse()
    canonical = ck.validate_course_knowledge(flipped)
    original = ck.validate_course_knowledge(doc)
    assert [point["text"] for point in canonical["lectures"][0]["key_points"]] == list(
        reversed([point["text"] for point in original["lectures"][0]["key_points"]])
    )


def test_key_point_anchor_ms_is_additive_and_projected():
    """RR-ANCHORFE-1：anchor_ms 是加性可选字段——带合法锚的要点原样投影到
    lecture_detail；旧快照（无该字段）验证仍是固定点，视图不投影锚键。"""
    doc = build_complete()
    doc["lectures"][0]["key_points"][0]["anchor_ms"] = 125000
    validated = ck.validate_course_knowledge(doc)
    assert validated["lectures"][0]["key_points"][0]["anchor_ms"] == 125000
    assert ck.canonical_json(ck.validate_course_knowledge(validated)) == ck.canonical_json(validated)
    view = ck.lecture_detail_view(validated, SUB_L1)
    assert view["key_points"][0]["anchor_ms"] == 125000
    assert "anchor_ms" not in view["key_points"][1]
    legacy_view = ck.lecture_detail_view(build_complete(), SUB_L1)
    assert all("anchor_ms" not in point for point in legacy_view["key_points"])


def test_ids_are_deterministic_and_position_independent():
    ref = REF_MAP["transcript_l1"]
    assert ref["citation_id"].startswith("ckc:")
    assert ck.citation_id_for(ref) == ref["citation_id"]
    other = copy.deepcopy(ref)
    other["locator"] = {"start_ms": 1, "end_ms": 2}
    assert ck.citation_id_for(other) != ref["citation_id"]
    assert ck.topic_id_for("小波变换", ["Wavelet Transform"]) == ck.topic_id_for(
        "小波变换", ["Wavelet Transform"]
    )
    assert ck.topic_id_for("小波变换", []) != ck.topic_id_for("小波变换改写", [])


def test_transcript_citation_is_jumpable():
    doc = ck.validate_course_knowledge(build_complete())
    refs = {ref["citation_id"]: ref for ref in doc["lectures"][0]["evidence_refs"]}
    ref = refs[REF_MAP["transcript_l1"]["citation_id"]]
    assert ref["kind"] == "transcript"
    assert ref["source_id"] == SEG_L1  # reuses the evidence.v1 segment id
    assert ref["locator"]["start_ms"] < ref["locator"]["end_ms"]


def test_assessment_reference_view_carries_no_answer():
    doc = ck.validate_course_knowledge(build_complete())
    item = doc["assessment_items"][0]
    assert set(item) == {
        "item_id", "course_id", "sub_id", "document_id", "question_no",
        "label", "content_hash", "citation_ids",
    }
    assert item["sub_id"] == ""  # course-level paper


def test_course_scope_document_page_is_visible_to_course_view():
    doc = ck.validate_course_knowledge(build_complete())
    view = ck.course_overview_view(doc)
    kinds = {(source["kind"], source["scope"]) for source in view["sources"]}
    assert ("document_page", "course") in kinds
    assert view["coverage"]["course_document_pages"] == 1
    merged = [topic for topic in view["topics"] if topic["title"] == "小波变换"]
    assert len(merged) == 1
    assert merged[0]["lecture_ids"] == [SUB_L1, SUB_L2]


@pytest.mark.parametrize("case", REJECTIONS, ids=[case["name"] for case in REJECTIONS])
def test_rejection_samples_fail_closed(case):
    document = _apply_ops(BUILDERS[case["base"]](), case["ops"])
    with pytest.raises(ck.CourseKnowledgeContractError) as error:
        ck.validate_course_knowledge(document)
    assert error.value.code == case["expected_code"]


def test_lecture_detail_view_raises_for_unknown_lecture():
    with pytest.raises(ck.CourseKnowledgeContractError) as error:
        ck.lecture_detail_view(build_complete(), "sub-does-not-exist")
    assert error.value.code == "lecture_not_found"


def test_from_json_rejects_duplicate_keys_and_non_finite():
    doc = ck.validate_course_knowledge(build_complete())
    assert ck.from_json(ck.to_json(doc)) == doc
    with pytest.raises(ck.CourseKnowledgeContractError) as error:
        ck.from_json('{"contract": "a", "contract": "b"}')
    assert error.value.code == "duplicate_key"
    with pytest.raises(ck.CourseKnowledgeContractError) as error:
        ck.from_json('{"contract": NaN}')
    assert error.value.code == "value_invalid"


def _paths(value, prefix=""):
    """Every addressable position inside a JSON value (bounded)."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append((prefix + "." + str(key), key))
            found.extend(_paths(item, prefix + "." + str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.append((f"{prefix}[{index}]", index))
            found.extend(_paths(item, f"{prefix}[{index}]"))
    return found[:400]


def _mutate(document: dict, seed: int) -> dict:
    """Deterministically corrupt one leaf of a valid document."""
    value = copy.deepcopy(document)
    places = [(path, key) for path, key in _paths(value)]
    if not places:
        return value
    path, key = places[seed % len(places)]
    target = value
    for part in _split_path(path)[:-1]:
        target = target[part]
    final = _split_path(path)[-1]
    action = seed % 5
    if action == 0:
        del target[final]
    elif action == 1:
        target[final] = {"unexpected": "object"}
    elif action == 2:
        target[final] = 10 ** 9
    elif action == 3:
        target[final] = "x" * 5000
    else:
        target[final] = ["unexpected", "array"]
    return value


def _split_path(path: str):
    parts = []
    for chunk in path.lstrip(".").split("."):
        while "[" in chunk:
            head, _, tail = chunk.partition("[")
            if head:
                parts.append(head)
            index, _, rest = tail.partition("]")
            parts.append(int(index))
            chunk = rest
        if chunk:
            parts.append(chunk)
    return parts


def test_fuzzed_documents_never_raise_anything_but_the_contract_error():
    """A malformed document must fail closed, never with a raw Python error.

    A non-contract exception here would surface as a 500 on the read route
    instead of a closed error code, so the validator is fuzzed with one
    deterministic corruption per leaf position.
    """
    base = build_complete()
    accepted = 0
    rejected = 0
    codes: dict[str, int] = {}
    for seed in range(160):
        candidate = _mutate(base, seed)
        try:
            ck.validate_course_knowledge(candidate)
        except ck.CourseKnowledgeContractError as error:
            rejected += 1
            codes[str(error.code)] = codes.get(str(error.code), 0) + 1
        except Exception as error:  # noqa: BLE001 - that is exactly the failure
            raise AssertionError(f"seed {seed} raised {type(error).__name__}: {error}") from error
        else:
            accepted += 1
    assert rejected + accepted == 160
    # Measured at 144/160 with twelve distinct closed codes; the floor keeps
    # this test from silently becoming vacuous if a mutation stops landing.
    assert rejected >= 120, codes
    assert len(codes) >= 8, codes
    # Harmless corruptions (e.g. dropping an optional label) stay acceptable.
    assert accepted >= 5, accepted


def test_fixture_file_matches_builders_and_views():
    assert FIXTURE_PATH.exists(), f"missing frozen fixture {FIXTURE_PATH}"
    frozen = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert frozen["fixture_schema"] == ck.FIXTURE_SCHEMA
    assert frozen["contract"] == ck.CONTRACT_ID
    generated = build_fixture()
    for name, builder in BUILDERS.items():
        assert ck.documents_match(frozen["documents"][name], builder()), name
    assert frozen["views"] == generated["views"]
    assert [case["name"] for case in frozen["rejections"]] == [case["name"] for case in REJECTIONS]


def test_fixture_views_are_the_three_frozen_shapes():
    frozen = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert set(frozen["views"]) == {"course_overview", "lecture_detail", "assessment_workspace"}
    assert frozen["views"]["course_overview"]["view"] == "course_overview"
    assert frozen["views"]["course_overview"]["lectures"][0]["stale_reasons"] == []
    assert frozen["views"]["lecture_detail"]["sub_id"] == SUB_L1
    assert frozen["views"]["assessment_workspace"]["counts"]["total"] == 1
    assert set(ck.VIEW_BUILDERS) == {"course_overview", "lecture_detail", "assessment_workspace"}


def _write_fixture() -> None:
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(
        json.dumps(build_fixture(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {FIXTURE_PATH}")


if __name__ == "__main__":  # pragma: no cover - fixture regeneration helper
    if "--write-fixture" in sys.argv:
        _write_fixture()
    else:
        raise SystemExit("usage: python tests/test_course_knowledge_contract.py --write-fixture")
