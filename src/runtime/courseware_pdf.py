"""Bounded local lecture courseware (slide) PDF generation.

Without a plan, consumes authorized iCourse ``pptimgurl`` records in stable
capture order, conservatively filters them, spills accepted pages to a
package-owned temporary directory, and assembles one image-only PDF beside a
small manifest. With a verified ``courseware_plan.v1`` from the cloud result,
downloads only the plan-selected captures in plan output order, proves every
page against the plan's exact SHA-256 before decode, and never invents a new
keep/order decision locally: a locator that moved gets one bounded
exact-hash reconciliation against the current inventory, and an expected
page that cannot be proven pauses with ``courseware_plan_changed`` instead of
being substituted, omitted or reordered.

Resource limits (per-image bytes/pixels, total bytes, wall clock, request
rate, distinct-page storm circuit) derive the "pause" state — there is
deliberately no low page-count ceiling. A paused run persists a resumable
ledger; only exact ``source_sha256`` duplicates ever collapse, so teacher
handwriting and annotation variants always survive.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageOps, UnidentifiedImageError

from src.api.icourse import ppt_record_order
from src.runtime.junk_page_filter import (
    DEVICE_PLACEHOLDER_SKIP_REASON,
    JUNK_BLACKLIST_NEAR_MAX,
    JUNK_FAMILY_MIN_MEMBERS,
    JUNK_LOCAL_ADJACENT_DHASH_MAX,
    JUNK_PAGE_SKIP_REASON,
    JUNK_PLACEHOLDER_MIN_DECK,
    adjudicate_deck_local,
    frame_features,
    hamming_distance,
    information_density,
    page_is_junk,
    placeholder_verdict,
)

# v4 (SRC-CLEANUP-1 U8): adjacent-frame groups keep their densest frame
# (information-density fold, replacing keep-first) and a deck-context pass
# kills device/system placeholder screens (Miracast standby, monitor confirm
# dialogs, classroom desktop) anchored on the deck's own content median —
# v3 resume ledgers predate both, so they refetch from scratch instead of
# mixing keep policies inside one deck.
# v3 (SRC-SYNDROME-1 追加C): adjacent-frame dedup (dHash distance ≤5) joins the
# accept policy and the local near-solid threshold tightens — v2 resume
# ledgers predate both, so they refetch from scratch instead of assembling a
# deck from pages accepted under mixed policies. v2: manual local run screens
# browser-junk pages (junk_page skip) the same way the cloud worker always
# has; v1 resume ledgers predate the local filter.
POLICY_VERSION = "courseware-pdf.v4"
MANIFEST_SCHEMA = "courselens.courseware-pdf-manifest.v1"

# Closed-set skip reasons (no field values, no URLs). One bad page is a
# counted skip, never a failed lecture.
RECORD_SKIPS = frozenset({"record_invalid", "record_url_missing"})
PAGE_SKIPS = frozenset({
    "fetch_failed", "empty", "html_body", "json_body",
    "unidentified_image", "decode_failed", "oversized_image",
    JUNK_PAGE_SKIP_REASON,
    DEVICE_PLACEHOLDER_SKIP_REASON,
})
DEDUP_SKIPS = frozenset({"duplicate_exact", "duplicate_adjacent"})
SKIP_REASONS = RECORD_SKIPS | PAGE_SKIPS | DEDUP_SKIPS

PAUSE_CODES = frozenset({
    "resource_budget_paused", "event_storm_paused", "wall_clock_paused",
    "courseware_plan_changed", "plan_fetch_paused", "plan_page_unreadable",
})
ANNOTATION_CLASSES = frozenset({"clean_candidate", "annotated_candidate", "unknown"})

# --- courseware_plan.v1 (cloud keep/order plan; no URLs may appear here) -----

PLAN_SCHEMA = "courseware_plan.v1"
# Mirrors src.runtime.automation.CLOUD_PROTOCOL_VERSION without importing the
# heavier automation module into the Pillow-only startup import path.
PLAN_PIPELINE = "cloud-automation.v3"
PLAN_KEYS = frozenset({
    "schema", "policy_version", "pipeline", "course_id", "sub_id",
    "inventory_digest", "entries", "excluded", "ordering", "counts",
})
PLAN_ENTRY_KEYS = frozenset({
    "output_position", "capture_position", "record_id",
    "capture_time", "capture_ordinal", "source_sha256", "page_label",
    "page_label_source", "annotation", "keep_reason",
    "version_of_position", "duplicate_count",
})
PLAN_EXCLUDED_KEYS = frozenset({
    "kept_position", "capture_time", "capture_ordinal", "source_sha256", "reason",
})
PLAN_ORDERING_KEYS = frozenset({"mode", "confidence"})
PLAN_COUNT_KEYS = frozenset({
    "input_events", "recognized", "kept", "exact_duplicates", "skipped",
})
PLAN_KEEP_REASONS = frozenset({"distinct_capture", "version_variant_retained"})
PLAN_EXCLUDE_REASONS = frozenset({"exact_duplicate"})
PLAN_ORDERING_MODES = frozenset({"page_label", "capture_order"})
PLAN_LABEL_SOURCES = frozenset({"", "ocr_text"})
PLAN_RECORD_ID_MAX_LENGTH = 64


class CoursewarePlanError(RuntimeError):
    """A structurally invalid or misbound plan; the run fails closed."""

    def __init__(self, code: str = "courseware_plan_invalid", message: str = ""):
        super().__init__(message or code)
        self.code = code if code in {"courseware_plan_invalid", "courseware_plan_mismatch"} \
            else "courseware_plan_invalid"


def courseware_plan_digest(plan: dict) -> str:
    """The canonical plan digest — identical to the importer/worker formula."""
    return hashlib.sha256(json.dumps(
        plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _plan_uint(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _plan_hex64(value: object) -> str | None:
    text = str(value or "").lower()
    if len(text) == 64 and all(char in "0123456789abcdef" for char in text):
        return text
    return None


def _plan_record_id(value: object) -> str | None:
    """Same bounded opaque shape the worker enforces before joining a plan."""
    record_id = str("" if value is None else value).strip()
    if not record_id:
        return ""
    if (
        len(record_id) > PLAN_RECORD_ID_MAX_LENGTH
        or any(char.isspace() for char in record_id)
        or "://" in record_id
        or "@" in record_id
    ):
        return None
    return record_id


def _plan_confidence(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if 0.0 <= number <= 1.0 else None


@dataclass(frozen=True)
class CoursewarePlan:
    """One structurally verified plan, normalized for the planned run."""

    course_id: str
    sub_id: str
    pipeline: str
    inventory_digest: str
    entries: list
    duplicates_by_capture_position: dict
    ordering: dict
    counts: dict
    digest: str


def validate_courseware_plan(
    plan: object,
    *,
    course_id: str = "",
    sub_id: str = "",
    plan_digest: str = "",
) -> CoursewarePlan:
    """Fail-closed structural verification of one ``courseware_plan.v1``.

    Enforces the frozen v1 field allowlists, output/capture position
    permutations, exact-hash shape, closed keep/order reason sets and the
    course/lecture binding; raises :class:`CoursewarePlanError` otherwise.
    """
    if not isinstance(plan, dict):
        raise CoursewarePlanError(message="plan is not an object")
    if set(plan) != PLAN_KEYS:
        raise CoursewarePlanError(message="plan field set is not courseware_plan.v1")
    if str(plan.get("schema") or "") != PLAN_SCHEMA:
        raise CoursewarePlanError(message="unsupported plan schema")
    if _plan_uint(plan.get("policy_version")) != 1:
        raise CoursewarePlanError(message="unsupported plan policy version")
    pipeline = str(plan.get("pipeline") or "")
    if pipeline != PLAN_PIPELINE:
        raise CoursewarePlanError(message="plan comes from another pipeline version")
    plan_course = str(plan.get("course_id") or "")
    plan_sub = str(plan.get("sub_id") or "")
    if not plan_course or not plan_sub:
        raise CoursewarePlanError(message="plan is missing its course/lecture binding")
    if (course_id and plan_course != str(course_id)) or (sub_id and plan_sub != str(sub_id)):
        raise CoursewarePlanError(
            code="courseware_plan_mismatch",
            message="plan is bound to another course or lecture",
        )
    inventory_digest = _plan_hex64(plan.get("inventory_digest"))
    if inventory_digest is None:
        raise CoursewarePlanError(message="plan inventory digest is malformed")

    entries_raw = plan.get("entries")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise CoursewarePlanError(message="plan has no kept entries")
    total = len(entries_raw)
    entries: list[dict] = []
    for item in entries_raw:
        if not isinstance(item, dict) or set(item) != PLAN_ENTRY_KEYS:
            raise CoursewarePlanError(message="plan entry field set is not v1")
        output_position = _plan_uint(item.get("output_position"))
        capture_position = _plan_uint(item.get("capture_position"))
        if not output_position or output_position > total:
            raise CoursewarePlanError(message="plan output position is out of range")
        if not capture_position or capture_position > total:
            raise CoursewarePlanError(message="plan capture position is out of range")
        record_id = _plan_record_id(item.get("record_id"))
        if record_id is None:
            raise CoursewarePlanError(message="plan record id is URL-like or oversized")
        capture_time = _plan_uint(item.get("capture_time"))
        capture_ordinal = _plan_uint(item.get("capture_ordinal"))
        if capture_time is None or capture_ordinal is None:
            raise CoursewarePlanError(message="plan capture guard is malformed")
        source_sha256 = _plan_hex64(item.get("source_sha256"))
        if source_sha256 is None:
            raise CoursewarePlanError(message="plan page hash is malformed")
        page_label = str(item.get("page_label") or "")
        page_label_source = str(item.get("page_label_source") or "")
        if page_label_source not in PLAN_LABEL_SOURCES:
            raise CoursewarePlanError(message="plan page label source is unknown")
        annotation = item.get("annotation")
        if not isinstance(annotation, dict) or set(annotation) != {"class", "confidence"}:
            raise CoursewarePlanError(message="plan annotation field set is not v1")
        annotation_class = str(annotation.get("class") or "")
        if annotation_class not in ANNOTATION_CLASSES:
            raise CoursewarePlanError(message="plan annotation class is unknown")
        annotation_confidence = _plan_confidence(annotation.get("confidence"))
        if annotation_confidence is None:
            raise CoursewarePlanError(message="plan annotation confidence is malformed")
        keep_reason = str(item.get("keep_reason") or "")
        if keep_reason not in PLAN_KEEP_REASONS:
            raise CoursewarePlanError(message="plan keep reason is unknown")
        version_of_position = _plan_uint(item.get("version_of_position"))
        duplicate_count = _plan_uint(item.get("duplicate_count"))
        if version_of_position is None or version_of_position > total \
                or duplicate_count is None:
            raise CoursewarePlanError(message="plan version/duplicate reference is malformed")
        entries.append({
            "output_position": output_position,
            "capture_position": capture_position,
            "record_id": record_id,
            "capture_time": capture_time,
            "capture_ordinal": capture_ordinal,
            "source_sha256": source_sha256,
            "page_label": page_label,
            "page_label_source": page_label_source,
            "annotation": {
                "class": annotation_class,
                "confidence": annotation_confidence,
            },
            "keep_reason": keep_reason,
            "version_of_position": version_of_position,
            "duplicate_count": duplicate_count,
        })
    if sorted(entry["output_position"] for entry in entries) != list(range(1, total + 1)):
        raise CoursewarePlanError(message="plan output positions are not 1..M")
    if sorted(entry["capture_position"] for entry in entries) != list(range(1, total + 1)):
        raise CoursewarePlanError(message="plan capture positions are not 1..M")
    if len({entry["source_sha256"] for entry in entries}) != total:
        raise CoursewarePlanError(message="plan keeps the same source hash twice")
    by_capture = {entry["capture_position"]: entry for entry in entries}

    excluded_raw = plan.get("excluded")
    if not isinstance(excluded_raw, list):
        raise CoursewarePlanError(message="plan excluded list is malformed")
    duplicates_by_capture: dict[int, list[dict]] = {}
    for item in excluded_raw:
        if not isinstance(item, dict) or set(item) != PLAN_EXCLUDED_KEYS:
            raise CoursewarePlanError(message="plan excluded field set is not v1")
        kept_position = _plan_uint(item.get("kept_position"))
        if not kept_position or kept_position > total:
            raise CoursewarePlanError(message="plan excluded kept position is out of range")
        capture_time = _plan_uint(item.get("capture_time"))
        capture_ordinal = _plan_uint(item.get("capture_ordinal"))
        if capture_time is None or capture_ordinal is None:
            raise CoursewarePlanError(message="plan excluded capture guard is malformed")
        source_sha256 = _plan_hex64(item.get("source_sha256"))
        if source_sha256 is None:
            raise CoursewarePlanError(message="plan excluded hash is malformed")
        if str(item.get("reason") or "") not in PLAN_EXCLUDE_REASONS:
            raise CoursewarePlanError(message="plan excluded reason is unknown")
        keeper = by_capture[kept_position]
        if keeper["source_sha256"] != source_sha256:
            raise CoursewarePlanError(message="plan excluded hash does not match its keeper")
        duplicates_by_capture.setdefault(kept_position, []).append({
            "original_id": "",
            "created_sec": capture_time,
            "capture_ordinal": capture_ordinal,
        })

    ordering = plan.get("ordering")
    if not isinstance(ordering, dict) or set(ordering) != PLAN_ORDERING_KEYS:
        raise CoursewarePlanError(message="plan ordering field set is not v1")
    if str(ordering.get("mode") or "") not in PLAN_ORDERING_MODES:
        raise CoursewarePlanError(message="plan ordering mode is unknown")
    if _plan_confidence(ordering.get("confidence")) is None:
        raise CoursewarePlanError(message="plan ordering confidence is malformed")
    counts = plan.get("counts")
    if not isinstance(counts, dict) or set(counts) != PLAN_COUNT_KEYS:
        raise CoursewarePlanError(message="plan counts field set is not v1")
    normalized_counts = {}
    for key in PLAN_COUNT_KEYS:
        value = _plan_uint(counts.get(key))
        if value is None:
            raise CoursewarePlanError(message="plan counts are malformed")
        normalized_counts[key] = value
    if normalized_counts["kept"] != total \
            or normalized_counts["exact_duplicates"] != len(excluded_raw):
        raise CoursewarePlanError(message="plan counts disagree with its entries")

    digest = courseware_plan_digest(plan)
    if plan_digest and digest != str(plan_digest):
        raise CoursewarePlanError(message="plan digest does not match its committed digest")
    return CoursewarePlan(
        course_id=plan_course,
        sub_id=plan_sub,
        pipeline=pipeline,
        inventory_digest=inventory_digest,
        entries=entries,
        duplicates_by_capture_position=duplicates_by_capture,
        ordering={"mode": str(ordering["mode"]), "confidence": float(ordering["confidence"])},
        counts=normalized_counts,
        digest=digest,
    )


@dataclass(frozen=True)
class PageBudget:
    """Large-budget circuit breakers — deliberately not page-count caps."""

    max_image_bytes: int = 24 * 1024 * 1024
    max_image_pixels: int = 40_000_000
    max_total_bytes: int = 768 * 1024 * 1024
    max_wall_seconds: float = 900.0
    min_request_interval: float = 0.05
    pdf_max_long_edge: int = 2200
    distinct_page_storm_limit: int = 2500
    jpeg_quality: int = 85
    # Extra downloads the one bounded exact-hash reconciliation may spend per
    # attempt when a plan locator drifted. A fresh attempt gets a fresh cap.
    plan_reconcile_fetch_cap: int = 48


@dataclass
class PageEntry:
    """One accepted page; everything here is nonsecret manifest metadata."""

    seq: int
    original_id: str
    created_sec: int
    created_ms: int
    source_sha256: str
    file: str
    marker_signature: str = ""
    page_label: str = ""
    annotation_class: str = "unknown"
    annotation_confidence: float = 0.0
    duplicates: list[dict] = field(default_factory=list)
    # Plan mode only: the plan's stable first-capture identity of this page
    # (output position stays ``seq``). Zero in the legacy capture-order run.
    capture_position: int = 0

    def as_manifest(self) -> dict:
        manifest = {
            "seq": self.seq,
            "original_id": self.original_id,
            "created_sec": self.created_sec,
            "created_ms": self.created_ms,
            "source_sha256": self.source_sha256,
            "marker_signature": self.marker_signature,
            "page_label": self.page_label,
            "annotation": {
                "class": self.annotation_class,
                "confidence": round(float(self.annotation_confidence), 3),
            },
            "duplicates": list(self.duplicates),
        }
        if self.capture_position:
            manifest["capture_position"] = self.capture_position
        return manifest

    def as_resume(self) -> dict:
        return {
            **self.as_manifest(),
            "file": self.file,
        }

    @classmethod
    def from_resume(cls, value: dict) -> "PageEntry":
        annotation = value.get("annotation") if isinstance(value.get("annotation"), dict) else {}
        return cls(
            seq=int(value.get("seq") or 0),
            original_id=str(value.get("original_id") or ""),
            created_sec=int(value.get("created_sec") or 0),
            created_ms=int(value.get("created_ms") or 0),
            source_sha256=str(value.get("source_sha256") or ""),
            file=str(value.get("file") or ""),
            marker_signature=str(value.get("marker_signature") or ""),
            page_label=str(value.get("page_label") or ""),
            annotation_class=str(annotation.get("class") or "unknown"),
            annotation_confidence=float(annotation.get("confidence") or 0.0),
            duplicates=[
                dict(item) for item in (value.get("duplicates") or [])
                if isinstance(item, dict)
            ],
            capture_position=int(value.get("capture_position") or 0),
        )


# --- conservative record validation -----------------------------------------


def validate_record(record: object) -> tuple[dict | None, str | None]:
    """Shape-check one ppt record; ``(None, reason)`` is a closed skip."""
    if not isinstance(record, dict):
        return None, "record_invalid"
    url = record.get("pptimgurl")
    if not isinstance(url, str) or not url.strip():
        return None, "record_url_missing"
    try:
        created_sec = int(record.get("created_sec", 0) or 0)
        created_ms = int(record.get("created_ms", 0) or 0)
    except (TypeError, ValueError):
        return None, "record_invalid"
    if created_sec < 0 or created_ms < 0:
        return None, "record_invalid"
    return record, None


def _text_body_kind(raw: bytes) -> str:
    """Classify a short prefix as an HTML/JSON body (same rules as Worker OCR)."""
    prefix = raw[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html"):
        return "html_body"
    if prefix[:1] in (b"{", b"["):
        return "json_body"
    return ""


# --- marker regions: auxiliary metadata only, never an order/deletion key ----

_MARKER_ZONES = (
    (0.72, 0.00, 1.00, 0.18),   # upper-right
    (0.72, 0.82, 1.00, 1.00),   # lower-right
    (0.30, 0.88, 0.70, 1.00),   # bottom-center
)

_PAGE_LABEL_RE = re.compile(
    r"(?:^|[^\d])(\d{1,3})\s*(?:/|of|共)\s*(\d{1,3})(?:[^\d]|$)"
)
_CHINESE_PAGE_LABEL_RE = re.compile(r"第\s*(\d{1,3})\s*页")


def marker_signature(image: Image.Image) -> str:
    """Coarse pixel digest of the three marker zones (metadata only)."""
    width, height = image.size
    gray = image.convert("L")
    chunks = []
    for left, top, right, bottom in _MARKER_ZONES:
        box = (
            int(width * left), int(height * top),
            int(width * right), int(height * bottom),
        )
        if box[2] <= box[0] or box[3] <= box[1]:
            chunks.append(b"")
            continue
        crop = gray.crop(box).resize((16, 9))
        chunks.append(bytes(value // 64 for value in crop.tobytes()))
    digest = hashlib.sha256(b"\0".join(chunks)).hexdigest()
    return f"mk:{digest[:16]}"


def marker_label(text: str) -> str:
    """Normalize ``11/60``-style page labels from existing OCR text.

    Returns ``""`` when the text carries no believable label; labels are
    stored as manifest metadata and never reorder or delete a page.
    """
    value = str(text or "")
    match = _PAGE_LABEL_RE.search(value)
    if match:
        current, total = int(match.group(1)), int(match.group(2))
        if 1 <= current <= total <= 999:
            return f"{current}/{total}"
        return ""
    match = _CHINESE_PAGE_LABEL_RE.search(value)
    if match:
        current = int(match.group(1))
        if 1 <= current <= 999:
            return f"{current}"
    return ""


def _excluded_zone_spans(width: int, height: int) -> list[tuple[int, int, int, int]]:
    """Inclusive pixel spans of the marker zones on a scaled page.

    Same clamped, at-least-one-pixel slicing the zone mask always used; the
    spans zero the zones out of the difference raster before counting.
    """
    spans = []
    for left, top, right, bottom in _MARKER_ZONES:
        x0, y0 = int(width * left), int(height * top)
        x1, y1 = int(width * right), int(height * bottom)
        spans.append((
            x0, y0,
            min(max(x1, x0 + 1) - 1, width - 1),
            min(max(y1, y0 + 1) - 1, height - 1),
        ))
    return spans


def classify_variant_pair(base: Image.Image, variant: Image.Image) -> dict:
    """Conservative combined-evidence annotation classification (metadata).

    Compares two same-aspect pages on a small aligned grayscale raster with
    the marker zones excluded. Localized high-contrast differences suggest a
    hand-drawn overlay; anything else stays ``unknown``. The result never
    deletes or replaces a page — an ``annotated_candidate`` is not a
    replacement for the earlier clean capture, and every non-exact variant
    remains in the PDF.
    """
    width = 320
    target = (width, max(1, int(width * base.size[1] / max(1, base.size[0]))))
    a = base.convert("L").resize(target)
    b = variant.convert("L").resize(target)
    if a.size != b.size:
        return {"class": "unknown", "confidence": 0.0}
    raster_width, raster_height = target
    diff = ImageChops.difference(a, b)
    draw = ImageDraw.Draw(diff)
    for x0, y0, x1, y1 in _excluded_zone_spans(raster_width, raster_height):
        draw.rectangle([x0, y0, x1, y1], fill=0)
    histogram = diff.histogram()
    total = raster_width * raster_height
    changed_count = sum(histogram[25:])
    fraction = changed_count / float(total)
    if fraction <= 0.0005:
        return {"class": "clean_candidate", "confidence": 0.8}
    bbox = diff.point(lambda value: 255 if value > 24 else 0).getbbox()
    if bbox is None:
        return {"class": "unknown", "confidence": 0.0}
    box_area = max(1.0, float((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])))
    spread = box_area / float(raster_height * raster_width)
    if fraction <= 0.06 and spread <= 0.5:
        return {"class": "annotated_candidate", "confidence": 0.55}
    return {"class": "unknown", "confidence": 0.0}


# --- decoding ---------------------------------------------------------------


def decode_slide(raw: bytes, budget: PageBudget) -> tuple[Image.Image | None, str | None]:
    """Decode one downloaded slide to a bounded RGB page or a closed skip."""
    if not raw:
        return None, "empty"
    text_kind = _text_body_kind(raw)
    if text_kind:
        return None, text_kind
    if len(raw) > budget.max_image_bytes:
        return None, "oversized_image"
    try:
        opened = Image.open(io.BytesIO(raw))
        width, height = opened.size
        if width * height > budget.max_image_pixels:
            return None, "oversized_image"
        normalized = ImageOps.exif_transpose(opened)
        page = normalized.convert("RGB")
    except UnidentifiedImageError:
        return None, "unidentified_image"
    except Exception:
        return None, "decode_failed"
    long_edge = max(page.size)
    if long_edge > budget.pdf_max_long_edge:
        scale = budget.pdf_max_long_edge / float(long_edge)
        page = page.resize(
            (max(1, int(page.size[0] * scale)), max(1, int(page.size[1] * scale))),
            Image.LANCZOS,
        )
    return page, None


# --- the run driver ----------------------------------------------------------


class CoursewarePdfRun:
    """One resumable fetch/filter/assemble pass over sorted slide records.

    ``fetch_page(record) -> bytes`` is injected by the caller (the local
    authorized iCourse session). The run owns only ``work_dir``; on success
    it publishes the PDF and manifest atomically and removes its own
    temporary files, and on pause/failure it leaves a resumable ledger.
    """

    def __init__(
        self,
        *,
        records: list,
        work_dir: Path,
        out_pdf: Path,
        out_manifest: Path,
        budget: PageBudget | None = None,
        fetch_page=None,
        clock=time.monotonic,
        sleeper=time.sleep,
        on_progress=None,
        plan: dict | None = None,
        plan_digest: str = "",
        course_id: str = "",
        sub_id: str = "",
    ):
        self.records = list(records or [])
        self.work_dir = Path(work_dir)
        self.out_pdf = Path(out_pdf)
        self.out_manifest = Path(out_manifest)
        self.budget = budget or PageBudget()
        self._fetch_page = fetch_page
        self._clock = clock
        self._sleeper = sleeper
        self._on_progress = on_progress
        self.plan = plan
        self.plan_digest = str(plan_digest or "")
        self.course_id = str(course_id or "")
        self.sub_id = str(sub_id or "")
        self._started = 0.0
        self._next_request_at = 0.0
        self._plan_fetched_urls: set[str] = set()

    def _report(self, stage: str, processed: int, kept: int, total: int) -> None:
        # 可信进度只来自真实 processed/total 计数；没有计数就没有百分比
        if self._on_progress is None:
            return
        try:
            self._on_progress(stage, int(processed), int(kept), int(total))
        except Exception:
            pass

    # -- resume ledger -------------------------------------------------------

    @property
    def _resume_path(self) -> Path:
        return self.work_dir / "resume.json"

    def _load_resume(self) -> dict:
        try:
            value = json.loads(self._resume_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if (
            not isinstance(value, dict)
            or value.get("policy_version") != POLICY_VERSION
            or value.get("mode") == "plan"
        ):
            return {}
        return value

    def _save_resume(
        self, *, cursor: int, entries: list[PageEntry], ledger: set[str],
        skipped: Counter, byte_spent: int,
    ) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "policy_version": POLICY_VERSION,
            "events_total": len(self.records),
            "cursor": int(cursor),
            "byte_spent": int(byte_spent),
            "ledger": sorted(ledger),
            "skipped": dict(skipped),
            "entries": [entry.as_resume() for entry in entries],
        }
        tmp = self._resume_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._resume_path)

    # -- helpers -------------------------------------------------------------

    def _entry_dhash(self, entry: PageEntry) -> str:
        """Recompute one accepted page's dHash from its stored file (resume seed)."""
        try:
            with Image.open(self.work_dir / entry.file) as image:
                return str(frame_features(image.convert("RGB"))["dhash"])
        except Exception:
            return ""

    def _wall_exceeded(self) -> bool:
        return (self._clock() - self._started) > self.budget.max_wall_seconds

    def _rate_limit(self) -> None:
        now = self._clock()
        if now < self._next_request_at:
            self._sleeper(self._next_request_at - now)
        self._next_request_at = max(self._clock(), self._next_request_at) \
            + self.budget.min_request_interval

    def _cleanup_owned_files(self) -> None:
        for entry in list(self.work_dir.glob("page-*.img")):
            entry.unlink(missing_ok=True)
        self._resume_path.unlink(missing_ok=True)
        tmp = self._resume_path.with_suffix(".json.tmp")
        tmp.unlink(missing_ok=True)

    def _page_files(self, entries: list[PageEntry]) -> list[Path]:
        return [self.work_dir / entry.file for entry in entries]

    def _replace_keeper_if_denser(
        self,
        keeper: PageEntry,
        page: Image.Image,
        source_sha: str,
        record_valid: dict,
        page_features: dict,
        last_by_label: dict[str, PageEntry],
        features_by_seq: dict[int, dict],
    ) -> bool:
        """U8①：相邻帧组内保留信息密度更大的帧——候选严格更密时原位接替。

        原基张折进台账后其身份字段随候选重写（文件名/seq 不变，下游引用与
        已persist的台账在下一次 checkpoint 自然合拢）；标注分类按接收路径
        同一判定对同标签前页重算。密度平局/更稀/页文件不可读一律不替换
        （保首张 fallback，擦板场景天然安全）。返回是否发生替换。
        """
        path = self.work_dir / keeper.file
        try:
            with Image.open(path) as keeper_img:
                keeper_img.load()
                keeper_std = information_density(keeper_img)
        except Exception:
            return False
        if information_density(page) <= keeper_std:
            return False
        keeper.duplicates.append({
            "original_id": str(keeper.original_id or ""),
            "created_sec": int(keeper.created_sec or 0),
        })
        keeper.original_id = str(record_valid.get("original_id") or "")
        keeper.created_sec = int(record_valid.get("created_sec", 0) or 0)
        keeper.created_ms = int(record_valid.get("created_ms", 0) or 0)
        keeper.source_sha256 = source_sha
        keeper.marker_signature = marker_signature(page)
        keeper.page_label = marker_label(str(record_valid.get("ocr_text") or ""))
        tmp_path = path.with_suffix(".img.tmp")
        with tmp_path.open("wb") as handle:
            page.save(handle, format="JPEG", quality=self.budget.jpeg_quality)
        os.replace(tmp_path, path)
        previous = last_by_label.get(keeper.page_label) if keeper.page_label else None
        if (
            previous is not None
            and previous is not keeper
            and previous.page_label == keeper.page_label
        ):
            previous_path = self.work_dir / previous.file
            if previous_path.is_file():
                with Image.open(previous_path) as base_img:
                    base_rgb = base_img.convert("RGB")
                verdict = classify_variant_pair(base_rgb, page)
                keeper.annotation_class = verdict["class"]
                keeper.annotation_confidence = verdict["confidence"]
        if keeper.page_label:
            last_by_label[keeper.page_label] = keeper
        features_by_seq[keeper.seq] = page_features
        return True

    def _screen_device_placeholders(
        self, entries: list[PageEntry], features_by_seq: dict[int, dict],
        skipped: Counter,
    ) -> int:
        """U8② deck pass：设备/系统占位画面（投屏待机/显示器确认框/桌面壁纸）。

        单帧统计无法区分「稀疏标题页」与「待机页」（灰度结构几乎重合），
        判定以本讲保留页中位 std 为锚（冻结阈值与校准记录见
        junk_page_filter.JUNK_PLACEHOLDER_*）；保留页不足
        JUNK_PLACEHOLDER_MIN_DECK 不判。与家族筛查同一记账方式：原地裁剪
        ``entries``、按闭集原因累加 ``skipped``、返回击杀数。
        """
        if len(entries) < JUNK_PLACEHOLDER_MIN_DECK:
            return 0
        resolved: list[tuple[PageEntry, dict]] = []
        for entry in entries:
            features = features_by_seq.get(entry.seq)
            if features is None:
                path = self.work_dir / entry.file
                try:
                    with Image.open(path) as page_img:
                        rgb_page = page_img.convert("RGB")
                        rgb_page.load()
                    features = frame_features(rgb_page)
                except Exception:
                    features = None
            if features is not None:
                resolved.append((entry, features))
        if len(resolved) < JUNK_PLACEHOLDER_MIN_DECK:
            return 0
        median_std = statistics.median(
            float(features["gv"]) ** 0.5 for _entry, features in resolved
        )
        killed = {
            entry.seq for entry, features in resolved
            if placeholder_verdict(features, median_std)
        }
        if not killed:
            return 0
        entries[:] = [entry for entry in entries if entry.seq not in killed]
        for seq in killed:
            features_by_seq.pop(seq, None)
        skipped[DEVICE_PLACEHOLDER_SKIP_REASON] = (
            int(skipped.get(DEVICE_PLACEHOLDER_SKIP_REASON) or 0) + len(killed)
        )
        return len(killed)

    def _screen_junk_families(
        self, entries: list[PageEntry], features_by_seq: dict[int, dict],
        skipped: Counter,
    ) -> int:
        """Deck-level junk pass over the survivors of the per-page screen.

        Survivors sitting within the blacklist near-miss zone are clustered;
        a family of ``JUNK_FAMILY_MIN_MEMBERS``+ perceptual twins is one junk
        page's capture variants and dies together.  The worker's semantic
        stage needs OCR text, so a lone weak-chrome page is never killed
        here.  Entries resumed from disk without a stashed feature dict get
        their features recomputed from the saved page file.  Mutates
        ``entries``/``skipped`` in place and returns the kill count.
        """
        records: list[dict] = []
        flagged: list[PageEntry] = []
        for entry in entries:
            features = features_by_seq.get(entry.seq)
            if features is None:
                path = self.work_dir / entry.file
                try:
                    with Image.open(path) as page_img:
                        rgb_page = page_img.convert("RGB")
                        rgb_page.load()  # force decode before the handle closes
                    features = frame_features(rgb_page)
                except Exception:
                    features = None  # unreadable page stays content, like OCR-side
            if features is None:
                continue
            records.append(features)
            flagged.append(entry)
        if len(flagged) < JUNK_FAMILY_MIN_MEMBERS:
            return 0
        verdicts = adjudicate_deck_local(records)
        killed = {
            entry.seq for entry, kill in zip(flagged, verdicts) if kill
        }
        if not killed:
            return 0
        entries[:] = [entry for entry in entries if entry.seq not in killed]
        for seq in killed:
            features_by_seq.pop(seq, None)
        skipped[JUNK_PAGE_SKIP_REASON] = (
            int(skipped.get(JUNK_PAGE_SKIP_REASON) or 0) + len(killed)
        )
        return len(killed)

    # -- main ----------------------------------------------------------------

    def run(self) -> dict:
        if self.plan is not None:
            return self._run_planned()
        self._started = self._clock()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        ordered = sorted(self.records, key=ppt_record_order)
        events_total = len(ordered)

        resume = self._load_resume()
        cursor = max(0, int(resume.get("cursor") or 0))
        if cursor > len(ordered):
            cursor = 0
            resume = {}
        entries = [PageEntry.from_resume(item) for item in resume.get("entries") or []]
        entries = [entry for entry in entries if (self.work_dir / entry.file).is_file()]
        ledger = {str(item) for item in resume.get("ledger") or []}
        ledger |= {entry.source_sha256 for entry in entries}
        skipped = Counter({str(k): int(v) for k, v in (resume.get("skipped") or {}).items()})
        byte_spent = int(resume.get("byte_spent") or 0)
        resumed = cursor > 0 or bool(entries)
        # 追加C②：相邻帧基线=最近保留页的 dHash；续跑时由最后一条 entry 的
        # 页文件重建（重建失败=无基线，首页照常保留，绝不误杀）。
        last_kept_dhash = self._entry_dhash(entries[-1]) if entries else ""

        budget = self.budget
        seq = (max((entry.seq for entry in entries), default=0) + 1) if entries else 1
        entry_by_sha: dict[str, PageEntry] = {entry.source_sha256: entry for entry in entries}
        # 同一 URL 在本次运行内绝不二次下载（URL 仅存于内存，不入清单）
        entry_by_url: dict[str, PageEntry] = {}
        last_by_label: dict[str, PageEntry] = {
            entry.page_label: entry for entry in entries if entry.page_label
        }
        # per-page junk-screen features, kept for the deck-level family pass
        features_by_seq: dict[int, dict] = {}

        def persist(cursor_now, entries_now, ledger_now, skipped_now, byte_spent_now):
            self._save_resume(
                cursor=cursor_now, entries=entries_now, ledger=ledger_now,
                skipped=skipped_now, byte_spent=byte_spent_now,
            )

        index = cursor
        paused: str | None = None

        def advance(steps: int = 1) -> None:
            nonlocal index
            index += steps
            self._report("pages", index, len(entries), events_total)

        if events_total:
            self._report("pages", index, len(entries), events_total)
        while index < events_total:
            record = ordered[index]
            if self._wall_exceeded():
                paused = "wall_clock_paused"
                self._report("pages", index, len(entries), events_total)
                persist(index, entries, ledger, skipped, byte_spent)
                break
            record_valid, record_reason = validate_record(record)
            if record_reason:
                skipped[record_reason] += 1
                advance()
                continue
            record_url = str(record_valid.get("pptimgurl") or "").strip()
            url_owner = entry_by_url.get(record_url)
            if url_owner is not None:
                # same URL already accepted in this run: never re-download it
                skipped["duplicate_exact"] += 1
                url_owner.duplicates.append({
                    "original_id": str(record_valid.get("original_id") or ""),
                    "created_sec": int(record_valid.get("created_sec", 0) or 0),
                })
                persist(index + 1, entries, ledger, skipped, byte_spent)
                advance()
                continue

            self._rate_limit()
            try:
                raw = self._fetch_page(record_valid) if self._fetch_page else b""
            except Exception:
                raw = b""
                fetch_failed = True
            else:
                fetch_failed = False
            if fetch_failed:
                skipped["fetch_failed"] += 1
                advance()
                continue
            byte_spent += len(raw)
            if byte_spent > budget.max_total_bytes:
                paused = "resource_budget_paused"
                self._report("pages", index, len(entries), events_total)
                persist(index, entries, ledger, skipped, byte_spent)
                break

            source_sha = hashlib.sha256(raw).hexdigest()
            if source_sha in ledger:
                skipped["duplicate_exact"] += 1
                owner = entry_by_sha.get(source_sha)
                if owner is not None:
                    owner.duplicates.append({
                        "original_id": str(record_valid.get("original_id") or ""),
                        "created_sec": int(record_valid.get("created_sec", 0) or 0),
                    })
                    # keep the persisted ledger in step with the folded event
                    persist(index + 1, entries, ledger, skipped, byte_spent)
                advance()
                continue

            page, reason = decode_slide(raw, budget)
            if reason:
                skipped[reason] += 1
                ledger.add(source_sha)  # bad bytes never re-fetched on resume
                advance()
                continue

            # junk screening (M15 blacklist + U4 featureless/chrome), computed
            # once here and reused by the deck family pass; a filter fault
            # never fails the batch — the page simply stays content.
            try:
                page_features = frame_features(page)
                junk = page_is_junk(page, page_features)
            except Exception:
                page_features, junk = None, False
            if junk:
                skipped[JUNK_PAGE_SKIP_REASON] += 1
                ledger.add(source_sha)  # junk never re-fetched on resume
                advance()
                continue

            # 追加C②+U8①：相邻帧去重——未翻页的连续快照（dHash 距离 ≤5）折成
            # 一组，组内按信息密度保留最大张（灰度 std）：板书渐增场景后帧信息
            # 更多，旧「保首张」会丢内容；擦板反向由「仅严格更密才替换」防住。
            # 替换=原基张折进台账、候选原位接替（文件名/seq 不变，身份字段与
            # 标注分类随新页重算），基线随替换更新为候选 dHash。
            # 黑名单近亲页旁路（near ≤ JUNK_BLACKLIST_NEAR_MAX）：它们必须
            # 作为 entry 进入 deck 家族筛查（家族 ≥3 整族杀），若被邻帧去重
            # 折叠会让家族凑不满成员、通知页家族漏杀。
            if (
                page_features is not None
                and last_kept_dhash
                and int(page_features["near"]) > JUNK_BLACKLIST_NEAR_MAX
                and hamming_distance(
                    str(page_features["dhash"]), last_kept_dhash
                )
                <= JUNK_LOCAL_ADJACENT_DHASH_MAX
            ):
                skipped["duplicate_adjacent"] += 1
                if entries:
                    keeper = entries[-1]
                    if self._replace_keeper_if_denser(
                        keeper, page, source_sha, record_valid, page_features,
                        last_by_label, features_by_seq,
                    ):
                        entry_by_sha[source_sha] = keeper
                        last_kept_dhash = str(page_features["dhash"])
                    else:
                        keeper.duplicates.append({
                            "original_id": str(record_valid.get("original_id") or ""),
                            "created_sec": int(record_valid.get("created_sec", 0) or 0),
                        })
                    ledger.add(source_sha)
                    persist(index + 1, entries, ledger, skipped, byte_spent)
                advance()
                continue

            if len(entries) + 1 > budget.distinct_page_storm_limit:
                paused = "event_storm_paused"
                self._report("pages", index, len(entries), events_total)
                persist(index, entries, ledger, skipped, byte_spent)
                break

            file_name = f"page-{seq:05d}.img"
            page_path = self.work_dir / file_name
            tmp_path = page_path.with_suffix(".img.tmp")
            with tmp_path.open("wb") as handle:
                page.save(handle, format="JPEG", quality=budget.jpeg_quality)
            os.replace(tmp_path, page_path)
            byte_spent += page_path.stat().st_size

            entry = PageEntry(
                seq=seq,
                original_id=str(record_valid.get("original_id") or ""),
                created_sec=int(record_valid.get("created_sec", 0) or 0),
                created_ms=int(record_valid.get("created_ms", 0) or 0),
                source_sha256=source_sha,
                file=file_name,
                marker_signature=marker_signature(page),
                page_label=marker_label(str(record_valid.get("ocr_text") or "")),
            )
            previous = last_by_label.get(entry.page_label) if entry.page_label else None
            if previous is not None and previous.page_label == entry.page_label:
                previous_path = self.work_dir / previous.file
                if previous_path.is_file():
                    with Image.open(previous_path) as base_img:
                        base_rgb = base_img.convert("RGB")
                    verdict = classify_variant_pair(base_rgb, page)
                    entry.annotation_class = verdict["class"]
                    entry.annotation_confidence = verdict["confidence"]
            if entry.page_label:
                last_by_label[entry.page_label] = entry

            entries.append(entry)
            ledger.add(source_sha)
            entry_by_sha[source_sha] = entry
            entry_by_url[record_url] = entry
            if page_features is not None:
                features_by_seq[seq] = page_features
                last_kept_dhash = str(page_features["dhash"])
            seq += 1
            advance()
            # compact checkpoint every 25 accepted pages
            if len(entries) % 25 == 0:
                persist(index, entries, ledger, skipped, byte_spent)

        if paused is None:
            self._screen_junk_families(entries, features_by_seq, skipped)
            self._screen_device_placeholders(entries, features_by_seq, skipped)

        kept = len(entries)
        duplicates_total = int(skipped.get("duplicate_exact") or 0) + int(
            skipped.get("duplicate_adjacent") or 0
        )
        outcome = {
            "policy_version": POLICY_VERSION,
            "events_total": events_total,
            "kept": kept,
            "duplicates": duplicates_total,
            "skipped": {name: count for name, count in sorted(skipped.items())},
            "resumed": resumed,
            "state": "completed" if paused is None else "paused",
        }
        if paused is not None:
            outcome["code"] = paused
            outcome["cursor"] = index
            return outcome

        if kept == 0:
            outcome["code"] = "no_pages"
            self._cleanup_owned_files()
            return outcome

        self._report("assemble", events_total, kept, events_total)
        self._assemble(entries)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "policy_version": POLICY_VERSION,
            "generated_at": int(time.time()),
            "events_total": events_total,
            "kept": kept,
            "duplicates": duplicates_total,
            "skipped": {name: count for name, count in sorted(skipped.items())},
            "pdf_file": self.out_pdf.name,
            "pages": [entry.as_manifest() for entry in entries],
        }
        self._publish_atomic(self.out_manifest, json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"))
        self._cleanup_owned_files()
        outcome["pdf"] = str(self.out_pdf)
        outcome["manifest"] = str(self.out_manifest)
        return outcome

    # -- plan-driven run (courseware_plan.v1) ---------------------------------

    def _save_plan_resume(
        self, plan_digest: str, *, entries: list[PageEntry], claimed: set[str],
        missed: set[str], byte_spent: int, cursor: int,
    ) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "policy_version": POLICY_VERSION,
            "mode": "plan",
            "plan_sha256": plan_digest,
            "cursor": int(cursor),
            "byte_spent": int(byte_spent),
            "claimed": sorted(claimed),
            "missed": sorted(missed),
            "entries": [entry.as_resume() for entry in entries],
        }
        tmp = self._resume_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._resume_path)

    def _load_plan_resume(self, plan_digest: str) -> dict:
        try:
            value = json.loads(self._resume_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if (
            not isinstance(value, dict)
            or value.get("policy_version") != POLICY_VERSION
            or value.get("mode") != "plan"
            or value.get("plan_sha256") != plan_digest
        ):
            return {}
        return value

    def _run_planned(self) -> dict:
        """Fetch only plan-selected captures in plan order and publish.

        Every page must be proven against the plan's exact SHA-256 before
        decode. A drifted locator gets one bounded exact-hash reconciliation
        against the current inventory (extra downloads only ever prove an
        expected hash); an expected page that cannot be proven pauses with
        ``courseware_plan_changed`` — never a substituted, omitted or
        reordered page, and never a partial publication.
        """
        plan = validate_courseware_plan(
            self.plan,
            course_id=self.course_id,
            sub_id=self.sub_id,
            plan_digest=self.plan_digest,
        )
        self._started = self._clock()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        budget = self.budget
        entries_out = plan.entries
        total = len(entries_out)
        expected_by_sha = {entry["source_sha256"]: entry for entry in entries_out}

        def rec_id(record: dict) -> str:
            return str(record.get("original_id") or record.get("id") or "")

        def rec_time(record: dict) -> int:
            try:
                return int(record.get("created_sec") or 0)
            except (TypeError, ValueError):
                return -1

        ordered_inv = sorted(
            (record for record in self.records if isinstance(record, dict)),
            key=ppt_record_order,
        )
        record_by_id = {
            rec_id(record): record for record in ordered_inv if rec_id(record)
        }

        resume = self._load_plan_resume(plan.digest)
        entries = [PageEntry.from_resume(item) for item in resume.get("entries") or []]
        entries = [
            entry for entry in entries
            if entry.file
            and (self.work_dir / entry.file).is_file()
            and 1 <= entry.seq <= total
            and entries_out[entry.seq - 1]["source_sha256"] == entry.source_sha256
        ]
        by_position: dict[int, PageEntry] = {entry.seq: entry for entry in entries}
        claimed_ids = {str(item) for item in resume.get("claimed") or []}
        missed_ids = {str(item) for item in resume.get("missed") or []}
        byte_spent = int(resume.get("byte_spent") or 0)
        resumed = bool(by_position) or bool(claimed_ids) or bool(missed_ids)
        reconcile_fetches = 0

        def first_unproven() -> int:
            for seq in range(1, total + 1):
                if seq not in by_position:
                    return seq
            return total + 1

        def persist() -> None:
            self._save_plan_resume(
                plan.digest,
                entries=list(by_position.values()),
                claimed=claimed_ids,
                missed=missed_ids,
                byte_spent=byte_spent,
                cursor=first_unproven() - 1,
            )

        def report(processed: int) -> None:
            self._report("pages", processed, len(by_position), total)

        def accept(page: dict, raw: bytes, record: dict | None) -> str | None:
            """Decode/normalize proven bytes into its plan page; code pauses."""
            nonlocal byte_spent
            if len(by_position) + 1 > budget.distinct_page_storm_limit:
                return "event_storm_paused"
            page_img, reason = decode_slide(raw, budget)
            if reason:
                # The bytes already match the plan's exact hash, so a decode
                # failure is a local limit, never a content skip.
                return "plan_page_unreadable"
            seq = page["output_position"]
            capture_position = page["capture_position"]
            file_name = f"page-{seq:05d}.img"
            page_path = self.work_dir / file_name
            tmp_path = page_path.with_suffix(".img.tmp")
            with tmp_path.open("wb") as handle:
                page_img.save(handle, format="JPEG", quality=budget.jpeg_quality)
            os.replace(tmp_path, page_path)
            byte_spent += page_path.stat().st_size
            by_position[seq] = PageEntry(
                seq=seq,
                original_id=rec_id(record) if record is not None else "",
                created_sec=(
                    int(record.get("created_sec") or 0)
                    if record is not None else page["capture_time"]
                ),
                created_ms=int(record.get("created_ms") or 0) if record is not None else 0,
                source_sha256=page["source_sha256"],
                file=file_name,
                marker_signature=marker_signature(page_img),
                page_label=page["page_label"],
                annotation_class=page["annotation"]["class"],
                annotation_confidence=page["annotation"]["confidence"],
                duplicates=[
                    dict(item)
                    for item in plan.duplicates_by_capture_position.get(
                        capture_position, [],
                    )
                ],
                capture_position=capture_position,
            )
            return None

        def download(record: dict) -> tuple[bytes, str | None]:
            """Rate-limited authorized fetch; a non-None code pauses."""
            self._rate_limit()
            url = str(record.get("pptimgurl") or "").strip()
            if url in self._plan_fetched_urls:
                return b"", None
            try:
                raw = self._fetch_page(record) if self._fetch_page else b""
            except Exception:
                return b"", "plan_fetch_paused"
            self._plan_fetched_urls.add(url)
            return (raw if raw else b""), None

        paused: str | None = None
        report(first_unproven() - 1)
        for page in entries_out:
            seq = page["output_position"]
            index = seq - 1
            if seq in by_position:
                report(seq)
                continue
            if self._wall_exceeded():
                paused = "wall_clock_paused"
                persist()
                break

            # -- fast path: opaque record id + capture-time guard -----------
            candidate = None
            if page["record_id"]:
                located = record_by_id.get(page["record_id"])
                if located is not None and rec_time(located) == page["capture_time"]:
                    candidate = located
            else:
                same_time = [
                    record for record in ordered_inv
                    if rec_time(record) == page["capture_time"]
                ]
                if page["capture_ordinal"] < len(same_time):
                    candidate = same_time[page["capture_ordinal"]]
            candidate_id = rec_id(candidate) if candidate is not None else ""
            if (
                candidate is not None
                and candidate_id not in missed_ids
                and candidate_id not in claimed_ids
            ):
                raw, code = download(candidate)
                if code is None:
                    byte_spent += len(raw)
                    if byte_spent > budget.max_total_bytes:
                        code = "resource_budget_paused"
                    else:
                        digest = hashlib.sha256(raw).hexdigest()
                        target = expected_by_sha.get(digest)
                        if target is not None and target["output_position"] not in by_position:
                            code = accept(target, raw, candidate)
                            if code is None:
                                claimed_ids.add(candidate_id)
                        else:
                            missed_ids.add(candidate_id)
                if code is not None:
                    paused = code
                    persist()
                    break
            if seq in by_position:
                report(seq)
                continue

            # -- one bounded exact-hash reconciliation scan ------------------
            pool = [
                record for record in ordered_inv
                if rec_id(record) not in claimed_ids and rec_id(record) not in missed_ids
            ]
            scan = (
                [record for record in pool if rec_time(record) == page["capture_time"]]
                + [record for record in pool if rec_time(record) != page["capture_time"]]
            )
            stop = False
            for record in scan:
                if seq in by_position:
                    break
                if reconcile_fetches >= budget.plan_reconcile_fetch_cap:
                    break
                raw, code = download(record)
                if code is not None:
                    paused = code
                    stop = True
                    break
                reconcile_fetches += 1
                byte_spent += len(raw)
                if byte_spent > budget.max_total_bytes:
                    paused = "resource_budget_paused"
                    stop = True
                    break
                digest = hashlib.sha256(raw).hexdigest()
                target = expected_by_sha.get(digest)
                if target is not None and target["output_position"] not in by_position:
                    code = accept(target, raw, record)
                    if code is not None:
                        paused = code
                        stop = True
                        break
                    claimed_ids.add(rec_id(record))
                else:
                    missed_ids.add(rec_id(record))
            if stop:
                persist()
                break
            report(seq)

        if paused is None and len(by_position) < total:
            # An expected page the current inventory cannot prove: pause
            # resumably instead of substituting, omitting or reordering.
            paused = "courseware_plan_changed"
            persist()
        if paused is not None:
            return {
                "policy_version": POLICY_VERSION,
                "events_total": plan.counts["input_events"],
                "kept": len(by_position),
                "duplicates": plan.counts["exact_duplicates"],
                "skipped": {},
                "resumed": resumed,
                "state": "paused",
                "code": paused,
                "cursor": first_unproven() - 1,
            }

        ordered_entries = [by_position[seq] for seq in range(1, total + 1)]
        self._report("assemble", total, total, total)
        self._assemble(ordered_entries)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "policy_version": POLICY_VERSION,
            "generated_at": int(time.time()),
            "events_total": plan.counts["input_events"],
            "kept": total,
            "duplicates": plan.counts["exact_duplicates"],
            "skipped": {},
            "pdf_file": self.out_pdf.name,
            "courseware_plan_digest": plan.digest,
            "courseware_plan": {
                "schema": PLAN_SCHEMA,
                "pipeline": plan.pipeline,
                "inventory_digest": plan.inventory_digest,
                "ordering": dict(plan.ordering),
                "counts": dict(plan.counts),
            },
            "pages": [entry.as_manifest() for entry in ordered_entries],
        }
        self._publish_atomic(
            self.out_manifest,
            json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8"),
        )
        self._cleanup_owned_files()
        return {
            "policy_version": POLICY_VERSION,
            "events_total": plan.counts["input_events"],
            "kept": total,
            "duplicates": plan.counts["exact_duplicates"],
            "skipped": {},
            "resumed": resumed,
            "state": "completed",
            "plan_digest": plan.digest,
            "pdf": str(self.out_pdf),
            "manifest": str(self.out_manifest),
        }

    # -- bounded page-wise assembly -------------------------------------------

    def _assemble(self, entries: list[PageEntry]) -> None:
        files = self._page_files(entries)

        def pages():
            for path in files:
                with Image.open(path) as stored:
                    yield stored.convert("RGB")

        generator = pages()
        tmp = self.out_pdf.with_suffix(".pdf.tmp")
        try:
            first = next(generator)
            first.save(
                tmp, format="PDF", save_all=True,
                append_images=generator,
                resolution=150.0,
            )
            os.replace(tmp, self.out_pdf)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            generator.close()

    @staticmethod
    def _publish_atomic(target: Path, payload: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(payload)
        os.replace(tmp, target)
