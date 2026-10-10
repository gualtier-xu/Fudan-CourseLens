"""身份目录清理脚本钉测（件①M3；TB-W2）。全部离线，零外联、零真实凭据。

钉面：默认干跑不删任何东西；--apply 只删身份目录、Tier A 结构性不可达；
路径逃逸与受保护目录级（.local-secrets/instances/产品仓）一律拒绝；
--list 机读；缺失目录诚实零动作。
"""

from __future__ import annotations

import io
import json
import contextlib
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.testbench import identity_cleanup as ic


class IdentityCleanupPins(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tbw2-cleanup-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "identity"
        self.identity = self.root / "github-co"
        self.identity.mkdir(parents=True)
        (self.identity / "storageState.json").write_text(
            json.dumps({"cookies": [{"name": "user_session", "value": "fake"}]}),
            encoding="utf-8",
        )
        (self.identity / "meta.json").write_text("{}", encoding="utf-8")
        # 假 Tier A：与身份根同级（真实工作区里 .local-secrets 也是 .testbench 的同级邻居）。
        self.fake_tier_a = self.tmp / ".local-secrets"
        self.fake_tier_a.mkdir()
        (self.fake_tier_a / "co-github-test.json").write_text("{}", encoding="utf-8")

    def _run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ic.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_default_is_dry_run_deletes_nothing(self) -> None:
        code, out, _ = self._run_main("github-co", "--root", str(self.tmp / "identity"))
        self.assertEqual(code, 0)
        self.assertTrue(self.identity.exists(), "默认干跑不得删除任何文件")
        self.assertTrue((self.identity / "storageState.json").exists())
        self.assertIn("干跑", out)
        self.assertIn("--apply", out)
        self.assertIn(".local-secrets", out, "干跑计划必须显式说 Tier A 不在删除面")

    def test_apply_deletes_identity_dir_tier_a_untouched(self) -> None:
        code, out, _ = self._run_main(
            "github-co", "--apply", "--root", str(self.tmp / "identity")
        )
        self.assertEqual(code, 0)
        self.assertFalse(self.identity.exists(), "apply 后 Tier B 目录应整体消失（轮换）")
        self.assertTrue(
            (self.fake_tier_a / "co-github-test.json").exists(),
            "Tier A 凭据本体必须分毫不动",
        )
        self.assertIn("已轮换", out)

    def test_missing_identity_is_honest_zero_action(self) -> None:
        code, out, _ = self._run_main("ghost-id", "--root", str(self.tmp / "identity"))
        self.assertEqual(code, 0)
        self.assertIn("没有可轮换", out)

    def test_path_escape_name_refused(self) -> None:
        for bad in ("../escape", "a/b", "..", ""):
            with self.subTest(bad=bad):
                code, _, err = self._run_main(bad, "--root", str(self.tmp / "identity"))
                self.assertEqual(code, 1)
                self.assertTrue(self.identity.exists(), "拒绝路径上不得有删除动作")
                self.assertIn("拒绝", err)

    def test_forbidden_component_root_refused(self) -> None:
        # 根路径任一级含 .local-secrets / instances → 结构性拒绝（防未来调用方手滑）。
        for forbidden in (".local-secrets", "instances"):
            root = self.tmp / forbidden / "sub"
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(ic.CleanupRefused):
                    ic.cleanup_plan("x", root)
                self.assertFalse(root.exists(), "拒绝路径上不得创建任何目录")

    def test_product_repo_root_refused(self) -> None:
        from tests.testbench.instances import product_repo_root

        with self.assertRaises(ic.CleanupRefused):
            ic.cleanup_plan("x", root=product_repo_root())

    def test_plan_reports_shape(self) -> None:
        plan = ic.cleanup_plan("github-co", self.root)
        self.assertTrue(plan.exists)
        self.assertEqual(plan.file_count, 2)
        self.assertGreater(plan.total_bytes, 0)
        self.assertEqual(plan.target_dir, self.identity)

    def test_list_identities_enumerates(self) -> None:
        (self.root / "second-id").mkdir()
        plans = ic.list_identities(self.root)
        self.assertEqual({p.identity for p in plans}, {"github-co", "second-id"})
        code, out, _ = self._run_main("--list", "--root", str(self.tmp / "identity"))
        self.assertEqual(code, 0)
        self.assertIn("github-co", out)

    def test_list_json_is_machine_readable(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ic.main(["--list", "--json", "--root", str(self.tmp / "identity")])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload[0]["identity"], "github-co")
        self.assertTrue(payload[0]["exists"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
