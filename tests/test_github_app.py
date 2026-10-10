from __future__ import annotations

import time
import tempfile
import unittest
import json
import base64
import threading
import requests
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from nacl.signing import SigningKey

from src.application import CourseLensApplication
from scripts import register_github_app
from shared.protocol.mirror import MANIFEST_SCHEMA, TRUST_SCHEMA, document_sha256, sign_document
from src.remote.connection import RemoteConnectionSupervisor
from src.remote.github_app import (
    DeviceAuthorization,
    ENDPOINT_CLASSES,
    GitHubAppClient,
    GitHubAppError,
    MANAGED_DESCRIPTION,
    TOKEN_URL,
    _bundled_runtime_assets,
    _bundled_worker_config,
)
from src.runtime.task_store import TaskStore

ROOT = Path(__file__).resolve().parents[1]


class MemoryCredentials:
    def __init__(self):
        self.values = {}
        self.singles = []
        self.batches = []
        self.deletions = []

    def save_secret(self, name, value):
        self.singles.append(str(name))
        self.values[name] = str(value)

    def update_secrets(self, updates, *, deletes=()):
        self.batches.append(dict(updates))
        self.values.update({str(name): str(value) for name, value in updates.items()})
        for name in deletes:
            self.values.pop(str(name), None)

    def load_secret(self, name):
        if name not in self.values:
            raise KeyError(name)
        return self.values[name]

    def has_secret(self, name):
        return name in self.values

    def delete_secret(self, name):
        self.deletions.append(str(name))
        return self.values.pop(name, None) is not None


class JsonResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return self.payload


class ManagedRepositoryFake:
    """Stateful GitHub API fake for first-run bootstrap flows.

    Covers identity, managed repository creation/readback, installation
    listing, and installation repository enumeration.  All other paths return
    a generic 201 so environment writes succeed after exact binding.
    """

    WORKER_ID = 101
    MAILBOX_ID = 202

    def __init__(self, *, installation_payload=None, exact_selection=True):
        self.repositories = {}
        self.created = []
        self.installation_payload = installation_payload
        self.exact_selection = exact_selection
        self.fail_once = {}
        # 环境 secrets 名集（真实 API 只回名不回值；PUT 走被测侧桩时同步进来）。
        self.environment_secrets = set()

    def add(self, full_name, *, private, repo_id, description=MANAGED_DESCRIPTION, owner="student"):
        self.repositories[full_name] = {
            "id": repo_id,
            "full_name": full_name,
            "owner": {"login": owner},
            "private": private,
            "description": description,
        }
        return dict(self.repositories[full_name])

    def api(self, method, path, **kwargs):
        self.created.append((method, path, kwargs.get("json")))
        failure = self.fail_once.pop(path, None)
        if failure is not None:
            raise failure
        if path == "/user":
            return JsonResponse({"login": "student", "id": 987654321})
        if path in {"/repos/student/Fudan-CourseLens-Worker", "/repos/student/Fudan-CourseLens-Mailbox"}:
            payload = self.repositories.get(path[len("/repos/"):])
            return JsonResponse(payload, 200) if payload else JsonResponse({}, 404)
        if path.endswith("/generate") and method == "POST":
            created = self.add("student/Fudan-CourseLens-Worker", private=False, repo_id=self.WORKER_ID)
            return JsonResponse(dict(created), 201)
        if path == "/user/repos" and method == "POST":
            created = self.add("student/Fudan-CourseLens-Mailbox", private=True, repo_id=self.MAILBOX_ID)
            return JsonResponse(dict(created), 201)
        if path == "/user/installations":
            payload = self.installation_payload
            if payload is None:
                payload = {"total_count": 1, "installations": [{
                    "id": 42, "app_slug": "fudan-courselens", "target_type": "User",
                    "account": {"login": "student", "type": "User"},
                    "permissions": {"actions": "write"},
                }]}
            return JsonResponse(payload)
        if path == "/user/installations/42/repositories":
            if self.exact_selection:
                return JsonResponse({"total_count": 2, "repositories": [
                    {"name": "Fudan-CourseLens-Worker",
                     "full_name": "student/Fudan-CourseLens-Worker",
                     "owner": {"login": "student"}},
                    {"name": "Fudan-CourseLens-Mailbox",
                     "full_name": "student/Fudan-CourseLens-Mailbox",
                     "owner": {"login": "student"}},
                ]})
            return JsonResponse({"total_count": 2, "repositories": [
                {"name": "Fudan-CourseLens-Worker",
                 "full_name": "student/Fudan-CourseLens-Worker",
                 "owner": {"login": "student"}},
                {"name": "unrelated-repository",
                 "full_name": "student/unrelated-repository",
                 "owner": {"login": "student"}},
            ]})
        if path.endswith("/commits/main"):
            return JsonResponse({"sha": "c" * 40, "commit": {"tree": {"sha": "b" * 40}}})
        if path == "/repos/student/Fudan-CourseLens-Worker/environments/courselens-worker/secrets":
            names = sorted(self.environment_secrets)
            return JsonResponse({"secrets": [{"name": name} for name in names]})
        return JsonResponse({}, 201)


class ProbeErrorFakeApp:
    """Minimal provider snapshot for probe-error evidence tests."""

    def __init__(self):
        self.values = {
            "app_configured": True,
            "authorized": True,
            "access_expires_at": time.time() + 3600,
            "refresh_available": True,
            "bootstrapped": False,
            "installation_url": "",
            "worker_repo": "",
            "mailbox_repo": "",
        }

    def snapshot(self):
        return dict(self.values)


class RepoDenialFake(ManagedRepositoryFake):
    """ManagedRepositoryFake whose deeper Worker probe paths refuse with 403.

    Repository detail reads still succeed, so identity/installation evidence
    stays computable; only the commits/workflows/actions/environments/
    secrets/variables section is denied — an installation/scope refusal,
    never an identity verdict.  ``deny_all=False`` refuses only the
    ``/actions/`` paths so the first refusal lands on ``repos_actions``.
    """

    def __init__(self, *, deny_all=True, **kwargs):
        super().__init__(**kwargs)
        self.deny_all = deny_all
        self.denied_paths = []

    def api(self, method, path, **kwargs):
        if path.startswith("/repos/student/Fudan-CourseLens-Worker/"):
            if self.deny_all or "/actions/" in path:
                self.denied_paths.append(path)
                raise GitHubAppError(
                    "GitHub 返回 HTTP 403（请求 unknown）",
                    code="permission_denied",
                    endpoint_class="repos_actions" if "/actions/" in path else "repos_detail",
                )
        return super().api(method, path, **kwargs)


class PreInstallMailboxDenialFake(ManagedRepositoryFake):
    """未安装 App 的真实令牌语义：私有 mailbox 详情读取 403。

    ``mailbox_denied`` 置位且仓库已存在时，mailbox 详情 GET 抛闭集
    repos_detail 403（刷新令牌在安装前的实测行为——仓库在，但令牌无权读）；
    创建 POST 照常成功并返回创建时载荷。仓库不存在时保持 404 形状。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mailbox_denied = False

    def api(self, method, path, **kwargs):
        if (
            self.mailbox_denied
            and path == "/repos/student/Fudan-CourseLens-Mailbox"
            and "student/Fudan-CourseLens-Mailbox" in self.repositories
        ):
            self.created.append((method, path, kwargs.get("json")))
            raise GitHubAppError(
                "GitHub 返回 HTTP 403（请求 unknown）",
                code="permission_denied",
                endpoint_class="repos_detail",
            )
        return super().api(method, path, **kwargs)


class CreationDenialFake(ManagedRepositoryFake):
    """模板生成步 403：仓库尚未创建（区别于复用路径的已存在 403）。

    bootstrap 的遗留状态降级只允许发生在「读回已存在仓库被拒」时；建仓步
    被 403（如二级限流）说明仓库还不存在，必须保持 fail-closed 上抛。"""

    def api(self, method, path, **kwargs):
        if path.endswith("/generate"):
            raise GitHubAppError(
                "GitHub 返回 HTTP 403（请求 unknown）",
                code="permission_denied",
                endpoint_class="repos_detail",
            )
        return super().api(method, path, **kwargs)


class ForeignBindingDenialFake(ManagedRepositoryFake):
    """Any API touch of the foreign stored binding raises repo-level 403.

    If the read-side owner gate regresses and the stale binding is probed
    with the current token, this fake fails loudly instead of answering 200
    the way a public foreign Worker would in the real incident.
    """

    def api(self, method, path, **kwargs):
        if path.startswith("/repos/former-account/"):
            raise GitHubAppError(
                "GitHub 返回 HTTP 403（请求 unknown）",
                code="permission_denied",
                endpoint_class="repos_detail",
            )
        return super().api(method, path, **kwargs)


class RepoDenialProbeApp:
    """Provider fake whose inspect result is the degraded repo-denial shape."""

    def __init__(self):
        self.values = {
            "app_configured": True,
            "authorized": True,
            "access_expires_at": time.time() + 3600,
            "refresh_available": True,
            "bootstrapped": False,
            "installation_url": "",
            "worker_repo": "",
            "mailbox_repo": "",
        }

    def snapshot(self):
        return dict(self.values)

    def inspect_managed_resources(self):
        return {
            "identity": {"login": "student", "account_id": 987654321},
            "installation": {
                "installed": False,
                "installation_id": 0,
                "repository_selection_exact": False,
                "missing_installation_repositories": [
                    "fudan-courselens-mailbox", "fudan-courselens-worker",
                ],
                "unexpected_installation_repositories": [],
            },
            "installation_setup_url": (
                "https://github.com/apps/fudan-courselens/installations/new/permissions"
                "?suggested_target_id=987654321&repository_ids[]=101&repository_ids[]=202"
            ),
            "repos_access_denied": "repos_detail",
            "worker": {
                "exists": True, "id": 101,
                "full_name": "student/Fudan-CourseLens-Worker",
                "owner": "student", "private": False, "archived": False,
                "disabled": False, "has_issues": True, "default_branch": "main",
                "managed": True, "is_template": False,
            },
            "mailbox": {"exists": False},
            "worker_commit": "",
            "worker_tree": "",
            "expected_commit": "a" * 40,
            "expected_tree": "b" * 40,
            "worker_dispatch_mode": "personal-worker",
            "workflows": {},
            "actions_enabled": False,
            "environment_exists": False,
            "secret_names": [],
            "variables": {},
            "rate_limit": {"remaining": 450, "reset_at": 0.0},
        }


class BindingMismatchProbeApp:
    """Provider fake whose inspect result ignores a cross-account stale binding.

    Worker carries the closed-set mismatch marker (treated as absent); mailbox
    was never bound and carries no marker, so per-card evidence independence
    is observable at probe level.
    """

    def __init__(self):
        self.values = {
            "app_configured": True,
            "authorized": True,
            "access_expires_at": time.time() + 3600,
            "refresh_available": True,
            "bootstrapped": False,
            "installation_url": "",
            "worker_repo": "",
            "mailbox_repo": "",
        }

    def snapshot(self):
        return dict(self.values)

    def inspect_managed_resources(self):
        return {
            "identity": {"login": "student", "account_id": 987654321},
            "installation": {
                "installed": False,
                "installation_id": 0,
                "repository_selection_exact": False,
                "missing_installation_repositories": [
                    "fudan-courselens-mailbox", "fudan-courselens-worker",
                ],
                "unexpected_installation_repositories": [],
            },
            "installation_setup_url": "",
            "repos_access_denied": "",
            "worker": {"exists": False, "binding_owner_mismatch": True},
            "mailbox": {"exists": False},
            "worker_commit": "",
            "worker_tree": "",
            "expected_commit": "a" * 40,
            "expected_tree": "b" * 40,
            "worker_dispatch_mode": "personal-worker",
            "workflows": {},
            "actions_enabled": False,
            "environment_exists": False,
            "secret_names": [],
            "variables": {},
            "rate_limit": {"remaining": 450, "reset_at": 0.0},
        }


class PreInstallAwaitingProbeApp:
    """Provider fake：probe 看到创建即存后的首跑中间态证据。

    Worker 公开可读照常完整；mailbox 携带「已建待安装」闭集降级标记
    （pre_install，安装前用户令牌读不了）；installation 缺失且证据带
    后端合成的预选 setup URL。
    """

    def __init__(self):
        self.values = {
            "app_configured": True,
            "authorized": True,
            "access_expires_at": time.time() + 3600,
            "refresh_available": True,
            "bootstrapped": False,
            "installation_url": "",
            "worker_repo": "student/Fudan-CourseLens-Worker",
            "mailbox_repo": "student/Fudan-CourseLens-Mailbox",
        }

    def snapshot(self):
        return dict(self.values)

    def inspect_managed_resources(self):
        return {
            "identity": {"login": "student", "account_id": 987654321},
            "installation": {
                "installed": False,
                "installation_id": 0,
                "repository_selection_exact": False,
                "missing_installation_repositories": [
                    "fudan-courselens-mailbox", "fudan-courselens-worker",
                ],
                "unexpected_installation_repositories": [],
            },
            "installation_setup_url": (
                "https://github.com/apps/fudan-courselens/installations/new/permissions"
                "?suggested_target_id=987654321&repository_ids[]=101&repository_ids[]=202"
            ),
            "repos_access_denied": "",
            "worker": {
                "exists": True, "id": 101,
                "full_name": "student/Fudan-CourseLens-Worker",
                "owner": "student", "private": False, "archived": False,
                "disabled": False, "has_issues": True, "default_branch": "main",
                "managed": True, "is_template": False,
            },
            "mailbox": {
                "exists": True, "id": 202,
                "full_name": "student/Fudan-CourseLens-Mailbox",
                "owner": "student", "private": True, "managed": True,
                "pre_install": True,
            },
            "worker_commit": "c" * 40,
            "worker_tree": "b" * 40,
            "expected_commit": "a" * 40,
            "expected_tree": "b" * 40,
            "worker_dispatch_mode": "personal-worker",
            "workflows": {
                "process.yml": {"exists": True, "state": "active"},
                "echo.yml": {"exists": True, "state": "active"},
            },
            "actions_enabled": True,
            "environment_exists": False,
            "secret_names": [],
            "variables": {},
            "rate_limit": {"remaining": 450, "reset_at": 0.0},
        }


class GitHubAppTests(unittest.TestCase):
    def setUp(self):
        self.credentials = MemoryCredentials()
        self.client = GitHubAppClient(self.credentials, client_id="client-id", app_slug="fudan-courselens")

    def test_repair_worker_token_expiry_surfaces_reauthorize_code(self):
        """U⑫：修复漏斗里令牌过期 ≠ 从未配置——闭集码引导前端只给重新授权，
        不把用户拽回全套首跑向导重演建仓。"""
        self.credentials.save_secret("github_worker_repo", "student/worker")
        self.credentials.save_secret("github_app_access_token", "stale-token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() - 10))
        with self.assertRaises(GitHubAppError) as caught:
            self.client.repair_worker()
        self.assertEqual(caught.exception.code, "authorization_refresh_required")

    def test_dispatch_workflow_accepts_run_metadata_response(self):
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/worker")
        with patch.object(
            self.client, "_api", return_value=JsonResponse({"workflow_run_id": 123}, 200)
        ) as api:
            self.client.dispatch_workflow("cloud-verify.yml", inputs={"config_hash": "digest"})
        self.assertEqual(api.call_args.kwargs["expected"], (200, 201, 204))

    def test_set_workflow_enabled_is_idempotent_at_target_state(self):
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/worker")
        with patch.object(
            self.client, "_api", return_value=JsonResponse({"state": "disabled_manually"})
        ) as api:
            self.client.set_workflow_enabled("cloud-daily.yml", False)
        api.assert_called_once()
        self.assertEqual(
            api.call_args.args[:2],
            ("GET", "/repos/student/worker/actions/workflows/cloud-daily.yml"),
        )

    def test_stale_job_token_cleanup_is_scoped_and_deletes_token_before_lease(self):
        task_id = "0" * 32
        events = []
        store = Mock()
        store.list_remote_token_leases.return_value = [{"task_id": task_id}]
        store.list_remote_runs.return_value = [{
            "task_id": task_id, "remote_state": "imported",
        }]
        store.active_remote_run_count.return_value = 0
        store.delete_remote_token_lease.side_effect = lambda value: events.append(
            ("lease", value)
        )
        with (
            patch.object(self.client, "list_worker_secrets", return_value=[{
                "name": "COURSELENS_JOB_TOKEN",
            }]),
            patch.object(
                self.client, "delete_job_token", side_effect=lambda: events.append(("token", ""))
            ),
        ):
            self.client.cleanup_stale_job_token_lease(task_id=task_id, task_store=store)
        self.assertEqual(events, [("token", ""), ("lease", task_id)])

    def test_stale_job_token_cleanup_rejects_foreign_lease(self):
        store = Mock()
        store.list_remote_token_leases.return_value = [{"task_id": "1" * 32}]
        with patch.object(self.client, "delete_job_token") as delete:
            with self.assertRaisesRegex(GitHubAppError, "Another remote task"):
                self.client.cleanup_stale_job_token_lease(
                    task_id="0" * 32, task_store=store
                )
        delete.assert_not_called()

    def test_stale_job_token_cleanup_rejects_any_active_remote_run(self):
        store = Mock()
        store.list_remote_token_leases.return_value = [{"task_id": "0" * 32}]
        store.active_remote_run_count.return_value = 1
        with patch.object(self.client, "delete_job_token") as delete:
            with self.assertRaisesRegex(GitHubAppError, "active remote task"):
                self.client.cleanup_stale_job_token_lease(
                    task_id="0" * 32, task_store=store
                )
        delete.assert_not_called()

    def test_stale_job_token_cleanup_rejects_live_in_process_lease(self):
        store = Mock()
        self.client._job_token_leases = 1
        with patch.object(self.client, "delete_job_token") as delete:
            with self.assertRaisesRegex(GitHubAppError, "live job-token lease"):
                self.client.cleanup_stale_job_token_lease(
                    task_id="0" * 32, task_store=store
                )
        delete.assert_not_called()

    def test_bundled_worker_release_defaults_to_approved_signed_mirror(self):
        with patch.dict("os.environ", {}, clear=True):
            config = _bundled_worker_config()
        self.assertEqual(config["mode"], "signed-mirror")
        mirror = _bundled_runtime_assets()["worker_mirror"]
        active = mirror["active"]
        previous = mirror.get("previous")
        entries = [active] if previous is None else [active, previous]
        for entry in entries:
            # REPUBLISH-23: the mirror pin moved to the renamed repository
            # (gualtier-xu/Fudan-CourseLens-Worker); the previous ring may
            # still carry the pre-rename -Release name (rename forwarding).
            self.assertIn(
                entry["repository"],
                {"gualtier-xu/Fudan-CourseLens-Worker", "gualtier-xu/Fudan-CourseLens-Worker-Release"},
            )
            self.assertRegex(entry["commit"], r"^[0-9a-f]{40}$")
            self.assertRegex(entry["tree"], r"^[0-9a-f]{40}$")
            self.assertRegex(entry["manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(entry["signing_key_id"], r"^release-[0-9a-z.-]+$")
            self.assertEqual(entry["trust_epoch"], 1)
            self.assertEqual(entry["protocol_versions"], ["2"])
        # The bundled config is exactly the pinned active release. A clean
        # trust root has no prior release; when one exists it must differ.
        self.assertEqual(config["commit"], active["commit"])
        self.assertEqual(config["tree"], active["tree"])
        self.assertEqual(config["manifest_sha256"], active["manifest_sha256"])
        if previous is not None:
            self.assertNotEqual(active["commit"], previous["commit"])
            self.assertNotEqual(active["tree"], previous["tree"])

    def test_legacy_feature_flag_cannot_override_the_signed_release(self):
        with patch.dict(
            "os.environ", {"COURSELENS_WORKER_MIRROR_MODE": "legacy-template"}, clear=True
        ):
            config = _bundled_worker_config()
        self.assertEqual(config["mode"], "signed-mirror")
        self.assertEqual(config["commit"], _bundled_runtime_assets()["worker_mirror"]["active"]["commit"])
        self.assertEqual(config["tree"], _bundled_runtime_assets()["worker_mirror"]["active"]["tree"])
        self.assertNotEqual(config["manifest_sha256"], "")

    def test_device_flow_saves_expiring_tokens_without_returning_them(self):
        authorization = DeviceAuthorization("device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5)
        responses = [
            JsonResponse({
                "access_token": "access-secret",
                "expires_in": 3600,
                "refresh_token": "refresh-secret",
                "refresh_token_expires_in": 7200,
            }),
            JsonResponse({"login": "student", "id": 123456}),
            JsonResponse({
                "total_count": 1, "installations": [{
                "id": 42,
                "app_slug": "fudan-courselens",
                "target_type": "User",
                "account": {"login": "student", "type": "User"},
                "permissions": {"actions": "write"},
            }]
            }),
        ]

        def external(method, url, **kwargs):
            if url.endswith("/repositories"):
                raise AssertionError("repository enumeration must not run at authorization time")
            return responses.pop(0)

        with patch.object(self.client, "_request_external", side_effect=external):
            result = self.client.poll_device_authorization(authorization)
        self.assertEqual(responses, [], "authorization binds without repository enumeration")
        self.assertEqual(result, {
            "state": "authorized", "login": "student", "account_id": 123456,
            "installed": True, "installation_status": "bound",
        })
        self.assertEqual(self.credentials.load_secret("github_app_access_token"), "access-secret")
        self.assertEqual(self.credentials.load_secret("github_app_installation_id"), "42")
        self.assertNotIn("access-secret", repr(result))

    def test_poll_slow_down_increases_interval_per_rfc8628(self):
        """C1-R20 micro（RFC 8628 §3.5）：slow_down 后 pending 结果回传
        interval+5（原 dead flag 零消费，客户端会以原间隔连续触发慢放）；
        普通 authorization_pending 保持原间隔。"""
        authorization = DeviceAuthorization("device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5)
        responses = [
            JsonResponse({"error": "authorization_pending"}),
            JsonResponse({"error": "slow_down"}),
        ]
        with patch.object(self.client, "_request_external", side_effect=responses):
            first = self.client.poll_device_authorization(authorization)
            second = self.client.poll_device_authorization(authorization)
        self.assertEqual(first, {"state": "pending", "slow_down": False, "interval": 5})
        self.assertEqual(second, {"state": "pending", "slow_down": True, "interval": 10})

    def test_repo_rename_404_maps_to_resource_missing_closed_set(self):
        """T3（夜14-R7 P0）：钉死仓 404 腿＝WORKER-DIAG-1 生产根因回归钉
        （2026-10-02 定谳：仓改名→404→全 LLM 面 remote_failed 归因一天）。
        404 必须落闭集码 resource_missing，文案携带 HTTP 状态与请求号、
        不携带任何敏感物料。"""
        response = Mock()
        response.status_code = 404
        response.headers = {"X-GitHub-Request-Id": "req-404-diag"}
        response.json.return_value = {"message": "Not Found"}
        with patch.object(self.client, "session") as session:
            session.request.return_value = response
            with self.assertRaises(GitHubAppError) as caught:
                self.client._api("GET", "/repos/gone/renamed-worker/commits/" + "a" * 40)
        self.assertEqual(caught.exception.code, "resource_missing")
        self.assertEqual(caught.exception.request_id, "req-404-diag")
        self.assertIn("404", str(caught.exception))
        self.assertNotIn("token", str(caught.exception).casefold())

    def test_validated_worker_release_fails_closed_on_pinned_repo_404(self):
        """T3 同根因第二腿：钉死发布校验在 404 时 fail-closed 上抛，绝不
        回落 stale 信任态（github_worker_verified_* 不被写入）。"""
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        response = Mock()
        response.status_code = 404
        response.headers = {"X-GitHub-Request-Id": "req-404"}
        with (
            patch("src.remote.github_app._bundled_worker_config", return_value={
                "mode": "signed-mirror",
                "repository": "gualtier-xu/Fudan-CourseLens-Worker",
                "commit": "a" * 40,
                "tree": "b" * 40,
            }),
            patch.object(self.client, "session") as session,
        ):
            session.request.return_value = response
            with self.assertRaises(GitHubAppError) as caught:
                self.client._validated_worker_release(token="token")
        self.assertEqual(caught.exception.code, "resource_missing")
        self.assertFalse(self.credentials.has_secret("github_worker_verified_tree"))
        self.assertFalse(self.credentials.has_secret("github_worker_verified_manifest"))

    def test_device_code_default_lifetime_is_900_seconds(self):
        """积压25①：GitHub 不回 expires_in 时缺省 900s（防 0/负寿命）。"""
        with patch.object(
            self.client, "_request_external",
            return_value=JsonResponse({"device_code": "d", "user_code": "ABCD-EFGH", "interval": 5}),
        ):
            authorization = self.client.start_device_authorization()
        self.assertAlmostEqual(authorization.expires_at - time.time(), 900.0, delta=5.0)
        self.assertEqual(authorization.interval, 5)

    def test_poll_treats_exact_expiry_boundary_as_expired(self):
        """积压25②：恰到期边界走 ``>=`` 语义——同一秒即 expired，不发网络。"""
        authorization = DeviceAuthorization(
            "device", "ABCD-EFGH", "https://github.com/login/device", time.time(), 5,
        )
        with patch.object(self.client, "_request_external") as external:
            self.assertEqual(self.client.poll_device_authorization(authorization), {"state": "expired"})
        external.assert_not_called()

    def test_poll_detects_wall_clock_jump_past_expiry(self):
        """积压25④：钟跳（墙钟前跳越过 expires_at）下 expired 判定成立。"""
        real_time = time.time
        authorization = DeviceAuthorization(
            "device", "ABCD-EFGH", "https://github.com/login/device", real_time() + 300, 5,
        )

        def jumped(*_args, **_kwargs):
            return real_time() + 3600.0

        with patch.object(self.client, "_request_external") as external, patch(
            "src.remote.github_app.time.time", side_effect=jumped,
        ):
            self.assertEqual(self.client.poll_device_authorization(authorization), {"state": "expired"})
        external.assert_not_called()

    def test_access_token_freshness_floor_is_max_of_60_and_requested_lifetime(self):
        """积压25③：``expires_at - now >= max(60, minimum_lifetime)`` 边界——
        61s 残余在 60s 请求下新鲜（零刷新）；59s 不新鲜走刷新；恰 60s 新鲜
        （``>=`` 语义）。时钟注入（patch ``src.remote.github_app.time.time``）
        使写入与读取取同一钟值——恰 60s 边界不随实钟两次取值间隙的 tick 翻转
        而随机翻转（2026-10-06 flake 根修：全量三红同族 residual=60.0）。"""
        cases = (
            (61.0, 0),   # 61s > 60s → 新鲜，零刷新
            (59.0, 1),   # 59s < 60s → 不新鲜，刷新一次
            (60.0, 0),   # 恰 60s → >= 判新鲜
        )
        frozen_now = 1_000_000.0
        for residual, expected_refreshes in cases:
            with self.subTest(residual=residual):
                self.credentials.values["github_app_access_token"] = "token"
                self.credentials.values["github_app_access_expires_at"] = str(frozen_now + residual)
                with (
                    patch("src.remote.github_app.time.time", return_value=frozen_now),
                    patch.object(self.client, "refresh_access_token") as refresh,
                ):
                    self.assertEqual(self.client.access_token(minimum_lifetime_seconds=60), "token")
                self.assertEqual(refresh.call_count, expected_refreshes)

    def test_access_token_freshness_boundary_stable_when_clock_advances(self):
        """翻转边界专用钉（2026-10-06 flake 根修）：token 以 ``now+60`` 写入后
        实钟推进 δ，读取时残余=60-δ。真实故障=Windows 实钟粗粒度 tick 恰在
        写入/读取之间翻转，残余 60.0→<60 随机判不新鲜。注入可控时钟把读取
        时刻钉在 59.9/60.0/60.1 三残余点：60.0 与 60.1 新鲜（``>=`` 闭边界，
        零刷新），59.9 走刷新——边界判定只取决于读取时刻残余，不随写入/读取
        间隙漂移。"""
        base = 2_000_000.0
        cases = (
            (60.1, 0),   # 残余 60.1 → 新鲜，零刷新
            (60.0, 0),   # 恰 60.0 → >= 闭边界判新鲜，零刷新
            (59.9, 1),   # 残余 59.9 → 不新鲜，刷新一次
        )
        for residual_at_read, expected_refreshes in cases:
            with self.subTest(residual_at_read=residual_at_read):
                self.credentials.values["github_app_access_token"] = "token"
                # 写入侧：expires_at = base + 60（与故障现场同一构造方式）
                self.credentials.values["github_app_access_expires_at"] = str(base + 60.0)
                # 读取侧：时钟被注入到使残余恰为 residual_at_read 的时刻
                with (
                    patch("src.remote.github_app.time.time", return_value=base + 60.0 - residual_at_read),
                    patch.object(self.client, "refresh_access_token") as refresh,
                ):
                    self.assertEqual(self.client.access_token(minimum_lifetime_seconds=60), "token")
                self.assertEqual(refresh.call_count, expected_refreshes)

    def test_access_token_short_residual_triggers_refresh_before_use(self):
        """积压25③补：请求更大寿命（900s）时 61s 残余同样判不新鲜——
        下限取 max(60, minimum_lifetime)，不是固定 60。"""
        self.credentials.values["github_app_access_token"] = "stale-token"
        self.credentials.values["github_app_access_expires_at"] = str(time.time() + 61.0)

        def refresh():
            self.credentials.values["github_app_access_expires_at"] = str(time.time() + 7200.0)
            self.credentials.values["github_app_access_token"] = "fresh-token"

        with patch.object(self.client, "refresh_access_token", side_effect=refresh):
            self.assertEqual(self.client.access_token(minimum_lifetime_seconds=900), "fresh-token")

    def test_device_flow_saves_grant_when_app_is_not_installed(self):
        """A fresh account without an installation still completes authorization."""
        authorization = DeviceAuthorization("device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5)
        responses = [
            JsonResponse({"access_token": "access-secret", "expires_in": 3600, "refresh_token": "refresh-secret"}),
            JsonResponse({"login": "student", "id": 123456}),
            JsonResponse({"total_count": 0, "installations": []}),
        ]
        with patch.object(self.client, "_request_external", side_effect=responses):
            result = self.client.poll_device_authorization(authorization)
        self.assertEqual(result, {
            "state": "authorized", "login": "student", "account_id": 123456,
            "installed": False, "installation_status": "missing",
        })
        self.assertEqual(self.credentials.load_secret("github_app_access_token"), "access-secret")
        self.assertEqual(self.credentials.load_secret("github_app_refresh_token"), "refresh-secret")
        self.assertFalse(self.credentials.has_secret("github_app_installation_id"),
                         "未安装时绝不持久化 installation id")
        self.assertNotIn("access-secret", repr(result))

    def test_device_flow_saves_grant_but_not_installation_when_scope_is_insufficient(self):
        authorization = DeviceAuthorization("device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5)
        responses = [
            JsonResponse({"access_token": "access-secret", "expires_in": 3600}),
            JsonResponse({"login": "student", "id": 123456}),
            JsonResponse({"total_count": 1, "installations": [{
                "id": 42, "app_slug": "fudan-courselens", "target_type": "User",
                "account": {"login": "student", "type": "User"}, "permissions": {"actions": "read"},
            }]}),
        ]
        with patch.object(self.client, "_request_external", side_effect=responses):
            result = self.client.poll_device_authorization(authorization)
        self.assertEqual(result["state"], "authorized")
        self.assertFalse(result["installed"])
        self.assertEqual(result["installation_status"], "scope_invalid")
        self.assertEqual(self.credentials.load_secret("github_app_access_token"), "access-secret",
                         "用户授权与 App 安装是两个独立状态；授权本身必须先保存")
        self.assertFalse(self.credentials.has_secret("github_app_installation_id"))

    def test_device_flow_treats_inconsistent_installation_inventory_as_unavailable(self):
        authorization = DeviceAuthorization("device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5)
        responses = [
            JsonResponse({"access_token": "access-secret", "expires_in": 3600}),
            JsonResponse({"login": "student", "id": 123456}),
            JsonResponse({"total_count": 5, "installations": [{
                "id": 42, "app_slug": "fudan-courselens", "target_type": "User",
                "account": {"login": "student", "type": "User"}, "permissions": {"actions": "write"},
            }]}),
        ]
        with patch.object(self.client, "_request_external", side_effect=responses):
            result = self.client.poll_device_authorization(authorization)
        self.assertEqual(result["installation_status"], "unavailable")
        self.assertFalse(result["installed"])
        self.assertEqual(self.credentials.load_secret("github_app_access_token"), "access-secret")
        self.assertFalse(self.credentials.has_secret("github_app_installation_id"))

    def test_device_flow_persists_grant_before_identity_network_failure(self):
        """设备码一次性：token 发放后 /user 网络异常不得丢弃用户已批准的授权。"""
        authorization = DeviceAuthorization("device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5)
        responses = [
            JsonResponse({"access_token": "access-secret", "expires_in": 3600, "refresh_token": "refresh-secret"}),
            GitHubAppError("GitHub 连接失败：ConnectionError", code="github_unreachable"),
        ]
        with patch.object(self.client, "_request_external", side_effect=responses):
            result = self.client.poll_device_authorization(authorization)
        self.assertEqual(result, {
            "state": "authorized", "login": "", "account_id": 0,
            "installed": False, "installation_status": "unavailable",
        })
        self.assertEqual(self.credentials.load_secret("github_app_access_token"), "access-secret")
        self.assertEqual(self.credentials.load_secret("github_app_refresh_token"), "refresh-secret")
        self.assertTrue(self.client.snapshot()["authorized"])

    def test_device_flow_clears_grant_only_for_definitively_invalid_token(self):
        """/user 明确无效凭据（401）才按既有 revoked 语义清除并抛闭集错误。"""
        authorization = DeviceAuthorization("device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5)
        responses = [
            JsonResponse({"access_token": "access-secret", "expires_in": 3600, "refresh_token": "refresh-secret"}),
            GitHubAppError("GitHub 返回 HTTP 401", code="authorization_revoked"),
        ]
        with patch.object(self.client, "_request_external", side_effect=responses):
            with self.assertRaises(GitHubAppError) as raised:
                self.client.poll_device_authorization(authorization)
        self.assertEqual(raised.exception.code, "authorization_revoked")
        self.assertFalse(self.credentials.has_secret("github_app_access_token"))
        self.assertFalse(self.credentials.has_secret("github_app_refresh_token"))

    def test_device_flow_keeps_grant_when_identity_check_is_rate_limited(self):
        """限流属瞬态失败：保留授权并返回闭集降级形态。"""
        authorization = DeviceAuthorization("device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5)
        responses = [
            JsonResponse({"access_token": "access-secret", "expires_in": 3600}),
            GitHubAppError("GitHub 返回 HTTP 429", code="rate_limited"),
        ]
        with patch.object(self.client, "_request_external", side_effect=responses):
            result = self.client.poll_device_authorization(authorization)
        self.assertEqual(result["state"], "authorized")
        self.assertEqual(result["installation_status"], "unavailable")
        self.assertFalse(result["installed"])
        self.assertEqual(self.credentials.load_secret("github_app_access_token"), "access-secret")

    def test_device_flow_keeps_grant_when_identity_payload_is_malformed(self):
        """200 响应缺 login/id 不是凭据无效的证据：保授权、降级返回。"""
        authorization = DeviceAuthorization("device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5)
        responses = [
            JsonResponse({"access_token": "access-secret", "expires_in": 3600}),
            JsonResponse({"login": "", "id": 0}),
        ]
        with patch.object(self.client, "_request_external", side_effect=responses):
            result = self.client.poll_device_authorization(authorization)
        self.assertEqual(result["state"], "authorized")
        self.assertEqual(result["installation_status"], "unavailable")
        self.assertEqual(self.credentials.load_secret("github_app_access_token"), "access-secret")

    def test_device_flow_keeps_grant_when_installation_payload_is_malformed(self):
        """安装清单解析崩溃（非 GitHubAppError）同样不得丢授权。"""
        authorization = DeviceAuthorization("device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5)
        responses = [
            JsonResponse({"access_token": "access-secret", "expires_in": 3600}),
            JsonResponse({"login": "student", "id": 123456}),
            Mock(**{"json.side_effect": ValueError("malformed payload")}),
        ]
        with patch.object(self.client, "_request_external", side_effect=responses):
            result = self.client.poll_device_authorization(authorization)
        self.assertEqual(result["state"], "authorized")
        self.assertEqual(result["login"], "student")
        self.assertEqual(result["installation_status"], "unavailable")
        self.assertFalse(result["installed"])
        self.assertEqual(self.credentials.load_secret("github_app_access_token"), "access-secret")
        self.assertFalse(self.credentials.has_secret("github_app_installation_id"))

    def test_device_flow_keeps_grant_when_installation_check_is_refused(self):
        """/user/installations 403（permission_denied）不弃 grant：按闭集降级透传。"""
        authorization = DeviceAuthorization("device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5)
        responses = [
            JsonResponse({"access_token": "access-secret", "expires_in": 3600, "refresh_token": "refresh-secret"}),
            JsonResponse({"login": "student", "id": 123456}),
            GitHubAppError(
                "GitHub 返回 HTTP 403（请求 unknown）", code="permission_denied",
                endpoint_class="user_installations",
            ),
        ]
        with patch.object(self.client, "_request_external", side_effect=responses):
            result = self.client.poll_device_authorization(authorization)
        self.assertEqual(result["state"], "authorized")
        self.assertEqual(result["installation_status"], "unavailable")
        self.assertFalse(result["installed"])
        self.assertEqual(self.credentials.load_secret("github_app_access_token"), "access-secret")
        self.assertEqual(self.credentials.load_secret("github_app_refresh_token"), "refresh-secret")

    def test_device_flow_clears_grant_when_identity_endpoint_refuses(self):
        """身份端点 403（permission_denied + user_identity）= 授权确证无效：清除并抛同款闭集错误。"""
        authorization = DeviceAuthorization("device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5)
        responses = [
            JsonResponse({"access_token": "access-secret", "expires_in": 3600, "refresh_token": "refresh-secret"}),
            GitHubAppError(
                "GitHub 返回 HTTP 403（请求 unknown）", code="permission_denied",
                endpoint_class="user_identity",
            ),
        ]
        with patch.object(self.client, "_request_external", side_effect=responses):
            with self.assertRaises(GitHubAppError) as raised:
                self.client.poll_device_authorization(authorization)
        self.assertEqual(raised.exception.code, "permission_denied")
        self.assertFalse(self.credentials.has_secret("github_app_access_token"))
        self.assertFalse(self.credentials.has_secret("github_app_refresh_token"))

    def test_device_flow_keeps_grant_for_non_identity_permission_denied(self):
        """分支规则钉：permission_denied 仅在 endpoint_class=user_identity 才弃 grant。"""
        authorization = DeviceAuthorization("device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5)
        with patch.object(self.client, "_request_external", side_effect=[
            JsonResponse({"access_token": "access-secret", "expires_in": 3600, "refresh_token": "refresh-secret"}),
            GitHubAppError(
                "GitHub 返回 HTTP 403（请求 unknown）", code="permission_denied",
                endpoint_class="repos_detail",
            ),
        ]):
            result = self.client.poll_device_authorization(authorization)
        self.assertEqual(result["state"], "authorized")
        self.assertEqual(self.credentials.load_secret("github_app_access_token"), "access-secret")
        self.assertTrue(self.client.snapshot()["authorized"])

    def test_secondary_rate_limit_403_is_classified_as_rate_limited(self):
        """403+Retry-After=GitHub 二级限流：分类为 rate_limited，而非 permission_denied。"""
        response = Mock(status_code=403, headers={
            "Retry-After": "57",
            "X-RateLimit-Remaining": "4983",
            "X-RateLimit-Reset": "0",
            "X-GitHub-Request-Id": "req-1",
        })
        session = Mock()
        session.request.return_value = response
        with patch.object(self.client, "session", session):
            with self.assertRaises(GitHubAppError) as raised:
                self.client._api("GET", "/user", token="token")
        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertEqual(raised.exception.retry_after, 57.0)
        self.assertEqual(raised.exception.endpoint_class, "user_identity")

    def test_plain_403_with_remaining_quota_stays_permission_denied(self):
        """无 Retry-After 且配额未尽的 403 仍是 permission_denied（原语义不扩散）。"""
        response = Mock(status_code=403, headers={
            "X-RateLimit-Remaining": "120",
            "X-RateLimit-Reset": "0",
            "X-GitHub-Request-Id": "req-2",
        })
        session = Mock()
        session.request.return_value = response
        with patch.object(self.client, "session", session):
            with self.assertRaises(GitHubAppError) as raised:
                self.client._api(
                    "GET", "/repos/student/Fudan-CourseLens-Worker/actions/permissions", token="token"
                )
        self.assertEqual(raised.exception.code, "permission_denied")
        self.assertEqual(raised.exception.endpoint_class, "repos_actions")

    def test_api_errors_carry_closed_set_endpoint_class(self):
        """路径前缀推导闭集端点类；token 端点标注 token_endpoint；自由文本标签收敛为空串。"""
        cases = [
            ("GET", "/user", "user_identity"),
            ("GET", "/user/installations", "user_installations"),
            ("POST", "/user/repos", "user_repos"),
            ("GET", "/repos/student/Fudan-CourseLens-Worker", "repos_detail"),
            ("GET", "/repos/student/Fudan-CourseLens-Worker/actions/variables", "repos_actions"),
            ("GET", "/repos/student/Fudan-CourseLens-Worker/contents/docs/README.md", "repos_detail"),
        ]
        for method, path, expected_class in cases:
            response = Mock(status_code=404, headers={"X-GitHub-Request-Id": "req-3"})
            session = Mock()
            session.request.return_value = response
            with patch.object(self.client, "session", session):
                with self.assertRaises(GitHubAppError) as raised:
                    self.client._api(method, path, token="token")
            self.assertEqual(raised.exception.endpoint_class, expected_class, path)
        response = Mock(status_code=401, headers={"X-GitHub-Request-Id": "req-4"})
        session = Mock()
        session.request.return_value = response
        with patch.object(self.client, "session", session):
            with self.assertRaises(GitHubAppError) as raised:
                self.client._request_external("POST", TOKEN_URL, data={"client_id": "client-id"})
        self.assertEqual(raised.exception.code, "authorization_revoked")
        self.assertEqual(raised.exception.endpoint_class, "token_endpoint")
        self.assertEqual(GitHubAppError("x", endpoint_class="Resource not accessible").endpoint_class, "")
        self.assertEqual(GitHubAppError("x").endpoint_class, "")
        self.assertEqual(ENDPOINT_CLASSES, frozenset({
            "token_endpoint", "user_identity", "user_installations",
            "user_repos", "repos_detail", "repos_actions",
        }))

    def test_probe_error_evidence_carries_endpoint_class(self):
        """D-C：观测 evidence 携带闭集 endpoint_class，前端可区分安装/身份拒绝。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            app = ProbeErrorFakeApp()
            supervisor = RemoteConnectionSupervisor(store, self.credentials, lambda: app)
            supervisor._record_probe_error(GitHubAppError(
                "GitHub 返回 HTTP 403（请求 req-5）", code="permission_denied",
                request_id="req-5", endpoint_class="user_installations",
            ))
            authorization = store.get_remote_observation("authorization")
            self.assertEqual(authorization["code"], "permission_denied")
            self.assertEqual(authorization["evidence"]["endpoint_class"], "user_installations")
            self.assertEqual(authorization["evidence"]["request_id"], "req-5")
            github_api = store.get_remote_observation("github_api")
            self.assertEqual(github_api["evidence"]["endpoint_class"], "user_installations")
            store.close()

    def test_persisted_grant_resumes_startup_verification_without_user_action(self):
        """授权已持久化后：新客户端实例凭既有 verify 链即可恢复，无需用户动作。"""
        authorization = DeviceAuthorization("device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5)
        with patch.object(self.client, "_request_external", side_effect=[
            JsonResponse({"access_token": "access-secret", "expires_in": 3600, "refresh_token": "refresh-secret"}),
            GitHubAppError("GitHub 连接失败：ConnectionError", code="github_unreachable"),
        ]):
            degraded = self.client.poll_device_authorization(authorization)
        self.assertEqual(degraded["installation_status"], "unavailable")

        revived = GitHubAppClient(self.credentials, client_id="client-id", app_slug="fudan-courselens")
        with patch.object(revived, "_request_external", side_effect=[
            JsonResponse({"login": "student", "id": 123456}),
            JsonResponse({"total_count": 1, "installations": [{
                "id": 42, "app_slug": "fudan-courselens", "target_type": "User",
                "account": {"login": "student", "type": "User"},
                "permissions": {"actions": "write"},
            }]}),
        ]):
            verified = revived.verify_user_authorization()
        self.assertEqual(verified, {"authorized": True, "installed": True})

    def test_read_only_integrity_does_not_refresh_or_save(self):
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 1))
        self.credentials.save_secret("github_worker_repo", "student/worker")
        with patch.object(self.client, "refresh_access_token") as refresh:
            with self.assertRaisesRegex(GitHubAppError, "不够新鲜"):
                self.client.check_worker_integrity(read_only=True)
        refresh.assert_not_called()

    def test_live_authorization_check_does_not_return_identity_or_token(self):
        self.credentials.save_secret("github_app_access_token", "secret-token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        with (
            patch.object(
                self.client, "_api", return_value=JsonResponse({"login": "student", "id": 123})
            ),
            patch.object(
                self.client, "_fresh_installation_state",
                return_value=({"id": 42}, "bound"),
            ) as installation_state,
        ):
            result = self.client.verify_user_authorization()
        self.assertEqual(result, {"authorized": True, "installed": True})
        self.assertNotIn("student", repr(result))
        self.assertNotIn("secret-token", repr(result))
        self.assertFalse(self.credentials.has_secret("github_app_installation_id"),
                         "verify 只读取证据，绝不持久化 installation id")
        installation_state.assert_called_once()

    def _run_bootstrap(self, fake):
        """Run bootstrap against a ManagedRepositoryFake with the standard patches."""
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        secret_names = []
        variable_names = []
        variable_values = {}
        mirror_digest = "d" * 64

        def record_repo_variable(_repo, name, value, _token):
            variable_names.append(name)
            variable_values[name] = value

        def trusted_integrity():
            self.credentials.save_secret("github_worker_verified_tree", "b" * 40)
            self.credentials.save_secret("github_worker_verified_manifest", mirror_digest)
            return {"trusted": True}

        with (
            patch("src.remote.github_app._bundled_worker_config", return_value={
                "mode": "signed-mirror",
                "repository": "gualtier-xu/Fudan-CourseLens-Worker",
                "commit": "a" * 40,
                "tree": "b" * 40,
                "manifest_sha256": mirror_digest,
            }),
            patch.object(self.client, "_api", side_effect=fake.api),
            patch.object(self.client, "_sync_managed_mailbox_documents") as sync_mailbox_documents,
            patch.object(self.client, "_put_environment_secret", side_effect=lambda _repo, name, _value, _token: (secret_names.append(name), fake.environment_secrets.add(name))),
            patch.object(self.client, "_put_repo_variable", side_effect=record_repo_variable),
            patch.object(self.client, "check_worker_integrity", side_effect=trusted_integrity),
        ):
            result = self.client.bootstrap_student_repositories()
        return {
            "result": result,
            "fake": fake,
            "sync_mailbox_documents": sync_mailbox_documents,
            "secret_names": secret_names,
            "variable_names": variable_names,
            "variable_values": variable_values,
        }

    def test_bootstrap_creates_managed_repositories_and_keeps_private_keys_remote(self):
        artifacts = self._run_bootstrap(ManagedRepositoryFake())
        result = artifacts["result"]
        sync_mailbox_documents = artifacts["sync_mailbox_documents"]
        secret_names = artifacts["secret_names"]
        variable_names = artifacts["variable_names"]
        created = artifacts["fake"].created
        self.assertEqual(result["setup_state"], "complete")
        self.assertTrue(result["bootstrapped"])
        self.assertEqual(result["worker_repo"], "student/Fudan-CourseLens-Worker")
        self.assertEqual(result["mailbox_repo"], "student/Fudan-CourseLens-Mailbox")
        self.assertEqual(secret_names, ["WORKER_INPUT_PRIVATE_KEY", "WORKER_SIGNING_PRIVATE_KEY"])
        self.assertNotIn("COURSELENS_JOB_TOKEN", secret_names)
        self.assertIn("COURSELENS_MAILBOX_REPO", variable_names)
        self.assertFalse(self.credentials.has_secret("worker_box_private_key"))
        self.assertFalse(self.credentials.has_secret("worker_signing_private_key"))
        self.assertTrue(result["worker_trusted"])
        self.assertTrue(any(path.endswith("/generate") for _, path, _ in created))
        self.assertTrue(any(path == "/user/repos" for _, path, _ in created))
        sync_mailbox_documents.assert_called_once_with("student/Fudan-CourseLens-Mailbox", "token")

    def test_bootstrap_pins_ocr_concurrency_to_two(self):
        """N9（夜14-R1）：执行仓变量 OCR 并发钉 2——幻灯多的讲 OCR 墙钟约
        −30-50%；代码侧帽 min(2, env)（worker ocr.py）不变，仍可经 Variables
        下调为 1。"""
        artifacts = self._run_bootstrap(ManagedRepositoryFake())
        self.assertEqual(
            artifacts["variable_values"].get("COURSELENS_OCR_CONCURRENCY"), "2"
        )
        self.assertEqual(
            artifacts["variable_values"].get("COURSELENS_ASR_STRATEGY"), "sequential"
        )
        self.assertEqual(
            artifacts["variable_values"].get("COURSELENS_IMAGE_PREFETCH"), "16"
        )

    def _run_bootstrap_recording_ensure_calls(self, fake):
        """Bootstrap with _ensure_managed_repository call arguments captured."""
        real = self.client._ensure_managed_repository
        calls = []

        def recording(owner, name, **kwargs):
            calls.append({"name": name, **kwargs})
            return real(owner, name, **kwargs)

        with patch.object(self.client, "_ensure_managed_repository", side_effect=recording):
            artifacts = self._run_bootstrap(fake)
        return artifacts, calls

    def test_bootstrap_pins_worker_repository_call_to_public_visibility(self):
        """钉：worker 实例仓的 _ensure_managed_repository 调用必须 private=False。

        worker 仓承载学生 Actions 跑批与镜像观测，公开可读是远端观测闭集
        的前提；未来任何翻转（truthy 私有化）都在此红。创建载荷同钉。"""
        artifacts, calls = self._run_bootstrap_recording_ensure_calls(ManagedRepositoryFake())
        self.assertEqual(artifacts["result"]["setup_state"], "complete")
        worker_calls = [call for call in calls if call.get("role") == "worker"]
        self.assertEqual(len(worker_calls), 1, "worker 建仓调用恰一次")
        call = worker_calls[0]
        self.assertEqual(call["name"], "Fudan-CourseLens-Worker")
        self.assertEqual(call["stored_secret"], "github_worker_repo")
        self.assertIs(call["private"], False, "worker 仓必须显式 private=False")
        create_payloads = [
            payload for _, path, payload in artifacts["fake"].created
            if path.endswith("/generate") and isinstance(payload, dict)
        ]
        self.assertEqual(len(create_payloads), 1)
        self.assertIs(create_payloads[0]["private"], False, "worker 创建载荷必须显式 private=False")

    def test_bootstrap_pins_mailbox_repository_call_to_private_visibility(self):
        """钉：mailbox 实例仓的 _ensure_managed_repository 调用必须 private=True。

        Mailbox 承载学生私有回传文档，私密性是隐私承诺的实现面；未来任何
        翻转（含改为公开或丢参）都在此红。创建载荷同钉。"""
        artifacts, calls = self._run_bootstrap_recording_ensure_calls(ManagedRepositoryFake())
        self.assertEqual(artifacts["result"]["setup_state"], "complete")
        mailbox_calls = [call for call in calls if call.get("role") == "mailbox"]
        self.assertEqual(len(mailbox_calls), 1, "mailbox 建仓调用恰一次")
        call = mailbox_calls[0]
        self.assertEqual(call["name"], "Fudan-CourseLens-Mailbox")
        self.assertEqual(call["stored_secret"], "github_mailbox_repo")
        self.assertIs(call["private"], True, "mailbox 仓必须显式 private=True")
        create_payloads = [
            payload for _, path, payload in artifacts["fake"].created
            if path == "/user/repos" and isinstance(payload, dict)
        ]
        self.assertEqual(len(create_payloads), 1)
        self.assertIs(create_payloads[0]["private"], True, "mailbox 创建载荷必须显式 private=True")

    def test_bootstrap_self_heals_stale_local_keys_when_remote_secrets_missing(self):
        """活体场景复刻：删仓重建后远端环境 secrets 空、本地残留旧公钥。

        旧判断只看本地公钥会误跳过上传 → environment_incomplete 死锁；
        成对自愈必须重新生成并上传两把私钥、更新本地公钥、作废旧完整性
        证据并立即重验（翻绿），setup_state=complete。"""
        stale_box, stale_signing = "stale-box-public", "stale-signing-public"
        self.credentials.save_secret("worker_box_public_key", stale_box)
        self.credentials.save_secret("worker_signing_public_key", stale_signing)
        artifacts = self._run_bootstrap(ManagedRepositoryFake())
        result = artifacts["result"]
        secret_names = artifacts["secret_names"]
        self.assertEqual(result["setup_state"], "complete")
        self.assertEqual(secret_names, ["WORKER_INPUT_PRIVATE_KEY", "WORKER_SIGNING_PRIVATE_KEY"])
        self.assertNotEqual(
            self.credentials.load_secret("worker_box_public_key"), stale_box,
            "残留旧公钥必须被重新生成的新公钥替换",
        )
        self.assertNotEqual(
            self.credentials.load_secret("worker_signing_public_key"), stale_signing,
        )
        self.assertIn("github_worker_verified_tree", self.credentials.deletions)
        self.assertIn("github_worker_verified_manifest", self.credentials.deletions)
        # 换钥后立即重验：证据重立、快照翻绿。
        self.assertTrue(result["worker_trusted"])
        self.assertEqual(
            self.credentials.load_secret("github_worker_verified_tree"), "b" * 40,
        )

    def test_bootstrap_skips_key_rotation_when_both_sides_established(self):
        """两侧齐备（本地公钥 + 远端两把私钥名）→ 零上传、零轮换、零证据作废。"""
        fake = ManagedRepositoryFake()
        fake.environment_secrets = {"WORKER_INPUT_PRIVATE_KEY", "WORKER_SIGNING_PRIVATE_KEY"}
        self.credentials.save_secret("worker_box_public_key", "box-public")
        self.credentials.save_secret("worker_signing_public_key", "signing-public")
        self.credentials.save_secret("github_worker_verified_tree", "b" * 40)
        self.credentials.save_secret("github_worker_verified_manifest", "d" * 64)
        artifacts = self._run_bootstrap(fake)
        result = artifacts["result"]
        self.assertEqual(result["setup_state"], "complete")
        self.assertEqual(
            artifacts["secret_names"], [],
            "两侧齐备时不得重新生成或上传任何密钥",
        )
        self.assertNotIn("github_worker_verified_tree", self.credentials.deletions)
        self.assertNotIn("github_worker_verified_manifest", self.credentials.deletions)
        secrets_reads = [
            path for _, path, _ in fake.created
            if path.endswith("/environments/courselens-worker/secrets")
        ]
        self.assertEqual(len(secrets_reads), 1, "远端名集恰读一次")

    def test_bootstrap_restores_local_public_keys_when_remote_secrets_present(self):
        """远端私钥在、本地公钥缺 → 重新生成（PUT 可观察）+ 本地补存公钥。"""
        fake = ManagedRepositoryFake()
        fake.environment_secrets = {"WORKER_INPUT_PRIVATE_KEY", "WORKER_SIGNING_PRIVATE_KEY"}
        artifacts = self._run_bootstrap(fake)
        result = artifacts["result"]
        self.assertEqual(result["setup_state"], "complete")
        self.assertEqual(
            artifacts["secret_names"],
            ["WORKER_INPUT_PRIVATE_KEY", "WORKER_SIGNING_PRIVATE_KEY"],
        )
        self.assertTrue(self.credentials.load_secret("worker_box_public_key"))
        self.assertTrue(self.credentials.load_secret("worker_signing_public_key"))

    def test_bootstrap_creates_repositories_before_demanding_installation(self):
        fake = ManagedRepositoryFake()
        self._run_bootstrap(fake)
        paths = [path for _, path, _ in fake.created]
        first_installation_scan = paths.index("/user/installations")
        last_creation = max(
            index for index, path in enumerate(paths)
            if path.endswith("/generate") or path == "/user/repos"
        )
        self.assertLess(
            last_creation, first_installation_scan,
            "两个受管仓库必须先于任何安装范围检查创建",
        )

    def test_bootstrap_never_calls_billing_or_plan_endpoints(self):
        fake = ManagedRepositoryFake()
        self._run_bootstrap(fake)
        for _method, path, _json in fake.created:
            self.assertNotIn("billing", path)
            self.assertNotIn("/plan", path.lower())

    def test_bootstrap_never_writes_a_remote_compute_switch_flag(self):
        """U1：开关退役——bootstrap 照旧不写 remote_enabled（凭据里再没有
        这个「暂停」位；云端处理默认允许，派发门只看连接/授权真值）。"""
        artifacts = self._run_bootstrap(ManagedRepositoryFake())

        self.assertTrue(artifacts["result"]["bootstrapped"])
        self.assertFalse(
            self.credentials.has_secret("remote_enabled"),
            "bootstrap must not write remote_enabled; the switch is retired",
        )

    def test_bootstrap_binds_repositories_and_keys_as_atomic_generations(self):
        self._run_bootstrap(ManagedRepositoryFake())

        binding_names = {
            "github_worker_repo", "github_mailbox_repo",
            "worker_box_public_key", "worker_signing_public_key",
        }
        for name in binding_names:
            self.assertNotIn(
                name, self.credentials.singles,
                f"{name} must be written through the atomic update_secrets swap",
            )
        # 创建即存（FIRST-RUN-SMOOTH-1）：两仓就绪的当下各一次原子批量写
        # （绑定 + 保存元数据）；安装 id 与公开密钥仍在其后原子落盘。
        self.assertEqual(len(self.credentials.batches), 4)
        worker_batch, mailbox_batch, installation_batch, keys_batch = self.credentials.batches
        self.assertEqual(worker_batch["github_worker_repo"], "student/Fudan-CourseLens-Worker")
        self.assertEqual(
            json.loads(worker_batch["github_managed_repo_meta"])["worker"],
            {"id": 101, "full_name": "student/Fudan-CourseLens-Worker",
             "owner": "student", "private": False, "managed": True},
        )
        self.assertEqual(mailbox_batch["github_mailbox_repo"], "student/Fudan-CourseLens-Mailbox")
        self.assertEqual(
            json.loads(mailbox_batch["github_managed_repo_meta"])["mailbox"],
            {"id": 202, "full_name": "student/Fudan-CourseLens-Mailbox",
             "owner": "student", "private": True, "managed": True},
        )
        self.assertEqual(installation_batch, {"github_app_installation_id": "42"})
        self.assertEqual(
            keys_batch,
            {"worker_box_public_key": self.credentials.values["worker_box_public_key"],
             "worker_signing_public_key": self.credentials.values["worker_signing_public_key"]},
        )

    def test_mailbox_document_sync_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            templates = {
                "README.md": root / "README.md",
                "docs/technical/README.md": root / "technical.md",
            }
            for index, template in enumerate(templates.values(), start=1):
                template.write_bytes(f"<!-- managed:{index} -->\n".encode())

            def api(method, path, **_kwargs):
                self.assertEqual(method, "GET")
                repository_path = path.split("/contents/", 1)[1]
                content = templates[repository_path].read_bytes()
                return JsonResponse({
                    "sha": "a" * 40,
                    "content": base64.b64encode(content).decode("ascii"),
                })

            with (
                patch("src.remote.github_app.MANAGED_MAILBOX_DOCUMENTS", templates),
                patch.object(self.client, "_api", side_effect=api) as request,
            ):
                changed = self.client._sync_managed_mailbox_documents("student/mailbox", "token")
        self.assertEqual(changed, {
            "README.md": False,
            "docs/technical/README.md": False,
        })
        self.assertEqual(request.call_count, 2)

    def test_mailbox_document_partial_sync_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            templates = {
                "README.md": root / "README.md",
                "docs/technical/README.md": root / "technical.md",
            }
            for template in templates.values():
                template.write_text("managed\n", encoding="utf-8")

            def api(method, path, **_kwargs):
                if path.endswith("/contents/README.md"):
                    return JsonResponse({}, 404) if method == "GET" else JsonResponse({}, 201)
                raise GitHubAppError("synthetic failure", code="github_http_error")

            with (
                patch("src.remote.github_app.MANAGED_MAILBOX_DOCUMENTS", templates),
                patch.object(self.client, "_api", side_effect=api),
            ):
                with self.assertRaises(GitHubAppError) as raised:
                    self.client._sync_managed_mailbox_documents("student/mailbox", "token")
        self.assertEqual(raised.exception.code, "mailbox_documents_partial_sync")

    def test_bootstrap_from_clean_account_awaits_installation_with_preselected_url(self):
        fake = ManagedRepositoryFake(installation_payload={"total_count": 0, "installations": []})
        result = self._run_bootstrap(fake)["result"]
        self.assertEqual(result["setup_state"], "awaiting_installation")
        self.assertEqual(result["installation_status"], "missing")
        self.assertEqual(result["worker_repo"], "student/Fudan-CourseLens-Worker")
        self.assertEqual(result["mailbox_repo"], "student/Fudan-CourseLens-Mailbox")
        self.assertEqual(
            result["installation_setup_url"],
            "https://github.com/apps/fudan-courselens/installations/new/permissions"
            "?suggested_target_id=987654321&repository_ids[]=101&repository_ids[]=202",
        )
        self.assertEqual(result["installation_settings_url"], "")
        self.assertFalse(self.credentials.has_secret("github_app_installation_id"))
        # 创建即存（FIRST-RUN-SMOOTH-1）：绑定与创建时元数据在建仓当下立即落盘；
        # 公开密钥仍只在精确安装读取之后落盘
        self.assertEqual(self.credentials.load_secret("github_worker_repo"), "student/Fudan-CourseLens-Worker")
        self.assertEqual(self.credentials.load_secret("github_mailbox_repo"), "student/Fudan-CourseLens-Mailbox")
        meta = json.loads(self.credentials.load_secret("github_managed_repo_meta"))
        self.assertEqual(meta["worker"]["id"], 101)
        self.assertEqual(meta["mailbox"]["id"], 202)
        self.assertFalse(self.credentials.has_secret("worker_box_public_key"))
        self.assertFalse(self.credentials.has_secret("worker_signing_public_key"))
        # 结构化中间态不是失败：Worker 与 Mailbox 都已创建
        self.assertIn("student/Fudan-CourseLens-Worker", fake.repositories)
        self.assertIn("student/Fudan-CourseLens-Mailbox", fake.repositories)

    def test_bootstrap_retry_after_awaiting_creates_no_duplicate_repositories(self):
        fake = ManagedRepositoryFake(installation_payload={"total_count": 0, "installations": []})
        awaiting = self._run_bootstrap(fake)["result"]
        self.assertEqual(awaiting["setup_state"], "awaiting_installation")
        # 用户已在 GitHub 确认安装：同一账号重试必须完全复用既有仓库
        fake.installation_payload = None
        result = self._run_bootstrap(fake)["result"]
        self.assertEqual(result["setup_state"], "complete")
        worker_creates = [item for item in fake.created if item[1].endswith("/generate")]
        mailbox_creates = [item for item in fake.created if item[1] == "/user/repos"]
        self.assertEqual(len(worker_creates), 1, "重试绝不重复创建 Worker")
        self.assertEqual(len(mailbox_creates), 1, "重试绝不重复创建 Mailbox")
        self.assertTrue(self.credentials.has_secret("github_app_installation_id"))
        self.assertEqual(self.credentials.load_secret("github_worker_repo"), "student/Fudan-CourseLens-Worker")

    def test_bootstrap_recovers_when_only_worker_was_created(self):
        fake = ManagedRepositoryFake()
        fake.fail_once["/user/repos"] = GitHubAppError("synthetic", code="github_request_failed")
        with self.assertRaises(GitHubAppError):
            self._run_bootstrap(fake)
        self.assertIn("student/Fudan-CourseLens-Worker", fake.repositories)
        self.assertNotIn("student/Fudan-CourseLens-Mailbox", fake.repositories)
        result = self._run_bootstrap(fake)["result"]
        self.assertEqual(result["setup_state"], "complete")
        worker_creates = [item for item in fake.created if item[1].endswith("/generate")]
        self.assertEqual(len(worker_creates), 1, "只创建了 Worker 时，恢复不得再建一个 Worker")
        self.assertEqual(
            set(fake.repositories),
            {"student/Fudan-CourseLens-Worker", "student/Fudan-CourseLens-Mailbox"},
            "恢复只补建缺失的 Mailbox，绝不产生后缀副本",
        )
        self.assertEqual(
            result["worker_repo"], "student/Fudan-CourseLens-Worker",
            "恢复必须复用既有受管 Worker",
        )

    def test_bootstrap_refuses_unmanaged_same_name_repository(self):
        fake = ManagedRepositoryFake(installation_payload={"total_count": 0, "installations": []})
        fake.add("student/Fudan-CourseLens-Worker", private=False, repo_id=999, description="user repository")
        with self.assertRaises(GitHubAppError) as raised:
            self._run_bootstrap(fake)
        self.assertEqual(raised.exception.code, "managed_repository_name_conflict")
        self.assertFalse(any(path.endswith("/generate") for _, path, _ in fake.created),
                         "同名冲突时绝不创建带后缀的替身仓库")
        self.assertFalse(any(path == "/user/repos" for _, path, _ in fake.created))

    def test_bootstrap_ignores_stored_binding_of_a_different_account(self):
        self.credentials.save_secret("github_worker_repo", "former-account/Fudan-CourseLens-Worker")
        self.credentials.save_secret("github_mailbox_repo", "former-account/Fudan-CourseLens-Mailbox")
        fake = ManagedRepositoryFake()
        result = self._run_bootstrap(fake)["result"]
        self.assertEqual(result["setup_state"], "complete")
        self.assertNotIn("/repos/former-account/Fudan-CourseLens-Worker", [path for _, path, _ in fake.created],
                         "换绑账号后旧账号绑定必须被忽略")
        self.assertEqual(self.credentials.load_secret("github_worker_repo"), "student/Fudan-CourseLens-Worker")

    def test_bootstrap_awaits_settings_adjustment_when_actions_permission_is_insufficient(self):
        fake = ManagedRepositoryFake(installation_payload={"total_count": 1, "installations": [{
            "id": 42, "app_slug": "fudan-courselens", "target_type": "User",
            "account": {"login": "student", "type": "User"}, "permissions": {"actions": "read"},
        }]})
        result = self._run_bootstrap(fake)["result"]
        self.assertEqual(result["setup_state"], "awaiting_installation")
        self.assertEqual(result["installation_status"], "scope_invalid")
        self.assertEqual(result["installation_settings_url"], "https://github.com/settings/installations/42")
        self.assertEqual(result["installation_setup_url"], "")
        self.assertFalse(self.credentials.has_secret("github_app_installation_id"))

    def test_bootstrap_awaits_settings_adjustment_when_selection_is_not_exact(self):
        fake = ManagedRepositoryFake(exact_selection=False)
        result = self._run_bootstrap(fake)["result"]
        self.assertEqual(result["setup_state"], "awaiting_installation")
        self.assertEqual(result["installation_status"], "not_exact")
        self.assertEqual(result["installation_settings_url"], "https://github.com/settings/installations/42")
        self.assertEqual(result["installation_setup_url"], "",
                         "范围不精确时绝不输出预选安装链接")
        self.assertFalse(self.credentials.has_secret("github_app_installation_id"))

    def test_bootstrap_awaits_without_links_when_installation_inventory_is_unavailable(self):
        fake = ManagedRepositoryFake(installation_payload={"total_count": 7, "installations": [{
            "id": 42, "app_slug": "fudan-courselens", "target_type": "User",
            "account": {"login": "student", "type": "User"}, "permissions": {"actions": "write"},
        }]})
        result = self._run_bootstrap(fake)["result"]
        self.assertEqual(result["setup_state"], "awaiting_installation")
        self.assertEqual(result["installation_status"], "unavailable")
        self.assertEqual(result["installation_settings_url"], "")
        self.assertEqual(result["installation_setup_url"], "")

    def test_bootstrap_rerun_before_installation_tolerates_private_mailbox_403(self):
        """D3：已建仓 + 未安装时重跑不得失败——私有 mailbox 读取 403（未安装时
        用户令牌无权）按「已建待安装」降级（用保存元数据），仍返回
        awaiting_installation 与预选 setup URL，绝不抛 permission_denied 终断
        动作（原死锁：按钮失败→链接永不出→用户无从安装）；且绝不重复建仓。"""
        fake = PreInstallMailboxDenialFake(installation_payload={"total_count": 0, "installations": []})
        awaiting = self._run_bootstrap(fake)["result"]
        self.assertEqual(awaiting["setup_state"], "awaiting_installation")
        self.assertEqual(self.credentials.load_secret("github_mailbox_repo"), "student/Fudan-CourseLens-Mailbox")
        # 用户尚未完成 App 安装、稍后重试：此时用户令牌读不了私有 mailbox（403）
        fake.mailbox_denied = True
        rerun = self._run_bootstrap(fake)["result"]
        self.assertEqual(rerun["setup_state"], "awaiting_installation")
        self.assertEqual(rerun["installation_status"], "missing")
        self.assertEqual(
            rerun["installation_setup_url"],
            "https://github.com/apps/fudan-courselens/installations/new/permissions"
            "?suggested_target_id=987654321&repository_ids[]=101&repository_ids[]=202",
        )
        self.assertEqual(len([item for item in fake.created if item[1] == "/user/repos"]), 1,
                         "重跑绝不重复创建 Mailbox")
        self.assertEqual(len([item for item in fake.created if item[1].endswith("/generate")]), 1,
                         "重跑绝不重复创建 Worker")

    def test_bootstrap_rerun_without_creation_evidence_downgrades_to_awaiting(self):
        """遗留状态降级（复用路径 403 零证据）：同名仓库已存在但 App 未安装
        读不了——bootstrap 不再死锁上抛，降级为 awaiting_installation：缺安装
        给账号级官方安装页（不发明仓库 id），失败仓库绝无绑定/元数据落盘，
        已验证的 Worker 名如实带出。"""
        fake = PreInstallMailboxDenialFake(installation_payload={"total_count": 0, "installations": []})
        fake.mailbox_denied = True
        fake.add("student/Fudan-CourseLens-Mailbox", private=True, repo_id=202)
        result = self._run_bootstrap(fake)["result"]
        self.assertEqual(result["setup_state"], "awaiting_installation")
        self.assertEqual(result["installation_status"], "missing")
        self.assertEqual(
            result["installation_setup_url"],
            "https://github.com/apps/fudan-courselens/installations/new",
            "零证据降级给账号级安装页，绝不发明预选仓库 id",
        )
        self.assertEqual(result["installation_settings_url"], "")
        self.assertEqual(result["worker_repo"], "student/Fudan-CourseLens-Worker")
        self.assertEqual(result["mailbox_repo"], "", "未验证的仓库名不得带出")
        self.assertFalse(self.credentials.has_secret("github_mailbox_repo"))
        meta = json.loads(self.credentials.load_secret("github_managed_repo_meta"))
        self.assertNotIn("mailbox", meta, "失败仓库绝无创建时证据落盘")
        self.assertIn("worker", meta, "先成功的 Worker 依创建即存落盘")

    def test_bootstrap_leftover_denial_with_scope_problem_yields_settings_url(self):
        """遗留状态降级且安装范围类问题并存：降级结果给受信设置页链接（真实
        安装 id），不给账号级安装页——两类指引互斥且各自可信。"""
        fake = PreInstallMailboxDenialFake(
            installation_payload={"total_count": 1, "installations": [{
                "id": 42, "app_slug": "fudan-courselens", "target_type": "User",
                "account": {"login": "student", "type": "User"}, "permissions": {"actions": "read"},
            }]},
        )
        fake.mailbox_denied = True
        fake.add("student/Fudan-CourseLens-Mailbox", private=True, repo_id=202)
        result = self._run_bootstrap(fake)["result"]
        self.assertEqual(result["setup_state"], "awaiting_installation")
        self.assertEqual(result["installation_status"], "scope_invalid")
        self.assertEqual(result["installation_settings_url"], "https://github.com/settings/installations/42")
        self.assertEqual(result["installation_setup_url"], "")

    def test_bootstrap_create_step_403_still_fails_closed(self):
        """建仓步 403（仓库尚不存在，如模板生成被限流）不降级：保持闭集上抛
        ——降级只属于「仓库已存在但读不了」的复用路径，绝不掩盖创建失败。"""
        fake = CreationDenialFake(installation_payload={"total_count": 0, "installations": []})
        with self.assertRaises(GitHubAppError) as raised:
            self._run_bootstrap(fake)
        self.assertEqual(raised.exception.code, "permission_denied")
        self.assertEqual(raised.exception.endpoint_class, "repos_detail")

    def test_bootstrap_marks_pre_install_token_and_refreshes_once_when_bound(self):
        """T5 安装即重发令牌：安装缺失期间的 bootstrap 落 pre-install 标记；
        安装 无→有 的下一次 bootstrap 自动重发用户令牌恰一次并清标记——
        免去手动 Revoke 重授权。"""
        awaiting = self._run_bootstrap(
            ManagedRepositoryFake(installation_payload={"total_count": 0, "installations": []})
        )["result"]
        self.assertEqual(awaiting["setup_state"], "awaiting_installation")
        self.assertEqual(self.credentials.load_secret("github_token_pre_install"), "1")

        # 安装出现前的下一次 bootstrap：带 refresh token 才能重发用户令牌
        self.credentials.save_secret("github_app_refresh_token", "refresh-token")

        def rotate():
            self.credentials.save_secret("github_app_access_token", "rotated-token")
            self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))

        fake = ManagedRepositoryFake()
        fake.add("student/Fudan-CourseLens-Worker", private=False, repo_id=101)
        fake.add("student/Fudan-CourseLens-Mailbox", private=True, repo_id=202)
        with patch.object(self.client, "refresh_access_token", side_effect=rotate) as refresh:
            result = self._run_bootstrap(fake)["result"]
        refresh.assert_called_once()
        self.assertFalse(self.credentials.has_secret("github_token_pre_install"))
        self.assertEqual(self.credentials.load_secret("github_app_access_token"), "rotated-token")
        self.assertEqual(result["setup_state"], "complete")

    def test_bootstrap_refresh_failure_keeps_pre_install_marker(self):
        """刷新失败上抛且标记保留：下次 bootstrap 恰重试一次（有界幂等）。"""
        self.credentials.save_secret("github_token_pre_install", "1")
        self.credentials.save_secret("github_app_refresh_token", "refresh-token")
        with patch.object(
            self.client, "refresh_access_token",
            side_effect=GitHubAppError("网络", code="github_unreachable"),
        ):
            with self.assertRaises(GitHubAppError):
                self._run_bootstrap(ManagedRepositoryFake())
        self.assertEqual(self.credentials.load_secret("github_token_pre_install"), "1")

    def test_bootstrap_without_pre_install_marker_never_refreshes(self):
        with patch.object(self.client, "refresh_access_token") as refresh:
            result = self._run_bootstrap(ManagedRepositoryFake())["result"]
        refresh.assert_not_called()
        self.assertEqual(result["setup_state"], "complete")

    def test_bootstrap_without_refresh_token_clears_marker_and_completes(self):
        """无 refresh token 时只清标记：绝不把可完成的初始化变成授权报错。"""
        self.credentials.save_secret("github_token_pre_install", "1")
        result = self._run_bootstrap(ManagedRepositoryFake())["result"]
        self.assertFalse(self.credentials.has_secret("github_token_pre_install"))
        self.assertEqual(result["setup_state"], "complete")

    def test_bootstrap_create_path_uses_creation_response_when_readback_refused(self):
        """创建路径的创建时证据：POST 201 载荷即元数据来源——创建成功但读取
        403 时，依闭集校验过的创建响应落盘绑定与元数据，仍走到 awaiting + URL。"""
        fake = PreInstallMailboxDenialFake(installation_payload={"total_count": 0, "installations": []})
        fake.mailbox_denied = True
        result = self._run_bootstrap(fake)["result"]
        self.assertEqual(result["setup_state"], "awaiting_installation")
        self.assertEqual(self.credentials.load_secret("github_mailbox_repo"), "student/Fudan-CourseLens-Mailbox")
        meta = json.loads(self.credentials.load_secret("github_managed_repo_meta"))
        self.assertEqual(meta["mailbox"]["id"], 202)
        self.assertEqual(meta["mailbox"]["owner"], "student")
        self.assertEqual(
            result["installation_setup_url"],
            "https://github.com/apps/fudan-courselens/installations/new/permissions"
            "?suggested_target_id=987654321&repository_ids[]=101&repository_ids[]=202",
        )

    def test_managed_resources_compose_preselected_setup_url_when_not_installed(self):
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/Fudan-CourseLens-Worker")
        self.credentials.save_secret("github_mailbox_repo", "student/Fudan-CourseLens-Mailbox")
        fake = ManagedRepositoryFake(installation_payload={"total_count": 0, "installations": []})
        fake.add("student/Fudan-CourseLens-Worker", private=False, repo_id=101)
        fake.add("student/Fudan-CourseLens-Mailbox", private=True, repo_id=202)
        with patch.object(self.client, "_api", side_effect=fake.api):
            resources = self.client.inspect_managed_resources()
        self.assertFalse(resources["installation"]["installed"])
        self.assertEqual(
            resources["installation_setup_url"],
            "https://github.com/apps/fudan-courselens/installations/new/permissions"
            "?suggested_target_id=987654321&repository_ids[]=101&repository_ids[]=202",
        )

    def test_managed_resources_omit_setup_url_when_installation_is_present(self):
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/Fudan-CourseLens-Worker")
        self.credentials.save_secret("github_mailbox_repo", "student/Fudan-CourseLens-Mailbox")
        fake = ManagedRepositoryFake()
        fake.add("student/Fudan-CourseLens-Worker", private=False, repo_id=101)
        fake.add("student/Fudan-CourseLens-Mailbox", private=True, repo_id=202)
        with patch.object(self.client, "_api", side_effect=fake.api):
            resources = self.client.inspect_managed_resources()
        self.assertTrue(resources["installation"]["installed"])
        self.assertEqual(resources["installation_setup_url"], "")

    def test_inspect_repo_denial_degrades_instead_of_raising(self):
        """未安装 + 仓库级 403：降级返回——identity/installation 照常、预选链接
        照常组装、闭集标记 repos_access_denied=repos_detail、被拒段如实为空。"""
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/Fudan-CourseLens-Worker")
        self.credentials.save_secret("github_mailbox_repo", "student/Fudan-CourseLens-Mailbox")
        fake = RepoDenialFake(deny_all=True, installation_payload={"total_count": 0, "installations": []})
        fake.add("student/Fudan-CourseLens-Worker", private=False, repo_id=101)
        fake.add("student/Fudan-CourseLens-Mailbox", private=True, repo_id=202)
        with patch.object(self.client, "_api", side_effect=fake.api):
            resources = self.client.inspect_managed_resources()
        self.assertEqual(resources["identity"], {"login": "student", "account_id": 987654321})
        self.assertFalse(resources["installation"]["installed"])
        self.assertEqual(
            resources["installation_setup_url"],
            "https://github.com/apps/fudan-courselens/installations/new/permissions"
            "?suggested_target_id=987654321&repository_ids[]=101&repository_ids[]=202",
        )
        self.assertEqual(resources["repos_access_denied"], "repos_detail")
        self.assertTrue(resources["worker"]["exists"], "仓库详情读取不受探测段拒绝影响")
        self.assertEqual(resources["workflows"], {})
        self.assertFalse(resources["actions_enabled"])
        self.assertFalse(resources["environment_exists"])
        self.assertEqual(resources["secret_names"], [])
        self.assertEqual(resources["variables"], {})
        self.assertEqual(resources["worker_commit"], "")
        # grant 无恙：仓库级 403 绝不丢弃已存授权
        self.assertEqual(self.credentials.load_secret("github_app_access_token"), "token")

    def test_inspect_repo_actions_denial_carries_repos_actions_class(self):
        """已安装 + 仅 /actions/ 段拒绝：首个拒绝端点类 repos_actions 进闭集标记，
        探测段在该处中止，已装路径的 setup URL 缺省行为不变。"""
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/Fudan-CourseLens-Worker")
        self.credentials.save_secret("github_mailbox_repo", "student/Fudan-CourseLens-Mailbox")
        fake = RepoDenialFake(deny_all=False)
        fake.add("student/Fudan-CourseLens-Worker", private=False, repo_id=101)
        fake.add("student/Fudan-CourseLens-Mailbox", private=True, repo_id=202)
        with patch.object(self.client, "_api", side_effect=fake.api):
            resources = self.client.inspect_managed_resources()
        self.assertEqual(resources["repos_access_denied"], "repos_actions")
        self.assertTrue(resources["installation"]["installed"])
        self.assertEqual(resources["installation_setup_url"], "")
        self.assertEqual(resources["worker_commit"], "c" * 40, "commits 读取未被拒时照常返回")
        self.assertEqual(resources["workflows"], {})
        self.assertFalse(resources["environment_exists"])

    def test_inspect_non_permission_repo_errors_still_propagate(self):
        """非 permission_denied 的仓库级错误（离线/限流）仍向上抛：降级只收窄到 403。"""
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/Fudan-CourseLens-Worker")
        self.credentials.save_secret("github_mailbox_repo", "student/Fudan-CourseLens-Mailbox")
        fake = ManagedRepositoryFake()
        fake.add("student/Fudan-CourseLens-Worker", private=False, repo_id=101)
        fake.add("student/Fudan-CourseLens-Mailbox", private=True, repo_id=202)
        fake.fail_once["/repos/student/Fudan-CourseLens-Worker/commits/main"] = GitHubAppError(
            "GitHub 连接失败：ConnectionError", code="github_unreachable",
        )
        with patch.object(self.client, "_api", side_effect=fake.api):
            with self.assertRaises(GitHubAppError) as raised:
                self.client.inspect_managed_resources()
        self.assertEqual(raised.exception.code, "github_unreachable")

    def test_inspect_pre_install_mailbox_denial_degrades_to_awaiting_installation(self):
        """D2/probe：预安装 403 的私有 mailbox 降级为「已建待安装」——不抛、
        闭集标记 pre_install、id 来自保存元数据；预选 setup URL 依创建时证据
        照常组装；worker 公开可读路径零变化。"""
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/Fudan-CourseLens-Worker")
        self.credentials.save_secret("github_mailbox_repo", "student/Fudan-CourseLens-Mailbox")
        self.credentials.save_secret("github_managed_repo_meta", json.dumps({
            "worker": {"id": 101, "full_name": "student/Fudan-CourseLens-Worker",
                       "owner": "student", "private": False, "managed": True},
            "mailbox": {"id": 202, "full_name": "student/Fudan-CourseLens-Mailbox",
                        "owner": "student", "private": True, "managed": True},
        }, sort_keys=True, separators=(",", ":")))
        fake = PreInstallMailboxDenialFake(installation_payload={"total_count": 0, "installations": []})
        fake.mailbox_denied = True
        fake.add("student/Fudan-CourseLens-Worker", private=False, repo_id=101)
        fake.add("student/Fudan-CourseLens-Mailbox", private=True, repo_id=202)
        with patch.object(self.client, "_api", side_effect=fake.api):
            resources = self.client.inspect_managed_resources()
        self.assertEqual(resources["mailbox"], {
            "id": 202, "full_name": "student/Fudan-CourseLens-Mailbox",
            "owner": "student", "private": True, "managed": True,
            "exists": True, "pre_install": True,
        })
        self.assertTrue(resources["worker"]["exists"])
        self.assertNotIn("pre_install", resources["worker"])
        self.assertFalse(resources["installation"]["installed"])
        self.assertEqual(
            resources["installation_setup_url"],
            "https://github.com/apps/fudan-courselens/installations/new/permissions"
            "?suggested_target_id=987654321&repository_ids[]=101&repository_ids[]=202",
        )
        # grant 无恙：预安装 403 绝不丢弃已存授权
        self.assertEqual(self.credentials.load_secret("github_app_access_token"), "token")

    def test_inspect_mailbox_denial_without_creation_evidence_still_raises(self):
        """probe 降级同样只信创建时证据：403 + 无保存元数据保持 fail-closed。"""
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/Fudan-CourseLens-Worker")
        self.credentials.save_secret("github_mailbox_repo", "student/Fudan-CourseLens-Mailbox")
        fake = PreInstallMailboxDenialFake(installation_payload={"total_count": 0, "installations": []})
        fake.mailbox_denied = True
        fake.add("student/Fudan-CourseLens-Worker", private=False, repo_id=101)
        fake.add("student/Fudan-CourseLens-Mailbox", private=True, repo_id=202)
        with patch.object(self.client, "_api", side_effect=fake.api):
            with self.assertRaises(GitHubAppError) as raised:
                self.client.inspect_managed_resources()
        self.assertEqual(raised.exception.code, "permission_denied")

    def test_installation_setup_url_uses_saved_meta_when_fresh_read_is_refused(self):
        """_installation_setup_url 的保存元数据第二来源（属主校验通过）：新鲜
        读取缺 id 时依创建即存元数据补齐，预选链接从建仓起即可组装。"""
        self.credentials.save_secret("github_managed_repo_meta", json.dumps({
            "worker": {"id": 101, "full_name": "student/Fudan-CourseLens-Worker",
                       "owner": "student", "private": False, "managed": True},
            "mailbox": {"id": 202, "full_name": "student/Fudan-CourseLens-Mailbox",
                        "owner": "student", "private": True, "managed": True},
        }, sort_keys=True, separators=(",", ":")))
        url = self.client._installation_setup_url(
            "student", 987654321,
            {"exists": True, "full_name": "student/Fudan-CourseLens-Worker",
             "owner": "student", "private": False, "managed": True, "id": 101},
            {"exists": True, "pre_install": True,
             "full_name": "student/Fudan-CourseLens-Mailbox",
             "owner": "student", "private": True, "managed": True},
        )
        self.assertEqual(
            url,
            "https://github.com/apps/fudan-courselens/installations/new/permissions"
            "?suggested_target_id=987654321&repository_ids[]=101&repository_ids[]=202",
        )

    def test_installation_setup_url_ignores_saved_meta_of_foreign_owner(self):
        """属主校验：异主保存元数据绝不参与补齐——两仓 id 不齐时链接为空。"""
        self.credentials.save_secret("github_managed_repo_meta", json.dumps({
            "worker": {"id": 101, "full_name": "former-account/Fudan-CourseLens-Worker",
                       "owner": "former-account", "private": False, "managed": True},
            "mailbox": {"id": 202, "full_name": "student/Fudan-CourseLens-Mailbox",
                        "owner": "student", "private": True, "managed": True},
        }, sort_keys=True, separators=(",", ":")))
        url = self.client._installation_setup_url(
            "student", 987654321,
            {"exists": True, "full_name": "student/Fudan-CourseLens-Worker",
             "owner": "student", "private": False, "managed": True},
            {"exists": True, "full_name": "student/Fudan-CourseLens-Mailbox",
             "owner": "student", "private": True, "managed": True, "id": 202},
        )
        self.assertEqual(url, "", "异主元数据不得为当前账号合成预选链接")

    def test_delete_job_token_skips_remote_call_before_installation(self):
        """创建即存后未安装态：从未签发任务令牌（无 installation id、无本地
        镜像、无清理挂起）时，delete_job_token 零远端调用——安装前用户令牌
        触达 secrets 端点只会 403，绝不能反断 disconnect。"""
        self.credentials.save_secret("github_worker_repo", "student/Fudan-CourseLens-Worker")
        calls = []
        with patch.object(
            self.client, "_api",
            side_effect=lambda *args, **kwargs: calls.append(args) or JsonResponse({}),
        ):
            self.client.delete_job_token()
        self.assertEqual(calls, [])
        # 安装已在案（既有语义）：仍照常发起远端清理
        self.credentials.save_secret("github_app_installation_id", "42")
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        with patch.object(
            self.client, "_api",
            side_effect=lambda *args, **kwargs: calls.append(args) or JsonResponse({}),
        ):
            self.client.delete_job_token()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "DELETE")

    def test_disconnect_forgets_managed_repo_meta_with_bindings(self):
        self.credentials.save_secret("github_worker_repo", "student/Fudan-CourseLens-Worker")
        self.credentials.save_secret("github_managed_repo_meta", "{}")
        self.client.disconnect()
        self.assertFalse(self.credentials.has_secret("github_managed_repo_meta"))
        self.assertFalse(self.credentials.has_secret("github_worker_repo"))

    def test_inspect_ignores_cross_account_stored_bindings_without_repo_calls(self):
        """读侧属主校验（与 bootstrap _ensure_managed_repository 同规则）：
        旧账号存量绑定按不存在处理——exists=False + 闭集标记
        binding_owner_mismatch，绝不以旧仓库名+新令牌打任何仓库级端点
        （跨账号陈旧绑定=首连 403 冻死事故根因）；mismatch 先短路， Prompt 4
        的 repos_access_denied 降级标记不得出现。"""
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "former-account/Fudan-CourseLens-Worker")
        self.credentials.save_secret("github_mailbox_repo", "former-account/Fudan-CourseLens-Mailbox")
        fake = ForeignBindingDenialFake(installation_payload={"total_count": 0, "installations": []})
        with patch.object(self.client, "_api", side_effect=fake.api):
            resources = self.client.inspect_managed_resources()
        self.assertEqual(resources["worker"], {"exists": False, "binding_owner_mismatch": True})
        self.assertEqual(resources["mailbox"], {"exists": False, "binding_owner_mismatch": True})
        self.assertEqual(resources["repos_access_denied"], "", "mismatch 先短路：降级标记不得出现")
        self.assertEqual(resources["worker_commit"], "")
        self.assertEqual(resources["workflows"], {})
        self.assertFalse(resources["actions_enabled"])
        self.assertEqual(resources["installation_setup_url"], "", "仓库未就绪绝不输出预选安装链接")
        self.assertFalse(
            any("former-account" in path for _, path, _ in fake.created),
            "旧账号仓库名绝不与当前令牌组合成任何 API 调用",
        )
        repo_level = [path for _, path, _ in fake.created if path.startswith("/repos/")]
        self.assertEqual(repo_level, [], "mismatch 时零仓库级调用")

    def test_inspect_binding_owner_mismatch_is_per_repository(self):
        """worker/mailbox 各自独立校验：仅 worker 绑定跨账号时，mailbox 照常
        读取且不带标记（属主匹配路径零新键、行为零变化），worker 按不存在
        处理且旧名零调用。"""
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "former-account/Fudan-CourseLens-Worker")
        self.credentials.save_secret("github_mailbox_repo", "student/Fudan-CourseLens-Mailbox")
        fake = ManagedRepositoryFake(installation_payload={"total_count": 0, "installations": []})
        fake.add("student/Fudan-CourseLens-Worker", private=False, repo_id=101)
        fake.add("student/Fudan-CourseLens-Mailbox", private=True, repo_id=202)
        with patch.object(self.client, "_api", side_effect=fake.api):
            resources = self.client.inspect_managed_resources()
        self.assertEqual(resources["worker"], {"exists": False, "binding_owner_mismatch": True})
        self.assertTrue(resources["mailbox"]["exists"], "同账号绑定照常读取")
        self.assertEqual(resources["mailbox"]["owner"], "student")
        self.assertNotIn("binding_owner_mismatch", resources["mailbox"], "属主匹配路径零新键")
        self.assertFalse(
            any("former-account" in path for _, path, _ in fake.created),
            "被忽略的 worker 绑定零仓库级调用",
        )
        # 属主匹配的 mailbox 照常参与预选链接组装口径：worker 缺席 → 链接为空
        self.assertEqual(resources["installation_setup_url"], "")

    def test_probe_carries_binding_owner_mismatch_marker_on_repository_cards(self):
        """probe 分组：mismatch 标记进 worker 卡 evidence（闭集布尔），引导走
        「仓库未建」bootstrap；无标记的 mailbox 卡 evidence 保持空——按构造
        零受扰。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            app = BindingMismatchProbeApp()
            supervisor = RemoteConnectionSupervisor(store, self.credentials, lambda: app)
            supervisor.probe()
            worker = store.get_remote_observation("worker_repository")
            self.assertEqual(worker["state"], "action_required")
            self.assertEqual(worker["code"], "worker_repository_missing")
            self.assertEqual(worker["actions"], ["bootstrap"])
            self.assertEqual(worker["evidence"], {"binding_owner_mismatch": True})
            mailbox = store.get_remote_observation("mailbox_repository")
            self.assertEqual(mailbox["code"], "mailbox_repository_missing")
            self.assertEqual(mailbox["actions"], ["bootstrap"])
            self.assertEqual(mailbox["evidence"], {}, "无标记卡片 evidence 不含该键")
            store.close()

    def test_probe_on_repos_denial_records_ready_authorization_and_guided_installation(self):
        """probe 分组：仓库级 403 降级后——authorization 记 ready（grant 无恙，
        login 进 evidence）、installation 记 action_required（受信 setup URL 与
        闭集 repos_denied 端点类进 evidence、动作 bootstrap）；未安装期间的
        仓库 403 属安装缺失的必然后果（T6 深分类）：下游五组件不再记
        repos_access_denied 独立故障行，由前端安装指引统一解释。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            app = RepoDenialProbeApp()
            supervisor = RemoteConnectionSupervisor(store, self.credentials, lambda: app)
            snapshot = supervisor.probe()
            authorization = store.get_remote_observation("authorization")
            self.assertEqual(authorization["state"], "ready")
            self.assertEqual(authorization["code"], "authorization_valid")
            self.assertEqual(authorization["evidence"]["login"], "student")
            installation = store.get_remote_observation("installation")
            self.assertEqual(installation["state"], "action_required")
            self.assertEqual(installation["code"], "installation_missing")
            self.assertEqual(installation["actions"], ["bootstrap"])
            self.assertEqual(
                installation["evidence"]["installation_setup_url"],
                "https://github.com/apps/fudan-courselens/installations/new/permissions"
                "?suggested_target_id=987654321&repository_ids[]=101&repository_ids[]=202",
            )
            self.assertEqual(installation["evidence"]["repos_denied"], "repos_detail")
            for component in ("worker_integrity", "workflow", "actions", "environment", "job_token"):
                observation = store.get_remote_observation(component)
                self.assertIsNone(
                    observation,
                    component + "：未安装期间的仓库 403 不得记为独立范围/权限故障（T6）",
                )
            self.assertNotEqual(
                store.get_remote_observation("authorization")["code"], "permission_denied",
                "仓库级 403 绝不再写成 authorization 的 permission_denied",
            )
            self.assertEqual(snapshot["overall"]["state"], "action_required")
            self.assertEqual(snapshot["overall"]["code"], "installation_missing")
            store.close()

    def test_probe_records_awaiting_installation_for_created_unverifiable_mailbox(self):
        """probe 分组（创建即存后的首跑中间态）：mailbox 降级证据记「已建待安装」
        而非 invalid/missing、动作 bootstrap；worker ready；installation 记
        action_required 且 evidence 带预选 setup URL；快照不再把已建仓库卡覆盖成
        worker_setup_incomplete；overall 停在 installation_missing。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            app = PreInstallAwaitingProbeApp()
            supervisor = RemoteConnectionSupervisor(store, self.credentials, lambda: app)
            snapshot = supervisor.probe()
            mailbox = store.get_remote_observation("mailbox_repository")
            self.assertEqual(mailbox["state"], "action_required")
            self.assertEqual(mailbox["code"], "mailbox_repository_awaiting_installation")
            self.assertEqual(mailbox["actions"], ["bootstrap"])
            self.assertEqual(mailbox["evidence"], {})
            worker = store.get_remote_observation("worker_repository")
            self.assertEqual(worker["state"], "ready")
            self.assertEqual(worker["code"], "worker_repository_ready")
            installation = store.get_remote_observation("installation")
            self.assertEqual(installation["state"], "action_required")
            self.assertEqual(installation["code"], "installation_missing")
            self.assertEqual(
                installation["evidence"]["installation_setup_url"],
                "https://github.com/apps/fudan-courselens/installations/new/permissions"
                "?suggested_target_id=987654321&repository_ids[]=101&repository_ids[]=202",
            )
            self.assertEqual(
                store.get_remote_observation("mailbox_history")["code"],
                "mailbox_repository_unavailable",
                "mailbox 未验证前历史段如实不可用",
            )
            by_name = {item["component"]: item for item in snapshot["components"]}
            self.assertEqual(
                by_name["worker_repository"]["state"], "ready",
                "创建即存后快照保留仓库卡实时证据，不再覆盖 worker_setup_incomplete",
            )
            self.assertEqual(
                by_name["mailbox_repository"]["code"], "mailbox_repository_awaiting_installation",
            )
            self.assertEqual(
                snapshot["configured_repositories"]["mailbox"], "student/Fudan-CourseLens-Mailbox",
            )
            self.assertEqual(snapshot["overall"]["state"], "action_required")
            self.assertEqual(snapshot["overall"]["code"], "installation_missing")
            store.close()

    def test_overall_blocking_prefers_bootstrap_pending_over_channel_test(self):
        """D-20261009-11（REMOTE-E2E-1-R4 from-zero 收紧安装范围后续跑死锁）：
        授权在案、bootstrap 未完成、channel_test 需要（收紧后真实并存形态）——
        overall.code 必须落 worker_setup_incomplete（前端 MF-7 自动续跑守卫恰等
        该码），不得被 channel_test_required 压过（修前=真实学生卡死无路，
        R4 实证单动作即愈=能力在、编排断）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            app = ProbeErrorFakeApp()
            supervisor = RemoteConnectionSupervisor(store, self.credentials, lambda: app)
            now = time.time()
            store.upsert_remote_observation(
                "channel_test", state="action_required", source="local",
                code="channel_test_required", actions=["test-channel"],
                observed_at=now, expires_at=now + 90,
            )
            snapshot = supervisor._snapshot_uncached()
            self.assertEqual(
                snapshot["overall"]["code"], "worker_setup_incomplete",
                "bootstrap 待续跑（fresh 本地推导）必须压过 channel_test_required（MF-7 守卫恰等）",
            )
            self.assertEqual(snapshot["overall"]["state"], "unknown")
            self.assertIn("bootstrap", snapshot["allowed_actions"])
            store.close()

    def test_snapshot_exposes_install_url_but_no_token(self):
        self.credentials.save_secret("github_app_access_token", "secret-token")
        snapshot = self.client.snapshot()
        self.assertTrue(snapshot["app_configured"])
        self.assertEqual(snapshot["installation_url"], "https://github.com/apps/fudan-courselens/installations/new")
        self.assertNotIn("secret-token", repr(snapshot))

    def test_reauthorization_keeps_worker_repositories_and_crypto_identity(self):
        for name, value in {
            "github_app_access_token": "access",
            "github_app_access_expires_at": "1",
            "github_app_refresh_token": "refresh",
            "github_app_refresh_expires_at": "2",
            "github_app_installation_id": "42",
            "github_remote_token": "remote",
            "github_worker_repo": "student/Fudan-CourseLens-Worker",
            "github_mailbox_repo": "student/Fudan-CourseLens-Mailbox",
            "github_managed_repo_meta": '{"mailbox": {"id": 202}}',
            "worker_box_public_key": "box-key",
            "worker_signing_public_key": "signing-key",
        }.items():
            self.credentials.save_secret(name, value)

        self.client.clear_user_authorization()

        self.assertFalse(self.credentials.has_secret("github_app_access_token"))
        self.assertFalse(self.credentials.has_secret("github_app_refresh_token"))
        self.assertFalse(self.credentials.has_secret("github_app_installation_id"))
        self.assertEqual(self.credentials.load_secret("github_worker_repo"), "student/Fudan-CourseLens-Worker")
        self.assertEqual(self.credentials.load_secret("github_mailbox_repo"), "student/Fudan-CourseLens-Mailbox")
        self.assertEqual(
            self.credentials.load_secret("github_managed_repo_meta"),
            '{"mailbox": {"id": 202}}',
            "创建时元数据与绑定同族：重授权保留，断开连接才清除",
        )
        self.assertEqual(self.credentials.load_secret("worker_box_public_key"), "box-key")
        self.assertEqual(self.credentials.load_secret("worker_signing_public_key"), "signing-key")

    def test_manifest_config_persists_only_public_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp) / "runtime-assets.json"
            assets.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
            payload = {
                "client_id": "Iv1.public",
                "slug": "fudan-courselens",
                "pem": "PRIVATE-KEY-SENTINEL",
                "client_secret": "CLIENT-SECRET-SENTINEL",
                "webhook_secret": "WEBHOOK-SECRET-SENTINEL",
            }
            with patch.object(register_github_app, "RUNTIME_ASSETS", assets):
                register_github_app._write_public_config(payload)
            saved = assets.read_text(encoding="utf-8")
            self.assertIn("Iv1.public", saved)
            self.assertIn("fudan-courselens", saved)
            self.assertNotIn("SENTINEL", saved)

    def test_manifest_requests_only_required_repository_permissions(self):
        manifest = register_github_app._manifest("http://127.0.0.1:8765/callback")
        self.assertEqual(manifest["default_events"], [])
        self.assertFalse(manifest["hook_attributes"]["active"])
        self.assertEqual(manifest["default_permissions"], {
            "actions": "write",
            "actions_variables": "write",
            "administration": "write",
            "contents": "write",
            "environments": "write",
            "issues": "write",
            # COMPUTE-GUARD-RETENTION-1：Plan/billing 权限已移除；
            # 仓库权限、安装范围与事件订阅保持原闭集不变。
            "workflows": "write",
        })
        self.assertEqual(
            sorted(set(manifest["default_permissions"])),
            sorted({"actions", "actions_variables", "administration", "contents",
                    "environments", "issues", "workflows"}),
            "不得新增或扩大任何权限",
        )

    def test_echo_verifier_holds_token_lease_before_loading_remote_settings(self):
        source = (Path(__file__).resolve().parents[1] / "scripts" / "verify_github_echo.py").read_text(encoding="utf-8")
        lease = source.index("with github_app.job_token_lease():")
        settings = source.index("RemoteSettings.load(credentials)", lease)
        execute = source.index("coordinator.execute(", settings)
        self.assertLess(lease, settings)
        self.assertLess(settings, execute)

    def test_remote_coordinator_is_prepared_after_token_lease_acquisition(self):
        events = []
        coordinator = object()
        service = CourseLensApplication.__new__(CourseLensApplication)
        service.task_store = object()

        class App:
            @contextmanager
            def job_token_lease(self, *, task_id, task_store):
                events.append(("enter", task_id, task_store))
                try:
                    yield
                finally:
                    events.append(("exit", task_id, task_store))

        service.github_app = App()
        service._prepare_remote_coordinator = Mock(
            side_effect=lambda: events.append(("prepare",)) or coordinator
        )

        with service._leased_remote_coordinator("task-12345678") as value:
            self.assertIs(value, coordinator)
            events.append(("execute",))

        self.assertEqual(
            [item[0] for item in events],
            ["enter", "prepare", "execute", "exit"],
        )

    def test_echo_verifier_does_not_invent_unknown_progress(self):
        from scripts.verify_github_echo import render_progress

        line = render_progress("remote_queue", None, "confirming run")
        self.assertIn("progress=unknown", line)
        self.assertNotIn("%", line)

    def test_repair_worker_rebuilds_exact_pinned_tree_before_advancing_main(self):
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/Fudan-CourseLens-Worker")
        old_commit = "1" * 40
        expected_commit = "2" * 40
        expected_tree = "3" * 40
        blob_sha = "4" * 40
        repaired_commit = "5" * 40
        calls = []

        def api(method, path, **kwargs):
            calls.append((method, path, kwargs.get("json")))
            if path == "/user":
                return JsonResponse({"login": "student"})
            if path == "/repos/student/Fudan-CourseLens-Worker":
                return JsonResponse({
                    "owner": {"login": "student"},
                    "private": False,
                    "description": "Managed by Fudan CourseLens desktop client",
                    "default_branch": "main",
                })
            if path.endswith("/commits/main"):
                count = sum(1 for item in calls if item[1].endswith("/commits/main"))
                tree = expected_tree if count > 1 else "6" * 40
                commit = repaired_commit if count > 1 else old_commit
                return JsonResponse({"sha": commit, "commit": {"tree": {"sha": tree}}})
            if "/git/trees/" in path and method == "GET":
                return JsonResponse({
                    "truncated": False,
                    "tree": [{"path": "worker.py", "mode": "100644", "type": "blob", "sha": blob_sha, "size": 12}],
                })
            if "/git/blobs/" in path and method == "GET":
                return JsonResponse({"sha": blob_sha, "encoding": "base64", "content": "aGVsbG8="})
            if path.endswith("/git/blobs"):
                return JsonResponse({"sha": blob_sha}, 201)
            if path.endswith("/git/trees"):
                return JsonResponse({"sha": expected_tree}, 201)
            if path.endswith("/git/commits"):
                return JsonResponse({"sha": repaired_commit}, 201)
            if path.endswith("/git/refs/heads/main"):
                return JsonResponse({}, 200)
            raise AssertionError((method, path))

        with (
            patch("src.remote.github_app._bundled_worker_config", return_value={
                "mode": "signed-mirror",
                "repository": "gualtier-xu/Fudan-CourseLens-Worker",
                "commit": expected_commit,
                "tree": expected_tree,
                "manifest_sha256": "d" * 64,
            }),
            patch.object(self.client, "_validated_worker_release", return_value={
                "mode": "signed-mirror",
                "repository": "gualtier-xu/Fudan-CourseLens-Worker",
                "commit": expected_commit,
                "tree": expected_tree,
                "manifest_sha256": "d" * 64,
                "manifest_verified": True,
            }),
            patch.object(self.client, "_api", side_effect=api),
        ):
            result = self.client.repair_worker()

        self.assertTrue(result["trusted"])
        self.assertTrue(result["repaired"])
        ref_call = next(item for item in calls if item[1].endswith("/git/refs/heads/main"))
        self.assertEqual(ref_call[2], {"sha": repaired_commit, "force": False})
        commit_call = next(item for item in calls if item[1].endswith("/git/commits"))
        self.assertEqual(commit_call[2]["parents"], [old_commit])

    def test_repair_worker_refuses_unmanaged_repository(self):
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/Fudan-CourseLens-Worker")

        def api(_method, path, **_kwargs):
            if path == "/user":
                return JsonResponse({"login": "student"})
            return JsonResponse({
                "owner": {"login": "student"},
                "private": False,
                "description": "user repository",
                "default_branch": "main",
            })

        with (
            patch.object(self.client, "_validated_worker_release", return_value={
                "mode": "signed-mirror",
                "repository": "gualtier-xu/Fudan-CourseLens-Worker",
                "commit": "2" * 40,
                "tree": "3" * 40,
                "manifest_verified": True,
            }),
            patch.object(self.client, "_api", side_effect=api),
        ):
            with self.assertRaisesRegex(RuntimeError, "拒绝修复"):
                self.client.repair_worker()

    def test_template_repository_integrity_fails_closed_without_github_calls(self):
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "gualtier-xu/Fudan-CourseLens-Worker")
        self.credentials.save_secret("github_worker_dispatch_sha", "f" * 40)
        with (
            patch("src.remote.github_app._bundled_worker_config", return_value={
                "mode": "signed-mirror",
                "repository": "gualtier-xu/Fudan-CourseLens-Worker",
                "commit": "a" * 40,
                "tree": "b" * 40,
                "manifest_sha256": "d" * 64,
            }),
            patch.object(self.client, "_api") as api,
        ):
            with self.assertRaises(GitHubAppError) as raised:
                self.client.check_worker_integrity()
        self.assertEqual(raised.exception.code, "personal_worker_migration_required")
        api.assert_not_called()
        self.assertTrue(self.credentials.has_secret("github_worker_dispatch_sha"))

    def test_template_repair_never_touches_template_main(self):
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "gualtier-xu/Fudan-CourseLens-Worker")
        with (
            patch("src.remote.github_app._bundled_worker_config", return_value={
                "mode": "signed-mirror",
                "repository": "gualtier-xu/Fudan-CourseLens-Worker",
                "commit": "a" * 40,
                "tree": "b" * 40,
                "manifest_sha256": "d" * 64,
            }),
            patch.object(self.client, "_api") as api,
        ):
            with self.assertRaises(GitHubAppError) as raised:
                self.client.repair_worker()
        self.assertEqual(raised.exception.code, "personal_worker_migration_required")
        api.assert_not_called()

    def test_authorization_time_installation_check_skips_repository_enumeration(self):
        def api(_method, path, **_kwargs):
            if path == "/user/installations":
                return JsonResponse({"total_count": 1, "installations": [{
                    "id": 42, "app_slug": "fudan-courselens", "target_type": "User",
                    "account": {"login": "student", "type": "User"},
                    "permissions": {"actions": "write"},
                }]})
            raise AssertionError(f"unexpected API path at authorization time: {path}")

        with patch.object(self.client, "_api", side_effect=api):
            installation = self.client._inspect_fresh_user_installation(
                "student", token="token", require_exact_repository_selection=False
            )
        self.assertEqual(installation["id"], 42)

    def test_exact_binding_requires_only_worker_and_mailbox_selected(self):
        base = {
            "id": 42, "app_slug": "fudan-courselens", "target_type": "User",
            "account": {"login": "student", "type": "User"},
            "permissions": {"actions": "write"},
        }

        def api(_method, path, **_kwargs):
            if path == "/user/installations":
                return JsonResponse({"total_count": 1, "installations": [dict(base)]})
            if path == "/user/installations/42/repositories":
                return JsonResponse({"total_count": 2, "repositories": [
                    {"name": "Fudan-CourseLens-Worker", "owner": {"login": "student"}},
                    {"name": "Fudan-CourseLens-Mailbox", "owner": {"login": "student"}},
                ]})
            raise AssertionError(path)

        with patch.object(self.client, "_api", side_effect=api):
            installation = self.client._inspect_fresh_user_installation(
                "student", token="token", require_exact_repository_selection=True
            )
        self.assertEqual(installation["id"], 42)

        def missing_mailbox(_method, path, **_kwargs):
            if path == "/user/installations":
                return JsonResponse({"total_count": 1, "installations": [dict(base)]})
            if path == "/user/installations/42/repositories":
                return JsonResponse({"total_count": 2, "repositories": [
                    {"name": "Fudan-CourseLens-Worker", "owner": {"login": "student"}},
                    {"name": "unrelated-repository", "owner": {"login": "student"}},
                ]})
            raise AssertionError(path)

        with patch.object(self.client, "_api", side_effect=missing_mailbox):
            with self.assertRaises(GitHubAppError) as raised:
                self.client._inspect_fresh_user_installation(
                    "student", token="token", require_exact_repository_selection=True
                )
        self.assertEqual(raised.exception.code, "installation_scope_not_exact")

    def test_managed_resources_report_installation_repository_selection_evidence(self):
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))

        def api(_method, path, **_kwargs):
            if path == "/user":
                return JsonResponse({"login": "student", "id": 7})
            if path == "/user/installations":
                return JsonResponse({"installations": [{
                    "id": 42, "app_slug": "fudan-courselens", "target_type": "User",
                    "account": {"login": "student"},
                }]})
            if path == "/user/installations/42/repositories":
                return JsonResponse({"total_count": 2, "repositories": [
                    {"name": "Fudan-CourseLens-Worker", "full_name": "student/Fudan-CourseLens-Worker"},
                    {"name": "unrelated-repository", "full_name": "student/unrelated-repository"},
                ]})
            raise AssertionError(path)

        with patch.object(self.client, "_api", side_effect=api):
            resources = self.client.inspect_managed_resources()
        installation = dict(resources["installation"])
        self.assertTrue(installation["installed"])
        self.assertEqual(installation["installation_id"], 42)
        self.assertFalse(installation["repository_selection_exact"])
        self.assertEqual(
            installation["missing_installation_repositories"],
            ["fudan-courselens-mailbox"],
        )
        self.assertEqual(
            installation["unexpected_installation_repositories"],
            ["student/unrelated-repository"],
        )
        self.assertEqual(resources["worker_dispatch_mode"], "personal-worker")
        self.assertEqual(
            set(installation),
            {
                "installed", "installation_id", "repository_selection_exact",
                "missing_installation_repositories", "unexpected_installation_repositories",
            },
        )

    def test_installation_selection_evidence_fails_soft_on_enumeration_error(self):
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))

        def api(_method, path, **_kwargs):
            if path == "/user":
                return JsonResponse({"login": "student", "id": 7})
            if path == "/user/installations":
                return JsonResponse({"installations": [{
                    "id": 42, "app_slug": "fudan-courselens", "target_type": "User",
                    "account": {"login": "student"},
                }]})
            if path == "/user/installations/42/repositories":
                raise GitHubAppError("synthetic inventory failure", code="permission_denied")
            raise AssertionError(path)

        with patch.object(self.client, "_api", side_effect=api):
            resources = self.client.inspect_managed_resources()
        installation = dict(resources["installation"])
        self.assertTrue(installation["installed"])
        self.assertFalse(installation["repository_selection_exact"])
        self.assertEqual(
            installation["repository_selection_error"],
            "installation_repository_inventory_unavailable",
        )
        self.assertEqual(installation["missing_installation_repositories"], [])
        self.assertEqual(installation["unexpected_installation_repositories"], [])

    def test_personal_worker_remains_compatible_with_tree_equivalent_commit(self):
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/Fudan-CourseLens-Worker")
        self.credentials.save_secret("github_worker_dispatch_sha", "f" * 40)
        release = {
            "mode": "signed-mirror",
            "repository": "gualtier-xu/Fudan-CourseLens-Worker",
            "commit": "2" * 40,
            "tree": "3" * 40,
            "manifest_sha256": "d" * 64,
            "trust_epoch": 7,
            "manifest_verified": True,
        }
        with (
            patch("src.remote.github_app._bundled_worker_config", return_value=release),
            patch.object(self.client, "_validated_worker_release", return_value=release),
            patch.object(self.client, "_api", return_value=JsonResponse({
                "sha": "4" * 40,
                "commit": {"tree": {"sha": release["tree"]}},
            })),
        ):
            result = self.client.check_worker_integrity()
        self.assertTrue(result["trusted"])
        self.assertEqual(result["dispatch_mode"], "personal-worker")
        self.assertEqual(result["dispatch_head_sha"], "")
        self.assertEqual(result["actual_commit"], "4" * 40)
        self.assertFalse(self.credentials.has_secret("github_worker_dispatch_sha"))

    def test_job_token_lease_syncs_once_and_deletes_after_last_user(self):
        events = []
        with (
            patch.object(self.client, "check_worker_integrity", return_value={"trusted": True}),
            patch.object(self.client, "sync_job_token", side_effect=lambda: events.append("sync") or "token"),
            patch.object(self.client, "delete_job_token", side_effect=lambda: events.append("delete")),
        ):
            self.assertEqual(self.client.acquire_job_token(), 1)
            self.assertEqual(self.client.acquire_job_token(), 2)
            self.assertEqual(self.client.release_job_token(), 1)
            self.assertEqual(events, ["sync"])
            self.assertEqual(self.client.release_job_token(), 0)
        self.assertEqual(events, ["sync", "delete"])

    def test_job_token_cleanup_failure_is_redacted_and_retried_later(self):
        with (
            patch.object(self.client, "check_worker_integrity", return_value={"trusted": True}),
            patch.object(self.client, "sync_job_token", return_value="token"),
            patch.object(self.client, "delete_job_token", side_effect=RuntimeError("secret response")),
        ):
            self.client.acquire_job_token()
            self.assertEqual(self.client.release_job_token(), 0)
        self.assertTrue(self.credentials.has_secret("github_job_token_cleanup_pending"))
        self.assertTrue(self.client.snapshot()["job_token_cleanup_pending"])

    def test_job_token_is_blocked_when_worker_tree_drifted(self):
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/Fudan-CourseLens-Worker")
        with (
            patch("src.remote.github_app._bundled_worker_config", return_value={
                "repository": "gualtier-xu/Fudan-CourseLens-Worker",
                "commit": "a" * 40,
                "tree": "b" * 40,
            }),
            patch.object(self.client, "_api", return_value=JsonResponse({
                "sha": "c" * 40,
                "commit": {"tree": {"sha": "d" * 40}},
            })),
            patch.object(self.client, "sync_job_token") as sync,
        ):
            with self.assertRaises(GitHubAppError) as caught:
                self.client.acquire_job_token()
        self.assertEqual(caught.exception.code, "worker_public_tree_drift")
        sync.assert_not_called()
        self.assertFalse(self.credentials.has_secret("github_worker_verified_tree"))

    def test_dispatch_gate_auto_repairs_mismatch_and_continues(self):
        """P59-U3(a)：派发门判得版本不一致时自动修复恰一次，成功后派发继续、用户无感。"""
        with (
            patch.object(self.client, "check_worker_integrity", return_value={"trusted": False}),
            patch.object(
                self.client, "repair_worker", return_value={"trusted": True, "repaired": True}
            ) as repair,
            patch.object(self.client, "sync_job_token") as sync,
            patch.object(self.client, "delete_job_token"),
        ):
            self.assertEqual(self.client.acquire_job_token(), 1)
            self.assertEqual(self.client.release_job_token(), 0)
        repair.assert_called_once()
        sync.assert_called_once()

    def test_dispatch_gate_keeps_closed_set_error_when_auto_repair_fails(self):
        """P59-U3(b)：自动修复失败时派发门既有闭集码与文案逐字不变，手动入口兜底。"""
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/worker")
        with (
            patch.object(self.client, "check_worker_integrity", return_value={"trusted": False}),
            patch.object(
                self.client,
                "repair_worker",
                side_effect=GitHubAppError("拒绝修复", code="personal_worker_migration_required"),
            ),
            patch.object(self.client, "sync_job_token") as sync,
        ):
            with self.assertRaises(GitHubAppError) as caught:
                self.client.acquire_job_token()
        self.assertEqual(caught.exception.code, "worker_tree_drifted")
        self.assertEqual(str(caught.exception), "专属 Worker 已被修改或版本过旧，已阻止发送媒体授权")
        sync.assert_not_called()

    def test_worker_auto_repair_failure_is_consumed_once_per_process(self):
        """P59-U3(c)：失败同样消耗额度——重入派发绝不重复自动修复。"""
        with (
            patch.object(self.client, "check_worker_integrity", return_value={"trusted": False}),
            patch.object(
                self.client,
                "repair_worker",
                side_effect=GitHubAppError("unreachable", code="github_error"),
            ) as repair,
            patch.object(self.client, "sync_job_token"),
        ):
            with self.assertRaises(GitHubAppError):
                self.client.acquire_job_token()
            second = self.client.ensure_worker_trusted()
            self.assertFalse(second["trusted"])
        repair.assert_called_once()

    def test_worker_auto_repair_success_is_also_once_per_process(self):
        """P59-U3(c)：成功同样记账——同进程同目标不二次修复，二次核查仍不 trusted 即如实返回。"""
        with (
            patch.object(self.client, "check_worker_integrity", return_value={"trusted": False}),
            patch.object(
                self.client, "repair_worker", return_value={"trusted": True, "repaired": True}
            ) as repair,
        ):
            first = self.client.ensure_worker_trusted()
            second = self.client.ensure_worker_trusted()
        self.assertTrue(first["trusted"])
        self.assertFalse(second["trusted"])
        repair.assert_called_once()

    def test_worker_auto_repair_refuses_template_repository_without_remote_repair(self):
        """P59-U3(d)：目标是公共模板本体时既有迁移 guard 原样上抛，绝不进入修复链。"""
        self.credentials.save_secret("github_worker_repo", "gualtier-xu/Fudan-CourseLens-Worker")
        with (
            patch("src.remote.github_app._bundled_worker_config", return_value={
                "repository": "gualtier-xu/Fudan-CourseLens-Worker",
                "commit": "a" * 40,
                "tree": "b" * 40,
            }),
            patch.object(self.client, "repair_worker") as repair,
        ):
            with self.assertRaises(GitHubAppError) as caught:
                self.client.ensure_worker_trusted()
        self.assertEqual(caught.exception.code, "personal_worker_migration_required")
        repair.assert_not_called()

    def test_worker_auto_repair_skipped_when_already_trusted(self):
        """P59-U3(e)：版本一致时除既有检查外零额外远程调用、绝不触发修复。"""
        with (
            patch.object(
                self.client, "check_worker_integrity", return_value={"trusted": True}
            ) as check,
            patch.object(self.client, "repair_worker") as repair,
        ):
            result = self.client.ensure_worker_trusted()
        self.assertTrue(result["trusted"])
        check.assert_called_once()
        repair.assert_not_called()

    def test_signed_mirror_release_verifies_public_commit_manifest_and_trust(self):
        root = SigningKey.generate()
        release = SigningKey.generate()
        root_private = base64.b64encode(bytes(root)).decode("ascii")
        root_public = base64.b64encode(bytes(root.verify_key)).decode("ascii")
        release_private = base64.b64encode(bytes(release)).decode("ascii")
        release_public = base64.b64encode(bytes(release.verify_key)).decode("ascii")
        trust = {
            "schema": TRUST_SCHEMA,
            "epoch": 3,
            "expires_at": int(time.time()) + 3600,
            "release_keys": [{
                "key_id": "release-test-01", "public_key": release_public, "status": "active",
            }],
            "revoked_manifests": [],
        }
        trust_signature = sign_document(trust, key_id="root-test-01", private_key=root_private)
        files = [{"path": "README.md", "mode": "100644", "size": 3, "sha256": "0" * 64}]
        from shared.protocol.mirror import canonical_json, sha256_hex
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "protocol": {"supported_versions": ["2"]},
            "payload": {
                "files": files,
                "tree_sha256": sha256_hex(canonical_json(files)),
                "git_tree": "d" * 40,
            },
        }
        manifest_signature = sign_document(
            manifest, key_id="release-test-01", private_key=release_private
        )
        commit = "a" * 40
        tree = "b" * 40
        documents = {
            "worker-mirror.manifest.json": manifest,
            "worker-mirror.manifest.sig": manifest_signature,
            "worker-mirror-trust.json": trust,
            "worker-mirror-trust.sig": trust_signature,
        }

        def api(_method, path, **_kwargs):
            if path.endswith(f"/commits/{commit}"):
                return JsonResponse({"sha": commit, "commit": {"tree": {"sha": tree}}})
            for name, value in documents.items():
                if f"/contents/{name}?ref=" in path:
                    raw = json.dumps(value).encode("utf-8")
                    return JsonResponse({
                        "type": "file", "encoding": "base64",
                        "content": base64.encodebytes(raw).decode("ascii"),
                    })
            raise AssertionError(path)

        with (
            patch("src.remote.github_app._bundled_worker_config", return_value={
                "mode": "signed-mirror",
                "repository": "gualtier-xu/Fudan-CourseLens-Worker",
                "commit": commit,
                "tree": tree,
                "manifest_sha256": document_sha256(manifest),
                "signing_key_id": "release-test-01",
                "trust_epoch": 3,
                "protocol_versions": ["2"],
                "root_keys": {"root-test-01": root_public},
                "paths": {},
            }),
            patch.object(self.client, "_api", side_effect=api),
        ):
            verified = self.client._validated_worker_release(token="token")

        self.assertTrue(verified["manifest_verified"])
        self.assertEqual(verified["manifest_sha256"], document_sha256(manifest))

    def test_repair_rejects_symlink_tree_entries(self):
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/Fudan-CourseLens-Worker")

        def api(method, path, **_kwargs):
            if path == "/user":
                return JsonResponse({"login": "student"})
            if path == "/repos/student/Fudan-CourseLens-Worker":
                return JsonResponse({
                    "owner": {"login": "student"}, "private": False,
                    "description": "Managed by Fudan CourseLens desktop client",
                    "default_branch": "main",
                })
            if path.endswith("/commits/main"):
                return JsonResponse({"sha": "1" * 40, "commit": {"tree": {"sha": "2" * 40}}})
            if "/git/trees/" in path and method == "GET":
                return JsonResponse({
                    "truncated": False,
                    "tree": [{
                        "path": "linked.py", "mode": "120000", "type": "blob",
                        "sha": "4" * 40, "size": 8,
                    }],
                })
            raise AssertionError((method, path))

        with (
            patch.object(self.client, "_validated_worker_release", return_value={
                "mode": "signed-mirror",
                "repository": "gualtier-xu/Fudan-CourseLens-Worker",
                "commit": "3" * 40,
                "tree": "5" * 40,
                "manifest_verified": True,
            }),
            patch.object(self.client, "_api", side_effect=api),
        ):
            with self.assertRaises(GitHubAppError):
                self.client.repair_worker()


class GitHubDeviceAuthorizationServiceTests(unittest.TestCase):
    def service(self):
        service = CourseLensApplication.__new__(CourseLensApplication)
        service.github_app = Mock()
        service._github_device_lock = threading.RLock()
        service._github_device_authorization = None
        service._github_device_last_poll = 0.0
        # GH-UX-REWORK-1：跨会话收口经 _bootstrap_with_selfheal（finally 清阶段），
        # Mock 壳需具备与真实应用同形的锁与阶段字段
        service._lock = threading.RLock()
        service._remote_action_stage = None
        return service

    def test_start_reuses_the_only_active_device_code(self):
        service = self.service()
        authorization = DeviceAuthorization(
            "device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5
        )
        service.github_app.snapshot.return_value = {"authorized": False}
        service.github_app.start_device_authorization.return_value = authorization

        first = service.start_github_device_authorization()
        second = service.start_github_device_authorization()

        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["user_code"], second["user_code"])
        service.github_app.start_device_authorization.assert_called_once_with()

    def test_forced_reauthorization_clears_tokens_and_starts_fresh(self):
        service = self.service()
        authorization = DeviceAuthorization(
            "new-device", "WXYZ-1234", "https://github.com/login/device", time.time() + 300, 5
        )
        service.github_app.snapshot.return_value = {"authorized": False}
        service.github_app.start_device_authorization.return_value = authorization
        service._github_device_authorization = DeviceAuthorization(
            "old-device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5
        )

        result = service.start_github_device_authorization(force=True)

        self.assertFalse(result["reused"])
        self.assertEqual(result["user_code"], "WXYZ-1234")
        service.github_app.clear_user_authorization.assert_called_once_with()
        service.github_app.start_device_authorization.assert_called_once_with()

    def test_start_does_not_replace_an_authorized_session(self):
        service = self.service()
        service.github_app.snapshot.return_value = {
            "authorized": True,
            "installed": True,
            "bootstrapped": True,
        }

        result = service.start_github_device_authorization()

        self.assertEqual(result["state"], "authorized")
        self.assertTrue(result["reused"])
        service.github_app.start_device_authorization.assert_not_called()

    def test_poll_continues_bootstrap_once_when_device_object_is_lost(self):
        """快照已授权但内存 device 对象丢失时，恰好继续一次已同意的 bootstrap。"""
        service = self.service()
        service.github_app.snapshot.return_value = {
            "authorized": True,
            "installed": True,
            "bootstrapped": False,
        }
        service.github_app.bootstrap_student_repositories.return_value = {
            "bootstrapped": True,
            "worker_trusted": False,
        }

        result = service.poll_github_device_authorization()

        self.assertEqual(result["state"], "authorized")
        self.assertTrue(result["installed"])
        self.assertTrue(result["bootstrapped"])
        service.github_app.bootstrap_student_repositories.assert_called_once()

    def test_poll_authorized_returns_pending_bootstrap_without_inline_setup(self):
        """GH-UX-REWORK-1（MF-1）：授权确认即刻回包（setup_state=pending_bootstrap），
        不再在同一 poll POST 内联跑可能持续数分钟的 bootstrap——初始化改由
        前端经 remote-connection/actions bootstrap 自动续跑（分步阶段+自愈）。"""
        service = self.service()
        service._github_device_authorization = DeviceAuthorization(
            "device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5
        )
        service.github_app.poll_device_authorization.return_value = {
            "state": "authorized", "login": "student", "account_id": 1,
            "installed": False, "installation_status": "missing",
        }

        result = service.poll_github_device_authorization()

        self.assertEqual(result["state"], "authorized")
        self.assertEqual(result["setup_state"], "pending_bootstrap")
        self.assertEqual(result["user_code"], "ABCD-EFGH")
        service.github_app.bootstrap_student_repositories.assert_not_called()

    def test_poll_degraded_authorized_result_returns_pending_bootstrap(self):
        """poll 返回闭集降级形态（authorized+unavailable）时同样即刻回包，
        不内联 bootstrap（降级形态由 bootstrap 幂等收口，不阻塞授权确认）。"""
        service = self.service()
        service._github_device_authorization = DeviceAuthorization(
            "device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5
        )
        service.github_app.poll_device_authorization.return_value = {
            "state": "authorized", "login": "student", "account_id": 1,
            "installed": False, "installation_status": "unavailable",
        }

        result = service.poll_github_device_authorization()

        self.assertEqual(result["state"], "authorized",
                         "降级 authorized 形态不得被改写为授权失败")
        self.assertEqual(result["installation_status"], "unavailable")
        self.assertEqual(result["setup_state"], "pending_bootstrap")
        service.github_app.bootstrap_student_repositories.assert_not_called()

    def test_startup_auto_connect_resume_uses_persisted_grant_without_user_action(self):
        """授权持久化后新实例启动：既有 auto_connect 恢复直接验证成功，无需用户动作。"""
        credentials = MemoryCredentials()
        client = GitHubAppClient(credentials, client_id="client-id", app_slug="fudan-courselens")
        authorization = DeviceAuthorization(
            "device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5
        )
        with patch.object(client, "_request_external", side_effect=[
            JsonResponse({"access_token": "access-secret", "expires_in": 3600, "refresh_token": "refresh-secret"}),
            GitHubAppError("GitHub 连接失败：ConnectionError", code="github_unreachable"),
        ]):
            degraded = client.poll_device_authorization(authorization)
        self.assertEqual(degraded["installation_status"], "unavailable")

        revived = GitHubAppClient(credentials, client_id="client-id", app_slug="fudan-courselens")
        with patch.object(revived, "_request_external", side_effect=[
            JsonResponse({"login": "student", "id": 123456}),
            JsonResponse({"total_count": 1, "installations": [{
                "id": 42, "app_slug": "fudan-courselens", "target_type": "User",
                "account": {"login": "student", "type": "User"},
                "permissions": {"actions": "write"},
            }]}),
        ]):
            service = self.service()
            service.credentials = credentials
            service.github_app = revived
            outcome = service._auto_connect_resume_github()

        self.assertEqual(outcome, {"state": "started", "code": "github_resume_verified"})

    def test_poll_without_device_object_skips_bootstrap_when_already_bootstrapped(self):
        service = self.service()
        service.github_app.snapshot.return_value = {"authorized": True, "bootstrapped": True}

        result = service.poll_github_device_authorization()

        self.assertEqual(result["state"], "authorized")
        self.assertTrue(result["bootstrapped"])
        service.github_app.bootstrap_student_repositories.assert_not_called()

    def test_bootstrap_github_sets_and_clears_stage_on_success(self):
        """bootstrap_github：成功路径阶段字段布防并在收口清理（快照轮询可见）。"""
        service = self.service()
        service._reload_remote_coordinator = Mock()
        service.github_app.bootstrap_student_repositories.return_value = {"setup_state": "complete"}
        observed = []

        def record_progress(**kwargs):
            progress = kwargs.get("progress")
            self.assertTrue(callable(progress), "progress 回调必须随调用传入")
            progress("syncing_documents")
            observed.append(dict(service._remote_action_stage or {}))
            return {"setup_state": "complete"}

        service.github_app.bootstrap_student_repositories.side_effect = record_progress

        result = service.bootstrap_github()

        self.assertEqual(result, {"setup_state": "complete"})
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].get("action"), "bootstrap")
        self.assertEqual(observed[0].get("label"), "正在同步文档")
        self.assertFalse(service._remote_action_stage, "动作收口阶段字段必须清理")

    def test_bootstrap_github_clears_stage_on_failure(self):
        """bootstrap_github：失败路径阶段字段同样清理（不留陈旧阶段）。"""
        service = self.service()
        service._reload_remote_coordinator = Mock()
        service.github_app.bootstrap_student_repositories.side_effect = GitHubAppError(
            "conflict", code="managed_repository_name_conflict"
        )

        with self.assertRaises(GitHubAppError):
            service.bootstrap_github()

        self.assertFalse(service._remote_action_stage)

    def test_bootstrap_selfheal_retries_transient_failures_with_backoff(self):
        """瞬态失败闭集（github_unreachable/rate_limited）退避重试直至成功；
        每次尝试阶段字段重建（progress 回调持续布防）。"""
        service = self.service()
        service._reload_remote_coordinator = Mock()
        attempts = []

        def flaky_bootstrap(**kwargs):
            attempts.append(len(attempts))
            if len(attempts) < 3:
                raise GitHubAppError(
                    "GitHub 连接失败", code="github_unreachable" if len(attempts) == 1 else "rate_limited"
                )
            return {"setup_state": "complete"}

        service.github_app.bootstrap_student_repositories.side_effect = flaky_bootstrap
        service._reload_remote_coordinator = Mock()

        with patch("src.application.BOOTSTRAP_SELFHEAL_RETRY_DELAYS_SECONDS", (0.0, 0.0)):
            result = service.bootstrap_github()

        self.assertEqual(result, {"setup_state": "complete"})
        self.assertEqual(len(attempts), 3, "恰两次瞬态重试后成功")
        self.assertFalse(service._remote_action_stage)

    def test_bootstrap_selfheal_never_retries_non_transient_failures(self):
        """非瞬态（权限/冲突类）失败一次上抛，绝不重试放大写面。"""
        service = self.service()
        service.github_app.bootstrap_student_repositories.side_effect = GitHubAppError(
            "conflict", code="managed_repository_name_conflict"
        )

        with patch("src.application.BOOTSTRAP_SELFHEAL_RETRY_DELAYS_SECONDS", (0.0, 0.0)):
            with self.assertRaises(GitHubAppError):
                service.bootstrap_github()

        service.github_app.bootstrap_student_repositories.assert_called_once()

    def test_poll_without_device_object_keeps_authorization_when_bootstrap_fails(self):
        service = self.service()
        service.github_app.snapshot.return_value = {"authorized": True, "bootstrapped": False}
        service.github_app.bootstrap_student_repositories.side_effect = GitHubAppError(
            "conflict", code="managed_repository_name_conflict"
        )

        result = service.poll_github_device_authorization()

        self.assertEqual(result["state"], "authorized",
                         "授权成功不能被后续创建失败改写为授权失败")
        self.assertEqual(result["setup_state"], "bootstrap_failed")
        self.assertEqual(result["setup_error_code"], "managed_repository_name_conflict")

    def test_poll_does_not_chain_bootstrap_when_already_bootstrapped(self):
        """GH-UX-REWORK-1：设备对象在场的 authorized 回包绝不内联 bootstrap
        （初始化由前端自动续跑），已建仓快照同样只回 pending_bootstrap。"""
        service = self.service()
        service._github_device_authorization = DeviceAuthorization(
            "device", "ABCD-EFGH", "https://github.com/login/device", time.time() + 300, 5
        )
        service.github_app.poll_device_authorization.return_value = {
            "state": "authorized", "login": "student", "account_id": 1,
            "installed": True, "installation_status": "bound",
        }
        service.github_app.snapshot.return_value = {"authorized": True, "bootstrapped": True}

        result = service.poll_github_device_authorization()

        self.assertEqual(result["state"], "authorized")
        self.assertEqual(result["setup_state"], "pending_bootstrap")
        service.github_app.bootstrap_student_repositories.assert_not_called()

    def test_poll_without_device_object_returns_awaiting_setup_result(self):
        """跨会话收口路径（device 对象丢失）保留内联幂等 bootstrap：awaiting
        形态原样并入 authorized 回包（与已同意首跑流同一语义）。"""
        service = self.service()
        service.github_app.snapshot.return_value = {"authorized": True, "bootstrapped": False}
        service.github_app.bootstrap_student_repositories.return_value = {
            "setup_state": "awaiting_installation",
            "installation_status": "missing",
            "installation_setup_url": (
                "https://github.com/apps/fudan-courselens/installations/new/permissions"
                "?suggested_target_id=1&repository_ids[]=2&repository_ids[]=3"
            ),
            "installation_settings_url": "",
        }

        result = service.poll_github_device_authorization()

        self.assertEqual(result["state"], "authorized")
        self.assertEqual(result["setup_state"], "awaiting_installation")
        service.github_app.bootstrap_student_repositories.assert_called_once()

    def test_repair_keeps_existing_worker_keys(self):
        service = self.service()
        service._reload_remote_coordinator = Mock()
        service.github_app.snapshot.side_effect = [
            {"bootstrapped": True},
            {"bootstrapped": True, "worker_trusted": True},
        ]
        service.github_app.repair_worker.return_value = {"trusted": True, "repaired": True}

        result = service.repair_github_worker()

        self.assertTrue(result["worker_trusted"])
        self.assertTrue(result["repaired"])
        service.github_app.bootstrap_student_repositories.assert_not_called()
        service.github_app.sync_managed_mailbox_documents.assert_called_once_with()
        service._reload_remote_coordinator.assert_called_once_with()

    def test_repair_finishes_an_interrupted_first_bootstrap(self):
        service = self.service()
        service._reload_remote_coordinator = Mock()
        service.github_app.snapshot.return_value = {"bootstrapped": False}
        service.github_app.repair_worker.return_value = {"trusted": True, "repaired": True}
        service.github_app.bootstrap_student_repositories.return_value = {
            "bootstrapped": True,
            "worker_trusted": True,
        }

        result = service.repair_github_worker()

        self.assertTrue(result["bootstrapped"])
        self.assertTrue(result["repaired"])
        service.github_app.bootstrap_student_repositories.assert_called_once_with()
        service.github_app.sync_managed_mailbox_documents.assert_not_called()


class AppManifestPermissionTests(unittest.TestCase):
    """App 注册清单不再请求 Plan (billing) 读取权限；既有远端 App 设置不动。"""

    def test_registration_manifest_does_not_request_plan_permission(self):
        manifest_source = (ROOT / "scripts" / "register_github_app.py").read_text(encoding="utf-8")
        self.assertNotIn('"plan"', manifest_source)
        self.assertNotIn("billing", manifest_source.casefold())


class GitHubAppReadonlyGetCacheTests(unittest.TestCase):
    """PARK-N3：信任门只读 GET 缓存壳（进程内短 TTL）钉子。

    现状锚：每派发信任门 = ``_validated_worker_release`` 的 5 个「完整 40 位
    SHA 寻址」GET（commits/{pin} + 4 份签名文档，Git 对象不可变）+ 个人仓
    ``commits/main``（可变分支，永不缓存）。二连调用第二跳必须零 release
    GET、仅 1 个 main GET；过期后全量回源；写操作与非 200 绝不入缓存；
    签名/摘要复核每跳照常本地重跑（缓存的是不可变字节，不是校验结论）。
    """

    def setUp(self):
        self.credentials = MemoryCredentials()
        self.client = GitHubAppClient(self.credentials, client_id="client-id", app_slug="fudan-courselens")
        from shared.protocol.mirror import canonical_json, sha256_hex
        self.commit = "a" * 40
        self.tree = "b" * 40
        root = SigningKey.generate()
        release = SigningKey.generate()
        root_public = base64.b64encode(bytes(root.verify_key)).decode("ascii")
        release_private = base64.b64encode(bytes(release)).decode("ascii")
        trust = {
            "schema": TRUST_SCHEMA,
            "epoch": 3,
            "expires_at": int(time.time()) + 3600,
            "release_keys": [{
                "key_id": "release-test-01",
                "public_key": base64.b64encode(bytes(release.verify_key)).decode("ascii"),
                "status": "active",
            }],
            "revoked_manifests": [],
        }
        files = [{"path": "README.md", "mode": "100644", "size": 3, "sha256": "0" * 64}]
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "protocol": {"supported_versions": ["2"]},
            "payload": {
                "files": files,
                "tree_sha256": sha256_hex(canonical_json(files)),
                "git_tree": "d" * 40,
            },
        }
        self.documents = {
            "worker-mirror.manifest.json": manifest,
            "worker-mirror.manifest.sig": sign_document(
                manifest, key_id="release-test-01", private_key=release_private
            ),
            "worker-mirror-trust.json": trust,
            "worker-mirror-trust.sig": sign_document(
                trust, key_id="root-test-01", private_key=base64.b64encode(bytes(root)).decode("ascii")
            ),
        }
        self.bundled = {
            "mode": "signed-mirror",
            "repository": "gualtier-xu/Fudan-CourseLens-Worker",
            "commit": self.commit,
            "tree": self.tree,
            "manifest_sha256": document_sha256(manifest),
            "signing_key_id": "release-test-01",
            "trust_epoch": 3,
            "protocol_versions": ["2"],
            "root_keys": {"root-test-01": root_public},
            "paths": {},
        }

    @staticmethod
    def _response(payload, status_code=200):
        response = requests.Response()
        response.status_code = status_code
        response._content = json.dumps(payload).encode("utf-8")
        response.encoding = "utf-8"
        response.headers["Content-Type"] = "application/json"
        return response

    def _install_transport(self):
        """真实 session 层计数假件：恰覆盖信任门 5 个 release GET + 个人仓 main。"""
        calls = []

        def respond(method, url, **_kwargs):
            calls.append((str(method).upper(), str(url)))
            if str(url).endswith(f"/commits/{self.commit}"):
                return self._response({"sha": self.commit, "commit": {"tree": {"sha": self.tree}}})
            for name, value in self.documents.items():
                if f"/contents/{name}?ref=" in str(url):
                    raw = json.dumps(value).encode("utf-8")
                    return self._response({
                        "type": "file", "encoding": "base64",
                        "content": base64.encodebytes(raw).decode("ascii"),
                    })
            if str(url).endswith("/commits/main"):
                return self._response({"sha": "c" * 40, "commit": {"tree": {"sha": self.tree}}})
            if str(url).endswith("/dispatches"):
                return self._response({}, 204)
            raise AssertionError((method, url))

        session = Mock()
        session.request.side_effect = respond
        return session, calls

    def _authorized_client(self):
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        self.credentials.save_secret("github_worker_repo", "student/worker")

    def test_second_dispatch_hop_serves_release_reads_from_cache(self):
        """命中钉：二连信任门第二跳零 release GET（5 个 SHA 寻址 GET 全命中），
        仅个人仓可变 main 恰 1 次实读；两跳签名复核结论逐字段一致。"""
        self._authorized_client()
        session, calls = self._install_transport()
        with (
            patch("src.remote.github_app._bundled_worker_config", return_value=self.bundled),
            patch.object(self.client, "session", session),
        ):
            first = self.client.check_worker_integrity()
            after_first = len(calls)
            second = self.client.check_worker_integrity()
        self.assertTrue(first["trusted"])
        self.assertTrue(second["trusted"])
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])
        self.assertEqual(first["manifest_verified"], second["manifest_verified"])
        self.assertEqual(first["signing_key_id"], second["signing_key_id"])
        self.assertEqual(after_first, 6, "首跳 = 5 个 release GET + 1 个个人仓 main GET")
        self.assertEqual(len(calls), 7, "第二跳仅个人仓 main 恰 1 次实读")
        second_hop = calls[after_first:]
        self.assertEqual([item for item in second_hop if "/contents/" in item[1]], [],
                         "二连派发第二跳零签名文档 GET")
        self.assertEqual([item for item in second_hop if f"/commits/{self.commit}" in item[1]], [],
                         "二连派发第二跳零 pin commit GET")
        self.assertTrue(second_hop[0][1].endswith("/commits/main"))

    def test_expired_cache_entries_refetch_all_release_reads(self):
        """过期钉：TTL 流逝后（白盒置条目为已过期）全部 release GET 回源重取。"""
        self._authorized_client()
        session, calls = self._install_transport()
        with (
            patch("src.remote.github_app._bundled_worker_config", return_value=self.bundled),
            patch.object(self.client, "session", session),
        ):
            self.assertTrue(self.client.check_worker_integrity()["trusted"])
            self.assertEqual(len(calls), 6)
            expired = {
                key: (time.time() - 1, content)
                for key, (_expires_at, content) in self.client._readonly_get_cache.items()
            }
            self.assertTrue(expired, "首跳后缓存应含 release 条目")
            self.client._readonly_get_cache.clear()
            self.client._readonly_get_cache.update(expired)
            self.assertTrue(self.client.check_worker_integrity()["trusted"])
        self.assertEqual(len(calls), 12, "过期后第二门全量回源（6 GET 再发）")

    def test_mutations_and_mutable_reads_are_never_cached(self):
        """写操作钉：POST 每次实发；可变分支头 GET 每次实读——绝不入缓存。"""
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        session, calls = self._install_transport()
        with patch.object(self.client, "session", session):
            for _ in range(2):
                self.client._api(
                    "POST", "/repos/student/worker/actions/workflows/echo.yml/dispatches",
                    token="t", expected=(200, 201, 204), json={"ref": "main"},
                )
            for _ in range(2):
                self.client._api("GET", "/repos/student/worker/commits/main", token="t")
        self.assertEqual(len(calls), 4, "POST ×2 实发 + 可变 main GET ×2 实读，零缓存")
        self.assertEqual(len([item for item in calls if item[0] == "POST"]), 2)

    def test_non_200_responses_are_never_cached(self):
        """非 200 钉：缺失证据（404）绝不入缓存——重复读每次实发（保守）。"""
        self.credentials.save_secret("github_app_access_token", "token")
        self.credentials.save_secret("github_app_access_expires_at", str(time.time() + 7200))
        calls = []

        def respond(_method, url, **_kwargs):
            calls.append(str(url))
            return self._response({"message": "Not Found"}, 404)

        session = Mock()
        session.request.side_effect = respond
        with patch.object(self.client, "session", session):
            for _ in range(2):
                with self.assertRaises(GitHubAppError):
                    self.client._api("GET", f"/repos/o/r/commits/{self.commit}", token="t")
        self.assertEqual(len(calls), 2, "404 不入缓存，第二次仍实读")

    def test_cache_whitelist_and_key_normalization(self):
        """白名单钉：仅完整 40 位 SHA 寻址的 commits/contents GET 可入缓存；
        键规范化 = scheme/host 小写 + query 按名稳定排序。"""
        cacheable = GitHubAppClient._readonly_cacheable
        key = GitHubAppClient._readonly_cache_key
        self.assertTrue(cacheable(f"https://api.github.com/repos/o/r/commits/{self.commit}"))
        self.assertTrue(cacheable(f"https://api.github.com/repos/o/r/contents/p/q.json?ref={self.commit}"))
        for refused in (
            "https://api.github.com/repos/o/r/commits/main",
            f"https://api.github.com/repos/o/r/contents/x?ref=main",
            f"https://api.github.com/repos/o/r/contents/x?ref={self.commit}&per_page=100",
            "https://api.github.com/repos/o/r/contents/x",
            f"https://api.github.com/repos/o/r/git/trees/{self.commit}",
            f"https://api.github.com/repos/o/r/commits/{self.commit}/pulls",
            f"http://api.github.com/repos/o/r/commits/{self.commit}",
            "https://github.com/login/device/code",
        ):
            self.assertFalse(cacheable(refused), refused)
        self.assertEqual(
            key(f"https://API.GitHub.com/repos/a/b/contents/x?ref={self.commit}"),
            key(f"https://api.github.com/repos/a/b/contents/x?ref={self.commit}"),
        )
        self.assertEqual(
            key(f"https://api.github.com/repos/a/b/y?per_page=100&ref={self.commit}"),
            key(f"https://api.github.com/repos/a/b/y?ref={self.commit}&per_page=100"),
        )


if __name__ == "__main__":
    unittest.main()
