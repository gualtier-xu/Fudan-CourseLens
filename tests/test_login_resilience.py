"""登录韧性：会话保活 nudge（Fix 5）与凭据拒绝分类（Fix 6）的行为测试。"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from path_utils import PROJECT_ROOT
import requests
from src.api.webvpn import (
    FudanAccountLocked,
    FudanChallengeRequired,
    FudanCredentialsRejected,
    FudanServiceMaintenance,
    WebVPNSession,
    account_locked,
    credentials_rejected,
    service_maintenance,
)
from src.application import _LOGIN_TERMINAL_MESSAGES, CourseLensApplication
from src.runtime.http_api import (
    FrontendSessionRegistry,
    MidRequestDisconnectLog,
    make_handler,
    nudge_auth_keepalive,
)
from src.runtime.network import (
    DEFAULT_PROXY,
    NetworkSettings,
    validate_vpn_connection_snapshot,
)
from tests.http_services import http_services


class KeepaliveNudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.app._set_login_status("ready", "icourse", "已连接", connected=True)
        self.app._client = object()

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def _wait_flag_reset(self) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with self.app._lock:
                if not self.app._client_nudge_in_flight:
                    return
            time.sleep(0.01)
        self.fail("nudge flag was not released")

    def _wait_calls(self, client: Mock, count: int) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and client.call_count < count:
            time.sleep(0.01)
        self.assertEqual(client.call_count, count)

    def test_fresh_window_spawns_no_verification(self):
        self.app._client_last_verified_at = time.monotonic() - 10.0
        self.app.client = Mock()
        self.app.nudge_client_verification()
        self.app.client.assert_not_called()
        self.assertFalse(self.app._client_nudge_in_flight)

    def test_expiring_window_verifies_once_then_resets_flag(self):
        self.app._client_last_verified_at = time.monotonic() - 250.0  # 剩余 50s
        self.app.client = Mock()
        self.app.nudge_client_verification()
        self._wait_calls(self.app.client, 1)
        self.app.client.assert_called_once_with(verify_before_reuse=True)
        self._wait_flag_reset()
        # 旗标复位后可再次触发（证明 finally 释放）
        self.app.nudge_client_verification()
        self._wait_calls(self.app.client, 2)

    def test_nudge_in_flight_is_not_duplicated(self):
        release = threading.Event()
        self.app._client_last_verified_at = time.monotonic() - 250.0

        def _blocked_client(**_kwargs):
            release.wait(timeout=2.0)

        self.app.client = _blocked_client
        self.app.nudge_client_verification()
        time.sleep(0.05)
        with self.app._lock:
            self.assertTrue(self.app._client_nudge_in_flight)
        self.app.nudge_client_verification()  # 在途时忽略
        release.set()

    def test_disconnected_or_missing_client_spawns_nothing(self):
        self.app._client_last_verified_at = time.monotonic() - 299.0
        self.app.client = Mock()
        self.app._client = None
        self.app.nudge_client_verification()
        self.app.client.assert_not_called()
        self.app._client = object()
        self.app._set_login_status("idle", "credentials", "等待登录", connected=False)
        self.app.nudge_client_verification()
        self.app.client.assert_not_called()


class _RaisingNudgeService:
    nudge_calls = 0

    def authentication_snapshot(self):
        return {"state": "ready", "code": "fudan_session_verified"}

    def nudge_client_verification(self):
        type(self).nudge_calls += 1
        raise RuntimeError("synthetic keepalive failure must not break heartbeats")


class FrontendSessionKeepaliveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _RaisingNudgeService.nudge_calls = 0
        frontend = PROJECT_ROOT / "frontend"
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(
                http_services(_RaisingNudgeService()), frontend,
                frontend_sessions=FrontendSessionRegistry(
                    lease_seconds=30, shutdown_grace_seconds=30, poll_seconds=1,
                ),
            ),
        )
        # FLAKYFIX-1（同族 fixture 卫生）：daemon_threads=False 让 tearDownClass
        # 的 server_close() 收齐在途 handler 线程——消除满载下在途 socket 与
        # 关停竞争的 WinError 10038 线程警告族源头（该族历史上常被误挂在
        # join 闪红上）。端点均为有界小响应，收齐无挂起面。
        cls.server.daemon_threads = False
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _post_session(self, action: str) -> dict:
        from urllib.request import Request, urlopen

        request = Request(
            f"{self.base}/api/v3/frontend-session",
            data=json.dumps({"session_id": "synthetic", "action": action}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read().decode("utf-8"))

    def test_heartbeat_survives_raising_nudge_and_close_skips_it(self):
        envelope = self._post_session("heartbeat")
        self.assertEqual(envelope["schema"], "courselens.api.v3")
        self.assertEqual(_RaisingNudgeService.nudge_calls, 1)
        self._post_session("open")
        self.assertEqual(_RaisingNudgeService.nudge_calls, 2)
        self._post_session("close")
        self.assertEqual(_RaisingNudgeService.nudge_calls, 2, "close 不触发保活")

    def test_nudge_helper_swallows_all_exceptions(self):
        calls = []

        def raising():
            calls.append(1)
            raise ValueError("synthetic")

        nudge_auth_keepalive(raising)
        self.assertEqual(len(calls), 1)
        nudge_auth_keepalive(None)  # 无绑定 application 时直接跳过


class CredentialsRejectedClassificationTests(unittest.TestCase):
    @staticmethod
    def _vpn_with_payload(payload: dict) -> WebVPNSession:
        class _Response:
            status_code = 200

            def __init__(self, body: dict):
                self._body = body

            def json(self):
                return self._body

        vpn = WebVPNSession()
        vpn.session = Mock()
        vpn.session.post = Mock(return_value=_Response(payload))
        return vpn

    def test_password_error_payload_carries_closed_code(self):
        vpn = self._vpn_with_payload({"code": "AUTH0003", "msg": "密码错误"})
        with self.assertRaises(FudanCredentialsRejected) as caught:
            vpn._auth_execute("sid", "enc", "lck", "entity", "chain", "request")
        self.assertEqual(caught.exception.code, "fudan_credentials_rejected")
        vpn.session.close()

    def test_transient_payload_stays_plain_runtime_error(self):
        vpn = self._vpn_with_payload({"code": "AUTH5000", "msg": "系统繁忙，请稍后再试"})
        with self.assertRaises(RuntimeError) as caught:
            vpn._auth_execute("sid", "enc", "lck", "entity", "chain", "request")
        self.assertNotIsInstance(caught.exception, FudanCredentialsRejected)
        self.assertEqual(str(getattr(caught.exception, "code", "")), "")
        vpn.session.close()

    def test_marker_matcher_is_closed_set(self):
        self.assertTrue(credentials_rejected({"msg": "用户名或密码错误"}))
        self.assertTrue(credentials_rejected({"error_description": "账号不存在"}))
        self.assertFalse(credentials_rejected({"msg": ""}))
        self.assertFalse(credentials_rejected({"msg": "系统繁忙"}))
        self.assertFalse(credentials_rejected({"code": "AUTH0003"}))


class ChallengeAwareClassificationTests(CredentialsRejectedClassificationTests):
    """P1-A：账号锁定与维护是独立闭集，和凭据拒绝/挑战/网络故障互不混淆。"""

    def test_locked_payload_carries_closed_code(self):
        vpn = self._vpn_with_payload({"code": "AUTH0012", "msg": "密码连续错误，账号已锁定"})
        with self.assertRaises(FudanAccountLocked) as caught:
            vpn._auth_execute("sid", "enc", "lck", "entity", "chain", "request")
        self.assertEqual(caught.exception.code, "fudan_account_locked")
        vpn.session.close()

    def test_maintenance_payload_carries_closed_code(self):
        vpn = self._vpn_with_payload({"code": "AUTH5030", "message": "系统维护中，请稍后再试"})
        with self.assertRaises(FudanServiceMaintenance) as caught:
            vpn._auth_execute("sid", "enc", "lck", "entity", "chain", "request")
        self.assertEqual(caught.exception.code, "fudan_service_maintenance")
        vpn.session.close()

    def test_lock_and_maintenance_take_precedence_over_generic_handlers(self):
        # 锁定文案不含凭据标记：即便同时含“密码”字样也绝不被归为凭据拒绝。
        self.assertTrue(account_locked({"msg": "密码连续错误，账号已锁定"}))
        self.assertFalse(credentials_rejected({"msg": "账号已锁定"}))
        self.assertFalse(account_locked({"msg": "密码错误"}))

    def test_matchers_are_closed_sets(self):
        self.assertTrue(account_locked({"error_description": "该账户已被锁定"}))
        self.assertTrue(account_locked({"msg": "账号已冻结"}))
        self.assertTrue(service_maintenance({"msg": "正在维护"}))
        self.assertTrue(service_maintenance({"error": "System maintenance"}))
        self.assertFalse(account_locked({"msg": "系统繁忙"}))
        self.assertFalse(service_maintenance({"msg": "密码错误"}))
        self.assertFalse(service_maintenance({"code": "AUTH5030"}))


class LoginRetryOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.service = CourseLensApplication(Path(self.temporary.name))
        self.service.set_credentials("student", "password")
        self.snapshots_during_retry: list[dict] = []
        self.attempts = 0

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def _run_retry(self, outcome: BaseException, max_attempts: int = 3) -> None:
        counter = {"count": 0}

        class _CountingVpn:
            def __init__(self, **_kwargs):
                self.session = Mock()

            def login(self, **_kwargs):
                counter["count"] += 1
                raise outcome

        def _record_snapshot(_seconds: float) -> None:
            self.snapshots_during_retry.append(self.service.authentication_snapshot())

        with patch("src.api.webvpn.WebVPNSession", _CountingVpn), \
                patch("time.sleep", _record_snapshot):
            with self.assertRaises(Exception) as caught:
                self.service._login_with_retry(max_attempts=max_attempts)
        self.assertIs(caught.exception, outcome)
        self.attempts = counter["count"]

    def test_credentials_rejected_breaks_after_first_attempt(self):
        self._run_retry(FudanCredentialsRejected("Authentication failed: {'msg': '密码错误'}"))
        self.assertEqual(self.attempts, 1, "凭据被拒只尝试一次，不做无意义重试")
        snapshot = self.service.authentication_snapshot()
        self.assertEqual(snapshot["state"], "degraded")
        self.assertEqual(snapshot["code"], "fudan_credentials_rejected")

    def test_transient_failure_still_exhausts_attempts(self):
        self._run_retry(RuntimeError("synthetic transient"))
        self.assertEqual(self.attempts, 3, "普通失败仍尝试满 3 次")
        snapshot = self.service.authentication_snapshot()
        self.assertEqual(snapshot["state"], "degraded")
        self.assertEqual(snapshot["code"], "fudan_login_failed")

    def test_retry_snapshot_exposes_step_attempt_max_attempts(self):
        self._run_retry(RuntimeError("synthetic transient"))
        self.assertTrue(self.snapshots_during_retry)
        snapshot = self.snapshots_during_retry[0]
        self.assertEqual(snapshot["state"], "checking")
        self.assertEqual(snapshot["code"], "fudan_session_checking")
        self.assertEqual(snapshot["step"], "webvpn")
        self.assertEqual(snapshot["attempt"], 1)
        self.assertEqual(snapshot["max_attempts"], 3)

    def test_terminal_snapshot_has_no_retry_progress_keys(self):
        self._run_retry(RuntimeError("synthetic transient"))
        snapshot = self.service.authentication_snapshot()
        for key in ("step", "attempt", "max_attempts"):
            self.assertNotIn(key, snapshot)


class _FakeCredentialStore:
    """与 CredentialStore 闭集行为一致的测试替身：不落真实秘密、不走 DPAPI。"""

    def __init__(self, accounts=None, secrets=None):
        self._accounts = dict(accounts or {})
        self.secrets = dict(secrets or {})
        self.deleted_accounts: list[str] = []
        self.checkpoints: dict[str, list] = {}
        self.cleared_all = 0

    def list_accounts(self):
        return [dict(value) for value in self._accounts.values()]

    def load(self, student_id):
        account = self._accounts.get(student_id)
        if not account:
            raise KeyError("Saved user not found")
        if account.get("requires_rotation") is True:
            raise RuntimeError("保存的密码需要更新，请重新输入新密码")
        return student_id, "synthetic-not-a-real-password"

    def delete_account(self, student_id):
        self.deleted_accounts.append(student_id)
        self.checkpoints.pop(student_id, None)
        return self._accounts.pop(student_id, None) is not None

    def has_secret(self, name):
        return name in self.secrets

    def delete_secret(self, name):
        return self.secrets.pop(name, None) is not None

    def save_secret(self, name, value):
        self.secrets[name] = value

    # --- 会话检查点（V5）：与 CredentialStore 相同的 fail-closed 语义 ---

    def save_session_checkpoint(self, student_id, cookies):
        account = self._accounts.get(student_id)
        if not isinstance(account, dict) or account.get("requires_rotation") is True:
            self.checkpoints.pop(student_id, None)
            return
        self.checkpoints[student_id] = [dict(item) for item in cookies]

    def load_session_checkpoint(self, student_id):
        account = self._accounts.get(student_id)
        if not isinstance(account, dict) or account.get("requires_rotation") is True:
            self.checkpoints.pop(student_id, None)
            return None
        cookies = self.checkpoints.get(student_id)
        return [dict(item) for item in cookies] if cookies else None

    def clear_session_checkpoint(self, student_id):
        return self.checkpoints.pop(student_id, None) is not None

    def clear_all_session_checkpoints(self):
        removed = len(self.checkpoints)
        self.checkpoints.clear()
        self.cleared_all += 1
        return removed


class AutoConnectPreferenceTests(unittest.TestCase):
    """启动自动连接偏好：校验闭集、持久化、快照无秘密。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.app.credentials = _FakeCredentialStore(
            accounts={
                "20301080001": {"student_id": "20301080001", "requires_rotation": False},
                "20301080002": {"student_id": "20301080002", "requires_rotation": True},
            },
        )

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def test_default_preference_is_off_and_versioned(self):
        snapshot = self.app.auto_connect_snapshot()
        self.assertEqual(snapshot["schema"], "courselens.auto-connect.v1")
        self.assertEqual(snapshot["fudan"], {"enabled": False, "account_id": "", "status": "off"})
        self.assertEqual(snapshot["github"], {"enabled": False, "status": "off"})

    def test_enable_requires_explicit_selectable_account(self):
        from src.application import AutoConnectPreferenceError

        for request, code in (
            ({"fudan": {"enabled": True, "account_id": ""}}, "auto_connect_account_required"),
            ({"fudan": {"enabled": True, "account_id": "ghost"}}, "auto_connect_account_missing"),
            ({"fudan": {"enabled": True, "account_id": "20301080002"}}, "auto_connect_account_rotation_required"),
            ({"github": {"enabled": True}}, "auto_connect_github_grant_missing"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(AutoConnectPreferenceError) as caught:
                    self.app.set_auto_connect_preference(request)
                self.assertEqual(caught.exception.code, code)
        # 被拒的开启请求绝不静默启用或切换账号
        snapshot = self.app.auto_connect_snapshot()
        self.assertFalse(snapshot["fudan"]["enabled"])
        self.assertFalse(snapshot["github"]["enabled"])

    def test_enable_persists_and_survives_reopening_the_store(self):
        self.app.set_auto_connect_preference({"fudan": {"enabled": True, "account_id": "20301080001"}})
        snapshot = self.app.auto_connect_snapshot()
        self.assertEqual(snapshot["fudan"]["status"], "ready")
        # 持久化在 app-state 存储：新进程（同数据目录）读回同一偏好
        reopened = CourseLensApplication(Path(self.temporary.name))
        try:
            self.assertTrue(reopened.auto_connect_snapshot()["fudan"]["enabled"])
        finally:
            reopened.close()

    def test_disabling_touches_no_credentials_and_no_logout(self):
        self.app.credentials.secrets["github_app_access_token"] = "synthetic"
        self.app.set_auto_connect_preference({"fudan": {"enabled": True, "account_id": "20301080001"}})
        self.app.set_auto_connect_preference({"github": {"enabled": True}})
        self.app.logout_fudan = Mock()
        self.app.set_auto_connect_preference({"fudan": {"enabled": False}, "github": {"enabled": False}})
        self.assertEqual(self.app.credentials.deleted_accounts, [], "关闭偏好不删除账号")
        self.assertEqual(self.app.credentials.secrets.get("github_app_access_token"), "synthetic", "关闭偏好不删除 GitHub 授权")
        self.app.logout_fudan.assert_not_called()
        snapshot = self.app.auto_connect_snapshot()
        self.assertEqual(snapshot["fudan"]["status"], "off")
        self.assertEqual(snapshot["github"]["status"], "off")

    def test_snapshots_are_non_secret(self):
        self.app.set_auto_connect_preference({"fudan": {"enabled": True, "account_id": "20301080001"}})
        auto = self.app.auto_connect_snapshot()
        self.assertEqual(self.app.settings_privacy_snapshot()["auto_connect"], auto)
        text = json.dumps(auto, ensure_ascii=False).casefold()
        for forbidden in ("password", "token", "cookie"):
            self.assertNotIn(forbidden, text)


class AutoConnectResumeTests(unittest.TestCase):
    """启动自动连接恢复：每进程至多一次、不阻塞、闭集失败停止、零自动外联动作。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.app.credentials = _FakeCredentialStore(
            accounts={
                "20301080001": {"student_id": "20301080001", "requires_rotation": False},
                "20301080002": {"student_id": "20301080002", "requires_rotation": False},
                "20301080003": {"student_id": "20301080003", "requires_rotation": True},
            },
        )
        self.app.github_app = Mock()
        self.used_accounts: list[str] = []
        self.refresh_calls = 0
        release = threading.Event()

        def _use_saved(student_id):
            release.wait(timeout=5)
            self.used_accounts.append(student_id)

        def _refresh():
            release.wait(timeout=5)
            self.refresh_calls += 1
            return {"state": "checking"}

        self.app.use_saved_credentials = _use_saved
        self.app.refresh_authorized_catalog_async = _refresh
        self.release = release

    def tearDown(self) -> None:
        self.release.set()
        self.app.close()
        self.temporary.cleanup()

    def _wait_for(self, condition, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.01)
        return False

    def test_disabled_preference_starts_nothing(self):
        result = self.app.start_auto_connect_resume()
        self.assertEqual(result, {"state": "off"})
        self.assertEqual(self.used_accounts, [])
        self.assertEqual(self.refresh_calls, 0)

    def test_resume_loads_only_the_selected_account(self):
        self.app.set_auto_connect_preference({"fudan": {"enabled": True, "account_id": "20301080001"}})
        self.app.start_auto_connect_resume()
        self.release.set()
        self.assertTrue(self._wait_for(lambda: self.refresh_calls >= 1), "恢复应触发既有登录/目录刷新")
        self.assertEqual(self.used_accounts, ["20301080001"], "只加载所选账号，绝不切换")

    def test_rotation_required_stops_fail_closed_without_login(self):
        # 开启时账号有效；此后密码被标记需轮换（偏好不自动失效），恢复必须止步
        self.app.set_auto_connect_preference({"fudan": {"enabled": True, "account_id": "20301080001"}})
        self.app.credentials._accounts["20301080001"]["requires_rotation"] = True
        self.app.start_auto_connect_resume()
        self.release.set()
        self.assertTrue(self._wait_for(lambda: self.app.auto_connect_snapshot()["last_resume"]["fudan"]["code"]))
        self.assertEqual(self.used_accounts, [], "需轮换凭据绝不发起登录")
        self.assertEqual(self.refresh_calls, 0)
        snapshot = self.app.auto_connect_snapshot()
        self.assertEqual(snapshot["last_resume"]["fudan"]["code"], "fudan_resume_rotation_required")
        self.assertEqual(snapshot["fudan"]["status"], "rotation_required")
        self.assertTrue(snapshot["fudan"]["enabled"], "停止不回改用户偏好")

    def test_missing_account_stops_fail_closed(self):
        self.app.set_auto_connect_preference({"fudan": {"enabled": True, "account_id": "20301080001"}})
        self.app.credentials._accounts.pop("20301080001")
        self.app.start_auto_connect_resume()
        self.release.set()
        self.assertTrue(self._wait_for(
            lambda: self.app.auto_connect_snapshot()["last_resume"]["fudan"]["code"] == "fudan_resume_account_missing",
        ))

    def test_startup_is_once_per_process_and_non_blocking(self):
        release = threading.Event()
        runs = []

        def _slow_run(_preference):
            release.wait(timeout=5)
            runs.append(1)

        self.app.set_auto_connect_preference({"fudan": {"enabled": True, "account_id": "20301080001"}})
        self.app._run_auto_connect_resume = _slow_run
        began = time.monotonic()
        result = self.app.start_auto_connect_resume()
        elapsed = time.monotonic() - began
        self.assertLess(elapsed, 1.0, "启动恢复不得阻塞调用方")
        self.assertEqual(result["state"], "started")
        again = self.app.start_auto_connect_resume()
        self.assertEqual(again, {"state": "already_started"}, "每进程至多一次")
        release.set()
        self.assertTrue(self._wait_for(lambda: bool(runs)))

    def test_transient_failure_keeps_preference_and_surfaces_single_outcome(self):
        def _raise(_student_id):
            raise RuntimeError("synthetic transient failure")

        self.app.use_saved_credentials = _raise
        self.app.set_auto_connect_preference({"fudan": {"enabled": True, "account_id": "20301080001"}})
        self.app.start_auto_connect_resume()
        self.release.set()
        self.assertTrue(self._wait_for(
            lambda: self.app.auto_connect_snapshot()["last_resume"]["fudan"]["code"] == "fudan_resume_failed",
        ))
        self.assertTrue(self.app.auto_connect_snapshot()["fudan"]["enabled"], "瞬时失败保留偏好等待手动重试")
        self.assertEqual(self.app.auto_connect_snapshot()["last_resume"]["fudan"]["state"], "stopped")

    def test_github_resume_is_read_only_verify(self):
        self.app.credentials.secrets["github_app_access_token"] = "synthetic"
        self.app.set_auto_connect_preference({"github": {"enabled": True}})
        self.app.github_app.verify_user_authorization.return_value = {"authorized": True, "installed": True}
        self.app.start_auto_connect_resume()
        self.release.set()
        self.assertTrue(self._wait_for(
            lambda: self.app.auto_connect_snapshot()["last_resume"]["github"]["code"] == "github_resume_verified",
        ))
        self.app.github_app.verify_user_authorization.assert_called_once()

    def test_github_resume_failures_stop_without_retry(self):
        from src.remote.github_app import GitHubAppError

        self.app.credentials.secrets["github_app_access_token"] = "synthetic"
        self.app.set_auto_connect_preference({"github": {"enabled": True}})
        for exc, code in (
            (GitHubAppError("GitHub 授权已过期，请重新连接"), "github_resume_failed"),
            (OSError("network unavailable"), "github_resume_unavailable"),
        ):
            with self.subTest(code=code):
                self.app.credentials.secrets["github_app_access_token"] = "synthetic"
                self.app._auto_connect_started = False
                self.app.github_app.verify_user_authorization.reset_mock()
                self.app.github_app.verify_user_authorization.side_effect = exc
                self.app.start_auto_connect_resume()
                self.release.set()
                self.assertTrue(self._wait_for(
                    lambda: self.app.auto_connect_snapshot()["last_resume"]["github"]["code"] == code,
                ))
                self.assertEqual(self.app.github_app.verify_user_authorization.call_count, 1, "失败不循环重试")

    def test_github_resume_without_grant_never_calls_verification(self):
        # 开启时授权存在；此后授权被清除（如重新授权流程），恢复必须止步且零调用
        self.app.credentials.secrets["github_app_access_token"] = "synthetic"
        self.app.set_auto_connect_preference({"github": {"enabled": True}})
        self.app.credentials.secrets.pop("github_app_access_token")
        self.app.start_auto_connect_resume()
        self.release.set()
        self.assertTrue(self._wait_for(
            lambda: self.app.auto_connect_snapshot()["last_resume"]["github"]["code"] == "github_resume_grant_missing",
        ))
        self.app.github_app.verify_user_authorization.assert_not_called()

    def test_resume_performs_zero_automatic_setup_actions(self):
        self.app.credentials.secrets["github_app_access_token"] = "synthetic"
        self.app.set_auto_connect_preference({
            "fudan": {"enabled": True, "account_id": "20301080001"},
            "github": {"enabled": True},
        })
        forbidden = {
            name: Mock()
            for name in (
                "start_github_device_authorization", "poll_github_device_authorization",
                "bootstrap_github", "verify_github_worker", "repair_github_worker",
                "start_remote_echo",
            )
        }
        for name, mock in forbidden.items():
            setattr(self.app, name, mock)
        with patch("webbrowser.open") as browser_open:
            self.app.start_auto_connect_resume()
            self.release.set()
            self.assertTrue(self._wait_for(lambda: self.refresh_calls >= 1))
            self.assertTrue(self._wait_for(
                lambda: self.app.auto_connect_snapshot()["last_resume"]["github"]["code"],
            ))
            for name, mock in forbidden.items():
                mock.assert_not_called()
            browser_open.assert_not_called()

    def test_close_stops_and_joins_probe_and_resume_threads(self):
        # T1 回归钉：close() 必须有界收口 start_auto_connect_resume 派生的
        # connection-path-probe / auto-connect-resume 线程——tearDown 清理
        # 临时目录时它们对 state.db 的瞬态连接即 WinError 32/145/267 根因。
        # P63：收口面纳入同点的 worker-auto-sync（P59 派生）；三处等待上限
        # 5s→15s——满载闪红两窗（P58/P62）均为宿主机停顿敏感、单跑恒绿，
        # 且 closer.join 原值 5s 倒挂在 close 自身 6.0s 预算之内；放宽只加
        # 容忍不改断言语义。
        # FN10（第 6 次满载闪红，P57/58/62/63/FN9 同族）：真竞态=close() 对
        # 每线程的 join 帽仅 1.0s（application.py `_join_budget` 处
        # min(1.0, …)），而被钉腿原先 `release.wait(timeout=5)` 只认主线程的
        # release——满载 GIL 饥饿下主线程 release.set() 晚于 close 的 1s join
        # 帽即被记 "timeout"。修法仍循测内宽限先例：被钉腿改为轮询
        # release **或** 本线程停事件，任一即退——close 置停后 ≤50ms 腿必醒，
        # 1s 帽内必 join 到；「close 开始收口时腿仍在工作腿内」的被测语义
        # 不变，只移除对主线程调度时序的依赖。
        # FLAKYFIX-1：`entered` 由三腿共享单事件改为三腿各自事件——closer
        # 启动前必须 probe/resume/sync **全部**已进 parked 工作腿（各 30s
        # 有界等待）。此前任一腿进场即放行 close()，满载 GIL 饥饿下慢腿未
        # 进场时 close() 的每线程 1.0s join 帽（application.py close 各线程
        # min(1.0, …)）必超、phases 记 "timeout" 伪红——FN10（6857bc6）只修
        # 了「已进场腿的唤醒」，未修「未进场腿的进场时序」，故 10-06~10-08
        # 仍连闪红。被测语义不变：「close 开始收口时腿已在工作腿内」从
        # 「至少一腿」钉成「三腿全部」，帽内收口由构造保证。
        entered_probe = threading.Event()
        entered_resume = threading.Event()
        entered_sync = threading.Event()
        release = threading.Event()

        def _park_until(stop_event):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if release.wait(timeout=0.05):
                    return
                if stop_event is not None and stop_event.is_set():
                    return

        def _parked_refresh(_service):
            entered_probe.set()
            _park_until(getattr(self.app, "_connection_probe_stop", None))

        def _parked_resume(_preference):
            entered_resume.set()
            _park_until(getattr(self.app, "_auto_connect_resume_stop", None))

        def _parked_sync():
            entered_sync.set()
            _park_until(getattr(self.app, "_worker_auto_sync_stop", None))

        self.app.network.refresh_route_decision = _parked_refresh
        self.app._run_auto_connect_resume = _parked_resume
        self.app.github_app.ensure_worker_trusted.side_effect = _parked_sync
        self.app.set_auto_connect_preference(
            {"fudan": {"enabled": True, "account_id": "20301080001"}}
        )
        self.assertEqual(self.app.start_auto_connect_resume()["state"], "started")
        probe = self.app._connection_probe_thread
        resume = self.app._auto_connect_resume_thread
        sync = self.app._worker_auto_sync_thread
        self.assertIsNotNone(probe)
        self.assertIsNotNone(resume)
        self.assertIsNotNone(sync)
        # FN9 既定指令（闪红直接改测内宽限不再豁免）：本成员在全量后段第 5 次
        # 闪红（P57/P58/P62/P63/FN9 同族），单跑恒绿——三处 15s 上限 30s，
        # 只加容忍不改断言语义。
        for leg_name, entered_leg in (
            ("probe", entered_probe), ("resume", entered_resume), ("sync", entered_sync),
        ):
            self.assertTrue(
                entered_leg.wait(timeout=30),
                f"{leg_name} 应已进入被钉住的工作腿（close 前三腿全部就绪门）",
            )
        closed: list[dict] = []
        closer = threading.Thread(
            target=lambda: closed.append(self.app.close()), daemon=True
        )
        closer.start()
        self.assertTrue(
            self.app._connection_probe_stop.wait(timeout=30),
            "close() 应先置停事件再有界 join",
        )
        self.assertTrue(self.app._auto_connect_resume_stop.is_set())
        self.assertTrue(self.app._worker_auto_sync_stop.is_set())
        release.set()
        closer.join(timeout=30)
        self.assertFalse(closer.is_alive(), "close() 不应被后台线程无限拖住")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["phases"]["connection_probe"], "stopped")
        self.assertEqual(closed[0]["phases"]["auto_connect_resume"], "stopped")
        self.assertEqual(closed[0]["phases"]["worker_auto_sync"], "stopped")
        self.assertFalse(probe.is_alive())
        self.assertFalse(resume.is_alive())
        self.assertFalse(sync.is_alive())
        alive = {thread.name for thread in threading.enumerate()}
        self.assertNotIn("connection-path-probe", alive)
        self.assertNotIn("auto-connect-resume", alive)
        self.assertNotIn("worker-auto-sync", alive)

    def test_close_stops_and_joins_session_self_heal_thread(self):
        # N9-H（P63 停车场销项）：自愈看门狗（SRC-SYNDROME-1 派生）纳入 close
        # 收口面——置停先于 client 关停、有界 join、超时放弃语义与同族一致；
        # 置停后不得再发起自愈 tick（停机途中零自愈外联）。
        entered = threading.Event()
        release = threading.Event()
        ticks: list[float] = []

        def _parked_once(backoff=None):
            # REALRUN-FIX-1：看门狗循环现以实例态推进（无参调用，恢复事件可在
            # tick 之间复位退避）——替身签名随产品契约同步，钉测意图不变。
            entered.set()
            release.wait(timeout=5)
            ticks.append(backoff)
            return backoff

        self.app._session_self_heal_once = _parked_once
        with patch("src.application.SESSION_SELF_HEAL_TICK_SECONDS", 0.05):
            # REALRUN-FIX-1：tick 节奏现读实例态退避（__init__ 以未打补丁常量
            # 播种）——按补丁值同步播种，快 tick 钉测语义不变。
            self.app._session_self_heal_backoff = 0.05
            self.app._start_session_self_heal()
            heal = self.app._session_self_heal_thread
            self.assertIsNotNone(heal)
            self.assertTrue(entered.wait(timeout=15), "self-heal 应已进入被钉住的 tick")
            closed: list[dict] = []
            closer = threading.Thread(
                target=lambda: closed.append(self.app.close()), daemon=True
            )
            closer.start()
            self.assertTrue(
                self.app._session_self_heal_stop.wait(timeout=15),
                "close() 应先置停事件再有界 join",
            )
            release.set()
            closer.join(timeout=15)
        self.assertFalse(closer.is_alive(), "close() 不应被后台线程无限拖住")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["phases"]["session_self_heal"], "stopped")
        self.assertFalse(heal.is_alive())
        alive = {thread.name for thread in threading.enumerate()}
        self.assertNotIn("session-self-heal", alive)
        # REALRUN-FIX-1：循环以实例态推进（无参调用 once）——恰一 tick 且置停
        # 后零再发的不变量保持，记录值为 None 即新契约形状。
        self.assertEqual(ticks, [None], "置停后不得再发起自愈 tick（停机途中零外联）")


class RouteFallbackLoginTests(unittest.TestCase):
    """P0.2：票前传输失败恰换一次路径；终止性失败绝不换路径、绝不重试。"""

    PROXY = "http://127.0.0.1:6268"

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.service = CourseLensApplication(Path(self.temporary.name))
        self.service.set_credentials("student", "password")
        self.ctor_kwargs: list[dict] = []

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()
        from src import application as application_module

        application_module._LAST_TICKET_SUCCESS.update(route=None, transport=None)

    def _run(self, outcomes, routes):
        test = self

        class _RecordingVpn:
            def __init__(self, **kwargs):
                test.ctor_kwargs.append(dict(kwargs))
                self.session = Mock()

            def login(self, **_kwargs):
                outcome = outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome

            def authenticate_icourse(self, **_kwargs):
                outcome = outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome

            def close(self):
                pass

        with patch("src.api.webvpn.WebVPNSession", _RecordingVpn), patch.object(
            self.service.network, "service_proxies", return_value=list(routes)
        ), patch("time.sleep"):
            try:
                self.service._login_with_retry(max_attempts=3)
            except Exception:
                pass

    def _proxies(self) -> list:
        return [kwargs.get("proxy_url") for kwargs in self.ctor_kwargs]

    def test_direct_failure_switches_to_proxy_once_then_stays(self):
        self._run(
            [requests.ConnectionError("direct down"), requests.ConnectionError("proxy down"), None, None],
            ["", self.PROXY],
        )
        self.assertEqual(self._proxies(), ["", self.PROXY, self.PROXY], "路径候选至多切换一次")

    def test_proxy_failure_switches_back_to_direct_once(self):
        self._run(
            [requests.ConnectionError("proxy down"), None, None],
            [self.PROXY, ""],
        )
        self.assertEqual(self._proxies(), [self.PROXY, ""])

    def test_credentials_rejected_never_switches_or_retries(self):
        self._run(
            [FudanCredentialsRejected("Authentication failed: {'msg': '密码错误'}")],
            ["", self.PROXY],
        )
        self.assertEqual(self._proxies(), [""])

    def test_challenge_required_never_switches_or_retries(self):
        self._run(
            [FudanChallengeRequired("需要安全验证 (code=AUTH0009)")],
            ["", self.PROXY],
        )
        self.assertEqual(self._proxies(), [""])
        snapshot = self.service.authentication_snapshot()
        self.assertEqual(snapshot["code"], "fudan_challenge_required")

    def test_account_locked_never_switches_or_retries(self):
        self._run(
            [FudanAccountLocked("账号已锁定 (code=AUTH0012)")],
            ["", self.PROXY],
        )
        self.assertEqual(len(self.ctor_kwargs), 1, "锁定态绝不重试")
        self.assertEqual(self._proxies(), [""], "锁定态绝不换路径")
        snapshot = self.service.authentication_snapshot()
        self.assertEqual(snapshot["code"], "fudan_account_locked")
        self.assertEqual(snapshot["actions"], ["login"])

    def test_service_maintenance_never_switches_or_retries(self):
        self._run(
            [FudanServiceMaintenance("系统维护中 (code=AUTH5030)")],
            ["", self.PROXY],
        )
        self.assertEqual(len(self.ctor_kwargs), 1, "维护态绝不重试")
        self.assertEqual(self._proxies(), [""], "维护态绝不换路径")
        snapshot = self.service.authentication_snapshot()
        self.assertEqual(snapshot["code"], "fudan_service_maintenance")

    def test_consumed_ticket_transport_failure_is_terminal(self):
        exc = RuntimeError("WebVPN ticket exchange failed (ConnectionError)")
        exc.code = "webvpn_ticket_transport_failed"
        self._run([exc], ["", self.PROXY])
        self.assertEqual(len(self.ctor_kwargs), 1, "ticket 已消费：绝不重试")
        self.assertEqual(self._proxies(), [""], "ticket 已消费：绝不换路径")

    def test_unsafe_redirect_is_terminal(self):
        exc = RuntimeError("Ticket redirect target is not allowed")
        exc.code = "fudan_redirect_unsafe"
        self._run([exc], ["", self.PROXY])
        self.assertEqual(len(self.ctor_kwargs), 1)
        self.assertEqual(self._proxies(), [""])

    def test_terminal_failure_clears_route_health_memory(self):
        """P3.1：凭据拒绝与路径无关——路由健康记忆（含活跃冷却）立即清场。"""
        self.assertTrue(self.service.network.note_route_failure("webvpn", ""))
        self._run(
            [FudanCredentialsRejected("Authentication failed: {'msg': '密码错误'}")],
            ["", self.PROXY],
        )
        self.assertEqual(self.service.network._route_health, {})

    def test_consumed_ticket_clears_route_health_memory(self):
        self.service.network.note_route_failure("webvpn", self.PROXY)
        exc = RuntimeError("WebVPN ticket exchange failed (ConnectionError)")
        exc.code = "webvpn_ticket_transport_failed"
        self._run([exc], ["", self.PROXY])
        self.assertEqual(len(self.ctor_kwargs), 1, "ticket 已消费：绝不重试")
        self.assertEqual(self.service.network._route_health, {})

    def test_login_success_records_verified_route(self):
        self._run([None, None], ["", self.PROXY])
        record = self.service.network._route_health.get("webvpn")
        self.assertIsNotNone(record)
        self.assertEqual(record["verified_route"], "direct")
        self.assertEqual(record["cooldown_until"], 0.0)

    def test_login_success_on_proxy_records_proxy_route(self):
        self._run([None, None], [self.PROXY, ""])
        record = self.service.network._route_health.get("webvpn")
        self.assertIsNotNone(record)
        self.assertEqual(record["verified_route"], "proxy")

    def test_repeated_transport_failures_arm_cooldown_once(self):
        """同一航班内交替传输失败：第一次获准翻转，第二次被抑制并计数。"""
        before = self.service.campus_metric_counters()["route_flap_suppressed"]
        self._run(
            [requests.ConnectionError("direct down"), requests.ConnectionError("proxy down"), None, None],
            ["", self.PROXY],
        )
        self.assertEqual(
            self.service.campus_metric_counters()["route_flap_suppressed"],
            before + 1,
            "冷却窗口内的重复翻转被抑制且只计一次",
        )


class HostResumeRouteStabilityTests(unittest.TestCase):
    """P3.1：宿主休眠恢复 = 路由代际事件；惰性判定、无轮询、不打断在途。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.service = CourseLensApplication(Path(self.temporary.name))
        self.service.set_credentials("student", "password")

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def _install_verified_client(self):
        old_client = Mock()
        old_client.check_alive.return_value = True
        with self.service._lock:
            self.service._client = old_client
            self.service._client_last_verified_at = time.monotonic()
            self.service._client_generation = self.service._route_generation
        self.service._set_login_status("ready", "icourse", "已连接", connected=True)
        return old_client

    def _observe_jump(self, *, wall_delta: float, mono_delta: float = 1.0) -> None:
        with self.service._lock:
            self.service._host_activity_observed = (
                time.monotonic() - mono_delta,
                time.time() - wall_delta,
            )

    def test_wall_clock_drift_bumps_generation_and_next_action_rebuilds(self):
        old_client = self._install_verified_client()
        self._observe_jump(wall_delta=3600.0)
        self.assertTrue(self.service._maybe_note_host_resume())
        self.assertGreater(
            self.service._route_generation, self.service._client_generation
        )
        # 恢复只标记代际：在途引用绝不被关闭。
        old_client.close.assert_not_called()
        new_client = Mock()
        with patch.object(
            self.service, "_login_with_retry", return_value=new_client
        ) as login:
            client = self.service.client()
        login.assert_called_once()
        self.assertIs(client, new_client)
        old_client.close.assert_called_once()

    def test_long_quiet_gap_is_treated_as_resume(self):
        self._install_verified_client()
        self._observe_jump(wall_delta=2000.0, mono_delta=2000.0)
        before = self.service.network.route_generation()
        self.assertTrue(self.service._maybe_note_host_resume())
        self.assertEqual(self.service.network.route_generation(), before + 1)

    def test_ordinary_rhythm_never_fires_resume(self):
        self.assertFalse(self.service._maybe_note_host_resume())
        before = self.service.network.route_generation()
        self.service._maybe_note_host_resume()  # 紧邻的第二次观察：无事件
        self.service.connection_snapshot()
        self.assertEqual(self.service.network.route_generation(), before)

    def test_resume_invalidates_route_decisions_and_health_memory(self):
        self.service.network.note_route_failure("webvpn", "")
        with patch.object(
            NetworkSettings,
            "_probe",
            side_effect=lambda url, proxy, *, timeout=None: {
                "healthy": True, "latency_ms": 5,
            },
        ):
            self.service.network.refresh_route_decision("icourse")
        self.assertEqual(
            self.service.network.service_proxies("webvpn"), [DEFAULT_PROXY, ""]
        )
        self._observe_jump(wall_delta=3600.0)
        self.service._maybe_note_host_resume()
        self.assertEqual(self.service.network._route_health, {})
        self.assertEqual(self.service.network._route_decisions, {})
        self.assertEqual(
            self.service.network.service_proxies("webvpn"), ["", DEFAULT_PROXY]
        )

    def test_resume_event_uses_note_resume_and_keeps_settings(self):
        self._observe_jump(wall_delta=3600.0)
        before = self.service.network.snapshot()
        self.assertTrue(self.service._maybe_note_host_resume())
        after = self.service.network.snapshot()
        self.assertEqual(after["mode"], before["mode"])
        self.assertEqual(after["proxy_url"], before["proxy_url"])
        self.assertEqual(after["route_generation"], before["route_generation"] + 1)


class SafeGetRecoveryWiringTests(unittest.TestCase):
    """P3.1 附件：client() 构建时绑定只读安全 GET 恢复钩子；恢复复用既有
    单航班纪元，终止性登录失败折损为 None（绝不额外重试）。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.service = CourseLensApplication(Path(self.temporary.name))
        self.service.set_credentials("student", "password")

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def test_login_path_binds_safe_get_recovery_hook(self):
        vpn_client = Mock()
        vpn_client.vpn = WebVPNSession()
        with patch.object(self.service, "_login_with_retry", return_value=vpn_client):
            client = self.service.client()
        self.assertIs(client, vpn_client)
        self.assertEqual(
            vpn_client.vpn.safe_get_recovery,
            self.service._vpn_for_safe_get_recovery,
        )

    def test_restore_path_binds_safe_get_recovery_hook_too(self):
        restored = Mock()
        restored.vpn = WebVPNSession()
        restored.vpn.logged_in = True
        with patch.object(
            self.service, "_restore_client_from_checkpoint", return_value=restored
        ):
            client = self.service.client()
        self.assertIs(client, restored)
        self.assertEqual(
            restored.vpn.safe_get_recovery,
            self.service._vpn_for_safe_get_recovery,
        )

    def test_recovery_returns_verified_vpn_of_current_epoch(self):
        live_client = Mock()
        live_client.check_alive.return_value = True
        with self.service._lock:
            self.service._client = live_client
            self.service._client_last_verified_at = time.monotonic()
            self.service._client_generation = self.service._route_generation
        self.service._set_login_status("ready", "icourse", "已连接", connected=True)
        vpn = self.service._vpn_for_safe_get_recovery()
        self.assertIs(vpn, live_client.vpn)

    def test_terminal_login_failure_degrades_recovery_to_none(self):
        self.service._restore_client_from_checkpoint = Mock(return_value=None)
        with patch.object(
            self.service,
            "_login_with_retry",
            side_effect=FudanCredentialsRejected("Authentication failed: {'msg': '密码错误'}"),
        ):
            self.assertIsNone(self.service._vpn_for_safe_get_recovery())
        # 终止性失败后快照保持诚实 fail-closed（具体闭集码映射由真实航班的
        # RouteFallbackLoginTests 覆盖）。
        snapshot = self.service.authentication_snapshot()
        self.assertEqual(snapshot["state"], "action_required")
        self.assertEqual(snapshot["code"], "fudan_login_required")


class SingleAuthFlightTests(unittest.TestCase):
    """P0.4：并发调用方共享同一次认证航班（成功与失败两个方向）。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.service = CourseLensApplication(Path(self.temporary.name))
        self.service.set_credentials("student", "password")

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def _install_blocking_vpn(self, attempts: dict, release: threading.Event, exc: Exception | None = None):
        test = self

        class _SlowVpn:
            AUTH_DEADLINE_SECONDS = 30

            def __init__(self, **_kwargs):
                self.session = Mock()

            def begin_authentication(self, _seconds):
                attempts["entered"] += 1
                release.wait(timeout=5)

            def end_authentication(self):
                return None

            def login(self, **_kwargs):
                attempts["count"] += 1
                if exc is not None:
                    raise exc

            def authenticate_icourse(self, **_kwargs):
                return None

        return _SlowVpn

    def _wait_waiters(self, count: int, timeout: float = 5.0) -> None:
        # FLAKYFIX-1：以产品排队观测器 `_refresh_waiters`（application.py
        # P0.4 单航班协议）替代盲 sleep——满载下 sleep(0.3) 可能早于第二
        # 调用方注册排队即放行 release。注意注册-消耗协议：first 在进入
        # login 前已把自己的注册数减回 0（entered 门保证此刻已进场），
        # 故飞行中 second 注册后计数恰为 count——读到它即证明 second 已
        # 注册，且 first 仍持刷新锁在场，second 绝不可能另起航班。
        # Event.wait 做节拍，免受 time.sleep 打补丁影响。断言语义零改动。
        tick = threading.Event()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.service._lock:
                if self.service._refresh_waiters >= count:
                    return
            tick.wait(0.01)
        self.fail(f"排队调用方未在 {timeout}s 内注册（_refresh_waiters < {count}）")

    def test_concurrent_success_shares_one_login(self):
        attempts = {"count": 0, "entered": 0}
        release = threading.Event()
        results: list = []
        errors: list = []

        def _submit():
            try:
                results.append(self.service.client())
            except Exception as exc:
                errors.append(exc)

        with patch("src.api.webvpn.WebVPNSession", self._install_blocking_vpn(attempts, release)):
            first = threading.Thread(target=_submit)
            first.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and attempts["entered"] < 1:
                time.sleep(0.01)
            second = threading.Thread(target=_submit)
            second.start()
            self._wait_waiters(1)  # 第二个调用方已注册排队（first 飞行在途持锁）
            release.set()
            first.join(timeout=5)
            second.join(timeout=5)
        self.assertFalse(errors)
        self.assertEqual(attempts["count"], 1, "并发调用只允许一次真实登录")
        self.assertEqual(len(results), 2)
        self.assertIs(results[0], results[1])

    def test_concurrent_failure_shares_one_result_without_second_login(self):
        attempts = {"count": 0, "entered": 0}
        release = threading.Event()
        errors: list = []
        logins = attempts

        def _submit():
            try:
                self.service.client()
            except Exception as exc:
                errors.append(exc)

        vpn_cls = self._install_blocking_vpn(
            logins, release, requests.ConnectionError("synthetic outage")
        )
        with patch("src.api.webvpn.WebVPNSession", vpn_cls), patch("time.sleep"):
            first = threading.Thread(target=_submit)
            first.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and attempts["entered"] < 1:
                time.sleep(0.01)
            second = threading.Thread(target=_submit)
            second.start()
            self._wait_waiters(1)  # 第二个调用方已注册排队（first 飞行在途持锁）
            release.set()
            first.join(timeout=10)
            second.join(timeout=10)
        # 一次航班 = 一次 _login_with_retry（内部既有 3 次有界尝试全部属于同一航班）。
        self.assertEqual(attempts["count"], 3, "失败也只允许一次真实登录航班")
        self.assertEqual(len(errors), 2, "两个调用方都得到同一结果")
        self.assertIs(errors[0], errors[1])

    def test_network_settings_change_rebuilds_generation_stale_client(self):
        from src.api.icourse import ICourseClient

        old_client = Mock()
        old_client.check_alive.return_value = True
        new_client = Mock(spec=ICourseClient)
        new_client.vpn = object()
        with self.service._lock:
            self.service._client = old_client
            self.service._client_last_verified_at = time.monotonic()
            self.service._client_generation = self.service._route_generation
        self.service._set_login_status("ready", "icourse", "已连接", connected=True)
        with patch.object(
            self.service, "_login_with_retry", return_value=new_client
        ) as login:
            self.service.update_network_settings("manual", "http://127.0.0.1:6268")
            client = self.service.client()
        login.assert_called_once()
        self.assertIs(client, new_client)
        old_client.close.assert_called_once()
        self.assertEqual(
            self.service.authentication_snapshot()["state"], "ready"
        )

    def test_inflight_client_reference_survives_generation_bump(self):
        old_client = Mock()
        old_client.check_alive.return_value = True
        with self.service._lock:
            self.service._client = old_client
            self.service._client_last_verified_at = time.monotonic()
            self.service._client_generation = self.service._route_generation
        held = self.service.client()  # 在途引用
        self.assertIs(held, old_client)
        self.service.update_network_settings("direct")
        # 在途请求继续使用自己的引用；设置变更只标记过期，绝不关闭在途客户端。
        old_client.close.assert_not_called()


class ConnectionSnapshotTests(unittest.TestCase):
    """P0.1 后端半：courselens.vpn-connection.v1 闭集快照。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.service = CourseLensApplication(Path(self.temporary.name))
        self.service.set_credentials("synthetic-user", "synthetic-password")

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def _validated_snapshot(self) -> dict:
        snapshot = self.service.connection_snapshot()
        self.assertEqual(
            validate_vpn_connection_snapshot(snapshot), [],
            "生产快照必须逐字满足冻结 v1 契约",
        )
        return snapshot

    def test_login_required_without_evidence_is_honest(self):
        snapshot = self._validated_snapshot()
        self.assertEqual(snapshot["state"], "login_required")
        self.assertEqual(snapshot["actions"], ["login"])
        self.assertIsNone(snapshot["expires_at"])
        self.assertEqual(snapshot["services"]["webvpn"]["verified"], False)
        self.assertEqual(snapshot["services"]["icourse"]["verified"], False)
        self.assertEqual(snapshot["network_path"], "unknown")

    def test_ready_snapshot_after_verified_session(self):
        client = Mock()
        client.check_alive.return_value = True
        with self.service._lock:
            self.service._client = client
            self.service._client_last_verified_at = time.monotonic()
            self.service._client_generation = self.service._route_generation
            self.service._webvpn_route = "direct"
            self.service._icourse_route_class = "webvpn"
        self.service._set_login_status("ready", "icourse", "已连接", connected=True)
        snapshot = self._validated_snapshot()
        self.assertEqual(snapshot["state"], "ready")
        self.assertEqual(snapshot["reason"], "direct_ok")
        self.assertEqual(snapshot["school_route"], "webvpn")
        self.assertEqual(snapshot["network_path"], "direct")
        self.assertEqual(snapshot["actions"], [])
        self.assertIsNotNone(snapshot["expires_at"])
        self.assertGreaterEqual(snapshot["expires_at"], snapshot["observed_at"])
        self.assertTrue(snapshot["services"]["webvpn"]["verified"])
        self.assertTrue(snapshot["services"]["icourse"]["verified"])

    def test_tun_hint_requires_bounded_route_evidence(self):
        # 证据支持：直连死、显式代理活 → 才允许 TUN 提示与关闭 TUN 动作。
        with patch.object(
            self.service.network,
            "route_evidence",
            side_effect=lambda service: {
                "decision": "proxy", "direct_ok": False, "proxy_ok": True,
            },
        ):
            snapshot = self._validated_snapshot()
        self.assertEqual(snapshot["reason"], "possible_tun_interference")
        self.assertIn("close-tun-and-retry", snapshot["actions"])

        # 无证据/证据中性：一律中性网络措辞，绝不猜 TUN。
        for evidence in (
            {"decision": "unknown", "direct_ok": None, "proxy_ok": None},
            {"decision": "direct", "direct_ok": True, "proxy_ok": True},
            {"decision": "direct", "direct_ok": False, "proxy_ok": False},
        ):
            with patch.object(self.service.network, "route_evidence", return_value=evidence):
                snapshot = self._validated_snapshot()
            self.assertNotEqual(snapshot["reason"], "possible_tun_interference")
            self.assertNotIn("close-tun-and-retry", snapshot["actions"])

    def test_probe_evidence_without_session_reports_network_or_login(self):
        cases = [
            ({"decision": "unknown", "direct_ok": False, "proxy_ok": False}, "network_unavailable"),
            ({"decision": "direct", "direct_ok": True, "proxy_ok": None}, "login_required"),
        ]
        for evidence, expected in cases:
            with patch.object(self.service.network, "route_evidence", return_value=evidence):
                snapshot = self._validated_snapshot()
            self.assertEqual(snapshot["state"], expected, evidence)

    def test_challenge_lock_maintenance_map_to_distinct_closed_states(self):
        """P1-A：每个失败原因 → 恰一个闭集状态 + 恰一个人类动作。"""
        expectations = {
            "fudan_credentials_rejected": ("login_required", "credentials_rejected", ["login"]),
            "fudan_challenge_required": ("challenge_required", "challenge", ["login"]),
            "fudan_account_locked": ("login_required", "credentials_rejected", ["open-settings"]),
            "fudan_service_maintenance": ("network_unavailable", "service_unavailable", ["retry"]),
        }
        for error_code, (state, reason, actions) in expectations.items():
            self.service._set_login_status(
                "error", "webvpn", _LOGIN_TERMINAL_MESSAGES[error_code], error_code=error_code,
            )
            snapshot = self._validated_snapshot()
            self.assertEqual(snapshot["state"], state, error_code)
            self.assertEqual(snapshot["reason"], reason, error_code)
            self.assertEqual(snapshot["actions"], actions, error_code)

    def test_maintenance_resists_tun_hint_overwrite(self):
        """维护是服务端事实：即使存在 TUN 证据也绝不改写为“可能受 TUN 影响”。"""
        with patch.object(
            self.service.network,
            "route_evidence",
            side_effect=lambda service: {"decision": "proxy", "direct_ok": False, "proxy_ok": True},
        ):
            self.service._set_login_status(
                "error", "webvpn", "校园服务暂时维护", error_code="fudan_service_maintenance",
            )
            snapshot = self._validated_snapshot()
        self.assertEqual(snapshot["reason"], "service_unavailable")
        self.assertEqual(snapshot["actions"], ["retry"])

    def test_snapshot_is_closed_set_and_secret_free(self):
        client = Mock()
        with self.service._lock:
            self.service._client = client
            self.service._client_last_verified_at = time.monotonic()
            self.service._client_generation = self.service._route_generation
            self.service._webvpn_route = "local_proxy"
        self.service._set_login_status("ready", "icourse", "已连接", connected=True)
        snapshot = self._validated_snapshot()
        text = json.dumps(snapshot, ensure_ascii=False).casefold()
        for forbidden in (
            "http", "password", "cookie", "token", "lck", "synthetic-user",
        ):
            self.assertNotIn(forbidden, text)

    def test_app_shell_publishes_connection_additively(self):
        shell = self.service.app_shell_snapshot()
        self.assertIn("connection", shell)
        self.assertEqual(
            validate_vpn_connection_snapshot(shell["connection"]), []
        )
        # 既有键保持不变：旧前端忽略新字段继续工作。
        for key in ("schema", "session", "authentication", "catalog", "settings"):
            self.assertIn(key, shell)
        self.assertEqual(shell["schema"], "courselens.app-shell.v2")

    def test_lifted_validator_matches_frozen_fixture(self):
        fixture_path = (
            Path(__file__).resolve().parent / "fixtures" / "vpn_connection_contract_v1.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(set(fixture["valid"]), {
            "ready_direct", "checking_cold_start", "degraded_proxy_fallback",
            "login_required", "expired_session",
        })
        for name, example in fixture["valid"].items():
            self.assertEqual(
                validate_vpn_connection_snapshot(example), [], name
            )

    def test_generation_reflects_route_generation_and_is_monotonic(self):
        first = self._validated_snapshot()["generation"]
        self.service.update_network_settings("direct")
        second = self._validated_snapshot()["generation"]
        self.service.update_network_settings("manual", "http://127.0.0.1:6268")
        third = self._validated_snapshot()["generation"]
        self.assertEqual(first, 0)
        self.assertLess(first, second)
        self.assertLess(second, third)

    def test_legacy_settings_without_generation_stay_compatible(self):
        # legacy app-state：无 route_generation 键 → 读作 0，快照照常构造。
        self.service.task_store.set_app_state("network_settings", {
            "mode": "auto", "proxy_url": "http://127.0.0.1:6268", "routes": {},
        })
        self.assertEqual(self.service.network.route_generation(), 0)
        snapshot = self._validated_snapshot()
        self.assertEqual(snapshot["generation"], 0)


class ConnectionProbeTests(unittest.TestCase):
    """P0.5：启动探测每进程至多一次、绝不阻塞、不改变既有返回值。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.service = CourseLensApplication(Path(self.temporary.name))
        self.probed: list[str] = []
        release = threading.Event()

        def _slow_probe(service_name):
            release.wait(timeout=5)
            self.probed.append(service_name)
            return {"decision": "unknown", "direct_ok": None, "proxy_ok": None}

        self.service.network.refresh_route_decision = _slow_probe
        self.release = release

    def tearDown(self) -> None:
        self.release.set()
        self.service.close()
        self.temporary.cleanup()

    def _wait_probed(self, count: int) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(self.probed) < count:
            time.sleep(0.01)
        self.assertEqual(len(self.probed), count)

    def test_probe_is_once_per_process_and_non_blocking(self):
        began = time.monotonic()
        result = self.service.start_connection_probe()
        self.assertLess(time.monotonic() - began, 1.0, "探测绝不阻塞调用方")
        self.assertEqual(result, {"state": "started"})
        self.assertEqual(self.service.start_connection_probe(), {"state": "already_started"})
        self.release.set()
        self._wait_probed(2)
        self.assertEqual(set(self.probed), {"webvpn", "icourse"})

    def test_auto_connect_resume_keeps_return_values_and_probes(self):
        self.assertEqual(self.service.start_auto_connect_resume(), {"state": "off"})
        self.release.set()
        self._wait_probed(2)


class CampusDiagnosticsTests(unittest.TestCase):
    """P1-D：按需校园诊断 —— 有界无凭据探测 + 闭集结论，绝不含原始数据。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.service = CourseLensApplication(Path(self.temporary.name))
        self.service.set_credentials("synthetic-user", "synthetic-password")

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def test_diagnostics_is_closed_set_with_coarse_bands(self):
        evidence = {
            "webvpn": {"decision": "proxy", "direct_ok": False, "proxy_ok": True},
            "icourse": {"decision": "direct", "direct_ok": True, "proxy_ok": True},
        }
        with patch.object(
            self.service.network,
            "refresh_route_decision",
            side_effect=lambda name: evidence[name],
        ):
            value = self.service.campus_diagnostics()
        self.assertEqual(value["schema"], "courselens.campus-diagnostics.v1")
        self.assertIn(value["mode"], {"auto", "direct", "manual"})
        self.assertEqual(value["state"], "login_required")  # 无会话：诚实聚合态
        self.assertEqual(value["next_action"], "login")
        self.assertEqual(set(value["services"]), {"webvpn", "icourse"})
        webvpn = value["services"]["webvpn"]
        icourse = value["services"]["icourse"]
        self.assertEqual(webvpn["route"], "proxy")
        self.assertTrue(webvpn["fallback_used"], "直连死+代理活 → 回退成功")
        self.assertEqual(icourse["route"], "direct")
        self.assertFalse(icourse["fallback_used"])
        for service in (webvpn, icourse):
            self.assertIn(service["latency_band"], {"fast", "normal", "slow", "unavailable"})
        self.assertIsInstance(value["checked_at"], int)
        self.assertGreater(value["checked_at"], 0)

    def test_diagnostics_payload_is_secret_free(self):
        value = self.service.campus_diagnostics()
        text = json.dumps(value, ensure_ascii=False).casefold()
        for forbidden in (
            "http", "127.0.0.1", "password", "cookie", "token", "lck", "synthetic-user",
        ):
            self.assertNotIn(forbidden, text)

    def test_probe_failure_degrades_to_unknown_evidence(self):
        def _boom(_name):
            raise requests.ConnectionError("probe failed")

        with patch.object(self.service.network, "refresh_route_decision", side_effect=_boom):
            value = self.service.campus_diagnostics()
        for service in value["services"].values():
            self.assertEqual(service["route"], "unknown")
            self.assertIsNone(service["direct_ok"])
            self.assertIsNone(service["proxy_ok"])
            self.assertFalse(service["fallback_used"])
            self.assertEqual(service["latency_band"], "unavailable")

    def test_latency_band_is_coarse_and_bounded(self):
        band = self.service._campus_latency_band
        self.assertEqual(band(False, 0.1), "unavailable")
        self.assertEqual(band(True, 0.5), "fast")
        self.assertEqual(band(True, 2.5), "normal")
        self.assertEqual(band(True, 9.0), "slow")


class _FakeRestoreVPN:
    """restore_session_checkpoint 编排的合成 WebVPNSession 替身：零网络。"""

    instances: list = []
    restore_calls: list = []
    restore_result = True
    release: threading.Event | None = None

    def __init__(self, step_callback=None, *, proxy_url="", transport="curl_h2"):
        self.proxy_url = proxy_url
        self._transport_class = transport
        self.logged_in = False
        self.closed = False
        self.auth_deadline = None
        type(self).instances.append(self)

    def begin_authentication(self, seconds):
        self.auth_deadline = seconds

    def end_authentication(self):
        self.auth_deadline = None

    def restore_session_checkpoint(self, cookies):
        type(self).restore_calls.append([dict(item) for item in cookies])
        if type(self).release is not None:
            type(self).release.wait(timeout=5)
        return type(self).restore_result

    def close(self):
        self.closed = True


class _SentinelClient:
    """普通登录路径的替身客户端：满足 client() 成功落账所需的最小形状。"""

    def __init__(self):
        self.vpn = object()  # 只被赋值给 _vpn，绝不触碰


class CheckpointRestoreFlightTests(unittest.TestCase):
    """V5 会话检查点恢复编排：单航班、fail-closed 退回普通登录、状态无秘密。"""

    ACCOUNT = "20301010001"

    def _checkpoint_cookies(self):
        return [
            {"name": "webvpn_session", "value": "synthetic-checkpoint-cookie", "domain": "webvpn.fudan.edu.cn", "path": "/"},
        ]

    def setUp(self):
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.store = _FakeCredentialStore(
            accounts={self.ACCOUNT: {"student_id": self.ACCOUNT, "requires_rotation": False}},
        )
        self.store.save_session_checkpoint(self.ACCOUNT, self._checkpoint_cookies())
        self.app.credentials = self.store
        self.app._credentials = {"student_id": self.ACCOUNT, "password": "synthetic-not-a-real-password"}
        self.app._client = None
        self.app._client_last_verified_at = 0.0
        self.app.network.service_proxies = Mock(return_value=[""])
        _FakeRestoreVPN.instances = []
        _FakeRestoreVPN.restore_calls = []
        _FakeRestoreVPN.restore_result = True
        _FakeRestoreVPN.release = None

    def tearDown(self):
        self.app.close()
        self.temporary.cleanup()

    def test_restore_success_installs_client_without_password_login(self):
        with patch("src.api.webvpn.WebVPNSession", _FakeRestoreVPN):
            client = self.app.client()
        self.assertIs(client.vpn, _FakeRestoreVPN.instances[-1])
        self.assertTrue(client.vpn.logged_in)
        self.assertIs(self.app._client, client)
        self.assertTrue(self.app._login_status["connected"])
        self.assertIn("恢复", str(self.app._login_status["message"]))
        self.assertEqual(_FakeRestoreVPN.restore_calls, [self._checkpoint_cookies()])
        # 恢复成功不丢弃检查点：同账号的下一次冷启动仍可恢复
        self.assertEqual(self.store.checkpoints.get(self.ACCOUNT), self._checkpoint_cookies())
        self.assertEqual(self.app._webvpn_route, "direct")

    def test_restore_success_records_verified_route_and_clears_cooldown(self):
        """P3.1：检查点恢复成功验证了该路由——写入健康记忆并清冷却。"""
        self.app.network.note_route_failure("webvpn", "")
        with patch("src.api.webvpn.WebVPNSession", _FakeRestoreVPN):
            client = self.app.client()
        self.assertIs(self.app._client, client)
        record = self.app.network._route_health.get("webvpn")
        self.assertIsNotNone(record)
        self.assertEqual(record["verified_route"], "direct")
        self.assertEqual(record["cooldown_until"], 0.0)

    def test_restore_rejection_clears_checkpoint_and_falls_back_to_login(self):
        _FakeRestoreVPN.restore_result = False
        sentinel = _SentinelClient()
        with patch("src.api.webvpn.WebVPNSession", _FakeRestoreVPN):
            with patch.object(self.app, "_login_with_retry", return_value=sentinel) as retry:
                client = self.app.client()
        self.assertIs(client, sentinel)
        retry.assert_called_once()
        self.assertEqual(_FakeRestoreVPN.restore_calls, [self._checkpoint_cookies()])
        self.assertTrue(_FakeRestoreVPN.instances[-1].closed, "恢复失败必须关闭替身会话")
        self.assertIsNone(self.store.checkpoints.get(self.ACCOUNT), "服务端拒绝后检查点必须丢弃")

    def test_missing_checkpoint_goes_straight_to_ordinary_login(self):
        self.store.clear_all_session_checkpoints()
        sentinel = _SentinelClient()
        with patch("src.api.webvpn.WebVPNSession", _FakeRestoreVPN):
            with patch.object(self.app, "_login_with_retry", return_value=sentinel) as retry:
                client = self.app.client()
        self.assertIs(client, sentinel)
        retry.assert_called_once()
        self.assertEqual(_FakeRestoreVPN.instances, [], "无检查点绝不构造恢复会话")

    def test_concurrent_startup_callers_share_one_restore_flight(self):
        release = threading.Event()
        _FakeRestoreVPN.release = release
        results: list = []
        errors: list = []
        # patch 只在主线程做一次：并发 patch 同一属性本身就不是线程安全的。
        with patch("src.api.webvpn.WebVPNSession", _FakeRestoreVPN), \
                patch.object(self.app, "_login_with_retry") as retry:

            def _caller():
                try:
                    results.append(self.app.client())
                except BaseException as exc:  # noqa: BLE001 - 测试收集全部失败
                    errors.append(exc)

            threads = [threading.Thread(target=_caller) for _ in range(5)]
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not _FakeRestoreVPN.restore_calls:
                time.sleep(0.01)
            release.set()
            for thread in threads:
                thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(len(_FakeRestoreVPN.restore_calls), 1, "并发启动必须共享同一次恢复航班")
        self.assertEqual(len(results), 5)
        self.assertEqual(len({id(client) for client in results}), 1, "所有等待者拿到同一客户端")
        retry.assert_not_called()

    def test_restore_status_and_client_state_stay_secret_free(self):
        with patch("src.api.webvpn.WebVPNSession", _FakeRestoreVPN):
            self.app.client()
        payload = json.dumps(dict(self.app._login_status), ensure_ascii=False).casefold()
        for forbidden in ("synthetic-checkpoint-cookie", "cookie", "password", "token"):
            self.assertNotIn(forbidden, payload)

    def test_logout_and_account_deletion_clear_checkpoints(self):
        self.app.logout_fudan()
        self.assertEqual(self.store.cleared_all, 1)
        self.assertIsNone(self.store.checkpoints.get(self.ACCOUNT))
        self.store.save_session_checkpoint(self.ACCOUNT, self._checkpoint_cookies())
        self.app.delete_saved_credentials(self.ACCOUNT)
        self.assertIsNone(self.store.checkpoints.get(self.ACCOUNT), "删除已保存账号必须丢弃其检查点")

    def test_terminal_login_failure_clears_checkpoint(self):
        from src.api.webvpn import FudanCredentialsRejected

        class _RejectingVPN(_FakeRestoreVPN):
            AUTH_DEADLINE_SECONDS = 30

            def login(self, student_id=None, password=None):
                raise FudanCredentialsRejected("synthetic credentials rejected")

        self.app._restore_client_from_checkpoint = Mock(return_value=None)
        with patch("src.api.webvpn.WebVPNSession", _RejectingVPN):
            with self.assertRaises(FudanCredentialsRejected):
                self.app._login_with_retry()
        self.assertIsNone(self.store.checkpoints.get(self.ACCOUNT), "凭据拒绝后检查点必须丢弃")
        self.assertEqual(self.app._login_status.get("error_code"), "fudan_credentials_rejected")


class RevalidateDebounceTests(unittest.TestCase):
    """SRC-SYNDROME-1 U2（第廿七案）：复用窗过期+保活补验在途=ready 防抖。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.app._set_login_status("ready", "icourse", "已连接", connected=True)
        self.app._client = object()

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def _lapse_window(self) -> None:
        self.app._client_last_verified_at = time.monotonic() - 400.0

    def test_revalidate_in_flight_keeps_ready_shape(self):
        self._lapse_window()
        release = threading.Event()

        def _blocked_client(**_kwargs):
            release.wait(timeout=2.0)

        self.app.client = _blocked_client
        snapshot = self.app.authentication_snapshot()  # 触发补验（旗标同步置位）
        self.assertEqual(snapshot["state"], "ready", "补验在途期间不得闪「验证中」")
        self.assertEqual(snapshot["code"], "fudan_session_verified")
        self.assertTrue(snapshot.get("revalidating"), "防抖必须带诚实披露键")
        self.assertFalse(snapshot["connected"], "未确认前 connected 保持 False")
        release.set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self.app._client_nudge_in_flight:
            time.sleep(0.01)
        snapshot = self.app.authentication_snapshot()
        self.assertEqual(snapshot["state"], "checking", "补验收口后回到诚实 checking")
        self.assertNotIn("revalidating", snapshot)

    def test_revalidate_grace_expiry_converges_to_checking(self):
        self._lapse_window()
        release = threading.Event()

        def _blocked_client(**_kwargs):
            release.wait(timeout=2.0)

        self.app.client = _blocked_client
        self.app.authentication_snapshot()
        with self.app._lock:
            self.assertTrue(self.app._client_nudge_in_flight)
            self.app._client_revalidate_started_at = time.monotonic() - 50.0  # 超 45s 收敛上限
        snapshot = self.app.authentication_snapshot()
        self.assertEqual(snapshot["state"], "checking", "补验超限必须收敛为诚实 checking")
        self.assertNotIn("revalidating", snapshot)
        release.set()

    def test_fresh_window_reports_plain_ready_without_flag(self):
        self.app._client_last_verified_at = time.monotonic() - 10.0
        self.app.client = Mock()
        snapshot = self.app.authentication_snapshot()
        self.assertEqual(snapshot["state"], "ready")
        self.assertNotIn("revalidating", snapshot)
        self.app.client.assert_not_called()


class SessionSelfHealTests(unittest.TestCase):
    """SRC-SYNDROME-1 U2（第廿六案）：degraded 自愈看门狗单 tick 语义。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.app._credentials = {"student_id": "sid", "password": "pw"}

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def _set_error(self, error_code: str) -> None:
        self.app._set_login_status(
            "error", "webvpn", "登录失败，请检查账号、密码或网络状态",
            error_code=error_code,
        )

    def test_recoverable_error_triggers_one_bounded_flight(self):
        self._set_error("timeout")
        self.app.client = Mock()
        backoff = self.app._session_self_heal_once(30.0)
        self.app.client.assert_called_once_with(verify_before_reuse=True)
        self.assertEqual(backoff, 30.0)

    def test_transport_and_maintenance_errors_are_recoverable(self):
        for code in ("network_unavailable", "webvpn_ticket_transport_failed", "fudan_service_maintenance"):
            with self.subTest(code=code):
                self._set_error(code)
                self.app.client = Mock()
                self.app._session_self_heal_once(30.0)
                self.app.client.assert_called_once()

    def test_identity_terminal_codes_never_retry(self):
        for code in (
            "fudan_credentials_rejected", "fudan_challenge_required",
            "fudan_account_locked", "fudan_redirect_unsafe", "fudan_ticket_rejected",
        ):
            with self.subTest(code=code):
                self._set_error(code)
                self.app.client = Mock()
                backoff = self.app._session_self_heal_once(30.0)
                self.app.client.assert_not_called()
                self.assertEqual(backoff, 30.0, "终态不重试也不升级退避")

    def test_failure_doubles_backoff_with_cap(self):
        self._set_error("timeout")
        # REALRUN-1 旅程6：退避>一拍的 tick 现在先做恢复探测——本钉用 None
        # （无证据，非 auto 模式同形）保持「探测不可用=既有航班节拍」语义，
        # 单测不触真网络。
        self.app._campus_path_recovered = lambda: None
        self.app.client = Mock(side_effect=RuntimeError("synthetic"))
        self.assertEqual(self.app._session_self_heal_once(30.0), 60.0)
        self.assertEqual(self.app._session_self_heal_once(60.0), 120.0)
        self.assertEqual(self.app._session_self_heal_once(1000.0), 600.0, "退避封顶")

    def test_healthy_or_unconfigured_state_resets_backoff(self):
        self.app._set_login_status("ready", "icourse", "已连接", connected=True)
        self.app.client = Mock()
        self.assertEqual(self.app._session_self_heal_once(600.0), 30.0)
        self.app.client.assert_not_called()
        self.app._credentials = {"student_id": "", "password": ""}
        self._set_error("timeout")
        self.assertEqual(self.app._session_self_heal_once(600.0), 30.0)
        self.app.client.assert_not_called()

    def test_recovered_path_resets_backoff_and_reverifies_now(self):
        """REALRUN-1 旅程6：断网恢复后 degraded 不再等满退避——恢复证据在案
        即复位退避为一拍并立即发起既有单航班重验。"""
        self._set_error("network_unavailable")
        self.app._campus_path_recovered = lambda: True
        self.app.client = Mock()
        backoff = self.app._session_self_heal_once(600.0)
        self.app.client.assert_called_once_with(verify_before_reuse=True)
        self.assertEqual(backoff, 30.0, "恢复证据在案：退避复位为一拍")
        with self.app._lock:
            self.assertEqual(self.app._session_self_heal_backoff, 30.0)

    def test_unrecovered_path_skips_flight_and_grows_backoff(self):
        """REALRUN-1 旅程6：探测确认校园路径仍不可达——本次不发起注定失败的
        登录航班（少打校园），退避照旧翻倍封顶。"""
        self._set_error("timeout")
        self.app._campus_path_recovered = lambda: False
        self.app.client = Mock()
        backoff = self.app._session_self_heal_once(120.0)
        self.app.client.assert_not_called()
        self.assertEqual(backoff, 240.0)
        self.assertEqual(self.app._session_self_heal_once(240.0), 480.0)
        self.assertEqual(self.app._session_self_heal_once(480.0), 600.0, "退避封顶")
        self.app.client.assert_not_called()

    def test_host_resume_resets_self_heal_backoff(self):
        """REALRUN-1 旅程6：宿主恢复=恢复事件——看门狗退避复位为一拍，
        校园会话重验最迟 ≤30s 内发起，不再沿用断网期放大的旧退避。"""
        self._set_error("timeout")
        with self.app._lock:
            self.app._session_self_heal_backoff = 600.0
            # 墙钟大幅超前单调钟=复睡唤醒判定（P3.1 惰性判定谓词同形）
            self.app._host_activity_observed = (time.monotonic(), time.time() - 1000.0)
        self.assertTrue(self.app._maybe_note_host_resume())
        with self.app._lock:
            self.assertEqual(self.app._session_self_heal_backoff, 30.0)


class LiveSessionDegradedTests(unittest.TestCase):
    """SRC-SYNDROME-1 U2（第廿六案）：会话 degraded 时直播观测链收编闭集码。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.app.close()
        self.temporary.cleanup()

    def test_observe_returns_closed_set_when_session_flight_fails(self):
        self.app.is_authorized_course = Mock(return_value=True)
        self.app.client = Mock(side_effect=RuntimeError("synthetic login failure"))
        value = self.app.observe_live_room("course-1")
        self.assertEqual(value, {"state": "unknown", "code": "live_session_unavailable"})
        self.app.client.assert_called_once()

    def test_observe_denied_stays_untouched(self):
        self.app.is_authorized_course = Mock(return_value=False)
        self.app.client = Mock()
        value = self.app.observe_live_room("course-1")
        self.assertEqual(value, {"state": "denied", "code": "live_authorization_denied"})
        self.app.client.assert_not_called()


class TimetableRestoringGateTests(unittest.TestCase):
    """SRC-SYNDROME-1 U2（第廿八案）：课表读族并入三态门+restoring_trusted 键。"""

    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.app = CourseLensApplication(Path(self.temporary.name))
        self.app.credentials = Mock()
        self.app.credentials.list_accounts = Mock(
            return_value=[{"student_id": "synthetic-user", "requires_rotation": False}]
        )
        # checking+自动登录可恢复：login connecting（→checking）+ 偏好 enabled+账号 ready
        self.app._set_login_status("connecting", "webvpn", "正在连接 WebVPN")
        self.app.task_store.set_app_state("auto_connect", {
            "fudan": {"enabled": True, "account_id": "synthetic-user"},
            "github": {"enabled": False},
            "last_resume": {},
        })
        frontend = PROJECT_ROOT / "frontend"
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(http_services(self.app), frontend),
        )
        # FLAKYFIX-1（同族 fixture 卫生）：tearDown server_close() 收齐在途
        # handler 线程后再清临时目录（WinError 10038 警告族源头同前注）。
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.app.close()
        self.temporary.cleanup()

    def _get(self, path: str):
        from urllib.error import HTTPError
        from urllib.request import urlopen

        try:
            with urlopen(f"{self.base}{path}", timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_timetable_get_passes_during_restoring_trusted(self):
        status, payload = self._get("/api/v3/timetable")
        self.assertEqual(status, 200, "恢复期课表本地快照必须放行（缓存先行）")
        self.assertEqual(payload.get("schema"), "courselens.api.v3")

    def test_authentication_snapshot_carries_restoring_trusted(self):
        status, payload = self._get("/api/v3/authentication")
        self.assertEqual(status, 200)
        data = payload.get("data") or payload
        self.assertEqual(data.get("state"), "checking")
        self.assertTrue(data.get("restoring_trusted"), "闭集增量键必须随快照下发")

    def test_timetable_get_rejected_without_auto_login(self):
        self.app.task_store.set_app_state("auto_connect", {
            "fudan": {"enabled": False, "account_id": ""},
            "github": {"enabled": False},
            "last_resume": {},
        })
        status, payload = self._get("/api/v3/timetable")
        self.assertEqual(status, 401, "非可恢复 checking 原样拒绝")
        self.assertEqual((payload.get("error_code") or (payload.get("data") or {}).get("error_code")), "timetable_login_required")


class MidRequestDisconnectLogTests(unittest.TestCase):
    """SRC-SYNDROME-1 U3：mid-request 行带时间戳并按 60s 窗口节流。"""

    def test_throttles_window_and_reports_suppressed_count(self):
        lines = []
        now = [1000.0]
        log = MidRequestDisconnectLog(clock=lambda: now[0], writer=lines.append)
        log.emit()
        self.assertEqual(len(lines), 1)
        self.assertIn("http: client connection ended mid-request", lines[0])
        self.assertRegex(lines[0].split(" ")[0], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$", "必须带本地时间戳")
        now[0] += 10.0
        log.emit()
        log.emit()
        self.assertEqual(len(lines), 1, "窗口内必须抑制，不刷屏")
        now[0] += 61.0
        log.emit()
        self.assertEqual(len(lines), 2)
        self.assertIn("suppressed 2", lines[1], "窗口后的下一行必须带抑制计数")


if __name__ == "__main__":
    unittest.main()
