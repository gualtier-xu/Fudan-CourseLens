#!/usr/bin/env python3
"""Deterministic evidence benchmark scorer for the ``evidence.v1`` metric
catalog (BENCH-1).

Turns an explicit synthetic reference/hypothesis scoring input into a
validated ``evidence.v1`` result document whose measurement rows reuse the
frozen catalog from :mod:`shared.evidence_contract` (``empty_measurement_tables``).
Rows whose metric cannot be computed from the supplied annotations keep
``value: null``; no semantic QA/retrieval/quiz score, price, runtime, or model
quality claim is ever produced.

Computed metric families (13 rows):

- ``asr_text``: ``cer``, ``wer``, ``terminology_accuracy``,
  ``numeric_accuracy``, ``unit_accuracy``, ``negation_accuracy``,
  ``unsupported_addition_rate``
- ``alignment``: ``boundary_mae_ms``, ``boundary_p95_ms``,
  ``speech_coverage``, ``alignment_failure_rate``
- ``presentation``: ``cue_duration_violation_rate``, ``cue_cps_p95``

Text normalization (identical for reference and hypothesis):

- CER text: NFC normalize, Unicode ``casefold``, remove every whitespace
  character.  Character error rate = (substitutions + deletions +
  insertions) / reference character count over unit-cost Levenshtein
  alignments; zero reference characters stay null.
- WER text: NFC normalize, Unicode ``casefold``, split on whitespace runs.
  Word error rate = (substitutions + deletions + insertions) / reference
  whitespace-delimited word count; zero reference words stay null.  CJK runs
  without spaces therefore form single tokens under the frozen definition.
- Occurrence text (terminology / numeric / unit / negation corpora): NFC
  normalize, Unicode ``casefold``, collapse whitespace runs to single spaces;
  the corpus is all segment texts joined with single spaces.  Scope terms and
  units are normalized the same way and counted as substring occurrences; an
  occurrence counts as transcribed when
  ``min(reference_count, hypothesis_count)`` covers it.  Duplicate scope
  entries count independently.
- Numeric items: regex
  ``(?<![0-9A-Za-z])(?:\\d+(?:\\.\\d+)?|\\.\\d+)(?:[eE][+-]?\\d+)?%?(?![0-9A-Za-z])``
  over the occurrence text (integers, decimals, percentages, scientific
  notation; thousands separators are not modeled).
- Negation markers: CJK markers ``不`` / ``无`` / ``未`` counted as substring
  occurrences; latin markers ``not`` / ``without`` counted with the
  word-boundary regex ``\\b(?:not|without)\\b``.  An occurrence counts as
  polarity-preserved via the same ``min(reference, hypothesis)`` rule.

Matching and aggregates:

- Reference/hypothesis segments are matched one-to-one by time overlap,
  considering only segments whose CER-normalized text is non-empty
  (``no_speech`` rows never participate).  Candidates (overlap > 0 ms) are
  sorted by descending overlap, then each side's content key
  ``(start_ms, end_ms, text, lang, no_speech)``; the first free side wins.
  Zero-duration anchors never match; their text is accounted as unmatched.
- Matched pairs feed both the Levenshtein aggregates and the boundary
  metrics; unmatched non-empty reference text counts as deletions, unmatched
  non-empty hypothesis text as insertions.  Per-pair anchor error is
  ``(|start delta| + |end delta|) / 2`` milliseconds; MAE is the mean over
  matched pairs and P95 uses the nearest-rank definition (sorted ascending,
  1-based index ``ceil(0.95 * n)``).
- ``speech_coverage`` = measure(union(voiced reference) ∩ union(non-empty
  hypothesis)) / measure(union(voiced reference)), where voiced means
  ``no_speech`` is not true; zero voiced reference duration stays null.
- ``alignment_failure_rate`` counts hypothesis correction records still in
  state ``needs_alignment`` divided by all non-rejected correction records.
- Cue metrics: ``cue_duration_violation_rate`` needs an explicit
  ``presentation.max_cue_duration_ms`` annotation and counts cues with
  ``end_ms - start_ms`` strictly above it.  ``cue_cps_p95`` counts one unit
  per Han ideograph (U+3400-U+4DBF, U+4E00-U+9FFF) plus one per latin word
  (``[0-9A-Za-z]+(?:[.'-][0-9A-Za-z]+)*``) per cue and divides by the cue
  duration in milliseconds as ``units * 1000 / duration_ms``; a zero-duration
  cue is a closed-set input error.
- Edit-distance backtrace tie-break, when several operations reach the same
  minimal cost, is fixed: diagonal (match/substitution), then deletion, then
  insertion.  ``S + D + I`` always equals the Levenshtein distance.

Input shape (closed key sets, see ``validate_scoring_input``): top-level keys
``scoring`` (must be ``evidence_benchmark_scoring_v1``), optional
``synthetic`` / ``description``, ``source`` (required; exactly ``kind``,
``origin``, ``title``, ``duration_ms``, ``source_sha256`` — the scorer
assigns IDs, inputs never carry ``id``), ``fingerprints`` (optional; exactly
``producer`` / ``model`` / ``config_hash`` / ``media_sha256``), ``scope``
(optional ``terms`` / ``units`` string lists), ``presentation`` (optional
``max_cue_duration_ms``), ``reference.segments`` and
``hypothesis.segments`` (rows with exactly ``start_ms``, ``end_ms``,
``text``, optional ``lang`` / ``no_speech``), ``hypothesis.corrections``
(rows with exactly ``state``, optional ``text`` / ``actor`` / ``reason``) and
``hypothesis.cues`` (rows with exactly ``start_ms``, ``end_ms``, ``lines``,
optional ``lang``).

Output document: ``contract`` + ``source`` + optional ``fingerprints`` +
``speech.segments`` (the reference rows, ID-stamped benchmark evidence) +
``measurements.tables`` (catalog rows with filled values) + additive
``x_benchmark`` provenance (normalization description, sorted hypothesis
annotations, matching summary).  All input strings therefore pass through the
contract's secret scan via ``validate_document``.  Unknown top-level input
keys are rejected so no annotation escapes validation.

CLI: ``python scripts/evidence_benchmark.py INPUT.json [-o OUTPUT.json]``;
canonical JSON goes to stdout, or to the explicitly supplied output path.
Errors print ``error: <code>: <message>`` on stderr and exit 2.

Standard library only; no network, provider, model, media, or real-account
access; no staging or commit.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared import evidence_contract as ec  # noqa: E402

SCORING_CONTRACT = "evidence_benchmark_scoring_v1"

# Closed set of scorer error codes.  Semantic provenance rules (source kind,
# fingerprint shapes, ID grammar, secret-like strings) stay with the contract
# and surface as ``ec.EvidenceContractError`` codes with the same nonzero exit.
ERROR_CODES = (
    "not_json_object",
    "input_contract_missing",
    "input_contract_unsupported",
    "section_unknown",
    "field_required",
    "type_invalid",
    "value_invalid",
    "value_negative",
    "anchor_descending",
    "cue_duration_zero",
    "duplicate_key",
    "io_error",
)

_SEGMENT_KEYS = frozenset({"start_ms", "end_ms", "text", "lang", "no_speech"})
_CORRECTION_KEYS = frozenset({"state", "text", "actor", "reason"})
_CUE_KEYS = frozenset({"start_ms", "end_ms", "lines", "lang"})
_SOURCE_KEYS = frozenset({"kind", "origin", "title", "duration_ms", "source_sha256"})
_FINGERPRINT_KEYS = frozenset({"producer", "model", "config_hash", "media_sha256"})
_TOP_LEVEL_KEYS = frozenset(
    {
        "scoring",
        "synthetic",
        "description",
        "source",
        "fingerprints",
        "scope",
        "presentation",
        "reference",
        "hypothesis",
    }
)
_SCOPE_KEYS = frozenset({"terms", "units"})
_PRESENTATION_KEYS = frozenset({"max_cue_duration_ms"})

_NUMERIC_ITEM_RE = re.compile(
    r"(?<![0-9A-Za-z])(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?%?(?![0-9A-Za-z])"
)
_LATIN_NEGATION_RE = re.compile(r"\b(?:not|without)\b")
_CJK_NEGATION_MARKERS = ("不", "无", "未")
_HAN_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF))
_LATIN_WORD_RE = re.compile(r"[0-9A-Za-z]+(?:[.'-][0-9A-Za-z]+)*")

NORMALIZATION_DESCRIPTION = {
    "cer_text": (
        "NFC normalize, Unicode casefold, remove every whitespace character; "
        "unit-cost Levenshtein over characters"
    ),
    "wer_text": (
        "NFC normalize, Unicode casefold, split on whitespace runs; "
        "unit-cost Levenshtein over tokens"
    ),
    "occurrence_text": (
        "NFC normalize, Unicode casefold, collapse whitespace runs to single "
        "spaces; corpus = segment texts joined with single spaces"
    ),
    "numeric_item_regex": _NUMERIC_ITEM_RE.pattern,
    "latin_negation_regex": _LATIN_NEGATION_RE.pattern,
    "cjk_negation_markers": list(_CJK_NEGATION_MARKERS),
    "cue_units": (
        "cue text = lines joined with single spaces; one unit per Han "
        "ideograph (U+3400-U+4DBF, U+4E00-U+9FFF) plus one per latin word "
        "regex [0-9A-Za-z]+(?:[.'-][0-9A-Za-z]+)*"
    ),
    "matching": (
        "greedy one-to-one time-overlap matching over segments with "
        "non-empty CER-normalized text; candidates sorted by descending "
        "overlap then each side's (start_ms, end_ms, text, lang, no_speech); "
        "zero-duration anchors never match"
    ),
    "boundary_error_ms": "per matched pair (|start delta| + |end delta|) / 2",
    "percentile": "nearest-rank: sorted ascending, 1-based index ceil(0.95*n)",
    "edit_tie_break": "diagonal (match/substitution), then deletion, then insertion",
    "null_policy": (
        "metric families without sufficient input stay null; zero "
        "denominators stay null"
    ),
    "reference_role": (
        "speech.segments carries the reference annotation rows; hypothesis "
        "annotations are recorded under inputs.hypothesis"
    ),
}


class BenchmarkInputError(ValueError):
    """Closed-set scoring-input failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, path: str = "") -> None:
        super().__init__(f"{code}: {message}" + (f" (at {path})" if path else ""))
        self.code = code
        self.message = message
        self.path = path


def _fail(code: str, message: str, path: str = "") -> None:
    raise BenchmarkInputError(code, message, path)


def _check(condition: bool, code: str, message: str, path: str = "") -> None:
    if not condition:
        _fail(code, message, path)


# ---------------------------------------------------------------------------
# Strict JSON parsing (mirrors the contract: duplicate keys and NaN/Infinity
# constants rejected).


def parse_strict_json(text: str):
    """Parse JSON text, rejecting duplicate keys and NaN/Infinity constants."""

    def _reject_constant(name: str):
        _fail("value_invalid", f"{name} is not valid JSON data")

    def _no_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail("duplicate_key", f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            text, parse_constant=_reject_constant, object_pairs_hook=_no_duplicate_keys
        )
    except json.JSONDecodeError as error:
        _fail("value_invalid", f"invalid JSON: {error.msg}")


# ---------------------------------------------------------------------------
# Input validation (structure and closed key sets only; semantic provenance
# rules are delegated to the contract validation of the output document).


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_closed_keys(obj: dict, allowed: frozenset, path: str) -> None:
    for key in obj:
        if key not in allowed:
            _fail("section_unknown", f"unknown key {key!r}", path)


def _require_ms(obj: dict, key: str, path: str) -> int:
    if key not in obj:
        _fail("field_required", f"{key} is required", f"{path}.{key}")
    value = obj[key]
    _check(_is_int(value), "type_invalid", f"{key} must be an integer", f"{path}.{key}")
    _check(value >= 0, "value_negative", f"{key} must be >= 0", f"{path}.{key}")
    return value


def _validate_segment_row(row, path: str) -> dict:
    _check(isinstance(row, dict), "type_invalid", "segment row must be an object", path)
    _require_closed_keys(row, _SEGMENT_KEYS, path)
    start = _require_ms(row, "start_ms", path)
    end = _require_ms(row, "end_ms", path)
    _check(start <= end, "anchor_descending", "start_ms must be <= end_ms", path)
    if "text" not in row:
        _fail("field_required", "text is required", f"{path}.text")
    _check(isinstance(row["text"], str), "type_invalid", "text must be a string", f"{path}.text")
    no_speech = row.get("no_speech", False)
    _check(isinstance(no_speech, bool), "type_invalid", "no_speech must be a boolean", f"{path}.no_speech")
    _check(
        not (no_speech and row["text"] != ""),
        "value_invalid",
        "segment with no_speech=true must have empty text",
        f"{path}.text",
    )
    lang = row.get("lang")
    _check(lang is None or isinstance(lang, str), "type_invalid", "lang must be a string or null", f"{path}.lang")
    return {"start_ms": start, "end_ms": end, "text": row["text"], "lang": lang, "no_speech": no_speech}


def _validate_correction_row(row, path: str) -> dict:
    _check(isinstance(row, dict), "type_invalid", "correction row must be an object", path)
    _require_closed_keys(row, _CORRECTION_KEYS, path)
    if "state" not in row:
        _fail("field_required", "state is required", f"{path}.state")
    state = row["state"]
    _check(
        state in ec.CORRECTION_STATES,
        "value_invalid",
        f"state must be one of {ec.CORRECTION_STATES}",
        f"{path}.state",
    )
    text = row.get("text")
    _check(text is None or isinstance(text, str), "type_invalid", "text must be a string or null", f"{path}.text")
    actor = row.get("actor")
    _check(
        actor is None or actor in ec.CORRECTION_ACTORS,
        "value_invalid",
        f"actor must be one of {ec.CORRECTION_ACTORS} or null",
        f"{path}.actor",
    )
    reason = row.get("reason")
    _check(reason is None or isinstance(reason, str), "type_invalid", "reason must be a string or null", f"{path}.reason")
    return {"state": state, "text": text, "actor": actor, "reason": reason}


def _validate_cue_row(row, path: str) -> dict:
    _check(isinstance(row, dict), "type_invalid", "cue row must be an object", path)
    _require_closed_keys(row, _CUE_KEYS, path)
    start = _require_ms(row, "start_ms", path)
    end = _require_ms(row, "end_ms", path)
    _check(start <= end, "anchor_descending", "start_ms must be <= end_ms", path)
    if "lines" not in row:
        _fail("field_required", "lines is required", f"{path}.lines")
    lines = row["lines"]
    _check(isinstance(lines, list), "type_invalid", "lines must be an array", f"{path}.lines")
    _check(1 <= len(lines) <= 8, "value_invalid", "cue.lines must hold 1-8 lines", f"{path}.lines")
    for index, line in enumerate(lines):
        _check(
            isinstance(line, str) and line != "",
            "value_invalid",
            "cue lines must be non-empty strings",
            f"{path}.lines[{index}]",
        )
    lang = row.get("lang")
    _check(lang is None or isinstance(lang, str), "type_invalid", "lang must be a string or null", f"{path}.lang")
    return {"start_ms": start, "end_ms": end, "lines": list(lines), "lang": lang}


def _validate_string_list(value, path: str) -> list:
    _check(isinstance(value, list), "type_invalid", "expected an array of strings", path)
    for index, item in enumerate(value):
        _check(
            isinstance(item, str) and item != "",
            "value_invalid",
            "list items must be non-empty strings",
            f"{path}[{index}]",
        )
    return list(value)


def validate_scoring_input(document):
    """Validate the scoring input shape and return normalized sections.

    Raises :class:`BenchmarkInputError` with a code from :data:`ERROR_CODES`.
    """
    if not isinstance(document, dict):
        _fail("not_json_object", "scoring input must be a JSON object")
    _require_closed_keys(document, _TOP_LEVEL_KEYS, "$")
    if "scoring" not in document:
        _fail("input_contract_missing", "missing scoring field", "scoring")
    if document["scoring"] != SCORING_CONTRACT:
        _fail(
            "input_contract_unsupported",
            f"unsupported scoring contract {document['scoring']!r}; expected {SCORING_CONTRACT!r}",
            "scoring",
        )
    synthetic = document.get("synthetic", False)
    _check(isinstance(synthetic, bool), "type_invalid", "synthetic must be a boolean", "synthetic")
    description = document.get("description")
    _check(
        description is None or isinstance(description, str),
        "type_invalid",
        "description must be a string",
        "description",
    )
    if "source" not in document:
        _fail("field_required", "source is required", "source")
    _check(isinstance(document["source"], dict), "type_invalid", "source must be an object", "source")
    _require_closed_keys(document["source"], _SOURCE_KEYS, "source")
    for key in ("kind", "origin", "title", "source_sha256"):
        if key in document["source"]:
            _check(
                document["source"][key] is None or isinstance(document["source"][key], str),
                "type_invalid",
                f"source.{key} must be a string or null",
                f"source.{key}",
            )
    if "duration_ms" in document["source"]:
        duration = document["source"]["duration_ms"]
        _check(
            duration is None or _is_int(duration),
            "type_invalid",
            "source.duration_ms must be an integer or null",
            "source.duration_ms",
        )
        _check(
            duration is None or duration >= 0,
            "value_negative",
            "source.duration_ms must be >= 0",
            "source.duration_ms",
        )
    fingerprints = document.get("fingerprints")
    _check(
        fingerprints is None or isinstance(fingerprints, dict),
        "type_invalid",
        "fingerprints must be an object",
        "fingerprints",
    )
    if fingerprints is not None:
        _require_closed_keys(fingerprints, _FINGERPRINT_KEYS, "fingerprints")
        for key in ("producer", "model", "config_hash", "media_sha256"):
            if key in fingerprints:
                _check(
                    fingerprints[key] is None or isinstance(fingerprints[key], str),
                    "type_invalid",
                    f"fingerprints.{key} must be a string or null",
                    f"fingerprints.{key}",
                )

    scope = document.get("scope", {})
    _check(isinstance(scope, dict), "type_invalid", "scope must be an object", "scope")
    _require_closed_keys(scope, _SCOPE_KEYS, "scope")
    scope_out = {}
    if "terms" in scope:
        scope_out["terms"] = _validate_string_list(scope["terms"], "scope.terms")
    if "units" in scope:
        scope_out["units"] = _validate_string_list(scope["units"], "scope.units")

    presentation = document.get("presentation", {})
    _check(isinstance(presentation, dict), "type_invalid", "presentation must be an object", "presentation")
    _require_closed_keys(presentation, _PRESENTATION_KEYS, "presentation")
    presentation_out = {}
    if "max_cue_duration_ms" in presentation:
        limit = presentation["max_cue_duration_ms"]
        _check(
            _is_int(limit),
            "type_invalid",
            "max_cue_duration_ms must be an integer",
            "presentation.max_cue_duration_ms",
        )
        _check(
            limit >= 0,
            "value_negative",
            "max_cue_duration_ms must be >= 0",
            "presentation.max_cue_duration_ms",
        )
        presentation_out["max_cue_duration_ms"] = limit

    if "reference" not in document:
        _fail("field_required", "reference is required", "reference")
    reference = document["reference"]
    _check(isinstance(reference, dict), "type_invalid", "reference must be an object", "reference")
    _require_closed_keys(reference, frozenset({"segments"}), "reference")
    if "segments" not in reference:
        _fail("field_required", "segments is required", "reference.segments")
    _check(
        isinstance(reference["segments"], list),
        "type_invalid",
        "reference.segments must be an array",
        "reference.segments",
    )
    reference_segments = [
        _validate_segment_row(row, f"reference.segments[{index}]")
        for index, row in enumerate(reference["segments"])
    ]

    if "hypothesis" not in document:
        _fail("field_required", "hypothesis is required", "hypothesis")
    hypothesis = document["hypothesis"]
    _check(isinstance(hypothesis, dict), "type_invalid", "hypothesis must be an object", "hypothesis")
    _require_closed_keys(
        hypothesis, frozenset({"segments", "corrections", "cues"}), "hypothesis"
    )
    if "segments" not in hypothesis:
        _fail("field_required", "segments is required", "hypothesis.segments")
    _check(
        isinstance(hypothesis["segments"], list),
        "type_invalid",
        "hypothesis.segments must be an array",
        "hypothesis.segments",
    )
    hypothesis_segments = [
        _validate_segment_row(row, f"hypothesis.segments[{index}]")
        for index, row in enumerate(hypothesis["segments"])
    ]
    corrections = hypothesis.get("corrections", [])
    _check(
        isinstance(corrections, list),
        "type_invalid",
        "hypothesis.corrections must be an array",
        "hypothesis.corrections",
    )
    correction_rows = [
        _validate_correction_row(row, f"hypothesis.corrections[{index}]")
        for index, row in enumerate(corrections)
    ]
    cues = hypothesis.get("cues", [])
    _check(
        isinstance(cues, list),
        "type_invalid",
        "hypothesis.cues must be an array",
        "hypothesis.cues",
    )
    cue_rows = [_validate_cue_row(row, f"hypothesis.cues[{index}]") for index, row in enumerate(cues)]

    return {
        "synthetic": synthetic,
        "description": description,
        "source": document["source"],
        "fingerprints": fingerprints,
        "scope": scope_out,
        "presentation": presentation_out,
        "reference_segments": reference_segments,
        "hypothesis_segments": hypothesis_segments,
        "corrections": correction_rows,
        "cues": cue_rows,
    }


# ---------------------------------------------------------------------------
# Normalization and primitive metric helpers (pure functions).


def normalize_cer_text(text: str) -> str:
    """NFC, casefold, remove every whitespace character."""
    return "".join(
        char for char in unicodedata.normalize("NFC", text).casefold() if not char.isspace()
    )


def normalize_word_tokens(text: str) -> list:
    """NFC, casefold, split on whitespace runs; CJK runs become single tokens."""
    return unicodedata.normalize("NFC", text).casefold().split()


def normalize_occurrence_text(text: str) -> str:
    """NFC, casefold, collapse whitespace runs to single spaces."""
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def segment_content_key(row: dict) -> tuple:
    """Total, order-independent content key for a segment row."""
    return (row["start_ms"], row["end_ms"], row["text"], row.get("lang") or "", row.get("no_speech", False))


def edit_operations(reference: str, hypothesis: str) -> tuple:
    """Unit-cost Levenshtein op counts ``(substitutions, deletions, insertions)``.

    The backtrace tie-break is fixed (diagonal, then deletion, then
    insertion) so the split is deterministic; the sum always equals the
    Levenshtein distance.  Accepts any indexable sequences of hashables.
    """
    n, m = len(reference), len(hypothesis)
    matrix = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        matrix[i][0] = i
    for j in range(m + 1):
        matrix[0][j] = j
    for i in range(1, n + 1):
        left_value = reference[i - 1]
        row = matrix[i]
        previous = matrix[i - 1]
        for j in range(1, m + 1):
            cost = 0 if left_value == hypothesis[j - 1] else 1
            row[j] = min(previous[j - 1] + cost, previous[j] + 1, row[j - 1] + 1)
    i, j = n, m
    substitutions = deletions = insertions = 0
    while i > 0 or j > 0:
        if (
            i > 0
            and j > 0
            and matrix[i][j] == matrix[i - 1][j - 1] + (0 if reference[i - 1] == hypothesis[j - 1] else 1)
        ):
            if reference[i - 1] != hypothesis[j - 1]:
                substitutions += 1
            i -= 1
            j -= 1
        elif i > 0 and matrix[i][j] == matrix[i - 1][j] + 1:
            deletions += 1
            i -= 1
        else:
            insertions += 1
            j -= 1
    return substitutions, deletions, insertions


def match_segments(reference_segments: list, hypothesis_segments: list) -> tuple:
    """Deterministic greedy one-to-one time-overlap matching.

    Only rows with non-empty CER-normalized text participate.  Returns
    ``(pairs, unmatched_reference, unmatched_hypothesis)`` where pairs are
    ``(reference_row, hypothesis_row, overlap_ms)`` sorted by the reference
    content key, and the unmatched lists are content-key sorted rows.
    """
    candidates = []
    for reference_row in reference_segments:
        if not normalize_cer_text(reference_row["text"]):
            continue
        for hypothesis_row in hypothesis_segments:
            if not normalize_cer_text(hypothesis_row["text"]):
                continue
            overlap = min(reference_row["end_ms"], hypothesis_row["end_ms"]) - max(
                reference_row["start_ms"], hypothesis_row["start_ms"]
            )
            if overlap > 0:
                candidates.append((-overlap, segment_content_key(reference_row), segment_content_key(hypothesis_row), reference_row, hypothesis_row))
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    used_reference = set()
    used_hypothesis = set()
    pairs = []
    for neg_overlap, _, _, reference_row, hypothesis_row in candidates:
        reference_key = segment_content_key(reference_row)
        hypothesis_key = segment_content_key(hypothesis_row)
        if reference_key in used_reference or hypothesis_key in used_hypothesis:
            continue
        used_reference.add(reference_key)
        used_hypothesis.add(hypothesis_key)
        pairs.append((reference_row, hypothesis_row, -neg_overlap))
    pairs.sort(key=lambda pair: segment_content_key(pair[0]))
    unmatched_reference = sorted(
        (row for row in reference_segments if normalize_cer_text(row["text"]) and segment_content_key(row) not in used_reference),
        key=segment_content_key,
    )
    unmatched_hypothesis = sorted(
        (row for row in hypothesis_segments if normalize_cer_text(row["text"]) and segment_content_key(row) not in used_hypothesis),
        key=segment_content_key,
    )
    return pairs, unmatched_reference, unmatched_hypothesis


def percentile_nearest_rank(sorted_values: list, quantile: float):
    """Nearest-rank percentile over an ascending-sorted list; None when empty."""
    if not sorted_values:
        return None
    rank = math.ceil(quantile * len(sorted_values))
    return sorted_values[rank - 1]


def _merged_intervals(intervals: list) -> list:
    merged = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def interval_union_measure(intervals: list) -> int:
    """Total covered milliseconds of the union of ``[start, end)`` intervals."""
    return sum(end - start for start, end in _merged_intervals(intervals))


def interval_intersection_measure(first: list, second: list) -> int:
    """Total covered milliseconds of the intersection of two interval unions."""
    left = _merged_intervals(first)
    right = _merged_intervals(second)
    i = j = 0
    total = 0
    while i < len(left) and j < len(right):
        start = max(left[i][0], right[j][0])
        end = min(left[i][1], right[j][1])
        if end > start:
            total += end - start
        if left[i][1] <= right[j][1]:
            i += 1
        else:
            j += 1
    return total


def occurrence_accuracy(reference_corpus: str, hypothesis_corpus: str, items: list):
    """``min(ref, hyp)`` occurrence accuracy over normalized corpus strings.

    ``items`` are raw scope strings; both corpora and items are normalized
    with :func:`normalize_occurrence_text`.  Returns ``None`` when the
    reference occurrence total is zero.
    """
    total = 0
    correct = 0
    hypothesis_counts = {}
    for item in items:
        item_normalized = normalize_occurrence_text(item)
        hypothesis_counts[item_normalized] = hypothesis_corpus.count(item_normalized)
    for item in items:
        item_normalized = normalize_occurrence_text(item)
        reference_count = reference_corpus.count(item_normalized)
        total += reference_count
        correct += min(reference_count, hypothesis_counts[item_normalized])
    if total == 0:
        return None
    return correct / total


def extract_numeric_items(occurrence_text: str) -> list:
    """Numeric items (integers, decimals, percentages, scientific notation)."""
    return _NUMERIC_ITEM_RE.findall(occurrence_text)


def numeric_accuracy(reference_corpus: str, hypothesis_corpus: str):
    """Share of reference numeric items transcribed exactly; None when none."""
    reference_items = extract_numeric_items(reference_corpus)
    if not reference_items:
        return None
    hypothesis_counts = {}
    for item in extract_numeric_items(hypothesis_corpus):
        hypothesis_counts[item] = hypothesis_counts.get(item, 0) + 1
    correct = 0
    for item in reference_items:
        correct += min(1, hypothesis_counts.get(item, 0))
    return correct / len(reference_items)


def negation_accuracy(reference_corpus: str, hypothesis_corpus: str):
    """Share of reference negation-marker occurrences preserved; None when none."""
    total = 0
    correct = 0
    for marker in _CJK_NEGATION_MARKERS:
        reference_count = reference_corpus.count(marker)
        total += reference_count
        correct += min(reference_count, hypothesis_corpus.count(marker))
    for pattern in (_LATIN_NEGATION_RE,):
        reference_count = len(pattern.findall(reference_corpus))
        total += reference_count
        correct += min(reference_count, len(pattern.findall(hypothesis_corpus)))
    if total == 0:
        return None
    return correct / total


def cue_units(lines: list) -> int:
    """Han ideographs plus latin words across the space-joined cue lines."""
    text = " ".join(lines)
    units = 0
    for char in text:
        codepoint = ord(char)
        if any(low <= codepoint <= high for low, high in _HAN_RANGES):
            units += 1
    units += len(_LATIN_WORD_RE.findall(text))
    return units


# ---------------------------------------------------------------------------
# Scoring.


def _corpus(rows: list) -> str:
    return normalize_occurrence_text(" ".join(row["text"] for row in rows))


def _text_metric_values(valid: dict) -> dict:
    """Compute the 13 catalog values; ``None`` marks insufficient input."""
    reference_segments = valid["reference_segments"]
    hypothesis_segments = valid["hypothesis_segments"]
    pairs, unmatched_reference, unmatched_hypothesis = match_segments(
        reference_segments, hypothesis_segments
    )

    reference_lengths = [len(normalize_cer_text(row["text"])) for row in reference_segments]
    hypothesis_lengths = [len(normalize_cer_text(row["text"])) for row in hypothesis_segments]
    reference_chars = sum(reference_lengths)
    hypothesis_chars = sum(hypothesis_lengths)

    substitutions = deletions = insertions = 0
    word_substitutions = word_deletions = word_insertions = 0
    boundary_errors = []
    for reference_row, hypothesis_row, _ in pairs:
        sub, dele, ins = edit_operations(
            normalize_cer_text(reference_row["text"]), normalize_cer_text(hypothesis_row["text"])
        )
        substitutions += sub
        deletions += dele
        insertions += ins
        w_sub, w_del, w_ins = edit_operations(
            normalize_word_tokens(reference_row["text"]), normalize_word_tokens(hypothesis_row["text"])
        )
        word_substitutions += w_sub
        word_deletions += w_del
        word_insertions += w_ins
        boundary_errors.append(
            (abs(hypothesis_row["start_ms"] - reference_row["start_ms"])
             + abs(hypothesis_row["end_ms"] - reference_row["end_ms"])) / 2
        )
    for row in unmatched_reference:
        deletions += len(normalize_cer_text(row["text"]))
        word_deletions += len(normalize_word_tokens(row["text"]))
    for row in unmatched_hypothesis:
        insertions += len(normalize_cer_text(row["text"]))
        word_insertions += len(normalize_word_tokens(row["text"]))

    reference_words = sum(len(normalize_word_tokens(row["text"])) for row in reference_segments)

    reference_corpus = _corpus(reference_segments)
    hypothesis_corpus = _corpus(hypothesis_segments)

    cer = substitutions + deletions + insertions
    wer = word_substitutions + word_deletions + word_insertions

    boundary_sorted = sorted(boundary_errors)
    matched_pair_count = len(boundary_sorted)

    voiced_reference_intervals = [
        (row["start_ms"], row["end_ms"]) for row in reference_segments if not row["no_speech"]
    ]
    nonempty_hypothesis_intervals = [
        (row["start_ms"], row["end_ms"])
        for row in hypothesis_segments
        if normalize_cer_text(row["text"])
    ]
    voiced_total = interval_union_measure(voiced_reference_intervals)
    covered = interval_intersection_measure(voiced_reference_intervals, nonempty_hypothesis_intervals)

    corrections = valid["corrections"]
    non_rejected = [row for row in corrections if row["state"] != "rejected"]
    still_needs_alignment = [row for row in non_rejected if row["state"] == "needs_alignment"]

    cues = valid["cues"]
    max_cue_duration = valid["presentation"].get("max_cue_duration_ms")
    duration_violations = None
    if cues and max_cue_duration is not None:
        duration_violations = sum(
            1 for row in cues if row["end_ms"] - row["start_ms"] > max_cue_duration
        )
    cps_values = []
    for row in cues:
        duration = row["end_ms"] - row["start_ms"]
        if duration == 0:
            _fail(
                "cue_duration_zero",
                "cue_cps_p95 is undefined for zero-duration cues",
                "hypothesis.cues",
            )
        cps_values.append(cue_units(row["lines"]) * 1000 / duration)

    values = {
        ("asr_text", "cer"): cer / reference_chars if reference_chars > 0 else None,
        ("asr_text", "wer"): wer / reference_words if reference_words > 0 else None,
        ("asr_text", "terminology_accuracy"): occurrence_accuracy(
            reference_corpus, hypothesis_corpus, valid["scope"].get("terms", [])
        ),
        ("asr_text", "numeric_accuracy"): numeric_accuracy(reference_corpus, hypothesis_corpus),
        ("asr_text", "unit_accuracy"): occurrence_accuracy(
            reference_corpus, hypothesis_corpus, valid["scope"].get("units", [])
        ),
        ("asr_text", "negation_accuracy"): negation_accuracy(reference_corpus, hypothesis_corpus),
        ("asr_text", "unsupported_addition_rate"): (
            insertions / hypothesis_chars if hypothesis_chars > 0 else None
        ),
        ("alignment", "boundary_mae_ms"): (
            sum(boundary_sorted) / matched_pair_count if matched_pair_count > 0 else None
        ),
        ("alignment", "boundary_p95_ms"): percentile_nearest_rank(boundary_sorted, 0.95),
        ("alignment", "speech_coverage"): covered / voiced_total if voiced_total > 0 else None,
        ("alignment", "alignment_failure_rate"): (
            len(still_needs_alignment) / len(non_rejected) if non_rejected else None
        ),
        ("presentation", "cue_duration_violation_rate"): (
            duration_violations / len(cues) if duration_violations is not None and cues else None
        ),
        ("presentation", "cue_cps_p95"): percentile_nearest_rank(sorted(cps_values), 0.95),
    }
    return values, pairs, unmatched_reference, unmatched_hypothesis


def score_benchmark(document) -> dict:
    """Score a parsed scoring-input object into a validated result document."""
    valid = validate_scoring_input(document)
    values, pairs, unmatched_reference, unmatched_hypothesis = _text_metric_values(valid)

    tables = ec.empty_measurement_tables()
    for row in tables:
        row["value"] = values.get((row["family"], row["metric"]))

    skeleton = {
        "contract": ec.CONTRACT_ID,
        "source": dict(valid["source"]),
        "speech": {"segments": [dict(row) for row in valid["reference_segments"]]},
        "measurements": {"tables": tables},
    }
    if valid["fingerprints"] is not None:
        skeleton["fingerprints"] = dict(valid["fingerprints"])
    stamped = ec.assign_ids(skeleton)

    segments_by_key = {
        segment_content_key(row): row["id"] for row in stamped["speech"]["segments"]
    }
    matched_records = [
        {
            "reference_id": segments_by_key[segment_content_key(reference_row)],
            "hypothesis": {
                "start_ms": hypothesis_row["start_ms"],
                "end_ms": hypothesis_row["end_ms"],
                "text": hypothesis_row["text"],
            },
            "overlap_ms": overlap,
        }
        for reference_row, hypothesis_row, overlap in pairs
    ]
    x_benchmark = {
        "scoring": SCORING_CONTRACT,
        "synthetic": valid["synthetic"],
        "normalization": NORMALIZATION_DESCRIPTION,
        "inputs": {
            "scope": {
                "terms": sorted(valid["scope"].get("terms", [])),
                "units": sorted(valid["scope"].get("units", [])),
            },
            "presentation": dict(valid["presentation"]),
            "hypothesis": {
                "segments": sorted(
                    (dict(row) for row in valid["hypothesis_segments"]),
                    key=segment_content_key,
                ),
                "corrections": sorted(
                    (dict(row) for row in valid["corrections"]),
                    key=lambda row: (row["state"], row["text"] or "", row["actor"] or "", row["reason"] or ""),
                ),
                "cues": sorted(
                    (dict(row) for row in valid["cues"]),
                    key=lambda row: (row["start_ms"], row["end_ms"], row["lines"]),
                ),
            },
        },
        "matching": {
            "matched_pairs": matched_records,
            "unmatched_reference_ids": [
                segments_by_key[segment_content_key(row)] for row in unmatched_reference
            ],
            "unmatched_hypothesis": [
                {"start_ms": row["start_ms"], "end_ms": row["end_ms"], "text": row["text"]}
                for row in unmatched_hypothesis
            ],
        },
    }
    if valid["description"] is not None:
        x_benchmark["description"] = valid["description"]
    stamped["x_benchmark"] = x_benchmark
    return ec.validate_document(stamped)


# ---------------------------------------------------------------------------
# CLI.


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic evidence benchmark scorer (evidence.v1 measurement catalog)."
    )
    parser.add_argument("input", help="path to the scoring input JSON")
    parser.add_argument("-o", "--output", help="explicit output path (default: stdout)")
    args = parser.parse_args(argv)

    def _report(error) -> None:
        location = f" (at {error.path})" if getattr(error, "path", "") else ""
        print(f"error: {error.code}: {error.message}{location}", file=sys.stderr)

    try:
        try:
            text = Path(args.input).read_text(encoding="utf-8")
        except OSError as error:
            _fail("io_error", f"cannot read input: {error.strerror or error}")
        document = parse_strict_json(text)
        result = score_benchmark(document)
    except (BenchmarkInputError, ec.EvidenceContractError) as error:
        _report(error)
        return 2

    payload = ec.canonical_json(result) + "\n"
    if args.output:
        try:
            Path(args.output).write_text(payload, encoding="utf-8", newline="\n")
        except OSError as error:
            print(f"error: io_error: cannot write output: {error.strerror or error}", file=sys.stderr)
            return 2
    else:
        sys.stdout.buffer.write(payload.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
