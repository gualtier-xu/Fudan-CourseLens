"""Chain-level login tests over the local simulator (fixtures/sim_server.py).

B3 链路级用例（夜4 login-testbench §落地建议 #3/#6）：
- challenge 挑战页 → 独立码 platform_challenge_required，零重试（挑战页需要
  人工点一次人机确认，程序重试只会连续撞上同一页）；
- 无 lck 普通页 → platform_auth_context_missing 保持在登录重试闭集内，外层
  梯全链重跑（fresh lck/ticket，绝不重放）后可恢复。
零真实端点：仅 127.0.0.1，OS 分配端口。
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from urllib.parse import urlparse

from courselens_worker import platform_session as ps
from courselens_worker.platform_session import (
    PlatformSession,
    PlatformSessionError,
    _MATERIALIZE_LOGIN_ATTEMPTS,
)
from courselens_worker.runner import safe_worker_error_detail

try:
    from fixtures.sim_server import STATS, set_mode, start
except ImportError:  # 直接从 worker/tests 目录运行时
    from sim_server import STATS, set_mode, start


class LoginChainSimTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = start()

    def setUp(self):
        set_mode("delivered")

    @contextmanager
    def _endpoints(self):
        saved = (
            ps.WEBVPN_BASE, ps.IDP_BASE, ps.ICOURSE_BASE,
            ps._ALLOWED_HOSTS, ps._validate_url, ps._validate_upstream_url,
        )
        ps.WEBVPN_BASE = self.base
        ps.IDP_BASE = self.base
        ps.ICOURSE_BASE = self.base
        ps._ALLOWED_HOSTS = {"127.0.0.1"}

        def _validate_local(value):
            try:
                parsed = urlparse(str(value or ""))
            except ValueError:
                raise ps._fail("platform_redirect_rejected")
            if (
                parsed.scheme != "http"
                or parsed.hostname != "127.0.0.1"
                or parsed.username
                or parsed.password
            ):
                raise ps._fail("platform_redirect_rejected")
            return parsed.geturl()

        ps._validate_url = _validate_local
        ps._validate_upstream_url = _validate_local
        try:
            yield
        finally:
            (
                ps.WEBVPN_BASE, ps.IDP_BASE, ps.ICOURSE_BASE,
                ps._ALLOWED_HOSTS, ps._validate_url, ps._validate_upstream_url,
            ) = saved

    def test_delivered_chain_signs_in_end_to_end(self):
        """§T1 冒烟判据：净链一次通过完整 WebVPN+课程登录。"""
        with self._endpoints():
            connector = PlatformSession(transport="requests")
            connector._login_webvpn_full("account", "password", attempts=1)
        self.assertTrue(connector._webvpn_ready)
        self.assertEqual(STATS["webvpn_entries"], 1)

    def test_challenge_page_raises_independent_code_without_retry(self):
        """挑战页 → 新码 + 可归约 + 不入任何重试闭集 + 服务端仅见一次进入。"""
        set_mode("challenge")
        with self._endpoints():
            connector = PlatformSession(transport="requests")
            with self.assertRaises(PlatformSessionError) as captured:
                connector._login_webvpn_full("account", "password", attempts=1)
        self.assertEqual(str(captured.exception), "platform_challenge_required")
        self.assertEqual(captured.exception.connection_stage, "webvpn_context")
        self.assertEqual(
            safe_worker_error_detail(captured.exception),
            "platform_challenge_required",
        )
        self.assertNotIn("platform_challenge_required", ps._RETRYABLE_LOGIN_ERRORS)
        self.assertNotIn(
            "platform_challenge_required", PlatformSession._RETRYABLE_WEBVPN_LEG_ERRORS
        )
        self.assertEqual(STATS["webvpn_entries"], 1, "挑战页零重试")

    def test_missing_lck_stays_retryable_and_ladder_recovers(self):
        """无 lck 普通页 → 维持既有码入外层闭集；平台恢复发 lck 后全链重跑成功。"""
        set_mode("not_delivered")
        codes = []
        connector = None
        with self._endpoints():
            for attempt in range(_MATERIALIZE_LOGIN_ATTEMPTS):
                connector = PlatformSession(transport="requests")
                try:
                    connector._login_webvpn_full("account", "password", attempts=1)
                    break
                except PlatformSessionError as exc:
                    codes.append(str(exc))
                    if (
                        str(exc) not in ps._RETRYABLE_LOGIN_ERRORS
                        or attempt == _MATERIALIZE_LOGIN_ATTEMPTS - 1
                    ):
                        raise
                    set_mode("delivered")  # 平台恢复发 lck：模拟外层梯的下一次全链重跑
        self.assertEqual(codes, ["platform_auth_context_missing"])
        self.assertTrue(connector._webvpn_ready)
        # set_mode 每次翻转都会清零计数：这里只对「恢复后的成功跑」计数——
        # 恰一次进入即成功；每次重试独立进入已由挑战页用例（零重试）钉住。
        self.assertEqual(STATS["webvpn_entries"], 1)


if __name__ == "__main__":
    unittest.main()
