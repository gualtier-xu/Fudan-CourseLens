from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from path_utils import PROJECT_ROOT
from src.runtime.http_api import make_handler
from src.runtime.task_store import TaskStore
from tests.http_services import http_services


class FakeConnectionService:
    def __init__(self, root: Path):
        self.task_store = TaskStore(root / "state.db")
        self.calls = []

    def remote_connection_snapshot(self):
        return {
            "schema": "courselens.remote-connection.v1",
            "overall": {
                "state": "unknown", "activity": "idle",
                "ready_for_dispatch": False, "code": "stale_evidence",
                "observed_at": 1, "expires_at": 2,
            },
            "components": [], "allowed_actions": ["diagnose"],
        }

    def remote_runs_snapshot(self):
        return []

    def remote_connection_action(self, action, *, operation_id, target_id, force):
        self.calls.append((action, operation_id, target_id, force))
        return {"operation_id": operation_id, "action": action, "state": "accepted", "result": {}}


class RemoteConnectionApiTests(unittest.TestCase):
    def setUp(self):
        cache = PROJECT_ROOT / "runtime" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=cache)
        self.service = FakeConnectionService(Path(self.tmp.name))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(self.service), PROJECT_ROOT / "frontend"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.service.task_store.close()
        self.tmp.cleanup()

    def test_remote_connection_snapshot_is_versioned_and_truthful(self):
        with urlopen(f"{self.base}/api/v3/remote-connection") as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["schema"], "courselens.api.v3")
        self.assertFalse(payload["data"]["overall"]["ready_for_dispatch"])
        self.assertEqual(payload["data"]["overall"]["state"], "unknown")

    def test_action_requires_operation_id_and_is_accepted_idempotently(self):
        body = json.dumps({"action": "diagnose", "operation_id": "op-12345678"}).encode()
        request = Request(
            f"{self.base}/api/v3/remote-connection/actions",
            data=body, method="POST", headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["data"]["operation"]["state"], "accepted")
        self.assertEqual(len(self.service.calls), 1)


class RemoteComputeDefaultAllowTests(unittest.TestCase):
    """CLOUD-CONSENT-AUTO-1 U1：云端处理开关退役——读模型只剩连接真值两字段，
    开关动作整对下线，凭据里不再写 remote_enabled。"""

    def setUp(self):
        cache = PROJECT_ROOT / "runtime" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=cache)
        from src.application import CourseLensApplication

        self.service = CourseLensApplication(Path(self.tmp.name))

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def test_switch_actions_are_retired_and_never_write_the_flag(self):
        for action in ("enable-remote-compute", "disable-remote-compute"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(ValueError, "unsupported"):
                    self.service.remote_connection_action(
                        action, operation_id="switch-retired-1"
                    )
        self.assertFalse(self.service.credentials.has_secret("remote_enabled"))

    def test_remote_connection_snapshot_exposes_connection_truth_only(self):
        """W4 四字段闭集收为两字段：只剩连接真值（configured/verified）。"""
        snapshot = self.service.remote_connection_snapshot()
        compute = snapshot.get("remote_compute")
        self.assertIsInstance(compute, dict)
        self.assertEqual(set(compute), {"configured", "verified"})
        self.assertFalse(compute["configured"])
        self.assertFalse(compute["verified"])


class _ReadyConnectionStub:
    """连接侧桩：通道 ready + 可派发，但零网络——U2 单一真值钉专用。"""

    def snapshot(self):
        return {
            "channel_test": {"state": "ready"},
            "overall": {"ready_for_dispatch": True, "state": "ready"},
        }

    def fresh_snapshot(self):
        return self.snapshot()

    def preflight(self, *args, **kwargs):
        return None

    def request_probe(self):
        return None

    def stop(self, *args, **kwargs):
        return True


class RemoteComputeConnectionTruthTests(unittest.TestCase):
    """CLOUD-CONSENT-AUTO-1 U1：入队门=连接就绪（协调器可建），开关不再有话语权；
    连接/授权/完整性缺件仍按 cloud_setup_required 闭集拒绝（51 的真值追补链不变）。"""

    def setUp(self):
        cache = PROJECT_ROOT / "runtime" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=cache)
        from src.application import CourseLensApplication

        self.service = CourseLensApplication(Path(self.tmp.name))
        self.service.remote_connection = _ReadyConnectionStub()

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def _save_worker_secrets(self):
        for name, value in (
            ("github_remote_token", "token-for-tests"),
            ("worker_box_public_key", "box-key"),
            ("worker_signing_public_key", "signing-key"),
        ):
            self.service.credentials.save_secret(name, value)

    def test_channel_ready_alone_opens_the_dispatch_gate(self):
        """连接就绪即可显式派发：零开关动作、零 remote_enabled 凭据。"""
        self._save_worker_secrets()
        snapshot = self.service.remote_compute_snapshot()
        self.assertTrue(snapshot["configured"])
        self.assertTrue(snapshot["verified"])
        self.assertNotIn("enabled", snapshot)
        self.assertNotIn("switch_locked", snapshot)
        self.service._reload_remote_coordinator()
        coordinator = self.service._prepare_remote_coordinator()
        self.assertIs(coordinator, self.service.remote_coordinator)
        self.assertFalse(self.service.credentials.has_secret("remote_enabled"))

    def test_stale_switch_credential_is_inert(self):
        """老装机的 remote_enabled=0（开关关过）不再构成拒绝理由。"""
        self._save_worker_secrets()
        self.service.credentials.save_secret("remote_enabled", "0")
        self.service._reload_remote_coordinator()
        self.assertIsNotNone(self.service.remote_coordinator)
        self.assertIsNotNone(self.service._prepare_remote_coordinator())

    def test_authorization_missing_still_reports_cloud_setup_required(self):
        """未连接（无凭据）仍拒绝，且码固定 cloud_setup_required。"""
        from src.application import CloudSetupRequired

        self.assertIsNone(self.service.remote_coordinator)
        with self.assertRaises(CloudSetupRequired) as captured:
            self.service._prepare_remote_coordinator()
        self.assertEqual(captured.exception.code, "cloud_setup_required")

    def test_invalid_worker_pin_still_reports_cloud_setup_required(self):
        """完整性缺口（签名 pin 非法）仍由 validate 拒绝，不放宽信任判据。"""
        from src.application import CloudSetupRequired

        self._save_worker_secrets()
        self.service.credentials.save_secret("github_worker_dispatch_sha", "not-a-valid-sha")
        self.service._reload_remote_coordinator()
        self.assertIsNone(self.service.remote_coordinator)
        with self.assertRaises(CloudSetupRequired) as captured:
            self.service._prepare_remote_coordinator()
        self.assertEqual(captured.exception.code, "cloud_setup_required")

    def test_startup_truth_reconcile_is_live_for_a_connected_install(self):
        """U1×51 接缝：协调器由连接真值建立后，51 的启动真值核对不再空转。"""
        self._save_worker_secrets()
        self.service._reload_remote_coordinator()
        self.assertIsNotNone(self.service.remote_coordinator)
        self.service.task_store.upsert_remote_run(
            "truth-seam-1", repository="owner/worker", workflow="process.yml",
            run_id=7, remote_state="running", dispatched_at=time.time(),
        )
        seen: list[str] = []
        self.service._reconcile_remote_task_result = (
            lambda task_id: seen.append(str(task_id)) or "running"
        )
        outcomes = self.service.reconcile_remote_results_on_startup()
        self.assertEqual(seen, ["truth-seam-1"])
        self.assertEqual(outcomes, {"running": 1})

    def test_channel_test_action_never_writes_a_switch_flag(self):
        """加密测试只验通道：动作收口后凭据里仍无 remote_enabled。"""
        self._save_worker_secrets()
        calls = []
        service = self.service
        service.start_remote_echo = lambda *args, **kwargs: calls.append("echo") or {}
        operation = service.remote_connection_action(
            "test-channel", operation_id="toggle-test-1"
        )
        self.assertEqual(operation["state"], "accepted")
        self.assertEqual(calls, ["echo"])
        self.assertFalse(service.credentials.has_secret("remote_enabled"))


if __name__ == "__main__":
    unittest.main()
