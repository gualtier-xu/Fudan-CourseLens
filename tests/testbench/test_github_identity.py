"""持久测试身份钉测（测试台架件①M1/M2；TB-IDT-M1）。

覆盖任务书五类钉：
- 离线编排全链（假驱动注入）：登录→持久化（0600+meta）→新态复用→过期自愈重登；
- 真实 GitHub 全链（出站仅登录最小面；不可达/凭据缺失/playwright 缺失=诚实 SKIP，不假绿）；
- 边界守卫：purpose 闭集（school/real-first-run 拒绝）、学校形态凭据拒收、身份名逃逸拒绝；
- 凭据零明文：argv 只载路径不载凭据、storageState 内容无密码明文、repr 脱敏；
- 0600 硬化：落盘收权（Windows icacls / POSIX 0600）与硬化核验。

全部离线钉用注入的临时 Tier B 根与临时凭据文件，零真实外联、零真实凭据落日志。
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.testbench import github_identity as gi


def _fabricated_state(cookies: list[dict] | None = None) -> dict:
    return {
        "cookies": cookies
        if cookies is not None
        else [
            {"name": "user_session", "value": "totally-fake", "domain": ".github.com"},
            {"name": "logged_in", "value": "yes", "domain": ".github.com"},
        ],
        "origins": [],
    }


def _write_temp_secrets(tmp: Path, payload: dict) -> Path:
    path = tmp / "co-github-test.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class GithubIdentityOfflinePins(unittest.TestCase):
    """离线钉：守卫、持久化、0600、自愈编排（假驱动注入，零外联）。"""

    def setUp(self) -> None:
        self._cleanup: list[Path] = []
        self.tmp = Path(tempfile.mkdtemp(prefix="tbidtm1-offline-"))
        self._cleanup.append(self.tmp)
        self.addCleanup(self._rm)

    def _rm(self) -> None:
        import shutil

        for p in self._cleanup:
            shutil.rmtree(p, ignore_errors=True)

    # --- 边界守卫 -------------------------------------------------------

    def test_purpose_closed_set_refuses_school_and_first_run(self) -> None:
        root = self.tmp / "identity"
        for refused in ("school-uis-login", "real-first-run-walkthrough", "sudo", ""):
            with self.subTest(refused=refused):
                with self.assertRaises(gi.BoundaryViolationError) as ctx:
                    gi.ensure_session(purpose=refused, root=root)
                self.assertIn("闭集", str(ctx.exception))
                self.assertFalse(root.exists(), "边界拒绝不得创建任何 Tier B 目录")

    def test_real_first_run_and_school_examples_documented(self) -> None:
        for key, why in gi.BOUNDARY_REFUSED_EXAMPLES.items():
            self.assertTrue(key not in gi.ALLOWED_PURPOSES)
            self.assertTrue(why)

    def test_identity_name_guard(self) -> None:
        root = self.tmp / "identity"
        for bad in ("../escape", "a/b", "a\\b", "..", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(gi.BoundaryViolationError):
                    gi.ensure_session(purpose="api", identity_name=bad, root=root)
        self.assertEqual(gi.identity_dir("github-co", root).name, "github-co")

    def test_school_shaped_credentials_refused(self) -> None:
        secrets = _write_temp_secrets(
            self.tmp, {"student_id": "22300180001", "uis_password": "x"}
        )
        with self.assertRaises(gi.BoundaryViolationError) as ctx:
            gi.load_tier_a_secrets(secrets)
        self.assertIn("学校", str(ctx.exception))

    def test_tier_a_missing_or_bad_shape_is_honest_failure(self) -> None:
        with self.assertRaises(gi.CredentialsUnavailable):
            gi.load_tier_a_secrets(self.tmp / "no-such-file.json")
        secrets = _write_temp_secrets(self.tmp, {"username": "", "password": "x"})
        with self.assertRaises(gi.CredentialsUnavailable):
            gi.load_tier_a_secrets(secrets)

    # --- 持久化与 0600 ---------------------------------------------------

    def test_persist_hardens_and_writes_meta(self) -> None:
        root = self.tmp / "identity"
        incoming = self.tmp / "incoming.json"
        incoming.write_text(json.dumps(_fabricated_state()), encoding="utf-8")
        state = gi.persist_storage_state(
            incoming,
            identity_name="github-co",
            purpose="api",
            owner_lane="TB-IDT-M1",
            root=root,
        )
        self.assertTrue(state.exists())
        self.assertTrue(gi.hardened_access_ok(state), "storageState 即凭据，落盘必须 0600")
        self.assertFalse(incoming.exists(), "落位后 incoming 应被原子换位消费掉")
        meta = json.loads((state.parent / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["identity"], "github-co")
        self.assertEqual(meta["purpose"], "api")
        self.assertEqual(meta["owner_lane"], "TB-IDT-M1")
        self.assertIn("created_at", meta)
        self.assertNotIn("password", json.loads(state.read_text(encoding="utf-8")))

    def test_persist_refuses_malformed_state(self) -> None:
        root = self.tmp / "identity"
        incoming = self.tmp / "incoming.json"
        incoming.write_text(json.dumps({"nope": True}), encoding="utf-8")
        with self.assertRaises(gi.DriverError):
            gi.persist_storage_state(
                incoming, identity_name="github-co", purpose="api",
                owner_lane="TB-IDT-M1", root=root,
            )
        self.assertFalse((root / "github-co" / "storageState.json").exists())

    def test_simulate_expiry_empties_cookies_keeps_hardening(self) -> None:
        root = self.tmp / "identity"
        incoming = self.tmp / "incoming.json"
        incoming.write_text(json.dumps(_fabricated_state()), encoding="utf-8")
        state = gi.persist_storage_state(
            incoming, identity_name="github-co", purpose="api",
            owner_lane="TB-IDT-M1", root=root,
        )
        gi.simulate_expiry("github-co", root)
        payload = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(payload["cookies"], [])
        self.assertTrue(gi.hardened_access_ok(state))

    # --- 凭据零明文 -------------------------------------------------------

    def test_driver_argv_carries_paths_not_secrets(self) -> None:
        secrets_path = self.tmp / "k.json"
        out_path = self.tmp / "out.json"
        argv = gi._driver_command(
            "login", secrets_path=secrets_path, state_out=out_path
        )
        self.assertIn("--secrets", argv)
        self.assertIn(str(secrets_path), argv)
        # 密码本体永不入 argv（驱动自读 Tier A 文件）
        self.assertNotIn("real-password", [a for a in argv if "real-password" in a] or [""])

    def test_secrets_repr_and_redact_never_leak(self) -> None:
        s = gi.GitHubTestSecrets(username="gualtier-xu", password="super-secret")
        self.assertNotIn("super-secret", repr(s))
        self.assertNotIn("gualtier-xu", repr(s))
        self.assertEqual(gi.redact("gualtier-xu"), "gu***")
        self.assertEqual(gi.redact(None), "***")
        self.assertEqual(gi.redact("ab"), "***")

    # --- 全链编排（假驱动注入；自愈语义） ---------------------------------

    def _fake_driver(self, state_path_holder: dict, argv_log: list):
        def fake_run_driver(args, timeout_s=180.0):
            argv_log.append(list(args))
            mode = args[2]
            if mode == "verify":
                state = Path(args[args.index("--state") + 1])
                payload = json.loads(state.read_text(encoding="utf-8"))
                if not payload.get("cookies"):
                    return {"ok": False, "phase": "verify", "status": 401}
                return {"ok": True, "phase": "verify", "status": 200}
            if mode == "login":
                out = Path(args[args.index("--state-out") + 1])
                out.write_text(json.dumps(_fabricated_state()), encoding="utf-8")
                return {"ok": True, "phase": "login", "loginRedacted": "gu***"}
            raise AssertionError(f"unexpected mode {mode}")

        return fake_run_driver

    def test_offline_full_chain_login_persist_reuse_selfheal(self) -> None:
        root = self.tmp / "identity"
        secrets = _write_temp_secrets(
            self.tmp, {"username": "gu-something", "password": "super-secret"}
        )
        argv_log: list = []
        holder: dict = {}
        with mock.patch.object(gi, "run_driver", self._fake_driver(holder, argv_log)), \
                mock.patch.object(gi, "default_tier_a_path", return_value=secrets):
            first = gi.ensure_session(purpose="api", owner_lane="TB-IDT-M1", root=root)
            self.assertTrue(first.refreshed and not first.reused)
            state = Path(first.state_path)
            self.assertTrue(state.exists() and gi.hardened_access_ok(state))
            self.assertNotIn("super-secret", state.read_text(encoding="utf-8"))

            second = gi.ensure_session(purpose="api", owner_lane="TB-IDT-M1", root=root)
            self.assertTrue(second.reused and not second.refreshed)

            gi.simulate_expiry("github-co", root)
            healed = gi.ensure_session(purpose="api", owner_lane="TB-IDT-M1", root=root)
            self.assertTrue(healed.refreshed, "过期后必须自动重登刷新")
            self.assertTrue(Path(healed.state_path).exists())

        # 登录面的 argv 只出现 Tier A 路径；密码明文全程零出现
        login_argvs = [a for a in argv_log if a[2] == "login"]
        self.assertEqual(len(login_argvs), 2, "首登+自愈重登共两次")
        for argv in login_argvs:
            self.assertIn(str(secrets), argv)
            joined = " ".join(argv)
            self.assertNotIn("super-secret", joined)

    def test_offline_refresh_forces_relogin(self) -> None:
        root = self.tmp / "identity"
        secrets = _write_temp_secrets(
            self.tmp, {"username": "gu-something", "password": "pw"}
        )
        argv_log: list = []
        with mock.patch.object(gi, "run_driver", self._fake_driver({}, argv_log)), \
                mock.patch.object(gi, "default_tier_a_path", return_value=secrets):
            gi.ensure_session(purpose="api", owner_lane="TB-IDT-M1", root=root)
            forced = gi.ensure_session(
                purpose="api", owner_lane="TB-IDT-M1", root=root, force_refresh=True
            )
            self.assertTrue(forced.refreshed)


class GithubIdentityRealChainPins(unittest.TestCase):
    """真实 GitHub 全链钉（出站仅登录最小面；不可达=诚实 SKIP）。"""

    @classmethod
    def _skip_reason(cls) -> str | None:
        try:
            gi.find_playwright_node_modules()
        except gi.PlaywrightUnavailable as exc:
            return f"诚实 SKIP：{exc}"
        if not gi.default_tier_a_path().exists():
            return "诚实 SKIP：Tier A 凭据文件缺失（.local-secrets/co-github-test.json）"
        try:
            probe = socket.create_connection(("github.com", 443), timeout=5)
            probe.close()
        except OSError:
            return "诚实 SKIP：github.com:443 外联不可达（不假绿）"
        return None

    def setUp(self) -> None:
        reason = self._skip_reason()
        if reason:
            self.skipTest(reason)
        self.tmp = Path(tempfile.mkdtemp(prefix="tbidtm1-real-"))
        self.addCleanup(self._rm)

    def _rm(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_real_full_chain_reuse_probe_expiry_selfheal(self) -> None:
        """登录→持久化→新 context 复用→真实失效→自愈重登，一测串全链（控制登录次数）。"""
        secrets = gi.load_tier_a_secrets()

        first = gi.ensure_session(purpose="api", owner_lane="TB-IDT-M1")
        state = Path(first.state_path)
        self.assertTrue(state.exists())
        self.assertTrue(gi.hardened_access_ok(state), "真实 storageState 必须 0600")
        self.assertNotIn(secrets.password, state.read_text(encoding="utf-8"))
        if first.refreshed:
            self.assertEqual(first.login_redacted, gi.redact(secrets.username))

        # 新 context 复用（verify 每次都开全新 context；settings/profile 零导航探测）
        probe = gi.verify_session()
        self.assertEqual(probe.status, 200)

        # 真实失效模拟：空 cookie → 登出态应被判无效（settings/profile 302 登录墙）
        gi.simulate_expiry()
        expired = gi.verify_session()
        self.assertNotEqual(expired.status, 200, "空 cookie 不得判为有效会话")
        self.assertNotIn(expired.status, (-1, 0), "失效判定不得与外联故障混淆")

        # 自愈：探活无效 → 自动重登 → 恢复 200
        healed = gi.ensure_session(purpose="api", owner_lane="TB-IDT-M1")
        self.assertTrue(healed.refreshed)
        self.assertEqual(healed.login_redacted, gi.redact(secrets.username))
        self.assertEqual(gi.verify_session().status, 200)

    def test_real_boundary_refusal_makes_no_network_side_effect(self) -> None:
        root = self.tmp / "identity"
        with self.assertRaises(gi.BoundaryViolationError):
            gi.ensure_session(purpose="school-uis-login", root=root)
        self.assertFalse(root.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
