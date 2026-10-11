from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from credentials import CredentialStore
from path_utils import PROJECT_ROOT
from src.remote.connection import RemoteConnectionSupervisor, verification_fingerprint
from src.remote.github_app import GitHubAppError
from src.runtime.task_store import TaskStore

TEST_CACHE = PROJECT_ROOT / "runtime" / "cache"
TEST_CACHE.mkdir(parents=True, exist_ok=True)

class FakeGitHubApp:
    def __init__(self):
        self.values = {
            "app_configured": True,
            "authorized": True,
            "installation_url": "https://github.com/apps/fudan-courselens/installations/new",
            "worker_repo": "student/Fudan-CourseLens-Worker",
            "mailbox_repo": "student/Fudan-CourseLens-Mailbox",
            "bootstrapped": True,
            "job_token_cleanup_pending": False,
        }
        self.installation = {
            "installed": True, "installation_id": 1,
            "repository_selection_exact": True,
            "missing_installation_repositories": [],
            "unexpected_installation_repositories": [],
        }
        # GITHUB-BIND-P0-1：发布环签名链验证面。默认健康（树信任即全链可信）；
        # 测试可注入 GitHubAppError 或 trusted=False 模拟确定性破损。
        self.check_worker_integrity_calls = 0
        self.release_verification: dict[str, bool] | Exception = {"trusted": True}

    def check_worker_integrity(self, *, read_only: bool = False) -> dict:
        self.check_worker_integrity_calls += 1
        if isinstance(self.release_verification, Exception):
            raise self.release_verification
        return dict(self.release_verification)

    def snapshot(self):
        return dict(self.values)

    def inspect_managed_resources(self):
        return {
            "identity": {"login": "student", "account_id": 123},
            "installation": dict(self.installation),
            "worker": {
                "exists": True, "private": False, "archived": False,
                "disabled": False, "managed": True, "owner": "student",
                "default_branch": "main",
            },
            "mailbox": {
                "exists": True, "private": True, "archived": False,
                "disabled": False, "managed": True, "owner": "student",
                "has_issues": True,
            },
            "worker_commit": "a" * 40,
            "worker_tree": "b" * 40,
            "expected_commit": "a" * 40,
            "expected_tree": "b" * 40,
            "workflows": {
                "process.yml": {"exists": True, "state": "active"},
                "echo.yml": {"exists": True, "state": "active"},
            },
            "actions_enabled": True,
            "environment_exists": True,
            "secret_names": ["WORKER_INPUT_PRIVATE_KEY", "WORKER_SIGNING_PRIVATE_KEY"],
            "variables": {"COURSELENS_MAILBOX_REPO": "student/Fudan-CourseLens-Mailbox"},
            "rate_limit": {"remaining": 5000, "reset_at": 0},
        }


class ReconcileAttemptKeyingTests(unittest.TestCase):
    """REALTEST-H1/H2 同根：reconcile 的 attempt 行键必须是任务级派发 attempt
    （run 行 attempt 列），而非 GitHub 单 run 的 run_attempt（首跑恒为 1）。
    此前每次轮询都会为第 3 次派发写出一行幻影 attempt=1 记录，其 import_state
    拷贝 remote_state，行转终态被循环跳过后冻结在 running。"""

    def test_reconcile_never_creates_phantom_github_run_attempt_rows(self):
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as tmp:
            task_store = TaskStore(Path(tmp) / "state.db")
            try:
                service = RemoteConnectionSupervisor.__new__(RemoteConnectionSupervisor)
                service.task_store = task_store
                service._record = lambda *args, **kwargs: None
                task_store.upsert_remote_run(
                    "taskr", repository="owner/repo", workflow="process.yml",
                    run_id=42, attempt=3, remote_state="running",
                )
                client = type("Client", (), {})()

                class _Response:
                    def __init__(self, payload):
                        self._payload = payload

                    def json(self):
                        return dict(self._payload)

                class _Payload(dict):
                    pass

                def api(method, path, **kwargs):
                    return _Response(_Payload({
                        "status": "in_progress", "conclusion": None, "run_attempt": 1,
                    }))

                client._api = api
                service._reconcile_runs(client)

                self.assertIsNone(
                    task_store.get_remote_attempt("taskr", 1),
                    "不得按 GitHub run_attempt 创建幻影 attempt=1 行",
                )
                row = task_store.get_remote_attempt("taskr", 3)
                self.assertIsNotNone(row, "任务派发 attempt 行应被更新")
                self.assertEqual(int(row["run_id"]), 42)
                self.assertEqual(row["github_status"], "in_progress")
            finally:
                task_store.close()


class RemoteConnectionTruthTests(unittest.TestCase):
    def test_configuration_presence_does_not_mean_ready(self):
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            app = FakeGitHubApp()
            supervisor = RemoteConnectionSupervisor(store, credentials, lambda: app)
            snapshot = supervisor.snapshot()
            self.assertNotEqual(snapshot["overall"]["state"], "ready")
            self.assertFalse(snapshot["overall"]["ready_for_dispatch"])
            store.close()

    def test_snapshot_exposes_trusted_provider_installation_url(self):
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            app = FakeGitHubApp()
            supervisor = RemoteConnectionSupervisor(store, credentials, lambda: app)
            snapshot = supervisor.snapshot()
            self.assertEqual(
                snapshot["installation_url"],
                "https://github.com/apps/fudan-courselens/installations/new",
            )
            # 缺失时必须为空串（闭集形态），前端只渲染后端提供的受信 URL。
            app.values.pop("installation_url")
            self.assertEqual(supervisor.snapshot()["installation_url"], "")
            store.close()

    def test_live_probe_requires_current_channel_proof(self):
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            for name, value in {
                "worker_box_public_key": "box",
                "worker_signing_public_key": "signing",
            }.items():
                credentials.save_secret(name, value)
            app = FakeGitHubApp()
            supervisor = RemoteConnectionSupervisor(store, credentials, lambda: app)
            first = supervisor.probe()
            self.assertFalse(first["overall"]["ready_for_dispatch"])
            store.set_app_state("remote_channel_verification", {
                "verified_at": time.time(),
                "fingerprint": verification_fingerprint(
                    tree="b" * 40, box_public_key="box", signing_public_key="signing"
                ),
            })
            second = supervisor.probe()
            self.assertTrue(second["overall"]["ready_for_dispatch"])
            self.assertEqual(second["overall"]["state"], "ready")
            store.close()

    def test_worker_integrity_surfaces_release_verification_break(self):
        """GITHUB-BIND-P0-1（2026-10-11 实测定谳）：树相等但发布环签名链确定
        性破损（pin 树丢元数据）时，worker_integrity 必须落精准闭集码并转
        action_required——不得照报 trusted 与 bootstrap 必败自相矛盾（该矛盾
        曾把 0.1.0 用户推进 installation 范围的错向排查）；负缓存保证同一
        (pin 树, 实例树) 只探一次，防 404 探测风暴。"""
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            app = FakeGitHubApp()
            app.release_verification = GitHubAppError(
                "公共 Worker 发布环缺少签名元数据文件，无法完成 Worker 完整性校验",
                code="worker_mirror_metadata_missing",
            )
            supervisor = RemoteConnectionSupervisor(store, credentials, lambda: app)
            supervisor.probe()
            observation = store.get_remote_observation("worker_integrity")
            self.assertIsNotNone(observation)
            self.assertEqual(str(observation["state"]), "action_required")
            self.assertEqual(str(observation["code"]), "worker_mirror_metadata_missing")
            supervisor.probe()
            self.assertEqual(
                app.check_worker_integrity_calls, 1,
                "确定性破损按 (pin, 树) 负缓存，第二次 probe 不得重探",
            )
            store.close()

    def test_worker_integrity_keeps_trusted_verdict_on_healthy_release_chain(self):
        """GITHUB-BIND-P0-1 回归钉：签名链健康时树信任判定原样保持
        ready/worker_tree_trusted，验证步不引入回归性 action_required。"""
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            app = FakeGitHubApp()
            supervisor = RemoteConnectionSupervisor(store, credentials, lambda: app)
            supervisor.probe()
            observation = store.get_remote_observation("worker_integrity")
            self.assertIsNotNone(observation)
            self.assertEqual(str(observation["state"]), "ready")
            self.assertEqual(str(observation["code"]), "worker_tree_trusted")
            store.close()

    def test_expired_evidence_never_reports_ready(self):
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            supervisor = RemoteConnectionSupervisor(store, credentials, FakeGitHubApp)
            for name in (
                "local_backend", "app_configuration", "github_api", "authorization",
                "installation", "worker_repository", "mailbox_repository", "worker_integrity",
                "workflow", "actions", "environment", "channel_test",
            ):
                store.upsert_remote_observation(
                    name, state="ready", source="github_api", code="ok",
                    observed_at=time.time() - 100, expires_at=time.time() - 1,
                )
            snapshot = supervisor.snapshot()
            self.assertFalse(snapshot["overall"]["ready_for_dispatch"])
            self.assertEqual(snapshot["overall"]["state"], "unknown")
            store.close()

    def test_action_required_beats_stale_evidence_in_overall(self):
        """REALRUN-1 N1/N2（2026-10-08 真测钉死）：首跑未授权时 authorization 在
        快照期被推导为 action_required（新鲜），而 local_backend 探针记录 TTL 仅
        20s——空闲节拍内证据过期（unknown/stale_evidence）。blocking 若按组件
        顺序选取就会被 local_backend 压住：overall 恒 unknown/stale_evidence，
        首屏连接点与引导行永不收敛（12/12 采样 40 分钟实测）。新契约=状态优先
        级消费：可行动结论胜出，overall=action_required/authorization_missing。"""
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            app = FakeGitHubApp()
            app.values["authorized"] = False
            supervisor = RemoteConnectionSupervisor(store, credentials, lambda: app)
            store.upsert_remote_observation(
                "local_backend", state="ready", source="local", code="local_backend_ready",
                observed_at=time.time() - 100, expires_at=time.time() - 1,
            )
            snapshot = supervisor.snapshot()
            authorization = next(
                item for item in snapshot["components"]
                if item["component"] == "authorization"
            )
            self.assertEqual(authorization["state"], "action_required")
            local_backend = next(
                item for item in snapshot["components"]
                if item["component"] == "local_backend"
            )
            self.assertEqual(local_backend["state"], "unknown")
            self.assertEqual(local_backend["code"], "stale_evidence")
            self.assertEqual(snapshot["overall"]["state"], "action_required")
            self.assertEqual(snapshot["overall"]["code"], "authorization_missing")
            self.assertFalse(snapshot["overall"]["ready_for_dispatch"])
            store.close()

    def test_probe_demands_exact_installation_repository_selection(self):
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            for name, value in {
                "worker_box_public_key": "box",
                "worker_signing_public_key": "signing",
            }.items():
                credentials.save_secret(name, value)
            app = FakeGitHubApp()
            app.installation = {
                "installed": True, "installation_id": 42,
                "repository_selection_exact": False,
                "missing_installation_repositories": ["fudan-courselens-mailbox"],
                "unexpected_installation_repositories": ["student/unrelated-repository"],
            }
            supervisor = RemoteConnectionSupervisor(store, credentials, lambda: app)
            snapshot = supervisor.probe()
            installation = next(
                item for item in snapshot["components"]
                if item["component"] == "installation"
            )
            self.assertEqual(installation["state"], "action_required")
            self.assertEqual(installation["code"], "installation_scope_not_exact")
            self.assertEqual(installation["actions"], ["restrict-github-app-installation"])
            self.assertEqual(installation["evidence"]["missing"], ["fudan-courselens-mailbox"])
            self.assertEqual(
                installation["evidence"]["unexpected"], ["student/unrelated-repository"]
            )
            self.assertEqual(
                installation["evidence"]["settings_url"],
                "https://github.com/settings/installations/42",
            )
            self.assertNotEqual(snapshot["overall"]["state"], "ready")
            store.close()

    def test_probe_records_unknown_state_when_selection_inventory_fails(self):
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            app = FakeGitHubApp()
            app.installation = {
                "installed": True, "installation_id": 42,
                "repository_selection_exact": False,
                "missing_installation_repositories": [],
                "unexpected_installation_repositories": [],
                "repository_selection_error": (
                    "installation_repository_inventory_unavailable"
                ),
            }
            supervisor = RemoteConnectionSupervisor(store, credentials, lambda: app)
            snapshot = supervisor.probe()
            installation = next(
                item for item in snapshot["components"]
                if item["component"] == "installation"
            )
            self.assertEqual(installation["state"], "unknown")
            self.assertEqual(installation["code"], "installation_selection_unknown")
            self.assertEqual(installation["actions"], ["diagnose"])
            self.assertEqual(
                installation["evidence"],
                {"reason": "installation_repository_inventory_unavailable"},
            )
            self.assertNotIn("missing", installation["evidence"])
            store.close()

    def test_probe_reports_bootstrap_for_template_worker_binding(self):
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            app = FakeGitHubApp()
            app.values["worker_repo"] = "gualtier-xu-co/Fudan-CourseLens-Worker"

            def inspect():
                resources = FakeGitHubApp.inspect_managed_resources(app)
                resources["worker"] = {
                    "exists": True, "private": False, "archived": False,
                    "disabled": False, "managed": False, "owner": "gualtier-xu",
                    "default_branch": "main",
                    "full_name": "gualtier-xu-co/Fudan-CourseLens-Worker",
                }
                return resources

            app.inspect_managed_resources = inspect
            supervisor = RemoteConnectionSupervisor(store, credentials, lambda: app)
            snapshot = supervisor.probe()
            worker = next(
                item for item in snapshot["components"]
                if item["component"] == "worker_repository"
            )
            self.assertEqual(worker["state"], "action_required")
            self.assertEqual(worker["actions"], ["bootstrap"])
            store.close()

    def test_probe_repos_denial_before_installation_attaches_to_installation_evidence(self):
        """T6 深分类收尾：未安装 + 仓库级 403 → 闭集端点类挂进 installation
        证据（前端安装指引据此解释「安装前读不了仓库属正常」）；五个下游组件
        不再以 repos_access_denied 独立故障行出现。"""
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            for name, value in {
                "worker_box_public_key": "box",
                "worker_signing_public_key": "signing",
            }.items():
                credentials.save_secret(name, value)
            app = FakeGitHubApp()
            app.installation = {
                "installed": False, "installation_id": 0,
                "repository_selection_exact": False,
                "missing_installation_repositories": [
                    "fudan-courselens-worker", "fudan-courselens-mailbox",
                ],
                "unexpected_installation_repositories": [],
            }

            def inspect():
                resources = FakeGitHubApp.inspect_managed_resources(app)
                resources["repos_access_denied"] = "repos_detail"
                return resources

            app.inspect_managed_resources = inspect
            supervisor = RemoteConnectionSupervisor(store, credentials, lambda: app)
            snapshot = supervisor.probe()
            installation = next(
                item for item in snapshot["components"]
                if item["component"] == "installation"
            )
            self.assertEqual(installation["code"], "installation_missing")
            self.assertEqual(installation["evidence"]["repos_denied"], "repos_detail")
            for component in ("worker_integrity", "workflow", "actions", "environment", "job_token"):
                row = next(
                    item for item in snapshot["components"]
                    if item["component"] == component
                )
                self.assertNotEqual(
                    row["code"], "repos_access_denied",
                    "未安装期间的仓库 403 不得渲染为独立范围/权限故障",
                )
            store.close()

    def test_probe_reports_preselected_install_url_when_app_is_not_installed(self):
        with tempfile.TemporaryDirectory(dir=TEST_CACHE) as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            credentials = CredentialStore(Path(tmp) / "credentials.json")
            for name, value in {
                "worker_box_public_key": "box",
                "worker_signing_public_key": "signing",
            }.items():
                credentials.save_secret(name, value)
            app = FakeGitHubApp()
            app.installation = {
                "installed": False, "installation_id": 0,
                "repository_selection_exact": False,
                "missing_installation_repositories": [
                    "fudan-courselens-worker", "fudan-courselens-mailbox",
                ],
                "unexpected_installation_repositories": [],
            }
            setup_url = (
                "https://github.com/apps/fudan-courselens/installations/new/permissions"
                "?suggested_target_id=123&repository_ids[]=5&repository_ids[]=6"
            )

            def inspect():
                resources = FakeGitHubApp.inspect_managed_resources(app)
                resources["installation_setup_url"] = setup_url
                return resources

            app.inspect_managed_resources = inspect
            supervisor = RemoteConnectionSupervisor(store, credentials, lambda: app)
            snapshot = supervisor.probe()
            installation = next(
                item for item in snapshot["components"]
                if item["component"] == "installation"
            )
            self.assertEqual(installation["state"], "action_required")
            self.assertEqual(installation["code"], "installation_missing")
            self.assertEqual(installation["actions"], ["bootstrap"],
                             "未安装时的恢复动作是继续初始化，绝不是强制重新授权")
            self.assertEqual(installation["evidence"]["installation_setup_url"], setup_url)
            self.assertIn("bootstrap", snapshot["allowed_actions"])
            self.assertNotEqual(snapshot["overall"]["state"], "ready")
            store.close()


if __name__ == "__main__":
    unittest.main()
