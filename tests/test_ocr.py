from __future__ import annotations

import io
import sys
import types
import unittest
from unittest.mock import patch

from PIL import Image

# The local client runtime does not install the Worker OCR stack; these unit
# contracts only need Pillow, so absent modules get inert stand-ins here while
# Worker CI exercises the real numpy/rapidocr build.
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

from courselens_worker.ocr import process_slides  # noqa: E402
from courselens_worker.runner import _process_materialized_job  # noqa: E402
from courselens_worker.source import SourceSecurityError  # noqa: E402


def _png_bytes(color: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 16), color).save(buffer, format="PNG")
    return buffer.getvalue()


PNG = _png_bytes("white")
HTML = b"<!doctype html><html><body>authorization expired</body></html>"


def _slides(*bodies: bytes) -> list[dict]:
    return [
        {"page_num": index + 1, "created_sec": 0, "source": {"url": f"https://example.invalid/{index}"}}
        for index in range(len(bodies))
    ]


class SlideOcrToleranceTests(unittest.TestCase):
    def _run(self, bodies: list[bytes], *, prior: dict | None = None):
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
                _slides(*bodies),
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
        checkpoints: list[dict] = []
        responses = {"https://example.invalid/0": HTML}
        with patch(
            "courselens_worker.ocr.fetch_bytes",
            side_effect=lambda source: responses[str(source.get("url"))],
        ), patch("courselens_worker.ocr._dhash", return_value="ab01ef01ab01ef01"):
            process_slides(
                _slides(HTML),
                progress=lambda *_args: None,
                checkpoint=checkpoints.append,
            )
        self.assertTrue(checkpoints)
        self.assertEqual(checkpoints[-1]["ppt_skipped"], {"unidentified_image": 1})


class RunnerSlideWarningTests(unittest.TestCase):
    def test_summary_job_degrades_with_warning_and_skip_metrics(self):
        job = {
            "task_id": "0123456789abcdef0123456789abcdef",
            "job_kind": "summary",
            "input_hash": "a" * 64,
            "pipeline": {"version": "v2"},
            "payload": {"title": "", "transcript": ["t"], "slides": [{"source": {}}]},
            "secrets": {},
        }
        with patch(
            "courselens_worker.ocr.process_slides",
            return_value=([], {"unidentified_image": 1}),
        ) as slides_call, patch(
            "courselens_worker.llm.create_summary",
            return_value={"markdown": "m", "chapters": [], "model": "deepseek-chat"},
        ) as summary_call:
            result = _process_materialized_job(job)
        self.assertTrue(slides_call.called)
        self.assertTrue(summary_call.called)
        self.assertEqual(result["outputs"]["ppt_pages"], [])
        self.assertEqual(result["metrics"]["slides_skipped"], {"unidentified_image": 1})
        self.assertEqual(result["warnings"], ["slides_skipped"])

    def test_clean_summary_job_keeps_empty_warning_list(self):
        job = {
            "task_id": "0123456789abcdef0123456789abcdef",
            "job_kind": "summary",
            "input_hash": "a" * 64,
            "pipeline": {"version": "v2"},
            "payload": {"title": "", "transcript": ["t"], "slides": []},
            "secrets": {},
        }
        with patch(
            "courselens_worker.llm.create_summary",
            return_value={"markdown": "m", "chapters": [], "model": "deepseek-chat"},
        ):
            result = _process_materialized_job(job)
        self.assertEqual(result["warnings"], [])
        self.assertNotIn("slides_skipped", result["metrics"])


if __name__ == "__main__":
    unittest.main()
