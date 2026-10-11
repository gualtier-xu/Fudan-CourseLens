from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.request import ProxyHandler, Request, build_opener

from src.runtime.lifecycle import (
    ERROR_INSTANCE_ACTIVE,
    ERROR_PORT_BUSY,
    InstanceLock,
    LifecycleController,
    LifecycleError,
    PUBLIC_STAGES,
    PUBLIC_STATES,
    install_signal_handlers,
)
from src.runtime.http_api import FrontendSessionRegistry
from src.runtime.startup_migration import coordinate_local_data
from src.runtime.task_store import REMOTE_RUN_LIFECYCLE_STATES, TaskStore
from src.application import CourseLensApplication


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _read_instance(root: Path, process: subprocess.Popen, timeout: float = 45.0) -> dict:
    path = root / "server-instance.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"child exited early: {process.returncode}: {stdout!r} {stderr!r}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            time.sleep(0.05)
    tails = _pipe_tails(process)
    raise AssertionError(
        "instance evidence was not published; "
        f"rc={process.poll()} stdout={tails[0]!r} stderr={tails[1]!r}"
    )


def _pipe_tails(process: subprocess.Popen, limit: int = 2000) -> tuple[str, str]:
    """Drained pipe tails（MAC-NIGHT-1）：超时断言携带子进程输出做诊断面。"""
    out = "".join(getattr(process, "_courselens_out_tail", []))
    err = "".join(getattr(process, "_courselens_err_tail", []))
    return (out[-limit:], err[-limit:])


def _start(root: Path, port: int = 0) -> subprocess.Popen:
    process = subprocess.Popen(
        [
            PYTHON,
            # MAC-NIGHT-1 round4：-u=子进程输出不进缓冲（尾巴即时成像）；
            # -X importtime=导入清单进 stderr 尾巴——若子进程卡在导入期，
            # 卡点直接可见；若导入完成而仍无「Fudan CourseLens UI」行，
            # 则卡点落在 bind 链（http.server.server_bind 的
            # socket.getfqdn 反查在部分 macOS 网络栈上可拖 10-30s——
            # 晨间决策 #2 候选，产品侧 2 行 server_bind 覆写可根治）。
            "-u",
            "-X",
            "importtime",
            "-m",
            "src",
            "serve",
            "--port",
            str(port),
            "--data-dir",
            str(root),
            "--no-open",
            "--keep-server",
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        ),
    )
    if os.name != "nt":
        # MAC-NIGHT-1：并发排水管——未读的 PIPE 一旦填满 64KB 缓冲会反向
        # 阻塞子进程（mac 腿特有的假挂起源），超时断言也要携带输出尾巴做
        # 诊断面。Windows 腿在用例内提前 return，不走本路径，零变化。
        process._courselens_out_tail = []
        process._courselens_err_tail = []
        for stream, sink in (
            (process.stdout, process._courselens_out_tail),
            (process.stderr, process._courselens_err_tail),
        ):
            def _drain(stream=stream, sink=sink):
                try:
                    for line in stream:
                        sink.append(line)
                except Exception:
                    pass
            threading.Thread(target=_drain, daemon=True).start()
    return process


def _shutdown(port: int) -> dict:
    url = f"http://127.0.0.1:{port}"
    request = Request(
        f"{url}/api/v3/lifecycle/shutdown",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", "Origin": url},
    )
    with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


class LifecycleContractTests(unittest.TestCase):
    @staticmethod
    def _active_work_service(*, active_remote=False):
        service = CourseLensApplication.__new__(CourseLensApplication)
        service.task_store = MagicMock()
        service.task_store.count.return_value = 0
        service.task_store.has_active_remote_run.return_value = active_remote
        service._active_subtitle_task_ids = set()
        service._active_summary_task_id = None
        service._active_question_task_id = None
        service._remote_echo_thread = None
        return service

    def test_persisted_active_remote_runs_keep_automatic_shutdown_alive(self):
        for state in REMOTE_RUN_LIFECYCLE_STATES:
            with self.subTest(state=state):
                service = self._active_work_service(active_remote=True)
                self.assertTrue(service.has_active_work())
                service.task_store.has_active_remote_run.assert_called_once_with(REMOTE_RUN_LIFECYCLE_STATES)

    def test_terminal_remote_run_does_not_keep_automatic_shutdown_alive(self):
        service = self._active_work_service()
        self.assertFalse(service.has_active_work())

    def test_remote_run_store_failure_keeps_automatic_shutdown_alive(self):
        service = self._active_work_service()
        service.task_store.has_active_remote_run.side_effect = OSError("state store unavailable")
        self.assertTrue(service.has_active_work())

    def test_public_state_and_stage_are_closed_sets(self):
        controller = LifecycleController()
        for state in PUBLIC_STATES:
            controller.transition(state, "bootstrap")
            self.assertEqual(controller.snapshot()["state"], state)
        for stage in PUBLIC_STAGES:
            controller.transition("starting", stage)
            self.assertEqual(controller.snapshot()["stage"], stage)
        with self.assertRaises(ValueError):
            controller.transition("secret-state", "bootstrap")
        with self.assertRaises(ValueError):
            controller.transition("starting", "course-url")

    def test_stale_instance_and_pid_reuse_are_removed_without_killing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "server-instance.json"
            root.mkdir(exist_ok=True)
            if os.name == "nt":
                path.write_text(
                    json.dumps({
                        "pid": 424_242,
                        "process_started_at": 1,
                        "instance_id": "test",
                    }),
                    encoding="utf-8",
                )
                with (
                    patch("src.runtime.lifecycle._process_alive", return_value=True),
                    patch("src.runtime.lifecycle._process_started_at", side_effect=(2, 3)),
                    patch("src.runtime.lifecycle.os.kill") as process_kill,
                ):
                    lock = InstanceLock(root, "test")
                    lock._remove_stale_instance_evidence()
                self.assertFalse(path.exists())
                process_kill.assert_not_called()
                return

            for process_started_at in (0, 1):
                path.write_text(
                    json.dumps({
                        "pid": os.getpid(),
                        "process_started_at": process_started_at,
                        "instance_id": "test",
                    }),
                    encoding="utf-8",
                )
                lock = InstanceLock(root, "test")
                lock.acquire()
                self.assertFalse(path.exists())
                lock.release()

    def test_second_instance_is_rejected_without_pid_action(self):
        if os.name == "nt":
            with tempfile.TemporaryDirectory() as folder:
                with (
                    patch("src.runtime.lifecycle._process_started_at", return_value=1),
                    patch("msvcrt.locking", side_effect=OSError("locked")) as locking,
                ):
                    second = InstanceLock(folder, "test")
                    with self.assertRaises(LifecycleError) as raised:
                        second.acquire()
                self.assertEqual(raised.exception.code, ERROR_INSTANCE_ACTIVE)
                locking.assert_called_once()
            return

        with tempfile.TemporaryDirectory() as folder:
            first = InstanceLock(folder, "test")
            second = InstanceLock(folder, "test")
            first.acquire()
            try:
                with self.assertRaises(LifecycleError) as raised:
                    second.acquire()
                self.assertEqual(raised.exception.code, ERROR_INSTANCE_ACTIVE)
            finally:
                first.release()

    def test_startup_migration_uses_both_store_schemas_before_open(self):
        with tempfile.TemporaryDirectory() as folder:
            result = coordinate_local_data(folder)
            self.assertEqual(result.status, "completed")
            store = TaskStore(Path(folder) / "state.db")
            store.close()
            self.assertTrue((Path(folder) / "learning.db").is_file())


class FrontendSessionLifecycleTests(unittest.TestCase):
    def test_last_closed_session_waits_for_active_work_then_grace(self):
        shutdown = threading.Event()
        active_work = {"value": True}
        registry = FrontendSessionRegistry(
            lease_seconds=0.05, shutdown_grace_seconds=0.03, close_grace_seconds=0.03,
            poll_seconds=0.01,
        )
        registry.set_shutdown_callback(shutdown.set, should_keep_alive=lambda: active_work["value"])
        try:
            registry.update("synthetic-tab-a", "open")
            registry.update("synthetic-tab-b", "open")
            registry.update("synthetic-tab-a", "close")
            self.assertFalse(shutdown.wait(0.08))
            registry.update("synthetic-tab-b", "close")
            self.assertFalse(shutdown.wait(0.08), "active work protects the service")
            active_work["value"] = False
            self.assertTrue(shutdown.wait(0.5), "last close drains after the grace period")
        finally:
            registry.stop()

    def test_defaults_cover_background_tab_round_trips(self):
        """BACKEND-DEATH-1①：默认租约 300s/失联宽限 60s，显式 close 保持 15s 短宽限。"""
        registry = FrontendSessionRegistry()
        try:
            self.assertEqual(registry.lease_seconds, 300.0)
            self.assertEqual(registry.shutdown_grace_seconds, 60.0)
            self.assertEqual(registry.close_grace_seconds, 15.0)
            # P67 U3c: the headless idle window is deliberately generous —
            # the daily pipeline's discovery phase runs before the first
            # queued task exists, and a premature exit would abort it.
            self.assertEqual(registry.headless_idle_seconds, 1800.0)
        finally:
            registry.stop()

    def test_headless_instance_ends_after_bounded_idle_when_work_settles(self):
        """P67 U3c (DEF-2①)：受管 --no-open 启动（每日任务形态）永远见不到浏览器
        页面，saw_frontend 两条自退路径都不会触发；registry 必须自己收尾——
        连续无工作满无头空闲窗即关停，有工作则窗口不断重置。"""
        shutdown = threading.Event()
        active_work = {"value": True}
        registry = FrontendSessionRegistry(
            lease_seconds=300.0, shutdown_grace_seconds=60.0, close_grace_seconds=15.0,
            headless_idle_seconds=0.05, poll_seconds=0.01,
        )
        registry.set_shutdown_callback(shutdown.set, should_keep_alive=lambda: active_work["value"])
        try:
            self.assertFalse(shutdown.wait(0.15), "active work protects a headless instance")
            active_work["value"] = False
            self.assertTrue(shutdown.wait(0.5), "headless idle must end the process")
        finally:
            registry.stop()

    def test_a_connected_page_disables_the_headless_idle_path(self):
        """P67 U3c：一旦真实页面开过，生命周期归页面路径管；无头空闲窗不得在
        其下触发，显式 close 仍走 15s 快路径。"""
        shutdown = threading.Event()
        registry = FrontendSessionRegistry(
            lease_seconds=300.0, shutdown_grace_seconds=0.25, close_grace_seconds=0.03,
            headless_idle_seconds=0.05, poll_seconds=0.01,
        )
        registry.set_shutdown_callback(shutdown.set, should_keep_alive=lambda: False)
        try:
            registry.update("synthetic-tab", "open")
            self.assertFalse(shutdown.wait(0.2), "a connected page owns the lifecycle")
            registry.update("synthetic-tab", "close")
            self.assertTrue(shutdown.wait(0.5), "explicit close keeps the fast-path grace")
        finally:
            registry.stop()

    def test_heartbeat_inside_lease_defers_expiry(self):
        """租约内补发心跳即续约：首开已超原始租约仍不判空、不关停。"""
        shutdown = threading.Event()
        registry = FrontendSessionRegistry(
            lease_seconds=0.2, shutdown_grace_seconds=0.3, close_grace_seconds=0.05,
            poll_seconds=0.01,
        )
        registry.set_shutdown_callback(shutdown.set, should_keep_alive=lambda: False)
        try:
            registry.update("synthetic-tab", "open")
            time.sleep(0.12)
            registry.update("synthetic-tab", "heartbeat")
            time.sleep(0.15)
            self.assertFalse(shutdown.wait(0.01), "heartbeat inside the lease renews the session")
            registry.update("synthetic-tab", "close")
            self.assertTrue(shutdown.wait(0.5), "explicit close keeps the short fast-path grace")
        finally:
            registry.stop()

    def test_expired_emptiness_waits_longer_than_explicit_close(self):
        """失联（租约到期）用长宽限，显式 close 用短宽限：同为空，close 先关停。"""
        shutdown = threading.Event()
        registry = FrontendSessionRegistry(
            lease_seconds=0.06, shutdown_grace_seconds=0.25, close_grace_seconds=0.04,
            poll_seconds=0.01,
        )
        registry.set_shutdown_callback(shutdown.set, should_keep_alive=lambda: False)
        try:
            registry.update("synthetic-tab", "open")
            # 0.15s 时租约（0.06s）早已到期且超过 close 短宽限（0.04s），
            # 但失联长宽限（0.25s）未满 → 服务必须仍在。
            self.assertFalse(shutdown.wait(0.15), "lost contact waits for the long grace")
            self.assertTrue(shutdown.wait(1.0), "long grace eventually shuts the service down")
        finally:
            registry.stop()

        shutdown = threading.Event()
        registry = FrontendSessionRegistry(
            # MAC-NIGHT-1 round4：时标 ×4 放量（慢 runner 线程调度抖动下
            # 0.15s 断言不稳定，round3 探针实证）；语义不变=close 短宽限
            # （0.16s）≪ 失联长宽限（1.0s），两分支判据仍互相区分。
            lease_seconds=0.24,
            shutdown_grace_seconds=1.0,
            close_grace_seconds=0.16,
            poll_seconds=0.02,
        )
        registry.set_shutdown_callback(shutdown.set, should_keep_alive=lambda: False)
        try:
            registry.update("synthetic-tab", "open")
            registry.update("synthetic-tab", "close")
            self.assertTrue(shutdown.wait(0.6), "explicit close keeps the short fast-path grace")
        finally:
            registry.stop()


class ShutdownReasonTraceTests(unittest.TestCase):
    def test_first_shutdown_request_prints_reason_once(self):
        """BACKEND-DEATH-1②：任意原因首次关停在 stdout 留痕一行，重复请求不打印。"""
        import contextlib
        import io

        from src.app import _TracedLifecycleController

        controller = _TracedLifecycleController()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertTrue(controller.request_shutdown("last_frontend_session"))
            self.assertFalse(controller.request_shutdown("duplicate"))
        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertIn("last_frontend_session", lines[0])


class LifecycleSubprocessTests(unittest.TestCase):
    def _assert_clean_exit(self, process: subprocess.Popen, root: Path) -> None:
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, (stdout, stderr))
        self.assertFalse((root / "server-instance.json").exists())
        self.assertFalse(any(root.glob(".*.tmp")))
        store = TaskStore(root / "state.db")
        try:
            self.assertFalse(store.list_tasks(states=("running", "pausing")))
        finally:
            store.close()

    def test_normal_api_shutdown_releases_instance_and_port(self):
        if os.name == "nt":
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                controller = LifecycleController()
                controller.transition("ready", "serving")
                callback_finished = threading.Event()
                controller.set_shutdown_callback(callback_finished.set)
                instance = InstanceLock(root, "test")
                instance.publish(43210)

                self.assertTrue(controller.request_shutdown("api_request"))
                self.assertTrue(callback_finished.wait(1))
                self.assertFalse(controller.request_shutdown("duplicate"))
                instance.release()

                self.assertEqual(controller.snapshot()["state"], "draining")
                self.assertFalse((root / "server-instance.json").exists())
            return

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            process = _start(root)
            instance = _read_instance(root, process)
            port = int(instance["port"])
            payload = _shutdown(port)
            self.assertTrue(payload["accepted"])
            self._assert_clean_exit(process, root)
            probe = socket.socket()
            try:
                probe.bind(("127.0.0.1", port))
            finally:
                probe.close()

    def test_catchable_interrupt_uses_the_same_drain_path(self):
        if os.name == "nt":
            controller = LifecycleController()
            handlers = {}

            def capture_handler(signum, handler):
                handlers[int(signum)] = handler

            with (
                patch("src.runtime.lifecycle.signal.getsignal", return_value=signal.SIG_DFL),
                patch("src.runtime.lifecycle.signal.signal", side_effect=capture_handler),
            ):
                previous = install_signal_handlers(controller)

            self.assertIn(int(signal.SIGINT), previous)
            self.assertIn(int(signal.SIGINT), handlers)
            handlers[int(signal.SIGINT)](signal.SIGINT, None)
            # BACKEND-DEATH-1④：snapshot 增 additive 键 shutdown_reason（尸检
            # [exit] 行的原因来源），闭集断言同步纳入。
            self.assertEqual(controller.snapshot(), {
                "state": "draining",
                "stage": "request_drain",
                "error_code": "",
                "accepting_requests": False,
                "shutdown_reason": "signal_2",
            })
            self.assertFalse(controller.request_shutdown("duplicate"))
            return

        # POSIX can safely exercise a real SIGINT inside an isolated child.
        code = (
            "import signal;"
            "from src.runtime.lifecycle import LifecycleController,install_signal_handlers;"
            "c=LifecycleController();"
            "install_signal_handlers(c);"
            "signal.raise_signal(signal.SIGINT);"
            "print(c.snapshot()['state'])"
        )
        result = subprocess.run(
            [PYTHON, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "draining")

    def test_port_occupied_fails_closed_without_instance_residue(self):
        if os.name == "nt":
            from src.app import serve

            services = MagicMock()
            services.lifecycle.close.return_value = {"timed_out": False}
            instance = MagicMock()
            with (
                tempfile.TemporaryDirectory() as folder,
                patch("src.app.InstanceLock", return_value=instance),
                patch("src.app.coordinate_local_data"),
                patch("src.app.compose_services", return_value=services),
                patch("src.app.make_handler", return_value=MagicMock()),
                patch("src.app._ExclusiveBindHTTPServer", side_effect=OSError("occupied")),
            ):
                with self.assertRaises(LifecycleError) as raised:
                    serve(data_dir=folder, port=43210, open_browser=False)

            self.assertEqual(raised.exception.code, ERROR_PORT_BUSY)
            instance.acquire.assert_called_once_with()
            instance.publish.assert_not_called()
            instance.release.assert_called_once_with()
            return

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = int(listener.getsockname()[1])
            try:
                process = _start(root, port)
                stdout, stderr = process.communicate(timeout=20)
            finally:
                listener.close()
            self.assertEqual(process.returncode, 1, (stdout, stderr))
            self.assertIn("LIFECYCLE_E_PORT_BUSY", stderr)
            self.assertFalse((root / "server-instance.json").exists())

    def test_hard_kill_converges_on_next_start(self):
        if os.name == "nt":
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                instance_path = root / "server-instance.json"
                stale_pid = 424_242
                instance_path.write_text(
                    json.dumps({
                        "schema": "courselens.instance.v2",
                        "instance_id": "stale",
                        "owner_token": "stale-owner",
                        "pid": stale_pid,
                        "process_started_at": 1,
                        "port": 1,
                    }),
                    encoding="utf-8",
                )

                with (
                    patch("src.runtime.lifecycle._process_alive", return_value=False) as process_alive,
                    patch("src.runtime.lifecycle._process_started_at", return_value=2),
                    patch("src.runtime.lifecycle.os.kill") as process_kill,
                ):
                    recovered = InstanceLock(root, "recovered")
                    recovered._remove_stale_instance_evidence()
                    self.assertFalse(instance_path.exists())
                    recovered.publish(43210)

                process_alive.assert_called_once_with(stale_pid)
                process_kill.assert_not_called()
                payload = json.loads(instance_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["instance_id"], "recovered")
                self.assertEqual(payload["pid"], os.getpid())
                self.assertEqual(payload["process_started_at"], 2)
                self.assertEqual(payload["port"], 43210)
            return

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = _start(root)
            _read_instance(root, first)
            first.kill()
            first.communicate(timeout=10)
            second = _start(root)
            instance = _read_instance(root, second)
            _shutdown(int(instance["port"]))
            self._assert_clean_exit(second, root)


class AutoConnectStartupWiringTests(unittest.TestCase):
    """启动自动连接接线：组合根必须暴露闭集的启动调用且不阻塞就绪。"""

    def test_compose_services_wires_auto_connect_to_the_application(self):
        from src.app import compose_services

        scratch = ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            services = compose_services(Path(folder))
            try:
                self.assertIs(
                    services.lifecycle.start_auto_connect.__func__,
                    CourseLensApplication.start_auto_connect_resume,
                )
            finally:
                services.close()

    def test_start_auto_connect_resume_is_off_without_preference(self):
        scratch = ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            app = CourseLensApplication(Path(folder))
            try:
                self.assertEqual(app.start_auto_connect_resume(), {"state": "off"})
                self.assertEqual(app.start_auto_connect_resume(), {"state": "already_started"})
            finally:
                app.close()

    def test_worker_auto_sync_skipped_without_bound_worker(self):
        """P59-U2：未绑定专属 Worker 时启动对账零远程调用（不起线程）。"""
        scratch = ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            app = CourseLensApplication(Path(folder))
            try:
                with patch.object(app.github_app, "ensure_worker_trusted") as ensure:
                    self.assertEqual(app.start_auto_connect_resume(), {"state": "off"})
                    time.sleep(0.1)
                    ensure.assert_not_called()
            finally:
                app.close()

    def test_worker_auto_sync_runs_once_in_background_when_bound(self):
        """P59-U2：已绑定专属 Worker 时后台对账恰一次，且自动连接偏好全关也照跑。"""
        scratch = ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            app = CourseLensApplication(Path(folder))
            try:
                for name in (
                    "github_worker_repo", "github_mailbox_repo",
                    "worker_box_public_key", "worker_signing_public_key",
                ):
                    app.credentials.save_secret(name, "student/worker" if name.endswith("repo") else "key")
                with patch.object(
                    app.github_app, "ensure_worker_trusted", return_value={"trusted": True}
                ) as ensure:
                    self.assertEqual(app.start_auto_connect_resume(), {"state": "off"})
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline and ensure.call_count == 0:
                        time.sleep(0.01)
                    self.assertEqual(ensure.call_count, 1)
                    self.assertEqual(app.start_auto_connect_resume(), {"state": "already_started"})
                    time.sleep(0.1)
                    self.assertEqual(ensure.call_count, 1, "每进程至多一次对账")
            finally:
                app.close()

    def test_worker_auto_sync_failure_stays_silent(self):
        """P59-U2：对账检查侧异常只闭集留痕，绝不上抛打扰用户。"""
        scratch = ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            app = CourseLensApplication(Path(folder))
            try:
                with patch.object(
                    app.github_app, "ensure_worker_trusted",
                    side_effect=RuntimeError("network down"),
                ):
                    app._run_worker_auto_sync()
            finally:
                app.close()

    def test_worker_auto_sync_skips_remote_check_after_close_stop(self):
        """P63-U1：close 置停后对账入口零远程调用（收口面对齐 probe/resume）。"""
        scratch = ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            app = CourseLensApplication(Path(folder))
            try:
                with patch.object(app.github_app, "ensure_worker_trusted") as ensure:
                    app._worker_auto_sync_stop.set()
                    app._run_worker_auto_sync()
                    ensure.assert_not_called()
            finally:
                app.close()

    def test_serve_starts_auto_connect_exactly_once_after_ready(self):
        source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("services.lifecycle.start_auto_connect()"), 1)
        ready_index = source.index('controller.transition("ready", "serving")')
        auto_index = source.index("services.lifecycle.start_auto_connect()")
        self.assertGreater(auto_index, ready_index, "自动连接在循环服务就绪之后才启动")


class ServerLogPruneTests(unittest.TestCase):
    """C4（PB-1）：启动期会话日志裁剪边界——新件保留、旧件删除、
    恰在 keep_days 边界的保留；非 .log 与目录缺失一律不碰/零计数。"""

    def test_prune_keeps_boundary_and_removes_older(self):
        from src.app import SERVER_LOG_KEEP_DAYS, prune_server_logs

        now = 1_800_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            cases = {
                "server-20260926-010101.log": now - 100,
                "server-20260901-010101.log": now - 15 * 86400,
                "server-python-20260912-010101.log": now - SERVER_LOG_KEEP_DAYS * 86400,
                "notes.txt": now - 400 * 86400,
            }
            for name, mtime in cases.items():
                path = log_dir / name
                path.write_text("x", encoding="utf-8")
                os.utime(path, (mtime, mtime))
            removed = prune_server_logs(log_dir, now=now)
            self.assertEqual(removed, 1)
            self.assertTrue((log_dir / "server-20260926-010101.log").exists(), "新件保留")
            self.assertTrue(
                (log_dir / "server-python-20260912-010101.log").exists(),
                "恰在 14 天边界的保留",
            )
            self.assertFalse((log_dir / "server-20260901-010101.log").exists(), "更旧件删除")
            self.assertTrue((log_dir / "notes.txt").exists(), "非 .log 不碰")

    def test_serve_calls_prune_before_binding(self):
        source = (ROOT / "src" / "app.py").read_text(encoding="utf-8")
        self.assertIn("prune_server_logs()", source)
        self.assertLess(
            source.index("prune_server_logs()"),
            source.index("_ExclusiveBindHTTPServer(\n                (host, port)"),
            "裁剪在启动绑定之前执行",
        )

    def test_missing_dir_returns_zero(self):
        from src.app import prune_server_logs

        self.assertEqual(prune_server_logs(Path(tempfile.gettempdir()) / "pb1-no-such-dir"), 0)


class RuntimeAutopsyLogTests(unittest.TestCase):
    """BACKEND-DEATH-1④：直启 serve 的死亡可归因面。

    背景=2026-10-10 F2/F6 两度后端静默死亡（product-onboardreal1-result-20261010
    定谳：宿主清理者 TerminateProcess，零 traceback/零 WER/零关停行）。本钉把
    三类死亡变成可归因：优雅退出→[exit] 行；原生崩溃→faulthandler 栈；硬终止→
    [heartbeat] 截断（最后心跳=死亡时间下界）。
    """

    def test_install_writes_header_redirects_stderr_and_enables_faulthandler(self):
        import faulthandler
        import sys as _sys

        from src.app import _RuntimeDiagnostics

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            was_enabled = faulthandler.is_enabled()
            diagnostics = _RuntimeDiagnostics(root, heartbeat_seconds=60.0)
            try:
                path = diagnostics.install()
                self.assertIsNotNone(path)
                self.assertIsNotNone(diagnostics.path)
                self.assertTrue(path.exists())
                self.assertTrue(path.name.startswith("backend-stderr-"))
                self.assertTrue(path.is_relative_to(root))
                self.assertTrue(faulthandler.is_enabled())
                self.assertIs(_sys.stderr, diagnostics._file)
            finally:
                diagnostics.close()
            if not was_enabled:
                faulthandler.disable()
            text = path.read_text(encoding="utf-8")
            self.assertIn("[autopsy]", text)
            self.assertIn("faulthandler=on", text)

    def test_heartbeat_and_exit_line_share_the_log_then_stop(self):
        import sys as _sys
        import time as _time

        from src.app import _RuntimeDiagnostics

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            diagnostics = _RuntimeDiagnostics(root, heartbeat_seconds=0.05)
            try:
                path = diagnostics.install()
                _time.sleep(0.3)
                diagnostics.record_exit("last_frontend_session")
            finally:
                diagnostics.close()
            text = path.read_text(encoding="utf-8")
            self.assertGreaterEqual(text.count("[heartbeat]"), 1)
            self.assertIn("[exit] reason=last_frontend_session", text)
            before = text.count("[heartbeat]")
            _time.sleep(0.15)
            self.assertEqual(
                path.read_text(encoding="utf-8").count("[heartbeat]"),
                before,
                "close 之后心跳线程必须停",
            )

    def test_stderr_restored_after_close(self):
        import sys as _sys

        from src.app import _RuntimeDiagnostics

        previous = _sys.stderr
        with tempfile.TemporaryDirectory() as folder:
            diagnostics = _RuntimeDiagnostics(Path(folder), heartbeat_seconds=60.0)
            try:
                self.assertIsNotNone(diagnostics.install())
                self.assertIsNot(previous, _sys.stderr)
            finally:
                diagnostics.close()
            self.assertIs(previous, _sys.stderr)

    def test_unwritable_root_degrades_to_none_without_raising(self):
        from src.app import _RuntimeDiagnostics

        with tempfile.TemporaryDirectory() as folder:
            blocker = Path(folder) / "blocker.log"
            blocker.write_text("not a dir", encoding="utf-8")
            diagnostics = _RuntimeDiagnostics(blocker)
            try:
                self.assertIsNone(diagnostics.install())
                self.assertIsNone(diagnostics.path)
            finally:
                diagnostics.close()

    def test_snapshot_exposes_shutdown_reason(self):
        controller = LifecycleController()
        self.assertEqual(controller.snapshot()["shutdown_reason"], "")
        self.assertTrue(controller.request_shutdown("last_frontend_session"))
        self.assertEqual(
            controller.snapshot()["shutdown_reason"], "last_frontend_session"
        )

    def test_serve_wires_autopsy_install_before_exit_recording(self):
        # 源钉：serve() 装尸检面、finally 收笔，且次序=先装面后收笔。
        import inspect

        from src import app as app_module

        source = inspect.getsource(app_module.serve)
        self.assertIn("_install_runtime_diagnostics(root)", source)
        self.assertIn("diagnostics.record_exit(", source)
        self.assertIn("diagnostics.close()", source)
        self.assertLess(
            source.index("_install_runtime_diagnostics(root)"),
            source.index("diagnostics.record_exit("),
        )


if __name__ == "__main__":
    unittest.main()
