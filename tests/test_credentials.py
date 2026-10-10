from __future__ import annotations

import json
import os
import tempfile
import threading
import traceback
import unittest
from multiprocessing import get_context
from pathlib import Path
from unittest.mock import patch

import credentials
from credentials import CredentialStore
from src.application import CourseLensApplication
from src.remote.github_app import GitHubAppClient


def _cross_process_mutation(path: str, start, result, mode: str) -> None:
    try:
        if not start.wait(10):
            raise RuntimeError("barrier timeout")
        store = CredentialStore(path)
        if mode == "oauth":
            store.update_secrets({
                "github_app_access_token": "process-access",
                "github_app_access_expires_at": "9000",
                "github_app_refresh_token": "process-refresh",
                "github_app_refresh_expires_at": "18000",
                "github_app_installation_id": "42",
            })
        else:
            store.save("process-user", "test-password")
            store.mark_account_rotation_required("process-user")
            store.delete_account("process-user")
            store.save_deepseek_key("test-key")
            store.mark_deepseek_key_rotation_required()
            store.delete_deepseek_key()
        result.put("")
    except BaseException as exc:
        result.put(traceback.format_exc())


class _DeepSeekKeyHost:
    """set_deepseek_key/_deepseek_key 会话/持久语义的最小宿主，复用真实 Application 方法。"""

    def __init__(self, store: CredentialStore):
        self.credentials = store
        self._deepseek_api_key = ""
        self._lock = threading.RLock()

    def _deepseek_key(self) -> str:
        return CourseLensApplication._deepseek_key(self)


@unittest.skipUnless(os.name == "nt", "DPAPI credential tests require Windows")
class CredentialRotationTests(unittest.TestCase):
    def _scratch_store(self):
        scratch = Path(__file__).resolve().parents[1] / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=scratch)
        return directory, Path(directory.name) / "credentials.json"

    def test_concurrent_envelope_updates_preserve_disjoint_secrets_and_complete_generation(self):
        directory, path = self._scratch_store()
        try:
            first = CredentialStore(path)
            second = CredentialStore(path)
            start = threading.Barrier(3)
            errors = []

            def save_disjoint(store, name, value):
                try:
                    start.wait()
                    store.update_secrets({name: value})
                except BaseException as exc:
                    errors.append(exc)

            left = threading.Thread(target=save_disjoint, args=(first, "left", "one"))
            right = threading.Thread(target=save_disjoint, args=(second, "right", "two"))
            left.start(); right.start(); start.wait(); left.join(5); right.join(5)
            self.assertFalse(left.is_alive() or right.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(CredentialStore(path).load_secret("left"), "one")
            self.assertEqual(CredentialStore(path).load_secret("right"), "two")

            first_client = GitHubAppClient(first, client_id="id", app_slug="app")
            second_client = GitHubAppClient(second, client_id="id", app_slug="app")
            generation = threading.Barrier(3)
            def save_generation(client, token, refresh, installation):
                try:
                    generation.wait()
                    client._save_tokens({"access_token": token, "expires_in": 900, "refresh_token": refresh, "refresh_token_expires_in": 1800}, installation_id=installation)
                except BaseException as exc:
                    errors.append(exc)
            one = threading.Thread(target=save_generation, args=(first_client, "access-one", "refresh-one", 41))
            two = threading.Thread(target=save_generation, args=(second_client, "access-two", "refresh-two", 42))
            one.start(); two.start(); generation.wait(); one.join(5); two.join(5)
            self.assertFalse(one.is_alive() or two.is_alive())
            self.assertEqual(errors, [])
            final = CredentialStore(path)
            token = final.load_secret("github_app_access_token")
            suffix = "one" if token == "access-one" else "two"
            self.assertIn(suffix, {"one", "two"})
            self.assertEqual(final.load_secret("github_app_refresh_token"), f"refresh-{suffix}")
            self.assertEqual(final.load_secret("github_app_installation_id"), "41" if suffix == "one" else "42")
            self.assertGreater(float(final.load_secret("github_app_access_expires_at")), 0)
            self.assertGreater(float(final.load_secret("github_app_refresh_expires_at")), 0)
        finally:
            directory.cleanup()

    def test_envelope_lock_timeout_preserves_old_bytes_and_leaves_no_temporary(self):
        directory, path = self._scratch_store()
        try:
            first = CredentialStore(path); second = CredentialStore(path)
            first.save_secret("stable", "old")
            before = path.read_bytes()
            acquired = threading.Event(); release = threading.Event()
            def hold_lock():
                with first._envelope_lock():
                    acquired.set(); release.wait(5)
            holder = threading.Thread(target=hold_lock)
            holder.start(); self.assertTrue(acquired.wait(5))
            with patch.object(credentials, "_LOCK_TIMEOUT_SECONDS", 0.05):
                with self.assertRaisesRegex(RuntimeError, "credential_envelope_lock_unavailable"):
                    second.save_secret("new", "value")
            release.set(); holder.join(5)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])
            self.assertEqual(CredentialStore(path).load_secret("stable"), "old")
        finally:
            directory.cleanup()

    def test_cross_process_oauth_and_account_deepseek_mutations_are_serializable(self):
        directory, path = self._scratch_store()
        try:
            context = get_context("spawn")
            for _ in range(10):
                start = context.Event(); results = context.Queue()
                oauth = context.Process(target=_cross_process_mutation, args=(str(path), start, results, "oauth"))
                account = context.Process(target=_cross_process_mutation, args=(str(path), start, results, "account"))
                oauth.start(); account.start(); start.set()
                oauth.join(15); account.join(15)
                self.assertEqual((oauth.exitcode, account.exitcode), (0, 0))
                child_results = [results.get(timeout=5), results.get(timeout=5)]
                self.assertEqual(child_results, ["", ""], child_results)
                final = CredentialStore(path)
                self.assertEqual(final.load_secret("github_app_access_token"), "process-access")
                self.assertEqual(final.load_secret("github_app_refresh_token"), "process-refresh")
                self.assertEqual(final.load_secret("github_app_installation_id"), "42")
                self.assertFalse(final.list_accounts())
                self.assertFalse(final.has_deepseek_key())
                self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])
                self.assertEqual(path.with_name(f".{path.name}.lock").read_bytes(), b"0")
        finally:
            directory.cleanup()

    def test_related_secrets_are_replaced_in_one_ciphertext_only_file(self):
        scratch = Path(__file__).resolve().parents[1] / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            path = Path(directory) / "credentials.json"
            store = CredentialStore(path)
            store.save_secret("old", "old-value")
            store.update_secrets(
                {"worker": "new-worker", "box": "new-public-key"},
                deletes=("old",),
            )

            self.assertEqual(store.load_secret("worker"), "new-worker")
            self.assertEqual(store.load_secret("box"), "new-public-key")
            self.assertFalse(store.has_secret("old"))
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("new-worker", raw)
            self.assertNotIn("new-public-key", raw)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_marked_password_cannot_be_reused_until_resaved(self):
        scratch = Path(__file__).resolve().parents[1] / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            store = CredentialStore(Path(directory) / "credentials.json")
            store.save("student", "old-password")
            self.assertFalse(store.list_accounts()[0]["requires_rotation"])
            self.assertEqual(store.load("student"), ("student", "old-password"))

            self.assertTrue(store.mark_account_rotation_required("student"))
            self.assertTrue(store.list_accounts()[0]["requires_rotation"])
            with self.assertRaisesRegex(RuntimeError, "需要更新"):
                store.load("student")

            store.save("student", "new-password")
            self.assertFalse(store.list_accounts()[0]["requires_rotation"])
            self.assertEqual(store.load("student"), ("student", "new-password"))

    def test_marked_deepseek_key_is_unavailable_until_resaved(self):
        scratch = Path(__file__).resolve().parents[1] / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            store = CredentialStore(Path(directory) / "credentials.json")
            store.save_deepseek_key("sk-old")
            self.assertTrue(store.has_deepseek_key())
            self.assertFalse(store.deepseek_key_requires_rotation())

            self.assertTrue(store.mark_deepseek_key_rotation_required())
            self.assertFalse(store.has_deepseek_key())
            self.assertTrue(store.deepseek_key_requires_rotation())
            with self.assertRaisesRegex(RuntimeError, "需要更新"):
                store.load_deepseek_key()

            store.save_deepseek_key("sk-new")
            self.assertTrue(store.has_deepseek_key())
            self.assertFalse(store.deepseek_key_requires_rotation())
            self.assertEqual(store.load_deepseek_key(), "sk-new")


@unittest.skipUnless(os.name == "nt", "DPAPI credential tests require Windows")
class DeepSeekSessionKeySemanticsTests(unittest.TestCase):
    """回归：remember=False 只设置会话 key，绝不静默删除已持久化的本机 key；
    持久删除只能经 delete_deepseek_key()。"""

    def _scratch_store(self):
        scratch = Path(__file__).resolve().parents[1] / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=scratch)
        return directory, Path(directory.name) / "credentials.json"

    def test_set_without_remember_keeps_persisted_key_and_applies_session_key(self):
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            store.save_deepseek_key("sk-synthetic-persisted")
            host = _DeepSeekKeyHost(store)

            with self.assertRaisesRegex(ValueError, "required"):
                CourseLensApplication.set_deepseek_key(host, "   ", remember=True)
            self.assertTrue(store.has_deepseek_key(), "空 key 提交不得触碰已持久化的本机 key")

            CourseLensApplication.set_deepseek_key(host, " sk-synthetic-session ", remember=False)

            self.assertTrue(store.has_deepseek_key(), "未勾选保存不得删除已持久化的本机 key")
            self.assertEqual(store.load_deepseek_key(), "sk-synthetic-persisted")
            self.assertEqual(CourseLensApplication._deepseek_key(host), "sk-synthetic-session", "会话 key 立即生效")
            self.assertTrue(CourseLensApplication.has_deepseek_key(host))

            self.assertTrue(CourseLensApplication.delete_deepseek_key(host))
            self.assertFalse(store.has_deepseek_key(), "持久删除仅经 delete_deepseek_key")
            self.assertEqual(CourseLensApplication._deepseek_key(host), "", "删除应同时清空会话 key")
        finally:
            directory.cleanup()

    def test_set_with_remember_persists_and_survives_session_only_overwrite(self):
        directory, path = self._scratch_store()
        try:
            host = _DeepSeekKeyHost(CredentialStore(path))

            CourseLensApplication.set_deepseek_key(host, "sk-synthetic-persisted-new", remember=True)
            self.assertTrue(host.credentials.has_deepseek_key())
            self.assertEqual(host.credentials.load_deepseek_key(), "sk-synthetic-persisted-new")

            CourseLensApplication.set_deepseek_key(host, "sk-synthetic-session", remember=False)
            self.assertEqual(
                host.credentials.load_deepseek_key(), "sk-synthetic-persisted-new",
                "会话覆盖后本机保存保持不变",
            )
            self.assertEqual(CourseLensApplication._deepseek_key(host), "sk-synthetic-session")

            self.assertTrue(CourseLensApplication.delete_deepseek_key(host))
            self.assertFalse(host.credentials.has_deepseek_key())
        finally:
            directory.cleanup()


def _checkpoint_cookies():
    """合成会话 cookie（显然非真实值）；形状与 WebVPN 捕获层输出一致。"""
    return [
        {"name": "webvpn_session", "value": "synthetic-session-cookie-a", "domain": "webvpn.fudan.edu.cn", "path": "/"},
        {"name": "tenant_synthetic_icourse", "value": "synthetic-session-cookie-b", "domain": "webvpn.fudan.edu.cn", "path": "/"},
    ]


@unittest.skipUnless(os.name == "nt", "DPAPI credential tests require Windows")
class SessionCheckpointStoreTests(unittest.TestCase):
    """V5 会话检查点：DPAPI 往返、闭集形状、fail-closed 丢弃、账号绑定与清理。"""

    def _scratch_store(self):
        scratch = Path(__file__).resolve().parents[1] / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=scratch)
        return directory, Path(directory.name) / "credentials.json"

    def test_round_trip_encrypts_and_never_restores_plaintext(self):
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            store.save("20301010001", "synthetic-not-a-real-password")
            cookies = _checkpoint_cookies()
            store.save_session_checkpoint("20301010001", cookies)
            loaded = store.load_session_checkpoint("20301010001")
            self.assertEqual(loaded, cookies)

            raw = path.read_text(encoding="utf-8")
            # 密文之外绝不出现 cookie 名/值、账号绑定原文
            for forbidden in (
                "synthetic-session-cookie-a", "synthetic-session-cookie-b",
                "webvpn_session", "tenant_synthetic_icourse",
                "courselens.session-checkpoint.v1:",
            ):
                self.assertNotIn(forbidden, raw)
            # 检查点子树里绝不出现账号 id：只有不可逆绑定哈希与 DPAPI 密文
            checkpoint_text = json.dumps(
                json.loads(raw)["session_checkpoints"], ensure_ascii=False
            )
            self.assertNotIn("20301010001", checkpoint_text)
            entry = json.loads(raw)["session_checkpoints"]
            self.assertEqual(len(entry), 1)
            (only_entry,) = entry.values()
            self.assertEqual(set(only_entry), {"version", "created_at", "expires_at", "payload"})
        finally:
            directory.cleanup()

    def test_expired_checkpoint_fails_closed_and_is_discarded(self):
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            store.save("20301010001", "synthetic-not-a-real-password")
            with patch.object(credentials, "SESSION_CHECKPOINT_TTL_SECONDS", 0.0):
                store.save_session_checkpoint("20301010001", _checkpoint_cookies())
            self.assertIsNone(store.load_session_checkpoint("20301010001"))
            self.assertEqual(store._read()["session_checkpoints"], {}, "过期检查点必须被丢弃")
            self.assertIsNone(store.load_session_checkpoint("20301010001"))
        finally:
            directory.cleanup()

    def test_unknown_envelope_schema_fails_closed(self):
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            store.save("20301010001", "synthetic-not-a-real-password")
            store.save_session_checkpoint("20301010001", _checkpoint_cookies())
            # 把 envelope 版本改成未知值（模拟旧版本/损坏写入）
            data = json.loads(path.read_text(encoding="utf-8"))
            for entry in data["session_checkpoints"].values():
                entry["version"] = 99
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self.assertIsNone(store.load_session_checkpoint("20301010001"))
            self.assertEqual(store._read()["session_checkpoints"], {})
        finally:
            directory.cleanup()

    def test_corrupted_ciphertext_fails_closed(self):
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            store.save("20301010001", "synthetic-not-a-real-password")
            store.save_session_checkpoint("20301010001", _checkpoint_cookies())
            data = json.loads(path.read_text(encoding="utf-8"))
            for entry in data["session_checkpoints"].values():
                entry["payload"] = "AAAA" + str(entry["payload"])[4:]
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self.assertIsNone(store.load_session_checkpoint("20301010001"))
            self.assertEqual(store._read()["session_checkpoints"], {})
        finally:
            directory.cleanup()

    def test_binding_isolates_accounts(self):
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            for account in ("20301010001", "20301010002"):
                store.save(account, "synthetic-not-a-real-password")
            cookies_a = _checkpoint_cookies()
            cookies_b = [{"name": "webvpn_session", "value": "synthetic-account-b-cookie", "domain": "webvpn.fudan.edu.cn", "path": "/"}]
            store.save_session_checkpoint("20301010001", cookies_a)
            store.save_session_checkpoint("20301010002", cookies_b)
            self.assertEqual(store.load_session_checkpoint("20301010001"), cookies_a)
            self.assertEqual(store.load_session_checkpoint("20301010002"), cookies_b)
            self.assertIsNone(store.load_session_checkpoint("ghost-account"))
            # 查询不存在账号绝不触碰其它账号的检查点
            self.assertEqual(store.load_session_checkpoint("20301010001"), cookies_a)
        finally:
            directory.cleanup()

    def test_rotation_flag_and_account_deletion_discard_checkpoint(self):
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            store.save("20301010001", "synthetic-not-a-real-password")
            store.save_session_checkpoint("20301010001", _checkpoint_cookies())
            self.assertIsNotNone(store.load_session_checkpoint("20301010001"))

            self.assertTrue(store.mark_account_rotation_required("20301010001"))
            self.assertIsNone(store.load_session_checkpoint("20301010001"), "轮换标记必须丢弃检查点")

            # 轮换未解除前重新保存的检查点也必须不可恢复（load 再次丢弃）
            store.save_session_checkpoint("20301010001", _checkpoint_cookies())
            self.assertIsNone(store.load_session_checkpoint("20301010001"))
            self.assertEqual(store._read()["session_checkpoints"], {})

            # 解除轮换后重新登录保存 → 删除账号连带丢弃
            store.save("20301010001", "synthetic-not-a-real-password-2")
            store.save_session_checkpoint("20301010001", _checkpoint_cookies())
            self.assertTrue(store.delete_account("20301010001"))
            self.assertIsNone(store.load_session_checkpoint("20301010001"))
        finally:
            directory.cleanup()

    def test_malformed_cookie_shapes_are_rejected_before_write(self):
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            bad_shapes = [
                [],
                "not-a-list",
                [{"name": "webvpn_session", "value": "v"}],
                [{"name": "bad name", "value": "synthetic", "domain": "webvpn.fudan.edu.cn", "path": "/"}],
                [{"name": "webvpn_session", "value": "bad\r\nvalue", "domain": "webvpn.fudan.edu.cn", "path": "/"}],
                [{"name": "webvpn_session", "value": "synthetic", "domain": "webvpn.fudan.edu.cn", "path": "/", "extra": "x"}],
                [{"name": "webvpn_session", "value": "synthetic", "domain": "webvpn.fudan.edu.cn", "path": "noslash"}],
            ]
            for shape in bad_shapes:
                with self.subTest(shape=shape):
                    with self.assertRaises(ValueError):
                        store.save_session_checkpoint("20301010001", shape)
            self.assertEqual(store._read()["session_checkpoints"], {}, "被拒绝的形状绝不落盘")
            with self.assertRaises(ValueError):
                store.save_session_checkpoint("", _checkpoint_cookies())
        finally:
            directory.cleanup()

    def test_explicit_clears_are_exact_and_counted(self):
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            store.save("20301010001", "synthetic-not-a-real-password")
            self.assertFalse(store.clear_session_checkpoint("20301010001"))
            store.save_session_checkpoint("20301010001", _checkpoint_cookies())
            self.assertTrue(store.clear_session_checkpoint("20301010001"))
            self.assertIsNone(store.load_session_checkpoint("20301010001"))
            store.save_session_checkpoint("20301010001", _checkpoint_cookies())
            store.save_session_checkpoint("20301010002", _checkpoint_cookies())
            self.assertEqual(store.clear_all_session_checkpoints(), 2)
            self.assertEqual(store.clear_all_session_checkpoints(), 0)
        finally:
            directory.cleanup()


def _downgrade_envelope_tokens_to_v1(path: Path, sections: tuple[str, ...]) -> None:
    """把信封指定子树里的全部 v2 密文改写为真实 v1 格式（无前缀、无附加熵）。

    仅用于合成迁移测试：明文逐字节不变、只把保护格式降级，等价一台
    升级前旧装机留下的信封。绝不触碰真实凭据存储。
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    rewritten = 0
    for section in sections:
        for entry in (data.get(section) or {}).values():
            if not isinstance(entry, dict):
                continue
            for field in ("password", "value", "payload"):
                token = entry.get(field)
                if isinstance(token, str) and not credentials._is_legacy_v1_token(token):
                    raw = credentials._unprotect(token)
                    entry[field] = credentials._protect_legacy_v1(raw)
                    rewritten += 1
    assert rewritten, "expected at least one v2 token to downgrade"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@unittest.skipUnless(os.name == "nt", "DPAPI credential tests require Windows")
class DpapiCiphertextVersionMigrationTests(unittest.TestCase):
    """R7 附加熵迁移：cp:v2: 前缀字面钉、v1 读兼容、惰性 v2 重存、
    错熵/裸解 fail-closed、三触点（记住账号 / V5 检查点 / GitHub UAT secrets）
    合成全链与迁移 CAS 安全。全程合成密文，绝不读写真实凭据存储。"""

    def _scratch_store(self):
        scratch = Path(__file__).resolve().parents[1] / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        directory = tempfile.TemporaryDirectory(dir=scratch)
        return directory, Path(directory.name) / "credentials.json"

    def test_v2_output_carries_literal_version_prefix_and_roundtrips(self):
        token = credentials._protect(b"synthetic-roundtrip-payload")
        self.assertTrue(token.startswith("cp:v2:"), "v2 版本前缀字面钉")
        self.assertEqual(credentials._unprotect(token), b"synthetic-roundtrip-payload")
        self.assertFalse(credentials._is_legacy_v1_token(token))

    def test_v2_blob_rejects_bare_and_wrong_entropy_decrypt(self):
        # 威胁模型钉：拿到 v2 密文的进程若无正确附加熵（裸 DPAPI 脚本视角）必须失败
        token = credentials._protect(b"synthetic-roundtrip-payload")
        blob = token[len("cp:v2:"):]
        with self.assertRaises(OSError):
            credentials._dpapi_unprotect(blob, None)
        with self.assertRaises(OSError):
            credentials._dpapi_unprotect(blob, b"wrong-synthetic-entropy")
        with patch.object(credentials, "_CIPHERTEXT_ENTROPY_V2", b"wrong-synthetic-entropy"):
            with self.assertRaises(OSError):
                credentials._unprotect(token)
        # store 级：错熵进程视角的 load 必须 fail-closed（异常上抛，绝不返回明文）
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            store.save("20301010001", "synthetic-not-a-real-password")
            with patch.object(credentials, "_CIPHERTEXT_ENTROPY_V2", b"wrong-synthetic-entropy"):
                with self.assertRaises(OSError):
                    store.load("20301010001")
        finally:
            directory.cleanup()

    def test_remembered_account_v1_reads_compatible_and_lazily_resaved_as_v2(self):
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            store.save("20301010001", "synthetic-not-a-real-password")
            _downgrade_envelope_tokens_to_v1(path, ("accounts",))
            v1_token = json.loads(path.read_text(encoding="utf-8"))["accounts"]["20301010001"]["password"]
            self.assertTrue(credentials._is_legacy_v1_token(v1_token))
            self.assertNotIn("cp:v2:", v1_token)

            self.assertEqual(
                store.load("20301010001"),
                ("20301010001", "synthetic-not-a-real-password"),
            )
            migrated = json.loads(path.read_text(encoding="utf-8"))["accounts"]["20301010001"]["password"]
            self.assertTrue(migrated.startswith("cp:v2:"), "v1 读取后必须惰性重存为 v2")
            self.assertNotEqual(migrated, v1_token)
            self.assertEqual(
                store.load("20301010001"),
                ("20301010001", "synthetic-not-a-real-password"),
            )
        finally:
            directory.cleanup()

    def test_v5_checkpoint_v1_reads_compatible_and_lazily_resaved_as_v2(self):
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            store.save("20301010001", "synthetic-not-a-real-password")
            cookies = _checkpoint_cookies()
            store.save_session_checkpoint("20301010001", cookies)
            _downgrade_envelope_tokens_to_v1(path, ("session_checkpoints",))
            binding = credentials._checkpoint_binding("20301010001")
            envelope = json.loads(path.read_text(encoding="utf-8"))["session_checkpoints"][binding]
            self.assertTrue(credentials._is_legacy_v1_token(envelope["payload"]))

            self.assertEqual(store.load_session_checkpoint("20301010001"), cookies)
            migrated = json.loads(path.read_text(encoding="utf-8"))["session_checkpoints"][binding]["payload"]
            self.assertTrue(migrated.startswith("cp:v2:"), "检查点读取后必须惰性重存为 v2")
            self.assertEqual(store.load_session_checkpoint("20301010001"), cookies)
        finally:
            directory.cleanup()

    def test_github_uat_secrets_v1_reads_compatible_and_lazily_resaved_as_v2(self):
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            client = GitHubAppClient(store, client_id="id", app_slug="fudan-courselens")
            client._save_tokens(
                {
                    "access_token": "synthetic-access",
                    "expires_in": 900,
                    "refresh_token": "synthetic-refresh",
                    "refresh_token_expires_in": 1800,
                },
                installation_id=42,
            )
            _downgrade_envelope_tokens_to_v1(path, ("secrets",))
            secrets = json.loads(path.read_text(encoding="utf-8"))["secrets"]
            for name in ("github_app_access_token", "github_app_refresh_token", "github_app_installation_id"):
                self.assertTrue(credentials._is_legacy_v1_token(secrets[name]["value"]), name)

            self.assertEqual(store.load_secret("github_app_access_token"), "synthetic-access")
            self.assertEqual(store.load_secret("github_app_refresh_token"), "synthetic-refresh")
            self.assertEqual(store.load_secret("github_app_installation_id"), "42")
            migrated = json.loads(path.read_text(encoding="utf-8"))["secrets"]
            for name in ("github_app_access_token", "github_app_refresh_token", "github_app_installation_id"):
                self.assertTrue(migrated[name]["value"].startswith("cp:v2:"), name)
        finally:
            directory.cleanup()

    def test_unknown_future_prefix_fails_closed(self):
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            store.save("20301010001", "synthetic-not-a-real-password")
            data = json.loads(path.read_text(encoding="utf-8"))
            data["accounts"]["20301010001"]["password"] = "cp:v9:QUJDREVGRw=="
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(OSError):
                store.load("20301010001")
        finally:
            directory.cleanup()

    def test_migration_is_compare_and_swap_safe(self):
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            store.save("20301010001", "synthetic-not-a-real-password")
            _downgrade_envelope_tokens_to_v1(path, ("accounts",))
            v1_token = json.loads(path.read_text(encoding="utf-8"))["accounts"]["20301010001"]["password"]
            # 迁移写入前另一写者已保存新密码（v2）——过期 v1 串绝不回写覆盖
            store.save("20301010001", "synthetic-concurrent-new-password")
            concurrent_token = json.loads(path.read_text(encoding="utf-8"))["accounts"]["20301010001"]["password"]
            store._migrate_legacy_v1_token(
                lambda data: data.get("accounts", {}).get("20301010001"),
                "password", v1_token, b"synthetic-stale-payload",
            )
            after = json.loads(path.read_text(encoding="utf-8"))["accounts"]["20301010001"]["password"]
            self.assertEqual(after, concurrent_token)
            self.assertEqual(store.load("20301010001")[1], "synthetic-concurrent-new-password")
        finally:
            directory.cleanup()

    def test_checkpoint_wrong_entropy_still_fails_closed_and_discards(self):
        directory, path = self._scratch_store()
        try:
            store = CredentialStore(path)
            store.save("20301010001", "synthetic-not-a-real-password")
            store.save_session_checkpoint("20301010001", _checkpoint_cookies())
            with patch.object(credentials, "_CIPHERTEXT_ENTROPY_V2", b"wrong-synthetic-entropy"):
                self.assertIsNone(store.load_session_checkpoint("20301010001"))
            self.assertEqual(store._read()["session_checkpoints"], {}, "错熵检查点必须被丢弃")
        finally:
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
