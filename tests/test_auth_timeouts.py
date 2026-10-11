import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from src.application import CourseLensApplication
from path_utils import PROJECT_ROOT
from src.api.webvpn import FudanCredentialsRejected, WebVPNSession


class AuthenticationTimeoutTests(unittest.TestCase):
    def test_webvpn_auth_requests_are_bounded(self):
        session = WebVPNSession()
        try:
            # UIS-LOGIN-P0-1（2026-10-11）：UIS 高峰慢登录实测（R4 真机 84.5s；
            # 用户实例遥测 13.5–22.5s 活响应被旧 read 15s 帽掐死）——auth 期
            # read 帽放宽到 30s，attempt 帽放宽到 90s 覆盖高峰尾巴。
            self.assertEqual(session.AUTH_TIMEOUT, (8, 30))
            self.assertEqual(session.TICKET_TIMEOUT, (5, 12))
            # 预热专用帽：比 VERIFY 耐心（慢网握手），仍远小于 attempt 帽；
            # VERIFY (3,5) 保持不动（checkpoint 恢复 20s 总时限设计依赖它）。
            self.assertEqual(session.WARMUP_TIMEOUT, (5, 10))
            self.assertEqual(session.VERIFY_TIMEOUT, (3, 5))
            self.assertGreaterEqual(
                session.AUTH_DEADLINE_SECONDS, 90,
                "attempt 帽必须覆盖 UIS 高峰慢链尾巴（R4 实测 84.5s）",
            )
            self.assertEqual(session.session.max_redirects, session.MAX_AUTH_REDIRECTS)
            self.assertLessEqual(session.MAX_AUTH_REDIRECTS, 8)
            session.begin_authentication(60)
            session.session._deadline = lambda: time.monotonic() - 1
            with self.assertRaises(requests.Timeout):
                session.session.get("https://example.invalid", timeout=60)
            session.end_authentication()
        finally:
            session.session.close()

    def test_login_flight_total_deadline_covers_slow_peak(self):
        """UIS-LOGIN-P0-1 契约钉：登录航班总时限必须装得下两次深尝试。

        旧默认 90s 在慢网下 attempt3 饥饿（45s+1s+44s+2s → 剩余 ≤0 即刻
        超时，实为 2 次真尝试）且单 attempt 盖不住高峰链。默认 180s +
        attempt 帽 90s = 两次深尝试；快失败路径仍由既有 3 次梯子约束。
        """
        import inspect

        signature = inspect.signature(CourseLensApplication._login_with_retry)
        default = signature.parameters["total_deadline_seconds"].default
        self.assertIsInstance(default, float)
        self.assertGreaterEqual(
            default, 180.0,
            "总时限必须允许两次 attempt 帽级深尝试（慢网 attempt3 饥饿教训）",
        )

    def test_timeout_is_backend_failure_not_optimistic_success(self):
        class TimeoutVpn:
            def __init__(self, **_kwargs):
                pass

            def login(self, **kwargs):
                raise requests.ReadTimeout("synthetic timeout")

        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            service = CourseLensApplication(Path(directory))
            try:
                service.set_credentials("student", "password")
                with patch("src.api.webvpn.WebVPNSession", TimeoutVpn):
                    with self.assertRaises(requests.ReadTimeout):
                        service._login_with_retry(max_attempts=1)
                snapshot = service.authentication_snapshot()
                self.assertEqual(snapshot["state"], "degraded")
                self.assertEqual(snapshot["code"], "timeout")
                self.assertEqual(snapshot["source"], "platform")
                self.assertFalse(snapshot["connected"])
            finally:
                service.close()

    def test_session_restore_prime_is_idempotent_and_quiet(self):
        """⑬a：恢复预热每进程恰一次、异常静默（预热失败=既有首读原样重试）。"""
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            service = CourseLensApplication(Path(directory))
            # 夜10-C：prime 派生线程持有池化句柄——finally 关 app 释放文件
            try:
                self.assertEqual(service.prime_session_restore()["state"], "started")
                self.assertEqual(service.prime_session_restore()["state"], "already_started")
            finally:
                service.close()

    def test_dirty_student_id_never_reaches_the_credential_store(self):
        """F13（N5FE-P3）：字面 undefined/null 学号在保存端直接拒绝，绝不落盘。"""
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            service = CourseLensApplication(Path(directory))
            # 夜10-C：池化下 store 句柄随 app 存活——finally 关 app 释放文件
            try:
                for dirty in ("undefined", "Undefined", "null", "  ", ""):
                    with self.assertRaises(ValueError):
                        service.set_credentials(dirty, "password", remember=True)
                self.assertEqual(service.credentials.list_accounts(), [])
            finally:
                service.close()

    def test_credentials_rejected_stops_retrying_immediately(self):
        attempts = {"count": 0}

        class RejectedVpn:
            def __init__(self, **_kwargs):
                self.session = Mock()

            def login(self, **_kwargs):
                attempts["count"] += 1
                raise FudanCredentialsRejected("Authentication failed: {'msg': '密码错误'}")

        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            service = CourseLensApplication(Path(directory))
            try:
                service.set_credentials("student", "password")
                with patch("src.api.webvpn.WebVPNSession", RejectedVpn), patch("time.sleep"):
                    with self.assertRaises(FudanCredentialsRejected):
                        service._login_with_retry(max_attempts=3)
                self.assertEqual(attempts["count"], 1, "凭据被拒不消耗剩余重试次数")
                snapshot = service.authentication_snapshot()
                self.assertEqual(snapshot["state"], "degraded")
                self.assertEqual(snapshot["code"], "fudan_credentials_rejected")
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
