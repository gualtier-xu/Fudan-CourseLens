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
            self.assertEqual(session.AUTH_TIMEOUT, (5, 15))
            self.assertEqual(session.TICKET_TIMEOUT, (5, 12))
            self.assertEqual(session.session.max_redirects, session.MAX_AUTH_REDIRECTS)
            self.assertLessEqual(session.MAX_AUTH_REDIRECTS, 8)
            session.begin_authentication(60)
            session.session._deadline = lambda: time.monotonic() - 1
            with self.assertRaises(requests.Timeout):
                session.session.get("https://example.invalid", timeout=60)
            session.end_authentication()
        finally:
            session.session.close()

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
