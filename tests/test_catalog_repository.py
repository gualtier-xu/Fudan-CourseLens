import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.runtime.catalog_repository import CatalogRepository


class CatalogRepositoryTests(unittest.TestCase):
    def test_new_catalog_has_versioned_state_and_no_json_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = CatalogRepository(Path(directory) / "state.db")
            repository.upsert_course("1", "Course")
            repository.upsert_lecture("1", {"sub_id": "2", "sub_title": "Lecture"})
            self.assertEqual(repository.schema_version(), 2)
            snapshot = repository.snapshot()
            self.assertEqual(snapshot["courses"]["1"]["title"], "Course")
            self.assertNotIn("manifest.json", snapshot)

    def test_v1_download_columns_are_removed_without_losing_catalog_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            db = sqlite3.connect(path)
            try:
                db.executescript(
                    """
                    CREATE TABLE catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO catalog_meta VALUES('schema_version','1');
                    CREATE TABLE catalog_courses (
                        course_id TEXT PRIMARY KEY, title TEXT NOT NULL, teacher TEXT NOT NULL DEFAULT '',
                        term TEXT NOT NULL DEFAULT '', department TEXT NOT NULL DEFAULT '',
                        authorization_state TEXT NOT NULL DEFAULT 'unknown', updated_at TEXT NOT NULL
                    );
                    INSERT INTO catalog_courses VALUES('1','Course','','','','verified','now');
                    CREATE TABLE catalog_lectures (
                        sub_id TEXT PRIMARY KEY, course_id TEXT NOT NULL, sub_title TEXT NOT NULL DEFAULT '',
                        date TEXT NOT NULL DEFAULT '', has_playback INTEGER NOT NULL DEFAULT 1,
                        status TEXT, file_path TEXT, error TEXT, size_bytes INTEGER,
                        progress_percent REAL, progress_label TEXT, downloaded_bytes INTEGER,
                        metadata_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
                    );
                    INSERT INTO catalog_lectures VALUES(
                        '2','1','Lecture','',1,'done','private.mp4','',7,100,'done',7,'{}','now'
                    );
                    """
                )
                db.commit()
            finally:
                db.close()
            repository = CatalogRepository(path)
            db = repository._connect()
            try:
                columns = {
                    row[1]
                    for row in db.execute("PRAGMA table_info(catalog_lectures)")
                }
            finally:
                db.close()
            self.assertFalse(columns.intersection({
                "status", "file_path", "error", "size_bytes", "progress_percent",
                "progress_label", "downloaded_bytes",
            }))
            self.assertEqual(repository.get_lecture("2")["sub_title"], "Lecture")
            self.assertEqual(repository.schema_version(), 2)

    def test_sidecar_json_is_never_imported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "manifest.json"
            legacy.write_text('{"courses":{"legacy":{"title":"Legacy"}}}', encoding="utf-8")
            repository = CatalogRepository(root / "state.db")
            self.assertEqual(repository.courses(), [])
            self.assertTrue(legacy.exists())
            repository.close()

    def test_snapshot_uses_one_connection_for_all_courses(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = CatalogRepository(Path(directory) / "state.db")
            for index in range(20):
                repository.upsert_course(str(index), f"Course {index}")
                repository.upsert_lecture(str(index), {"sub_id": f"lecture-{index}"})
            calls = 0
            original = repository._connect

            def counted_connect():
                nonlocal calls
                calls += 1
                return original()

            repository._connect = counted_connect
            snapshot = repository.snapshot()
            self.assertEqual(len(snapshot["courses"]), 20)
            self.assertEqual(len(snapshot["lectures"]), 20)
            self.assertEqual(calls, 1)

    def test_identity_scoped_catalog_uses_one_connection_and_excludes_other_courses(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = CatalogRepository(Path(directory) / "state.db")
            for index in range(3):
                repository.upsert_course(str(index), f"Course {index}")
                repository.upsert_lecture(str(index), {"sub_id": f"lecture-{index}"})
            calls = 0
            original = repository._connect

            def counted_connect():
                nonlocal calls
                calls += 1
                return original()

            repository._connect = counted_connect
            courses = repository.courses_for_ids({"0", "2", "missing"})
            self.assertEqual([item["course_id"] for item in courses], ["0", "2"])
            self.assertEqual(
                [item["lectures"][0]["sub_id"] for item in courses],
                ["lecture-0", "lecture-2"],
            )
            self.assertEqual(calls, 1)

    def test_verified_catalog_replacement_is_atomic_and_removes_stale_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = CatalogRepository(Path(directory) / "state.db")
            repository.upsert_course("stale", "Stale")
            repository.upsert_lecture("stale", {"sub_id": "stale-lecture"})
            repository.replace_authorized_catalog([{
                "course_id": "current",
                "title": "Current",
                "authorization_state": "verified",
                "lectures": [{"sub_id": "current-lecture", "has_playback": True}],
            }])
            self.assertEqual([item["course_id"] for item in repository.courses()], ["current"])
            self.assertIsNone(repository.get_lecture("stale-lecture"))
            self.assertEqual(repository.get_lecture("current-lecture")["course_id"], "current")

            with self.assertRaises(ValueError):
                repository.replace_authorized_catalog([{
                    "course_id": "rejected",
                    "authorization_state": "unknown",
                    "lectures": [],
                }])
            self.assertEqual([item["course_id"] for item in repository.courses()], ["current"])

    def test_non_dict_course_entry_fails_closed_with_closed_set_message(self):
        # 夜10-C T17：非 dict 课程条目曾以 dict() 内部消息逃逸闭集文案。
        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as directory:
            repository = CatalogRepository(Path(directory) / "state.db")
            with self.assertRaises(ValueError) as caught:
                repository.replace_authorized_catalog(["not-a-dict"])
            self.assertIn("invalid or duplicate course", str(caught.exception))
            with self.assertRaises(ValueError) as caught_none:
                repository.replace_authorized_catalog([None])
            self.assertIn("invalid or duplicate course", str(caught_none.exception))


if __name__ == "__main__":
    unittest.main()
