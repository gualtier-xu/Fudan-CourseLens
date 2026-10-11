# Evidence Contract v1 (`evidence.v1`)

Status: **frozen for the subtitle/AI foundation batch** (2026-09-12).
Module: `shared/evidence_contract.py` (Python standard library only).
Fixture: `tests/fixtures/evidence_benchmark_v1.json`.
Accepted input: `product-subtitle-ai-u0-synthesis-result-20260912.md` in the
append-only result ledger.

This contract is a **domain data contract**, not a wire protocol change. It
does not modify `shared/protocol/*` (`job.v2`, `result.v2`, `control.v2`,
`sealed.v2`), SRT/VTT exports, `summary`, `chapters`, `ppt_pages`, or any
client/worker/UI behavior. Consumer packages add fields that *reference*
these objects; they must keep the existing exports intact during migration.

## 1. Envelope

A document is a JSON object:

```json
{
  "contract": "evidence.v1",
  "source": { "..." : "SourceIdentity (required)" },
  "fingerprints": { "..." : "ProducerFingerprints (optional)" },
  "speech": { "segments": [ "SpeechSegment, optional" ] },
  "corrections": [ "Correction, optional" ],
  "cues": [ "DisplayCue, optional" ],
  "slides": { "entities": [ "SlideEntity" ], "events": [ "SlideEvent" ] },
  "units": [ "EvidenceUnit, optional" ],
  "checkpoints": [ "Checkpoint, optional" ],
  "measurements": { "tables": [ "MeasurementRow, optional" ] }
}
```

Missing sections normalize to empty (`[]` / `{}`); unknown top-level and
per-object keys are **additive and preserved** through validation and
round-trip, but their string values are scanned for credential-shaped
content like every other string. The `contract` field itself is closed:
only `evidence.v1` validates.

## 2. Immutable versus regenerable

| Object | Class | Authoritative? | Regenerable? |
|---|---|---|---|
| `source` (SourceIdentity) | immutable provenance | yes | no |
| `speech.segments` (raw ASR hypothesis) | immutable evidence | yes | no |
| segment `tokens` / `confidence` / `source_hash` | alignment points inside the immutable segment envelope | no (excluded from segment identity) | yes |
| `corrections` (records of edit proposals) | immutable audit records | yes as records | no |
| `cues` (display cues) | derived view | **never** | yes |
| `slides.entities` (SlideEntity) | immutable identity | yes | no |
| `slides.events` (SlideEvent) | immutable occurrence | yes | no |
| `units` (EvidenceUnit of every kind) | interpretation / view | **never** | yes |
| `checkpoints` | immutable pipeline progress records | yes as records | no |
| `measurements.tables[].value` | benchmark result slot | no | yes |

**Notes, answers, and quizzes are views over evidence, not evidence.** They
must cite at least one evidence span (`unit.spans`) and are regenerated
whenever their inputs change. Display cues must declare `derived_from`
pointing at a segment or a non-rejected correction.

## 3. Stable ID grammar

Every object carries `id` matching:

```
<namespace>:<12 lowercase hex>
namespace ∈ {src, seg, cor, cue, slent, slevt, unit, chk, mtr}
id = namespace + ":" + sha256(canonical_json(identity_fields))[:12]
```

`canonical_json` is `json.dumps(..., ensure_ascii=False, sort_keys=True,
separators=(",", ":"), allow_nan=False)` over the identity-field dict.
Validation recomputes and verifies every ID (`id_mismatch`) and rejects
duplicates inside a namespace (`duplicate_id`).

Identity fields per type:

| Type | Identity fields | Excluded (regenerable/additive) |
|---|---|---|
| SourceIdentity | `kind, origin, title, duration_ms, source_sha256` | — |
| SpeechSegment | `source_id, start_ms, end_ms, text, lang, no_speech, producer, model, config_hash` | `tokens, confidence, source_hash`, any additive keys |
| Correction | `target, state, text, start_ms, end_ms, actor, reason` | additive keys |
| DisplayCue | `start_ms, end_ms, lines, derived_from, lang` | additive keys |
| SlideEntity | `source_id, deck_id, page, content_sha256` | `title, region`, additive keys |
| SlideEvent | `entity, start_ms, end_ms` | additive keys |
| EvidenceUnit | `kind, title, time, spans, content` | additive keys |
| Checkpoint | `source_id, stage, seq, status, position_ms, payload_sha256, detail` | additive keys |
| MeasurementRow | `family, metric, unit, direction, definition, dims, scope` | `value` |

Consequences:

- IDs derive from **source identity, temporal/page anchors, and the
  producer/model/config fingerprint** — never from display-array position
  alone. Array order is canonicalized during normalization, so any input
  permutation validates to the identical document.
- Re-generating evidence from the same source with the same fingerprint
  reproduces the same IDs (idempotent re-runs); a different model/config
  yields new segment IDs while the old document remains valid.
- Filling a measurement `value` never changes the row ID, so benchmark
  result tables stay addressable across runs.
- Unit `content` is part of identity: regeneration with the same inputs is
  deterministic, and changed content is a new unit, not a mutation.

## 4. Absolute provenance

- **Time**: all `*_ms` fields are integer milliseconds from source start.
  There are no floating-point seconds in this contract. (The known K4
  `start_ms`/`start_seconds` chapter-consumer mismatch is explicitly **out
  of scope** here and remains a separate candidate.)
- Anchors must be non-negative (`anchor_negative`), `start_ms <= end_ms`
  (`anchor_descending`), and within `source.duration_ms` when the duration
  is known (`anchor_out_of_range`). A segment ending exactly at the
  duration (e.g. the 600-second boundary) is valid.
- **Overlaps are allowed** between segments, cues, and slide events
  (diarization overlap, slide re-shows); normalization sorts arrays
  canonically instead of clamping.
- Token time points are absolute `[text, start_ms, end_ms|null]` triples
  inside their segment's anchors and must be non-descending. `end_ms: null`
  marks a point without a measured end.
- **Pages** are 1-based integers. **Regions** are source-pixel rectangles
  `{x, y, w, h}` with `x, y >= 0`, `w, h >= 1`.
- `source.origin == "external_import"` **requires** `source_sha256`
  (imports must not discard source provenance — U0 finding). A per-segment
  `source_hash`, when present, must equal the document-level
  `source.source_sha256`.

## 5. Corrections

A Correction is an immutable record about a proposed edit to one segment;
the segment itself is never mutated.

| State | Meaning | Rules |
|---|---|---|
| `timing_preserved` | text corrected, anchors kept | explicit anchors, if present, must equal the target's (`state_conflict`); text must differ from the target (`empty_correction`) |
| `needs_alignment` | "alignment required": anchors moved or text replaced such that token time points are stale | must actually change anchors or text (`empty_correction`); downstream consumers must re-derive token/point timing before display |
| `rejected` | proposal refused; audit only | original evidence untouched; **cues must not derive from it and units must not cite it** (`invalid_derivation`) |

`actor` is one of `user`, `llm`, `rule`, or null. `reason` is free text.

## 6. SlideEntity versus SlideEvent

- **SlideEntity** = stable slide identity: `deck_id`, 1-based `page`,
  optional `content_sha256`, optional `title`, optional `region`.
- **SlideEvent** = one occurrence of an entity on the timeline:
  `entity` reference, absolute `start_ms`, `end_ms` (null = still shown at
  capture end).

The same slide shown twice is **one entity with two events** with distinct
IDs; dHash-style deduplication that collapses repeat showings loses the
events (U0 finding) and must not be replicated in this model.

## 7. EvidenceUnit

`kind` ∈ `section`, `knowledge_unit`, `key_moment`, `note`, `qa`,
`quiz_item`. Every unit carries a non-empty `spans` array of references
`{"kind": "segment"|"correction"|"slide_event", "id": ...}` resolving inside
the same document (rejected corrections cannot be cited), an optional
absolute `time` range, and a regenerable `content` object (shape is
kind-specific and intentionally open in v1). Sections/knowledge units/key
moments are also interpretations — the authoritative layer is always the
underlying segments, corrections, and slide events.

## 8. Fingerprints and secret hygiene

`fingerprints` records producer/model/config identity **without secrets**:
`producer` (required when the block is non-empty), `model`, `config_hash`
(12–64 lowercase hex), `media_sha256` (64-hex). Config hashes are computed
over normalized config, never raw environments.

Validation rejects, anywhere in the document:

- credential-shaped strings (`sk-…`, `AKIA…`, `ghp_…`, `xox…-`,
  `Bearer …`, PEM private-key blocks, `api_key:/secret:/password:…`
  assignments) → `secret_like`;
- field names that look like credential stores (`secret`, `password`,
  `api_key`, `credential`, …) → `secret_like_key`;
- non-finite numbers (NaN/Infinity are not valid JSON data).

Strings that are exactly a contract ID or a 64-hex digest are exempt from
the value scan because hashes are legitimate provenance.

## 9. Checkpoints

Checkpoints record pipeline progress with interruption metadata: `stage`,
`seq`, `status` (`complete` | `interrupted`), absolute `position_ms`
(optional), `payload_sha256` of the serialized checkpoint payload
(optional), and `detail`. This is the contract-level shape; the wire-level
checkpoint envelope (hash-bound pass-through) is unchanged. The benchmark
pairs this with `checkpoint_size_bytes` and `recovery_time_s` measurements.

## 10. Measurement tables (empty definitions)

`measurements.tables` rows are `{"id", family, metric, unit, direction,
definition, value, dims, scope}` where `(family, metric)` must exist in the
module catalog (`metric_unknown`), `unit`/`direction`/`definition` must
match the catalog entry, and `value` is `null` until a benchmark run fills
it. **No values or thresholds are fabricated in this package**; thresholds
are preregistered later from baseline variance and product risk.

Catalog (30 rows, `value: null`):

| Family | Metric | Unit | Direction | Measures |
|---|---|---|---|---|
| asr_text | `cer` | ratio | lower_is_better | character error rate (S+D+I)/N, null when N=0 |
| asr_text | `wer` | ratio | lower_is_better | word error rate (S+D+I)/N, null when N=0 |
| asr_text | `terminology_accuracy` | ratio | higher_is_better | exact surface match of scope term-list occurrences |
| asr_text | `numeric_accuracy` | ratio | higher_is_better | integers/decimals/percentages/scientific notation exact |
| asr_text | `unit_accuracy` | ratio | higher_is_better | SI/currency/percentage unit items exact |
| asr_text | `negation_accuracy` | ratio | higher_is_better | polarity of 不/无/未/not/without scopes preserved |
| asr_text | `unsupported_addition_rate` | ratio | lower_is_better | hypothesis content without reference support |
| alignment | `boundary_mae_ms` | ms | lower_is_better | mean absolute anchor error over matched spans |
| alignment | `boundary_p95_ms` | ms | lower_is_better | p95 absolute anchor error |
| alignment | `speech_coverage` | ratio | higher_is_better | voiced reference time covered by non-empty hypotheses |
| alignment | `alignment_failure_rate` | ratio | lower_is_better | corrected spans still `needs_alignment` after realignment |
| presentation | `suber` | ratio | lower_is_better | subtitle error rate over cue blocks/line segmentation |
| presentation | `cue_duration_violation_rate` | ratio | lower_is_better | cues exceeding the presentation duration limit |
| presentation | `cue_cps_p95` | chars_per_second | lower_is_better | p95 characters-per-second across cues |
| presentation | `cue_line_width_violation_rate` | ratio | lower_is_better | lines exceeding the display width limit |
| retrieval | `retrieval_recall_at_k` | ratio | higher_is_better | relevant units in top k (k recorded in `scope`) |
| retrieval | `retrieval_mrr` | score | higher_is_better | mean reciprocal rank of first relevant unit |
| retrieval | `retrieval_evidence_sufficiency` | ratio | higher_is_better | answers citing ≥ minimum evidence units |
| qa | `qa_faithfulness` | ratio | higher_is_better | non-refusal answers fully entailed by cited evidence |
| qa | `qa_refusal_rate` | ratio | information | refusals for insufficient evidence (policy target) |
| quiz | `quiz_validity` | ratio | higher_is_better | items answerable from cited evidence with one determinate answer |
| quiz | `quiz_answerability` | ratio | higher_is_better | items answerable from cited evidence alone |
| quiz | `quiz_correctness` | ratio | higher_is_better | generated answers matching adjudicated references |
| performance | `rtf` | ratio | lower_is_better | processing wall time / media duration |
| performance | `rss_peak_mb` | MB | lower_is_better | peak resident set size |
| performance | `disk_delta_mb` | MB | lower_is_better | net durable bytes written |
| cost | `api_tokens_total` | count | information | provider-reported input+output counts (no credentials) |
| cost | `api_cost_usd` | usd | information | provider-reported billing estimate |
| checkpoint | `checkpoint_size_bytes` | bytes | information | largest serialized checkpoint payload |
| checkpoint | `recovery_time_s` | s | lower_is_better | interruption → resumed equivalent progress |

## 11. Validation API and error codes

`shared/evidence_contract.py` exposes: `canonical_json`, `compute_id`,
`normalize_document`, `validate_document`, `assign_ids`, `to_json`,
`from_json` (strict parse: duplicate keys, NaN/Infinity rejected),
`empty_measurement_tables`, the closed-set constants, `METRICS`, and
`EvidenceContractError` with `code`/`message`/`path`.

Codes: `not_json_object`, `contract_missing`, `contract_unsupported`,
`type_invalid`, `field_required`, `value_invalid`, `value_unsupported`,
`status_unsupported`, `id_malformed`, `id_mismatch`, `duplicate_id`,
`anchor_negative`, `anchor_descending`, `anchor_out_of_range`,
`reference_missing`, `duplicate_reference`, `invalid_derivation`,
`empty_correction`, `state_conflict`, `metric_unknown`, `secret_like`,
`secret_like_key`, `duplicate_key`.

Field checks run before ID verification, so a moved anchor is reported as
`anchor_*`/`state_conflict` even when the ID no longer matches.

## 12. Compatibility rules

- Additive fields anywhere are preserved through validation and round-trip
  and never enter identity, so adding metadata does not churn IDs.
- Closed sets (`source.kind`/`origin`, correction `state`/`actor`,
  `unit.kind`, span kinds, checkpoint `status`, measurement
  `unit`/`direction`/`definition`, the metric catalog) require a new
  contract version to extend; do not widen them silently.
- The benchmark fixture embeds catalog-generated measurement rows and
  content-addressed IDs; changing the catalog or any identity field
  definition requires regenerating
  `tests/fixtures/evidence_benchmark_v1.json` (its tests enforce equality
  with `empty_measurement_tables()` and re-verified IDs).
- This contract introduces no network, provider, UI, or worker changes and
  performs no real-account, media, or remote validation.
