"""Data migration package tests (D12 data sovereignty P0, 2026-10-07).

Synthetic fixtures only.  Covers: the export/import roundtrip (both databases
plus the four file namespaces), deterministic storage_path rebasing on the new
machine, wrong-password and tampered-payload rejection, the schema-version
closed gate, the credentials re-entry closed set (password never in receipts),
the export idempotency ledger with single-use download tokens, staged uploads,
and the HTTP routes (upload → import → shutdown reason → download, 410 on
reused tokens, no course-session gate on this lane by design).
"""

from __future__ import annotations

import errno
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import uuid
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.request import Request, urlopen

from src.application import CourseLensApplication
from src.runtime import data_migration as data_migration_module
from src.runtime.catalog_repository import CatalogRepository
from src.runtime.data_migration import (
    CURRENT_SCHEMA_VERSION,
    DataMigrationError,
    ERROR_BUSY,
    ERROR_PACKAGE_INVALID,
    ERROR_PASSWORD_INVALID,
    ERROR_SCHEMA_TOO_NEW,
    ERROR_WRITE_FAILED,
    EXPORT_RECEIPT_SCHEMA,
    IMPORT_RECEIPT_SCHEMA,
    MANIFEST_NAME,
    GUIDE_NAME,
    build_migration_package,
    import_migration_package,
    inspect_migration_package,
)
from src.runtime.http_api import make_handler
from src.runtime.learning_store import LearningStore
from src.runtime.task_store import TaskStore

PASSWORD = "migrate-2026"
OLD_MACHINE_ROOT = "C:/old-machine/CourseLens/data"


def _seed_minimal_root(root: Path) -> None:
    """A synthetic data root built on the real schemas: one row, one document."""
    root.mkdir(parents=True, exist_ok=True)
    LearningStore(root / "learning.db")  # canonical learning schema
    tasks = TaskStore(root / "state.db")  # canonical state schema
    tasks.set_app_state("automation.migration-proof", "migration-proof-v1")
    now = time.time()
    with closing(sqlite3.connect(root / "learning.db")) as db:
        db.execute(
            "INSERT INTO watch_progress(sub_id,course_id,position_ms,duration_ms,"
            "completed,playback_rate,updated_at) VALUES('1-a','1',42,2000,0,1.0,?)",
            (now,),
        )
        db.execute(
            "INSERT INTO learning_documents(document_id,course_id,sub_id,title,"
            "original_name,extension,media_type,storage_path,sha256,size_bytes,"
            "page_count,extraction_state,created_at,updated_at) "
            "VALUES('doc0001','1','1-a','合成讲义','notes.pdf','pdf','application/pdf',"
            "?, 'synthetic-sha', 16, 1, 'ready', ?, ?)",
            (f"{OLD_MACHINE_ROOT}/documents/doc0001/original.pdf", now, now),
        )
        db.commit()


def _seed_namespaces(root: Path) -> dict[str, Path]:
    documents = root / "documents" / "doc0001"
    documents.mkdir(parents=True, exist_ok=True)
    (documents / "original.pdf").write_bytes(b"%PDF-1.7 synthetic")
    summaries = root / "summaries" / "lec-abc"
    summaries.mkdir(parents=True)
    (summaries / "summary.md").write_text("# 合成总结", encoding="utf-8")
    courseware = root / "courseware" / "lec-abc"
    courseware.mkdir(parents=True)
    (courseware / "slide-1.png").write_bytes(b"png-bytes")
    subtitles = root / "artifacts" / "subtitles" / "1-1-a"
    subtitles.mkdir(parents=True)
    (subtitles / "segments.json").write_text("{}", encoding="utf-8")
    # Regenerable/internal namespaces must never enter the package.
    cache = root / "cache"
    cache.mkdir()
    (cache / "junk.bin").write_bytes(b"0" * 1024)
    return {
        "document": documents / "original.pdf",
        "summary": summaries / "summary.md",
        "slide": courseware / "slide-1.png",
        "subtitle": subtitles / "segments.json",
    }


def _read_package_zip(package_path: Path, password: str):
    """White-box: decrypt the container and return (zipfile, manifest dict)."""
    from src.runtime.data_migration import (
        _decrypt_payload,
        _open_package_zip,
        _read_container_header,
    )

    staging = package_path.parent / f".test-inspect-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        header = _read_container_header(package_path)
        payload = _decrypt_payload(package_path, header, staging, password=password)
        archive, manifest = _open_package_zip(payload)
        members = set(archive.namelist())
        archive.close()
        return members, manifest, payload
    finally:
        import shutil

        shutil.rmtree(staging, ignore_errors=True)


class DataMigrationEngineTests(unittest.TestCase):
    """Module-level engine: roundtrip, rebasing, closed gates."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.source_root = self.base / "old-machine-data"
        self.new_root = self.base / "new-machine-data"
        _seed_minimal_root(self.source_root)
        self.files = _seed_namespaces(self.source_root)
        self.package = self.base / "courselens-data.clmig"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def export(self, *, password: str = PASSWORD, root: Path | None = None) -> dict:
        return build_migration_package(
            root or self.source_root,
            self.package,
            password=password,
            app_version="0.1.0",
        )

    def import_into_new_root(self, *, password: str = PASSWORD) -> dict:
        return import_migration_package(self.new_root, self.package, password=password)

    def test_roundtrip_restores_databases_and_file_namespaces(self):
        receipt = self.export()
        self.assertEqual(receipt["schema"], EXPORT_RECEIPT_SCHEMA)
        self.assertTrue(self.package.is_file())
        self.assertTrue(receipt["namespaces"]["documents"]["files"] == 1)
        # Regenerable cache must never enter the package (closed packaging set).
        members, manifest, _payload = _read_package_zip(self.package, PASSWORD)
        self.assertIn(MANIFEST_NAME, members)
        self.assertIn(GUIDE_NAME, members)
        self.assertIn("learning.db", members)
        self.assertIn("state.db", members)
        self.assertTrue(any(name.startswith("documents/") for name in members))
        self.assertFalse(any(name.startswith("cache/") for name in members))
        manifest_text = json.dumps(manifest, sort_keys=True)
        self.assertNotIn("合成", manifest_text)  # zero row values in the manifest
        self.assertNotIn("automation.migration-proof", manifest_text)  # keys are row data too

        placement = self.import_into_new_root()
        self.assertEqual(placement["schema"], IMPORT_RECEIPT_SCHEMA)
        with closing(sqlite3.connect(self.new_root / "learning.db")) as db:
            progress = db.execute("SELECT position_ms FROM watch_progress").fetchall()
        self.assertEqual(progress, [(42,)])
        with closing(sqlite3.connect(self.new_root / "state.db")) as db:
            value = db.execute(
                "SELECT value_json FROM app_state WHERE key='automation.migration-proof'"
            ).fetchone()
        self.assertEqual(value, ('"migration-proof-v1"',))
        self.assertTrue((self.new_root / "documents" / "doc0001" / "original.pdf").is_file())
        self.assertTrue((self.new_root / "summaries" / "lec-abc" / "summary.md").is_file())
        self.assertTrue((self.new_root / "courseware" / "lec-abc" / "slide-1.png").is_file())
        self.assertTrue(
            (self.new_root / "artifacts" / "subtitles" / "1-1-a" / "segments.json").is_file()
        )
        self.assertFalse((self.new_root / "cache" / "junk.bin").exists())

    def test_import_rebases_absolute_storage_paths_to_the_new_machine(self):
        self.export()
        placement = self.import_into_new_root()
        expected = self.new_root / "documents" / "doc0001" / "original.pdf"
        with closing(sqlite3.connect(self.new_root / "learning.db")) as db:
            stored = db.execute(
                "SELECT storage_path FROM learning_documents WHERE document_id='doc0001'"
            ).fetchone()[0]
        self.assertEqual(Path(stored), expected.resolve())
        self.assertNotIn(OLD_MACHINE_ROOT, stored)
        self.assertEqual(placement["rebased_document_paths"], 1)

    def test_wrong_password_is_rejected_without_placement(self):
        self.export()
        info = inspect_migration_package(self.package, password=PASSWORD)
        self.assertEqual(info["app_version"], "0.1.0")
        self.assertIn("learning.db", info["databases"])
        with self.assertRaises(DataMigrationError) as wrong:
            inspect_migration_package(self.package, password="wrong-password")
        self.assertEqual(wrong.exception.code, ERROR_PASSWORD_INVALID)
        with self.assertRaises(DataMigrationError) as wrong_import:
            self.import_into_new_root(password="wrong-password")
        self.assertEqual(wrong_import.exception.code, ERROR_PASSWORD_INVALID)
        # Fail-closed: nothing was placed.
        self.assertFalse((self.new_root / "learning.db").exists())

    def test_tampered_payload_is_rejected_by_gcm_authentication(self):
        self.export()
        raw = bytearray(self.package.read_bytes())
        raw[-20] ^= 0x01  # flip one bit inside the ciphertext
        self.package.write_bytes(bytes(raw))
        with self.assertRaises(DataMigrationError) as tampered:
            self.import_into_new_root()
        # GCM failure surfaces as wrong-password-or-tampered (shared honest code).
        self.assertEqual(tampered.exception.code, ERROR_PASSWORD_INVALID)
        self.assertFalse((self.new_root / "learning.db").exists())

    def test_schema_version_too_new_is_rejected(self):
        future = self.base / "future-root"
        _seed_minimal_root(future)
        self.files = _seed_namespaces(future)
        # Claim a schema version newer than this client understands.
        with closing(sqlite3.connect(future / "learning.db")) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS local_data_schema_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR REPLACE INTO local_data_schema_meta VALUES('schema_version',?)",
                (str(CURRENT_SCHEMA_VERSION + 1),),
            )
            db.commit()
        build_migration_package(future, self.package, password=PASSWORD, app_version="9.9.9")
        with self.assertRaises(DataMigrationError) as too_new:
            self.import_into_new_root()
        self.assertEqual(too_new.exception.code, ERROR_SCHEMA_TOO_NEW)

    def test_credentials_reentry_notice_is_a_closed_set_and_password_never_leaks(self):
        self.export()
        placement = self.import_into_new_root()
        steps = placement["credentials_reentry"]
        self.assertEqual(
            [item["id"] for item in steps],
            ["fudan_account", "github_authorization", "deepseek_key"],
        )
        serialized = json.dumps(placement, ensure_ascii=False)
        self.assertNotIn(PASSWORD, serialized)
        self.assertNotIn(OLD_MACHINE_ROOT, serialized)

    def test_short_password_is_rejected_on_export(self):
        with self.assertRaises(DataMigrationError) as weak:
            self.export(password="short")
        self.assertEqual(weak.exception.code, ERROR_PASSWORD_INVALID)

    def test_inspect_summary_is_redacted(self):
        self.export()
        info = inspect_migration_package(self.package, password=PASSWORD)
        self.assertEqual(info["schema"], "courselens.data-migration-inspect.v1")
        self.assertEqual(info["databases"]["learning.db"], {"schema_version": 0})
        self.assertEqual(info["namespaces"]["documents"], {"files": 1, "bytes": len(b"%PDF-1.7 synthetic")})


class _FakeSearchIndex:
    def __init__(self) -> None:
        self.refreshes: list[dict] = []

    def request_refresh(self, sub_ids=None, *, force=False):
        self.refreshes.append({"force": bool(force)})
        return {"state": "requested"}


class _FakeApp:
    """Narrow stand-in carrying exactly the attributes the migration methods use."""

    def __init__(self, root: Path):
        self.output_dir = root
        self.task_store = TaskStore(root / "state.db")
        self.catalog_repository = CatalogRepository(root / "state.db")
        self.learning_store = LearningStore(root / "learning.db")
        self.search_index = _FakeSearchIndex()
        self._migration_download_lock = threading.Lock()
        self._migration_downloads: dict[str, dict] = {}

    export_action = CourseLensApplication.data_migration_export_action
    download_file = CourseLensApplication.data_migration_download_file
    stage_upload = CourseLensApplication.data_migration_stage_upload
    import_action = CourseLensApplication.data_migration_import_action


class DataMigrationApplicationTests(unittest.TestCase):
    """Application layer: export ledger, single-use tokens, staged upload, import."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.source_root = self.base / "old-machine-data"
        self.new_root = self.base / "new-machine-data"
        _seed_minimal_root(self.source_root)
        self.files = _seed_namespaces(self.source_root)
        self.app = _FakeApp(self.source_root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_export_receipt_replay_and_single_use_download_token(self):
        first = self.app.export_action(operation_id="export-op-0001", password=PASSWORD)
        self.assertEqual(first["schema"], "courselens.data-migration-action-result.v1")
        self.assertEqual(first["status"], "accepted")
        download = first["result"]["download"]
        self.assertTrue(download["url"].startswith("/api/v3/data-migration/file?token="))
        # Same operation_id replays the stored receipt without a second build.
        second = self.app.export_action(operation_id="export-op-0001", password=PASSWORD)
        self.assertEqual(second["result"]["download"]["url"], download["url"])
        # Token is single-use.
        path, filename = self.app.download_file(download["url"].split("token=", 1)[1])
        self.assertTrue(path.is_file())
        self.assertTrue(filename.endswith(".clmig"))
        with self.assertRaises(FileNotFoundError):
            self.app.download_file(download["url"].split("token=", 1)[1])

    def test_stage_upload_and_import_consumes_the_staged_package(self):
        self.app.export_action(operation_id="export-op-0002", password=PASSWORD)
        token_package = next((self.source_root / "migration-exports").glob("*.clmig"))
        payload = token_package.read_bytes()
        staged = self.app.stage_upload(io.BytesIO(payload), expected_bytes=len(payload))
        self.assertRegex(staged["package_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(staged["bytes"], len(payload))

        receipt = self.app.import_action(
            operation_id="import-op-0001",
            package_id=staged["package_id"],
            password=PASSWORD,
        )
        self.assertEqual(receipt["status"], "accepted")
        result = receipt["result"]
        self.assertEqual(result["schema"], IMPORT_RECEIPT_SCHEMA)
        self.assertEqual(result["rebased_document_paths"], 1)
        self.assertTrue(
            (self.source_root / "documents" / "doc0001" / "original.pdf").is_file()
        )
        # Staged package consumed; import staging leaves no residue.
        self.assertFalse(
            (self.source_root / "migration-import-staging" / f"{staged['package_id']}.clmig").exists()
        )
        self.assertTrue(self.app.search_index.refreshes[-1]["force"])

    def test_import_rejects_remote_run_blockers_with_zero_execution(self):
        self.app.export_action(operation_id="export-op-0003", password=PASSWORD)
        token_package = next((self.source_root / "migration-exports").glob("*.clmig"))
        payload = token_package.read_bytes()
        staged = self.app.stage_upload(io.BytesIO(payload), expected_bytes=len(payload))
        staged_path = self.source_root / "migration-import-staging" / f"{staged['package_id']}.clmig"
        # A live remote run is the one external fact that must refuse the import;
        # the local releasable blockers are released inside the engine instead.
        with mock.patch(
            "src.application._client_reset_blockers",
            return_value=[{"code": "active_remote_run", "count": 1}],
        ):
            receipt = self.app.import_action(
                operation_id="import-op-0002",
                package_id=staged["package_id"],
                password=PASSWORD,
            )
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual(receipt["blockers"], [{"code": "active_remote_run", "count": 1}])
        self.assertTrue(staged_path.is_file())  # staged package kept for retry


class _MigrationService:
    """Narrow double exposing exactly the container paths the routes touch."""

    def __init__(self, root: Path):
        self.root = root
        self._app = _FakeApp(root)
        self.learning = SimpleNamespace(repository=self._app.learning_store)
        self.auth_catalog = SimpleNamespace(
            authentication_snapshot=lambda: {"state": "ready"},
        )
        self.tasks = SimpleNamespace(repository=self._app.task_store)
        self.shutdown_requests: list[str] = []
        self.lifecycle = SimpleNamespace(request_shutdown=self._record_shutdown)
        self.settings = SimpleNamespace(
            migration_export_action=lambda *, operation_id, password="":
                _FakeApp.export_action(self._app, operation_id=operation_id, password=password),
            migration_import_action=lambda *, operation_id, package_id, password="":
                _FakeApp.import_action(self._app, operation_id=operation_id, package_id=package_id, password=password),
            migration_stage_upload=lambda reader, *, expected_bytes:
                _FakeApp.stage_upload(self._app, reader, expected_bytes=expected_bytes),
            migration_download_file=lambda token:
                _FakeApp.download_file(self._app, token),
        )

    def _record_shutdown(self, reason: str) -> bool:
        self.shutdown_requests.append(str(reason))
        return True


class DataMigrationRouteTests(unittest.TestCase):
    """HTTP layer: upload → actions → download; no course-session gate here."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmp.name)
        self.source_root = self.base_dir / "old-machine-data"
        _seed_minimal_root(self.source_root)
        self.files = _seed_namespaces(self.source_root)
        self.service = _MigrationService(self.source_root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service, self.source_root))
        # FLAKYFIX-1（fixture 卫生）：实例级 daemon_threads=False——
        # ThreadingMixIn 的 block_on_close 使 tearDown 的 server_close() 收齐
        # 全部在途 handler 线程后再返回；默认 daemon=True 时在途下载流可与
        # _tmp.cleanup() 竞争打开中的包文件句柄（满载 WinError 32 ERROR 伪红）。
        server.daemon_threads = False
        self._server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{server.server_port}"

    def tearDown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._tmp.cleanup()

    def _post_json(self, path: str, body: dict):
        request = Request(
            f"{self.base}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request) as response:
            return response.status, json.loads(response.read())["data"]

    def _expect_error(self, path: str, body: dict, *, status: int, error_code: str):
        request = Request(
            f"{self.base}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, status)
        self.assertEqual(json.loads(caught.exception.read())["error_code"], error_code)

    def test_upload_export_download_roundtrip_and_shutdown_reason(self):
        # Build a package through the application layer and register its token.
        receipt = _FakeApp.export_action(self.service._app, operation_id="route-op-0001", password=PASSWORD)
        # MAC-NIGHT-1 诊断钉：mac 探针在此 IndexError（token 缺失形态未知）
        # ——先显式断言 url 形状，红时把实际 url 带进摘要做根因证据。
        export_url = receipt["result"]["download"]["url"]
        self.assertIn(
            "token=", export_url,
            f"export receipt url must carry the single-use token: {export_url!r}",
        )
        token = export_url.split("token=", 1)[1]
        package_path = Path(receipt["result"]["path"])
        # Upload it back through the raw route (simulates a package on this machine).
        request = Request(
            f"{self.base}/api/v3/data-migration/package", data=package_path.read_bytes(),
            headers={"Content-Type": "application/octet-stream"}, method="POST",
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 201)
            staged = json.loads(response.read())["data"]
        self.assertEqual(staged["schema"], "courselens.data-migration-upload.v1")
        self.assertRegex(staged["package_id"], r"^[0-9a-f]{32}$")

        status, import_receipt = self._post_json("/api/v3/data-migration/actions", {
            "action": "import", "operation_id": "route-op-0002",
            "package_id": staged["package_id"], "password": PASSWORD,
        })
        self.assertEqual(status, 200)
        self.assertEqual(import_receipt["status"], "accepted")
        # FLAKYFIX-1：回执 200 已到但 request_shutdown 落账在服务端 handler
        # 线程上晚一拍（import 路由「回执写完才关停」，json_response 不
        # flush）——盲读 shutdown_requests[-1] 与落账竞争。改为有界轮询
        # （≤5s）等落账出现，断言语义不变。
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and (
            not self.service.shutdown_requests
            or self.service.shutdown_requests[-1] != "migration_import"
        ):
            time.sleep(0.01)
        self.assertEqual(self.service.shutdown_requests[-1], "migration_import")

        # Download via GET streams the exact package once; second fetch is gone.
        # FLAKYFIX-1：期望字节必须在 GET **之前**快照——下载路由是「流出即删」
        # （http_api._serve_migration_download finally 段 path.unlink），令牌
        # 映射的恰是 package_path 本尊（export_action 注册原包路径）。此前
        # GET 返回后再 read_bytes() 与产品设计的删除竞争：满载下服务端先完成
        # unlink 即 FileNotFoundError 伪红（孤立复跑绿惯犯根因，scratch 探针
        # 实测 GET 返回 16ms 后文件即被产品删除）。断言语义不变：流出的字节
        # 仍必须逐字节等于导出包；比较基准从「下载后的同一文件」改为「下载
        # 前的快照」。
        expected_package = package_path.read_bytes()
        with urlopen(f"{self.base}/api/v3/data-migration/file?token={token}") as response:
            body = response.read()
            self.assertEqual(response.status, 200)
        self.assertEqual(body, expected_package)
        with self.assertRaises(urllib.error.HTTPError) as gone:
            urlopen(f"{self.base}/api/v3/data-migration/file?token={token}")
        self.assertEqual(gone.exception.code, 410)

    def test_actions_validation_is_strict_and_lane_has_no_course_gate(self):
        # The migration lane deliberately runs while the course session is idle.
        self.service.auth_catalog = SimpleNamespace(
            authentication_snapshot=lambda: {"state": "idle"},
        )
        self._expect_error(
            "/api/v3/data-migration/actions",
            {"action": "bogus", "operation_id": "route-op-0003", "password": PASSWORD},
            status=400, error_code="migration_action_invalid",
        )
        self._expect_error(
            "/api/v3/data-migration/actions",
            {"action": "export", "operation_id": "route-op-0004", "password": "short"},
            status=400, error_code="migration_action_invalid",
        )
        self._expect_error(
            "/api/v3/data-migration/actions",
            {"action": "import", "operation_id": "route-op-0005",
             "package_id": "zzz", "password": PASSWORD},
            status=400, error_code="migration_action_invalid",
        )
        # Unknown package id -> honest closed error (not a crash).
        self._expect_error(
            "/api/v3/data-migration/actions",
            {"action": "import", "operation_id": "route-op-0006",
             "package_id": "a" * 32, "password": PASSWORD},
            status=400, error_code="MIGRATION_E_PACKAGE_INVALID",
        )
        # Empty upload is rejected before any write.
        self._expect_error_raw()

    def _expect_error_raw(self):
        request = Request(
            f"{self.base}/api/v3/data-migration/package", data=b"",
            headers={"Content-Type": "application/octet-stream"}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(json.loads(caught.exception.read())["error_code"], "request_body_invalid")

    def test_wrong_password_on_import_maps_to_closed_error(self):
        receipt = _FakeApp.export_action(self.service._app, operation_id="route-op-0007", password=PASSWORD)
        package_bytes = Path(receipt["result"]["path"]).read_bytes()
        request = Request(
            f"{self.base}/api/v3/data-migration/package", data=package_bytes,
            headers={"Content-Type": "application/octet-stream"}, method="POST",
        )
        with urlopen(request) as response:
            staged = json.loads(response.read())["data"]
        self._expect_error(
            "/api/v3/data-migration/actions",
            {"action": "import", "operation_id": "route-op-0008",
             "package_id": staged["package_id"], "password": "wrong-password"},
            status=400, error_code="MIGRATION_E_PASSWORD_INVALID",
        )


class DataMigrationBusyAndRollbackTests(unittest.TestCase):
    """D-20261009-05 三态钉：忙碌拒（单飞）/忙碌安全成/中途失败回滚 + 原数据保全。

    忙碌应用（活动连接握着 state.db-wal）是搬家导入的常态而非例外：
    根修前 unlink -wal 撞 WinError 32 必败且留下「新文件 + 旧库」混合态；
    根修后清理只是卫生，任何失败路径都自动回滚到原位或如实指明 trash 保全。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.old_root = self.base / "old-machine-data"
        self.new_root = self.base / "busy-machine-data"
        self._seed_root(self.old_root, marker="old-marker", doc_tag=b"old-bytes", learn_rows=1)
        self._seed_root(self.new_root, marker="old-marker", doc_tag=b"old-bytes", learn_rows=1)
        self.package = self.base / "package.clmig"
        build_migration_package(
            self._seed_root(self.base / "package-src", marker="new-marker", doc_tag=b"new-bytes", learn_rows=2),
            self.package,
            password=PASSWORD,
            app_version="0.1.0",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _seed_root(root: Path, *, marker: str, doc_tag: bytes, learn_rows: int) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        LearningStore(root / "learning.db")
        TaskStore(root / "state.db").set_app_state("automation.migration-proof", marker)
        now = time.time()
        with closing(sqlite3.connect(root / "learning.db")) as db:
            for index in range(learn_rows):
                db.execute(
                    "INSERT INTO watch_progress(sub_id,course_id,position_ms,duration_ms,"
                    "completed,playback_rate,updated_at) VALUES(?,?,?,2000,0,1.0,?)",
                    (f"{index}-a", "1", index * 10, now),
                )
            db.commit()
        documents = root / "documents" / f"doc-{marker}"
        documents.mkdir(parents=True, exist_ok=True)
        (documents / "original.pdf").write_bytes(doc_tag)
        summaries = root / "summaries" / f"lec-{marker}"
        summaries.mkdir(parents=True, exist_ok=True)
        (summaries / "summary.md").write_text(f"# {marker}", encoding="utf-8")
        return root

    def _marker(self, root: Path) -> object:
        return TaskStore(root / "state.db").get_app_state("automation.migration-proof", "<missing>")

    def _learning_rows(self, root: Path) -> int:
        with closing(sqlite3.connect(root / "learning.db")) as db:
            return int(db.execute("SELECT COUNT(*) FROM watch_progress").fetchone()[0])

    def _residue_dirs(self, root: Path) -> list[str]:
        return sorted(
            path.name for path in root.iterdir()
            if path.is_dir() and (
                path.name.startswith("migration-import-trash-")
                or path.name.startswith(".migration-import-")
            )
        )

    def test_busy_app_with_open_connection_import_succeeds(self):
        """根修钉：忙碌态（活动连接握着 state.db-wal）导入安全成功，数据经旧连接可见。"""
        held = sqlite3.connect(self.new_root / "state.db", timeout=30)
        try:
            held.execute("CREATE TABLE IF NOT EXISTS busy_probe(k TEXT PRIMARY KEY, v TEXT)")
            held.execute("INSERT OR REPLACE INTO busy_probe VALUES('held', '1')")
            held.commit()  # 制造被握住的 -wal 句柄（根修前必撞 WinError 32）
            receipt = import_migration_package(self.new_root, self.package, password=PASSWORD)
            self.assertEqual(receipt["schema"], IMPORT_RECEIPT_SCHEMA)
            # 恢复真的发生了：经导入前就打开的旧连接读到新库内容。
            self.assertEqual(self._marker(self.new_root), "new-marker")
            self.assertEqual(self._learning_rows(self.new_root), 2)
            self.assertEqual(
                (self.new_root / "documents" / "doc-new-marker" / "original.pdf").read_bytes(),
                b"new-bytes",
            )
        finally:
            held.close()

    def test_stale_wal_leftovers_are_cleaned_and_never_fatal(self):
        """遗留 -wal/-shm 是卫生不是成败项：静默态导入成功且清干净。"""
        (self.new_root / "state.db-wal").write_bytes(b"stale-wal-bytes")
        (self.new_root / "learning.db-shm").write_bytes(b"stale-shm-bytes")
        receipt = import_migration_package(self.new_root, self.package, password=PASSWORD)
        self.assertEqual(receipt["schema"], IMPORT_RECEIPT_SCHEMA)
        self.assertFalse((self.new_root / "state.db-wal").exists())
        self.assertFalse((self.new_root / "learning.db-shm").exists())
        self.assertEqual(self._marker(self.new_root), "new-marker")

    def test_single_flight_second_import_is_refused_with_precondition(self):
        """忙碌拒钉：并发导入是唯一前置条件类拒绝，回执诚实指明等待语义。"""
        with data_migration_module._IMPORT_SINGLE_FLIGHT:
            with self.assertRaises(DataMigrationError) as ctx:
                import_migration_package(self.new_root, self.package, password=PASSWORD)
        self.assertEqual(ctx.exception.code, ERROR_BUSY)
        self.assertIn("已经有一个导入", ctx.exception.instruction)
        # 拒绝发生在任何落位之前：原数据原样。
        self.assertEqual(self._marker(self.new_root), "old-marker")
        self.assertEqual(self._residue_dirs(self.new_root), [])

    def test_midway_failure_rolls_back_to_originals(self):
        """中途失败钉：落位第二步失手 → 自动回滚，原数据逐项在位，零残留。"""
        original_place = data_migration_module._place_namespace

        def failing_place(staged_root, namespace, root, trash):
            if namespace == "summaries":
                raise OSError(errno.EBUSY, "合成占用：summaries 落位失手")
            return original_place(staged_root, namespace, root, trash)

        with mock.patch.object(data_migration_module, "_place_namespace", failing_place):
            with self.assertRaises(DataMigrationError) as ctx:
                import_migration_package(self.new_root, self.package, password=PASSWORD)
        self.assertEqual(ctx.exception.code, ERROR_WRITE_FAILED)
        self.assertIn("自动恢复原位", ctx.exception.instruction)
        self.assertNotIn("稍后重试", ctx.exception.instruction)
        # 原数据保全断言：库、命名空间逐项与导入前相同。
        self.assertEqual(self._marker(self.new_root), "old-marker")
        self.assertEqual(self._learning_rows(self.new_root), 1)
        self.assertEqual(
            (self.new_root / "documents" / "doc-old-marker" / "original.pdf").read_bytes(),
            b"old-bytes",
        )
        self.assertEqual(
            (self.new_root / "summaries" / "lec-old-marker" / "summary.md").read_text(encoding="utf-8"),
            "# old-marker",
        )
        # 回滚干净：trash 与暂存区一个不剩。
        self.assertEqual(self._residue_dirs(self.new_root), [])

    def test_stuck_rollback_preserves_originals_in_trash_and_says_so(self):
        """回执诚实钉：连回滚都被占用时，如实指明原数据完好保存在 trash 文件夹。"""
        original_place = data_migration_module._place_namespace

        def failing_place(staged_root, namespace, root, trash):
            if namespace == "summaries":
                raise OSError(errno.EBUSY, "合成占用：summaries 落位失手")
            return original_place(staged_root, namespace, root, trash)

        with mock.patch.object(data_migration_module, "_place_namespace", failing_place), \
                mock.patch.object(
                    data_migration_module, "_rollback_import",
                    lambda *args, **kwargs: ["documents: 合成回滚占用"],
                ):
            with self.assertRaises(DataMigrationError) as ctx:
                import_migration_package(self.new_root, self.package, password=PASSWORD)
        self.assertEqual(ctx.exception.code, ERROR_WRITE_FAILED)
        self.assertIn("migration-import-trash-", ctx.exception.instruction)
        self.assertIn("一点都没丢", ctx.exception.instruction)
        # trash 没被清掉，原件在里面可以寻回。
        trash_dirs = [
            path for path in self.new_root.iterdir()
            if path.is_dir() and path.name.startswith("migration-import-trash-")
        ]
        self.assertEqual(len(trash_dirs), 1)
        displaced = list(trash_dirs[0].glob("documents-*"))
        self.assertEqual(len(displaced), 1)
        self.assertEqual(
            (displaced[0] / "doc-old-marker" / "original.pdf").read_bytes(),
            b"old-bytes",
        )


if __name__ == "__main__":
    unittest.main()
