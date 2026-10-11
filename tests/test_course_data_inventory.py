"""Synthetic-fixture tests for the frozen course-data inventory store layer."""

import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from src.runtime.catalog_repository import CatalogRepository
from src.runtime.course_data_inventory import (
    ACTION_RESULT_SCHEMA,
    BYTE_BASIS,
    COURSE_DATA_ACTIONS,
    COURSE_DATA_BLOCKER_CODES,
    COURSE_DATA_CATEGORIES,
    LECTURE_PAGE_SCHEMA,
    SUMMARY_SCHEMA,
    CourseDataInventory,
    courseware_lecture_key,
    subtitle_artifact_key,
)
from src.runtime.learning_store import LearningStore
from src.runtime.student_features import ensure_student_feature_schema
from src.runtime.task_store import TaskStore


C1 = "course-1"
S1 = "lecture-1"
S2 = "lecture-2"
GHOST_SUB = "ghost-lecture"
ORPHAN_COURSE = "course-orphan"


def _document_id(course_id: str, sub_id: str, digest: str) -> str:
    return hashlib.sha256(f"{course_id}:{sub_id}:{digest}".encode("utf-8")).hexdigest()[:32]


class CourseDataInventoryFixture:
    """Synthetic two-database workspace; no real data is ever touched."""

    def __init__(self, root: Path, *, with_student_features: bool = True):
        self.root = root
        self.catalog = CatalogRepository(root / "state.db")
        self.tasks = TaskStore(root / "state.db")
        self.learning = LearningStore(root / "learning.db")
        if with_student_features:
            ensure_student_feature_schema(root / "learning.db")
        self.raw_state = sqlite3.connect(root / "state.db")
        self.raw_learning = sqlite3.connect(root / "learning.db")

    def close(self) -> None:
        self.raw_state.commit()
        self.raw_learning.commit()
        self.raw_state.close()
        self.raw_learning.close()
        self.catalog.close()
        self.tasks.close()
        self.learning.close()

    def seed_catalog(self) -> None:
        self.catalog.upsert_course(C1, "Course One", teacher="Teacher")
        self.catalog.upsert_lecture(C1, {"sub_id": S1, "sub_title": "Lecture One", "date": "2026-09-01"})
        self.catalog.upsert_lecture(C1, {"sub_id": S2, "sub_title": "Lecture Two", "date": "2026-09-02"})

    def seed_learning(self) -> None:
        self.learning.save_watch_progress(
            course_id=C1, sub_id=S1, position_seconds=30, duration_seconds=100,
        )
        self.learning.replace_transcript_segments(
            S1,
            source_path="synthetic.srt",
            source_mtime_ns=1,
            source_size=11,
            segments=[{"start_ms": 0, "end_ms": 1000, "text": "hello world"}],
        )
        self.learning.insert_ppt_pages_pending(S1, [{"page_num": 1, "created_sec": 0}])
        self.learning.update_ppt_page(S1, 1, "slide text", "done")
        artifact = self.learning.begin_ai_artifact(
            course_id=C1, sub_id=S1, kind="lecture_summary",
            input_hash="a" * 64, prompt_version="v1",
        )
        self.learning.complete_ai_artifact(
            artifact["artifact_id"], model="synthetic", markdown="# summary",
            content={"k": 1},
        )
        self.raw_learning.execute(
            """INSERT INTO bookmarks(bookmark_id,course_id,sub_id,start_ms,end_ms,note,status,created_at,updated_at)
               VALUES('b1',?,?,0,10,'note','open',1.0,2.0)""",
            (C1, S1),
        )
        self.raw_learning.execute(
            """INSERT INTO quiz_items(quiz_id,course_id,sub_id,question_type,question,answer,explanation,created_at)
               VALUES('q1',?,?,'choice','What is X?','X','Because Y',3.0)""",
            (C1, S1),
        )
        self.raw_learning.execute(
            "INSERT INTO quiz_attempts(attempt_id,quiz_id,answer,correct,confidence,created_at)"
            " VALUES('a1','q1','X',1,4,4.0)"
        )
        self.raw_learning.execute(
            "INSERT INTO review_plans(plan_id,title,exam_at,available_minutes,updated_at)"
            " VALUES('p1','plan',99.0,30,5.0)"
        )
        digest = hashlib.sha256(b"document bytes").hexdigest()
        document = _document_id(C1, S1, digest)
        directory = self.root / "documents" / document
        directory.mkdir(parents=True)
        (directory / "original.pdf").write_bytes(b"D" * 40)
        self.raw_learning.execute(
            """INSERT INTO learning_documents(
                   document_id,course_id,sub_id,title,original_name,extension,media_type,
                   storage_path,sha256,size_bytes,page_count,extraction_state,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                document, C1, S1, "Doc", "original.pdf", ".pdf", "application/pdf",
                str(directory / "original.pdf"), digest, 40, 1, "ready", 1.0, 2.0,
            ),
        )
        self.raw_learning.commit()

    def seed_task(self) -> None:
        self.tasks.add_task("subtitle", C1, S1, {"source": "synthetic"})

    def seed_automation_rule(self) -> None:
        self.tasks.replace_automation_rules([{"course_id": C1, "enabled": True}])

    def seed_files(self) -> None:
        subtitle_dir = self.root / "artifacts" / "subtitles" / subtitle_artifact_key(C1, S1)
        subtitle_dir.mkdir(parents=True)
        (subtitle_dir / "subtitle.srt").write_bytes(b"S" * 25)
        courseware_dir = self.root / "courseware" / courseware_lecture_key(C1, S1)
        courseware_dir.mkdir(parents=True)
        (courseware_dir / "slides.pdf").write_bytes(b"C" * 70)
        orphan_documents = self.root / "documents" / ("0" * 32)
        orphan_documents.mkdir(parents=True)
        (orphan_documents / "original.pdf").write_bytes(b"O" * 11)
        orphan_subtitles = self.root / "artifacts" / "subtitles" / ("f" * 64)
        orphan_subtitles.mkdir(parents=True)
        (orphan_subtitles / "subtitle.srt").write_bytes(b"O" * 13)
        orphan_courseware = self.root / "courseware" / ("lec-" + "f" * 16)
        orphan_courseware.mkdir(parents=True)
        (orphan_courseware / "slides.pdf").write_bytes(b"O" * 17)
        malformed = self.root / "artifacts" / "subtitles" / "not-a-hash"
        malformed.mkdir(parents=True)

    def seed_blockers(self) -> None:
        self.raw_state.execute(
            """INSERT INTO remote_runs(task_id,repository,workflow,remote_state,updated_at)
               VALUES('task-remote','repo','wf','running',1.0)"""
        )
        self.raw_state.execute(
            "INSERT INTO remote_run_attempts(task_id,attempt,cleanup_state,updated_at)"
            " VALUES('task-remote',1,'pending',1.0)"
        )
        self.raw_state.execute(
            "INSERT INTO automation_imports(artifact_id,state,observed_at,updated_at)"
            " VALUES(123,'downloading',1.0,1.0)"
        )
        self.raw_state.commit()


class CourseDataInventoryTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self):
        self._temporary.cleanup()

    def _inventory(self, fixture: CourseDataInventoryFixture) -> CourseDataInventory:
        return CourseDataInventory(
            learning_store=fixture.learning,
            catalog_repository=fixture.catalog,
            task_store=fixture.tasks,
            data_root=self.root,
        )

    def test_quiz_text_bytes_counts_items_once_and_attempts_individually(self):
        """COUNTS-VERIFY-1 F1（P2）：quiz 字符量曾按 LEFT JOIN 联表行数（=作答数）
        重复计入题干/解析——两次作答虚增恰好一倍题干+解析，随作答数线性放大。
        修复后：题干/解析恰计一次，作答内容逐条计入。"""
        fixture = CourseDataInventoryFixture(self.root)
        try:
            fixture.seed_catalog()
            fixture.seed_learning()
            fixture.seed_task()
            fixture.seed_automation_rule()
            fixture.seed_files()
            fixture.raw_learning.execute(
                "INSERT INTO quiz_attempts(attempt_id,quiz_id,answer,correct,confidence,created_at)"
                " VALUES('a2','q1','X again',0,2,5.0)"
            )
            fixture.raw_learning.commit()
            summary = self._inventory(fixture).summary()
            row = summary["rows"][0]
            quizzes = row["categories"]["quizzes"]
            self.assertEqual(quizzes["count"], 1, "条数不受作答数影响")
            expected = (
                len("What is X?") + len("Because Y")
                + len("X") + len("X again")
            )
            self.assertEqual(
                quizzes["text_bytes"], expected,
                "题干/解析恰一次+作答逐条（不再随作答数虚增）",
            )
        finally:
            fixture.close()

    def test_summary_aggregates_synthetic_categories_and_files(self):
        fixture = CourseDataInventoryFixture(self.root)
        try:
            fixture.seed_catalog()
            fixture.seed_learning()
            fixture.seed_task()
            fixture.seed_automation_rule()
            fixture.seed_files()
            summary = self._inventory(fixture).summary()
            self.assertEqual(summary["schema"], SUMMARY_SCHEMA)
            self.assertEqual(summary["byte_basis"], BYTE_BASIS)
            self.assertEqual(summary["page"]["total"], 1)
            row = summary["rows"][0]
            self.assertEqual(row["course_id"], C1)
            self.assertTrue(row["in_catalog"])
            self.assertEqual(row["title"], "Course One")
            self.assertEqual(row["teacher"], "Teacher")
            self.assertEqual(row["lecture_count"], 2)
            categories = row["categories"]
            self.assertEqual(categories["progress"]["count"], 1)
            self.assertEqual(categories["transcript"]["count"], 1)
            self.assertEqual(categories["transcript"]["text_bytes"], len("hello world"))
            self.assertEqual(categories["ppt"]["count"], 1)
            self.assertEqual(categories["ppt"]["text_bytes"], len("slide text"))
            expected_artifact_bytes = len("# summary") + len(json.dumps({"k": 1}))
            self.assertEqual(categories["artifacts"]["text_bytes"], expected_artifact_bytes)
            self.assertEqual(categories["documents"]["count"], 1)
            self.assertEqual(categories["bookmarks"]["count"], 1)
            self.assertEqual(categories["quizzes"]["count"], 1)
            self.assertEqual(
                categories["quizzes"]["text_bytes"],
                len("What is X?") + len("Because Y") + len("X"),
            )
            self.assertEqual(categories["tasks"]["count"], 1)
            self.assertEqual(categories["automation"]["count"], 1)
            self.assertEqual(
                sorted(categories), sorted([
                    "progress", "transcript", "ppt", "artifacts", "documents",
                    "bookmarks", "quizzes", "tasks", "automation",
                ]),
            )
            self.assertEqual(row["file_bytes"], {"documents": 40, "subtitles": 25, "courseware": 70})
            self.assertEqual(row["total_file_bytes"], 135)
            self.assertEqual(summary["orphan_artifacts"], {"directories": 4, "bytes": 11 + 13 + 17})
            self.assertGreater(summary["database_bytes"]["total"], 0)
            self.assertEqual(
                summary["database_bytes"]["total"],
                summary["database_bytes"]["state_db"] + summary["database_bytes"]["learning_db"],
            )
            self.assertEqual(summary["unattributed"]["categories"]["review"]["count"], 1)
        finally:
            fixture.close()

    def test_orphan_courses_detected_with_not_exists_semantics(self):
        fixture = CourseDataInventoryFixture(self.root)
        try:
            fixture.seed_catalog()
            fixture.learning.begin_ai_artifact(
                course_id=ORPHAN_COURSE, sub_id="orphan-lecture", kind="lecture_summary",
                input_hash="b" * 64, prompt_version="v1",
            )
            inventory = self._inventory(fixture)
            without_orphans = inventory.summary()
            self.assertEqual(
                [row["course_id"] for row in without_orphans["rows"]], [C1],
            )
            with_orphans = inventory.summary(include_orphans=True)
            self.assertEqual(
                [row["course_id"] for row in with_orphans["rows"]], [C1, ORPHAN_COURSE],
            )
            orphan_row = with_orphans["rows"][1]
            self.assertFalse(orphan_row["in_catalog"])
            self.assertNotIn("title", orphan_row)
            self.assertNotIn("teacher", orphan_row)
            self.assertNotIn("lecture_count", orphan_row)
            self.assertEqual(orphan_row["categories"]["artifacts"]["count"], 1)
        finally:
            fixture.close()

    def test_unresolvable_sub_lectures_land_in_unattributed(self):
        fixture = CourseDataInventoryFixture(self.root)
        try:
            fixture.seed_catalog()
            fixture.learning.replace_transcript_segments(
                GHOST_SUB,
                source_path="ghost.srt",
                source_mtime_ns=1,
                source_size=5,
                segments=[{"start_ms": 0, "end_ms": 10, "text": "ghost"}],
            )
            summary = self._inventory(fixture).summary()
            self.assertEqual(
                summary["unattributed"]["categories"]["transcript"]["count"], 1,
            )
            self.assertEqual(
                [row["course_id"] for row in summary["rows"]], [C1],
            )
            ghost_course = None
            for row in summary["rows"]:
                if row.get("categories", {}).get("transcript"):
                    ghost_course = row
            self.assertIsNone(ghost_course)
        finally:
            fixture.close()

    def test_not_understood_hotspot_counts_are_additive_row_fields(self):
        """POLISH-1 F8b（化身走查 F8b）：「没听懂」热点计数随行出场（可选字段，
        不进冻结 categories 闭集）；普通书签不混入；零热点课程无该键；删除
        书签即回落；缺表诚实空表。"""
        fixture = CourseDataInventoryFixture(self.root)
        try:
            fixture.seed_catalog()
            fixture.seed_learning()
            inventory = self._inventory(fixture)
            summary = inventory.summary()
            row = summary["rows"][0]
            self.assertNotIn("not_understood_count", row, "零热点课程不出现热点字段")
            fixture.raw_learning.execute(
                """INSERT INTO bookmarks(bookmark_id,course_id,sub_id,start_ms,end_ms,note,status,created_at,updated_at)
                   VALUES('h1',?,?,0,5,'没听懂','open',3.0,4.0)""",
                (C1, S1),
            )
            fixture.raw_learning.execute(
                """INSERT INTO bookmarks(bookmark_id,course_id,sub_id,start_ms,end_ms,note,status,created_at,updated_at)
                   VALUES('h2',?,?,6,9,'没听懂','open',3.0,4.0)""",
                (C1, S2),
            )
            fixture.raw_learning.commit()
            counts = fixture.learning.not_understood_bookmark_counts()
            self.assertEqual(counts, {C1: 2})
            summary = inventory.summary()
            row = summary["rows"][0]
            self.assertEqual(row["not_understood_count"], 2, "普通书签（note='note'）不混入热点计数")
            self.assertNotIn("hotspots", row.get("categories", {}), "热点不进冻结 categories 闭集")
            # 硬删即回落
            fixture.raw_learning.execute("DELETE FROM bookmarks WHERE bookmark_id IN ('h1','h2')")
            fixture.raw_learning.commit()
            self.assertEqual(fixture.learning.not_understood_bookmark_counts(), {})
            self.assertNotIn("not_understood_count", inventory.summary()["rows"][0])
        finally:
            fixture.close()

    def test_not_understood_counts_survive_missing_optional_schema(self):
        """可选 student-features schema 缺表（with_student_features=False）时，
        计数诚实返回空表而非抛错——沿用聚合面既有 OperationalError 降级语义。"""
        fixture = CourseDataInventoryFixture(self.root, with_student_features=False)
        try:
            self.assertEqual(fixture.learning.not_understood_bookmark_counts(), {})
        finally:
            fixture.close()

    def test_stored_text_bytes_are_additive_row_fields(self):
        """AVATAR-POLISH-1 UP-G1（化身走查 S1 升级观察）：页头「数据 X」的逐课
        去向——随行补 stored_text_bytes（=该课各类别 text_bytes 合计，additive
        可选字段，不进冻结 categories 闭集）；零存量课程无该键；数值与行内
        categories 栅格同源可对账；与页头 database_bytes（sqlite 文件字节，含
        索引/空闲页）不同基，只做行内合计，不做跨基对账。"""
        fixture = CourseDataInventoryFixture(self.root)
        try:
            fixture.seed_catalog()
            summary = self._inventory(fixture).summary()
            row = summary["rows"][0]
            self.assertNotIn("stored_text_bytes", row, "零存量课程不出现该字段")
            fixture.seed_learning()
            summary = self._inventory(fixture).summary()
            row = summary["rows"][0]
            categories_sum = sum(
                int(aggregate.get("text_bytes") or 0)
                for aggregate in row["categories"].values()
            )
            self.assertGreater(categories_sum, 0, "种子数据应产出非零文字存量")
            self.assertEqual(row["stored_text_bytes"], categories_sum, "字段与行内 categories 栅格同源")
            self.assertNotIn("stored_text_bytes", row.get("categories", {}), "不进冻结 categories 闭集")
        finally:
            fixture.close()

    def test_cross_course_task_records_bucket_into_unattributed(self):
        fixture = CourseDataInventoryFixture(self.root)
        try:
            fixture.seed_catalog()
            fixture.tasks.add_task("search_answer", "1,2,3", "", {"query": "synthetic"})
            inventory = self._inventory(fixture)
            aggregates = fixture.tasks.course_task_aggregates()
            self.assertEqual(aggregates[("1,2,3", "")]["kind"], "search_answer")
            summary = inventory.summary(include_orphans=True)
            self.assertNotIn("1,2,3", [row["course_id"] for row in summary["rows"]])
            self.assertEqual(summary["unattributed"]["categories"]["tasks"]["count"], 1)
            self.assertEqual(inventory.lecture_page("1,2,3")["total"], 0)
            fixture.tasks.add_task("subtitle", C1, S1, {"source": "synthetic"})
            self.assertEqual(fixture.tasks.course_task_aggregates()[(C1, S1)]["kind"], "subtitle")
            summary = inventory.summary(include_orphans=True)
            self.assertEqual([row["course_id"] for row in summary["rows"]], [C1])
            self.assertEqual(summary["rows"][0]["categories"]["tasks"]["count"], 1)
            self.assertEqual(summary["unattributed"]["categories"]["tasks"]["count"], 1)
        finally:
            fixture.close()

    def test_lecture_page_pagination_and_orphan_lectures(self):
        fixture = CourseDataInventoryFixture(self.root)
        try:
            fixture.seed_catalog()
            fixture.learning.save_watch_progress(
                course_id=C1, sub_id="extra-lecture", position_seconds=1, duration_seconds=2,
            )
            inventory = self._inventory(fixture)
            page = inventory.lecture_page(C1)
            self.assertEqual(page["schema"], LECTURE_PAGE_SCHEMA)
            self.assertEqual(page["total"], 3)
            self.assertEqual(
                [item["sub_id"] for item in page["lectures"]],
                [S1, S2, "extra-lecture"],
            )
            self.assertTrue(page["lectures"][0]["in_catalog"])
            self.assertEqual(page["lectures"][0]["title"], "Lecture One")
            self.assertEqual(page["lectures"][0]["date"], "2026-09-01")
            clipped = inventory.lecture_page(C1, limit=2, offset=2)
            self.assertEqual([item["sub_id"] for item in clipped["lectures"]], ["extra-lecture"])
            self.assertFalse(clipped["lectures"][0]["in_catalog"])
            self.assertNotIn("title", clipped["lectures"][0])
            self.assertLessEqual(clipped["page"]["limit"], 50)
        finally:
            fixture.close()

    def test_lifecycle_blockers_reuse_existing_closed_probes(self):
        fixture = CourseDataInventoryFixture(self.root)
        try:
            fixture.seed_task()
            fixture.seed_automation_rule()
            fixture.seed_blockers()
            blockers = self._inventory(fixture).lifecycle_blockers(
                sub_ids=(S1, S2), course_ids=(C1,),
            )
            codes = {blocker["code"]: blocker for blocker in blockers}
            self.assertEqual(
                sorted(codes), sorted([
                    "active_task", "active_remote_run", "active_automation_import",
                    "automation_rule", "cleanup_pending",
                ]),
            )
            self.assertEqual(codes["active_task"]["count"], 1)
            self.assertEqual(codes["active_task"]["sub_ids"], [S1])
            self.assertEqual(codes["active_remote_run"]["count"], 1)
            self.assertEqual(codes["active_automation_import"]["count"], 1)
            self.assertEqual(codes["cleanup_pending"]["count"], 1)
            self.assertEqual(codes["automation_rule"]["count"], 1)
            self.assertEqual(codes["automation_rule"]["course_ids"], [C1])
            self.assertTrue(set(COURSE_DATA_BLOCKER_CODES).issuperset(codes))
        finally:
            fixture.close()

    def test_action_result_enforces_frozen_closed_sets(self):
        fixture = CourseDataInventoryFixture(self.root)
        try:
            inventory = self._inventory(fixture)
            accepted = inventory.action_result(
                "export", accepted=True, operation_id="op-1", result={"manifest": {}},
            )
            self.assertEqual(accepted["schema"], ACTION_RESULT_SCHEMA)
            self.assertEqual(accepted["action"], "export")
            self.assertEqual(accepted["status"], "accepted")
            self.assertEqual(accepted["operation_id"], "op-1")
            self.assertEqual(accepted["result"], {"manifest": {}})
            rejected = inventory.action_result(
                "purge-derived", accepted=False, operation_id="op-2",
                blockers=[{"code": "active_task", "count": 1}],
            )
            self.assertEqual(rejected["status"], "rejected")
            self.assertEqual(rejected["blockers"], [{"code": "active_task", "count": 1}])
            with self.assertRaises(ValueError):
                inventory.action_result("delete-everything", accepted=True, operation_id="op-3")
            with self.assertRaises(ValueError):
                inventory.action_result(
                    "export", accepted=False, operation_id="op-4",
                    blockers=[{"code": "made_up_blocker", "count": 1}],
                )
            self.assertEqual(
                COURSE_DATA_ACTIONS,
                ("rebuild-search", "purge-derived", "remove-copies", "delete-records", "export",
                 "release-stuck",  # U5/M16：解锁卡住的生命周期行
                 "export-study-stats"),  # STUDY-STATS-M3：学习统计导出（JSON+CSV×2）
            )
            self.assertEqual(sorted(COURSE_DATA_CATEGORIES), sorted([
                "progress", "transcript", "ppt", "artifacts", "documents", "references",
                "timeline", "search", "bookmarks", "quizzes", "review", "tasks", "automation",
            ]))
        finally:
            fixture.close()

    def test_missing_optional_feature_tables_are_omitted_not_fabricated(self):
        fixture = CourseDataInventoryFixture(self.root, with_student_features=False)
        try:
            fixture.seed_catalog()
            fixture.learning.save_watch_progress(
                course_id=C1, sub_id=S1, position_seconds=5, duration_seconds=10,
            )
            aggregates = fixture.learning.course_data_category_aggregates()
            self.assertIn("progress", next(iter(aggregates["course_sub"].values())))
            for absent in ("bookmarks", "quizzes"):
                self.assertTrue(all(
                    absent not in categories
                    for categories in aggregates["course_sub"].values()
                ))
            self.assertNotIn("review", aggregates["global"])
            summary = self._inventory(fixture).summary()
            row = summary["rows"][0]
            self.assertEqual(row["categories"]["progress"]["count"], 1)
            self.assertNotIn("bookmarks", row["categories"])
            self.assertNotIn("unattributed", summary)
        finally:
            fixture.close()

    def test_store_aggregate_methods_expose_read_only_shapes(self):
        fixture = CourseDataInventoryFixture(self.root)
        try:
            fixture.seed_catalog()
            fixture.seed_learning()
            fixture.seed_task()
            learning = fixture.learning.course_data_category_aggregates()
            self.assertIn((C1, S1), learning["course_sub"])
            self.assertIn(S1, learning["sub"])
            self.assertIn("transcript", learning["sub"][S1])
            document_id = _document_id(C1, S1, hashlib.sha256(b"document bytes").hexdigest())
            self.assertEqual(learning["document_ids"][document_id], (C1, S1))
            tasks = fixture.tasks.course_task_aggregates()
            self.assertEqual(tasks[(C1, S1)]["count"], 1)
            fixture.tasks.replace_automation_rules([{"course_id": C1}])
            rules = fixture.tasks.automation_course_rule_counts()
            self.assertEqual(rules[C1]["count"], 1)
            pairs = fixture.catalog.lecture_course_pairs()
            self.assertEqual(pairs[S1]["course_id"], C1)
            self.assertEqual(pairs[S2]["sub_title"], "Lecture Two")
        finally:
            fixture.close()

    def test_directory_byte_sum_rejects_paths_outside_namespace_root(self):
        fixture = CourseDataInventoryFixture(self.root)
        try:
            outside = self.root / "outside"
            outside.mkdir()
            (outside / "payload.bin").write_bytes(b"x" * 9)
            namespace_root = (self.root / "documents").resolve()
            size, safe = CourseDataInventory._directory_file_bytes(outside, namespace_root)
            self.assertEqual((size, safe), (0, False))
            inside = self.root / "documents" / ("1" * 32)
            inside.mkdir(parents=True)
            (inside / "original.pdf").write_bytes(b"y" * 7)
            size, safe = CourseDataInventory._directory_file_bytes(inside, namespace_root)
            self.assertEqual((size, safe), (7, True))
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main()
