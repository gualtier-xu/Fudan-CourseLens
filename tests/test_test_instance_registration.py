"""serve 测试实例自动注册钉（测试台架批 4·件③M2 src 侧，TESTBENCH-DESIGN-1 §3.3 M2）。

验收判据（设计原文）：serve test mode 挂接=自动注册/心跳/释放；claim 与
tests/testbench/instances.py 同格式互认；注册失败不拦服务（可观测性设施）。
本文件全部离线（除一个本机回环子进程冒烟），零外联、零真实注册表触碰
（注册表目录一律临时目录注入 COURSELENS_TEST_REGISTRY）。

读取纪律（A3）+上下文预算（A2）适用；预计请求数 ≈8。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.runtime import test_instance as ti
from src.runtime import test_mode as tm
from src.runtime.test_mode import TestModeContractError as _ContractError

INSTANCE_ENV = tm.TEST_INSTANCE_ENV
OWNER_ENV = tm.TEST_INSTANCE_OWNER_ENV
REGISTRY_ENV = tm.TEST_REGISTRY_ENV
MODE_ENV = tm.TEST_MODE_ENV
DATA_ENV = tm.DATA_DIR_ENV

PYTHON = str(ROOT / ".venv-client-py310" / "Scripts" / "python.exe")


class _EnvCase(unittest.TestCase):
    _WATCHED = (INSTANCE_ENV, OWNER_ENV, REGISTRY_ENV, MODE_ENV, DATA_ENV)

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


class TestEnvContract(_EnvCase):
    def test_unset_env_yields_no_registrar(self) -> None:
        self.assertIsNone(ti.registrar_from_env({}))

    def test_invalid_instance_name_refused(self) -> None:
        for bad in ("../escape", "a/b", "", "  ", "x" * 101, ".hidden"):
            if not bad.strip():
                continue  # 空串 = 未启用（另行断言）
            with self.assertRaises(_ContractError) as caught:
                tm.validate_instance_name(bad)
            self.assertEqual(caught.exception.code, "test_instance_name_invalid")
        with self.assertRaises(_ContractError):
            ti.registrar_from_env({INSTANCE_ENV: "../escape"})
        # 空串 = 未启用注册（不抛）。
        self.assertEqual(tm.validate_instance_name(""), "")
        self.assertEqual(tm.validate_instance_name("remotee2e1r4-backend"), "remotee2e1r4-backend")

    def test_env_readers_and_owner_default(self) -> None:
        registrar = ti.registrar_from_env(
            {INSTANCE_ENV: "lane1-backend", OWNER_ENV: "LANE-1", REGISTRY_ENV: "R:/reg"}
        )
        self.assertIsNotNone(registrar)
        self.assertEqual(registrar.name, "lane1-backend")
        self.assertEqual(registrar.owner_lane, "LANE-1")
        self.assertEqual(registrar.registry_dir, Path("R:/reg"))
        # owner 未设 → serve-auto；registry 未设 → 工作区默认注册表。
        fallback = ti.registrar_from_env({INSTANCE_ENV: "lane2-backend"})
        self.assertEqual(fallback.owner_lane, "serve-auto")
        self.assertEqual(fallback.registry_dir, ti.default_registry_dir())


class TestRegistrationLifecycle(_EnvCase):
    def _temp_registry(self, temp) -> Path:
        base = Path(temp.name) if hasattr(temp, "name") else Path(temp)
        registry = base / "registry"
        registry.mkdir(parents=True, exist_ok=True)
        return registry

    def test_fresh_registration_manager_compatible(self) -> None:
        """自动注册的 claim 与实例管理器同格式互认（list/is_stale/字段全对）。"""
        from tests.testbench.instances import ClaimRecord, TestInstanceManager

        with tempfile.TemporaryDirectory() as temp:
            registry = self._temp_registry(temp)
            data_dir = Path(temp) / "sandbox-data"
            data_dir.mkdir()
            registrar = ti.ServeInstanceRegistrar(
                name="tbw4-pin-backend", registry_dir=registry, owner_lane="TB-W4"
            )
            registrar.register(port=17777, data_dir=data_dir)
            try:
                manager = TestInstanceManager(registry_dir=registry)
                listing = manager.list_instances()
                self.assertEqual([item["name"] for item in listing], ["tbw4-pin-backend"])
                self.assertFalse(manager.is_stale("tbw4-pin-backend"))
                record = ClaimRecord.from_dict(
                    json.loads(registrar.claim_path.read_text(encoding="utf-8"))
                )
                self.assertEqual(record.pid, os.getpid())
                self.assertEqual(record.port, 17777)
                self.assertEqual(record.kind, "backend")
                self.assertEqual(record.owner_lane, "TB-W4")
                self.assertEqual(record.data_dir, data_dir.resolve())
                self.assertTrue(
                    any(event.get("type") == "serve_register" for event in record.events)
                )
            finally:
                registrar.close()

    def test_adopt_lane_claim_preserves_owner(self) -> None:
        """「先 claim 再起进程」：serve 采纳车道 claim——owner/事件史保留，pid/port 换本体。"""
        from tests.testbench.instances import TestInstanceManager

        with tempfile.TemporaryDirectory() as temp:
            registry = self._temp_registry(temp)
            manager = TestInstanceManager(registry_dir=registry)
            handle = manager.claim("tbw4-pin-adopt", owner_lane="LANE-X")
            try:
                data_dir = Path(temp) / "adopt-data"
                data_dir.mkdir()
                registrar = ti.ServeInstanceRegistrar(
                    name="tbw4-pin-adopt", registry_dir=registry, owner_lane="serve-auto"
                )
                registrar.register(port=handle.port, data_dir=data_dir)
                try:
                    record = json.loads(registrar.claim_path.read_text(encoding="utf-8"))
                    self.assertEqual(record["owner_lane"], "LANE-X")  # 车道追责链不断
                    self.assertEqual(record["pid"], os.getpid())  # 持有进程=serve 本体
                    self.assertEqual(record["port"], handle.port)
                    self.assertEqual(
                        [event["type"] for event in record["events"]], ["claim", "serve_adopt"]
                    )
                finally:
                    registrar.close()
                self.assertFalse(registrar.claim_path.exists())
                # close 后 manager 侧同名释放视图一致（claim 已删）。
                with self.assertRaises(Exception):
                    manager.heartbeat("tbw4-pin-adopt")
            finally:
                leftover = registry / "tbw4-pin-adopt.json"
                if leftover.exists():
                    leftover.unlink()

    def test_heartbeat_touches_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            registrar = ti.ServeInstanceRegistrar(
                name="tbw4-pin-beat", registry_dir=self._temp_registry(temp), owner_lane="TB-W4"
            )
            registrar.register(port=17790, data_dir=temp)
            try:
                old = time.time() - 60.0
                os.utime(registrar.claim_path, (old, old))
                registrar.heartbeat()
                self.assertGreater(os.path.getmtime(registrar.claim_path), old + 30.0)
            finally:
                registrar.close()

    def test_heartbeat_thread_renews_periodically(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            registrar = ti.ServeInstanceRegistrar(
                name="tbw4-pin-thread",
                registry_dir=self._temp_registry(temp),
                owner_lane="TB-W4",
                heartbeat_interval=0.05,
            )
            registrar.register(port=17791, data_dir=temp)
            try:
                old = time.time() - 60.0
                os.utime(registrar.claim_path, (old, old))
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if os.path.getmtime(registrar.claim_path) > old + 30.0:
                        break
                    time.sleep(0.05)
                self.assertGreater(os.path.getmtime(registrar.claim_path), old + 30.0)
            finally:
                registrar.close()

    def test_close_is_idempotent_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            registrar = ti.ServeInstanceRegistrar(
                name="tbw4-pin-close", registry_dir=self._temp_registry(temp), owner_lane="TB-W4"
            )
            registrar.register(port=17792, data_dir=temp)
            self.assertTrue(registrar.claim_path.exists())
            registrar.close()
            self.assertFalse(registrar.claim_path.exists())
            registrar.close()  # 幂等
            self.assertFalse(registrar.claim_path.exists())

    def test_close_keeps_foreign_claim(self) -> None:
        """易主（接管形态）后退出不删他人 claim——release 语义与实例管理器一致。"""
        with tempfile.TemporaryDirectory() as temp:
            registrar = ti.ServeInstanceRegistrar(
                name="tbw4-pin-foreign", registry_dir=self._temp_registry(temp), owner_lane="TB-W4"
            )
            registrar.register(port=17793, data_dir=temp)
            record = json.loads(registrar.claim_path.read_text(encoding="utf-8"))
            record["pid"] = 999999  # 模拟已被接管换主（pid 非本进程）
            registrar._replace_atomic(registrar.claim_path, record)
            registrar.close()
            self.assertTrue(registrar.claim_path.exists(), "他人持有的 claim 不得被本进程删除")
            registrar.claim_path.unlink()  # 收尾自清（本钉自建的临时物）

    def test_concurrent_create_race_converges(self) -> None:
        """并发注册同名（一方 O_EXCL 胜出，另一方转采纳）：恰好一份 claim、pid 归胜者。"""
        with tempfile.TemporaryDirectory() as temp:
            registry = self._temp_registry(temp)
            winners: list[int] = []
            barrier = threading.Barrier(2)

            def _race(tag: int) -> None:
                barrier.wait()
                registrar = ti.ServeInstanceRegistrar(
                    name="tbw4-pin-race", registry_dir=registry, owner_lane=f"LANE-{tag}"
                )
                registrar.register(port=17700 + tag, data_dir=temp)
                winners.append(tag)
                registrar.close()

            threads = [threading.Thread(target=_race, args=(i,)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(len(winners), 2)
            leftovers = list(registry.glob("tbw4-pin-race*"))
            self.assertEqual(leftovers, [], f"两注册器都释放后注册表应干净：{leftovers}")


class TestServeWiring(_EnvCase):
    def test_app_serve_wires_register_and_close(self) -> None:
        """接线源码钉：serve() 必含注册+释放两触点（抽掉任一即红）。"""
        text = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        self.assertIn("_make_test_instance_registrar()", text)
        self.assertIn("test_instance_registrar.register(", text)
        self.assertIn("test_instance_registrar.close()", text)
        self.assertIn("validate_instance_name", text)

    def test_env_literals_confined_to_contract_module(self) -> None:
        offenders: list[str] = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            if path.name == "test_mode.py":
                continue
            text = path.read_text(encoding="utf-8")
            if any(literal in text for literal in ("COURSELENS_TEST_INSTANCE", "COURSELENS_TEST_REGISTRY")):
                offenders.append(str(path))
        self.assertEqual(offenders, [], "实例注册 env 字面量只允许出现在契约模块")

    def test_serve_registration_smoke(self) -> None:
        """子进程真起 serve：注册行+claim 内容（pid=进程本体）→优雅关停→claim 释放。"""
        with tempfile.TemporaryDirectory(dir=ROOT / "runtime" / "cache", prefix="tbw4-smoke-") as sandbox:
            registry = Path(sandbox) / "registry"
            env = {k: v for k, v in os.environ.items() if k not in set(self._WATCHED)}
            env.update({
                MODE_ENV: "real",
                DATA_ENV: str(Path(sandbox) / "data"),
                INSTANCE_ENV: "tbw4-smoke-backend",
                REGISTRY_ENV: str(registry),
                "PYTHONIOENCODING": "utf-8",
            })
            (Path(sandbox) / "data").mkdir()
            proc = subprocess.Popen(
                [PYTHON, "-m", "src", "serve", "--port", "0", "--no-open"],
                cwd=str(ROOT), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace",
            )
            out_lines: list[str] = []

            def _drain(pipe) -> None:
                for line in pipe:
                    out_lines.append(line)

            drain_thread = threading.Thread(target=_drain, args=(proc.stdout,), daemon=True)
            drain_thread.start()
            claim_path = registry / "tbw4-smoke-backend.json"
            try:
                ui_line = None
                deadline = time.monotonic() + 90.0
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        break
                    ui_line = next((l for l in out_lines if "Fudan CourseLens UI:" in l), None)
                    if ui_line:
                        break
                    time.sleep(0.1)
                self.assertTrue(ui_line, f"serve 未就绪：{out_lines!r}")
                url = ui_line.split("Fudan CourseLens UI:", 1)[1].strip()
                # 注册行 = 机读证据；claim 内容 = pid/端口同源。
                self.assertTrue(
                    any("测试实例已注册：tbw4-smoke-backend" in line for line in out_lines),
                    f"缺注册横幅：{out_lines!r}",
                )
                deadline = time.monotonic() + 10.0
                record = {}
                while time.monotonic() < deadline:
                    if claim_path.exists():
                        record = json.loads(claim_path.read_text(encoding="utf-8"))
                        break
                    time.sleep(0.1)
                # Windows venv 启动器存在父子进程链（proc.pid=launcher），故按
                # stale 判定同口径断言：claim pid=存活进程；关停后死亡。
                from tests.testbench.instances import pid_alive

                claim_pid = int(record.get("pid", 0))
                self.assertGreater(claim_pid, 0)
                self.assertTrue(pid_alive(claim_pid), f"claim pid 应存活：{record}")
                self.assertEqual(int(record.get("port", 0)), int(url.rsplit(":", 1)[1].strip("/")))
                self.assertEqual(record.get("owner_lane"), "serve-auto")
                # 就绪等待（health ok = accepting_requests）——就绪前 shutdown 路由 503。
                deadline = time.monotonic() + 60.0
                while time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=3) as resp:
                            if resp.status == 200 and json.loads(resp.read().decode("utf-8")).get("ok"):
                                break
                    except (urllib.error.URLError, OSError, ValueError):
                        pass
                    time.sleep(0.2)
                # 优雅关停经产品自身关停路由（lifecycle/shutdown）→ finally 释放 claim。
                request = urllib.request.Request(
                    url.rstrip("/") + "/api/v3/lifecycle/shutdown", data=b"{}", method="POST",
                    headers={"Content-Type": "application/json"},
                )
                last_error: Exception | None = None
                for _attempt in range(5):
                    try:
                        with urllib.request.urlopen(request, timeout=10) as resp:
                            self.assertEqual(resp.status, 202)
                        last_error = None
                        break
                    except urllib.error.HTTPError as exc:
                        last_error = exc
                        time.sleep(0.5)
                self.assertIsNone(last_error, f"shutdown 路由未接受：{last_error}")
                proc.wait(timeout=30)
                self.assertEqual(proc.returncode, 0, f"优雅关停应零退出码；out={out_lines!r}")
                self.assertFalse(claim_path.exists(), "优雅退出后 claim 必须已释放")
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                drain_thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
