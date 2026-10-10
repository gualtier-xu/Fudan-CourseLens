from __future__ import annotations

"""DEAD-TASK-PURGE：顽固残留任务根修（2026-10-07 用户点名痛点）。

三分修的行级/链路钉：
- A 启动僵尸清扫：崩溃残留的在途行（queued/running/pausing）在启动链上
  被「取证（recover_for_startup 改写前）→ 落账 failed（zombie_session_cleanup）」，
  自然落入 90 天终态保留窗；与 recover_for_startup 的 paused 恢复扫描状态
  条件互斥，绝不误杀真活跃（远端生命周期白名单）与用户暂停。
- B 用户面「清除卡住的任务」：store 层双门（30 分钟无进展 + 远端无活跃
  run）、幂等、AS10 同款清 busy 键；http_api tasks/clear-stuck 闭集路由
  {"cleared": N} 形态；死因码进 TASK_ERROR_CODES 与前端文案等集（全局等集
  钉在 test_automation，此处带键在场钉）。
- 启动链挂载源码钉：取证恰一次且先于 recover_for_startup，落账恰一次且
  在 recover_remote_runs_on_startup 链上（与 test_closed_result_import 的
  call-once 钉同纪律）。
"""

import json
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock
from urllib.request import Request, urlopen

from src.application import CourseLensApplication
from src.runtime.http_api import TASK_ERROR_CODES, make_handler, public_task
from src.runtime.task_store import (
    REMOTE_BUSY_STATE_KEY_PREFIX,
    STUCK_TASK_STALE_SECONDS,
    USER_CLEARED_STUCK_REASON,
    ZOMBIE_SESSION_CLEANUP_REASON,
    TaskStore,
)
from tests.frontend_family import family_text
from tests.http_services import http_services

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _backdate(store: TaskStore, task_id: str, *, seconds_ago: float) -> None:
    """把任务行 updated_at 回拨（update_task 白名单不含 updated_at，走 SQL）。"""
    db = sqlite3.connect(store.path)
    try:
        db.execute(
            "UPDATE tasks SET updated_at=? WHERE task_id=?",
            (time.time() - float(seconds_ago), task_id),
        )
        db.commit()
    finally:
        db.close()


class ClearStuckTasksTests(unittest.TestCase):
    """B：store 层清除卡住任务的双门语义、边界与幂等。"""

    def _seed(self, store: TaskStore, *, sub: str, kind: str = "subtitle", config: str = "automatic") -> dict:
        task, _ = store.add_task(kind, "c1", sub, {}, config_key=config)
        return task

    def test_clears_only_stale_inflight_without_active_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            stale_queued = self._seed(store, sub="q1")
            stale_running = self._seed(store, sub="r1")
            store.update_task(stale_running["task_id"], state="running")
            _backdate(store, stale_running["task_id"], seconds_ago=3600)
            stale_pausing = self._seed(store, sub="p1")
            store.update_task(stale_pausing["task_id"], state="pausing")
            _backdate(store, stale_pausing["task_id"], seconds_ago=3600)
            _backdate(store, stale_queued["task_id"], seconds_ago=3600)
            fresh_running = self._seed(store, sub="r2")
            store.update_task(fresh_running["task_id"], state="running")
            _backdate(store, fresh_running["task_id"], seconds_ago=60)
            remote_active = self._seed(store, sub="r3")
            store.update_task(remote_active["task_id"], state="running")
            _backdate(store, remote_active["task_id"], seconds_ago=3600)
            store.upsert_remote_run(
                remote_active["task_id"], repository="owner/worker", workflow="worker.yml",
                run_id=1, remote_state="running",
            )
            paused_stale = self._seed(store, sub="pz1")
            store.update_task(paused_stale["task_id"], state="paused")
            _backdate(store, paused_stale["task_id"], seconds_ago=3600)

            cleared = store.clear_stuck_tasks()

            self.assertEqual(
                sorted(cleared),
                sorted([
                    stale_queued["task_id"], stale_running["task_id"], stale_pausing["task_id"],
                ]),
                "恰清除三个在途僵尸：queued/running/pausing 各一",
            )
            for task_id in cleared:
                row = store.get_task(task_id)
                self.assertEqual(row["state"], "failed")
                self.assertEqual(row["error"], USER_CLEARED_STUCK_REASON)
                self.assertIsNotNone(row["finished_at"], "落终态时刻，进 90 天保留窗计时")
            self.assertEqual(store.get_task(fresh_running["task_id"])["state"], "running")
            self.assertEqual(store.get_task(remote_active["task_id"])["state"], "running")
            self.assertEqual(store.get_task(paused_stale["task_id"])["state"], "paused")
            # 幂等：已清行不再命中
            self.assertEqual(store.clear_stuck_tasks(), [])

    def test_clear_stuck_respects_custom_window_and_clears_busy_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            recent = self._seed(store, sub="w1")
            _backdate(store, recent["task_id"], seconds_ago=600)
            self.assertEqual(store.clear_stuck_tasks(stale_seconds=300), [recent["task_id"]])
            # AS10 同款：终态即清 busy 退避键，不随任务残留
            busy = self._seed(store, sub="w2")
            _backdate(store, busy["task_id"], seconds_ago=3600)
            store.set_app_state(f"{REMOTE_BUSY_STATE_KEY_PREFIX}{busy['task_id']}", "3")
            store.clear_stuck_tasks()
            self.assertIsNone(store.get_app_state(f"{REMOTE_BUSY_STATE_KEY_PREFIX}{busy['task_id']}"))
            self.assertEqual(STUCK_TASK_STALE_SECONDS, 1800.0, "默认判定窗=30 分钟")

    def test_dead_task_reason_codes_in_closed_sets(self):
        self.assertIn(ZOMBIE_SESSION_CLEANUP_REASON, TASK_ERROR_CODES)
        self.assertIn(USER_CLEARED_STUCK_REASON, TASK_ERROR_CODES)
        drawer = family_text("tasks-drawer")
        self.assertIn(ZOMBIE_SESSION_CLEANUP_REASON, drawer, "前端文案表同笔带键（等集钉）")
        self.assertIn(USER_CLEARED_STUCK_REASON, drawer, "前端文案表同笔带键（等集钉）")


class StartupZombieSweepTests(unittest.TestCase):
    """A：启动僵尸清扫的取证/落账、互斥边界与启动链挂载钉。"""

    def _service(self, root: Path) -> CourseLensApplication:
        service = CourseLensApplication.__new__(CourseLensApplication)
        service.task_store = TaskStore(root / "state.db")
        service.credentials = Mock()
        service.credentials.has_secret.return_value = False
        service._lock = threading.RLock()
        return service

    def _crash_residue(self, store: TaskStore, *, kind: str = "subtitle") -> dict:
        task, _ = store.add_task(kind, "1", "2", {}, config_key="crash")
        store.update_task(task["task_id"], state="running")
        _backdate(store, task["task_id"], seconds_ago=3600)
        return task

    def test_crash_residue_is_captured_before_rewrite_and_swept_to_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            task = self._crash_residue(service.task_store)

            # 取证在 recover_for_startup 改写之前：此刻仍是原始在途态
            self.assertEqual(
                service._collect_startup_zombie_task_ids(), [task["task_id"]],
            )
            # 生产语义：__init__ 把取证结果挂在实例上，启动链据此落账
            service._startup_zombie_task_ids = service._collect_startup_zombie_task_ids()
            service.task_store.recover_for_startup()
            self.assertEqual(service.recover_remote_runs_on_startup(), 0)
            swept = service.task_store.get_task(task["task_id"])
            self.assertEqual(swept["state"], "failed")
            self.assertEqual(swept["error"], ZOMBIE_SESSION_CLEANUP_REASON)
            self.assertIsNotNone(swept["finished_at"])
            public = public_task(swept, service.task_store)
            self.assertEqual(public["error_code"], ZOMBIE_SESSION_CLEANUP_REASON)
            # 落入 90 天终态保留窗（刚落账仍可见）
            self.assertIn(
                task["task_id"], [row["task_id"] for row in service.task_store.list_tasks()],
            )
            # 幂等：重复落账零改写
            self.assertEqual(service._reconcile_zombie_tasks_on_startup(), 0)

    def test_fresh_inflight_and_user_paused_are_never_zombies(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            fresh, _ = service.task_store.add_task("subtitle", "1", "3", {}, config_key="fresh")
            service.task_store.update_task(fresh["task_id"], state="running")
            _backdate(service.task_store, fresh["task_id"], seconds_ago=60)
            user_paused, _ = service.task_store.add_task("summary", "1", "4", {}, config_key="paused")
            service.task_store.pause_task(user_paused["task_id"])

            self.assertEqual(service._collect_startup_zombie_task_ids(), [])
            service.task_store.recover_for_startup()
            service.recover_remote_runs_on_startup()
            self.assertEqual(service.task_store.get_task(fresh["task_id"])["state"], "paused")
            self.assertNotEqual(
                service.task_store.get_task(fresh["task_id"])["error"],
                ZOMBIE_SESSION_CLEANUP_REASON,
            )
            self.assertEqual(service.task_store.get_task(user_paused["task_id"])["state"], "paused")

    def test_remotely_active_stale_task_is_left_to_truth_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            task, _ = service.task_store.add_task("subtitle", "1", "5", {}, config_key="remote-live")
            service.task_store.update_task(task["task_id"], state="running")
            _backdate(service.task_store, task["task_id"], seconds_ago=3600)
            service.task_store.upsert_remote_run(
                task["task_id"], repository="owner/worker", workflow="worker.yml",
                run_id=1, remote_state="running",
            )

            self.assertEqual(service._collect_startup_zombie_task_ids(), [])
            service.task_store.recover_for_startup()
            service.recover_remote_runs_on_startup()
            row = service.task_store.get_task(task["task_id"])
            self.assertNotEqual(row["state"], "failed", "远端仍活：交给核真/恢复链，绝不误杀")
            self.assertNotEqual(row["error"], ZOMBIE_SESSION_CLEANUP_REASON)

    def test_startup_chain_mount_pins(self):
        app_src = (PROJECT_ROOT / "src" / "application.py").read_text(encoding="utf-8")
        self.assertEqual(
            len(re.findall(r"self\._reconcile_zombie_tasks_on_startup\(\)", app_src)),
            1,
            "僵尸落账只在 recover_remote_runs_on_startup 链上调用恰一次",
        )
        self.assertEqual(
            len(re.findall(r"self\._collect_startup_zombie_task_ids\(\)", app_src)),
            1,
            "僵尸取证只在 __init__ 调用恰一次",
        )
        capture_at = app_src.index("self._collect_startup_zombie_task_ids()")
        recover_at = app_src.index("self.task_store.recover_for_startup()")
        self.assertLess(
            capture_at, recover_at,
            "取证必须先于 recover_for_startup 的在途改写（否则与正常关机暂停行不可区分）",
        )


class _StuckTaskService:
    """Minimal service double: only the task-center stuck-clear surface runs."""

    def __init__(self, root: Path):
        self.task_store = TaskStore(root / "state.db")

    @staticmethod
    def authentication_snapshot():
        return {"state": "ready"}


class ClearStuckHttpRouteTests(unittest.TestCase):
    """B：tasks/clear-stuck 闭集路由形态（成功 + 幂等零清）。"""

    def test_http_clear_stuck_route_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = _StuckTaskService(root)
            store = service.task_store
            stale, _ = store.add_task("subtitle", "c1", "s1", {}, config_key="automatic")
            _backdate(store, stale["task_id"], seconds_ago=3600)
            fresh, _ = store.add_task("summary", "c1", "s2", {}, config_key="summary")
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), make_handler(http_services(service), root)
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"

                def call(payload: dict):
                    body = json.dumps(payload).encode()
                    return lambda: Request(
                        f"{base}/api/v3/tasks/clear-stuck", data=body,
                        headers={"Content-Type": "application/json"}, method="POST",
                    )

                with urlopen(call({})()) as response:
                    self.assertEqual(response.status, 200)
                    value = json.loads(response.read())
                self.assertEqual(value["schema"], "courselens.api.v3")
                self.assertEqual(value["data"], {"cleared": 1})
                self.assertEqual(store.get_task(stale["task_id"])["error"], USER_CLEARED_STUCK_REASON)
                self.assertEqual(store.get_task(fresh["task_id"])["state"], "queued")
                # 幂等：再清零
                with urlopen(call({})()) as response:
                    value = json.loads(response.read())
                self.assertEqual(value["data"], {"cleared": 0})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
