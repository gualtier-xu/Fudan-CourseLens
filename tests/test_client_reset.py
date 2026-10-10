"""Client reset tests (CLIENT-RESET-1, frozen contract 2026-09-16).

Synthetic fixtures only.  Covers: full-chain reset → fresh-state assertions,
the delete_derived both-way matrix, the whole-batch blocker matrix (zero
execution), the receipt written outside the data root with the newest-5
retention cap, credentials cleared via existing deletion APIs (values never
read), and the closed two-repo deletion set.  HTTP-layer tests pin the gate,
strict request types, the reset_confirm_required / reset_blocked error codes,
the accepted-only idempotency ledger, and the deferred request_shutdown.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.application import (
    _CLIENT_RESET_SINGLE_FLIGHT,
    _client_reset_receipt_directory,
    client_reset_perform_action,
)
from src.remote.github_app import GitHubAppError
from src.runtime.http_api import make_handler
from src.runtime.learning_store import LearningStore
from src.runtime.catalog_repository import CatalogRepository
from src.runtime.task_store import TaskStore
from src.runtime.course_data_inventory import (
    courseware_lecture_key,
    subtitle_artifact_key,
)
from src.runtime.student_features import ensure_student_feature_schema


RESET_CONFIRM = "重置"


class _FakeCredentials:
    """Synthetic credential store recording every API call; values never read."""

    def __init__(self) -> None:
        self.accounts = [{"student_id": "24110000001"}, {"student_id": "24110000002"}]
        self.secrets = {
            "github_worker_repo": "student/courselens-worker-abc",
            "github_mailbox_repo": "student/courselens-mailbox-abc",
            "github_app_client_id": "Iv1.synthetic",
            # Worker 公钥：无 github_ 前缀的断开闭集成员，reset 必须显式补删。
            "worker_box_public_key": "synthetic-box-public",
            "worker_signing_public_key": "synthetic-signing-public",
            # Two residuals that are OUTSIDE the github_* closed set:
            "network_github_proxy": "http://127.0.0.1:7890",
            "cloud_state_key": "synthetic-state-key",
        }
        self.calls: list[tuple] = []

    def list_accounts(self):
        self.calls.append(("list_accounts",))
        return list(self.accounts)

    def delete_account(self, student_id):
        self.calls.append(("delete_account", str(student_id)))
        self.accounts = [item for item in self.accounts if item["student_id"] != student_id]
        return True

    def clear_all_session_checkpoints(self):
        self.calls.append(("clear_all_session_checkpoints",))
        return 3

    def delete_deepseek_key(self):
        self.calls.append(("delete_deepseek_key",))
        return True

    def has_secret(self, name):
        self.calls.append(("has_secret", str(name)))
        return name in self.secrets

    def load_secret(self, name):
        self.calls.append(("load_secret", str(name)))
        return str(self.secrets.get(name, ""))

    def list_secret_names(self, *, prefix=""):
        self.calls.append(("list_secret_names", str(prefix)))
        return [name for name in self.secrets if name.startswith(prefix)]

    def delete_secret(self, name):
        self.calls.append(("delete_secret", str(name)))
        return self.secrets.pop(name, None) is not None

    def load_deepseek_key(self):
        raise AssertionError("reset must never read the DeepSeek key value")


class _FakeGitHubApp:
    """Records GitHub API calls; optionally fails or refuses on one path.

    ``fail_on`` raises a transient ``github_unreachable`` (must abort the
    reset); ``refuse_on`` raises the architectural permission-class refusal
    (App token has no delete capability — degrades to manual deletion).
    """

    def __init__(self, fail_on: str = "", refuse_on: str = "") -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_on = str(fail_on)
        self.refuse_on = str(refuse_on)

    def access_token(self, *, minimum_lifetime_seconds: int = 900):
        self.calls.append(("access_token", ""))
        if self.fail_on == "token":
            raise GitHubAppError("GitHub 尚未授权", code="authorization_missing")
        return "synthetic-token"

    def _api(self, method, path, *, token="", expected=(200,), **kwargs):
        self.calls.append((str(method), str(path)))
        if self.fail_on and self.fail_on in str(path):
            raise GitHubAppError("GitHub 连接失败", code="github_unreachable")
        if self.refuse_on and self.refuse_on in str(path):
            raise GitHubAppError("GitHub 返回 HTTP 403", code="permission_denied")
        return SimpleNamespace(status_code=204)


class _FakeSearchIndex:
    def __init__(self) -> None:
        self.refreshes: list[dict] = []

    def request_refresh(self, sub_ids=None, *, force=False):
        self.refreshes.append({"force": bool(force)})
        return {"state": "requested"}


class _ResetHarness(unittest.TestCase):
    """Shared synthetic fixture: two stores, one data root, one receipt root."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # Receipts live OUTSIDE the data root: a separate temp root.
        self._receipts_tmp = tempfile.TemporaryDirectory()
        self.receipt_root = Path(self._receipts_tmp.name) / "reset-receipts"
        self.catalog_repository = CatalogRepository(self.root / "state.db")
        self.task_store = TaskStore(self.root / "state.db")
        self.learning_store = LearningStore(self.root / "learning.db")
        ensure_student_feature_schema(self.root / "learning.db")
        self.credentials = _FakeCredentials()
        self.github = _FakeGitHubApp()
        self.search_index = _FakeSearchIndex()
        patcher = mock.patch(
            "src.application._client_reset_receipt_directory",
            return_value=self.receipt_root,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._receipts_tmp.cleanup()
        self._tmp.cleanup()

    # -- seed helpers ----------------------------------------------------------

    def seed_course(self, course_id: str = "1", title: str = "线性代数") -> None:
        self.catalog_repository.upsert_course(course_id, title, teacher="张老师")
        self.catalog_repository.upsert_lecture(course_id, {
            "sub_id": f"{course_id}-a", "sub_title": "第一讲", "date": "2026-09-01",
        })

    def seed_learning_rows(self) -> None:
        with closing(sqlite3.connect(self.learning_store.path)) as db, db:
            db.execute(
                "INSERT INTO transcript_sources(sub_id,source_path,source_mtime_ns,source_size,"
                "segment_count,updated_at) VALUES(?,?,?,?,?,?)",
                ("1-a", "synthetic.wav", 1, 10, 1, time.time()),
            )
            db.execute(
                "INSERT INTO transcript_segments(sub_id,segment_index,start_ms,end_ms,text,evidence_json)"
                " VALUES(?,?,?,?,?,?)",
                ("1-a", 1, 0, 1000, "合成字幕", ""),
            )
            db.execute(
                "INSERT INTO ai_artifacts(artifact_id,course_id,sub_id,kind,status,input_hash,"
                "prompt_version,content_json,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("art-1", "1", "1-a", "lecture_summary", "completed", "hash-1",
                 "pv-1", "{}", time.time(), time.time()),
            )
            db.execute(
                "INSERT INTO ai_artifact_parts(artifact_id,part_index,source_hash,status,updated_at)"
                " VALUES(?,?,?,?,?)",
                ("art-1", 0, "hash-1", "completed", time.time()),
            )
            db.execute(
                "INSERT INTO model_provenance(artifact_id,model,prompt_version,input_hash,created_at)"
                " VALUES(?,?,?,?,?)",
                ("art-1", "synthetic-model", "pv-1", "hash-1", time.time()),
            )
            db.execute(
                "INSERT INTO ppt_pages(sub_id,page_num,created_sec,pptimgurl,text,ocr_status)"
                " VALUES(?,?,?,?,?,?)",
                ("1-a", 1, 0, "http://synthetic/slide.png", "幻灯片文本", "done"),
            )
            db.execute(
                "INSERT INTO watch_progress(sub_id,course_id,position_ms,duration_ms,completed,"
                "playback_rate,updated_at) VALUES(?,?,?,?,?,?,?)",
                ("1-a", "1", 1000, 2000, 0, 1.0, time.time()),
            )

    def seed_artifact_directories(self) -> dict[str, Path]:
        subtitle_dir = self.root / "artifacts" / "subtitles" / subtitle_artifact_key("1", "1-a")
        courseware_dir = self.root / "courseware" / courseware_lecture_key("1", "1-a")
        subtitle_dir.mkdir(parents=True)
        courseware_dir.mkdir(parents=True)
        (subtitle_dir / "segments.json").write_text("{}", encoding="utf-8")
        (courseware_dir / "slide-1.png").write_bytes(b"png")
        return {"subtitles": subtitle_dir, "courseware": courseware_dir}

    def seed_documents_dir(self) -> Path:
        document_dir = self.root / "documents" / "doc-1"
        document_dir.mkdir(parents=True)
        (document_dir / "notes.txt").write_text("合成讲义", encoding="utf-8")
        (self.root / "migration-backups").mkdir(parents=True)
        (self.root / "migration-backups" / "keep.txt").write_text("x", encoding="utf-8")
        return document_dir

    def reset_call(self, *, operation_id: str = "reset-op-0001", confirm_typed: str = RESET_CONFIRM,
                   delete_derived: bool = False, delete_github_repos: bool = False) -> dict:
        return client_reset_perform_action(
            learning_store=self.learning_store,
            catalog_repository=self.catalog_repository,
            task_store=self.task_store,
            credentials=self.credentials,
            github_app=self.github,
            data_root=self.root,
            search_index=self.search_index,
            operation_id=operation_id,
            confirm_typed=confirm_typed,
            delete_derived=delete_derived,
            delete_github_repos=delete_github_repos,
        )

    def learning_row_count(self, table: str) -> int:
        with closing(sqlite3.connect(self.learning_store.path)) as db, db:
            return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


class ClientResetEngineTests(_ResetHarness):
    def test_full_chain_reset_restores_fresh_state_and_preserves_derived(self):
        self.seed_course()
        self.seed_learning_rows()
        directories = self.seed_artifact_directories()
        document_dir = self.seed_documents_dir()
        (self.root / "updates").mkdir(parents=True)
        (self.root / "updates" / "state.json").write_text("{}", encoding="utf-8")

        receipt = self.reset_call()

        self.assertEqual(receipt["schema"], "courselens.client-reset-action-result.v1")
        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(receipt["action"], "reset")
        result = receipt["result"]
        self.assertTrue(result["preserved_derived"])
        self.assertEqual(result["deleted"]["accounts"], 2)
        self.assertEqual(result["deleted"]["session_checkpoints"], 3)
        self.assertTrue(result["deleted"]["deepseek_key"])
        self.assertEqual(result["deleted"]["documents"], 1)
        self.assertEqual(result["deleted"]["github_secrets"], 5)  # github_* + 两把 Worker 公钥
        self.assertEqual(result["databases_rebuilt"], ["state.db", "learning.db"])

        # Local state is fresh: catalog, tasks, ledger, records are gone.
        self.assertEqual(self.catalog_repository.courses(), [])
        self.assertEqual(self.task_store.count(), 0)
        self.assertIsNone(self.task_store.get_app_state("anything", None))
        self.assertEqual(self.learning_row_count("watch_progress"), 0)
        # Derived groups survive the base reset and re-attach on return.
        self.assertEqual(self.learning_row_count("transcript_sources"), 1)
        self.assertEqual(self.learning_row_count("transcript_segments"), 1)
        self.assertEqual(self.learning_row_count("ai_artifacts"), 1)
        self.assertEqual(self.learning_row_count("ai_artifact_parts"), 1)
        self.assertEqual(self.learning_row_count("model_provenance"), 1)
        self.assertEqual(self.learning_row_count("ppt_pages"), 1)
        self.assertTrue(directories["subtitles"].exists())
        self.assertTrue(directories["courseware"].exists())
        self.assertFalse(document_dir.exists())
        self.assertFalse((self.root / "migration-backups").exists())
        self.assertFalse((self.root / "updates" / "state.json").exists())
        # The rebuilt stores keep working (existing ensure paths).
        self.seed_course("2")
        self.assertEqual(len(self.catalog_repository.courses()), 1)
        self.assertTrue(self.search_index.refreshes[0]["force"])

    def test_delete_derived_true_clears_the_three_derived_groups(self):
        self.seed_course()
        self.seed_learning_rows()
        directories = self.seed_artifact_directories()

        receipt = self.reset_call(operation_id="reset-op-0002", delete_derived=True)

        self.assertNotIn("preserved_derived", receipt["result"])
        cleared = receipt["result"]["cleared_rows"]
        self.assertGreaterEqual(cleared["transcript_segments"], 1)
        self.assertGreaterEqual(cleared["ai_artifacts"], 1)
        self.assertGreaterEqual(cleared["ppt_pages"], 1)
        for table in ("transcript_sources", "transcript_segments", "ai_artifacts",
                      "ai_artifact_parts", "model_provenance", "ppt_pages"):
            self.assertEqual(self.learning_row_count(table), 0, table)
        self.assertFalse(directories["subtitles"].exists())
        self.assertFalse(directories["courseware"].exists())
        self.assertGreater(receipt["result"]["bytes_freed"], 0)

    def test_blocker_matrix_rejects_with_zero_execution(self):
        """U5 第十四案根治后的阻塞矩阵：本地阻塞源在重置内被释放（不再构成
        拒绝，更不再是永久死锁），唯一保留的拒绝是「远端运行在途」——外部
        GitHub 运行稍后完成的回写不该落进重建后的空库。"""
        self.seed_course()
        # 本地可释放类：任务/导入/cleanup/规则/租约 → 重置照常执行并披露释放计数
        releasable = {
            "active_task": lambda: self.task_store.add_task("subtitle", "1", "1-a", {}),
            "active_automation_import": lambda: self._insert(
                "INSERT INTO automation_imports(artifact_id,state,observed_at,updated_at)"
                " VALUES(1,'importing',?,?)", (time.time(), time.time())),
            "cleanup_pending": lambda: self._insert(
                "INSERT INTO remote_run_attempts(task_id,attempt,import_state,updated_at)"
                " VALUES('remote-1',1,'cleanup_pending',?)", (time.time(),)),
            "automation_rule": lambda: self._insert(
                "INSERT INTO automation_course_rules(profile_id,course_id,rule_json,updated_at)"
                " VALUES('p1','1','{}',?)", (time.time(),)),
            "active_token_lease": lambda: self._insert(
                "INSERT INTO remote_token_leases(task_id,state,updated_at)"
                " VALUES('remote-1','held',?)", (time.time(),)),
        }
        index = 0
        for code, seed in releasable.items():
            index += 1
            seed()
            receipt = self.reset_call(operation_id=f"reset-rel-{index:04d}")
            self.assertEqual(receipt["status"], "accepted", code)
            self.assertIn("released", receipt["result"], code)
            # 红钉④：重建后活动工作全零——tasks/remote_runs/remote_attempts
            # 一个不剩，重新初始化也不复活。
            self.assertEqual(self.task_store.count(), 0, code)
            self.assertEqual(self.task_store.active_remote_run_count(), 0, code)
            self.assertEqual(self.task_store.active_remote_attempt_count(), 0, code)
            self.assertEqual(self.task_store.automation_profile_ids(), [], code)
            self.assertEqual(len(list(self.task_store.list_remote_token_leases())), 0, code)
            self._clear_blockers()
        # 唯一保留的拒绝：远端运行在途 → 整单拒绝、零执行
        document_dir = self.seed_documents_dir()
        self.seed_learning_rows()
        self._insert(
            "INSERT INTO remote_runs(task_id,repository,workflow,updated_at)"
            " VALUES('remote-1','owner/repo','x.yml',?)", (time.time(),))
        try:
            receipt = self.reset_call(operation_id="reset-blk-remote-0001")
        finally:
            self._clear_blockers()
        self.assertEqual(receipt["status"], "rejected")
        self.assertEqual([item["code"] for item in receipt["blockers"]], ["active_remote_run"])
        self.assertEqual(receipt["blockers"][0]["count"], 1)
        # Zero execution: no files or rows changed by the refused call.
        self.assertTrue(document_dir.exists())
        self.assertEqual(self.learning_row_count("transcript_segments"), 1)

    def _insert(self, sql: str, args: tuple) -> None:
        with closing(sqlite3.connect(self.root / "state.db")) as db, db:
            db.execute(sql, args)

    def _clear_blockers(self) -> None:
        with closing(sqlite3.connect(self.root / "state.db")) as db, db:
            for table in ("tasks", "remote_runs", "automation_imports",
                          "remote_run_attempts", "automation_course_rules",
                          "remote_token_leases"):
                db.execute(f"DELETE FROM {table}")

    def test_typed_confirmation_is_enforced_by_the_engine(self):
        for wrong in ("", "确认", "重置 ", " reset"):
            with self.assertRaises(Exception) as caught:
                self.reset_call(operation_id="reset-cfg-0001", confirm_typed=wrong)
            self.assertEqual(getattr(caught.exception, "code", ""), "reset_confirm_required")
        # Nothing ran: no receipt written, stores untouched.
        self.assertFalse(self.receipt_root.exists())

    def test_receipt_is_written_outside_the_data_root_with_retention_cap(self):
        self.seed_course()
        self.receipt_root.mkdir(parents=True)
        for index in range(5):
            stale = self.receipt_root / f"reset-2026010{index}T000000.000Z.json"
            stale.write_text("{}", encoding="utf-8")
            os.utime(stale, (index * 1000, index * 1000))
        receipt = self.reset_call()
        written = self.receipt_root / receipt["result"]["receipt"]
        self.assertTrue(written.exists())
        self.assertNotIn(str(self.root), str(written.resolve()))
        manifest = json.loads(written.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "courselens.course-data-summary.v1")
        self.assertTrue(manifest["rows"] or manifest["page"]["total"] == 0)
        names = sorted(item.name for item in self.receipt_root.glob("reset-*.json"))
        self.assertEqual(len(names), 5)  # newest 5 kept
        self.assertIn(written.name, names)
        self.assertNotIn("reset-20260100T000000.000Z.json", names)  # oldest pruned
        # The manifest was captured BEFORE the destructive phase: the seeded
        # course is still present in it.
        self.assertGreaterEqual(manifest["page"]["total"], 1)

    def test_managed_install_layout_redirects_receipts_outside_the_data_root(self):
        managed = (self.root / "install").resolve()
        (managed / "state").mkdir(parents=True)
        (managed / "state" / "install-layout.json").write_text(
            json.dumps({"schema": "courselens.managed-install.v1"}), encoding="utf-8")
        project_root = managed / "versions" / "v1"
        project_root.mkdir(parents=True)
        environment = {"COURSELENS_INSTALL_ROOT": str(managed)}
        with mock.patch.dict(os.environ, environment), \
             mock.patch("src.application.PROJECT_ROOT", project_root):
            directory = _client_reset_receipt_directory()
        self.assertEqual(directory, managed / "reset-receipts")
        self.assertNotEqual(Path(directory).resolve().parent, self.root.resolve())

    def test_credentials_are_cleared_via_apis_and_values_never_read(self):
        self.seed_course()
        self.reset_call(operation_id="reset-op-0003", delete_github_repos=True)
        calls = [entry[0] for entry in self.credentials.calls]
        self.assertIn("list_accounts", calls)
        self.assertEqual(calls.count("delete_account"), 2)
        self.assertIn("clear_all_session_checkpoints", calls)
        self.assertIn("delete_deepseek_key", calls)
        # Repo deletion reads only the two closed-set names; the sweep reads
        # names, never values; the DeepSeek value is never loaded.
        self.assertEqual(self.credentials.calls.count(("load_secret", "github_worker_repo")), 1)
        self.assertEqual(self.credentials.calls.count(("load_secret", "github_mailbox_repo")), 1)
        self.assertNotIn(("load_secret", "github_app_client_id"), self.credentials.calls)
        # Worker 公钥属断开闭集（github_app.disconnect 同族），无前缀也被补删；
        # 真正的连接无关残留则必须越过清扫存活。
        self.assertNotIn("worker_box_public_key", self.credentials.secrets)
        self.assertNotIn("worker_signing_public_key", self.credentials.secrets)
        self.assertIn("network_github_proxy", self.credentials.secrets)
        self.assertIn("cloud_state_key", self.credentials.secrets)

    def test_repo_deletion_uses_exactly_the_two_recorded_repositories(self):
        self.seed_course()
        self.seed_documents_dir()
        receipt = self.reset_call(operation_id="reset-op-0004", delete_github_repos=True)
        delete_calls = [entry for entry in self.github.calls if entry[0] == "DELETE"]
        self.assertEqual(delete_calls, [
            ("DELETE", "/repos/student/courselens-worker-abc"),
            ("DELETE", "/repos/student/courselens-mailbox-abc"),
        ])
        self.assertEqual(receipt["result"]["repos_deleted"], [
            "student/courselens-worker-abc", "student/courselens-mailbox-abc",
        ])

    def test_repo_deletion_failure_aborts_before_any_local_change(self):
        self.seed_course()
        self.seed_learning_rows()
        directories = self.seed_artifact_directories()
        document_dir = self.seed_documents_dir()
        self.github.fail_on = "mailbox"
        with self.assertRaises(GitHubAppError):
            self.reset_call(operation_id="reset-op-0005", delete_github_repos=True)
        # The worker repo DELETE happened, the mailbox DELETE failed: nothing
        # local was touched and credentials were not cleared.
        self.assertTrue(directories["subtitles"].exists())
        self.assertTrue(document_dir.exists())
        self.assertEqual(self.learning_row_count("transcript_segments"), 1)
        self.assertEqual(len(self.credentials.accounts), 2)
        self.assertIn("github_worker_repo", self.credentials.secrets)
        # The pre-reset receipt manifest is a harmless diagnostic export
        # written before the remote phase; it lives outside the data root.
        if self.receipt_root.exists():
            for stray in self.receipt_root.iterdir():
                self.assertNotIn(str(self.root), str(stray.resolve()))

    def test_permission_refusal_degrades_to_manual_deletion_and_reset_proceeds(self):
        """T3 降级：App 令牌没有删仓权限（架构性 permission 类拒绝）时本地重置
        照常完成——被拒仓库记入手删回执（受信 settings 链接 + 闭集 reason），
        其余仓库照常 API 删除，本地数据全部清理。"""
        self.seed_course()
        self.seed_documents_dir()
        self.github.refuse_on = "mailbox"
        receipt = self.reset_call(operation_id="reset-op-0007", delete_github_repos=True)
        delete_calls = [entry for entry in self.github.calls if entry[0] == "DELETE"]
        self.assertEqual(delete_calls, [
            ("DELETE", "/repos/student/courselens-worker-abc"),
            ("DELETE", "/repos/student/courselens-mailbox-abc"),
        ], "两个记录在案的仓库都尝试删除（被拒不中断循环）")
        self.assertEqual(receipt["result"]["repos_deleted"], ["student/courselens-worker-abc"])
        self.assertEqual(receipt["result"]["repos_manual_deletion"], [{
            "repo": "student/courselens-mailbox-abc",
            "settings_url": "https://github.com/student/courselens-mailbox-abc/settings",
            "reason": "app_token_cannot_delete",
        }])
        # 本地重置照常：文档已清、凭据已扫（与 abort 语义形成对照）
        self.assertFalse((self.root / "documents" / "doc-1").exists())
        self.assertNotIn("github_worker_repo", self.credentials.secrets)

    def test_concurrent_reset_is_rejected_single_flight(self):
        self.seed_course()
        with _CLIENT_RESET_SINGLE_FLIGHT:
            with self.assertRaises(Exception) as caught:
                self.reset_call(operation_id="reset-op-0006")
        self.assertEqual(getattr(caught.exception, "code", ""), "reset_blocked")


class _ClientResetService:
    """Narrow double exposing exactly the container paths the routes touch."""

    def __init__(self, root: Path):
        self.root = root
        self.catalog_repository = CatalogRepository(root / "state.db")
        self.task_store = TaskStore(root / "state.db")
        self.learning_store = LearningStore(root / "learning.db")
        ensure_student_feature_schema(root / "learning.db")
        self.credentials = _FakeCredentials()
        self.github = _FakeGitHubApp()
        self.search_index = _FakeSearchIndex()
        self.shutdown_requests: list[str] = []
        self.learning = SimpleNamespace(repository=self.learning_store)
        self._auth_state = "ready"
        self.auth_catalog = SimpleNamespace(
            catalog=self.catalog_repository,
            authentication_snapshot=lambda: {"state": self._auth_state},
        )
        self.tasks = SimpleNamespace(repository=self.task_store)
        self.lifecycle = SimpleNamespace(
            request_shutdown=self._record_shutdown,
        )
        self.settings = SimpleNamespace(
            client_reset_action=self._client_reset_action,
        )

    def _record_shutdown(self, reason: str) -> bool:
        self.shutdown_requests.append(str(reason))
        return True

    def _client_reset_action(self, *, operation_id: str, confirm_typed: str = "",
                             delete_derived: bool = False,
                             delete_github_repos: bool = False) -> dict:
        return client_reset_perform_action(
            learning_store=self.learning_store,
            catalog_repository=self.catalog_repository,
            task_store=self.task_store,
            credentials=self.credentials,
            github_app=self.github,
            data_root=self.root,
            search_index=self.search_index,
            operation_id=str(operation_id),
            confirm_typed=str(confirm_typed or ""),
            delete_derived=bool(delete_derived),
            delete_github_repos=bool(delete_github_repos),
        )

    def set_auth_state(self, state: str) -> None:
        self._auth_state = state


class ClientResetRouteTests(_ResetHarness):
    def setUp(self) -> None:
        super().setUp()
        self.service = _ClientResetService(self.root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service, self.root))
        self._server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{server.server_port}"

    def tearDown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        super().tearDown()

    def post(self, path: str, body: dict):
        request = Request(
            f"{self.base}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request) as response:
            return response.status, json.loads(response.read())["data"]

    def expect_error(self, path: str, *, status: int, error_code: str, body: dict):
        request = Request(
            f"{self.base}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, status)
        self.assertEqual(json.loads(caught.exception.read())["error_code"], error_code)

    def seed_catalog(self) -> None:
        self.catalog_repository = self.service.catalog_repository
        self.task_store = self.service.task_store
        self.learning_store = self.service.learning_store
        self.seed_course()

    def wait_for_shutdown(self, expected: list[str]) -> list[str]:
        """The route fires request_shutdown after the reply is written; poll."""
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self.service.shutdown_requests == expected:
                break
            time.sleep(0.02)
        return self.service.shutdown_requests

    def test_route_joins_the_course_session_gate(self):
        self.seed_catalog()
        self.service.set_auth_state("idle")
        self.expect_error(
            "/api/v3/client-reset/actions",
            body={"action": "reset", "operation_id": "gate-op-0001", "confirm_typed": RESET_CONFIRM},
            status=401, error_code="fudan_login_required",
        )
        self.service.set_auth_state("ready")

    def test_route_validates_strict_types_and_closed_action(self):
        self.seed_catalog()
        self.expect_error(
            "/api/v3/client-reset/actions",
            body={"action": "factory-wipe", "operation_id": "valid-op-0001"},
            status=400, error_code="reset_action_invalid",
        )
        self.expect_error(
            "/api/v3/client-reset/actions",
            body={"action": "reset", "operation_id": "short"},
            status=400, error_code="reset_action_invalid",
        )
        self.expect_error(
            "/api/v3/client-reset/actions",
            body={"action": "reset", "operation_id": "valid-op-0002",
                  "confirm_typed": RESET_CONFIRM, "delete_derived": "yes"},
            status=400, error_code="reset_action_invalid",
        )
        self.expect_error(
            "/api/v3/client-reset/actions",
            body={"action": "reset", "operation_id": "valid-op-0003",
                  "confirm_typed": None},
            status=400, error_code="reset_action_invalid",
        )
        self.expect_error(
            "/api/v3/client-reset/actions",
            body={"action": "reset", "operation_id": "valid-op-0004",
                  "confirm_typed": "确认"},
            status=400, error_code="reset_confirm_required",
        )

    def test_route_blockers_map_to_reset_blocked_409(self):
        """U5 后唯一仍触发 reset_blocked 的路径：远端运行在途（本地阻塞已被
        释放语义接管）。"""
        self.seed_catalog()
        self.service.task_store.add_task("subtitle", "1", "1-a", {})
        with closing(sqlite3.connect(self.service.task_store.path)) as db, db:
            db.execute(
                "INSERT INTO remote_runs(task_id,repository,workflow,updated_at)"
                " VALUES('remote-1','owner/repo','x.yml',?)", (time.time(),))
        self.expect_error(
            "/api/v3/client-reset/actions",
            body={"action": "reset", "operation_id": "blocked-op-001",
                  "confirm_typed": RESET_CONFIRM},
            status=409, error_code="reset_blocked",
        )
        # Rejections are retryable: nothing entered the ledger.
        self.assertIsNone(self.service.task_store.get_app_state(
            "client-reset:blocked-op-001", None))

    def test_route_accepts_stores_ledger_and_requests_shutdown(self):
        self.seed_catalog()
        self.seed_learning_rows()
        body = {"action": "reset", "operation_id": "accept-op-0001",
                "confirm_typed": RESET_CONFIRM,
                "delete_derived": False, "delete_github_repos": True}
        status, receipt = self.post("/api/v3/client-reset/actions", body)
        self.assertEqual(status, 200)
        self.assertEqual(receipt["schema"], "courselens.client-reset-action-result.v1")
        self.assertEqual(receipt["status"], "accepted")
        self.assertEqual(receipt["result"]["repos_deleted"],
                         ["student/courselens-worker-abc", "student/courselens-mailbox-abc"])
        # Accepted receipts only, stored in the freshly rebuilt state.db.
        stored = self.service.task_store.get_app_state("client-reset:accept-op-0001", None)
        self.assertEqual(stored["receipt"], receipt)
        # Shutdown fires after the reply, with the reset reason, exactly once.
        self.assertEqual(self.wait_for_shutdown(["reset"]), ["reset"])
        # Replay returns the stored receipt without re-executing.
        self.service.shutdown_requests.clear()
        _, replay = self.post("/api/v3/client-reset/actions", body)
        self.assertEqual(replay, receipt)
        self.assertEqual(self.service.shutdown_requests, [])
        # Same operation_id with different flags conflicts.
        self.expect_error(
            "/api/v3/client-reset/actions",
            body={"action": "reset", "operation_id": "accept-op-0001",
                  "confirm_typed": RESET_CONFIRM, "delete_derived": True},
            status=409, error_code="operation_id_conflict",
        )

    def test_route_github_failure_maps_to_bad_gateway_without_local_change(self):
        self.seed_catalog()
        self.seed_documents_dir()
        self.service.github.fail_on = "mailbox"
        self.expect_error(
            "/api/v3/client-reset/actions",
            body={"action": "reset", "operation_id": "ghfail-op-001",
                  "confirm_typed": RESET_CONFIRM, "delete_github_repos": True},
            status=502, error_code="github_unreachable",
        )
        self.assertTrue((self.root / "documents" / "doc-1").exists())
        self.assertEqual(self.service.shutdown_requests, [])


if __name__ == "__main__":
    unittest.main()
