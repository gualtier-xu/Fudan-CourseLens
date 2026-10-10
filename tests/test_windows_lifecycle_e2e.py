from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
CI_ROOT = Path(os.environ.get("COURSELENS_WINDOWS_LIFECYCLE_ROOT", ""))
PID_LEDGER = Path(os.environ.get("COURSELENS_WINDOWS_LIFECYCLE_PID_LEDGER", ""))
CHILD_CODE = (
    "import sys;"
    "COURSELENS_WINDOWS_LIFECYCLE_CHILD='1';"
    "from src.app import serve;"
    "serve(data_dir=sys.argv[1],port=int(sys.argv[2]),"
    "open_browser=False,stop_when_frontend_closes=False)"
)


def _windows_ci_enabled() -> bool:
    if os.name != "nt":
        return False
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return False
    if os.environ.get("COURSELENS_WINDOWS_LIFECYCLE_E2E") != "1":
        return False
    runner_temp = os.environ.get("RUNNER_TEMP")
    configured_root = os.environ.get("COURSELENS_WINDOWS_LIFECYCLE_ROOT")
    configured_ledger = os.environ.get("COURSELENS_WINDOWS_LIFECYCLE_PID_LEDGER")
    if not runner_temp or not configured_root or not configured_ledger:
        return False
    try:
        runner_root = Path(runner_temp).resolve()
        return (
            Path(configured_root).resolve().is_relative_to(runner_root)
            and Path(configured_ledger).resolve().is_relative_to(runner_root)
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _record_process(process: subprocess.Popen[str], data_root: Path) -> None:
    PID_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with PID_LEDGER.open("a", encoding="ascii") as stream:
        stream.write(json.dumps({
            "pid": process.pid,
            "data_root": str(data_root.resolve()),
        }, ensure_ascii=True) + "\n")


def _start(data_root: Path, port: int = 0) -> subprocess.Popen[str]:
    data_root.mkdir(parents=True, exist_ok=True)
    child_environment = os.environ.copy()
    child_environment["COURSELENS_DATA_DIR"] = str(data_root.resolve())
    process = subprocess.Popen(
        [
            PYTHON,
            "-c",
            CHILD_CODE,
            str(data_root),
            str(port),
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        env=child_environment,
    )
    _record_process(process, data_root)
    return process


def _read_instance(
    data_root: Path,
    process: subprocess.Popen[str],
    timeout: float = 15.0,
) -> dict[str, object]:
    instance_path = data_root / "server-instance.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(
                f"child exited early: {process.returncode}: {stdout!r} {stderr!r}"
            )
        try:
            payload = json.loads(instance_path.read_text(encoding="utf-8"))
            if int(payload.get("pid") or 0) == process.pid:
                return payload
        except (FileNotFoundError, OSError, TypeError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError("instance evidence was not published")


def _shutdown(port: int) -> dict[str, object]:
    origin = f"http://127.0.0.1:{port}"
    request = Request(
        f"{origin}/api/v3/lifecycle/shutdown",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", "Origin": origin},
    )
    with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def _force_kill(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    subprocess.run(
        ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _assert_no_residue(test: unittest.TestCase, data_root: Path) -> None:
    test.assertFalse((data_root / "server-instance.json").exists())
    test.assertFalse(any(data_root.glob(".*.tmp")))


def _assert_port_released(test: unittest.TestCase, port: int) -> None:
    deadline = time.monotonic() + 5
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.1)
        finally:
            probe.close()
    test.fail(f"port {port} was not released: {last_error}")


@unittest.skipUnless(
    _windows_ci_enabled(),
    "real Windows lifecycle E2E is restricted to an isolated GitHub runner",
)
class WindowsLifecycleE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        CI_ROOT.mkdir(parents=True, exist_ok=True)

    def test_api_shutdown_releases_evidence_and_port(self) -> None:
        with tempfile.TemporaryDirectory(dir=CI_ROOT) as folder:
            data_root = Path(folder)
            process = _start(data_root)
            try:
                instance = _read_instance(data_root, process)
                port = int(instance["port"])
                self.assertTrue(_shutdown(port)["accepted"])
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, (stdout, stderr))
                _assert_no_residue(self, data_root)
                _assert_port_released(self, port)
            finally:
                _force_kill(process)

    def test_real_instance_lock_rejects_second_process(self) -> None:
        with tempfile.TemporaryDirectory(dir=CI_ROOT) as folder:
            data_root = Path(folder)
            first = _start(data_root)
            second: subprocess.Popen[str] | None = None
            try:
                first_instance = _read_instance(data_root, first)
                second = _start(data_root)
                stdout, stderr = second.communicate(timeout=10)
                self.assertEqual(second.returncode, 1, (stdout, stderr))
                self.assertIn("LIFECYCLE_E_INSTANCE_ACTIVE", stderr)
                self.assertIsNone(first.poll())
                current = json.loads(
                    (data_root / "server-instance.json").read_text(encoding="utf-8")
                )
                self.assertEqual(current["pid"], first.pid)
                self.assertTrue(_shutdown(int(first_instance["port"]))["accepted"])
                stdout, stderr = first.communicate(timeout=20)
                self.assertEqual(first.returncode, 0, (stdout, stderr))
                _assert_no_residue(self, data_root)
            finally:
                if second is not None:
                    _force_kill(second)
                _force_kill(first)

    def test_ctrl_break_uses_graceful_drain(self) -> None:
        with tempfile.TemporaryDirectory(dir=CI_ROOT) as folder:
            data_root = Path(folder)
            process = _start(data_root)
            try:
                instance = _read_instance(data_root, process)
                port = int(instance["port"])
                process.send_signal(signal.CTRL_BREAK_EVENT)
                stdout, stderr = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, (stdout, stderr))
                _assert_no_residue(self, data_root)
                _assert_port_released(self, port)
            finally:
                _force_kill(process)

    def test_real_port_conflict_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir=CI_ROOT) as folder:
            data_root = Path(folder)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = int(listener.getsockname()[1])
            process = _start(data_root, port)
            try:
                stdout, stderr = process.communicate(timeout=20)
            finally:
                listener.close()
                _force_kill(process)
            self.assertEqual(process.returncode, 1, (stdout, stderr))
            self.assertIn("LIFECYCLE_E_PORT_BUSY", stderr)
            _assert_no_residue(self, data_root)

    def test_hard_kill_recovers_stale_evidence_on_restart(self) -> None:
        with tempfile.TemporaryDirectory(dir=CI_ROOT) as folder:
            data_root = Path(folder)
            first = _start(data_root)
            recovered: subprocess.Popen[str] | None = None
            try:
                stale = _read_instance(data_root, first)
                self.assertEqual(stale["pid"], first.pid)
                _force_kill(first)
                self.assertTrue((data_root / "server-instance.json").exists())

                recovered = _start(data_root)
                current = _read_instance(data_root, recovered)
                self.assertEqual(current["pid"], recovered.pid)
                self.assertNotEqual(current["owner_token"], stale["owner_token"])
                self.assertTrue(_shutdown(int(current["port"]))["accepted"])
                stdout, stderr = recovered.communicate(timeout=20)
                self.assertEqual(recovered.returncode, 0, (stdout, stderr))
                _assert_no_residue(self, data_root)
            finally:
                if recovered is not None:
                    _force_kill(recovered)
                _force_kill(first)


if __name__ == "__main__":
    unittest.main()
