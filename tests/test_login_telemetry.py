"""登录探针跳过 + 阶段遥测的离线行为测试。

全部 fake 驱动、零真实外联（文末有约定自检）。覆盖：
- 成功路径两条票腿均零确认探针（且仅此探针被跳过）；
- 非 2xx / 登录页终点直接抛出，不发探针；异常恢复路径仍发探针；
- credentials_rejected 不重试语义不变；
- LoginStageTelemetry：两种回调形态归一化、闭集外丢弃、artifact 不泄漏；
- _DeadlineSession.send 每请求 elapsed（闭集 outcome + 显式阶段标签）；
- 基准场景的快速单元版（成功/错密码/超时/5xx/双败保主因/catalog 失败/
  会话过期/并发双提交）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from Crypto.PublicKey import RSA

from path_utils import PROJECT_ROOT
from src.api.webvpn import (
    _DeadlineSession,
    FudanCredentialsRejected,
    WebVPNSession,
)
from src.application import (
    LOGIN_STAGE_CACHE_DIRNAME,
    CatalogRefreshError,
    CourseLensApplication,
    LoginStageTelemetry,
)
from src.runtime import config


class FakeResponse:
    """Rich offline response: status/headers/Location/url/text/history/json()."""

    def __init__(self, status=200, *, location="", url="", payload=None, text=""):
        self.status_code = status
        self.headers = {"Location": location} if location else {}
        self.url = url
        self.text = text
        self.history = []
        self._payload = payload
        self.closed = False

    def close(self):
        self.closed = True

    def json(self):
        if self._payload is None:
            raise ValueError("synthetic response has no payload")
        return self._payload


_PUB_KEY_B64 = "".join(
    line
    for line in RSA.generate(2048).export_key().decode("ascii").splitlines()
    if "KEY" not in line
)
_AUTH_METHODS_PAYLOAD = {
    "data": [{"moduleCode": "userAndPwd", "authChainCode": "chain-synthetic"}],
    "requestType": "chain_type",
}
_AUTH_OK_PAYLOAD = {"code": "200", "loginToken": "synthetic-login-token"}
_AUTH_REJECTED_PAYLOAD = {"code": "AUTH0003", "msg": "密码错误"}


def success_chain(*, with_probes: bool) -> list:
    """Offline response script for one full vpn.login() + authenticate_icourse()."""
    chain = [
        # --- vpn.login() ---
        FakeResponse(302, location=f"{config.IDP_BASE}/ac/synthetic?lck=webvpn-lck"),
        FakeResponse(200, payload=_AUTH_METHODS_PAYLOAD),
        FakeResponse(200, payload={"data": _PUB_KEY_B64}),
        FakeResponse(200, payload=_AUTH_OK_PAYLOAD),
        FakeResponse(
            200,
            text=(
                'var locationValue = "'
                f"{config.WEBVPN_BASE}/users/cas?ticket=ST-webvpn-synthetic"
                '";'
            ),
        ),
        FakeResponse(302, location="/portal"),
        FakeResponse(200, url=f"{config.WEBVPN_BASE}/portal"),
        # --- vpn.authenticate_icourse() ---
        FakeResponse(200, url=f"{config.WEBVPN_BASE}/"),
        FakeResponse(200, url=f"{config.ICOURSE_BASE}/ac/synthetic?lck=icourse-lck"),
        FakeResponse(200, payload=_AUTH_METHODS_PAYLOAD),
        FakeResponse(200, payload={"data": _PUB_KEY_B64}),
        FakeResponse(200, payload=_AUTH_OK_PAYLOAD),
        FakeResponse(
            200,
            text=(
                'var locationValue = "'
                f"{config.ICOURSE_BASE}/cas?ticket=ST-icourse-synthetic"
                '";'
            ),
        ),
        FakeResponse(302, location="/portal"),
        FakeResponse(200, url=f"{config.ICOURSE_BASE}/portal"),
    ]
    if with_probes:
        # 旧行为：正常票链完成后的两个确认探针 + iCourse infosimple 探针
        chain.insert(7, FakeResponse(200, url=f"{config.WEBVPN_BASE}/"))
        chain.append(FakeResponse(200, payload={"code": 0}))
    return chain


class ScriptedSession:
    """make_vpn 风格的 get+post fake（非 _DeadlineSession → 绕过 curl_cffi）。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        return self._handle(url, kwargs)

    def post(self, url, **kwargs):
        return self._handle(url, kwargs)

    def _handle(self, url, kwargs):
        self.calls.append((url, kwargs, getattr(self, "stage_label", "")))
        if not self.responses:
            raise AssertionError(f"unexpected request #{len(self.calls)}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if not response.url:
            response.url = url
        return response

    def close(self):
        pass


def make_login_vpn(callback=None, *, with_probes=False):
    vpn = WebVPNSession(step_callback=callback)
    vpn.session = ScriptedSession(success_chain(with_probes=with_probes))
    return vpn


class ProbeSkipTests(unittest.TestCase):
    def test_success_path_runs_zero_confirm_probes_on_both_legs(self):
        events = []
        vpn = make_login_vpn(lambda step, details=None: events.append((step, details)))

        self.assertTrue(vpn.login("synthetic.student", "synthetic-password-value"))
        self.assertTrue(
            vpn.authenticate_icourse("synthetic.student", "synthetic-password-value")
        )

        portal_root_calls = [
            call for call in vpn.session.calls if call[0] == config.WEBVPN_BASE + "/"
        ]
        self.assertEqual(
            len(portal_root_calls),
            1,
            "成功路径只保留 iCourse 预检探针，webvpn 票后确认探针必须被跳过",
        )
        self.assertEqual(
            portal_root_calls[0][2],
            "icourse_preflight_probe",
            "预检探针必须带显式阶段标签",
        )
        self.assertFalse(
            any("infosimple" in call[0] for call in vpn.session.calls),
            "iCourse 票后确认探针（infosimple）必须被跳过",
        )
        # 闭包外的显式阶段标签序列：两腿各自 steps + 票腿前清空
        labels = [call[2] for call in vpn.session.calls]
        self.assertEqual(
            labels,
            [
                "webvpn_auth_context",
                "webvpn_auth_methods",
                "webvpn_public_key",
                "webvpn_auth_execute",
                "webvpn_cas_ticket",
                "",
                "",
                "icourse_preflight_probe",
                "icourse_casapi",
                "icourse_auth_methods",
                "icourse_public_key",
                "icourse_auth_execute",
                "icourse_cas_ticket",
                "",
                "",
            ],
        )
        emitted = {step for step, _ in events}
        self.assertIn("webvpn_ticket_complete", emitted)
        self.assertIn("icourse_ticket_complete", emitted)

    def test_non_2xx_ticket_terminal_raises_without_probe(self):
        vpn = make_login_vpn()
        vpn.session.responses = [
            FakeResponse(302, location="/portal"),
            FakeResponse(503, url=f"{config.WEBVPN_BASE}/portal"),
        ]
        with self.assertRaisesRegex(RuntimeError, "status=503"):
            vpn._establish_session(f"{config.WEBVPN_BASE}/users/cas?ticket=x")
        # 仅票腿两跳，无任何确认探针
        self.assertEqual(len(vpn.session.calls), 2)
        self.assertFalse(
            any(call[0] == config.WEBVPN_BASE + "/" for call in vpn.session.calls)
        )

    def test_login_page_ticket_terminal_raises_without_probe(self):
        vpn = make_login_vpn()
        vpn.session.responses = [
            FakeResponse(200, url=f"{config.WEBVPN_BASE}/login"),
        ]
        with self.assertRaisesRegex(RuntimeError, "login page"):
            vpn._establish_session(f"{config.WEBVPN_BASE}/users/cas?ticket=x")
        self.assertEqual(len(vpn.session.calls), 1, "登录页终点不发确认探针")

    def test_recovery_paths_still_probe_on_both_legs(self):
        # webvpn 腿：票腿读超时后由 portal 探针裁决恢复
        vpn = make_login_vpn()
        vpn.session.responses = [
            requests.ReadTimeout("synthetic"),
            FakeResponse(200, url=f"{config.WEBVPN_BASE}/"),
        ]
        vpn._establish_session(f"{config.WEBVPN_BASE}/users/cas?ticket=x")
        self.assertEqual(len(vpn.session.calls), 2)
        self.assertEqual(vpn.session.calls[1][2], "webvpn_portal_probe")

        # icourse 腿：票腿读超时后由 infosimple 探针裁决恢复
        vpn = make_login_vpn()
        vpn.session.responses = [
            requests.ReadTimeout("synthetic"),
            FakeResponse(200, payload={"code": 0}),
        ]
        with patch("src.api.webvpn.get_vpn_url", return_value="synthetic-vpn-url"):
            vpn._establish_icourse_session(f"{config.WEBVPN_BASE}/users/cas?ticket=x")
        self.assertEqual(len(vpn.session.calls), 2)
        self.assertEqual(vpn.session.calls[1][2], "icourse_portal_probe")

    def test_pre_flight_probe_still_gates_authenticate_icourse(self):
        vpn = make_login_vpn()
        vpn.session.responses = [
            FakeResponse(302, location=f"{config.WEBVPN_BASE}/login"),
        ]
        with self.assertRaisesRegex(RuntimeError, "re-login needed"):
            vpn.authenticate_icourse("synthetic.student", "synthetic-password-value")
        self.assertEqual(vpn.session.calls[0][2], "icourse_preflight_probe")
        self.assertEqual(len(vpn.session.calls), 1)


class CredentialsRejectedTests(unittest.TestCase):
    def test_rejection_propagates_from_scripted_webvpn_execute(self):
        vpn = make_login_vpn()
        vpn.session.responses[3] = FakeResponse(200, payload=_AUTH_REJECTED_PAYLOAD)
        with self.assertRaises(FudanCredentialsRejected):
            vpn.login("synthetic.student", "synthetic-password-value")

    def test_service_level_rejection_never_retries(self):
        attempts = {"count": 0}

        class RejectedVpn:
            def __init__(self, **_kwargs):
                self.session = Mock()

            def login(self, **_kwargs):
                attempts["count"] += 1
                raise FudanCredentialsRejected("synthetic rejected")

        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            service = CourseLensApplication(Path(directory))
            try:
                service.set_credentials("synthetic.student", "synthetic-password-value")
                with patch("src.api.webvpn.WebVPNSession", RejectedVpn), patch("time.sleep"):
                    with self.assertRaises(FudanCredentialsRejected):
                        service._login_with_retry(max_attempts=3)
                self.assertEqual(attempts["count"], 1, "凭据被拒不消耗剩余重试次数")
            finally:
                service.close()


class _DeadlineSessionElapsedTests(unittest.TestCase):
    def test_send_records_elapsed_with_closed_outcome_and_bare_status(self):
        recorded = []
        session = _DeadlineSession(
            lambda: None,
            elapsed_callback=lambda stage, ms, outcome, status: recorded.append(
                (stage, ms, outcome, status)
            ),
        )
        session.stage_label = "webvpn_auth_context"
        try:
            with patch("requests.Session.send", return_value=FakeResponse(200)):
                session.get("https://synthetic.invalid/x", timeout=5)
            self.assertEqual(len(recorded), 1)
            stage, duration_ms, outcome, status = recorded[0]
            self.assertEqual(stage, "webvpn_auth_context")
            self.assertEqual(outcome, "ok")
            self.assertEqual(status, 200)
            self.assertIsInstance(duration_ms, int)

            with patch(
                "requests.Session.send", side_effect=requests.ReadTimeout("synthetic")
            ):
                with self.assertRaises(requests.ReadTimeout):
                    session.get("https://synthetic.invalid/x", timeout=5)
            self.assertEqual(recorded[-1][0], "webvpn_auth_context")
            self.assertEqual(recorded[-1][2], "timeout")
            self.assertIsNone(recorded[-1][3])

            with patch(
                "requests.Session.send",
                side_effect=requests.ConnectionError("synthetic"),
            ):
                with self.assertRaises(requests.ConnectionError):
                    session.get("https://synthetic.invalid/x", timeout=5)
            self.assertEqual(recorded[-1][2], "connection_error")
        finally:
            session.close()

    def test_empty_stage_label_records_nothing(self):
        recorded = []
        session = _DeadlineSession(
            lambda: None,
            elapsed_callback=lambda stage, ms, outcome, status: recorded.append(1),
        )
        try:
            with patch("requests.Session.send", return_value=FakeResponse(200)):
                session.get("https://synthetic.invalid/x", timeout=5)
            self.assertEqual(recorded, [])
        finally:
            session.close()

    def test_expired_deadline_records_nothing_and_never_touches_transport(self):
        import time as time_mod

        recorded = []
        session = _DeadlineSession(
            lambda: time_mod.monotonic() - 1,
            elapsed_callback=lambda *args: recorded.append(args),
        )
        try:
            with patch(
                "requests.Session.send", side_effect=AssertionError("network touched")
            ):
                with self.assertRaises(requests.Timeout):
                    session.get("https://synthetic.invalid/x", timeout=5)
            self.assertEqual(recorded, [])
        finally:
            session.close()


class TelemetryWriterTests(unittest.TestCase):
    def setUp(self):
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _lines(self, writer):
        files = sorted(writer._directory.glob("login-stage-*.jsonl"))
        self.assertEqual(len(files), 1)
        return files[0].read_text(encoding="utf-8").splitlines()

    def test_both_callback_signatures_are_normalized(self):
        writer = LoginStageTelemetry(self.directory, attempt=2)
        writer("webvpn_ticket_complete", {"elapsed_seconds": 1.25, "redirects": 3})
        writer("catalog_context")  # DirectICourseSession 的单参形态
        writer("webvpn_auth_context", {"duration_ms": 350, "outcome": "ok", "status": 200})
        lines = self._lines(writer)
        self.assertEqual(len(lines), 3)
        first, second, third = (json.loads(line) for line in lines)
        self.assertEqual(first["stage"], "webvpn_ticket_complete")
        self.assertEqual(first["duration_ms"], 1250)
        self.assertEqual(first["outcome"], "ok")
        self.assertEqual(second["stage"], "catalog_context")
        self.assertEqual(second["outcome"], "marker")
        self.assertIsNone(second["duration_ms"])
        self.assertEqual(third["duration_ms"], 350)
        self.assertEqual(third["http_status"], 200)
        for record in (first, second, third):
            self.assertEqual(record["attempt"], 2)
            self.assertIsNone(record["operation_id_sha256_12"])
            self.assertEqual(record["reason"], "http-layer")
            self.assertIsInstance(record["ts"], int)
            self.assertTrue(set(record) <= LoginStageTelemetry.CLOSED_KEYS)

    def test_unknown_stage_names_are_dropped(self):
        writer = LoginStageTelemetry(self.directory)
        writer("webvpn_totally_unknown", {"duration_ms": 5, "outcome": "ok"})
        writer("catalog_not_in_closed_set")
        writer("webvpn_portal_probe", {"url": "https://synthetic.invalid/x"})  # 阶段在闭集内
        lines = self._lines(writer)
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["stage"], "webvpn_portal_probe")
        self.assertNotIn("url", record)

    def test_error_emits_map_into_closed_outcomes(self):
        writer = LoginStageTelemetry(self.directory)
        writer("webvpn_ticket_error", {"elapsed_seconds": 0.5, "error_type": "ReadTimeout"})
        writer("icourse_ticket_error", {"elapsed_seconds": 0.6, "error_type": "ConnectionError"})
        writer("icourse_ticket_complete", {"elapsed_seconds": 1.0, "redirects": None, "recovered_after": "ReadTimeout"})
        outcomes = [json.loads(line)["outcome"] for line in self._lines(writer)]
        self.assertEqual(outcomes, ["timeout", "connection_error", "recovered"])

    def test_writer_failures_never_affect_login(self):
        blocker = self.directory / "blocker.file"
        blocker.write_text("occupied", encoding="utf-8")
        writer = LoginStageTelemetry(blocker)  # 目录位置被文件占用 → mkdir 必败
        writer("catalog_context")  # 必须静默吞掉，绝不抛出

    def test_set_attempt_is_safe(self):
        writer = LoginStageTelemetry(self.directory)
        writer.set_attempt(3)
        writer("catalog_context")
        record = json.loads(self._lines(writer)[0])
        self.assertEqual(record["attempt"], 3)

    def test_bounded_retention_keeps_newest_20_and_prunes_once(self):
        now = time.time()
        # 预置 25 个假 artifact：mtime 逐一递减，index 越大越旧
        names = [f"login-stage-{9000 + index}.jsonl" for index in range(25)]
        for index, name in enumerate(names):
            path = self.directory / name
            path.write_text('{"stage": "catalog_context"}\n', encoding="utf-8")
            os.utime(path, (now - index * 60, now - index * 60))
        writer = LoginStageTelemetry(self.directory)
        writer("catalog_context")  # 首次写入：先 prune 一次，再落当前文件

        kept = sorted(self.directory.glob("login-stage-*.jsonl"))
        # 语义：prune 发生在当前文件写入之前 → 保留 20 个历史最新 + 1 个当前
        self.assertEqual(len(kept), 21)
        remaining_names = {path.name for path in kept}
        self.assertTrue(writer._path.exists(), "当前 artifact 必须保留")
        self.assertIn(writer._path.name, remaining_names)
        for index in range(20):  # 最新 20 个历史文件保留
            self.assertIn(names[index], remaining_names)
        for index in range(20, 25):  # 最旧 5 个被清理
            self.assertNotIn(names[index], remaining_names)

        # 每实例至多一次：之后新出现的更旧文件不再触发第二次 prune
        for index in range(10):
            late = self.directory / f"login-stage-{8000 + index}.jsonl"
            late.write_text('{"stage": "catalog_context"}\n', encoding="utf-8")
            os.utime(late, (now - 3600 - index, now - 3600 - index))
        writer("catalog_context")  # 第二次写入：不再 prune
        self.assertEqual(len(list(self.directory.glob("login-stage-*.jsonl"))), 31)

    def test_prune_never_touches_non_matching_entries(self):
        now = time.time()
        # 20 个较新的匹配文件占满保留名额，把最旧的"同名目录"挤进待删区间
        for index in range(20):
            dummy = self.directory / f"login-stage-{9500 + index}.jsonl"
            dummy.write_text("{}", encoding="utf-8")
            os.utime(dummy, (now - index * 60, now - index * 60))
        # 用户资产：非匹配文件 + 子目录（内含一个匹配名文件）必须原样保留
        benchmark_file = self.directory / "benchmark-results.json"
        benchmark_file.write_text("{}", encoding="utf-8")
        nested_dir = self.directory / "benchmark-telemetry"
        nested_dir.mkdir()
        nested_file = nested_dir / "login-stage-7777.jsonl"
        nested_file.write_text("keep\n", encoding="utf-8")
        # 与 artifact 同名模式的目录：is_file() 守卫必须跳过，绝不 unlink 目录
        patterned_dir = self.directory / "login-stage-oldest.jsonl"
        patterned_dir.mkdir()
        os.utime(patterned_dir, (now - 86400, now - 86400))

        writer = LoginStageTelemetry(self.directory)
        writer("catalog_context")  # 触发 prune

        self.assertEqual(benchmark_file.read_text(encoding="utf-8"), "{}")
        self.assertTrue(nested_dir.is_dir())
        self.assertEqual(nested_file.read_text(encoding="utf-8"), "keep\n")
        self.assertTrue(patterned_dir.is_dir(), "同名模式的目录必须被 is_file() 守卫跳过")
        self.assertEqual(
            len(list(self.directory.glob("login-stage-*.jsonl"))),
            22,
            "20 个历史最新 + 1 个当前文件 + 1 个被守卫跳过的同名目录（未删除）",
        )


class ArtifactLeakageTests(unittest.TestCase):
    """端到端：真实 WebVPNSession 走完整离线链路，检查 artifact 每一行。"""

    def test_artifact_lines_never_leak_request_data(self):
        class ScriptedSuccessVpn(WebVPNSession):
            def __init__(self, step_callback=None, *, proxy_url="", transport="curl_h2"):
                super().__init__(step_callback=step_callback, proxy_url=proxy_url, transport=transport)
                self.session.close()
                self.session = ScriptedSession(success_chain(with_probes=False))

        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        telemetry_dir = (
            PROJECT_ROOT
            / "runtime"
            / "cache"
            / LOGIN_STAGE_CACHE_DIRNAME
            / "test-telemetry"
        )
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            service = CourseLensApplication(Path(directory))
            try:
                service.set_credentials("synthetic.student", "synthetic-password-value")
                with (
                    patch("src.api.webvpn.WebVPNSession", ScriptedSuccessVpn),
                    patch(
                        "src.application.LOGIN_STAGE_CACHE_DIRNAME",
                        f"{LOGIN_STAGE_CACHE_DIRNAME}/test-telemetry",
                    ),
                ):
                    client = service._login_with_retry(max_attempts=1)
                self.assertIsNotNone(client)
            finally:
                service.close()
        try:
            files = sorted(telemetry_dir.glob("login-stage-*.jsonl"))
            self.assertTrue(files, "遥测接线必须至少产出一个 artifact 文件")
            stages = set()
            for path in files:
                for line in path.read_text(encoding="utf-8").splitlines():
                    record = json.loads(line)
                    stages.add(record["stage"])
                    self.assertTrue(record, "每行必须是非空 JSON 对象")
                    self.assertTrue(set(record) <= LoginStageTelemetry.CLOSED_KEYS)
                    self.assertIn(record["stage"], LoginStageTelemetry.CLOSED_STAGES)
                    self.assertIn(record["outcome"], LoginStageTelemetry.CLOSED_OUTCOMES)
                    for forbidden in ("ticket=", "lck=", "http://", "https://", "password"):
                        self.assertNotIn(forbidden, line)
                    payload = {key: value for key, value in record.items() if key != "ts"}
                    self.assertIsNone(
                        re.search(r"\d{10,}", json.dumps(payload)),
                        f"疑似学号/长数字泄漏: {line}",
                    )
            # 端到端 artifact 至少覆盖票腿总时长记录与 attempt 归一化
            self.assertIn("webvpn_ticket_complete", stages)
            self.assertIn("icourse_ticket_complete", stages)
        finally:
            shutil.rmtree(telemetry_dir, ignore_errors=True)


class _CatalogScenarioClient:
    """test_icourse_session_recovery 同款最小假 client。"""

    def __init__(self, alive=True):
        self.vpn = object()
        self.closed = False
        self._alive = alive
        self.with_catalog_calls = 0

    def check_alive(self):
        return self._alive

    def close(self):
        self.closed = True

    def with_catalog_session(self, direct):
        self.with_catalog_calls += 1
        return self


class ScenarioTests(unittest.TestCase):
    """基准场景的快速单元版（全部离线、毫秒级）。"""

    def setUp(self):
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.service = CourseLensApplication(Path(self.temporary.name))
        self.service.set_credentials("synthetic.student", "synthetic-password-value")

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def test_success_stores_client(self):
        class SuccessVpn:
            AUTH_DEADLINE_SECONDS = 30

            def __init__(self, **_kwargs):
                self.session = Mock()
                self.login_calls = 0
                self.icourse_calls = 0

            def begin_authentication(self, _seconds):
                return None

            def end_authentication(self):
                return None

            def login(self, **_kwargs):
                self.login_calls += 1

            def authenticate_icourse(self, **_kwargs):
                self.icourse_calls += 1

        with patch("src.api.webvpn.WebVPNSession", SuccessVpn):
            client = self.service._login_with_retry(max_attempts=3)
        self.assertEqual(client.vpn.login_calls, 1)
        self.assertEqual(client.vpn.icourse_calls, 1)

    def test_wrong_password_fails_fast_without_retry(self):
        attempts = {"count": 0}

        class RejectedVpn:
            def __init__(self, **_kwargs):
                self.session = Mock()

            def login(self, **_kwargs):
                attempts["count"] += 1
                raise FudanCredentialsRejected("synthetic rejected")

        with patch("src.api.webvpn.WebVPNSession", RejectedVpn), patch("time.sleep"):
            with self.assertRaises(FudanCredentialsRejected):
                self.service._login_with_retry(max_attempts=3)
        self.assertEqual(attempts["count"], 1)

    def test_webvpn_timeout_exhausts_attempts_and_reports_timeout(self):
        class TimeoutVpn:
            def __init__(self, **_kwargs):
                self.session = Mock()

            def login(self, **_kwargs):
                raise requests.ReadTimeout("synthetic timeout")

        with patch("src.api.webvpn.WebVPNSession", TimeoutVpn), patch("time.sleep"):
            with self.assertRaises(requests.ReadTimeout):
                self.service._login_with_retry(max_attempts=3)
        snapshot = self.service.authentication_snapshot()
        self.assertEqual(snapshot["state"], "degraded")
        self.assertEqual(snapshot["code"], "timeout")

    def test_5xx_stage_failure_exhausts_attempts(self):
        class ServerErrorVpn:
            def __init__(self, **_kwargs):
                self.session = Mock()

            def login(self, **_kwargs):
                raise RuntimeError("Authentication failed: {'code': 500}")

        with patch("src.api.webvpn.WebVPNSession", ServerErrorVpn), patch("time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "500"):
                self.service._login_with_retry(max_attempts=3)
        snapshot = self.service.authentication_snapshot()
        self.assertEqual(snapshot["code"], "fudan_login_failed")

    def test_double_route_failure_keeps_primary_cause(self):
        from src.api.icourse_direct import DirectICourseError

        base = Mock()
        base.closed = False
        base.check_alive.return_value = True
        base.list_authorized_courses.side_effect = DirectICourseError(
            "catalog_ticket_missing"
        )
        fallback = Mock()
        fallback.list_authorized_courses.side_effect = DirectICourseError(
            "catalog_timeout"
        )
        base.with_catalog_session.return_value = fallback

        with (
            patch.object(self.service.network, "service_proxies", return_value=[""]),
            patch("src.api.icourse_direct.DirectICourseSession", return_value=Mock()),
        ):
            with self.assertRaises(CatalogRefreshError) as caught:
                self.service._load_authorized_catalog(base)
        # 闭集优先级保留主因：catalog_timeout 高于 catalog_ticket_missing
        self.assertEqual(caught.exception.code, "catalog_timeout")
        self.assertFalse(base.closed)

    def test_catalog_failure_keeps_verified_session(self):
        client = _CatalogScenarioClient(alive=True)
        base = Mock()
        base.check_alive.return_value = True
        base.list_authorized_courses.side_effect = RuntimeError("synthetic")
        base.with_catalog_session.return_value = Mock(
            list_authorized_courses=Mock(side_effect=RuntimeError("synthetic"))
        )
        with (
            patch.object(self.service.network, "service_proxies", return_value=[""]),
            patch("src.api.icourse_direct.DirectICourseSession", return_value=Mock()),
        ):
            with self.assertRaises(CatalogRefreshError):
                self.service._load_authorized_catalog(base)
        self.assertFalse(client.closed)

    def test_confirmed_session_expiry_requires_login(self):
        client = Mock()
        client.check_alive.return_value = False
        client.list_authorized_courses.side_effect = RuntimeError("synthetic")
        with patch.object(self.service.network, "service_proxies", return_value=[]):
            with self.assertRaises(CatalogRefreshError) as caught:
                self.service._load_authorized_catalog(client)
        self.assertEqual(caught.exception.code, "catalog_session_expired")

    def test_concurrent_double_submit_logs_in_once(self):
        entered = threading.Event()
        release = threading.Event()
        attempts = {"count": 0}

        class SlowVpn:
            AUTH_DEADLINE_SECONDS = 30

            def __init__(self, **_kwargs):
                self.session = Mock()

            def begin_authentication(self, _seconds):
                entered.set()
                release.wait(timeout=3)

            def end_authentication(self):
                return None

            def login(self, **_kwargs):
                attempts["count"] += 1

            def authenticate_icourse(self, **_kwargs):
                pass

        results = []
        errors = []

        def _submit():
            try:
                results.append(self.service.client())
            except Exception as exc:  # pragma: no cover - 只在回归时触发
                errors.append(exc)

        with patch("src.api.webvpn.WebVPNSession", SlowVpn):
            threads = [threading.Thread(target=_submit) for _ in range(2)]
            threads[0].start()
            self.assertTrue(entered.wait(timeout=3), "第一次登录应已进入")
            threads[1].start()
            time.sleep(0.3)  # 让第二次提交先阻塞在 refresh 锁上
            release.set()
            threads[0].join(timeout=5)
            threads[1].join(timeout=5)
        self.assertFalse(errors)
        self.assertFalse(threads[0].is_alive() or threads[1].is_alive())
        self.assertEqual(attempts["count"], 1, "并发双提交只允许一次真实登录")
        self.assertEqual(len(results), 2)
        self.assertIs(results[0], results[1])


class OfflineConventionTests(unittest.TestCase):
    """约定自检：本文件必须保持纯离线（fake 驱动）。"""

    def test_no_real_network_tooling_in_this_module(self):
        source = Path(__file__).read_text(encoding="utf-8")
        # 动态拼接待禁用词，避免自检文本自身命中
        forbidden = ["url" + "open", "urllib." + "request", "socket." + "socket"]
        forbidden.append("threading" + "HTTPServer".capitalize())
        forbidden.append("ht" + "tx")
        forbidden.append("curl_" + "requests")
        for needle in forbidden:
            self.assertNotIn(needle, source)


if __name__ == "__main__":
    unittest.main()
