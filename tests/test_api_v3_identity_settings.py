from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.application import CourseLensApplication
from src.runtime.http_api import FrontendSessionRegistry, make_handler
from src.runtime.network import validate_vpn_connection_snapshot
from tests.http_services import http_services


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_SECRET = "synthetic-secret-must-not-be-returned"
SYNTHETIC_PERSISTED_SECRET = "synthetic-persisted-key-must-not-be-returned"
SYNTHETIC_SESSION_SECRET = "synthetic-session-key-must-not-be-returned"


class _StateStore:
    def __init__(self):
        self.values = {}

    def get_app_state(self, key, default=None):
        return self.values.get(key, default)

    def set_app_state(self, key, value):
        self.values[key] = value

    # CLIENT-STATE-R1：镜像 list_tasks 新契约（newest_first/终态保留窗）。
    def list_tasks(self, limit=200, newest_first=False, terminal_retention_days=None):
        return []


class _Credentials:
    def __init__(self):
        self.deepseek = ""
        self.accounts = {"synthetic-user": {"student_id": "synthetic-user", "requires_rotation": False}}
        self.deleted_accounts = []
        self.saved_secrets = {}
        self.deleted_secrets = []

    def list_accounts(self):
        return [dict(value) for value in self.accounts.values()]

    def has_deepseek_key(self):
        return bool(self.deepseek)

    def deepseek_key_requires_rotation(self):
        return False

    def delete_account(self, student_id):
        self.deleted_accounts.append(student_id)
        return self.accounts.pop(student_id, None) is not None

    def has_secret(self, name):
        return name in self.saved_secrets

    def delete_secret(self, name):
        self.deleted_secrets.append(name)
        return self.saved_secrets.pop(name, None) is not None


class _Network:
    @staticmethod
    def snapshot():
        return {"state": "ready", "mode": "auto", "proxy_url": ""}


class _Service:
    def __init__(self):
        self.task_store = _StateStore()
        self.credentials = _Credentials()
        self.network = _Network()
        self.received_password = ""
        self.auth_state = "checking"
        self._deepseek_api_key = ""
        self.logout_calls = 0
        # T1-R（夜14-R7 P0 信任面）：已存凭据删除动作的录制桩——
        # 记录每次路由调用透传的 student_id，供幂等语义断言。
        self.deleted_credential_calls: list[str] = []
        self.saved_credential_students = {"synthetic-user"}
        # S09-A：用量洞察客户端经 http_services 适配器可达；默认 None=未配置
        self.github_app_client = None
        self.auto_connect = {
            "schema": "courselens.auto-connect.v1",
            "fudan": {"enabled": False, "account_id": "", "status": "off"},
            "github": {"enabled": False, "status": "off"},
            "last_resume": {"observed_at": 0.0, "fudan": {"state": "", "code": ""}, "github": {"state": "", "code": ""}},
        }

    def authentication_snapshot(self):
        return {
            "state": self.auth_state, "source": "fudan", "code": "catalog_verification_pending",
            "observed_at": 1, "expires_at": 2,
        }

    @staticmethod
    def connection_snapshot():
        return {
            "schema": "courselens.vpn-connection.v1",
            "state": "login_required", "network_path": "unknown", "school_route": "unknown",
            "reason": "unknown", "observed_at": 1, "expires_at": None, "retry_after": None,
            "actions": ["login"], "generation": 0,
            "services": {
                "webvpn": {"state": "unknown", "route": "unknown", "verified": False},
                "icourse": {"state": "unknown", "route": "unknown", "verified": False},
            },
        }

    @staticmethod
    def campus_diagnostics():
        return {
            "schema": "courselens.campus-diagnostics.v1",
            "checked_at": 1, "mode": "auto",
            "services": {
                "webvpn": {"route": "direct", "direct_ok": True, "proxy_ok": None, "fallback_used": False, "latency_band": "fast"},
                "icourse": {"route": "unknown", "direct_ok": None, "proxy_ok": None, "fallback_used": False, "latency_band": "unavailable"},
            },
            "state": "login_required", "next_action": "login",
            # P2-E：去标识计数器随按需诊断原样透传（纯计数，无身份/网络数据）。
            "counters": {"auth_flights_total": 2, "reauth_flights": 1},
        }

    @staticmethod
    def authorized_catalog_snapshot(**_filters):
        return {
            "state": "ready",
            "source": "authorized-catalog",
            "courses": [{"course_id": "authorized-course", "title": "Synthetic"}],
            "course_count": 1,
            "lecture_count": 0,
        }

    @staticmethod
    def settings_privacy_snapshot():
        return {
            "state": "ready", "credentials": {"state": "ready"},
            "analytics": {"state": "ready", "enabled": False},
            "remote": {"overall": {"state": "checking"}},
        }

    # WIRING-FIX-1：settings GET 的 deepseek 三元组与顶层读数桩（缺任一该路由 500）。
    def has_deepseek_key(self):
        return bool(self._deepseek_api_key)

    @staticmethod
    def max_deepseek_tokens_limit():
        return 4096

    @staticmethod
    def ai_usage_month():
        return {"requests": 0}

    @staticmethod
    def task_usage_month():
        return {"tasks": 0}

    @staticmethod
    def onboarding_snapshot():
        return {"state": "ready", "consent": {"accepted": False}}

    def set_credentials(self, _student_id, password, remember=False):
        self.received_password = password

    @staticmethod
    def refresh_authorized_catalog_async():
        return {
            "state": "checking", "source": "fudan", "code": "catalog_verification_pending",
            "observed_at": 1, "expires_at": 2,
        }

    @staticmethod
    def use_saved_credentials(_student_id):
        return None

    def logout_fudan(self):
        self.logout_calls += 1
        return {"state": "action_required", "code": "fudan_login_required"}

    def delete_saved_credentials(self, student_id):
        self.deleted_credential_calls.append(student_id)
        if student_id in self.saved_credential_students:
            self.saved_credential_students.discard(student_id)
            return True
        return False

    def set_auto_connect_preference(self, request):
        from src.application import AutoConnectPreferenceError

        if not isinstance(request, dict):
            raise AutoConnectPreferenceError("auto_connect_request_invalid")
        fudan = request.get("fudan")
        if isinstance(fudan, dict):
            enabled = bool(fudan.get("enabled"))
            account_id = str(fudan.get("account_id") or "").strip()
            if enabled:
                if not account_id:
                    raise AutoConnectPreferenceError("auto_connect_account_required")
                account = self.credentials.accounts.get(account_id)
                if account is None:
                    raise AutoConnectPreferenceError("auto_connect_account_missing")
                if bool(account.get("requires_rotation")):
                    raise AutoConnectPreferenceError("auto_connect_account_rotation_required")
            self.auto_connect["fudan"] = {
                "enabled": enabled,
                "account_id": account_id if enabled else "",
                "status": "ready" if enabled else "off",
            }
        github = request.get("github")
        if isinstance(github, dict):
            enabled = bool(github.get("enabled"))
            if enabled and not self.credentials.has_secret("github_app_access_token"):
                raise AutoConnectPreferenceError("auto_connect_github_grant_missing")
            self.auto_connect["github"] = {"enabled": enabled, "status": "ready" if enabled else "off"}
        return {
            "schema": self.auto_connect["schema"],
            "fudan": dict(self.auto_connect["fudan"]),
            "github": dict(self.auto_connect["github"]),
            "last_resume": dict(self.auto_connect["last_resume"]),
        }

    def set_deepseek_key(self, api_key, remember=False):
        # 与真实 Application 一致：remember=False 仅会话 key，不触碰持久 key
        self._deepseek_api_key = api_key
        if remember:
            self.credentials.deepseek = api_key

    def delete_deepseek_key(self):
        existed = bool(self.credentials.deepseek)
        self.credentials.deepseek = ""
        self._deepseek_api_key = ""
        return existed

    def _deepseek_key(self):
        return self._deepseek_api_key or self.credentials.deepseek

    def has_deepseek_key(self):
        return bool(self._deepseek_key())

    @staticmethod
    def update_network_settings(mode, proxy_url):
        return {"state": "ready", "mode": mode, "proxy_url": proxy_url, "routes": {}}

    @staticmethod
    def diagnose_network():
        return {"state": "ready", "services": {}}

    @staticmethod
    def update_processing_consent(accepted):
        return {"state": "ready", "consent": {"accepted": bool(accepted)}}


class IdentitySettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.service = _Service()
        self.sessions = FrontendSessionRegistry(
            lease_seconds=30, shutdown_grace_seconds=30, poll_seconds=1,
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(http_services(self.service), ROOT / "frontend", frontend_sessions=self.sessions),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.sessions.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _post(self, route, body, expected=200):
        request = Request(
            f"{self.base}{route}", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            response = urlopen(request)
        except HTTPError as exc:
            if exc.code != expected:
                raise
            return exc.code, json.loads(exc.read())
        with response:
            self.assertEqual(response.status, expected)
            return response.status, json.loads(response.read())

    def _get(self, route, expected=200):
        try:
            response = urlopen(f"{self.base}{route}")
        except HTTPError as exc:
            if exc.code != expected:
                raise
            return exc.code, json.loads(exc.read())
        with response:
            self.assertEqual(response.status, expected)
            return response.status, json.loads(response.read())

    def test_authentication_route_attaches_connection_snapshot_additively(self):
        _, payload = self._get("/api/v3/authentication")
        data = payload["data"]
        # 既有键原样保留（旧前端忽略新增键继续工作）。
        for key in ("state", "source", "code", "observed_at", "expires_at"):
            self.assertIn(key, data)
        connection = data.get("connection")
        self.assertIsInstance(connection, dict)
        self.assertEqual(connection.get("schema"), "courselens.vpn-connection.v1")
        self.assertEqual(validate_vpn_connection_snapshot(connection), [])

    def test_settings_snapshot_carries_deepseek_readiness_triple(self):
        # WIRING-FIX-1（BROWSERWALK-3 F1-P1-1）：深度问答 key 门读
        # settings.deepseek.configured（search-palette refreshDeepReadiness）；
        # 三元组与 accounts 路由同源同式，纯布尔无 key 物料。
        _, payload = self._get("/api/v3/settings")
        triple = payload["data"]["deepseek"]
        self.assertEqual(set(triple), {"configured", "saved", "requires_rotation"})
        self.assertFalse(triple["configured"])
        self.assertFalse(triple["saved"])
        self.service._deepseek_api_key = "sk-synthetic"
        _, payload = self._get("/api/v3/settings")
        self.assertTrue(payload["data"]["deepseek"]["configured"])
        self.service._deepseek_api_key = ""

    def test_campus_diagnostics_route_is_closed_set_and_on_demand(self):
        _, payload = self._get("/api/v3/campus-diagnostics")
        value = payload["data"]
        self.assertEqual(value["schema"], "courselens.campus-diagnostics.v1")
        self.assertEqual(set(value["services"]), {"webvpn", "icourse"})
        for service in value["services"].values():
            self.assertIn(service["route"], {"direct", "proxy", "unknown"})
            self.assertIn(service["latency_band"], {"fast", "normal", "slow", "unavailable"})
            self.assertIsInstance(service["fallback_used"], bool)
        # P2-E：计数器只透传纯整数闭集，绝不携带身份/网络数据。
        self.assertEqual(
            value["counters"], {"auth_flights_total": 2, "reauth_flights": 1},
        )
        text = json.dumps(value).casefold()
        for forbidden in ("http", "127.0.0.1", "password", "cookie", "token", "lck"):
            self.assertNotIn(forbidden, text)

    def test_authentication_is_accepted_as_checking_and_never_echoes_password(self):
        body = {
            "action": "login", "operation_id": "auth-operation-0001",
            "student_id": "synthetic-user", "password": SYNTHETIC_SECRET,
            "remember": True,
        }
        _, first = self._post("/api/v3/authentication/actions", body, expected=202)
        _, duplicate = self._post("/api/v3/authentication/actions", body, expected=200)
        self.assertEqual(first["data"]["authentication"]["state"], "checking")
        self.assertEqual(first["data"], duplicate["data"])
        self.assertEqual(self.service.received_password, SYNTHETIC_SECRET)
        self.assertNotIn(SYNTHETIC_SECRET, json.dumps(first))
        self.assertNotIn("password", json.dumps(first).casefold())

    def test_account_and_secret_snapshots_return_configuration_only(self):
        with urlopen(f"{self.base}/api/v3/accounts") as response:
            accounts = json.loads(response.read())
        self.assertEqual(accounts["schema"], "courselens.api.v3")
        self.assertNotIn(SYNTHETIC_SECRET, json.dumps(accounts))

        _, saved = self._post("/api/v3/secrets/actions", {
            "action": "set-deepseek", "api_key": SYNTHETIC_SECRET, "remember": True,
        })
        self.assertTrue(saved["data"]["configured"])
        self.assertNotIn(SYNTHETIC_SECRET, json.dumps(saved))
        with urlopen(f"{self.base}/api/v3/accounts") as response:
            configured = json.loads(response.read())
        self.assertEqual(configured["data"]["deepseek"], {
            "configured": True, "saved": True, "requires_rotation": False,
        })
        self.assertNotIn(SYNTHETIC_SECRET, json.dumps(configured))

    def test_set_deepseek_without_remember_keeps_persisted_key_session_only(self):
        self.service.credentials.deepseek = SYNTHETIC_PERSISTED_SECRET
        _, saved = self._post("/api/v3/secrets/actions", {
            "action": "set-deepseek", "api_key": SYNTHETIC_SESSION_SECRET, "remember": False,
        })
        self.assertEqual(saved["data"], {"configured": True, "saved": True, "deleted": False})
        self.assertEqual(self.service._deepseek_api_key, SYNTHETIC_SESSION_SECRET)
        self.assertEqual(self.service.credentials.deepseek, SYNTHETIC_PERSISTED_SECRET)
        with urlopen(f"{self.base}/api/v3/accounts") as response:
            accounts = json.loads(response.read())
        self.assertEqual(accounts["data"]["deepseek"], {
            "configured": True, "saved": True, "requires_rotation": False,
        })
        for payload in (saved, accounts):
            self.assertNotIn(SYNTHETIC_SESSION_SECRET, json.dumps(payload))
            self.assertNotIn(SYNTHETIC_PERSISTED_SECRET, json.dumps(payload))

    def test_account_delete_action_route_is_closed_set_and_idempotent(self):
        # T1-R（夜14-R7 P0 信任面）：删除已存凭据是设置页真实可达的不可逆动作
        # （settings.js postV3 accounts/actions），此前路由级零钉。三腿：
        # ①happy → envelope deleted=true 且 service 收到透传的 student_id；
        # ②同 action 二次 → deleted=false（幂等语义钉死）；③未知 action/缺
        # student_id → 400 account_action_invalid，绝不静默当作 delete。
        self.assertIn("synthetic-user", self.service.saved_credential_students)
        _, deleted = self._post("/api/v3/accounts/actions", {
            "action": "delete", "student_id": "synthetic-user",
        })
        self.assertEqual(deleted["data"], {"deleted": True})
        self.assertNotIn("password", json.dumps(deleted).casefold())
        self.assertEqual(self.service.deleted_credential_calls, ["synthetic-user"])

        _, again = self._post("/api/v3/accounts/actions", {
            "action": "delete", "student_id": "synthetic-user",
        })
        self.assertEqual(again["data"], {"deleted": False}, "二次删除必须幂等返回 deleted=false")
        self.assertEqual(self.service.deleted_credential_calls, ["synthetic-user", "synthetic-user"])

        for body in (
            {"action": "rm_rf", "student_id": "synthetic-user"},
            {"action": "DELETE", "student_id": ""},
            {"action": "delete"},
            {"action": "delete", "student_id": "   "},
        ):
            with self.subTest(body=body):
                _, rejected = self._post("/api/v3/accounts/actions", body, expected=400)
                self.assertEqual(rejected["error_code"], "account_action_invalid")
        self.assertEqual(
            self.service.deleted_credential_calls, ["synthetic-user", "synthetic-user"],
            "被拒请求绝不触发删除",
        )

    def test_frontend_session_lifecycle_is_backend_confirmed(self):
        for action, expected in (("open", 1), ("heartbeat", 1), ("close", 0)):
            _, value = self._post("/api/v3/frontend-session", {
                "action": action, "session_id": "frontend-session-0001",
            })
            self.assertEqual(value["data"]["active_sessions"], expected)

    def test_only_health_and_v3_api_namespaces_are_supported(self):
        with urlopen(f"{self.base}/api/health") as response:
            health = json.loads(response.read())
        self.assertTrue(health["ok"])
        self.assertEqual(health["pid"], os.getpid())
        canon = json.loads((ROOT / "courselens-version.json").read_text(encoding="utf-8"))
        self.assertEqual(health["version"], canon["version"])
        for path in (
            "/api/status", "/api/download", "/api/subtitle", "/api/summary",
            "/api/open-folder", "/api/remote-compute",
        ):
            with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                urlopen(f"{self.base}{path}")
            self.assertEqual(caught.exception.code, 404)

    def test_catalog_uses_only_the_identity_scoped_service(self):
        self.service.auth_state = "ready"
        with urlopen(f"{self.base}/api/v3/catalog?page=1&page_size=24") as response:
            catalog = json.loads(response.read())
        self.assertEqual(
            [course["course_id"] for course in catalog["data"]["courses"]],
            ["authorized-course"],
        )
        self.assertEqual(catalog["data"]["source"], "authorized-catalog")

    def test_identity_scoped_views_fail_closed_while_authentication_is_pending(self):
        self.service.auth_state = "checking"
        content_key = {"tasks": "tasks", "concepts": "concepts", "analytics": "summary"}
        empty_content = {"tasks": [], "concepts": [], "analytics": None}
        for route in ("tasks", "concepts", "analytics"):
            with self.subTest(route=route), urlopen(f"{self.base}/api/v3/{route}") as response:
                value = json.loads(response.read())["data"]
            # Pending verification reports checking honestly: the snapshot code passes
            # through verbatim (never a fabricated login code), no login action is
            # offered, and protected content stays empty.
            self.assertEqual(value["state"], "checking")
            self.assertEqual(value["code"], "catalog_verification_pending")
            self.assertEqual(value["actions"], [])
            self.assertEqual(value[content_key[route]], empty_content[route])
        # Confirmed non-ready states stay fail closed: login required, content empty.
        self.service.auth_state = "degraded"
        for route in ("tasks", "concepts", "analytics"):
            with self.subTest(route=route), urlopen(f"{self.base}/api/v3/{route}") as response:
                value = json.loads(response.read())["data"]
            self.assertEqual(value["state"], "action_required")
            self.assertIn("login", value["actions"])
            self.assertEqual(value[content_key[route]], empty_content[route])


    def test_auto_connect_enable_requires_explicit_selectable_account(self):
        _, rejected = self._post("/api/v3/settings/actions", {
            "action": "set-auto-connect", "fudan": {"enabled": True, "account_id": ""},
        }, expected=409)
        self.assertEqual(rejected["error_code"], "auto_connect_account_required")
        self.assertEqual(self.service.credentials.deleted_accounts, [])

        _, rejected = self._post("/api/v3/settings/actions", {
            "action": "set-auto-connect", "fudan": {"enabled": True, "account_id": "ghost-account"},
        }, expected=409)
        self.assertEqual(rejected["error_code"], "auto_connect_account_missing")

        self.service.credentials.accounts["rotation-user"] = {
            "student_id": "rotation-user", "requires_rotation": True,
        }
        _, rejected = self._post("/api/v3/settings/actions", {
            "action": "set-auto-connect", "fudan": {"enabled": True, "account_id": "rotation-user"},
        }, expected=409)
        self.assertEqual(rejected["error_code"], "auto_connect_account_rotation_required")
        # 被拒的开启请求绝不静默切换到其他已保存账号
        self.assertFalse(self.service.auto_connect["fudan"]["enabled"])

    def test_auto_connect_round_trip_is_closed_set_and_disabling_touches_no_credentials(self):
        _, enabled = self._post("/api/v3/settings/actions", {
            "action": "set-auto-connect",
            "fudan": {"enabled": True, "account_id": "synthetic-user"},
        })
        self.assertEqual(enabled["data"]["fudan"], {
            "enabled": True, "account_id": "synthetic-user", "status": "ready",
        })
        self.assertNotIn("password", json.dumps(enabled).casefold())
        self.assertNotIn("token", json.dumps(enabled).casefold())

        _, disabled = self._post("/api/v3/settings/actions", {
            "action": "set-auto-connect", "fudan": {"enabled": False},
        })
        self.assertEqual(disabled["data"]["fudan"]["enabled"], False)
        self.assertEqual(self.service.credentials.deleted_accounts, [], "关闭偏好不删除凭据")
        self.assertEqual(self.service.logout_calls, 0, "关闭偏好不触发登出")

    def test_auto_connect_github_enable_requires_stored_user_grant(self):
        _, rejected = self._post("/api/v3/settings/actions", {
            "action": "set-auto-connect", "github": {"enabled": True},
        }, expected=409)
        self.assertEqual(rejected["error_code"], "auto_connect_github_grant_missing")
        self.service.credentials.saved_secrets["github_app_access_token"] = "synthetic"
        _, enabled = self._post("/api/v3/settings/actions", {
            "action": "set-auto-connect", "github": {"enabled": True},
        })
        self.assertEqual(enabled["data"]["github"], {"enabled": True, "status": "ready"})

    def test_auto_connect_unknown_action_stays_rejected(self):
        _, rejected = self._post("/api/v3/settings/actions", {
            "action": "export-credentials",
        }, expected=400)
        self.assertEqual(rejected["error_code"], "settings_action_invalid")

    def test_update_network_proxy_url_rejects_dirty_values_without_faking_success(self):
        # D8：手动代理必填且可解析（http/https + 主机非空）；脏值一律
        # proxy_url_invalid，不再静默补 http:// 前缀或静默换默认地址。
        for proxy in ("", "127.0.0.1:8888", "socks5://127.0.0.1:1080", "http://"):
            _, rejected = self._post("/api/v3/settings/actions", {
                "action": "update-network", "mode": "manual", "proxy_url": proxy,
            }, expected=400)
            self.assertEqual(rejected["error_code"], "proxy_url_invalid")
        _, saved = self._post("/api/v3/settings/actions", {
            "action": "update-network", "mode": "manual", "proxy_url": "http://127.0.0.1:8888",
        })
        self.assertEqual(saved["data"]["mode"], "manual")
        self.assertEqual(saved["data"]["proxy_url"], "http://127.0.0.1:8888")
        # 非 manual 模式携带脏值同样拒绝（隐藏输入框里的陈旧脏值不再被静默收下）
        _, rejected = self._post("/api/v3/settings/actions", {
            "action": "update-network", "mode": "auto", "proxy_url": "127.0.0.1:8888",
        }, expected=400)
        self.assertEqual(rejected["error_code"], "proxy_url_invalid")
        # 非 manual 模式空值照常通过（该模式不消费代理字段）
        _, saved = self._post("/api/v3/settings/actions", {
            "action": "update-network", "mode": "auto", "proxy_url": "",
        })
        self.assertEqual(saved["data"]["mode"], "auto")


class GithubBillingRemovalRouteTests(unittest.TestCase):
    """Billing/用量洞察功能整体移除：路由 404、任务载荷不再携带预算面
    （「今日计算保护」已于 2026-09-22 整体移除，protection_alert 恒 null）。"""

    def setUp(self):
        self.service = _Service()
        self.sessions = FrontendSessionRegistry(
            lease_seconds=30, shutdown_grace_seconds=30, poll_seconds=1,
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(http_services(self.service), ROOT / "frontend", frontend_sessions=self.sessions),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.sessions.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _get(self, route):
        with urlopen(f"{self.base}{route}") as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read())

    def _post(self, route, body, expected=200):
        request = Request(
            f"{self.base}{route}", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            response = urlopen(request)
        except HTTPError as exc:
            if exc.code != expected:
                raise
            return exc.code, json.loads(exc.read())
        with response:
            self.assertEqual(response.status, expected)
            return response.status, json.loads(response.read())

    def test_billing_routes_are_gone(self):
        try:
            with urlopen(f"{self.base}/api/v3/github-billing"):
                self.fail("GET github-billing 必须已删除")
        except HTTPError as exc:
            self.assertEqual(exc.code, 404)
        try:
            self._post("/api/v3/github-billing/actions", {"action": "set-enabled", "enabled": True})
            self.fail("POST github-billing/actions 必须已删除")
        except HTTPError as exc:
            self.assertEqual(exc.code, 404)

    def test_tasks_payload_replaces_worker_budget_with_trigger_only_alert(self):
        self.service.auth_state = "ready"  # 通过课程闸门：tasks 路由返回完整载荷
        self.service.task_store.set_app_state("github_billing_preference", {"enabled": True})
        self.service.task_store.set_app_state("github_billing_cache", "stale-inert-row")
        tasks = self._get("/api/v3/tasks")["data"]
        self.assertIn("tasks", tasks)
        self.assertNotIn("worker_budget", tasks, "常驻数值预算载荷必须删除")
        self.assertIsNone(tasks["protection_alert"], "预算移除后保护告警恒为 null")


class ConnectionSnapshotResumeAdvanceTests(unittest.TestCase):
    """P3.1：轮询面（GET /api/v3/authentication 附加的 connection 快照）在
    宿主休眠恢复后推进路由代际，且快照保持合同闭集；恢复事件只发生一次。"""

    def setUp(self) -> None:
        scratch = ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        frontend = ROOT / "frontend"
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(
                http_services(self.app),
                frontend,
                frontend_sessions=FrontendSessionRegistry(
                    lease_seconds=30, shutdown_grace_seconds=30, poll_seconds=1,
                ),
            ),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.app.close()
        self.temporary.cleanup()

    def _get_authentication(self) -> dict:
        with urlopen(Request(f"{self.base}/api/v3/authentication")) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read().decode("utf-8"))

    def test_poll_surface_reflects_resume_generation_advance(self):
        first = self._get_authentication()["data"]["connection"]["generation"]
        # 模拟一次宿主休眠：墙钟大幅领先单调钟（惰性判定，无后台轮询）。
        with self.app._lock:
            self.app._host_activity_observed = (
                time.monotonic() - 1.0,
                time.time() - 3600.0,
            )
        second_payload = self._get_authentication()
        connection = second_payload["data"]["connection"]
        self.assertEqual(connection["generation"], first + 1)
        self.assertEqual(validate_vpn_connection_snapshot(connection), [])
        # 恢复是单次代际事件：后续轮询代际保持稳定（无重复翻转/无轮询放大）。
        third = self._get_authentication()["data"]["connection"]["generation"]
        self.assertEqual(third, connection["generation"])


if __name__ == "__main__":
    unittest.main()
