"""U8 资料分类学：类型/scope 加性迁移、闭集校验、课程级导入、扫描披露。"""

from __future__ import annotations

import base64
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.runtime.document_alignment import (
    DOCUMENT_SCOPES,
    DOCUMENT_TYPES,
    DocumentError,
    register_document,
)
from src.runtime.learning_schema import initialize_learning_schema
from src.runtime.materials_center import materials_center_scan


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


class DocumentTaxonomyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_additive_migration_adds_type_and_scope_columns(self):
        path = self.root / "learning.db"
        with closing(sqlite3.connect(path)) as db, db:
            # 旧形状（无 doc_type/scope）：模拟既有用户库
            db.execute(
                """CREATE TABLE learning_documents (
                    document_id TEXT PRIMARY KEY, course_id TEXT NOT NULL,
                    sub_id TEXT NOT NULL, title TEXT NOT NULL,
                    original_name TEXT NOT NULL, extension TEXT NOT NULL,
                    media_type TEXT NOT NULL DEFAULT '', storage_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    page_count INTEGER NOT NULL DEFAULT 0,
                    extraction_state TEXT NOT NULL, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL)"""
            )
            db.execute(
                "INSERT INTO learning_documents VALUES('doc-old','c1','s1','旧资料','old.txt',"
                "'.txt','','old.txt','sha',1,0,'ready',1,2)"
            )
        with closing(sqlite3.connect(path)) as db, db:
            initialize_learning_schema(db)
        with closing(sqlite3.connect(path)) as db, db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(learning_documents)").fetchall()}
            row = db.execute("SELECT doc_type, scope FROM learning_documents WHERE document_id='doc-old'").fetchone()
        self.assertIn("doc_type", columns)
        self.assertIn("scope", columns)
        self.assertEqual(row, ("other", "lecture"), "旧资料默认 lecture+other，行数据不动")

    def test_register_document_closed_sets_and_course_scope(self):
        path = self.root / "learning.db"
        with closing(sqlite3.connect(path)) as db, db:
            initialize_learning_schema(db)
        raw = _b64("第一讲：极限与连续性".encode("utf-8"))
        common = dict(
            db_path=path, storage_root=self.root, course_id="c1", sub_id="s1",
            title="讲义", original_name="notes.txt", media_type="text/plain",
            content_base64=raw,
        )
        for bad_type in ("magic", "真题"):  # 非法枚举；空串按设计回落 other
            with self.assertRaises(DocumentError):
                register_document(**common, doc_type=bad_type)
        for bad_scope in ("galaxy", "shared"):
            with self.assertRaises(DocumentError):
                register_document(**common, scope=bad_scope)
        document = register_document(**common, doc_type="exam_paper", scope="course")
        self.assertEqual(document["doc_type"], "exam_paper")
        self.assertEqual(document["scope"], "course")
        self.assertEqual(document["sub_id"], "", "课程级资料不挂讲次")
        with closing(sqlite3.connect(path)) as db, db:
            row = db.execute("SELECT doc_type, scope, sub_id FROM learning_documents").fetchone()
        self.assertEqual(row, ("exam_paper", "course", ""))

    def test_scan_exposes_type_and_scope(self):
        path = self.root / "learning.db"
        with closing(sqlite3.connect(path)) as db, db:
            initialize_learning_schema(db)
        register_document(
            path, self.root, course_id="c1", sub_id="s1", title="历年真题",
            original_name="exam.txt", media_type="text/plain",
            content_base64=_b64("第1题 ……".encode("utf-8")),
            doc_type="exam_paper", scope="lecture",
        )
        from src.runtime.learning_store import LearningStore
        from src.runtime.task_store import TaskStore

        catalog = type("_C", (), {"lecture_course_pairs": lambda self: {}})()
        value = materials_center_scan(
            data_root=path.parent,
            catalog_repository=catalog,
            learning_store=LearningStore(path),
            task_store=TaskStore(self.root / "state.db"),
        )
        document_entries = [e for e in value["entries"] if e["kind"] == "document"]
        self.assertTrue(document_entries)
        self.assertEqual(document_entries[0]["doc_type"], "exam_paper")
        self.assertEqual(document_entries[0]["scope"], "lecture")


if __name__ == "__main__":
    unittest.main()
