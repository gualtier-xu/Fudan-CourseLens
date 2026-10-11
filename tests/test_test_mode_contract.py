"""COURSELENS_TEST_MODE 双态契约钉（测试台架批 2·件②M1，TESTBENCH-DESIGN-1 §2）。

钉面五层：
1. 契约闭集（模式值/egress 白名单档/错误码）与发布版惰性（未设 env = 恒放行）。
2. serve 启动契约 fail-closed：沙盒数据目录强制（单一语义/冲突/默认目录拒）、
   6268 端口禁用、synthetic 档禁 ALLOW、非法 env 值拒——人话 + 闭集码。
3. 出站 fail-closed 五处客户端接线行为钉（webvpn/icourse_direct/github×2/
   deepseek/update/live）+ 审计行证据（egress_blocked+host 可事后复核）。
4. --e2e 别名 + 测试模式/发布版双态启动冒烟（启动/health 零差异确认）。
5. 收编与协同：synthetic_shell_server=正式 synthetic 后端（模式归一/引导
   旗标闭集/装配前故障注入）+ 件③实例管理器 TEST_MODE+claimed 实例组合钉。

发布版惰性源码钉：env 字面量只允许出现在 src/runtime/test_mode.py；
五处接线 import 面闭集钉（app + 五客户端族 + update + live_room）。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from src.runtime import test_mode as tm
from src.runtime.lifecycle import LifecycleError
from src.runtime.test_mode import TestModeContractError as _ContractError

MODE_ENV = tm.TEST_MODE_ENV
ALLOW_ENV = tm.EGRESS_ALLOW_ENV
DATA_ENV = tm.DATA_DIR_ENV

CANONICAL_PYTHON = Path(
    ROOT / ".venv-client-py310" / "Scripts" / "python.exe"
)
PYTHON = str(CANONICAL_PYTHON if CANONICAL_PYTHON.exists() else sys.executable)


def _clean_test_mode_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {MODE_ENV, ALLOW_ENV}}
    if extra:
        env.update(extra)
    return env


class _EnvCase(unittest.TestCase):
    def setUp(self) -> None:
        self._env_snapshot = {
            k: os.environ.get(k) for k in (MODE_ENV, ALLOW_ENV, DATA_ENV)
        }

    def tearDown(self) -> None:
        for key, value in self._env_snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# 1. 契约闭集与发布版惰性（单元）
# ---------------------------------------------------------------------------


class TestContractClosedSets(_EnvCase):
    def test_mode_values_are_closed(self) -> None:
        self.assertEqual(tm.TEST_MODE_VALUES, {"synthetic", "real"})
        self.assertEqual(tm.EGRESS_TIER_VALUES, {"school:readonly", "github:api"})
        self.assertEqual(
            tm.TEST_MODE_ERROR_CODES,
            {
                "test_mode_invalid",
                "test_data_dir_required",
                "test_data_dir_conflict",
                "test_data_dir_default_forbidden",
                "test_port_reserved",
                "test_egress_allow_invalid",
                "test_egress_allow_synthetic",
                "test_mode_conflict",
                # TB-W4 件③M2：serve 实例注册名非法（COURSELENS_TEST_INSTANCE
                # 拼写/路径逃逸）——启动契约 fail-closed 拒启。
                "test_instance_name_invalid",
                # TB-W4 件②M3：real 档测试身份凭据文件四码（模式联动/白名单档
                # 联动/文件缺失/形态非法）——真 UIS 登录链 fail-closed 前置。
                "test_credentials_mode_conflict",
                "test_credentials_tier_required",
                "test_credentials_file_missing",
                "test_credentials_file_invalid",
            },
        )

    def test_resolve_unset_is_empty_gate_open(self) -> None:
        environ = _clean_test_mode_env()
        self.assertEqual(tm.resolve_test_mode(environ), "")
        self.assertFalse(tm.is_test_mode(environ))
        # 发布版唯一分支：未设 env 时任意主机恒放行。
        self.assertEqual(tm.egress_denial_reason("api.deepseek.com", mode=""), "")
        self.assertEqual(tm.egress_denial_reason("id.fudan.edu.cn", mode=""), "")

    def test_resolve_valid_modes(self) -> None:
        self.assertEqual(tm.resolve_test_mode({MODE_ENV: "synthetic"}), "synthetic")
        self.assertEqual(tm.resolve_test_mode({MODE_ENV: "real"}), "real")

    def test_resolve_invalid_raises_closed_code(self) -> None:
        with self.assertRaises(_ContractError) as caught:
            tm.resolve_test_mode({MODE_ENV: "banana"})
        self.assertEqual(caught.exception.code, "test_mode_invalid")
        self.assertIn("synthetic", caught.exception.human_message)

    def test_gate_fail_closed_on_invalid_mode(self) -> None:
        # 运行时门遇非法值 = fail-closed 拒绝（不抛，审计行承载）。
        self.assertEqual(
            tm.egress_denial_reason("127.0.0.1", mode="invalid"), "test_mode_invalid"
        )


class TestEgressWhitelist(_EnvCase):
    def test_synthetic_loopback_only(self) -> None:
        for host in tm.LOOPBACK_HOSTS:
            self.assertEqual(tm.egress_denial_reason(host, mode="synthetic"), "")
        for host in (
            "api.deepseek.com",
            "id.fudan.edu.cn",
            "icourse.fudan.edu.cn",
            "api.github.com",
            "github.com",
            "example.com",
        ):
            self.assertEqual(tm.egress_denial_reason(host, mode="synthetic"), "egress_blocked")

    def test_real_default_deny_all_external(self) -> None:
        for host in ("api.github.com", "id.fudan.edu.cn", "api.deepseek.com"):
            self.assertEqual(
                tm.egress_denial_reason(host, mode="real", tiers=frozenset()),
                "egress_blocked",
            )

    def test_real_school_readonly_tier_exact_set(self) -> None:
        tiers = frozenset({tm.EGRESS_TIER_SCHOOL_READONLY})
        for host in tm.TIER_HOSTS[tm.EGRESS_TIER_SCHOOL_READONLY]:
            self.assertEqual(tm.egress_denial_reason(host, mode="real", tiers=tiers), "")
        # 档外主机仍然拒绝（deepseek/GitHub 不随 school 档放行）。
        self.assertEqual(tm.egress_denial_reason("api.deepseek.com", mode="real", tiers=tiers), "egress_blocked")
        self.assertEqual(tm.egress_denial_reason("api.github.com", mode="real", tiers=tiers), "egress_blocked")

    def test_real_github_api_tier_exact_set(self) -> None:
        tiers = frozenset({tm.EGRESS_TIER_GITHUB_API})
        for host in tm.TIER_HOSTS[tm.EGRESS_TIER_GITHUB_API]:
            self.assertEqual(tm.egress_denial_reason(host, mode="real", tiers=tiers), "")
        self.assertEqual(tm.egress_denial_reason("id.fudan.edu.cn", mode="real", tiers=tiers), "egress_blocked")

    def test_allow_env_parsing(self) -> None:
        self.assertEqual(
            tm.allowed_egress_tiers({MODE_ENV: "real", ALLOW_ENV: "github:api, school:readonly"}),
            {"github:api", "school:readonly"},
        )
        with self.assertRaises(_ContractError) as caught:
            tm.allowed_egress_tiers({MODE_ENV: "real", ALLOW_ENV: "everything"})
        self.assertEqual(caught.exception.code, "test_egress_allow_invalid")
        # synthetic 档设 ALLOW = 契约违例（合成档结构上不出本机）。
        with self.assertRaises(_ContractError) as caught:
            tm.allowed_egress_tiers({MODE_ENV: "synthetic", ALLOW_ENV: "github:api"})
        self.assertEqual(caught.exception.code, "test_egress_allow_synthetic")
        # 未设 env：永不怕 ALLOW（惰性）。
        self.assertEqual(tm.allowed_egress_tiers({ALLOW_ENV: "github:api"}), frozenset())

    def test_ensure_egress_allowed_audit_and_raise(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(tm.EgressBlockedError) as caught:
                tm.ensure_egress_allowed("https://api.deepseek.com/user/balance", purpose="pin", environ={MODE_ENV: "synthetic"})
        self.assertEqual(caught.exception.code, "egress_blocked")
        self.assertEqual(caught.exception.host, "api.deepseek.com")
        audit = err.getvalue()
        self.assertIn("[egress] blocked", audit)
        self.assertIn("code=egress_blocked", audit)
        self.assertIn("host=api.deepseek.com", audit)
        self.assertIn("purpose=pin", audit)
        # 放行侧：白名单档命中 = allowed 审计行（逐 host 事后复核）。
        err2 = io.StringIO()
        with redirect_stderr(err2):
            host = tm.ensure_egress_allowed(
                "https://id.fudan.edu.cn/authorize",
                purpose="pin",
                environ={MODE_ENV: "real", ALLOW_ENV: "school:readonly"},
            )
        self.assertEqual(host, "id.fudan.edu.cn")
        self.assertIn("[egress] allowed", err2.getvalue())
        self.assertIn("detail=school:readonly", err2.getvalue())
        # 环回放行零噪音（合成壳高频路径不刷审计）。
        err3 = io.StringIO()
        with redirect_stderr(err3):
            tm.ensure_egress_allowed("http://127.0.0.1:17705/api", purpose="pin", environ={MODE_ENV: "synthetic"})
        self.assertEqual(err3.getvalue(), "")
        # 发布版：恒放行零打印。
        err4 = io.StringIO()
        with redirect_stderr(err4):
            tm.ensure_egress_allowed("https://example.com/", purpose="pin", environ={})
        self.assertEqual(err4.getvalue(), "")


# ---------------------------------------------------------------------------
# 2. 沙盒数据目录 + 端口契约（单元）
# ---------------------------------------------------------------------------


class TestDataDirContract(_EnvCase):
    def test_requires_explicit_dir(self) -> None:
        with self.assertRaises(_ContractError) as caught:
            tm.test_data_dir_contract(None, environ={MODE_ENV: "synthetic"})
        self.assertEqual(caught.exception.code, "test_data_dir_required")

    def test_conflict_between_env_and_param(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            with self.assertRaises(_ContractError) as caught:
                tm.test_data_dir_contract(b, environ={MODE_ENV: "synthetic", DATA_ENV: a})
            self.assertEqual(caught.exception.code, "test_data_dir_conflict")

    def test_env_single_semantic_wins(self) -> None:
        with tempfile.TemporaryDirectory() as a:
            resolved = tm.test_data_dir_contract(None, environ={MODE_ENV: "synthetic", DATA_ENV: a})
            self.assertEqual(resolved, Path(a).resolve())

    def test_platform_default_forbidden(self) -> None:
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        default_dir = str(Path(local) / "CourseLens")
        with self.assertRaises(_ContractError) as caught:
            tm.test_data_dir_contract(None, environ={MODE_ENV: "synthetic", DATA_ENV: default_dir})
        self.assertEqual(caught.exception.code, "test_data_dir_default_forbidden")
        # 产品仓内默认数据根（开发树真实数据）同样禁指。
        with self.assertRaises(_ContractError) as caught:
            tm.test_data_dir_contract(
                None,
                environ={MODE_ENV: "synthetic", DATA_ENV: str(ROOT / "runtime" / "data")},
            )
        self.assertEqual(caught.exception.code, "test_data_dir_default_forbidden")

    def test_sandbox_dir_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as sandbox:
            resolved = tm.test_data_dir_contract(
                sandbox, environ={MODE_ENV: "synthetic", DATA_ENV: sandbox}
            )
            self.assertEqual(resolved, Path(sandbox).resolve())

    def test_publish_data_dir_env(self) -> None:
        with tempfile.TemporaryDirectory() as sandbox:
            try:
                tm.publish_data_dir_env(Path(sandbox))
                self.assertEqual(os.environ[DATA_ENV], str(Path(sandbox).resolve()))
            finally:
                os.environ.pop(DATA_ENV, None)


class TestPortContract(_EnvCase):
    def test_product_default_port_forbidden(self) -> None:
        with self.assertRaises(_ContractError) as caught:
            tm.ensure_test_port_allowed(6268)
        self.assertEqual(caught.exception.code, "test_port_reserved")

    def test_os_port_and_test_range_allowed(self) -> None:
        self.assertEqual(tm.ensure_test_port_allowed(0), 0)
        self.assertEqual(tm.ensure_test_port_allowed(17705), 17705)


# ---------------------------------------------------------------------------
# 3. serve 启动契约 fail-closed（进程内直调；只测拒绝面，不起服务）
# ---------------------------------------------------------------------------


class TestServeStartupContract(_EnvCase):
    def _expect_refusal(self, data_dir, port, code: str) -> None:
        from src.app import serve

        with self.assertRaises(LifecycleError) as caught:
            serve(data_dir=data_dir, port=port, open_browser=False)
        self.assertEqual(caught.exception.code, code)

    def test_data_dir_required(self) -> None:
        os.environ[MODE_ENV] = "synthetic"
        os.environ.pop(DATA_ENV, None)
        self._expect_refusal(None, 0, "test_data_dir_required")

    def test_data_dir_conflict(self) -> None:
        os.environ[MODE_ENV] = "synthetic"
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            os.environ[DATA_ENV] = a
            self._expect_refusal(b, 0, "test_data_dir_conflict")

    def test_default_dir_forbidden(self) -> None:
        os.environ[MODE_ENV] = "synthetic"
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        os.environ[DATA_ENV] = str(Path(local) / "CourseLens")
        self._expect_refusal(None, 0, "test_data_dir_default_forbidden")

    def test_product_port_reserved(self) -> None:
        os.environ[MODE_ENV] = "synthetic"
        with tempfile.TemporaryDirectory() as sandbox:
            os.environ[DATA_ENV] = sandbox
            self._expect_refusal(None, 6268, "test_port_reserved")

    def test_invalid_mode(self) -> None:
        os.environ[MODE_ENV] = "banana"
        os.environ.pop(DATA_ENV, None)
        self._expect_refusal(None, 0, "test_mode_invalid")

    def test_allow_under_synthetic_refused(self) -> None:
        os.environ[MODE_ENV] = "synthetic"
        os.environ[ALLOW_ENV] = "github:api"
        with tempfile.TemporaryDirectory() as sandbox:
            os.environ[DATA_ENV] = sandbox
            self._expect_refusal(None, 0, "test_egress_allow_synthetic")


# ---------------------------------------------------------------------------
# 4. 出站 fail-closed 五处客户端接线行为钉（全部离线）
# ---------------------------------------------------------------------------


class TestEgressWiring(_EnvCase):
    def test_webvpn_deadline_session_blocks_external(self) -> None:
        from src.api.webvpn import _DeadlineSession

        session = _DeadlineSession(deadline=lambda: None)
        prepared = requests.Request("GET", "https://id.fudan.edu.cn/authorize").prepare()
        err = io.StringIO()
        with redirect_stderr(err):
            with mock.patch("requests.adapters.HTTPAdapter.send") as adapter_send:
                os.environ[MODE_ENV] = "synthetic"
                with self.assertRaises(requests.ConnectionError) as caught:
                    session.send(prepared, timeout=1)
        # 门先于适配器：外部主机根本不会发起连接。
        adapter_send.assert_not_called()
        self.assertIn("egress_blocked", str(caught.exception))
        self.assertIn("host=id.fudan.edu.cn", err.getvalue())

    def test_webvpn_deadline_session_allows_loopback(self) -> None:
        from src.api.webvpn import _DeadlineSession

        session = _DeadlineSession(deadline=lambda: None)
        prepared = requests.Request("GET", "http://127.0.0.1:9/?ping").prepare()
        sentinel = requests.Response()  # Session.send 会回填 elapsed 等属性
        with mock.patch("requests.adapters.HTTPAdapter.send", return_value=sentinel):
            os.environ[MODE_ENV] = "synthetic"
            self.assertIs(session.send(prepared, timeout=1), sentinel)

    def test_icourse_direct_blocks_external(self) -> None:
        from src.api.icourse_direct import DirectICourseSession

        session = DirectICourseSession()
        err = io.StringIO()
        with redirect_stderr(err):
            os.environ[MODE_ENV] = "synthetic"
            with self.assertRaises(RuntimeError) as caught:
                session._request("GET", "https://icourse.fudan.edu.cn/catalog")
        self.assertEqual(getattr(caught.exception, "code", ""), "network_unavailable")
        self.assertIn("host=icourse.fudan.edu.cn", err.getvalue())

    def test_github_client_blocks_external(self) -> None:
        from src.remote.github_client import GitHubClient

        client = GitHubClient("token-pin")
        err = io.StringIO()
        with redirect_stderr(err):
            os.environ[MODE_ENV] = "synthetic"
            with self.assertRaises(RuntimeError) as caught:
                client._request("GET", "/zen")
        self.assertEqual(caught.exception.code, "egress_blocked")
        self.assertIn("host=api.github.com", err.getvalue())

    def test_github_app_blocks_external(self) -> None:
        from src.remote.github_app import GitHubAppClient

        stub = GitHubAppClient.__new__(GitHubAppClient)  # 门在属性触达前先拒
        err = io.StringIO()
        with redirect_stderr(err):
            os.environ[MODE_ENV] = "synthetic"
            with self.assertRaises(RuntimeError) as caught:
                GitHubAppClient._request_external(stub, "POST", "https://api.github.com/app")
        self.assertEqual(caught.exception.code, "github_unreachable")
        self.assertIn("egress_blocked", str(caught.exception))
        self.assertIn("host=api.github.com", err.getvalue())

    def test_deepseek_balance_blocked_to_unavailable(self) -> None:
        from src.application import CourseLensApplication

        scratch = Path(ROOT / "runtime" / "cache")
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch, prefix="tbmodem1-pin-") as temp:
            app = CourseLensApplication(Path(temp))
            try:
                app.set_deepseek_key("sk-tbmodem1-pin", remember=False)
                err = io.StringIO()
                with redirect_stderr(err):
                    os.environ[MODE_ENV] = "synthetic"
                    snapshot = app.deepseek_balance_snapshot()
                self.assertEqual(snapshot["state"], "unavailable")
                self.assertIn("code=egress_blocked", err.getvalue())
                self.assertIn("host=api.deepseek.com", err.getvalue())
            finally:
                app.close()

    def test_update_fetch_blocked_and_loopback_allowed(self) -> None:
        from src.update.service import UpdateError, UpdateService

        svc = UpdateService.__new__(UpdateService)
        svc.transport = lambda url, destination, limit, **kwargs: b"tbmodem1"
        svc.fault = None
        err = io.StringIO()
        with redirect_stderr(err):
            os.environ[MODE_ENV] = "synthetic"
            with self.assertRaises(UpdateError) as caught:
                svc._fetch(
                    "https://github.com/fudan/repo/releases/download/v1/m.zip",
                    None, 1,
                    SimpleNamespace(
                        manifest_url="https://github.com/fudan/repo/releases/download/v1/m.zip",
                        allowed_hosts={"github.com"},
                    ),
                )
        self.assertEqual(caught.exception.code, "source_url_blocked")
        self.assertIn("host=github.com", err.getvalue())
        # 环回注入 transport（合成/本机桩形态）放行：_fetch 的更新链自身
        # 反 SSRF 守卫（_validate_hop_url 只收全局可路由地址）与本钉无关，
        # 打桩只验证门的放行序（环回 → 注入 transport，零连接）。
        loop_manifest = "https://127.0.0.1:9/manifest.json"
        from urllib.parse import urlsplit

        with mock.patch(
            "src.update.service._validate_hop_url",
            return_value=(urlsplit(loop_manifest), "127.0.0.1"),
        ):
            payload = svc._fetch(
                loop_manifest, None, 1,
                SimpleNamespace(manifest_url=loop_manifest, allowed_hosts={"127.0.0.1"}),
            )
        self.assertEqual(payload, b"tbmodem1")

    def test_live_room_blocked_to_closed_code(self) -> None:
        from src.runtime.live_room import LiveRoomError, LiveRoomService

        svc = LiveRoomService.__new__(LiveRoomService)  # 门先于 resolver/播放桩
        err = io.StringIO()
        out = io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            os.environ[MODE_ENV] = "synthetic"
            with self.assertRaises(LiveRoomError) as caught:
                svc._request(None, "https://icourse.fudan.edu.cn/live/a.m3u8", range_header="")
        self.assertEqual(caught.exception.args[0], "live_upstream_unreachable")
        # 门审计行（stderr）+ live 闭集收编 tee 行（stdout）双证据。
        self.assertIn("code=egress_blocked", err.getvalue())
        self.assertIn("host=icourse.fudan.edu.cn", err.getvalue())
        self.assertIn("reason=egress_blocked", out.getvalue())


# ---------------------------------------------------------------------------
# 5. 发布版惰性钉（源码面 + 双态启动冒烟）
# ---------------------------------------------------------------------------


class TestReleaseLaziness(_EnvCase):
    def test_env_literals_confined_to_contract_module(self) -> None:
        offenders: list[str] = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if path.name == "test_mode.py":
                continue
            if "COURSELENS_TEST_MODE" in text or "COURSELENS_TEST_EGRESS_ALLOW" in text:
                offenders.append(str(path))
        self.assertEqual(offenders, [], "测试模式 env 字面量只允许出现在契约模块")

    def test_wiring_import_surface_closed(self) -> None:
        expected = {
            "app.py",  # 启动契约（数据目录/端口/--e2e）
            "webvpn.py",  # 学校客户端（requests 瓶颈 + curl 票腿）
            "icourse_direct.py",  # 学校客户端（直连）
            "github_client.py",  # GitHub 远程链
            "github_app.py",  # GitHub App
            "application.py",  # DeepSeek 余额
            "live_room.py",  # 直播/媒体上游
        }
        wired: set[str] = set()
        for path in (ROOT / "src").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "test_mode import" in text or "from src.runtime import test_mode" in text:
                wired.add(path.name)
        missing = expected - wired
        self.assertEqual(missing, set(), f"五处客户端接线缺漏: {sorted(missing)}")
        # update/service.py 经相对链也可接线；显式核对。
        self.assertIn(
            "ensure_egress_allowed",
            (ROOT / "src" / "update" / "service.py").read_text(encoding="utf-8"),
        )

    def _run_serve_smoke(
        self, extra_env: dict[str, str], args: list[str], *, expect_mode_banner: str | None
    ) -> dict:
        with tempfile.TemporaryDirectory(dir=ROOT / "runtime" / "cache", prefix="tbmodem1-smoke-") as sandbox:
            env = _clean_test_mode_env({DATA_ENV: sandbox})
            env.update(extra_env)
            proc = subprocess.Popen(
                [PYTHON, "-m", "src", "serve", *args],
                cwd=str(ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            )
            out_tail: list[str] = []
            err_tail: list[str] = []

            def _drain(pipe, sink: list[str]) -> None:
                for line in pipe:
                    sink.append(line)

            threads = [
                threading.Thread(target=_drain, args=(proc.stdout, out_tail), daemon=True),
                threading.Thread(target=_drain, args=(proc.stderr, err_tail), daemon=True),
            ]
            for thread in threads:
                thread.start()
            try:
                ui_line = None
                deadline = time.monotonic() + 60.0
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        break
                    ui_line = next((l for l in out_tail if "Fudan CourseLens UI:" in l), None)
                    if ui_line:
                        break
                    time.sleep(0.05)
                self.assertTrue(ui_line, f"serve 未就绪：out={out_tail!r} err={err_tail!r}")
                url = ui_line.split("Fudan CourseLens UI:", 1)[1].strip()
                health: dict = {}
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=3) as resp:
                            health = {"status": resp.status, "body": json.loads(resp.read().decode("utf-8"))}
                        if health.get("body", {}).get("ok"):
                            break
                    except (urllib.error.URLError, OSError, ValueError):
                        pass
                    time.sleep(0.2)
                self.assertEqual(health.get("status"), 200, f"health 未就绪：{health} err={err_tail!r}")
                self.assertTrue(health["body"]["ok"], f"health not ok: {health}")
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                for thread in threads:
                    thread.join(timeout=5)
            stdout = "".join(out_tail)
            stderr = "".join(err_tail)
        self.assertNotIn("[egress]", stdout + stderr, "冒烟期不应有任何 egress 审计行（零外联）")
        if expect_mode_banner is None:
            self.assertNotIn("测试模式", stdout, "发布版启动不得出现测试模式横幅")
        else:
            self.assertIn(f"测试模式：{expect_mode_banner}", stdout)
        return {"stdout": stdout, "stderr": stderr}

    def test_release_startup_zero_diff_smoke(self) -> None:
        """发布版惰性：不设 env 的启动路径 = 就绪 + health 零差异 + 零测试模式痕迹。"""
        self._run_serve_smoke({}, ["--port", "0", "--no-open"], expect_mode_banner=None)

    def test_synthetic_mode_startup_smoke(self) -> None:
        self._run_serve_smoke(
            {MODE_ENV: "synthetic"}, ["--port", "0", "--no-open"], expect_mode_banner="synthetic"
        )

    def test_e2e_alias_smoke(self) -> None:
        """--e2e ≡ COURSELENS_TEST_MODE=real：别名生效且横幅可见。"""
        self._run_serve_smoke(
            {}, ["--port", "0", "--no-open", "--e2e"], expect_mode_banner="real"
        )

    def test_e2e_conflicts_with_synthetic_env(self) -> None:
        env = _clean_test_mode_env({MODE_ENV: "synthetic", DATA_ENV: str(ROOT / "runtime" / "cache")})
        proc = subprocess.run(
            [PYTHON, "-m", "src", "serve", "--e2e"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--e2e", proc.stderr)
        self.assertIn("synthetic", proc.stderr)


# ---------------------------------------------------------------------------
# 6. 件③实例管理器协同钉（TEST_MODE + claimed 实例组合）
# ---------------------------------------------------------------------------


class TestInstanceManagerCombo(_EnvCase):
    def test_claimed_instance_satisfies_contract(self) -> None:
        from tests.testbench.instances import (
            TEST_PORT_MAX,
            TEST_PORT_MIN,
            TestInstanceManager,
        )

        handle = None
        instance_dir = None
        try:
            manager = self._manager()
            handle = manager.claim("tbmodem1-pin-backend", owner_lane="TB-MODE-M1")
            instance_dir = manager.registry_dir / handle.name
            os.environ[MODE_ENV] = "synthetic"
            os.environ[DATA_ENV] = str(handle.data_dir)
            resolved = tm.test_data_dir_contract(None)
            self.assertEqual(resolved, Path(handle.data_dir).resolve())
            self.assertEqual(tm.ensure_test_port_allowed(handle.port), handle.port)
            self.assertTrue(TEST_PORT_MIN <= handle.port <= TEST_PORT_MAX)
        finally:
            if handle is not None:
                handle.release()
            if instance_dir is not None and instance_dir.exists():
                import shutil

                shutil.rmtree(instance_dir, ignore_errors=True)
            os.environ.pop(DATA_ENV, None)
            os.environ.pop(MODE_ENV, None)

    def _manager(self):
        from tests.testbench.instances import TestInstanceManager

        workspace = ROOT.parent
        registry_root = workspace / ".testbench" / "tmp-tbmodem1-pin"
        registry_root.mkdir(parents=True, exist_ok=True)
        return TestInstanceManager(registry_dir=registry_root)

    def test_claimed_instance_with_default_dir_refused(self) -> None:
        from tests.testbench.instances import TestInstanceManager

        handle = None
        instance_dir = None
        try:
            manager = self._manager()
            handle = manager.claim("tbmodem1-pin-default-refusal", owner_lane="TB-MODE-M1")
            instance_dir = manager.registry_dir / handle.name
            os.environ[MODE_ENV] = "synthetic"
            local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            os.environ[DATA_ENV] = str(Path(local) / "CourseLens")
            with self.assertRaises(_ContractError) as caught:
                tm.test_data_dir_contract(None)
            self.assertEqual(caught.exception.code, "test_data_dir_default_forbidden")
        finally:
            if handle is not None:
                handle.release()
            if instance_dir is not None and instance_dir.exists():
                import shutil

                shutil.rmtree(instance_dir, ignore_errors=True)
            os.environ.pop(DATA_ENV, None)
            os.environ.pop(MODE_ENV, None)


# ---------------------------------------------------------------------------
# 7. 件⑥合成壳收编钉（正式 synthetic 后端）
# ---------------------------------------------------------------------------


class TestSyntheticShellIncorporation(_EnvCase):
    def test_shell_mode_resolution(self) -> None:
        self.assertEqual(tm.resolve_synthetic_backend_mode({}), "synthetic")
        self.assertEqual(
            tm.resolve_synthetic_backend_mode({MODE_ENV: "synthetic"}), "synthetic"
        )
        with self.assertRaises(_ContractError) as caught:
            tm.resolve_synthetic_backend_mode({MODE_ENV: "real"})
        self.assertEqual(caught.exception.code, "test_mode_conflict")
        with self.assertRaises(_ContractError) as caught:
            tm.resolve_synthetic_backend_mode({MODE_ENV: "junk"})
        self.assertEqual(caught.exception.code, "test_mode_invalid")

    def test_backend_recipe_documents_entry_and_flags(self) -> None:
        recipe = tm.synthetic_backend_recipe()
        self.assertIn(tm.SYNTHETIC_BACKEND_MODULE, recipe)
        self.assertIn("--seed-clock", recipe)
        self.assertIn("--onboarding-guide", recipe)
        self.assertEqual(
            tm.SYNTHETIC_BACKEND_MODULE, "tests.synthetic_shell_server"
        )
        self.assertEqual(
            tm.ONBOARDING_GUIDE_MODES,
            ("new", "dismissed", "completed", "corrupt", "legacy", "auto"),
        )
        self.assertEqual(tm.SEED_CLOCK_MODES, ("real", "frozen"))

    def _services_for(self, onboarding_guide: str, onboarding_fault: str):
        """经正式后端装配路径构建服务（build→seed/fault→http_services 绑定）。"""
        from src.runtime.http_api import make_handler
        from tests.http_services import http_services
        from tests.synthetic_shell_server import (
            _seed_onboarding_guide,
            apply_synthetic_seeds,
            build_service,
        )

        scratch = Path(ROOT / "runtime" / "cache")
        scratch.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch, prefix="tbmodem1-shell-")
        root = Path(temp.name)
        service = build_service(root, onboarding_profile="ready")
        _seed_onboarding_guide(service, onboarding_guide)
        if onboarding_fault == "actions":
            def _synthetic_onboarding_fault(_action: str, _version: str) -> dict:
                raise RuntimeError("synthetic onboarding action fault")
            service.onboarding_guide_action = _synthetic_onboarding_fault
        apply_synthetic_seeds(service, presets=[], seed_tasks_path=None, seed_clock="real")
        services = http_services(service)
        handler = make_handler(services, ROOT / "frontend")
        return temp, service, services, handler

    def test_adapter_binds_onboarding_transparencies(self) -> None:
        """收编核验：适配层显式透传引导两键（车道不再事后补挂）。"""
        temp, service, services, _handler = self._services_for("completed", "none")
        try:
            self.assertTrue(callable(services.auth_catalog.onboarding_guide_snapshot))
            self.assertTrue(callable(services.auth_catalog.onboarding_guide_action))
            snapshot = services.auth_catalog.onboarding_guide_snapshot()
            self.assertEqual(snapshot.get("disposition"), "completed")
            # maybeAutoOpen 协同：completed 记录 auto_opened=True → 前端不再自动弹。
            self.assertTrue(snapshot.get("auto_opened"))
        finally:
            service.close()
            temp.cleanup()

    def test_onboarding_fault_effective_through_adapter(self) -> None:
        """故障注入在装配前生效：经适配层的 action 确实抛错（恢复路径可演练）。"""
        temp, service, services, _handler = self._services_for("new", "actions")
        try:
            with self.assertRaises(RuntimeError):
                services.auth_catalog.onboarding_guide_action("pin", "v1")
        finally:
            service.close()
            temp.cleanup()

    def test_shell_self_sets_synthetic_mode_subprocess(self) -> None:
        """正式后端 E2E：未设 env 的既有命令行原样可用（自设 synthetic）+ health 200。"""
        env = _clean_test_mode_env()
        proc = subprocess.Popen(
            [PYTHON, "-m", "tests.synthetic_shell_server", "--port", "0"],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
        out_tail: list[str] = []
        err_tail: list[str] = []

        def _drain(pipe, sink: list[str]) -> None:
            for line in pipe:
                sink.append(line)

        threads = [
            threading.Thread(target=_drain, args=(proc.stdout, out_tail), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, err_tail), daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            url = None
            deadline = time.monotonic() + 90.0
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                url_line = next((l for l in out_tail if l.startswith("http://127.0.0.1:")), None)
                if url_line:
                    url = url_line.strip()
                    break
                time.sleep(0.05)
            self.assertTrue(url, f"合成壳未就绪：out={out_tail!r} err={err_tail!r}")
            with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200)
            self.assertTrue(body.get("ok"))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
            for thread in threads:
                thread.join(timeout=5)
        stdout = "".join(out_tail)
        self.assertIn("COURSELENS_TEST_MODE=synthetic", stdout)
        self.assertIn("formal synthetic backend", stdout)
        # 合成壳进程零外联：除本机环回外无任何 egress 审计行。
        self.assertNotIn("[egress] blocked", "".join(err_tail))


if __name__ == "__main__":
    unittest.main()
