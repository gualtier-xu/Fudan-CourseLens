"""API-layer tests for the course-data management routes (FEATURE-DATA-API-1).

Covers the frozen contract surface: course-session gate, closed pagination
bounds (lectures ≤ 50, summary ≤ 200), action closed set, operation_id
idempotency through the app-state ledger, the whole-batch lifecycle-blocker
rejection matrix, and the confirmation gradient.  All fixtures are synthetic.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.runtime.http_api import make_handler
from src.runtime.learning_store import LearningStore
from src.runtime.catalog_repository import CatalogRepository
from src.runtime.task_store import TaskStore
from src.runtime.student_features import ensure_student_feature_schema
from src.application import course_data_perform_action
from src.runtime.course_data_inventory import (
    courseware_lecture_key,
    subtitle_artifact_key,
)
from src.runtime.document_alignment import register_document
from src.runtime.study_stats import ensure_study_stats_schema


class _CourseDataService:
    """Narrow double exposing exactly the container paths the routes touch."""

    def __init__(self, root: Path):
        self.root = root
        self.catalog_repository = CatalogRepository(root / "state.db")
        self.task_store = TaskStore(root / "state.db")
        self.learning_store = LearningStore(root / "learning.db")
        ensure_student_feature_schema(root / "learning.db")
        self.learning = SimpleNamespace(
            repository=self.learning_store,
            refresh_search_index=lambda **_kwargs: {"state": "requested"},
            course_data_action=self._course_data_action,
        )
        self._auth_state = "ready"
        self.auth_catalog = SimpleNamespace(
            catalog=self.catalog_repository,
            authentication_snapshot=lambda: {"state": self._auth_state},
        )
        self.tasks = SimpleNamespace(repository=self.task_store)

    def _course_data_action(self, action: str, *, operation_id: str, course_ids=(),
                            sub_ids=(), confirm: bool = False, confirm_typed: str = "",
                            include_orphans: bool = False) -> dict:
        return course_data_perform_action(
            action,
            learning_store=self.learning_store,
            catalog_repository=self.catalog_repository,
            task_store=self.task_store,
            data_root=self.root,
            operation_id=str(operation_id),
            course_ids=tuple(course_ids),
            sub_ids=tuple(sub_ids),
            confirm=bool(confirm),
            confirm_typed=str(confirm_typed or ""),
            include_orphans=bool(include_orphans),
            refresh_search_index=lambda: self.learning.refresh_search_index(force=True),
        )

    def set_auth_state(self, state: str) -> None:
        self._auth_state = state

    def seed_course(self, course_id: str = "1", title: str = "线性代数", teacher: str = "张老师") -> None:
        self.catalog_repository.upsert_course(course_id, title, teacher=teacher)
        self.catalog_repository.upsert_lecture(course_id, {
            "sub_id": f"{course_id}-a", "sub_title": "第一讲", "date": "2026-09-01",
        })
        self.catalog_repository.upsert_lecture(course_id, {
            "sub_id": f"{course_id}-b", "sub_title": "第二讲", "date": "2026-09-08",
        })

    def seed_transcript(self, sub_id: str) -> None:
        # closing() + transaction: ``with`` on a sqlite3 connection only commits,
        # it never closes.  An unclosed connection is kept alive by its own
        # statement cache (CPython >= 3.12 backs that cache with
        # functools.lru_cache, forming a self-reference cycle), so reclamation
        # waits for a cyclic GC pass and tearDown's _tmp.cleanup() races the
        # open file handle (WinError 32).
        with closing(sqlite3.connect(self.learning_store.path)) as db, db:
            db.execute(
                "INSERT INTO transcript_sources(sub_id,source_path,source_mtime_ns,source_size,"
                "segment_count,updated_at) VALUES(?,?,?,?,?,?)",
                (sub_id, "synthetic.wav", 1, 10, 1, time.time()),
            )
            db.execute(
                "INSERT INTO transcript_segments(sub_id,segment_index,start_ms,end_ms,text,evidence_json)"
                " VALUES(?,?,?,?,?,?)",
                (sub_id, 1, 0, 1000, "合成字幕", ""),
            )

    def seed_progress_and_records(self, course_id: str, sub_id: str) -> None:
        with closing(sqlite3.connect(self.learning_store.path)) as db, db:
            db.execute(
                "INSERT INTO watch_progress(sub_id,course_id,position_ms,duration_ms,completed,"
                "playback_rate,updated_at) VALUES(?,?,?,?,?,?,?)",
                (sub_id, course_id, 1000, 2000, 0, 1.0, time.time()),
            )
            db.execute(
                "INSERT INTO bookmarks(bookmark_id,course_id,sub_id,start_ms,end_ms,note,status,"
                "explanation_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"bm-{sub_id}", course_id, sub_id, 0, 10, "", "open", "{}", time.time(), time.time()),
            )

    def seed_artifact_directories(self, course_id: str, sub_id: str) -> dict[str, Path]:
        root = self.root
        subtitle_dir = root / "artifacts" / "subtitles" / subtitle_artifact_key(course_id, sub_id)
        courseware_dir = root / "courseware" / courseware_lecture_key(course_id, sub_id)
        subtitle_dir.mkdir(parents=True)
        courseware_dir.mkdir(parents=True)
        (subtitle_dir / "segments.json").write_text("{}", encoding="utf-8")
        (courseware_dir / "slide-1.png").write_bytes(b"png")
        return {"subtitles": subtitle_dir, "courseware": courseware_dir}


class _CourseDataApiServer(ThreadingHTTPServer):
    """Handler threads must be joined before the temp tree is removed.

    ThreadingHTTPServer marks handler threads daemon, so ThreadingMixIn's
    close-time join never tracks them: tearDown's ``server_close()`` returns
    while a handler is still finishing post-response work, and its transient
    learning.db/state.db connection races ``_tmp.cleanup()`` (WinError 32
    family).  Keeping ``block_on_close`` at its True default while opting out
    of daemon mode makes ``server_close()`` join every handler thread.
    """

    daemon_threads = False


class CourseDataApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.service = _CourseDataService(root)
        self.root = root
        server = _CourseDataApiServer(("127.0.0.1", 0), make_handler(self.service, root))
        self._server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{server.server_port}"

    def tearDown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._tmp.cleanup()

    # -- helpers -------------------------------------------------------------

    def get(self, path: str):
        with urlopen(f"{self.base}{path}") as response:
            return response.status, json.loads(response.read())["data"]

    def post(self, path: str, body: dict):
        request = Request(
            f"{self.base}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request) as response:
            return response.status, json.loads(response.read())["data"]

    def expect_error(self, path: str, *, status: int, error_code: str, body: dict | None = None, method: str = "POST"):
        data = json.dumps(body or {}).encode() if method == "POST" else None
        request = Request(
            f"{self.base}{path}", data=data,
            headers={"Content-Type": "application/json"}, method=method,
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, status)
        self.assertEqual(json.loads(caught.exception.read())["error_code"], error_code)

    # -- gate ----------------------------------------------------------------

    def test_course_data_routes_join_the_course_session_gate(self):
        for state in ("idle", "checking"):
            self.service.set_auth_state(state)
            self.expect_error(
                "/api/v3/course-data", method="GET", status=401, error_code="fudan_login_required",
            )
            self.expect_error(
                "/api/v3/course-data/lectures?course_id=1", method="GET",
                status=401, error_code="fudan_login_required",
            )
            self.expect_error(
                "/api/v3/course-data/actions",
                body={"action": "export", "operation_id": "gate-op-1"},
                status=401, error_code="fudan_login_required",
            )
        self.service.set_auth_state("ready")
        status, _ = self.get("/api/v3/course-data")
        self.assertEqual(status, 200)

    # -- frozen read shapes --------------------------------------------------

    def test_summary_and_lecture_pages_return_frozen_shapes(self):
        self.service.seed_course("1", "线性代数")
        self.service.seed_course("2", "概率论")
        self.service.seed_transcript("1-a")
        self.service.seed_progress_and_records("1", "1-a")
        status, summary = self.get("/api/v3/course-data")
        self.assertEqual(status, 200)
        self.assertEqual(summary["schema"], "courselens.course-data-summary.v1")
        self.assertEqual(summary["page"], {"page": 1, "page_size": 50, "total": 2})
        row = next(item for item in summary["rows"] if item["course_id"] == "1")
        self.assertEqual(row["title"], "线性代数")
        self.assertTrue(row["in_catalog"])
        self.assertIn("transcript", row["categories"])
        self.assertIn("progress", row["categories"])
        status, page = self.get("/api/v3/course-data/lectures?course_id=1&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(page["schema"], "courselens.course-data-lecture-page.v1")
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["page"], {"limit": 1, "offset": 0})
        self.assertEqual(len(page["lectures"]), 1)
        self.assertEqual(page["lectures"][0]["sub_id"], "1-a")
        self.assertTrue(page["lectures"][0]["in_catalog"])
        status, missing = self.get("/api/v3/course-data/lectures?course_id=unknown")
        self.assertEqual(missing["in_catalog"], False)
        self.assertEqual(missing["total"], 0)
        self.assertEqual(missing["lectures"], [])

    def test_pagination_bounds_are_closed(self):
        self.service.seed_course("1")
        _, page = self.get("/api/v3/course-data/lectures?course_id=1&limit=999")
        self.assertEqual(page["page"]["limit"], 50)
        self.assertLessEqual(len(page["lectures"]), 50)
        _, page = self.get("/api/v3/course-data/lectures?course_id=1&limit=0&offset=-5")
        self.assertEqual(page["page"], {"limit": 1, "offset": 0})
        _, summary = self.get("/api/v3/course-data?page_size=99999&page=0")
        self.assertEqual(summary["page"]["page_size"], 200)
        self.assertEqual(summary["page"]["page"], 1)
        with self.assertRaises(HTTPError) as caught:
            urlopen(f"{self.base}/api/v3/course-data/lectures?limit=10")
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(json.loads(caught.exception.read())["error_code"], "course_data_page_invalid")
        with self.assertRaises(HTTPError) as caught:
            urlopen(f"{self.base}/api/v3/course-data?page_size=abc")
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(json.loads(caught.exception.read())["error_code"], "course_data_page_invalid")

    # -- action closed set and request validation -----------------------------

    def test_action_validation_rejects_unknown_inputs(self):
        self.service.seed_course("1")
        self.expect_error(
            "/api/v3/course-data/actions",
            body={"action": "delete-everything", "operation_id": "valid-op-1"},
            status=400, error_code="course_data_action_invalid",
        )
        self.expect_error(
            "/api/v3/course-data/actions",
            body={"action": "export", "operation_id": "short"},
            status=400, error_code="course_data_action_invalid",
        )
        self.expect_error(
            "/api/v3/course-data/actions",
            body={"action": "export", "operation_id": "valid-op-2", "course_ids": "1"},
            status=400, error_code="course_data_action_invalid",
        )

    # -- idempotency -----------------------------------------------------------

    def test_operation_id_replays_receipt_and_conflicts_on_reuse(self):
        self.service.seed_course("1")
        body = {"action": "export", "operation_id": "export-op-1"}
        _, first = self.post("/api/v3/course-data/actions", body)
        self.assertEqual(first["schema"], "courselens.course-data-action-result.v1")
        self.assertEqual(first["status"], "accepted")
        self.assertIn("manifest", first["result"])
        _, replay = self.post("/api/v3/course-data/actions", body)
        self.assertEqual(first, replay)
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/v3/course-data/actions", {
                "action": "rebuild-search", "operation_id": "export-op-1",
            })
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(json.loads(caught.exception.read())["error_code"], "operation_id_conflict")

    # -- STUDY-STATS-M3 学习统计导出 -------------------------------------------

    def test_export_study_stats_writes_three_files_with_frozen_contract(self):
        self.service.seed_course("1", title="线性代数")
        ensure_study_stats_schema(self.service.learning_store.path)
        now = time.time()
        with closing(sqlite3.connect(self.service.learning_store.path)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO study_daily_seconds(local_date,course_id,sub_id,active_seconds,updated_at)"
                " VALUES(date('now','localtime'),'1','1-a',600,?)", (now,))
            db.execute(
                "INSERT INTO bookmarks(bookmark_id,course_id,sub_id,start_ms,end_ms,note,status,"
                "explanation_json,created_at,updated_at) VALUES('bm-1','1','1-a',0,10,"
                "'SENSITIVE-NOTE-MARKER','open','{}',?,?)", (now, now))
            db.execute(
                "INSERT OR REPLACE INTO watch_progress(sub_id,course_id,position_ms,duration_ms,"
                "completed,playback_rate,updated_at) VALUES('1-a','1',500,1000,0,1.0,?)", (now,))
        status, receipt = self.post("/api/v3/course-data/actions", {
            "action": "export-study-stats", "operation_id": "stats-export-op-1",
        })
        self.assertEqual(status, 200)
        self.assertEqual(receipt["schema"], "courselens.course-data-action-result.v1")
        self.assertEqual(receipt["status"], "accepted")
        files = receipt["result"]["files"]
        self.assertEqual(len(files), 3, "JSON+CSV×2 恰三文件")
        export_dir = self.root / "study-stats-exports"
        by_name = {item["filename"]: item for item in files}
        json_name = next(name for name in by_name if name.endswith(".json"))
        daily_name = next(name for name in by_name if name.endswith("-daily.csv"))
        lectures_name = next(name for name in by_name if name.endswith("-lectures.csv"))
        for item in files:
            self.assertEqual(item["bytes"], (export_dir / item["filename"]).stat().st_size,
                             "回执字节数=实盘文件")
        # 文件路由直下：闭集名 200 携带同一字节；非法名 404 闭集码。
        with urlopen(f"{self.base}/api/v3/study-stats/file?name={daily_name}") as response:
            daily_bytes = response.read()
        self.assertIn("seconds".encode(), daily_bytes)
        raw_json = (export_dir / json_name).read_text(encoding="utf-8-sig")
        payload = json.loads(raw_json)
        self.assertEqual(payload["schema"], "courselens.study-stats-export.v1")
        self.assertIn("weights", payload["method_constants"], "权重常量随导出带出（可调不可隐）")
        self.assertEqual(payload["daily"][0]["course_id"], "1")
        # 脱敏钉：书签 note（用户内容）绝不进任何导出文件。
        for item in files:
            blob = (export_dir / item["filename"]).read_bytes()
            self.assertNotIn(b"SENSITIVE-NOTE-MARKER", blob)
        self.expect_error(
            "/api/v3/study-stats/file?name=../learning.db", method="GET",
            status=404, error_code="study_stats_export_unavailable",
        )
        self.expect_error(
            "/api/v3/study-stats/file?name=courselens-study-stats-20260101.json", method="GET",
            status=404, error_code="study_stats_export_unavailable",
        )
        # 幂等台账：同 operation_id 重放同一回执。
        _, replay = self.post("/api/v3/course-data/actions", {
            "action": "export-study-stats", "operation_id": "stats-export-op-1",
        })
        self.assertEqual(replay, receipt)

    def test_export_study_stats_rows_carry_titles_and_counts_only(self):
        self.service.seed_course("1", title="线性代数")
        ensure_study_stats_schema(self.service.learning_store.path)
        now = time.time()
        with closing(sqlite3.connect(self.service.learning_store.path)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO study_daily_seconds(local_date,course_id,sub_id,active_seconds,updated_at)"
                " VALUES(date('now','localtime'),'1','1-a',300,?)", (now,))
            db.execute(
                "INSERT INTO quiz_items(quiz_id,course_id,sub_id,question_type,question,answer,created_at)"
                " VALUES('q1','1','1-a','single','题干','答案',?)", (now,))
            db.execute(
                "INSERT INTO quiz_attempts(attempt_id,quiz_id,answer,correct,confidence,created_at)"
                " VALUES('a1','q1','答',1,1,?)", (now,))
        status, receipt = self.post("/api/v3/course-data/actions", {
            "action": "export-study-stats", "operation_id": "stats-export-op-2",
        })
        self.assertEqual(status, 200)
        export_dir = self.root / "study-stats-exports"
        lectures_name = next(
            item["filename"] for item in receipt["result"]["files"]
            if item["filename"].endswith("-lectures.csv")
        )
        text = (export_dir / lectures_name).read_text(encoding="utf-8-sig")
        self.assertIn("线性代数", text, "课程名列=目录标题（目录元数据）")
        self.assertIn("quiz_graded", text)
        self.assertIn("2026-09-01 第一讲", text, "讲次标签=目录 date+sub_title")
        rows = [line for line in text.splitlines() if line]
        self.assertEqual(len(rows), 2, "表头+恰一行讲明细（目录零行补齐）")

    # -- lifecycle blocker rejection matrix -------------------------------------

    def test_blocker_matrix_rejects_mutating_actions_whole_batch(self):
        self.service.seed_course("1")
        self.service.seed_artifact_directories("1", "1-a")
        cases = {
            "active_task": lambda: self.service.task_store.add_task("subtitle", "1", "1-a", {}),
            "active_remote_run": lambda: self._insert_remote_run(),
            "automation_rule": lambda: self._insert_automation_rule("1"),
        }
        for code, seed in cases.items():
            seed()
            try:
                with self.assertRaises(HTTPError) as caught:
                    self.post("/api/v3/course-data/actions", {
                        "action": "purge-derived", "operation_id": f"blocked-{code}",
                        "course_ids": ["1"], "confirm": True,
                    })
                self.assertEqual(caught.exception.code, 409)
                receipt = json.loads(caught.exception.read())["data"]
                self.assertEqual(receipt["schema"], "courselens.course-data-action-result.v1")
                self.assertEqual(receipt["status"], "rejected")
                self.assertEqual(receipt["blockers"][0]["code"], code)
            finally:
                self._reset_blockers()
        # Whole-batch means nothing was executed: artifacts remain on disk.
        self.assertTrue((self.root / "artifacts" / "subtitles" / subtitle_artifact_key("1", "1-a")).exists())
        # The rejected operation never entered the ledger: same operation_id can retry.
        receipt = self.service.task_store.get_app_state("course-data:blocked-active_task", None)
        self.assertIsNone(receipt)

    def _insert_remote_run(self) -> None:
        with closing(sqlite3.connect(self.root / "state.db")) as db, db:
            db.execute(
                "INSERT INTO remote_runs(task_id,repository,workflow,updated_at)"
                " VALUES('remote-1','owner/repo','x.yml', ?)",
                (time.time(),),
            )

    def test_release_stuck_unblocks_the_data_page_lifecycle(self):
        """U5/M16 解锁路径：卡住的行强制收尾（长期任务/NULL 远端运行），
        终态历史与合法配置（自动化规则）不动；解锁后变更动作恢复可达。"""
        self.service.seed_course("1")
        self.service.seed_artifact_directories("1", "1-a")
        self.service.task_store.add_task("subtitle", "1", "1-a", {})
        self._insert_remote_run()
        self._insert_automation_rule("1")
        receipt = self.post("/api/v3/course-data/actions", {
            "action": "release-stuck", "operation_id": "stuck-release-1",
        })[1]
        self.assertEqual(receipt["schema"], "courselens.course-data-action-result.v1")
        self.assertEqual(receipt["status"], "accepted")
        released = receipt["result"]["released"]
        self.assertEqual(released["tasks"], 1)
        self.assertEqual(released["remote_runs"], 1)
        self.assertEqual(self.service.task_store.count(), 0)
        self.assertEqual(self.service.task_store.active_remote_run_count(), 0)
        # 合法配置不被解锁误删：规则仍在，purge 仍被且仅被它拒绝
        with self.assertRaises(HTTPError) as caught:
            self.post("/api/v3/course-data/actions", {
                "action": "purge-derived", "operation_id": "stuck-post-rule",
                "course_ids": ["1"], "confirm": True,
            })
        receipt = json.loads(caught.exception.read())["data"]
        self.assertEqual([item["code"] for item in receipt["blockers"]], ["automation_rule"])
        # 配置保留、阻塞可自行解除后，动作恢复可达
        self.service.task_store.replace_automation_rules([], profile_id="p1")
        receipt = self.post("/api/v3/course-data/actions", {
            "action": "purge-derived", "operation_id": "stuck-post-ok",
            "course_ids": ["1"], "confirm": True,
        })[1]
        self.assertEqual(receipt["status"], "accepted")

    def _insert_automation_rule(self, course_id: str) -> None:
        with closing(sqlite3.connect(self.root / "state.db")) as db, db:
            db.execute(
                "INSERT INTO automation_course_rules(profile_id,course_id,rule_json,updated_at)"
                " VALUES('p1',?, '{}', ?)",
                (course_id, time.time()),
            )

    def _reset_blockers(self) -> None:
        with closing(sqlite3.connect(self.root / "state.db")) as db, db:
            db.execute("DELETE FROM tasks")
            db.execute("DELETE FROM remote_runs")
            db.execute("DELETE FROM automation_course_rules")

    # -- confirmation gradient ---------------------------------------------------

    def test_confirmation_gradient_follows_the_frozen_tiers(self):
        self.service.seed_course("1")
        status, receipt = self.post("/api/v3/course-data/actions", {
            "action": "rebuild-search", "operation_id": "tier-rebuild-1",
        })
        self.assertEqual((status, receipt["status"]), (200, "accepted"))
        status, receipt = self.post("/api/v3/course-data/actions", {
            "action": "export", "operation_id": "tier-export-1",
        })
        self.assertEqual((status, receipt["status"]), (200, "accepted"))
        for action in ("purge-derived", "remove-copies", "delete-records"):
            self.expect_error(
                "/api/v3/course-data/actions",
                body={"action": action, "operation_id": f"tier-{action}", "course_ids": ["1"]},
                status=400, error_code="course_data_confirm_required",
            )

    # -- execution semantics -------------------------------------------------------

    def test_purge_derived_removes_only_derived_rows(self):
        self.service.seed_course("1")
        self.service.seed_transcript("1-a")
        self.service.seed_transcript("1-b")
        self.service.seed_progress_and_records("1", "1-a")
        status, receipt = self.post("/api/v3/course-data/actions", {
            "action": "purge-derived", "operation_id": "purge-op-1",
            "course_ids": ["1"], "confirm": True,
        })
        self.assertEqual((status, receipt["status"]), (200, "accepted"))
        self.assertGreaterEqual(receipt["result"]["deleted"]["transcript_segments"], 2)
        with closing(sqlite3.connect(self.service.learning_store.path)) as db, db:
            remaining = db.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0]
            progress = db.execute("SELECT COUNT(*) FROM watch_progress").fetchone()[0]
        self.assertEqual(remaining, 0)
        self.assertEqual(progress, 1)

    def test_remove_copies_deletes_files_and_optional_orphans(self):
        self.service.seed_course("1")
        directories = self.service.seed_artifact_directories("1", "1-a")
        document = register_document(
            self.service.learning_store.path, self.root,
            course_id="1", sub_id="1-a", title="讲义", original_name="notes.txt",
            media_type="text/plain",
            content_base64=base64.b64encode("合成讲义内容".encode()).decode(),
        )
        document_dir = self.root / "documents" / document["document_id"]
        self.assertTrue(document_dir.exists())
        orphan = self.root / "courseware" / "lec-0123456789abcdef"
        orphan.mkdir(parents=True)
        status, receipt = self.post("/api/v3/course-data/actions", {
            "action": "remove-copies", "operation_id": "copies-op-1",
            "course_ids": ["1"], "confirm": True,
        })
        self.assertEqual((status, receipt["status"]), (200, "accepted"))
        self.assertGreaterEqual(receipt["result"]["removed"]["documents"], 1)
        self.assertFalse(directories["subtitles"].exists())
        self.assertFalse(directories["courseware"].exists())
        self.assertFalse(document_dir.exists())
        self.assertTrue(orphan.exists())
        status, receipt = self.post("/api/v3/course-data/actions", {
            "action": "remove-copies", "operation_id": "copies-op-2",
            "include_orphans": True, "confirm": True, "confirm_typed": "清除全部孤儿",
        })
        self.assertEqual((status, receipt["status"]), (200, "accepted"))
        self.assertGreaterEqual(receipt["result"]["orphans_removed"], 1)
        self.assertFalse(orphan.exists())

    def test_remove_copies_reaches_rolled_over_courses(self):
        """第十五案（DATA-DELETE-REPAIR-1）：学期滚动后退出目录的课程仍可释放。

        该课程的行在数据页仍可见（learning 聚合）且可勾选，但定向展开若只认
        catalog，remove-copies 会 course_data_target_invalid 400，文件原地不动。
        同一动作还必须先解析 known 对再删文档行：文档行是 learning 聚合的
        来源，先删后算会让同讲次的课件/字幕被静默跳过。
        """
        document = register_document(
            self.service.learning_store.path, self.root,
            course_id="9", sub_id="9-a", title="旧学期讲义", original_name="old.txt",
            media_type="text/plain",
            content_base64=base64.b64encode("旧学期内容".encode()).decode(),
        )
        document_dir = self.root / "documents" / document["document_id"]
        courseware_dir = self.root / "courseware" / courseware_lecture_key("9", "9-a")
        courseware_dir.mkdir(parents=True)
        (courseware_dir / "slides.pdf").write_bytes(b"%PDF-1.4 synthetic")
        (courseware_dir / "manifest.json").write_text("{}", encoding="utf-8")
        subtitles_dir = self.root / "artifacts" / "subtitles" / subtitle_artifact_key("9", "9-a")
        subtitles_dir.mkdir(parents=True)
        (subtitles_dir / "lecture.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n", encoding="utf-8")
        self.assertTrue(document_dir.exists())
        status, receipt = self.post("/api/v3/course-data/actions", {
            "action": "remove-copies", "operation_id": "copies-rollover-1",
            "course_ids": ["9"], "confirm": True,
        })
        self.assertEqual((status, receipt["status"]), (200, "accepted"))
        self.assertGreaterEqual(receipt["result"]["removed"]["documents"], 1)
        self.assertGreaterEqual(receipt["result"]["removed"]["courseware"], 1)
        self.assertGreaterEqual(receipt["result"]["removed"]["subtitles"], 1)
        self.assertFalse(document_dir.exists())
        self.assertFalse(courseware_dir.exists())
        self.assertFalse(subtitles_dir.exists())

    def test_delete_records_requires_the_typed_course_name(self):
        self.service.seed_course("1", "线性代数")
        self.service.seed_course("2", "概率论")
        self.service.seed_progress_and_records("1", "1-a")
        # Batches confirm one course at a time (逐课程确认).
        self.expect_error(
            "/api/v3/course-data/actions",
            body={"action": "delete-records", "operation_id": "records-op-0",
                  "course_ids": ["1", "2"], "confirm": True, "confirm_typed": "线性代数"},
            status=400, error_code="course_data_confirm_required",
        )
        # Wrong typed receipt is rejected.
        self.expect_error(
            "/api/v3/course-data/actions",
            body={"action": "delete-records", "operation_id": "records-op-1",
                  "course_ids": ["1"], "confirm": True, "confirm_typed": "错误的课名"},
            status=400, error_code="course_data_confirm_required",
        )
        # Non-catalog courses cannot be typed-confirmed (fail-closed).
        self.expect_error(
            "/api/v3/course-data/actions",
            body={"action": "delete-records", "operation_id": "records-op-2",
                  "course_ids": ["ghost"], "confirm": True, "confirm_typed": "ghost"},
            status=400, error_code="course_data_target_invalid",
        )
        status, receipt = self.post("/api/v3/course-data/actions", {
            "action": "delete-records", "operation_id": "records-op-3",
            "course_ids": ["1"], "confirm": True, "confirm_typed": "线性代数",
        })
        self.assertEqual((status, receipt["status"]), (200, "accepted"))
        self.assertGreaterEqual(receipt["result"]["deleted"]["watch_progress"], 1)
        self.assertGreaterEqual(receipt["result"]["deleted"]["bookmarks"], 1)
        with closing(sqlite3.connect(self.service.learning_store.path)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM watch_progress").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
