"""Synthetic OCR evidence identity pipeline: worker records → store → search.

Exercises the real grouping/identity code paths end to end with fake fetch
and OCR engines plus a temporary database.  No network, accounts, or real
slide data are touched.
"""

from __future__ import annotations

import io
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKER_ROOT = REPO_ROOT / "worker"
for _candidate in (str(REPO_ROOT), str(_WORKER_ROOT)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from src.runtime.learning_schema import initialize_learning_schema  # noqa: E402
from src.runtime.learning_store import LearningStore, _ppt_evidence_metadata  # noqa: E402
from src.runtime.search_index import LearningSearchIndex  # noqa: E402
from src.runtime.subtitle_reader import is_evidence_id  # noqa: E402

try:
    from PIL import Image
except ModuleNotFoundError:
    Image = None

class _PixelMatrix:
    """Minimal 2-D array-like covering exactly the contract ``_dhash`` uses.

    ``np.asarray(pil_image)`` arrives as a row-major pixel grid; ``_dhash``
    slices both axes, compares elementwise, and flattens.  Nothing else is
    implemented — the engine input array is consumed by the patched engine.
    """

    def __init__(self, rows):
        self._rows = rows

    def __getitem__(self, key):
        row_slice, column_slice = key
        return _PixelMatrix([row[column_slice] for row in self._rows[row_slice]])

    def __gt__(self, other):
        return _PixelMatrix([
            [int(left > right) for left, right in zip(left_row, right_row)]
            for left_row, right_row in zip(self._rows, other._rows)
        ])

    def flatten(self):
        return [value for row in self._rows for value in row]


def _asarray(value):
    width, height = value.size
    pixels = list(value.getdata())
    return _PixelMatrix([
        pixels[offset:offset + width] for offset in range(0, width * height, width)
    ])


# Stand-ins keep the worker OCR module importable wherever the real stack is
# absent (same pattern as worker/tests/test_ocr.py).  The client runtime is
# intentionally numpy-free, so the numpy stand-in must satisfy the small
# ndarray contract worker ``_dhash`` exercises (2-D slicing, elementwise
# ``>``, ``flatten``) — otherwise every page fails closed as ``ocr_failed``.
try:
    import numpy  # noqa: F401
except ModuleNotFoundError:
    _numpy = types.ModuleType("numpy")
    _numpy.asarray = _asarray
    sys.modules.setdefault("numpy", _numpy)
try:
    import rapidocr_onnxruntime  # noqa: F401
except ModuleNotFoundError:
    _rapid = types.ModuleType("rapidocr_onnxruntime")
    _rapid.RapidOCR = object
    sys.modules.setdefault("rapidocr_onnxruntime", _rapid)

DECK = {"deck_id": "deck-0f1e2d3c4b5a", "source_id": "src:0f1e2d3c4b5a"}


def _png(color: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 24), color).save(buffer, format="PNG")
    return buffer.getvalue()


@unittest.skipIf(Image is None, "Pillow runtime required for OCR pipeline tests")
class OcrEvidencePipelineTests(unittest.TestCase):
    def _process(self, bodies: list, deck: dict | None = DECK):
        from courselens_worker.ocr import process_slides

        responses = {
            f"https://example.invalid/{index}": body for index, body in enumerate(bodies)
        }
        slides = [
            {
                "page_num": index + 1,
                "created_sec": (index + 1) * 30,
                "source": {"url": f"https://example.invalid/{index}"},
                **({"deck": deck} if deck else {}),
            }
            for index in range(len(bodies))
        ]
        with patch(
            "courselens_worker.ocr.fetch_bytes",
            side_effect=lambda source: responses[str(source.get("url"))],
        ), patch(
            "courselens_worker.ocr._engine",
            return_value=lambda image: ([["box", "repeated text"]], 0.1),
        ):
            return process_slides(slides, progress=lambda *_args: None)

    def _store_with_pages(self, directory: str, pages: list) -> LearningStore:
        store = LearningStore(Path(directory) / "learning.db")
        store.sync_search_catalog([{
            "sub_id": "lecture", "course_id": "course", "course_title": "course title",
            "lecture_title": "lecture title", "teacher": "", "catalog_version": "v1",
        }])
        store.import_remote_summary(
            course_id="course",
            sub_id="lecture",
            input_hash="a" * 64,
            model="model",
            markdown="summary",
            chapters=[],
            ppt_pages=pages,
        )
        return store

    def test_repeated_slide_flows_from_worker_to_store_and_search(self):
        repeated, unique = _png("white"), _png("black")
        pages, skipped = self._process([repeated, unique, repeated])
        self.assertEqual(skipped, {})
        self.assertEqual(len(pages), 3)
        self.assertEqual(pages[0]["entity_id"], pages[2]["entity_id"])
        self.assertNotEqual(pages[0]["event_id"], pages[2]["event_id"])
        self.assertNotEqual(pages[0]["entity_id"], pages[1]["entity_id"])
        for page in pages:
            self.assertEqual(page["deck_id"], DECK["deck_id"])
            self.assertTrue(is_evidence_id(page["entity_id"]))
            self.assertTrue(is_evidence_id(page["event_id"]))

        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_pages(directory, pages)
            done = store.get_done_ppt_pages("lecture")
            self.assertEqual([page["page_num"] for page in done], [1, 2, 3])
            self.assertEqual(done[0]["entity_id"], done[2]["entity_id"])
            self.assertNotEqual(done[0]["event_id"], done[2]["event_id"])
            self.assertEqual(done[0]["content_sha256"], pages[0]["source_sha256"])
            self.assertNotIn("source_sha256", done[0])
            self.assertNotIn("url", done[0])
            # Re-importing the same result stays idempotent per (sub, page).
            store.import_remote_summary(
                course_id="course", sub_id="lecture", input_hash="a" * 64,
                model="model", markdown="summary", chapters=[], ppt_pages=pages,
            )
            self.assertEqual(store.count_total_ppt_pages("lecture"), 3)

            index = LearningSearchIndex(
                store,
                catalog_snapshot=lambda: {},
                subtitle_sync=lambda sub_id: None,
            )
            catalog = {
                "course_title": "course title", "lecture_title": "lecture title",
                "teacher": "", "catalog_version": "v1",
            }
            documents = index._documents_for("lecture", catalog, "ver")
            ppt_docs = [document for document in documents if document.source == "ppt"]
            self.assertEqual(len(ppt_docs), 3)
            self.assertEqual(
                sorted(document.source_ref for document in ppt_docs),
                sorted(page["event_id"] for page in done),
            )
            store.replace_search_documents(
                "lecture", [document.public() for document in documents], indexed_version="ver"
            )
            result = index.search("repeated", sources=["ppt"])
            evidence = {row["evidence_id"] for row in result["results"]}
            self.assertEqual(
                evidence, {page["event_id"] for page in done}
            )

    def test_legacy_pages_without_metadata_keep_page_number_references(self):
        pages, _ = self._process([_png("white")], deck=None)
        self.assertEqual(len(pages), 1)
        self.assertNotIn("entity_id", pages[0])
        with tempfile.TemporaryDirectory() as directory:
            store = self._store_with_pages(directory, pages)
            done = store.get_done_ppt_pages("lecture")
            self.assertEqual(done[0]["page_num"], 1)
            self.assertNotIn("event_id", done[0])
            index = LearningSearchIndex(
                store,
                catalog_snapshot=lambda: {},
                subtitle_sync=lambda sub_id: None,
            )
            catalog = {
                "course_title": "course title", "lecture_title": "lecture title",
                "teacher": "", "catalog_version": "v1",
            }
            documents = index._documents_for("lecture", catalog, "ver")
            ppt_docs = [document for document in documents if document.source == "ppt"]
            self.assertEqual([document.source_ref for document in ppt_docs], ["1"])
            self.assertFalse(any(is_evidence_id(document.source_ref) for document in ppt_docs))

    def test_ppt_metadata_is_bounded_to_valid_identities(self):
        page = {
            "entity_id": "not-an-id",
            "event_id": "slevt:0123456789ab",
            "deck_id": "deck-ZZZ",
            "source_sha256": "nope",
            "url": "https://secret.example/cover",
            "title": "private course title",
            "account": "someone",
        }
        metadata = _ppt_evidence_metadata(page)
        self.assertEqual(metadata, {"event_id": "slevt:0123456789ab"})

    def test_old_schema_upgrades_in_place_without_row_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "learning.db"
            legacy = sqlite3.connect(path)
            legacy.executescript(
                """
                CREATE TABLE ppt_pages (
                    sub_id TEXT NOT NULL,
                    page_num INTEGER NOT NULL,
                    created_sec INTEGER NOT NULL,
                    pptimgurl TEXT,
                    text TEXT,
                    ocr_status TEXT NOT NULL DEFAULT 'pending',
                    ocr_at REAL,
                    dhash TEXT,
                    PRIMARY KEY(sub_id, page_num)
                );
                INSERT INTO ppt_pages VALUES('legacy',1,9,'','old text','done',1.0,'abcd');
                INSERT INTO ppt_pages VALUES('legacy',2,19,'','kept text','done',2.0,'ef01');
                """
            )
            legacy.commit()
            legacy.close()

            store = LearningStore(path)
            store.import_remote_summary(
                course_id="course",
                sub_id="legacy",
                input_hash="a" * 64,
                model="model",
                markdown="summary",
                chapters=[],
                ppt_pages=[{
                    "page_num": 1, "created_sec": 9, "text": "imported text",
                    "dhash": "abcd", "source_sha256": "b" * 64,
                    "entity_id": "slent:0123456789ab",
                    "event_id": "slevt:0123456789ab",
                    "deck_id": DECK["deck_id"],
                }],
            )
            done = store.get_done_ppt_pages("legacy")
            self.assertEqual(len(done), 2)
            updated = next(page for page in done if page["page_num"] == 1)
            untouched = next(page for page in done if page["page_num"] == 2)
            self.assertEqual(updated["text"], "imported text")
            self.assertEqual(updated["entity_id"], "slent:0123456789ab")
            self.assertEqual(updated["content_sha256"], "b" * 64)
            self.assertEqual(untouched["text"], "kept text")
            self.assertNotIn("entity_id", untouched)
            check = sqlite3.connect(path)
            columns = {row[1] for row in check.execute("PRAGMA table_info(ppt_pages)")}
            check.close()
            self.assertIn("evidence_json", columns)

    def test_initializer_adds_the_column_to_a_fresh_database(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(Path(directory) / "learning.db")
            initialize_learning_schema(db)
            columns = {row[1] for row in db.execute("PRAGMA table_info(ppt_pages)")}
            db.close()
        self.assertIn("evidence_json", columns)


if __name__ == "__main__":
    unittest.main()
