from __future__ import annotations

import base64
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from pypdf import PdfWriter
from src.application import CourseLensApplication

from src.runtime.document_alignment import (
    DocumentError,
    align_document,
    delete_document,
    ensure_document_schema,
    extract_pages,
    get_document,
    list_documents,
    register_document,
    update_alignment,
)
from src.runtime.student_features import ensure_student_feature_schema
from src.runtime.learning_store import LearningStore
from src.runtime.search_index import LearningSearchIndex


def _pptx_bytes(slides: list[str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for index, text in enumerate(slides, start=1):
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                (
                    '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                    f"<p:cSld><a:t>{text}</a:t></p:cSld></p:sld>"
                ).encode("utf-8"),
            )
    return output.getvalue()


class DocumentExtractionTests(unittest.TestCase):
    def test_pptx_text_and_empty_slide_are_classified(self):
        pages = extract_pages(_pptx_bytes(["矩阵特征值与线性变换", ""]), ".pptx")
        self.assertEqual(pages[0]["text"], "矩阵特征值与线性变换")
        self.assertEqual(pages[0]["extraction_state"], "ready")
        self.assertEqual(pages[1]["extraction_state"], "ocr_required")

    def test_scanned_pdf_is_not_reported_as_text_ready(self):
        output = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=320, height=240)
        writer.write(output)
        pages = extract_pages(output.getvalue(), ".pdf")
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["extraction_state"], "ocr_required")
        self.assertEqual(pages[0]["text"], "")

    def test_legacy_binary_ppt_is_rejected(self):
        with self.assertRaises(DocumentError) as caught:
            extract_pages(b"legacy", ".ppt")
        self.assertEqual(caught.exception.code, "document_type_unsupported")


class DocumentAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "learning.db"
        ensure_student_feature_schema(self.db_path)
        ensure_document_schema(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_service_import_keeps_document_when_transcript_is_unavailable(self):
        service = CourseLensApplication.__new__(CourseLensApplication)
        service.learning_store = LearningStore(self.db_path)
        service.search_index = SimpleNamespace(request_refresh=lambda *_args, **_kwargs: None)

        def unavailable(_sub_id):
            raise FileNotFoundError("subtitle unavailable")

        service.subtitle_segments = unavailable
        document = service.import_learning_document(
            course_id="course-1",
            sub_id="lecture-missing",
            title="无字幕讲义",
            original_name="notes.txt",
            media_type="text/plain",
            content_base64=base64.b64encode("尚未对齐的讲义".encode("utf-8")).decode("ascii"),
        )
        self.assertEqual(document["sub_id"], "lecture-missing")
        self.assertIsNone(document["pages"][0]["alignment_id"])

    def _register(self, text: str, name: str = "notes.txt") -> dict:
        return register_document(
            self.db_path,
            self.root,
            course_id="course-1",
            sub_id="lecture-1",
            title="线性代数讲义",
            original_name=name,
            media_type="text/plain",
            content_base64=base64.b64encode(text.encode("utf-8")).decode("ascii"),
        )

    def test_register_is_hash_idempotent_and_hides_storage_path(self):
        first = self._register("矩阵乘法与线性映射")
        second = self._register("矩阵乘法与线性映射")
        self.assertEqual(first["document_id"], second["document_id"])
        self.assertNotIn("storage_path", first)
        self.assertEqual(len(list_documents(self.db_path, sub_id="lecture-1")), 1)
        stored = list((self.root / "documents" / first["document_id"]).iterdir())
        self.assertEqual([path.name for path in stored], ["original.txt"])

    def test_monotonic_alignment_and_manual_confirmation(self):
        document = self._register(
            "矩阵 特征值 特征向量 线性变换 重点\f"
            "概率 分布 期望 方差 随机变量 考试重点"
        )
        segments = [
            {"start_ms": 0, "end_ms": 15000, "text": "今天讲矩阵和线性变换，重点是特征值与特征向量"},
            {"start_ms": 60000, "end_ms": 76000, "text": "接下来讲随机变量的概率分布、期望和方差，这是考试重点"},
        ]
        result = align_document(self.db_path, document["document_id"], segments)
        self.assertEqual(result["page_count"], 2)
        self.assertLessEqual(result["alignments"][0]["start_ms"], result["alignments"][1]["start_ms"])
        page = get_document(self.db_path, document["document_id"])["pages"][0]
        updated = update_alignment(
            self.db_path,
            alignment_id=page["alignment_id"],
            start_ms=12000,
            end_ms=18000,
            status="confirmed",
        )
        self.assertEqual(updated["status"], "confirmed")
        self.assertEqual(updated["confidence"], 1.0)
        self.assertTrue(updated["evidence"]["manual"])

    def test_duplicate_and_empty_pages_require_review(self):
        raw = _pptx_bytes(["完全相同的重复页面", "完全相同的重复页面", ""])
        document = register_document(
            self.db_path,
            self.root,
            course_id="course-1",
            sub_id="lecture-1",
            title="重复页",
            original_name="slides.pptx",
            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
            content_base64=base64.b64encode(raw).decode("ascii"),
        )
        result = align_document(self.db_path, document["document_id"], [
            {"start_ms": 0, "end_ms": 10000, "text": "完全相同的重复页面"},
        ])
        self.assertEqual([item["status"] for item in result["alignments"]], [
            "review_required", "review_required", "review_required",
        ])
        self.assertEqual(result["alignments"][2]["evidence"]["reason"], "page_text_unavailable")

    def test_delete_removes_database_rows_before_local_directory(self):
        document = self._register("可以删除的本地讲义")
        directory = delete_document(self.db_path, document["document_id"])
        self.assertTrue(directory.is_dir())
        self.assertEqual(list_documents(self.db_path), [])

    def test_confirmed_document_page_is_available_to_local_search(self):
        document = self._register("矩阵谱分解与特征向量")
        align_document(self.db_path, document["document_id"], [
            {"start_ms": 12000, "end_ms": 22000, "text": "矩阵谱分解与特征向量"},
        ])
        page = get_document(self.db_path, document["document_id"])["pages"][0]
        update_alignment(
            self.db_path,
            alignment_id=page["alignment_id"],
            start_ms=12000,
            end_ms=22000,
            status="confirmed",
        )
        store = LearningStore(self.db_path)
        index = LearningSearchIndex(
            store,
            lambda: {"authorization_state": "ready", "courses": {}, "lectures": {}},
            lambda _sub_id: {},
        )
        documents = index._documents_for(
            "lecture-1",
            {"course_title": "线性代数", "lecture_title": "矩阵", "teacher": "教师", "catalog_version": "v1"},
            "indexed-v1",
        )
        matched = [item for item in documents if item.source == "document"]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].start_ms, 12000)

    def test_search_catalog_pruning_is_bulk_and_removes_derived_documents(self):
        store = LearningStore(self.db_path)
        rows = [
            {
                "sub_id": f"lecture-{index}",
                "course_id": "course",
                "course_title": "Course",
                "lecture_title": f"Lecture {index}",
                "teacher": "Teacher",
                "catalog_version": "v1",
            }
            for index in range(3)
        ]
        store.sync_search_catalog(rows)
        for row in rows:
            store.replace_search_documents(
                row["sub_id"],
                [{
                    "doc_key": f"title:{row['sub_id']}:catalog",
                    "source": "title",
                    "source_ref": "catalog",
                    "document_title": row["lecture_title"],
                    "display_text": row["lecture_title"],
                    "search_text": row["lecture_title"].casefold(),
                    "source_version": "v1",
                }],
                indexed_version="v1",
            )
        store._delete_search_documents = lambda *_args: (_ for _ in ()).throw(
            AssertionError("pruning must use one bulk transaction")
        )
        store.sync_search_catalog(rows[:1])
        with store._connect() as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM search_catalog").fetchone()[0], 1)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM search_documents").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
