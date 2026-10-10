"""real 档测试身份凭据契约钉（测试台架批 4·件②M3，TESTBENCH-DESIGN-1 §2.3 M3）。

验收判据（设计原文）：真 UIS 登录链在 real 档内全自动且零凭据落盘；默认档
零差异。本文件全部离线：联动契约/装载器 schema/脱敏面/env 字面量封闭/
serve 接线源码钉。真实网络腿由 tests/testbench/real_chain_selftest.py 单独
执行（真凭据只走内存，永不进任何钉测夹具——钉内出现的全是显式假值）。

读取纪律（A3）+上下文预算（A2）适用；预计请求数 ≈6。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.runtime import test_mode as tm
from src.runtime import test_credentials as tc
from src.runtime.test_mode import TestModeContractError as _ContractError

CREDS_ENV = tm.TEST_CREDENTIALS_FILE_ENV
MODE_ENV = tm.TEST_MODE_ENV
ALLOW_ENV = tm.EGRESS_ALLOW_ENV


class _EnvCase(unittest.TestCase):
    _WATCHED = (CREDS_ENV, MODE_ENV, ALLOW_ENV)

    def setUp(self) -> None:
        self._snapshot = {k: os.environ.get(k) for k in self._WATCHED}
        for key in self._WATCHED:
            os.environ.pop(key, None)

    def tearDown(self) -> None:
        for key, value in self._snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestLinkageContract(_EnvCase):
    def _write_creds(self, payload: dict) -> str:
        temp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, dir=str(ROOT / "runtime" / "cache"),
            encoding="utf-8",
        )
        json.dump(payload, temp)
        temp.close()
        self.addCleanup(os.unlink, temp.name)
        return temp.name

    def test_unset_env_is_noop(self) -> None:
        self.assertEqual(tm.test_credentials_file({}), "")
        self.assertEqual(tm.resolve_test_credentials_linkage({}), "")

    def test_file_requires_real_mode(self) -> None:
        creds = self._write_creds({"student_id": "fakesid", "password": "fakepwd"})
        with self.assertRaises(_ContractError) as caught:
            tm.resolve_test_credentials_linkage({CREDS_ENV: creds})
        self.assertEqual(caught.exception.code, "test_credentials_mode_conflict")
        with self.assertRaises(_ContractError) as caught:
            tm.resolve_test_credentials_linkage(
                {CREDS_ENV: creds, MODE_ENV: "synthetic"}
            )
        self.assertEqual(caught.exception.code, "test_credentials_mode_conflict")

    def test_file_requires_school_readonly_tier(self) -> None:
        """联动 fail-closed：真 UIS 登录必出站学校主机，缺 school:readonly 档启动即拒。"""
        creds = self._write_creds({"student_id": "fakesid", "password": "fakepwd"})
        with self.assertRaises(_ContractError) as caught:
            tm.resolve_test_credentials_linkage(
                {CREDS_ENV: creds, MODE_ENV: "real"}
            )
        self.assertEqual(caught.exception.code, "test_credentials_tier_required")
        # github:api 在册不算——必须含 school:readonly。
        with self.assertRaises(_ContractError) as caught:
            tm.resolve_test_credentials_linkage(
                {CREDS_ENV: creds, MODE_ENV: "real", ALLOW_ENV: "github:api"}
            )
        self.assertEqual(caught.exception.code, "test_credentials_tier_required")

    def test_missing_file_refused(self) -> None:
        with self.assertRaises(_ContractError) as caught:
            tm.resolve_test_credentials_linkage(
                {
                    CREDS_ENV: str(ROOT / "runtime" / "cache" / "no-such-creds-file.json"),
                    MODE_ENV: "real",
                    ALLOW_ENV: "school:readonly",
                }
            )
        self.assertEqual(caught.exception.code, "test_credentials_file_missing")

    def test_valid_linkage_resolves(self) -> None:
        creds = self._write_creds({"student_id": "fakesid", "password": "fakepwd"})
        resolved = tm.resolve_test_credentials_linkage(
            {CREDS_ENV: creds, MODE_ENV: "real", ALLOW_ENV: "school:readonly,github:api"}
        )
        self.assertEqual(Path(resolved), Path(creds).resolve())


class TestLoaderSchema(_EnvCase):
    def _write_creds(self, payload: dict) -> str:
        temp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, dir=str(ROOT / "runtime" / "cache"),
            encoding="utf-8",
        )
        json.dump(payload, temp)
        temp.close()
        self.addCleanup(os.unlink, temp.name)
        return temp.name

    def _env(self, creds: str) -> dict:
        return {CREDS_ENV: creds, MODE_ENV: "real", ALLOW_ENV: "school:readonly"}

    def test_load_valid_and_redaction(self) -> None:
        creds = self._write_creds({"student_id": "fakesid123", "password": "fakepwd456"})
        loaded = tc.load_test_credentials(self._env(creds))
        self.assertEqual(loaded.student_id, "fakesid123")
        self.assertEqual(loaded.password, "fakepwd456")
        # repr/str 恒脱敏：假值本体不得出现。
        for text in (repr(loaded), str(loaded)):
            self.assertNotIn("fakesid123", text)
            self.assertNotIn("fakepwd456", text)
            self.assertIn("***", text)

    def test_load_accepts_username_and_account_aliases(self) -> None:
        for key in ("username", "account"):
            creds = self._write_creds({key: "fakesid", "password": "fakepwd"})
            loaded = tc.load_test_credentials(self._env(creds))
            self.assertEqual(loaded.student_id, "fakesid")

    def test_load_accepts_credentials_wrapper(self) -> None:
        """Tier A 包裹形态（工作区 real-bench 同形）：credentials 子对象 + fudan_account。"""
        creds = self._write_creds({
            "schema": "real-bench",
            "credentials": {"fudan_account": "fakesid", "fudan_password": "fakepwd"},
        })
        loaded = tc.load_test_credentials(self._env(creds))
        self.assertEqual(loaded.student_id, "fakesid")
        self.assertEqual(loaded.password, "fakepwd")

    def test_invalid_shapes_refused(self) -> None:
        for payload in (
            {"password": "fakepwd"},                      # 缺身份
            {"student_id": "fakesid"},                    # 缺密码
            {"student_id": "", "password": "fakepwd"},    # 空身份
            {"student_id": "fakesid", "password": ""},    # 空密码
            {"student_id": 123, "password": "fakepwd"},   # 非字符串身份
            "not-a-dict",                                 # 顶层形态错
        ):
            creds = self._write_creds(payload)
            with self.assertRaises(_ContractError) as caught:
                tc.load_test_credentials(self._env(creds))
            self.assertEqual(caught.exception.code, "test_credentials_file_invalid")

    def test_unresolvable_env_never_loads(self) -> None:
        """联动不过 = 装载器直接拒绝（凭据内容零触达）。"""
        creds = self._write_creds({"student_id": "fakesid", "password": "fakepwd"})
        with self.assertRaises(_ContractError):
            tc.load_test_credentials({CREDS_ENV: creds, MODE_ENV: "synthetic"})

    def test_audit_line_redacted(self) -> None:
        import io
        from contextlib import redirect_stderr

        creds = self._write_creds({"student_id": "fakesid123", "password": "fakepwd456"})
        err = io.StringIO()
        with redirect_stderr(err):
            tc.load_test_credentials(self._env(creds))
        text = err.getvalue()
        self.assertNotIn("fakesid123", text)
        self.assertNotIn("fakepwd456", text)
        self.assertIn("[test-credentials]", text)

    def test_loader_module_writes_nothing(self) -> None:
        """零落盘结构证据：装载前后凭据文件目录内容不变，进程不产生新文件。"""
        creds = self._write_creds({"student_id": "fakesid", "password": "fakepwd"})
        before = sorted(p.name for p in Path(creds).parent.iterdir())
        tc.load_test_credentials(self._env(creds))
        after = sorted(p.name for p in Path(creds).parent.iterdir())
        self.assertEqual(before, after)


class TestWiringSurface(_EnvCase):
    def test_env_literals_confined_to_contract_module(self) -> None:
        offenders: list[str] = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            if path.name == "test_mode.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "COURSELENS_TEST_CREDENTIALS" in text:
                offenders.append(str(path))
        self.assertEqual(offenders, [], "凭据文件 env 字面量只允许出现在契约模块")

    def test_serve_startup_contract_wires_linkage(self) -> None:
        text = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        self.assertIn("resolve_test_credentials_linkage", text)


if __name__ == "__main__":
    unittest.main()
