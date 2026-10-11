"""Focused tests for the evidence.v1 contract module and benchmark fixture.

Covers JSON round-trip, deterministic position-independent IDs, provenance
rules, correction-state rules, SlideEntity/SlideEvent distinction, malformed
input rejection, additive-field tolerance, and the synthetic fixture shape
including its negative-case matrix.  Standard library only; no network, no
real accounts, no provider calls.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unittest
from pathlib import Path

from shared import evidence_contract as ec

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "evidence_benchmark_v1.json"

SOURCE_SEED = "courseLens-synthetic-source-bytes-001"
MEDIA_SEED = "courseLens-synthetic-media-bytes-001"
CONFIG_SEED = "courseLens-synthetic-config-001"


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _apply_mutation(document: dict, case: dict) -> dict:
    mutated = copy.deepcopy(document)
    if "set" in case:
        parent = mutated
        for key in case["set"][:-1]:
            parent = parent[key]
        parent[case["set"][-1]] = case["value"]
    elif "remove" in case:
        parent = mutated
        for key in case["remove"][:-1]:
            parent = parent[key]
        del parent[case["remove"][-1]]
    elif "clone" in case:
        parent = mutated
        for key in case["clone"][:-1]:
            parent = parent[key]
        parent.append(copy.deepcopy(parent[case["clone"][-1]]))
    else:
        raise AssertionError(f"unknown mutation op in {case['name']}")
    return mutated


def _expect_error(validatee, code: str):
    """Assert validate_document raises EvidenceContractError with ``code``."""
    with unittest.TestCase().assertRaises(ec.EvidenceContractError) as caught:
        ec.validate_document(validatee)
    unittest.TestCase().assertEqual(caught.exception.code, code)


class EvidenceFixtureTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with FIXTURE_PATH.open("r", encoding="utf-8") as handle:
            cls.fixture = json.load(handle)
        cls.document = cls.fixture["document"]


class TestIdGrammarAndDeterminism(EvidenceFixtureTestBase):
    def test_compute_id_is_deterministic_and_discriminating(self):
        first = ec.compute_id("seg", {"start_ms": 0, "text": "a"})
        second = ec.compute_id("seg", {"start_ms": 0, "text": "a"})
        other = ec.compute_id("seg", {"start_ms": 0, "text": "b"})
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertRegex(first, r"^seg:[0-9a-f]{12}$")

    def test_fixture_ids_match_contract_grammar(self):
        for segment in self.document["speech"]["segments"]:
            self.assertRegex(segment["id"], r"^seg:[0-9a-f]{12}$")
        for unit in self.document["units"]:
            self.assertRegex(unit["id"], r"^unit:[0-9a-f]{12}$")
        self.assertRegex(self.document["source"]["id"], r"^src:[0-9a-f]{12}$")

    def test_array_order_does_not_change_validation_result(self):
        shuffled = copy.deepcopy(self.document)
        shuffled["speech"]["segments"].reverse()
        shuffled["corrections"].reverse()
        shuffled["cues"].reverse()
        shuffled["units"].reverse()
        shuffled["slides"]["entities"].reverse()
        shuffled["slides"]["events"].reverse()
        shuffled["checkpoints"].reverse()
        shuffled["measurements"]["tables"].reverse()
        self.assertEqual(ec.validate_document(shuffled), self.document)

    def test_segment_ids_do_not_depend_on_position(self):
        ids_before = [s["id"] for s in self.document["speech"]["segments"]]
        shuffled = copy.deepcopy(self.document)
        shuffled["speech"]["segments"].reverse()
        validated = ec.validate_document(shuffled)
        ids_after = [s["id"] for s in validated["speech"]["segments"]]
        self.assertEqual(sorted(ids_before), sorted(ids_after))
        self.assertEqual(ids_before, ids_after)


class TestRoundTrip(EvidenceFixtureTestBase):
    def test_fixture_document_is_in_normal_form(self):
        self.assertEqual(ec.validate_document(self.document), self.document)

    def test_fixture_document_json_round_trip(self):
        validated = ec.validate_document(self.document)
        text = ec.to_json(validated)
        reparsed = ec.from_json(text)
        self.assertEqual(reparsed, validated)
        self.assertEqual(ec.to_json(reparsed), text)

    def test_canonical_json_layout(self):
        validated = ec.validate_document(self.document)
        expected = json.dumps(
            validated, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        self.assertEqual(ec.to_json(validated), expected)

    def test_minimal_document_round_trip(self):
        minimal = {
            "contract": ec.CONTRACT_ID,
            "source": {
                "kind": "recording",
                "origin": "native_upload",
                "title": "Minimal synthetic source",
                "duration_ms": 1000,
            },
        }
        validated = ec.assign_ids(minimal)
        self.assertTrue(validated["source"]["id"].startswith("src:"))
        self.assertEqual(validated["speech"]["segments"], [])
        self.assertEqual(ec.from_json(ec.to_json(validated)), validated)

    def test_missing_id_is_rejected(self):
        minimal = {
            "contract": ec.CONTRACT_ID,
            "source": {"kind": "recording", "origin": "native_upload", "duration_ms": 1000},
        }
        _expect_error(minimal, "field_required")

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(ec.EvidenceContractError) as caught:
            ec.from_json('{"contract":"evidence.v1","contract":"evidence.v1"}')
        self.assertEqual(caught.exception.code, "duplicate_key")

    def test_invalid_json_rejected(self):
        with self.assertRaises(ec.EvidenceContractError) as caught:
            ec.from_json("{oops")
        self.assertEqual(caught.exception.code, "value_invalid")

    def test_nan_constant_rejected(self):
        with self.assertRaises(ec.EvidenceContractError) as caught:
            ec.from_json('{"contract":"evidence.v1","measurements":{"x": NaN}}')
        self.assertEqual(caught.exception.code, "value_invalid")


class TestProvenance(EvidenceFixtureTestBase):
    def test_fixture_source_fingerprints_are_deterministic_hashes(self):
        source = self.document["source"]
        self.assertEqual(source["source_sha256"], _sha(SOURCE_SEED))
        fingerprints = self.document["fingerprints"]
        self.assertEqual(fingerprints["media_sha256"], _sha(MEDIA_SEED))
        self.assertEqual(fingerprints["config_hash"], _sha(CONFIG_SEED))
        self.assertEqual(fingerprints["producer"], "courselens-synthetic-producer")

    def test_external_import_requires_source_sha256(self):
        imported = {
            "contract": ec.CONTRACT_ID,
            "source": {
                "kind": "document",
                "origin": "external_import",
                "title": "Synthetic import",
                "duration_ms": None,
                "source_sha256": None,
            },
        }
        with self.assertRaises(ec.EvidenceContractError) as caught:
            ec.validate_document(imported)
        self.assertEqual(caught.exception.code, "field_required")
        imported["source"]["source_sha256"] = "a" * 64
        validated = ec.assign_ids(imported)
        self.assertTrue(validated["source"]["id"].startswith("src:"))

    def test_segment_source_hash_must_match_source(self):
        mutated = copy.deepcopy(self.document)
        mutated["speech"]["segments"][0]["source_hash"] = "b" * 64
        _expect_error(mutated, "value_invalid")
        restored = copy.deepcopy(self.document)
        restored["speech"]["segments"][0]["source_hash"] = restored["source"]["source_sha256"]
        self.assertEqual(ec.validate_document(restored), restored)

    def test_all_anchors_absolute_within_duration_and_600s_boundary(self):
        duration = self.document["source"]["duration_ms"]
        self.assertEqual(duration, 600000)
        for segment in self.document["speech"]["segments"]:
            self.assertIsInstance(segment["start_ms"], int)
            self.assertIsInstance(segment["end_ms"], int)
            self.assertGreaterEqual(segment["start_ms"], 0)
            self.assertLessEqual(segment["end_ms"], duration)
        self.assertTrue(any(s["end_ms"] == 600000 for s in self.document["speech"]["segments"]))


class TestCorrections(EvidenceFixtureTestBase):
    def test_fixture_covers_all_three_states(self):
        states = {c["state"] for c in self.document["corrections"]}
        self.assertEqual(states, set(ec.CORRECTION_STATES))

    def test_timing_preserved_anchor_change_rejected(self):
        corrections = self.document["corrections"]
        index = next(i for i, c in enumerate(corrections) if c["state"] == "timing_preserved")
        target = next(
            s for s in self.document["speech"]["segments"] if s["id"] == corrections[index]["target"]
        )
        mutated = copy.deepcopy(self.document)
        mutated["corrections"][index]["start_ms"] = target["start_ms"] + 1
        mutated["corrections"][index]["end_ms"] = target["end_ms"]
        _expect_error(mutated, "state_conflict")

    def test_needs_alignment_requires_a_change(self):
        corrections = self.document["corrections"]
        index = next(i for i, c in enumerate(corrections) if c["state"] == "timing_preserved")
        target = next(
            s for s in self.document["speech"]["segments"] if s["id"] == corrections[index]["target"]
        )
        mutated = copy.deepcopy(self.document)
        mutated["corrections"][index]["state"] = "needs_alignment"
        mutated["corrections"][index]["text"] = target["text"]
        _expect_error(mutated, "empty_correction")

    def test_rejected_correction_cannot_back_units(self):
        corrections = self.document["corrections"]
        rejected = next(c for c in corrections if c["state"] == "rejected")
        note_index = next(i for i, u in enumerate(self.document["units"]) if u["kind"] == "note")
        mutated = copy.deepcopy(self.document)
        mutated["units"][note_index]["spans"][0]["id"] = rejected["id"]
        _expect_error(mutated, "invalid_derivation")


class TestSlides(EvidenceFixtureTestBase):
    def test_repeated_slide_identity_keeps_one_entity_with_two_events(self):
        entities = self.document["slides"]["entities"]
        page_two = [e for e in entities if e["page"] == 2]
        self.assertEqual(len(page_two), 1)
        entity_id = page_two[0]["id"]
        occurrences = [e for e in self.document["slides"]["events"] if e["entity"] == entity_id]
        self.assertEqual(len(occurrences), 2)
        self.assertNotEqual(occurrences[0]["id"], occurrences[1]["id"])
        self.assertNotEqual(entity_id, occurrences[0]["id"])

    def test_slide_page_must_be_positive(self):
        mutated = copy.deepcopy(self.document)
        mutated["slides"]["entities"][0]["page"] = 0
        _expect_error(mutated, "value_invalid")


class TestUnits(EvidenceFixtureTestBase):
    def test_fixture_covers_all_unit_kinds(self):
        kinds = {u["kind"] for u in self.document["units"]}
        self.assertEqual(kinds, set(ec.UNIT_KINDS))

    def test_views_cite_evidence(self):
        for unit in self.document["units"]:
            self.assertGreaterEqual(len(unit["spans"]), 1)


class TestMeasurements(EvidenceFixtureTestBase):
    def test_fixture_tables_equal_empty_catalog(self):
        rows = self.document["measurements"]["tables"]
        self.assertEqual(
            sorted(rows, key=lambda r: r["id"]),
            sorted(ec.empty_measurement_tables(), key=lambda r: r["id"]),
        )

    def test_all_fixture_values_null(self):
        for row in self.document["measurements"]["tables"]:
            self.assertIsNone(row["value"])
        self.assertTrue(self.fixture["measurement_expectations"]["all_values_null"])

    def test_catalog_families_complete(self):
        families = {spec["family"] for spec in ec.METRICS}
        self.assertEqual(
            families,
            {
                "asr_text",
                "alignment",
                "presentation",
                "retrieval",
                "qa",
                "quiz",
                "performance",
                "cost",
                "checkpoint",
            },
        )
        names = {spec["metric"] for spec in ec.METRICS}
        for expected in (
            "cer",
            "wer",
            "suber",
            "boundary_mae_ms",
            "boundary_p95_ms",
            "speech_coverage",
            "alignment_failure_rate",
            "cue_cps_p95",
            "retrieval_evidence_sufficiency",
            "qa_faithfulness",
            "qa_refusal_rate",
            "quiz_validity",
            "rtf",
            "api_tokens_total",
            "checkpoint_size_bytes",
            "recovery_time_s",
        ):
            self.assertIn(expected, names)
        self.assertEqual(len(ec.METRICS), 30)

    def test_filling_value_keeps_row_identity(self):
        original_id = self.document["measurements"]["tables"][0]["id"]
        mutated = copy.deepcopy(self.document)
        mutated["measurements"]["tables"][0]["value"] = 0.5
        validated = ec.validate_document(mutated)
        self.assertEqual(validated["measurements"]["tables"][0]["id"], original_id)

    def test_value_rejects_bool_and_nonfinite(self):
        mutated = copy.deepcopy(self.document)
        mutated["measurements"]["tables"][0]["value"] = True
        _expect_error(mutated, "value_invalid")
        mutated = copy.deepcopy(self.document)
        mutated["measurements"]["tables"][0]["value"] = float("nan")
        _expect_error(mutated, "value_invalid")


class TestCheckpoints(EvidenceFixtureTestBase):
    def test_fixture_has_interrupted_checkpoint_metadata(self):
        statuses = {c["status"] for c in self.document["checkpoints"]}
        self.assertEqual(statuses, set(ec.CHECKPOINT_STATUSES))
        interrupted = next(
            c for c in self.document["checkpoints"] if c["status"] == "interrupted"
        )
        self.assertIsInstance(interrupted["position_ms"], int)


class TestSpeechAnchors(EvidenceFixtureTestBase):
    def test_overlap_segments_allowed(self):
        segments = self.document["speech"]["segments"]
        by_start = {s["start_ms"]: s for s in segments}
        first = by_start[30500]
        second = by_start[33000]
        self.assertLess(first["start_ms"], second["end_ms"])
        self.assertLess(second["start_ms"], first["end_ms"])

    def test_silence_segment_shape(self):
        silence = next(
            s for s in self.document["speech"]["segments"] if s["no_speech"] is True
        )
        self.assertEqual(silence["text"], "")

    def test_confidence_out_of_range_rejected(self):
        mutated = copy.deepcopy(self.document)
        mutated["speech"]["segments"][0]["confidence"] = 1.5
        _expect_error(mutated, "value_invalid")

    def test_token_points_are_absolute_within_segment(self):
        segment = self.document["speech"]["segments"][0]
        self.assertTrue(segment["tokens"])
        for text, start, end in segment["tokens"]:
            self.assertIsInstance(text, str)
            self.assertTrue(text)
            self.assertGreaterEqual(start, segment["start_ms"])
            if end is not None:
                self.assertLessEqual(end, segment["end_ms"])


class TestSecretsAndAdditive(EvidenceFixtureTestBase):
    def test_additive_fields_tolerated_and_do_not_change_ids(self):
        original_id = self.document["speech"]["segments"][0]["id"]
        mutated = copy.deepcopy(self.document)
        mutated["x_vendor_note"] = {"vendor": "synthetic", "flags": [1, 2]}
        mutated["speech"]["segments"][0]["x_raw_confidence"] = 0.42
        validated = ec.validate_document(mutated)
        self.assertEqual(validated["x_vendor_note"], {"vendor": "synthetic", "flags": [1, 2]})
        self.assertEqual(validated["speech"]["segments"][0]["x_raw_confidence"], 0.42)
        self.assertEqual(validated["speech"]["segments"][0]["id"], original_id)

    def test_additive_secret_value_rejected(self):
        mutated = copy.deepcopy(self.document)
        mutated["speech"]["segments"][0]["x_note"] = "Bearer abcdef0123456789abcdef"
        _expect_error(mutated, "secret_like")


class TestFixtureContract(EvidenceFixtureTestBase):
    def test_fixture_metadata(self):
        self.assertEqual(self.fixture["fixture"], "evidence_benchmark_v1")
        self.assertEqual(self.fixture["contract"], ec.CONTRACT_ID)
        self.assertIs(self.fixture["synthetic"], True)

    def test_negative_cases_all_rejected_with_expected_codes(self):
        cases = self.fixture["negative_cases"]
        self.assertEqual(len(cases), 24)
        for case in cases:
            with self.subTest(case=case["name"]):
                mutated = _apply_mutation(self.document, case)
                with self.assertRaises(ec.EvidenceContractError) as caught:
                    ec.validate_document(mutated)
                self.assertEqual(caught.exception.code, case["expect_code"])

    def test_document_hygiene_synthetic_only(self):
        raw = json.dumps(self.document, ensure_ascii=False)
        for forbidden in ("http://", "https://", "sk-", "Fudan", "@", "Bearer", "BEGIN"):
            self.assertNotIn(forbidden, raw)

    def test_code_switch_numbers_formula_silence_scenarios_present(self):
        segments = self.document["speech"]["segments"]
        texts = [s["text"] for s in segments]
        self.assertTrue(
            any(re.search(r"[\u4e00-\u9fff]", t) and re.search(r"[A-Za-z]", t) for t in texts),
            "expected a Mandarin/English code-switch segment",
        )
        joined = "".join(texts)
        for marker in ("3.14159", "E=mc^2", "50毫克每升", "2.5 kilograms"):
            self.assertIn(marker, joined)


if __name__ == "__main__":
    unittest.main()
