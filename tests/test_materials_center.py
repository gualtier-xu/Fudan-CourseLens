"""API-layer tests for the unified materials file center (DATA-DELETE-REPAIR-1).

The study page documents tab aggregates three entry kinds — imported
documents, generated courseware PDFs, and exported AI-summary markdown —
each with export (stream the existing file) and delete.  All fixtures are
synthetic; no network, no real courseware generation.
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
from src.runtime.document_alignment import list_documents, register_document
from src.runtime.course_data_inventory import courseware_lecture_key
from src.runtime.courseware_pdf import MANIFEST_SCHEMA
from src.runtime.materials_center import summary_export_key, write_summary_export
from src.application import CourseLensApplication

# 章节键合成内容：两件 ai_artifacts（timestamp_summary + lecture_chapters）由
# import_remote_summary 原子写入，作为「总结删除不动学习数据」的对照物。
_SYNTHETIC_SUMMARY = "本讲围绕合成主题展开。"
_SYNTHETIC_CHAPTERS = [{"title": "开场", "summary": "问候与大纲", "start_ms": 0}]


class _MaterialsLearningHost:
    """Bind the REAL application methods onto a narrow host.

    ``delete_learning_document`` and ``_export_summary_markdown`` only touch
    learning_store / catalog_repository / output_dir / search_index, so the
    unbound functions run unmodified against this double.
    """

    delete_learning_document = CourseLensApplication.delete_learning_document
    _export_summary_markdown = CourseLensApplication._export_summary_markdown

    def __init__(self, learning_store, catalog_repository, output_dir):
        self.learning_store = learning_store
        self.catalog_repository = catalog_repository
        self.output_dir = output_dir

        class _SearchIndex:
            def request_refresh(self, *_args, **_kwargs) -> None:
                return None

        self.search_index = _SearchIndex()


class _MaterialsService:
    """Narrow double exposing exactly the container paths the routes touch."""

    def __init__(self, root: Path):
        self.root = root
        self.catalog_repository = CatalogRepository(root / "state.db")
        self.task_store = TaskStore(root / "state.db")
        self.learning_store = LearningStore(root / "learning.db")
        ensure_student_feature_schema(root / "learning.db")
        self._host = _MaterialsLearningHost(self.learning_store, self.catalog_repository, root)
        self.learning = SimpleNamespace(
            repository=self.learning_store,
            delete_learning_document=self._host.delete_learning_document,
        )
        self._auth_state = "ready"
        self.auth_catalog = SimpleNamespace(
            catalog=self.catalog_repository,
            authentication_snapshot=lambda: {"state": self._auth_state},
        )
        self.tasks = SimpleNamespace(repository=self.task_store)

    def set_auth_state(self, state: str) -> None:
        self._auth_state = state

    def seed_course(self, course_id: str = "1", title: str = "线性代数") -> None:
        self.catalog_repository.upsert_course(course_id, title, teacher="张老师")
        self.catalog_repository.upsert_lecture(course_id, {
            "sub_id": f"{course_id}-a", "sub_title": "第一讲", "date": "2026-09-01",
        })

    def seed_document(self, course_id: str, sub_id: str, name: str = "讲义") -> dict:
        return register_document(
            self.learning_store.path, self.root,
            course_id=course_id, sub_id=sub_id, title=name, original_name=f"{name}.txt",
            media_type="text/plain",
            content_base64=base64.b64encode(f"{name}内容".encode()).decode(),
        )

    def seed_courseware(self, course_id: str, sub_id: str, *, with_manifest: bool = True) -> Path:
        directory = self.root / "courseware" / courseware_lecture_key(course_id, sub_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "slides.pdf").write_bytes(b"%PDF-1.4 synthetic courseware")
        if with_manifest:
            (directory / "manifest.json").write_text(
                json.dumps({"schema": MANIFEST_SCHEMA, "kept": 3, "generated_at": 1725150000}),
                encoding="utf-8",
            )
        return directory

    def seed_summary_import(self, course_id: str, sub_id: str) -> None:
        self.learning_store.import_remote_summary(
            course_id=course_id, sub_id=sub_id, input_hash="hash-1",
            model="deepseek-chat", markdown=_SYNTHETIC_SUMMARY,
            chapters=_SYNTHETIC_CHAPTERS, ppt_pages=[], metrics={},
        )


class _MaterialsApiServer(ThreadingHTTPServer):
    daemon_threads = False


class MaterialsCenterApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.service = _MaterialsService(root)
        self.root = root
        server = _MaterialsApiServer(("127.0.0.1", 0), make_handler(self.service, root))
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

    def get_raw(self, path: str):
        with urlopen(f"{self.base}{path}") as response:
            return response.status, response.read()

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

    def entries_of(self, payload: dict, kind: str | None = None) -> list[dict]:
        values = payload["entries"]
        if kind is None:
            return values
        return [value for value in values if value["kind"] == kind]

    # -- gate ----------------------------------------------------------------

    def test_materials_routes_join_the_course_session_gate(self):
        self.service.set_auth_state("idle")
        self.expect_error("/api/v3/materials", method="GET", status=401, error_code="fudan_login_required")
        self.service.set_auth_state("ready")
        status, _ = self.get("/api/v3/materials")
        self.assertEqual(status, 200)

    # -- chain ① courseware -----------------------------------------------------

    def test_courseware_visible_then_delete_returns_to_ungenerated_state(self):
        self.service.seed_course("1")
        directory = self.service.seed_courseware("1", "1-a")
        status, payload = self.get("/api/v3/materials?course_id=1")
        self.assertEqual((status, payload["scan_truncated"]), (200, False))
        rows = self.entries_of(payload, "courseware")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], courseware_lecture_key("1", "1-a"))
        self.assertEqual(row["sub_id"], "1-a")
        self.assertEqual(row["course_id"], "1")
        self.assertEqual(row["lecture_title"], "第一讲")
        self.assertTrue(row["download_name"].endswith(".pdf"))
        self.assertGreater(row["size"], 0)
        status, value = self.post("/api/v3/materials/actions", {
            "action": "delete", "kind": "courseware", "id": row["id"],
        })
        self.assertEqual((status, value["deleted"]), (200, True))
        self.assertFalse(directory.exists())
        _, payload = self.get("/api/v3/materials?course_id=1")
        self.assertEqual(self.entries_of(payload, "courseware"), [])

    def test_courseware_delete_refuses_while_generation_is_active(self):
        self.service.seed_course("1")
        directory = self.service.seed_courseware("1", "1-a")
        self.service.task_store.add_task("courseware_pdf", "1", "1-a", {})
        self.expect_error(
            "/api/v3/materials/actions",
            body={"action": "delete", "kind": "courseware",
                  "id": courseware_lecture_key("1", "1-a")},
            status=409, error_code="materials_courseware_busy",
        )
        self.assertTrue(directory.exists())
        with closing(sqlite3.connect(self.root / "state.db")) as db, db:
            db.execute("UPDATE tasks SET state='failed'")
        status, _ = self.post("/api/v3/materials/actions", {
            "action": "delete", "kind": "courseware", "id": courseware_lecture_key("1", "1-a"),
        })
        self.assertEqual(status, 200)
        self.assertFalse(directory.exists())

    # -- chain ② documents ------------------------------------------------------

    def test_document_visible_then_delete_leaves_zero_residue(self):
        self.service.seed_course("1")
        document = self.service.seed_document("1", "1-a")
        document_dir = self.root / "documents" / document["document_id"]
        status, payload = self.get("/api/v3/materials?course_id=1")
        rows = self.entries_of(payload, "document")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], document["document_id"])
        self.assertEqual(rows[0]["name"], "讲义")
        self.assertGreater(rows[0]["size"], 0)
        status, value = self.post("/api/v3/materials/actions", {
            "action": "delete", "kind": "document", "id": document["document_id"],
        })
        self.assertEqual((status, value["deleted"]), (200, True))
        self.assertEqual(list_documents(self.service.learning_store.path), [])
        self.assertFalse(document_dir.exists())
        _, payload = self.get("/api/v3/materials?course_id=1")
        self.assertEqual(self.entries_of(payload, "document"), [])

    # -- chain ③ summary ---------------------------------------------------------

    def test_summary_visible_exportable_delete_keeps_learning_rows(self):
        self.service.seed_course("1")
        self.service.seed_summary_import("1", "1-a")
        self.service._host._export_summary_markdown(
            "1", "1-a", markdown=_SYNTHETIC_SUMMARY, chapters=_SYNTHETIC_CHAPTERS,
        )
        key = summary_export_key("1", "1-a")
        exported = self.root / "summaries" / key
        markdowns = list(exported.glob("*.md"))
        self.assertEqual(len(markdowns), 1)
        disk_bytes = markdowns[0].read_bytes()
        status, payload = self.get("/api/v3/materials?course_id=1")
        rows = self.entries_of(payload, "summary")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], key)
        status, raw = self.get_raw(f"/api/v3/materials/file?kind=summary&id={key}")
        self.assertEqual((status, raw), (200, disk_bytes))
        self.assertIn("第一讲".encode(), raw)  # 标题行 = 讲次可读名
        self.assertIn(_SYNTHETIC_SUMMARY.encode(), raw)
        self.assertIn("开场".encode(), raw)
        status, _ = self.post("/api/v3/materials/actions", {
            "action": "delete", "kind": "summary", "id": key,
        })
        self.assertEqual(status, 200)
        self.assertFalse(exported.exists())
        with closing(sqlite3.connect(self.service.learning_store.path)) as db:
            kept = db.execute(
                "SELECT COUNT(*) FROM ai_artifacts WHERE sub_id='1-a'",
            ).fetchone()[0]
        self.assertEqual(kept, 2)  # timestamp_summary + lecture_chapters 保留

    def test_summary_export_hook_survives_unknown_lecture(self):
        # 学期滚动：讲次不在目录也能导出（文件名回落到讲次 id）。
        self.service.seed_summary_import("9", "9-a")
        self.service._host._export_summary_markdown(
            "9", "9-a", markdown=_SYNTHETIC_SUMMARY, chapters=_SYNTHETIC_CHAPTERS,
        )
        exported = self.root / "summaries" / summary_export_key("9", "9-a")
        markdowns = list(exported.glob("*.md"))
        self.assertEqual(len(markdowns), 1)
        self.assertIn("9-a", markdowns[0].name)

    # -- export streaming boundary ----------------------------------------------

    def test_export_stream_rejects_unknown_and_escaping_ids(self):
        # 资产路由 404 走 send_error（非 JSON 体），只断言状态码。
        self.service.seed_course("1")
        self.service.seed_document("1", "1-a")
        for path in (
            "/api/v3/materials/file?kind=courseware&id=../../state.db",
            "/api/v3/materials/file?kind=document&id=unknown0123",
            "/api/v3/materials/file?kind=quiz&id=whatever",
            "/api/v3/materials/file?kind=summary&id=sum-0123456789abcdef",
        ):
            with self.assertRaises(HTTPError) as caught:
                urlopen(f"{self.base}{path}")
            self.assertEqual(caught.exception.code, 404)
        # 缺参=400（closed set 校验），不是资源不存在。
        with self.assertRaises(HTTPError) as caught:
            urlopen(f"{self.base}/api/v3/materials/file?kind=summary")
        self.assertEqual(caught.exception.code, 400)

    # -- chain ⑤ anti-duplication / anti-bloat pins ------------------------------

    def test_same_file_second_import_stays_one_entry(self):
        self.service.seed_course("1")
        content = base64.b64encode("同一份讲义".encode()).decode()
        first = register_document(
            self.service.learning_store.path, self.root,
            course_id="1", sub_id="1-a", title="讲义", original_name="same.txt",
            media_type="text/plain", content_base64=content,
        )
        second = register_document(
            self.service.learning_store.path, self.root,
            course_id="1", sub_id="1-a", title="讲义改名", original_name="same.txt",
            media_type="text/plain", content_base64=content,
        )
        self.assertEqual(first["document_id"], second["document_id"])
        self.assertEqual(len(list_documents(self.service.learning_store.path)), 1)
        self.assertEqual(len(list((self.root / "documents").iterdir())), 1)

    def test_courseware_rescan_stays_single_entry_without_tmp_residue(self):
        self.service.seed_course("1")
        directory = self.service.seed_courseware("1", "1-a", with_manifest=False)
        _, payload = self.get("/api/v3/materials?course_id=1")
        self.assertEqual(len(self.entries_of(payload, "courseware")), 0)  # 无 manifest 不算成品
        (directory / "slides.pdf").write_bytes(b"%PDF-1.4 regenerated")
        (directory / "manifest.json").write_text(
            json.dumps({"schema": MANIFEST_SCHEMA, "kept": 5, "generated_at": 1725150900}),
            encoding="utf-8",
        )
        _, payload = self.get("/api/v3/materials?course_id=1")
        rows = self.entries_of(payload, "courseware")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["size"], len(b"%PDF-1.4 regenerated"))
        self.assertEqual(list(directory.glob("*.tmp")), [])

    def test_summary_repeat_completion_overwrites_same_file(self):
        self.service.seed_course("1")
        self.service.seed_summary_import("1", "1-a")
        self.service._host._export_summary_markdown(
            "1", "1-a", markdown="第一版", chapters=_SYNTHETIC_CHAPTERS,
        )
        self.service._host._export_summary_markdown(
            "1", "1-a", markdown="第二版内容", chapters=_SYNTHETIC_CHAPTERS,
        )
        exported = self.root / "summaries" / summary_export_key("1", "1-a")
        markdowns = list(exported.glob("*.md"))
        self.assertEqual(len(markdowns), 1)
        self.assertIn("第二版内容".encode(), markdowns[0].read_bytes())
        # 讲次改名：新文件名落盘后旧名不残留（恰一件纪律）。
        self.service._host._export_summary_markdown(
            "1", "1-a", markdown="第三版", chapters=_SYNTHETIC_CHAPTERS,
        )
        markdowns = list(exported.glob("*.md"))
        self.assertEqual(len(markdowns), 1)

    # -- rolled-over lectures stay manageable in the file center -----------------

    def test_rolled_over_lecture_materials_stay_manageable(self):
        # 课程已退出目录（无 catalog 行），但文档/课件仍在：全量视图必须
        # 仍能看到并逐条删除（课件按 key 定位，不依赖 catalog）。
        self.service.seed_document("9", "9-a", name="旧学期讲义")
        directory = self.service.seed_courseware("9", "9-a")
        status, payload = self.get("/api/v3/materials")
        documents = self.entries_of(payload, "document")
        courseware = self.entries_of(payload, "courseware")
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["course_id"], "9")
        self.assertEqual(len(courseware), 1)
        self.assertEqual(courseware[0]["course_id"], "9")
        self.assertEqual(courseware[0]["lecture_title"], "")
        key = courseware[0]["id"]
        status, _ = self.post("/api/v3/materials/actions", {
            "action": "delete", "kind": "courseware", "id": key,
        })
        self.assertEqual(status, 200)
        self.assertFalse(directory.exists())

    # -- closed action set ---------------------------------------------------------

    def test_materials_actions_reject_unknown_inputs(self):
        self.expect_error(
            "/api/v3/materials/actions",
            body={"action": "archive", "kind": "summary", "id": "sum-0123456789abcdef"},
            status=400, error_code="materials_action_invalid",
        )
        self.expect_error(
            "/api/v3/materials/actions",
            body={"action": "delete"},
            status=400, error_code="materials_action_invalid",
        )
        self.expect_error(
            "/api/v3/materials/actions",
            body={"action": "delete", "kind": "quiz", "id": "x"},
            status=400, error_code="materials_kind_invalid",
        )
        self.expect_error(
            "/api/v3/materials/actions",
            body={"action": "delete", "kind": "summary", "id": "not-a-key"},
            status=400, error_code="materials_entry_invalid",
        )
        self.expect_error(
            "/api/v3/materials/actions",
            body={"action": "delete", "kind": "summary", "id": "sum-0123456789abcdef"},
            status=404, error_code="materials_entry_missing",
        )


if __name__ == "__main__":
    unittest.main()
