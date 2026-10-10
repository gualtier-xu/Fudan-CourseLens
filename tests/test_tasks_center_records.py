from __future__ import annotations

from tests.frontend_family import family_text

"""TASKS-CENTER-1 U4：任务记录删除面（记录行级删除，绝不触产物）。

覆盖 TaskStore.delete_task / delete_failed_tasks 的行级语义（终态门、
直接耦合行同删、退役预算史表原样保留）与 http_api 的两个闭集
路由（tasks/delete、tasks/delete-failed）的成功/404/409 形态。
"""

import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.runtime.http_api import make_handler
from src.runtime.task_store import TaskStore
from tests.http_services import http_services


class _DeleteTaskService:
    """Minimal service double: only the task-center deletion surface runs."""

    def __init__(self, root: Path):
        self.task_store = TaskStore(root / "state.db")

    @staticmethod
    def authentication_snapshot():
        return {"state": "ready"}


class TaskRecordDeletionTests(unittest.TestCase):
    def _failed_task(self, store: TaskStore, *, with_details: bool = False) -> dict:
        task, _ = store.add_task("subtitle", "c1", "s1", {}, config_key="automatic")
        store.mark_terminal(task["task_id"], "failed", error="boom")
        if with_details:
            store.upsert_v3_metadata(task["task_id"], dedupe_key=f"sub:{task['task_id']}")
            store.upsert_remote_run(
                task["task_id"], repository="owner/repo", workflow="subtitle.yml",
            )
            store.upsert_remote_attempt(task["task_id"], 1, github_status="failure")
            store.set_remote_token_lease(task["task_id"], state="held")
        return store.get_task(task["task_id"])  # type: ignore[return-value]

    def test_delete_task_removes_record_and_coupled_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task = self._failed_task(store, with_details=True)
            task_id = task["task_id"]

            deleted = store.delete_task(task_id)
            self.assertEqual(deleted["task_id"], task_id)
            self.assertIsNone(store.get_task(task_id))
            self.assertIsNone(store.get_v3_metadata(task_id))
            self.assertIsNone(store.get_remote_run(task_id))
            self.assertEqual(store.list_remote_attempts(task_id=task_id), [])
            self.assertEqual(store.list_remote_token_leases(), [])
            # 退役预算史表不动：结算行随库原样保留（惰性历史）
            db = sqlite3.connect(Path(tmp) / "state.db")
            try:
                settled = db.execute(
                    "SELECT COUNT(*) FROM task_budget_settlements WHERE task_id=?", (task_id,)
                ).fetchone()[0]
            finally:
                db.close()
            self.assertEqual(settled, 0)

    def test_delete_task_rejects_active_and_missing_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            task, _ = store.add_task("subtitle", "c1", "s1", {}, config_key="automatic")
            with self.assertRaises(ValueError):
                store.delete_task(task["task_id"])
            self.assertIsNotNone(store.get_task(task["task_id"]))
            with self.assertRaises(KeyError):
                store.delete_task("task:missing")
            # 终态家族（completed/canceled）一律可删
            store.mark_terminal(task["task_id"], "canceled")
            store.delete_task(task["task_id"])
            self.assertIsNone(store.get_task(task["task_id"]))

    def test_delete_failed_tasks_clears_only_failed_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            failed_a = self._failed_task(store)
            failed_b = self._failed_task(store)
            store.add_task("subtitle", "c1", "s2", {}, config_key="automatic")
            completed, _ = store.add_task("summary", "c1", "s3", {}, config_key="automatic")
            store.mark_terminal(completed["task_id"], "completed")

            deleted = store.delete_failed_tasks()
            self.assertEqual(deleted, 2)
            self.assertIsNone(store.get_task(failed_a["task_id"]))
            self.assertIsNone(store.get_task(failed_b["task_id"]))
            self.assertIsNotNone(store.get_task(completed["task_id"]))
            # 空表再清：幂等返回 0
            self.assertEqual(store.delete_failed_tasks(), 0)

    def test_http_delete_routes_closed_set_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = _DeleteTaskService(root)
            store = service.task_store
            task = self._failed_task(store)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), make_handler(http_services(service), root)
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"

                def call(route: str, payload: dict):
                    body = json.dumps(payload).encode()
                    return lambda: Request(
                        f"{base}/api/v3/{route}", data=body,
                        headers={"Content-Type": "application/json"}, method="POST",
                    )

                with self.assertRaises(HTTPError) as missing:
                    urlopen(call("tasks/delete", {"task_id": "task:missing"})())
                self.assertEqual(missing.exception.code, 404)
                self.assertEqual(
                    json.loads(missing.exception.read())["error_code"], "task_not_found"
                )

                with self.assertRaises(HTTPError) as invalid:
                    urlopen(call("tasks/delete", {})())
                self.assertEqual(invalid.exception.code, 400)
                self.assertEqual(
                    json.loads(invalid.exception.read())["error_code"], "task_action_invalid"
                )

                active, _ = store.add_task("summary", "c1", "s9", {}, config_key="automatic")
                with self.assertRaises(HTTPError) as active_error:
                    urlopen(call("tasks/delete", {"task_id": active["task_id"]})())
                self.assertEqual(active_error.exception.code, 409)
                self.assertEqual(
                    json.loads(active_error.exception.read())["error_code"],
                    "task_action_invalid",
                )

                with urlopen(call("tasks/delete", {"task_id": task["task_id"]})()) as response:
                    self.assertEqual(response.status, 200)
                    value = json.loads(response.read())
                self.assertEqual(value["schema"], "courselens.api.v3")
                self.assertEqual(
                    value["data"], {"deleted": True, "task_id": task["task_id"], "state": "failed"}
                )
                self.assertIsNone(store.get_task(task["task_id"]))

                self._failed_task(store)
                self._failed_task(store)
                with urlopen(call("tasks/delete-failed", {})()) as response:
                    self.assertEqual(response.status, 200)
                    value = json.loads(response.read())
                self.assertEqual(value["data"], {"deleted": 2})
                self.assertEqual(
                    [item["state"] for item in store.list_tasks(states=["failed"])], []
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


    def test_drawer_source_bans_generic_school_login_guess(self):
        """U2/U5 源码钉：禁「学校登录服务临时繁忙」类通用猜测兜底回归；
        remote_failed 如实指向云端处理，未知码兜底如实「未能确认」。"""
        root = Path(__file__).resolve().parents[1]
        drawer = family_text("tasks-drawer")
        self.assertNotIn("学校登录服务", drawer)
        self.assertIn("云端处理没有完成", drawer)
        self.assertIn("这次失败的原因未能确认", drawer)


if __name__ == "__main__":
    unittest.main()
