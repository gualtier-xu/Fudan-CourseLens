from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

# The private Windows CI installs the real Worker OCR stack; lighter runtimes
# (the public mirror unit job) only provide the signing dependencies, so these
# contracts skip there instead of failing to import.
try:
    from PIL import Image
except ModuleNotFoundError:
    Image = None

# numpy and rapidocr are imported by the OCR module itself; inert stand-ins
# keep the import contract testable wherever only Pillow is present.
try:
    import numpy  # noqa: F401
except ModuleNotFoundError:
    _numpy = types.ModuleType("numpy")
    _numpy.asarray = lambda value: value
    sys.modules.setdefault("numpy", _numpy)
try:
    import rapidocr_onnxruntime  # noqa: F401
except ModuleNotFoundError:
    _rapid = types.ModuleType("rapidocr_onnxruntime")
    _rapid.RapidOCR = object
    sys.modules.setdefault("rapidocr_onnxruntime", _rapid)

from courselens_worker.source import SourceSecurityError  # noqa: E402

if Image is not None:
    import io

    def _png_bytes(color: str) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (32, 16), color).save(buffer, format="PNG")
        return buffer.getvalue()

    PNG = _png_bytes("white")
    HTML = b"<!doctype html><html><body>authorization expired</body></html>"
else:
    PNG = b""
    HTML = b"<!doctype html><html><body>authorization expired</body></html>"


def _slides(count: int) -> list[dict]:
    return [
        {"page_num": index + 1, "created_sec": 0, "source": {"url": f"https://example.invalid/{index}"}}
        for index in range(count)
    ]


@unittest.skipIf(Image is None, "Pillow runtime required for slide OCR tests")
class SlideOcrToleranceTests(unittest.TestCase):
    def _run(self, bodies: list, *, prior: dict | None = None):
        from courselens_worker.ocr import process_slides

        responses = {f"https://example.invalid/{index}": body for index, body in enumerate(bodies)}
        def fake_fetch(source):
            value = responses[str(source.get("url"))]
            if isinstance(value, Exception):
                raise value
            return value

        with patch("courselens_worker.ocr.fetch_bytes", side_effect=fake_fetch), \
             patch("courselens_worker.ocr._dhash", return_value="ab01ef01ab01ef01"), \
             patch("courselens_worker.ocr._engine", return_value=lambda image: ([["box", "text"]], 0.1)):
            pages, skipped = process_slides(
                _slides(len(bodies)),
                progress=lambda *_args: None,
                prior_checkpoint=prior,
            )
        return pages, skipped

    def test_non_image_slide_is_skipped_without_failing_the_batch(self):
        pages, skipped = self._run([PNG, HTML])
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["text"], "text")
        self.assertEqual(skipped, {"unidentified_image": 1})

    def test_all_slides_unavailable_still_returns_without_raising(self):
        pages, skipped = self._run([HTML, HTML])
        self.assertEqual(pages, [])
        self.assertEqual(skipped, {"unidentified_image": 2})

    def test_fetch_failure_maps_to_closed_set_reason(self):
        pages, skipped = self._run([
            SourceSecurityError("source image returned HTTP 403"),
        ])
        self.assertEqual(pages, [])
        self.assertEqual(skipped, {"image_http_error": 1})

    def test_duplicate_slide_is_counted_not_duplicated(self):
        pages, skipped = self._run([PNG, PNG])
        self.assertEqual(len(pages), 1)
        self.assertEqual(skipped, {"duplicate": 1})

    def test_skip_counts_survive_checkpoint_resume(self):
        prior = {
            "ocr_completed_items": 1,
            "ppt_pages": [],
            "ppt_skipped": {"unidentified_image": 1},
        }
        pages, skipped = self._run([HTML, HTML], prior=prior)
        self.assertEqual(pages, [])
        self.assertEqual(skipped, {"unidentified_image": 2})

    def test_checkpoint_payload_carries_skip_counts(self):
        from courselens_worker.ocr import process_slides

        checkpoints: list[dict] = []
        with patch(
            "courselens_worker.ocr.fetch_bytes",
            side_effect=lambda source: HTML,
        ), patch("courselens_worker.ocr._dhash", return_value="ab01ef01ab01ef01"):
            process_slides(
                _slides(1),
                progress=lambda *_args: None,
                checkpoint=checkpoints.append,
            )
        self.assertTrue(checkpoints)
        self.assertEqual(checkpoints[-1]["ppt_skipped"], {"unidentified_image": 1})


class RunnerSlideWarningTests(unittest.TestCase):
    """Runner wiring is importable and testable without the OCR stack."""

    def _summary_job(self, slides: list) -> dict:
        return {
            "task_id": "0123456789abcdef0123456789abcdef",
            "job_kind": "summary",
            "input_hash": "a" * 64,
            "pipeline": {"version": "v2"},
            "payload": {"title": "", "transcript": ["t"], "slides": slides},
            "secrets": {},
        }

    def _run_summary(self, job: dict, slides_result):
        from unittest.mock import Mock

        from courselens_worker.runner import _process_materialized_job

        ocr_stub = types.ModuleType("courselens_worker.ocr")
        ocr_stub.process_slides = Mock(return_value=slides_result)
        llm_stub = types.ModuleType("courselens_worker.llm")
        llm_stub.create_summary = Mock(
            return_value={"markdown": "m", "chapters": [], "model": "deepseek-chat"}
        )
        with patch.dict(
            sys.modules,
            {"courselens_worker.ocr": ocr_stub, "courselens_worker.llm": llm_stub},
        ):
            result = _process_materialized_job(job)
        return result, ocr_stub.process_slides, llm_stub.create_summary

    def test_summary_job_degrades_with_warning_and_skip_metrics(self):
        result, slides_call, summary_call = self._run_summary(
            self._summary_job([{"source": {}}]), ([], {"unidentified_image": 1})
        )
        self.assertTrue(slides_call.called)
        self.assertTrue(summary_call.called)
        self.assertEqual(result["outputs"]["ppt_pages"], [])
        self.assertEqual(result["metrics"]["slides_skipped"], {"unidentified_image": 1})
        self.assertEqual(result["warnings"], ["slides_skipped"])

    def test_clean_summary_job_keeps_empty_warning_list(self):
        result, slides_call, _summary_call = self._run_summary(
            self._summary_job([]), (["prior-page"], {})
        )
        self.assertFalse(slides_call.called)
        self.assertEqual(result["outputs"]["ppt_pages"], [])
        self.assertEqual(result["warnings"], [])
        self.assertNotIn("slides_skipped", result["metrics"])


if __name__ == "__main__":
    unittest.main()
