"""Focused tests for the deterministic evidence benchmark scorer (BENCH-1).

Covers exact hand-derived metric values on the synthetic scoring fixture,
determinism and input-order stability, null denominators for insufficient
input, closed-set input error codes, contract validation of the output
document, secret-like input rejection through the existing contract, and the
CLI stdout / explicit-output / error behavior.  Standard library only; no
network, no real accounts, no provider calls; the fixture carries no real
media or course content.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shared import evidence_contract as ec
from scripts import evidence_benchmark as bench

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "evidence_benchmark_scoring_v1.json"

# Hand-derived expectations for the synthetic fixture (see the fixture
# description and the scorer docstring for the exact formulas):
# - matched pairs: R0/H0 (delete 每升), R1/H1 (sub s->c), R2/H2-R4/H4, R8/H8
#   identical, R5/H5 (insert "any"); R6 unmatched (deletions), H7 unmatched
#   over reference silence (insertions).
# - CER characters: reference 234 (20+44+32+50+13+31+37+0+7), hypothesis 199;
#   ops S=1, D=2+37=39, I=3+2=5.
# - WER words: reference 41 (1+8+6+11+1+6+7+0+1); ops S=2, D=7, I=2.
# - boundary errors ms: [150, 100, 0, 500, 750, 0, 0].
# - coverage: voiced reference union 47800 ms, covered 41100 ms.
EXPECTED_VALUES = {
    ("asr_text", "cer"): 45 / 234,
    ("asr_text", "wer"): 11 / 41,
    ("asr_text", "terminology_accuracy"): 3 / 4,
    ("asr_text", "numeric_accuracy"): 3 / 4,
    ("asr_text", "unit_accuracy"): 1 / 2,
    ("asr_text", "negation_accuracy"): 3 / 4,
    ("asr_text", "unsupported_addition_rate"): 5 / 199,
    ("alignment", "boundary_mae_ms"): 1500 / 7,
    ("alignment", "boundary_p95_ms"): 750,
    ("alignment", "speech_coverage"): 41100 / 47800,
    ("alignment", "alignment_failure_rate"): 1 / 2,
    ("presentation", "cue_duration_violation_rate"): 1 / 5,
    ("presentation", "cue_cps_p95"): 15000 / 8100,
}

EXPECTED_NULL_METRICS = {
    ("presentation", "suber"),
    ("presentation", "cue_line_width_violation_rate"),
    ("retrieval", "retrieval_recall_at_k"),
    ("retrieval", "retrieval_mrr"),
    ("retrieval", "retrieval_evidence_sufficiency"),
    ("qa", "qa_faithfulness"),
    ("qa", "qa_refusal_rate"),
    ("quiz", "quiz_validity"),
    ("quiz", "quiz_answerability"),
    ("quiz", "quiz_correctness"),
    ("performance", "rtf"),
    ("performance", "rss_peak_mb"),
    ("performance", "disk_delta_mb"),
    ("cost", "api_tokens_total"),
    ("cost", "api_cost_usd"),
    ("checkpoint", "checkpoint_size_bytes"),
    ("checkpoint", "recovery_time_s"),
}


def _base_input() -> dict:
    return {
        "scoring": bench.SCORING_CONTRACT,
        "source": {
            "kind": "recording",
            "origin": "native_upload",
            "title": "Synthetic minimal scoring source",
            "duration_ms": 60000,
        },
        "reference": {"segments": []},
        "hypothesis": {"segments": []},
    }


def _segment(start: int, end: int, text: str, **extra) -> dict:
    row = {"start_ms": start, "end_ms": end, "text": text, "lang": None, "no_speech": False}
    row.update(extra)
    return row


def _score_values(input_doc: dict) -> dict:
    document = bench.score_benchmark(input_doc)
    return {(row["family"], row["metric"]): row["value"] for row in document["measurements"]["tables"]}


def _expect_error(callable_, code: str):
    with unittest.TestCase().assertRaises((bench.BenchmarkInputError, ec.EvidenceContractError)) as caught:
        callable_()
    unittest.TestCase().assertEqual(caught.exception.code, code)


class ScoringFixtureTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.document = bench.score_benchmark(cls.fixture)
        cls.values = {
            (row["family"], row["metric"]): row["value"]
            for row in cls.document["measurements"]["tables"]
        }


class TestFixtureMetricValues(ScoringFixtureTestBase):
    def test_exact_known_metric_values(self):
        for key, expected in EXPECTED_VALUES.items():
            with self.subTest(metric=key):
                self.assertEqual(self.values[key], expected)

    def test_all_other_catalog_rows_stay_null(self):
        self.assertEqual(len(self.document["measurements"]["tables"]), 30)
        for key in EXPECTED_NULL_METRICS:
            with self.subTest(metric=key):
                self.assertIsNone(self.values[key])
        filled = {key for key, value in self.values.items() if value is not None}
        self.assertEqual(filled, set(EXPECTED_VALUES))

    def test_filled_rows_keep_catalog_identity(self):
        empty_rows = {
            (row["family"], row["metric"]): row for row in ec.empty_measurement_tables()
        }
        for row in self.document["measurements"]["tables"]:
            empty = empty_rows[(row["family"], row["metric"])]
            for field in ("id", "unit", "direction", "definition", "dims", "scope"):
                self.assertEqual(row[field], empty[field], row["metric"])

    def test_output_document_is_normal_form_and_round_trips(self):
        self.assertEqual(ec.validate_document(self.document), self.document)
        self.assertEqual(ec.from_json(ec.to_json(self.document)), self.document)

    def test_reference_rows_present_as_evidence_with_ids(self):
        segments = self.document["speech"]["segments"]
        self.assertEqual(len(segments), 9)
        for segment in segments:
            self.assertRegex(segment["id"], r"^seg:[0-9a-f]{12}$")
        self.assertRegex(self.document["source"]["id"], r"^src:[0-9a-f]{12}$")
        self.assertEqual(
            self.document["fingerprints"]["producer"],
            self.fixture["fingerprints"]["producer"],
        )

    def test_matching_summary_records_pairs_and_unmatched(self):
        matching = self.document["x_benchmark"]["matching"]
        self.assertEqual(len(matching["matched_pairs"]), 7)
        self.assertEqual(matching["matched_pairs"][0]["overlap_ms"], 7900)
        matched_ids = {pair["reference_id"] for pair in matching["matched_pairs"]}
        self.assertEqual(len(matching["unmatched_reference_ids"]), 1)
        unmatched_id = matching["unmatched_reference_ids"][0]
        self.assertRegex(unmatched_id, r"^seg:[0-9a-f]{12}$")
        self.assertNotIn(unmatched_id, matched_ids)
        self.assertEqual(
            matching["unmatched_hypothesis"],
            [{"start_ms": 50500, "end_ms": 53500, "text": "呃嗯"}],
        )


class TestDeterminism(ScoringFixtureTestBase):
    def test_repeated_scoring_is_byte_identical(self):
        again = bench.score_benchmark(self.fixture)
        self.assertEqual(ec.canonical_json(again), ec.canonical_json(self.document))

    def test_input_order_stability(self):
        shuffled = copy.deepcopy(self.fixture)
        shuffled["reference"]["segments"].reverse()
        shuffled["hypothesis"]["segments"].reverse()
        shuffled["hypothesis"]["corrections"].reverse()
        shuffled["hypothesis"]["cues"].reverse()
        shuffled["scope"]["terms"].reverse()
        shuffled["scope"]["units"].reverse()
        document = bench.score_benchmark(shuffled)
        self.assertEqual(ec.canonical_json(document), ec.canonical_json(self.document))


class TestNormalizationAndPrimitives(unittest.TestCase):
    def test_cer_normalization_removes_whitespace_and_casefolds(self):
        self.assertEqual(bench.normalize_cer_text("  Let's\tGO\n"), "let'sgo")
        self.assertEqual(bench.normalize_cer_text("ＡＢ"), "ａｂ")

    def test_occurrence_normalization_collapses_whitespace(self):
        self.assertEqual(bench.normalize_occurrence_text(" a \n b  c "), "a b c")

    def test_edit_operations_counts(self):
        self.assertEqual(bench.edit_operations("kitten", "kitten"), (0, 0, 0))
        self.assertEqual(bench.edit_operations("abcd", "abxd"), (1, 0, 0))
        self.assertEqual(bench.edit_operations("abc", "abcx"), (0, 0, 1))
        self.assertEqual(bench.edit_operations("abcx", "abc"), (0, 1, 0))
        # "ab" -> "ba": deletion+insertion and two substitutions both cost 2;
        # the documented tie-break prefers the diagonal (substitutions).
        self.assertEqual(bench.edit_operations("ab", "ba"), (2, 0, 0))
        self.assertEqual(sum(bench.edit_operations("ab", "ba")), 2)

    def test_percentile_nearest_rank(self):
        self.assertIsNone(bench.percentile_nearest_rank([], 0.95))
        self.assertEqual(bench.percentile_nearest_rank([7], 0.95), 7)
        self.assertEqual(bench.percentile_nearest_rank(sorted([3, 1, 2]), 0.95), 3)
        self.assertEqual(bench.percentile_nearest_rank(list(range(1, 21)), 0.95), 19)

    def test_numeric_extraction_shapes(self):
        items = bench.extract_numeric_items(
            bench.normalize_occurrence_text("equals 3.0e8 meters, 45%, and v2 plus 3.14159.")
        )
        self.assertEqual(items, ["3.0e8", "45%", "3.14159"])
        self.assertEqual(bench.extract_numeric_items("no digits here"), [])

    def test_negation_word_boundary(self):
        # "annotations" contains the substring "not" but has no word boundary.
        corpus = bench.normalize_occurrence_text("it works without annotations")
        self.assertEqual(len(bench._LATIN_NEGATION_RE.findall(corpus)), 1)
        self.assertEqual(bench.negation_accuracy(corpus, corpus), 1.0)
        self.assertIsNone(bench.negation_accuracy("annotations", "annotations"))

    def test_cue_units_mixed_scripts(self):
        self.assertEqual(bench.cue_units(["今天我们讨论第三章，", "剂量是50毫克。"]), 15)
        self.assertEqual(bench.cue_units(["Let's start with the first", "topic: gradient decent."]), 8)

    def test_match_segments_is_one_to_one_and_prefers_larger_overlap(self):
        reference = [_segment(0, 10000, "aaaa"), _segment(12000, 20000, "bbbb")]
        hypothesis = [_segment(9000, 13000, "cccc")]
        pairs, unmatched_ref, unmatched_hyp = bench.match_segments(reference, hypothesis)
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0]["text"], "aaaa")
        self.assertEqual(pairs[0][2], 1000)
        self.assertEqual([row["text"] for row in unmatched_ref], ["bbbb"])
        self.assertEqual(unmatched_hyp, [])

    def test_zero_duration_anchors_never_match(self):
        pairs, _, _ = bench.match_segments(
            [_segment(100, 100, "hi")], [_segment(100, 100, "hi")]
        )
        self.assertEqual(pairs, [])

    def test_interval_measures(self):
        self.assertEqual(bench.interval_union_measure([(0, 5), (5, 10), (20, 30)]), 20)
        self.assertEqual(bench.interval_union_measure([(0, 10), (2, 5)]), 10)
        self.assertEqual(
            bench.interval_intersection_measure([(0, 10), (20, 30)], [(5, 25)]), 10
        )
        self.assertEqual(bench.interval_intersection_measure([(0, 5)], [(10, 20)]), 0)


class TestNullDenominators(unittest.TestCase):
    def test_empty_reference_nulls_text_and_alignment_metrics(self):
        values = _score_values(
            {
                **_base_input(),
                "hypothesis": {"segments": [_segment(0, 1000, "hello")]},
            }
        )
        self.assertIsNone(values[("asr_text", "cer")])
        self.assertIsNone(values[("asr_text", "wer")])
        self.assertIsNone(values[("alignment", "boundary_mae_ms")])
        self.assertIsNone(values[("alignment", "boundary_p95_ms")])
        self.assertIsNone(values[("alignment", "speech_coverage")])
        self.assertEqual(values[("asr_text", "unsupported_addition_rate")], 1.0)

    def test_empty_hypothesis_nulls_unsupported_rate(self):
        values = _score_values(
            {
                **_base_input(),
                "reference": {"segments": [_segment(0, 1000, "hello")]},
            }
        )
        self.assertIsNone(values[("asr_text", "unsupported_addition_rate")])
        self.assertEqual(values[("asr_text", "cer")], 1.0)

    def test_all_silent_reference_nulls_coverage_and_cer(self):
        values = _score_values(
            {
                **_base_input(),
                "reference": {
                    "segments": [_segment(0, 4000, "", no_speech=True)],
                    },
                "hypothesis": {"segments": [_segment(0, 4000, "hello")]},
            }
        )
        self.assertIsNone(values[("alignment", "speech_coverage")])
        self.assertIsNone(values[("asr_text", "cer")])

    def test_non_overlapping_spans_null_boundary_metrics(self):
        values = _score_values(
            {
                **_base_input(),
                "reference": {"segments": [_segment(0, 1000, "aaaa")]},
                "hypothesis": {"segments": [_segment(50000, 51000, "aaaa")]},
            }
        )
        self.assertIsNone(values[("alignment", "boundary_mae_ms")])
        self.assertIsNone(values[("alignment", "boundary_p95_ms")])

    def test_no_corrections_nulls_alignment_failure_rate(self):
        values = _score_values(_base_input())
        self.assertIsNone(values[("alignment", "alignment_failure_rate")])

    def test_missing_cues_or_limit_null_cue_metrics(self):
        values = _score_values(_base_input())
        self.assertIsNone(values[("presentation", "cue_duration_violation_rate")])
        self.assertIsNone(values[("presentation", "cue_cps_p95")])
        values = _score_values(
            {
                **_base_input(),
                "hypothesis": {
                    "segments": [],
                    "cues": [{"start_ms": 0, "end_ms": 4000, "lines": ["你好"]}],
                },
            }
        )
        self.assertIsNone(values[("presentation", "cue_duration_violation_rate")])
        self.assertEqual(values[("presentation", "cue_cps_p95")], 2 * 1000 / 4000)

    def test_scope_without_reference_occurrences_nulls_accuracy(self):
        values = _score_values(
            {
                **_base_input(),
                "scope": {"terms": ["absent term"], "units": ["absent unit"]},
            }
        )
        self.assertIsNone(values[("asr_text", "terminology_accuracy")])
        self.assertIsNone(values[("asr_text", "unit_accuracy")])
        self.assertIsNone(values[("asr_text", "numeric_accuracy")])
        self.assertIsNone(values[("asr_text", "negation_accuracy")])


class TestInputErrorCodes(unittest.TestCase):
    def test_error_codes_are_a_unique_closed_set(self):
        self.assertEqual(len(bench.ERROR_CODES), len(set(bench.ERROR_CODES)))

    def test_root_and_contract_errors(self):
        _expect_error(lambda: bench.score_benchmark([1, 2]), "not_json_object")
        _expect_error(lambda: bench.score_benchmark({}), "input_contract_missing")
        _expect_error(
            lambda: bench.score_benchmark({**_base_input(), "scoring": "scoring.v0"}),
            "input_contract_unsupported",
        )

    def test_unknown_sections_rejected(self):
        _expect_error(
            lambda: bench.score_benchmark({**_base_input(), "bogus": {}}),
            "section_unknown",
        )
        input_doc = _base_input()
        input_doc["reference"]["notes"] = []
        _expect_error(lambda: bench.score_benchmark(input_doc), "section_unknown")
        input_doc = _base_input()
        input_doc["hypothesis"]["segments"] = [_segment(0, 100, "x", tokens=None)]
        _expect_error(lambda: bench.score_benchmark(input_doc), "section_unknown")
        input_doc = _base_input()
        input_doc["source"] = {**input_doc["source"], "id": "src:000000000000"}
        _expect_error(lambda: bench.score_benchmark(input_doc), "section_unknown")

    def test_required_fields(self):
        input_doc = _base_input()
        del input_doc["source"]
        _expect_error(lambda: bench.score_benchmark(input_doc), "field_required")
        input_doc = _base_input()
        input_doc["reference"] = {}
        _expect_error(lambda: bench.score_benchmark(input_doc), "field_required")
        input_doc = _base_input()
        input_doc["hypothesis"]["segments"] = [{"start_ms": 0, "end_ms": 100}]
        _expect_error(lambda: bench.score_benchmark(input_doc), "field_required")
        input_doc = _base_input()
        input_doc["hypothesis"]["corrections"] = [{"text": "x"}]
        _expect_error(lambda: bench.score_benchmark(input_doc), "field_required")
        input_doc = _base_input()
        input_doc["hypothesis"]["cues"] = [{"start_ms": 0, "end_ms": 100}]
        _expect_error(lambda: bench.score_benchmark(input_doc), "field_required")

    def test_type_errors(self):
        input_doc = _base_input()
        input_doc["reference"]["segments"] = "nope"
        _expect_error(lambda: bench.score_benchmark(input_doc), "type_invalid")
        input_doc = _base_input()
        input_doc["reference"]["segments"] = [_segment("0", 100, "x")]
        _expect_error(lambda: bench.score_benchmark(input_doc), "type_invalid")
        input_doc = _base_input()
        input_doc["presentation"] = {"max_cue_duration_ms": 7000.0}
        _expect_error(lambda: bench.score_benchmark(input_doc), "type_invalid")
        input_doc = _base_input()
        input_doc["hypothesis"]["segments"] = [_segment(0, 100, "x", no_speech="yes")]
        _expect_error(lambda: bench.score_benchmark(input_doc), "type_invalid")

    def test_negative_and_descending_anchors(self):
        input_doc = _base_input()
        input_doc["reference"]["segments"] = [_segment(-1, 100, "x")]
        _expect_error(lambda: bench.score_benchmark(input_doc), "value_negative")
        input_doc = _base_input()
        input_doc["presentation"] = {"max_cue_duration_ms": -1}
        _expect_error(lambda: bench.score_benchmark(input_doc), "value_negative")
        input_doc = _base_input()
        input_doc["hypothesis"]["segments"] = [_segment(200, 100, "x")]
        _expect_error(lambda: bench.score_benchmark(input_doc), "anchor_descending")
        input_doc = _base_input()
        input_doc["hypothesis"]["cues"] = [{"start_ms": 200, "end_ms": 100, "lines": ["x"]}]
        _expect_error(lambda: bench.score_benchmark(input_doc), "anchor_descending")

    def test_value_invalid_annotations(self):
        input_doc = _base_input()
        input_doc["hypothesis"]["segments"] = [_segment(0, 100, "x", no_speech=True)]
        _expect_error(lambda: bench.score_benchmark(input_doc), "value_invalid")
        input_doc = _base_input()
        input_doc["hypothesis"]["corrections"] = [{"state": "applied"}]
        _expect_error(lambda: bench.score_benchmark(input_doc), "value_invalid")
        input_doc = _base_input()
        input_doc["hypothesis"]["corrections"] = [{"state": "rejected", "actor": "robot"}]
        _expect_error(lambda: bench.score_benchmark(input_doc), "value_invalid")
        input_doc = _base_input()
        input_doc["hypothesis"]["cues"] = [{"start_ms": 0, "end_ms": 100, "lines": []}]
        _expect_error(lambda: bench.score_benchmark(input_doc), "value_invalid")
        input_doc = _base_input()
        input_doc["scope"] = {"terms": [""]}
        _expect_error(lambda: bench.score_benchmark(input_doc), "value_invalid")

    def test_zero_duration_cue_rejected_for_cps(self):
        input_doc = _base_input()
        input_doc["hypothesis"]["cues"] = [{"start_ms": 1000, "end_ms": 1000, "lines": ["x"]}]
        _expect_error(lambda: bench.score_benchmark(input_doc), "cue_duration_zero")

    def test_strict_json_parse_rejects_duplicates_and_nan(self):
        _expect_error(lambda: bench.parse_strict_json('{"a": 1, "a": 2}'), "duplicate_key")
        _expect_error(lambda: bench.parse_strict_json('{"x": NaN}'), "value_invalid")
        _expect_error(lambda: bench.parse_strict_json("{oops"), "value_invalid")

    def test_non_finite_anchor_fails_closed(self):
        def _call():
            input_doc = _base_input()
            input_doc["source"]["duration_ms"] = 1e999
            return bench.score_benchmark(input_doc)

        _expect_error(_call, "type_invalid")


class TestSecretRejectionThroughContract(unittest.TestCase):
    def test_secret_like_hypothesis_text_rejected(self):
        input_doc = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        input_doc["hypothesis"]["segments"][0]["text"] = "call api_key: sk-0123456789abcdef012345"
        with self.assertRaises(ec.EvidenceContractError) as caught:
            bench.score_benchmark(input_doc)
        self.assertEqual(caught.exception.code, "secret_like")

    def test_secret_like_source_title_rejected(self):
        input_doc = _base_input()
        input_doc["source"]["title"] = "Bearer abcdef0123456789abcdef"
        with self.assertRaises(ec.EvidenceContractError) as caught:
            bench.score_benchmark(input_doc)
        self.assertEqual(caught.exception.code, "secret_like")


class TestCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = ROOT / "scripts" / "evidence_benchmark.py"
        cls.expected = bench.score_benchmark(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(self.script), *[str(arg) for arg in args]],
            capture_output=True,
            cwd=str(ROOT),
            timeout=120,
        )

    def test_stdout_smoke_on_synthetic_fixture(self):
        result = self._run(FIXTURE_PATH)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        self.assertEqual(json.loads(result.stdout.decode("utf-8")), self.expected)

    def test_explicit_output_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "scoring-result.json"
            result = self._run(FIXTURE_PATH, "-o", out_path)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
            self.assertEqual(result.stdout, b"")
            written = out_path.read_text(encoding="utf-8")
            self.assertTrue(written.endswith("\n"))
            self.assertEqual(written, ec.canonical_json(json.loads(written)) + "\n")
            self.assertEqual(json.loads(written), self.expected)

    def test_error_exits_nonzero_with_code_on_stderr(self):
        input_doc = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        input_doc["hypothesis"]["segments"][0]["text"] = "call api_key: sk-0123456789abcdef012345"
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = Path(tmp) / "bad-input.json"
            bad_path.write_text(json.dumps(input_doc, ensure_ascii=False), encoding="utf-8")
            result = self._run(bad_path)
        self.assertEqual(result.returncode, 2)
        self.assertIn("secret_like", result.stderr.decode("utf-8"))

    def test_missing_input_file_reports_io_error(self):
        result = self._run(ROOT / "tests" / "fixtures" / "no_such_scoring_input.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("io_error", result.stderr.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
