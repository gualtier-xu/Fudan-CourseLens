from __future__ import annotations

"""SHELL-SEED-1（N9-F）合成壳种子接口行为钉。

钉两层形状：
1. 种子函数层：preset 落库后的 repository / 快照形状（全部经产品写路径）。
2. HTTP 层：http_services + make_handler 装配后 /api/v3/* 的返回形状——
   与前端消费点（tasks-drawer / settings remoteRotateAction / accounts 卡）
   的门槛字段一一对应。
墓碑语义钉：播种 → 删除 → 重播种 = 干净抑制（P57 唯一入口）；
持久根钉：跨实例存续（重启壳不复现的物理前提）。
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.runtime.http_api import make_handler, public_tasks
from tests.http_services import http_services
from tests.synthetic_shell_server import (
    FROZEN_SEED_EPOCH,
    SEED_PRESETS,
    apply_synthetic_seeds,
    build_service,
    seed_task_rows,
)


def _http_get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _service_scratch() -> tempfile.TemporaryDirectory:
    """服务根临时目录：凭据库门只收项目内路径（path_utils.ensure_inside_project），
    与既有服务级测试同源（runtime/cache 下的包内临时目录，用后即清）。"""
    scratch = ROOT / "runtime" / "cache"
    scratch.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=scratch)


class LiveGrantStubContractTests(unittest.TestCase):
    """C2-2 防回退钉（N15-W1 收梯）：合成壳 consume stub 必须收产品端的
    view 形参（直播二期乙1 契约 consume_grant(grant, view=)）——缺形参时
    合成壳直播间 500，浏览器走查车道全阻断。"""

    def test_consume_stub_accepts_the_view_kwarg(self):
        scratch = _service_scratch()
        try:
            service = build_service(Path(scratch.name))
            try:
                receipt = service.consume_live_room_grant(
                    service.issue_live_room_grant("900001")["grant"], view="student"
                )
                self.assertEqual(receipt["session_id"], "s" * 32)
                self.assertEqual(receipt["manifest_id"], "m" * 24)
            finally:
                service.close()
        finally:
            scratch.cleanup()


class SeedTaskRowsTests(unittest.TestCase):
    """种子行落库形状：queued/running/终态 + 消耗绝对值 + 闭集字段表。"""

    def setUp(self):
        self._temporary = _service_scratch()
        self.service = build_service(Path(self._temporary.name))

    def tearDown(self):
        self.service.close()
        self._temporary.cleanup()

    def test_running_seed_sets_state_started_at_and_progress(self):
        seeded = seed_task_rows(self.service, [{
            "kind": "subtitle", "course_id": "9000", "sub_id": "90001",
            "state": "running",
            "progress": {"label": "正在转写", "completed": 96, "total": 600},
        }])
        self.assertEqual(len(seeded), 1)
        task = self.service.task_store.get_task(seeded[0])
        self.assertEqual(task["state"], "running")
        self.assertIsNotNone(task["started_at"])
        self.assertEqual(task["progress"]["label"], "正在转写")

    def test_terminal_seed_records_error_and_usage_as_absolute_values(self):
        seeded = seed_task_rows(self.service, [{
            "kind": "subtitle", "course_id": "9000", "sub_id": "90002",
            "state": "failed", "error": "GitHubAppError: worker_tree_drifted",
            "deepseek_tokens": 120000, "runner_seconds": 754.0,
        }])
        task = self.service.task_store.get_task(seeded[0])
        self.assertEqual(task["state"], "failed")
        self.assertIn("worker_tree_drifted", task["error"])
        self.assertEqual(task["deepseek_tokens"], 120000)
        self.assertAlmostEqual(task["runner_seconds"], 754.0)

    def test_usage_fields_default_to_null_not_zero(self):
        seeded = seed_task_rows(self.service, [{
            "kind": "subtitle", "course_id": "9000", "sub_id": "90003",
            "state": "completed",
        }])
        task = self.service.task_store.get_task(seeded[0])
        self.assertIsNone(task["deepseek_tokens"])
        self.assertIsNone(task["runner_seconds"])

    def test_unknown_field_and_state_are_closed_set_rejected(self):
        with self.assertRaises(ValueError):
            seed_task_rows(self.service, [{
                "kind": "subtitle", "course_id": "9000", "sub_id": "90001",
                "state": "queued", "totally_unknown": 1,
            }])
        with self.assertRaises(ValueError):
            seed_task_rows(self.service, [{
                "kind": "subtitle", "course_id": "9000", "sub_id": "90001",
                "state": "flying",
            }])


class FrozenSeedClockTests(unittest.TestCase):
    """HARNESS-FIX-1（VISBASE-2-R2 移交配方）：--seed-clock frozen 冻结钟锚定。

    视觉 harness 的浏览器时钟冻结在 2026-10-20 10:08，而 public_task 的
    stale 判定用真实钟（running TTL=20s）——真实钟种子让「播种→采集」墙钟
    间隔跨 20s 即 fresh/stale 混切（同一次运行内两态共存），且 running 卡
    elapsed=real_now-started_at 随间隔漂移。frozen 档把全部种子时间戳锚为
    相对 FROZEN_SEED_EPOCH 的定值：渲染形状与墙钟间隔彻底解耦。"""

    def setUp(self):
        self._temporary = _service_scratch()
        self.service = build_service(Path(self._temporary.name))

    def tearDown(self):
        self.service.close()
        self._temporary.cleanup()

    def test_frozen_running_seed_is_fresh_with_fixed_elapsed(self):
        seeded = seed_task_rows(self.service, [{
            "kind": "subtitle", "course_id": "9000", "sub_id": "90001",
            "state": "running",
            "progress": {
                "label": "正在转写音频",
                "media_duration_seconds": 600,
                "processed_media_seconds": 96,
            },
        }], clock="frozen")
        task = self.service.task_store.get_task(seeded[0])
        self.assertEqual(task["state"], "running")
        self.assertAlmostEqual(task["created_at"], FROZEN_SEED_EPOCH - 3600.0)
        self.assertAlmostEqual(task["started_at"], FROZEN_SEED_EPOCH - 60.0)
        self.assertAlmostEqual(task["updated_at"], FROZEN_SEED_EPOCH)
        self.assertIsNone(task["finished_at"])
        self.assertAlmostEqual(task["progress"]["elapsed_active_seconds"], 60.0)
        public = public_tasks(self.service.task_store, [task])[0]
        # 真实钟（远早于冻结时刻）判 fresh；elapsed 走 progress 定值不再随墙钟走。
        self.assertFalse(public["stale"])
        self.assertAlmostEqual(public["elapsed_seconds"], 60.0)
        self.assertAlmostEqual(public["expires_at"], FROZEN_SEED_EPOCH + 20.0)

    def test_frozen_terminal_seeds_are_deterministic(self):
        seeded_failed = seed_task_rows(self.service, [{
            "kind": "subtitle", "course_id": "9000", "sub_id": "90002",
            "state": "failed", "error": "GitHubAppError: worker_tree_drifted",
        }], clock="frozen")
        failed = self.service.task_store.get_task(seeded_failed[0])
        # created=updated=finished 同值、started NULL：前端 runtime 行确定性缺席
        # （U1「绝不渲染 0 秒」意图态），杜绝时钟刻度相等性竞速。
        self.assertAlmostEqual(failed["created_at"], FROZEN_SEED_EPOCH - 660.0)
        self.assertAlmostEqual(failed["updated_at"], FROZEN_SEED_EPOCH - 660.0)
        self.assertAlmostEqual(failed["finished_at"], FROZEN_SEED_EPOCH - 660.0)
        self.assertIsNone(failed["started_at"])
        seeded_completed = seed_task_rows(self.service, [{
            "kind": "subtitle", "course_id": "9000", "sub_id": "90003",
            "state": "completed", "runner_seconds": 754.0,
        }], clock="frozen")
        completed = self.service.task_store.get_task(seeded_completed[0])
        # started NULL → elapsed 链 None → 前端 runtime 行走 runner 分支（像素保持）。
        self.assertAlmostEqual(completed["created_at"], FROZEN_SEED_EPOCH - 720.0)
        self.assertAlmostEqual(completed["finished_at"], FROZEN_SEED_EPOCH - 600.0)
        self.assertIsNone(completed["started_at"])
        publics = public_tasks(
            self.service.task_store,
            [failed, completed],
            now=FROZEN_SEED_EPOCH + 3600.0,
        )
        self.assertFalse(publics[0]["stale"])
        self.assertFalse(publics[1]["stale"])

    def test_seed_clock_is_closed_set(self):
        with self.assertRaises(ValueError):
            seed_task_rows(self.service, [{
                "kind": "subtitle", "course_id": "9000", "sub_id": "90001",
                "state": "queued",
            }], clock="bogus")


class SeedPresetTests(unittest.TestCase):
    """preset 摘要与前端门槛字段的落库形状。"""

    def setUp(self):
        self._temporary = _service_scratch()
        self.service = build_service(Path(self._temporary.name))

    def tearDown(self):
        self.service.close()
        self._temporary.cleanup()

    def test_preset_names_are_closed_set(self):
        self.assertEqual(
            set(SEED_PRESETS),
            {"tasks-active", "tasks-failed", "tasks-completed",
             "github-missing-secret", "deepseek-saved"},
        )

    def test_tasks_failed_maps_error_text_to_closed_code(self):
        summary = apply_synthetic_seeds(self.service, presets=["tasks-failed"])
        self.assertEqual(summary["automation_run_keys"], ["synthetic-automation-run-0001"])
        run = self.service.task_store.get_automation_run("synthetic-automation-run-0001")
        self.assertIsNotNone(run)
        self.assertEqual(run["conclusion"], "failure")
        tasks = self.service.task_store.list_tasks(states=("failed",))
        self.assertEqual(len(tasks), 1)
        public = public_tasks(self.service.task_store, tasks)
        self.assertEqual(public[0]["error_code"], "worker_tree_drifted")

    def test_tasks_completed_exposes_absolute_token_usage(self):
        apply_synthetic_seeds(self.service, presets=["tasks-completed"])
        tasks = self.service.task_store.list_tasks(states=("completed",))
        self.assertEqual(len(tasks), 1)
        public = public_tasks(self.service.task_store, tasks)
        # 前端 taskTokenUsageText：<1万 原值；≥1万 ≈X 万 tokens（120000 → ≈12 万）。
        self.assertEqual(public[0]["deepseek_tokens"], 120000)

    def test_github_missing_secret_wrapper_patches_only_environment_gate(self):
        apply_synthetic_seeds(self.service, presets=["github-missing-secret"])
        snapshot = self.remote_snapshot()
        components = {item["component"]: item for item in snapshot["components"]}
        environment = components["environment"]
        self.assertEqual(environment["state"], "action_required")
        self.assertEqual(environment["code"], "environment_incomplete")
        self.assertIn("rotate-worker-keys", environment["actions"])
        self.assertEqual(
            environment["evidence"]["missing_secrets"],
            ["WORKER_INPUT_PRIVATE_KEY", "WORKER_SIGNING_PRIVATE_KEY"],
        )
        self.assertFalse(environment.get("stale"))
        self.assertGreater(environment["expires_at"], environment["observed_at"])
        # 其余组件零触碰：authorization 组件仍在且未被改写成缺钥形状。
        authorization = components["authorization"]
        self.assertNotEqual(authorization.get("code"), "environment_incomplete")

    def test_github_missing_secret_keeps_fresh_query_semantics(self):
        apply_synthetic_seeds(self.service, presets=["github-missing-secret"])
        # fresh=True 透传真快照路径（路由有参读会带 fresh），包装不得吞参。
        snapshot = self.remote_snapshot(fresh=True)
        self.assertIn("components", snapshot)

    def remote_snapshot(self, **kwargs):
        # http_services 绑定时机与 main() 相同：先种子后装配。
        services = http_services(self.service)
        return services.remote_compute.connection_snapshot(**kwargs)

    def test_deepseek_saved_preset_persists_via_product_flow(self):
        apply_synthetic_seeds(self.service, presets=["deepseek-saved"])
        self.assertTrue(self.service.has_deepseek_key())
        self.assertTrue(self.service.credentials.has_deepseek_key())
        self.assertFalse(self.service.credentials.deepseek_key_requires_rotation())

    def test_unknown_preset_rejected(self):
        with self.assertRaises(ValueError):
            apply_synthetic_seeds(self.service, presets=["tasks-flying"])


class TombstoneRestartSemanticsTests(unittest.TestCase):
    """P57 墓碑钉：播种 → 删除 → 重播种 = 抑制；删除仅限终态。"""

    def setUp(self):
        self._temporary = _service_scratch()
        self.service = build_service(Path(self._temporary.name))

    def tearDown(self):
        self.service.close()
        self._temporary.cleanup()

    def test_reseed_after_delete_is_suppressed_by_tombstone(self):
        apply_synthetic_seeds(self.service, presets=["tasks-failed"])
        self.assertEqual(len(self.service.task_store.list_automation_runs()), 1)
        self.service.task_store.delete_automation_run("synthetic-automation-run-0001")
        self.assertEqual(self.service.task_store.list_automation_runs(), [])
        summary = apply_synthetic_seeds(self.service, presets=["tasks-failed"])
        # 墓碑命中：upsert 唯一入口干净 no-op，摘要不再登记该 key。
        self.assertEqual(summary["automation_run_keys"], [])
        self.assertEqual(self.service.task_store.list_automation_runs(), [])

    def test_active_run_delete_is_rejected(self):
        apply_synthetic_seeds(self.service, presets=["tasks-failed"])
        run = self.service.task_store.get_automation_run("synthetic-automation-run-0001")
        self.service.task_store.upsert_automation_run(
            "synthetic-automation-run-active", workflow="cloud-daily.yml",
            trigger_kind="manual", state="running", conclusion="",
        )
        with self.assertRaises(ValueError):
            self.service.task_store.delete_automation_run("synthetic-automation-run-active")
        self.assertIsNotNone(run)


class TaskDeleteRestartSemanticsTests(unittest.TestCase):
    """SWEEPFIX-1 T3（化身走查 SWEEP1-T3）：普通任务记录的删除权威性。

    复现（修前红）：同根重建服务（=同 --seed 重启壳）重播种时，被学生删除
    的 failed/completed 种子记录以新 task_id 复活（P57 墓碑只盖自动化运行
    记录，普通任务走硬删无删账）。修后绿：删除事实入持久删账，重播种干净
    抑制，删除权威性语义与 P57 对齐。
    """

    def test_reseed_after_task_delete_does_not_resurrect(self):
        with _service_scratch() as temporary:
            root = Path(temporary)
            first = build_service(root)
            try:
                apply_synthetic_seeds(first, presets=["tasks-failed", "tasks-completed"])
                self.assertEqual(len(first.task_store.list_tasks(states=("failed",))), 1)
                self.assertEqual(len(first.task_store.list_tasks(states=("completed",))), 1)
                for state in ("failed", "completed"):
                    task = first.task_store.list_tasks(states=(state,))[0]
                    first.task_store.delete_task(task["task_id"])
                self.assertEqual(first.task_store.list_tasks(states=("failed",)), [])
                self.assertEqual(first.task_store.list_tasks(states=("completed",)), [])
            finally:
                first.close()
            second = build_service(root)  # 同根重建 = 同种子重启壳
            try:
                summary = apply_synthetic_seeds(second, presets=["tasks-failed", "tasks-completed"])
                # 删除权威性：被删除的种子身份不以新 task_id 复活。
                self.assertEqual(summary["seeded_task_ids"], [])
                self.assertEqual(second.task_store.list_tasks(states=("failed",)), [])
                self.assertEqual(second.task_store.list_tasks(states=("completed",)), [])
            finally:
                second.close()

    def test_ledger_suppresses_only_deleted_identities(self):
        """对照组：删账只抑制被删除过的身份。删 failed 留 completed，重播种
        = completed 照常重播、failed 干净抑制。（未删身份重播时既有累积语义
        ——add_task 终态不去重——属壳层既有行为，非本包改动面。）"""
        with _service_scratch() as temporary:
            root = Path(temporary)
            first = build_service(root)
            try:
                apply_synthetic_seeds(first, presets=["tasks-failed", "tasks-completed"])
                failed = first.task_store.list_tasks(states=("failed",))[0]
                first.task_store.delete_task(failed["task_id"])
            finally:
                first.close()
            second = build_service(root)
            try:
                summary = apply_synthetic_seeds(second, presets=["tasks-failed", "tasks-completed"])
                seeded_states = {
                    str(second.task_store.get_task(task_id)["state"])
                    for task_id in summary["seeded_task_ids"]
                }
                self.assertEqual(seeded_states, {"completed"}, "删账只抑制被删身份（failed 抑制，completed 照常重播）")
                self.assertEqual(second.task_store.list_tasks(states=("failed",)), [])
            finally:
                second.close()


class StateDirPersistenceTests(unittest.TestCase):
    """--state-dir 语义：数据跨实例存续（重启壳不复现的物理前提）。"""

    def test_tombstone_and_seed_survive_instance_rebuild(self):
        with _service_scratch() as temporary:
            root = Path(temporary)
            first = build_service(root)
            try:
                apply_synthetic_seeds(first, presets=["tasks-failed"])
                first.task_store.delete_automation_run("synthetic-automation-run-0001")
                first.task_store.set_app_state("seed-pin-marker", {"v": 1})
            finally:
                first.close()
            second = build_service(root)
            try:
                self.assertEqual(
                    second.task_store.get_app_state("seed-pin-marker"), {"v": 1}
                )
                summary = apply_synthetic_seeds(second, presets=["tasks-failed"])
                self.assertEqual(summary["automation_run_keys"], [])
                self.assertEqual(second.task_store.list_automation_runs(), [])
            finally:
                second.close()


class SeedHttpShapeTests(unittest.TestCase):
    """装配后 HTTP 形状钉：前端消费的门槛字段在 /api/v3/* 上成立。"""

    def test_seeded_surfaces_expose_frontend_gate_fields(self):
        with _service_scratch() as temporary:
            service = build_service(Path(temporary))
            try:
                apply_synthetic_seeds(service, presets=[
                    "tasks-active", "tasks-failed", "tasks-completed",
                    "github-missing-secret", "deepseek-saved",
                ])
                services = http_services(service)
                server = ThreadingHTTPServer(
                    ("127.0.0.1", 0), make_handler(services, ROOT / "frontend"),
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = f"http://127.0.0.1:{server.server_port}"
                try:
                    tasks_payload = _http_get_json(f"{base}/api/v3/tasks")
                    self.assertEqual(tasks_payload["schema"], "courselens.api.v3")
                    tasks = tasks_payload["data"]["tasks"]
                    states = sorted(task["state"] for task in tasks)
                    self.assertEqual(states, ["completed", "failed", "running"])
                    by_state = {task["state"]: task for task in tasks}
                    self.assertEqual(by_state["completed"]["deepseek_tokens"], 120000)
                    self.assertEqual(by_state["failed"]["error_code"], "worker_tree_drifted")
                    # SWEEPFIX-3 T6（化身走查 SWEEP1-T6）：播种 running 字幕任务经
                    # 真实 public_task 序列化后，单位必须走 seconds 派生（合同键
                    # media_duration_seconds/processed_media_seconds），阶段人话
                    # label 在位——修前红=种子用非合同键 progress_unit，派生落
                    # items，「转写秒数」被前端说成「96 / 600 项」。
                    self.assertEqual(by_state["running"]["progress_unit"], "seconds")
                    self.assertEqual(by_state["running"]["label"], "正在转写音频")
                    self.assertEqual(by_state["running"]["completed"], 96.0)
                    self.assertEqual(by_state["running"]["total"], 600.0)
                    self.assertIn("counts", tasks_payload["data"])

                    automation = _http_get_json(f"{base}/api/v3/automation")["data"]
                    run_keys = [run["run_key"] for run in automation["runs"]]
                    self.assertIn("synthetic-automation-run-0001", run_keys)

                    remote = _http_get_json(f"{base}/api/v3/remote-connection")["data"]
                    environment = next(
                        item for item in remote["components"]
                        if item["component"] == "environment"
                    )
                    self.assertEqual(environment["state"], "action_required")
                    self.assertIn("rotate-worker-keys", environment["actions"])
                    self.assertTrue(environment["evidence"]["missing_secrets"])

                    accounts = _http_get_json(f"{base}/api/v3/accounts")["data"]
                    self.assertTrue(accounts["deepseek"]["saved"])
                    self.assertTrue(accounts["deepseek"]["configured"])
                finally:
                    server.shutdown()
                    server.server_close()
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
