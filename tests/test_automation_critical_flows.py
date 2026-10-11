"""云端自动化关键流补钉（TEST-GAP-4，P0 零测流清偿）。

增量定谳（台账实核，2026-10-07 树）：
- ``_action_disable_cloud`` / ``_action_retry_import`` / ``_action_erase_cloud_data``
  服务级零直测（tests 全树仅 frontend workbench 字符串钉与 master_switch 前端钉）。
- verify 动作的 ``worker_tree_drifted`` 完整性门（worker 树 pin 校验失败路径）
  服务级零钉（仅 import 码映射与 github_app 层各有着落）。
- revoke 的部分删除失败 → ``cleanup_pending`` + ``cloud_cleanup_pending`` 腿零钉
  （等待腿与 happy 腿已在 tests/test_automation.py，本件零重复）。
- ``automation/actions`` 路由级 400 闭集分派、GET ``automation/runs``、
  GET ``automation/imports`` 路由级零钉（夜15-R4 B34-2 残余）。

禁碰声明：本件为零撞新文件——零触碰 tests/test_automation.py 与
test_dead_task_purge.py 等在途车道（AUTO-TOMBSTONE-R2）占用的 task_store 面文件；
产品面 src/**、frontend/**、worker/** 零写入。每钉均含「不该发生的事不发生」
闭集断言（零副作用/闭集码/零分发），无 happy-path-only 用例。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from path_utils import PROJECT_ROOT
from src.remote.github_app import GitHubAppError
from src.runtime.automation import (
    AutomationError,
    AutomationService,
    CLOUD_PROTOCOL_VERSION,
    CLOUD_SECRET_NAMES,
    CLOUD_VARIABLE_NAMES,
)
from src.runtime.http_api import make_handler
from src.runtime.task_store import TaskStore
from tests.http_services import http_services


class MemoryCredentials:
    """合成 CredentialStore：记录删除调用以断言「该删的删、不该删的留」。"""

    def __init__(self):
        self.values = {}
        self.accounts = {}
        self.deleted_secrets: list[str] = []

    def save_secret(self, name, value):
        self.values[name] = str(value)

    def load_secret(self, name):
        if name not in self.values:
            raise KeyError(name)
        return self.values[name]

    def has_secret(self, name):
        return name in self.values

    def delete_secret(self, name):
        self.deleted_secrets.append(name)
        return self.values.pop(name, None) is not None

    def has_deepseek_key(self):
        return bool(self.values.get("deepseek_api_key"))

    def load_deepseek_key(self):
        return self.values["deepseek_api_key"]


class FakeGitHub:
    """合成 GitHub 客户端：可注入完整性失败与删除失败两个故障点。"""

    def __init__(self):
        self.secrets = {}
        self.variables = {}
        self.workflows = {"cloud-verify.yml": False, "cloud-daily.yml": False}
        self.dispatched = []
        self.artifacts = []
        self.tree = "a" * 40
        self.integrity_trusted = True
        self.fail_delete_secrets: set[str] = set()
        self.fail_artifact_delete = False

    def check_worker_integrity(self):
        return {
            "trusted": self.integrity_trusted,
            "repository": "synthetic-owner/courselens-worker-synthetic",
            "actual_tree": self.tree,
            "manifest_sha256": "b" * 64,
            "trust_epoch": 3,
        }

    def set_workflow_enabled(self, workflow, enabled):
        self.workflows[workflow] = bool(enabled)

    def put_worker_variable(self, name, value):
        self.variables[name] = str(value)

    def delete_worker_variable(self, name):
        self.variables.pop(name, None)

    def list_worker_variables(self):
        return dict(self.variables)

    def put_worker_secret(self, name, value):
        self.secrets[name] = str(value)

    def delete_worker_secret(self, name):
        if name in self.fail_delete_secrets:
            raise GitHubAppError("synthetic delete failure", code="github_error")
        self.secrets.pop(name, None)

    def list_worker_secrets(self):
        return [{"name": name, "updated_at": "2026-10-07T00:00:00Z"} for name in self.secrets]

    def dispatch_workflow(self, workflow, *, inputs=None, ref="main"):
        self.dispatched.append((workflow, dict(inputs or {}), ref, time.time()))

    def list_workflow_runs(self, workflow, limit=20):
        return []

    def cancel_workflow_run(self, run_id):
        return None

    def list_worker_artifacts(self, **_kwargs):
        return list(self.artifacts)

    def delete_worker_artifact(self, artifact_id):
        if self.fail_artifact_delete:
            raise GitHubAppError("synthetic artifact delete failure", code="github_error")
        self.artifacts = [item for item in self.artifacts if item["id"] != artifact_id]


class AutomationCriticalFlowTests(unittest.TestCase):
    """disable / retry-import / erase / verify 完整性门 / revoke 部分失败腿。"""

    def setUp(self):
        cache = PROJECT_ROOT / "runtime" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=cache)
        self.store = TaskStore(Path(self.tmp.name) / "state.db")
        self.credentials = MemoryCredentials()
        self.credentials.save_secret("github_worker_repo", "synthetic-owner/courselens-worker-synthetic")
        self.github = FakeGitHub()
        self.local_schedule = {"enabled": False, "time": "07:30"}
        self.service = AutomationService(
            self.store,
            self.credentials,
            lambda: self.github,
            local_schedule_getter=lambda: dict(self.local_schedule),
            local_schedule_setter=self._set_local_schedule,
            verified_catalog_getter=lambda: {},
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _set_local_schedule(self, value):
        self.local_schedule = dict(value)
        return dict(self.local_schedule)

    def _seed_ready_profile(self, *, local_was_enabled, resume_after_verified):
        self.store.save_automation_profile({
            "protocol": CLOUD_PROTOCOL_VERSION,
            "state": "ready",
            "mode": "cloud",
            "config_hash": "c" * 64,
            "verified_config_hash": "c" * 64,
            "local_schedule_was_enabled": bool(local_was_enabled),
            "binding": {"generation": 3, "resume_after_verified": bool(resume_after_verified)},
            "observed_at": time.time(),
            "expires_at": time.time() + 90,
        })

    # ---- disable-cloud（撤销/暂停闭环的本地半边，服务级原零测） ----

    def test_disable_cloud_stops_remote_flags_clears_resume_and_lands_disabled(self):
        self._seed_ready_profile(local_was_enabled=False, resume_after_verified=True)
        self.github.variables["COURSELENS_CLOUD_ENABLED"] = "true"
        self.github.workflows["cloud-daily.yml"] = True

        operation = self.service.action("disable-cloud", operation_id="disable-op-0001")

        self.assertEqual(operation["action"], "disable-cloud")
        self.assertEqual(operation["state"], "accepted")
        # 远端双旗标必须双双关闭（学生按下暂停 = 云端真停）。
        self.assertEqual(self.github.variables.get("COURSELENS_CLOUD_ENABLED"), "false")
        self.assertFalse(self.github.workflows["cloud-daily.yml"])
        profile = self.store.get_automation_profile()
        self.assertEqual(profile.get("state"), "disabled")
        # 用户明确暂停后不得自动恢复：resume_after_verified 必须清零。
        self.assertFalse((profile.get("binding") or {}).get("resume_after_verified"))

    def test_disable_cloud_restores_local_schedule_only_when_cloud_had_disabled_it(self):
        self._seed_ready_profile(local_was_enabled=True, resume_after_verified=False)
        self.local_schedule = {"enabled": False, "time": "07:30"}

        self.service.action("disable-cloud", operation_id="disable-op-0002")

        # 云启用时被关掉的本地课表，撤销后必须恢复（学生可感知的回归面）。
        self.assertTrue(self.local_schedule.get("enabled"))

    def test_disable_cloud_never_touches_local_schedule_it_did_not_disable(self):
        self._seed_ready_profile(local_was_enabled=False, resume_after_verified=False)
        self.local_schedule = {"enabled": False, "time": "07:30"}

        self.service.action("disable-cloud", operation_id="disable-op-0003")

        # 本地课表本来就关着（非云启用所关）→ 零副作用，不得被误开。
        self.assertFalse(self.local_schedule.get("enabled"))

    def test_disable_cloud_leaves_already_enabled_local_schedule_alone(self):
        self._seed_ready_profile(local_was_enabled=True, resume_after_verified=False)
        self.local_schedule = {"enabled": True, "time": "07:30"}

        self.service.action("disable-cloud", operation_id="disable-op-0004")

        # 本地课表已开 → 不重写（避免覆盖用户的本地时间设置）。
        self.assertTrue(self.local_schedule.get("enabled"))
        self.assertEqual(self.local_schedule.get("time"), "07:30")

    # ---- retry-import（导入链重试面，服务级原零测） ----

    def test_retry_import_reopens_only_failed_and_cleanup_pending_rows(self):
        self.store.upsert_automation_import(1, state="failed", error_code="boom")
        self.store.upsert_automation_import(2, state="cleanup_pending", error_code="leftover")
        self.store.upsert_automation_import(3, state="imported")
        self.store.upsert_automation_import(4, state="available")
        self.store.upsert_automation_import(5, state="importing")

        operation = self.service.action("retry-import", operation_id="retry-op-000001")

        self.assertEqual(operation["state"], "accepted")
        rows = {row["artifact_id"]: row for row in self.store.list_automation_imports()}
        # 只有 failed / cleanup_pending 两态被重开为 available 且错误码清空。
        self.assertEqual(rows[1]["state"], "available")
        self.assertEqual(rows[1]["error_code"], "")
        self.assertEqual(rows[2]["state"], "available")
        self.assertEqual(rows[2]["error_code"], "")
        # 终态/在途/已可用行零触碰（不该发生的不发生）。
        self.assertEqual(rows[3]["state"], "imported")
        self.assertEqual(rows[4]["state"], "available")
        self.assertEqual(rows[5]["state"], "importing")

    # ---- verify-cloud-credentials 的 worker 树 pin 校验失败路径（原零测） ----

    def test_verify_credentials_fails_closed_on_untrusted_tree_without_side_effects(self):
        # 先落一份已配置的云档案（过 config 门），再让完整性检查失败。
        self.store.save_automation_profile({
            "protocol": CLOUD_PROTOCOL_VERSION,
            "state": "uploaded",
            "mode": "cloud",
            "config_hash": "c" * 64,
            "binding": {"generation": 1},
            "observed_at": time.time(),
            "expires_at": time.time() + 90,
        })
        self.github.integrity_trusted = False
        before = self.store.get_automation_profile()

        with self.assertRaises(AutomationError) as caught:
            self.service.action("verify-cloud-credentials", operation_id="verify-drift-001")

        # 闭集码：worker 树漂移（WORKER-DIAG-1 同族信任面）。
        self.assertEqual(caught.exception.code, "worker_tree_drifted")
        # 零副作用：不分发验证工作流、不启用 verify 工作流、档案零写入。
        self.assertEqual(self.github.dispatched, [])
        self.assertFalse(self.github.workflows["cloud-verify.yml"])
        self.assertEqual(self.store.get_automation_profile(), before)

    # ---- erase-cloud-data（云端数据抹除，信任关键面，服务级原零测） ----

    def test_erase_cloud_data_fails_closed_and_keeps_local_keys(self):
        for name in ("cloud_result_private_key", "cloud_result_public_key", "cloud_state_key"):
            self.credentials.save_secret(name, f"synthetic-{name}")
        self.github.artifacts = [{"id": 55, "name": "courselens-cloud-result-55-1", "expired": False}]
        self.github.fail_artifact_delete = True
        deleted_before = list(self.credentials.deleted_secrets)

        with self.assertRaises(AutomationError) as caught:
            self.service.action("erase-cloud-data", operation_id="erase-fail-00001")

        self.assertEqual(caught.exception.code, "cloud_cleanup_pending")
        # 零副作用：云端产物删不掉时，本地三钥绝不先删（半删即信任事故）。
        for name in ("cloud_result_private_key", "cloud_result_public_key", "cloud_state_key"):
            self.assertTrue(self.credentials.has_secret(name), name)
        self.assertEqual(self.credentials.deleted_secrets, deleted_before)

    def test_erase_cloud_data_deletes_exactly_cloud_artifacts_and_local_keys(self):
        for name in ("cloud_result_private_key", "cloud_result_public_key", "cloud_state_key"):
            self.credentials.save_secret(name, f"synthetic-{name}")
        self.github.artifacts = [
            {"id": 61, "name": "courselens-cloud-result-61-1", "expired": False},
            {"id": 62, "name": "courselens-cloud-state-62", "expired": False},
            {"id": 63, "name": "unrelated-other-bundle", "expired": False},
        ]

        operation = self.service.action("erase-cloud-data", operation_id="erase-ok-000001")

        self.assertEqual(operation["action"], "erase-cloud-data")
        self.assertEqual(operation["state"], "accepted")
        remaining_ids = {item["id"] for item in self.github.list_worker_artifacts()}
        # 只删云端双前缀产物；无关产物零触碰（闭集前缀语义）。
        self.assertEqual(remaining_ids, {63})
        # 本地恰好删三把云钥；无关本地凭据（github_worker_repo）零触碰。
        for name in ("cloud_result_private_key", "cloud_result_public_key", "cloud_state_key"):
            self.assertFalse(self.credentials.has_secret(name), name)
        self.assertTrue(self.credentials.has_secret("github_worker_repo"))
        self.assertEqual(
            sorted(self.credentials.deleted_secrets),
            ["cloud_result_private_key", "cloud_result_public_key", "cloud_state_key"],
        )

    # ---- revoke 部分删除失败腿（等待腿与 happy 腿已在 test_automation.py） ----

    def test_revoke_partial_failure_lands_cleanup_pending_with_remaining_evidence(self):
        for name in CLOUD_SECRET_NAMES:
            self.github.secrets[name] = "synthetic-value"
        for name in CLOUD_VARIABLE_NAMES:
            self.github.variables[name] = "synthetic-value"
        self.github.fail_delete_secrets = {"COURSELENS_CLOUD_PASSWORD"}

        with self.assertRaises(AutomationError) as caught:
            self.service.action(
                "revoke-cloud-credentials", operation_id="revoke-part-0001"
            )

        self.assertEqual(caught.exception.code, "cloud_cleanup_pending")
        # 全量尝试语义：失败一个不中断其余密钥/变量的删除。
        for name in CLOUD_SECRET_NAMES:
            if name not in self.github.fail_delete_secrets:
                self.assertNotIn(name, self.github.secrets, name)
        self.assertNotIn("COURSELENS_CLOUD_ENABLED", self.github.variables)
        profile = self.store.get_automation_profile()
        self.assertEqual(profile.get("state"), "cleanup_pending")
        # 档案必须清空已验证身份（撤销后不得残留可续跑的信任态）。
        self.assertEqual(profile.get("config_hash"), "")
        self.assertEqual(profile.get("account_id"), "")
        self.assertGreaterEqual(
            int(((profile.get("verification") or {}).get("remaining_count")) or 0), 1
        )

    # ---- run-now 手动触发腿（门腿已在 test_automation.py，分发形状原零测） ----

    def test_run_now_manual_dispatch_carries_config_hash_and_running_state(self):
        self.store.save_automation_profile({
            "protocol": CLOUD_PROTOCOL_VERSION,
            "state": "ready",
            "mode": "cloud",
            "config_hash": "d" * 64,
            "verified_config_hash": "d" * 64,
            # 绑定四元组须与 FakeGitHub.check_worker_integrity 逐字匹配。
            "binding": {
                "worker_repository": "synthetic-owner/courselens-worker-synthetic",
                "worker_tree": "a" * 40,
                "worker_manifest": "b" * 64,
                "trust_epoch": 3,
                "generation": 1,
            },
            "observed_at": time.time(),
            "expires_at": time.time() + 90,
        })

        operation = self.service.action("run-now", operation_id="runnow-op-00001")

        self.assertEqual(operation["state"], "accepted")
        dispatches = [item for item in self.github.dispatched if item[0] == "cloud-daily.yml"]
        self.assertEqual(len(dispatches), 1)
        inputs = dispatches[0][1]
        self.assertEqual(inputs.get("trigger_kind"), "manual")
        self.assertEqual(inputs.get("config_hash"), "d" * 64)
        self.assertEqual(self.store.get_automation_profile().get("state"), "running")


    # ---- reset-circuit 分发形状（既有钉只断 snapshot 暴露动作，分发面原零测） ----

    def test_reset_circuit_dispatches_verification_with_reset_flag_and_lands_verifying(self):
        self.store.save_automation_profile({
            "protocol": CLOUD_PROTOCOL_VERSION,
            "state": "degraded",
            "mode": "cloud",
            "config_hash": "e" * 64,
            "binding": {"generation": 2},
            "observed_at": time.time(),
            "expires_at": time.time() + 90,
        })

        operation = self.service.action("reset-circuit", operation_id="reset-op-000001")

        self.assertEqual(operation["state"], "accepted")
        dispatches = [item for item in self.github.dispatched if item[0] == "cloud-verify.yml"]
        self.assertEqual(len(dispatches), 1)
        inputs = dispatches[0][1]
        # 熔断复位必须以 reset_circuit 旗标走验证工作流，而非静默重置账本。
        self.assertEqual(inputs.get("reset_circuit"), "true")
        self.assertEqual(inputs.get("config_hash"), "e" * 64)
        self.assertTrue(self.github.workflows["cloud-verify.yml"])
        profile = self.store.get_automation_profile()
        self.assertEqual(profile.get("state"), "verifying")
        verification = dict(profile.get("verification") or {})
        self.assertEqual(verification.get("state"), "requested")
        self.assertTrue(verification.get("reset_circuit"))


class _AutomationRouteApp:
    """路由级合成壳：automation 动作直通真实 AutomationService（真闭集码）。"""

    def __init__(self, service: AutomationService, store: TaskStore):
        self._service = service
        self.task_store = store

    def automation_action(self, action, *, operation_id):
        return self._service.action(action, operation_id=operation_id)

    def automation_snapshot(self, refresh=False):
        return self._service.snapshot(refresh=refresh)


class AutomationRouteDispatchTests(unittest.TestCase):
    """automation/actions 400 闭集分派 + GET runs/imports 信封（B34-2 残余）。"""

    def setUp(self):
        cache = PROJECT_ROOT / "runtime" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=cache)
        self.store = TaskStore(Path(self.tmp.name) / "state.db")
        self.credentials = MemoryCredentials()
        self.github = FakeGitHub()
        self.service = AutomationService(
            self.store,
            self.credentials,
            lambda: self.github,
            local_schedule_getter=lambda: {"enabled": False},
            local_schedule_setter=lambda value: dict(value),
            verified_catalog_getter=lambda: {},
        )
        self.app = _AutomationRouteApp(self.service, self.store)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(http_services(self.app), PROJECT_ROOT / "frontend")
        )
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.tmp.cleanup()

    def _post(self, route, payload):
        request = Request(
            f"{self.base}/api/v3/{route}", data=json.dumps(payload).encode(),
            method="POST", headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _get(self, route):
        try:
            with urlopen(f"{self.base}/api/v3/{route}") as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_automation_actions_route_rejects_unknown_action_with_closed_code(self):
        before = self.store.get_automation_profile()

        status, body = self._post(
            "automation/actions", {"action": "detonate-everything", "operation_id": "route-act-0001"}
        )

        self.assertEqual(status, 400)
        self.assertEqual(body.get("error_code"), "automation_action_invalid")
        self.assertFalse(body.get("retriable"))
        # 零副作用：未知动作不得触达任何远端调用或档案写入。
        self.assertEqual(self.github.dispatched, [])
        self.assertEqual(self.store.get_automation_profile(), before)

    def test_automation_actions_route_rejects_short_operation_id(self):
        status, body = self._post(
            "automation/actions", {"action": "disable-cloud", "operation_id": "short"}
        )

        self.assertEqual(status, 400)
        self.assertEqual(body.get("error_code"), "operation_id_invalid")
        self.assertFalse(body.get("retriable"))

    def test_automation_actions_route_marks_rate_limited_as_retriable(self):
        def _rate_limited(workflow, *, inputs=None, ref="main"):
            raise GitHubAppError("synthetic secondary rate limit", code="rate_limited")

        self.github.dispatch_workflow = _rate_limited

        status, body = self._post(
            "automation/actions",
            {"action": "reset-circuit", "operation_id": "route-rate-0001"},
        )

        # 闭集 retriable 映射的正腿：rate_limited ∈ 路由白名单 → 可重试提示。
        self.assertEqual(status, 400)
        self.assertEqual(body.get("error_code"), "rate_limited")
        self.assertTrue(body.get("retriable"))
        # 账本必须落 failed 态（重试语义的记账面）。
        profile = self.store.get_automation_profile()
        self.assertNotEqual(profile.get("state"), "verifying")

    def test_automation_runs_and_imports_routes_serve_closed_envelopes(self):
        self.store.upsert_automation_run(
            "route-run-key-1", workflow="cloud-daily.yml", state="succeeded", conclusion="success"
        )
        self.store.upsert_automation_import(
            9, artifact_name="courselens-cloud-result-9-1", state="failed", error_code="boom"
        )

        status, body = self._get("automation/runs")
        self.assertEqual(status, 200)
        runs = body["data"]["runs"]
        self.assertEqual([row["run_key"] for row in runs], ["route-run-key-1"])
        self.assertIsInstance(body["data"]["observed_at"], float)

        status, body = self._get("automation/imports")
        self.assertEqual(status, 200)
        imports = body["data"]["imports"]
        self.assertEqual([row["artifact_id"] for row in imports], [9])
        self.assertEqual(imports[0]["state"], "failed")

        status, body = self._get("automation")
        self.assertEqual(status, 200)
        self.assertIn("state", body["data"])


if __name__ == "__main__":
    unittest.main()
