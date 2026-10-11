"""FRESH_OPERATIONS 旗标契约（TB-W3，TESTBENCH-DESIGN-1 件②M2 尾差）。

E2E-2v6 教训收编：复用沙盒数据目录时，``stored_id`` 会把上一次运行的旧
失败记录当既有结果回放（12:57 的失败记录在下一轮被原样吐回）。旗标
``COURSELENS_TEST_FRESH_OPERATIONS=1``（仅测试模式生效）让各 operation
入口跳过幂等回放历史、一律重新执行。

钉面三层：
1. **helper 语义钉**：发布版（未设 COURSELENS_TEST_MODE）恒 False——发布
   路径与本旗标零接触；测试模式值闭集 = {"1"}。
2. **行为钉**：真实合成服务 + 真实 sqlite store，预置带 marker 的 stale
   operation 记录——无旗标=回放（响应含 marker）；置旗标=重执行（marker
   消失、store 被新结果覆盖）。
3. **接线+文档钉**：四处 stored_id 回放分支（http_api×3 + automation×1）
   守卫必须逐处在位；旗标必须在 ``synthetic_backend_recipe`` 车道文档
   一处可查清单内。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from src.runtime import test_mode as tm
from src.runtime.http_api import make_handler
from tests.http_services import http_services
from tests.synthetic_shell_server import build_service

ROOT = Path(__file__).resolve().parents[1]

MODE_ENV = tm.TEST_MODE_ENV
FRESH_ENV = tm.FRESH_OPERATIONS_ENV


def _clear_flag_env() -> None:
    import os

    for key in (MODE_ENV, FRESH_ENV):
        os.environ.pop(key, None)


def _set_flag_env(mode: str, fresh: str | None) -> None:
    import os

    os.environ[MODE_ENV] = mode
    if fresh is None:
        os.environ.pop(FRESH_ENV, None)
    else:
        os.environ[FRESH_ENV] = fresh


class FreshOperationsHelperTests(unittest.TestCase):
    """helper 语义钉：发布版惰性 + 测试模式门 + 值闭集。"""

    def tearDown(self) -> None:
        _clear_flag_env()

    def test_release_without_test_mode_is_always_false(self) -> None:
        """未设 COURSELENS_TEST_MODE（发布版）：旗标无论设不设都 False。"""
        environ = {FRESH_ENV: "1"}
        self.assertFalse(tm.fresh_operations_requested(environ))
        self.assertFalse(tm.fresh_operations_requested({}))

    def test_test_mode_value_closed_set(self) -> None:
        """测试模式下值闭集 {"1"}：1=启用；未设/其他值一律不启用。"""
        self.assertTrue(tm.fresh_operations_requested({MODE_ENV: "synthetic", FRESH_ENV: "1"}))
        self.assertFalse(tm.fresh_operations_requested({MODE_ENV: "synthetic"}))
        self.assertFalse(tm.fresh_operations_requested({MODE_ENV: "synthetic", FRESH_ENV: "true"}))
        self.assertFalse(tm.fresh_operations_requested({MODE_ENV: "synthetic", FRESH_ENV: "0"}))
        self.assertTrue(tm.fresh_operations_requested({MODE_ENV: "real", FRESH_ENV: "1"}))

    def test_env_literal_confined_to_contract_module(self) -> None:
        """旗标 env 字面量只允许出现在契约模块（与既有双旗标同钉扩容）。"""
        offenders: list[str] = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            if path.name == "test_mode.py":
                continue
            if FRESH_ENV in path.read_text(encoding="utf-8"):
                offenders.append(str(path))
        self.assertEqual(offenders, [], "FRESH_OPERATIONS env 字面量只允许出现在契约模块")


class FreshOperationsReplayBehaviorTests(unittest.TestCase):
    """行为钉：stale marker 判别「回放」vs「重执行」（真实合成服务）。"""

    def tearDown(self) -> None:
        _clear_flag_env()

    def test_stale_replay_without_flag_and_fresh_rerun_with_flag(self) -> None:
        import os

        with tempfile.TemporaryDirectory(
            dir=ROOT / "runtime" / "cache", prefix="tbw3-fresh-"
        ) as sandbox:
            service = build_service(Path(sandbox), onboarding_profile="ready")
            services = http_services(service)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), make_handler(services, Path(sandbox) / "frontend")
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                stored_id = "authentication:tbw3-fresh-01"
                stale = {"marker": "stale-1257", "action": "logout", "operation_id": "tbw3-fresh-01"}
                services.tasks.repository.set_app_state(stored_id, stale)

                # 无旗标：回放既有记录——stale marker 原样吐回（E2E-2v6 坑形）。
                _clear_flag_env()
                body = self._post_authentication(base, "tbw3-fresh-01")
                self.assertIn(
                    "stale-1257", json.dumps(body), f"无旗标应回放 stale 记录: {body}"
                )

                # 置旗标：跳过回放、重新执行——marker 消失，store 被新结果覆盖。
                _set_flag_env("synthetic", "1")
                self.assertTrue(tm.fresh_operations_requested())
                body = self._post_authentication(base, "tbw3-fresh-01")
                self.assertNotIn(
                    "stale-1257", json.dumps(body), f"置旗标应重执行而非回放: {body}"
                )
                rerun = services.tasks.repository.get_app_state(stored_id, None)
                self.assertIsInstance(rerun, dict)
                self.assertNotIn("marker", rerun, f"store 应被新结果覆盖: {rerun}")

                # 旗标撤除后回放语义复原（新 operation_id 走既有幂等契约）。
                _clear_flag_env()
                self.assertFalse(tm.fresh_operations_requested())
                services.tasks.repository.set_app_state(
                    "authentication:tbw3-fresh-02", {"marker": "stale-again"}
                )
                body = self._post_authentication(base, "tbw3-fresh-02")
                self.assertIn("stale-again", json.dumps(body), f"撤旗后应复原回放: {body}")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                service.close()
                _clear_flag_env()
                os.environ.pop(MODE_ENV, None)

    @staticmethod
    def _post_authentication(base: str, operation_id: str) -> dict:
        payload = json.dumps({"action": "logout", "operation_id": operation_id}).encode()
        request = urllib.request.Request(
            f"{base}/api/v3/authentication/actions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))


class FreshOperationsWiringAndDocTests(unittest.TestCase):
    """接线+文档钉：四处回放分支守卫在位；旗标入车道文档一处可查清单。"""

    def test_replay_branches_guarded(self) -> None:
        expected = {
            ROOT / "src" / "runtime" / "http_api.py": 3,
            ROOT / "src" / "runtime" / "automation.py": 1,
        }
        for path, count in expected.items():
            text = path.read_text(encoding="utf-8")
            self.assertEqual(
                text.count("and not fresh_operations_requested()"),
                count,
                f"{path.name} 回放守卫缺漏（应 {count} 处）",
            )
            self.assertIn(
                "from src.runtime.test_mode import fresh_operations_requested",
                text,
                f"{path.name} 守卫接线导入缺位",
            )

    def test_flag_documented_in_lane_recipe(self) -> None:
        """车道文档一处可查全部旗标：env 旗标闭集必须在配方 docstring 集中。"""
        recipe_doc = tm.synthetic_backend_recipe.__doc__ or ""
        for literal in (
            tm.TEST_MODE_ENV,
            tm.EGRESS_ALLOW_ENV,
            tm.FRESH_OPERATIONS_ENV,
        ):
            self.assertIn(literal, recipe_doc, f"车道文档缺旗标 {literal}")


if __name__ == "__main__":
    unittest.main()
