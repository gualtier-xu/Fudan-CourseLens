"""Chrome 生命周期封装钉测（件③M2 tests 侧；TB-W2）。

两类钉：
- **离线钉**（零 Chrome、零外联）：argv 构建纯函数、指纹匹配纯函数、taskkill
  唯一形态（/IM 结构性排除）、kind 守卫、CDP 000 语义、失败收尾释放 claim；
- **真实起停钉**（chrome.exe 在位才跑，否则诚实 SKIP）：10 次 up/down 循环
  零残留进程、零端口冲突，且运行前已存在的用户 Chrome 进程全程不受扰
  （cmdline 复核留证）。
"""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.testbench import chrome_lifecycle as cl
from tests.testbench.instances import TestInstanceManager, detect_port_conflicts, pid_alive


def _chrome_present() -> bool:
    import os

    hint = os.environ.get(
        "COURSELENS_TESTBENCH_CHROME",
        os.path.join(
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            "Google", "Chrome", "Application", "chrome.exe",
        ),
    )
    return Path(hint).is_file()


def _fake_processes() -> list[dict]:
    """模拟进程表：本车道实例 / 用户默认 Chrome / 他道实例 / 无 cmdline 系统进程。"""
    ours = r"D:\ws\.testbench\instances\tbw2-c1\chrome-profile"
    return [
        {
            "ProcessId": 101,
            "CommandLine": rf'"C:\...\chrome.exe" --user-data-dir={ours} --remote-debugging-port=17705',
        },
        {
            "ProcessId": 102,
            "CommandLine": r'"C:\...\chrome.exe" --user-data-dir="C:\Users\stu\AppData\Local\Google\Chrome\User Data" --type=renderer',
        },
        {
            "ProcessId": 103,
            "CommandLine": r'"C:\...\chrome.exe" --user-data-dir=D:\ws\.testbench\instances\otherlane-c1\chrome-profile',
        },
        {"ProcessId": 104, "CommandLine": None},
    ]


class ChromeArgvPins(unittest.TestCase):
    """argv 纯函数钉：独立 profile 铁律 + CDP 端口 + 可选项按需。"""

    def test_argv_carries_profile_and_cdp(self) -> None:
        argv = cl.build_chrome_argv(
            profile_dir=r"D:\ws\.testbench\instances\n1\chrome-profile",
            cdp_port=17705,
        )
        self.assertIn("--user-data-dir=D:\\ws\\.testbench\\instances\\n1\\chrome-profile", argv)
        self.assertIn("--remote-debugging-port=17705", argv)
        self.assertIn("--no-first-run", argv)
        self.assertIn("--headless=new", argv)
        self.assertFalse(any("User Data" in a for a in argv), "禁触用户默认 profile")

    def test_proxy_and_headless_optional(self) -> None:
        argv = cl.build_chrome_argv(profile_dir="p", cdp_port=1, headless=False)
        self.assertFalse(any(a.startswith("--headless") for a in argv))
        argv2 = cl.build_chrome_argv(
            profile_dir="p", cdp_port=1, proxy_server="http://127.0.0.1:7890"
        )
        self.assertIn("--proxy-server=http://127.0.0.1:7890", argv2)
        argv3 = cl.build_chrome_argv(profile_dir="p", cdp_port=1)
        self.assertFalse(any(a.startswith("--proxy-server") for a in argv3))


class FingerprintPins(unittest.TestCase):
    """指纹匹配纯函数钉：只认本实例 profile，用户/他道实例/空 cmdline 一律不认。"""

    OURS = r"D:\ws\.testbench\instances\tbw2-c1\chrome-profile"

    def test_match_only_ours(self) -> None:
        for row in _fake_processes():
            matched = cl.match_fingerprint(row["CommandLine"], self.OURS)
            if row["ProcessId"] == 101:
                self.assertTrue(matched)
            else:
                self.assertFalse(matched, f"PID {row['ProcessId']} 不得被误判为本实例进程")

    def test_separator_and_case_variants_match(self) -> None:
        self.assertTrue(cl.match_fingerprint("d:/ws/.TESTBENCH/instances/tbw2-c1/chrome-profile", self.OURS))
        self.assertFalse(cl.match_fingerprint(None, self.OURS))
        self.assertFalse(cl.match_fingerprint("", self.OURS))
        # 前缀相似但不同实例：不得误匹配。
        self.assertFalse(
            cl.match_fingerprint(
                r"D:\ws\.testbench\instances\tbw2-c10\chrome-profile",
                self.OURS,
            )
        )


class TaskkillFormPins(unittest.TestCase):
    """杀进程唯一形态钉：逐 PID /T /F；/IM 全量杀结构性排除。"""

    def test_taskkill_command_shape(self) -> None:
        import subprocess

        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            cl._taskkill_tree(4242)
            argv = run.call_args.args[0]
            self.assertEqual(argv, ["taskkill", "/PID", "4242", "/T", "/F"])

    def test_module_never_issues_im_kill(self) -> None:
        import inspect

        source = inspect.getsource(cl)
        # 文档里可以「提到」/IM（禁令本身要写出来）；代码里不许出现其 argv 形态。
        self.assertNotIn('"/IM"', source)
        self.assertNotIn("'/IM'", source)
        self.assertNotIn('"taskkill", "/IM"', source)


class CdpProbePins(unittest.TestCase):
    """CDP 000 语义钉：连接拒绝/超时=000（curl 语义），不是任何假状态。"""

    def test_closed_port_reports_000(self) -> None:
        sock = __import__("socket").socket()
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]
        sock.close()
        self.assertEqual(cl.cdp_probe(free_port, timeout_s=1.0), "000")


class DownGuardPins(unittest.TestCase):
    """down 守卫钉：kind 不是 chrome 拒绝；失败收尾必须释放 claim（失败不留僵尸）。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tbw2-chrome-offline-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.manager = TestInstanceManager(self.tmp / "registry", heartbeat_interval=3600)

    def test_down_refuses_non_chrome_kind(self) -> None:
        self.manager.claim("tbw2-backend", kind="backend", owner_lane="TB-W2")
        with self.assertRaises(cl.ChromeLifecycleError) as ctx:
            cl.chrome_down(self.manager, "tbw2-backend", owner_lane="TB-W2")
        self.assertIn("kind='backend'", str(ctx.exception))
        # claim 仍在（拒绝不动它）。
        self.assertTrue((self.tmp / "registry" / "tbw2-backend.json").exists())

    def test_failed_up_cleans_and_releases_claim(self) -> None:
        """CDP 永远起不来 → 指纹清现场 + claim 释放 + ChromeStartupError（不留僵尸）。"""
        with mock.patch.object(cl, "find_chrome_executable", return_value=Path("C:/fake/chrome.exe")):
            with mock.patch.object(cl, "_start_detached", return_value=999999):
                with mock.patch.object(cl, "cdp_probe", return_value="000"):
                    with self.assertRaises(cl.ChromeStartupError):
                        cl.chrome_up(
                            self.manager,
                            "tbw2-failup",
                            owner_lane="TB-W2",
                            ready_timeout_s=1.5,
                        )
        self.assertFalse(
            (self.tmp / "registry" / "tbw2-failup.json").exists(),
            "up 失败必须释放 claim（失败也不留僵尸）",
        )

    def test_up_refuses_without_chrome_executable(self) -> None:
        with mock.patch.object(
            cl,
            "find_chrome_executable",
            side_effect=cl.ChromeUnavailable("本机常见安装位找不到 chrome.exe"),
        ):
            with self.assertRaises(cl.ChromeUnavailable):
                cl.chrome_up(self.manager, "tbw2-nochrome", owner_lane="TB-W2")
        self.assertFalse((self.tmp / "registry" / "tbw2-nochrome.json").exists())


@unittest.skipUnless(_chrome_present(), "诚实 SKIP：本机找不到 chrome.exe")
class ChromeLifecycleRealCycles(unittest.TestCase):
    """验收钉：10 次起停循环零残留、零端口冲突，用户 Chrome 全程不受扰。"""

    CYCLES = 10

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tbw2-chrome-real-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.manager = TestInstanceManager(self.tmp / "registry", heartbeat_interval=3600)

    def _user_processes_snapshot(self, exclude_fingerprints: set[str]) -> dict[int, str]:
        """现有 chrome.exe 里不属于任何本测试实例指纹的进程（=用户的/他道的）。"""
        out: dict[int, str] = {}
        script = (
            "$d = @(Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "Select-Object ProcessId,CommandLine); "
            "if ($d.Count -gt 0) { $d | ConvertTo-Json -Compress } else { '[]' }"
        )
        import json
        import subprocess

        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        payload = json.loads((proc.stdout or "[]").strip() or "[]")
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            cmdline = row.get("CommandLine") or ""
            if any(fp.lower() in cmdline.lower() for fp in exclude_fingerprints):
                continue
            out[int(row["ProcessId"])] = cmdline
        return out

    def test_ten_up_down_cycles_zero_residue_user_untouched(self) -> None:
        evidence: list[dict] = []
        fingerprints: set[str] = set()
        user_before = self._user_processes_snapshot(fingerprints)

        for cycle in range(1, self.CYCLES + 1):
            name = f"tbw2-cycle-{cycle}"
            up = cl.chrome_up(self.manager, name, owner_lane="TB-W2")
            fingerprints.add(str(up.profile_dir))
            self.assertEqual(cl.cdp_probe(up.cdp_port), "200", f"cycle {cycle}: up 后 CDP 应就绪")
            self.assertTrue(pid_alive(up.chrome_pid), f"cycle {cycle}: 启动器进程应存活")

            down = cl.chrome_down(self.manager, name, owner_lane="TB-W2")
            self.assertEqual(down.cdp_status, "000", f"cycle {cycle}: down 后 CDP 必须 000")
            self.assertTrue(down.port_free, f"cycle {cycle}: 端口必须实测可再绑定")
            residual = cl.list_fingerprint_processes(down.profile_dir)
            self.assertEqual(residual, [], f"cycle {cycle}: 零残留进程")
            self.assertFalse(
                (self.tmp / "registry" / f"{name}.json").exists(),
                f"cycle {cycle}: claim 必须已释放",
            )
            conflicts = detect_port_conflicts(self.manager)
            self.assertEqual(conflicts, [], f"cycle {cycle}: 零端口冲突")
            evidence.append(
                {
                    "cycle": cycle,
                    "cdp_port": up.cdp_port,
                    "ready_seconds": up.ready_seconds,
                    "killed_pids": list(down.killed_pids),
                }
            )

        # 用户 Chrome 不受扰：起停前已存在的进程逐 PID 复核（cmdline 留证）。
        user_after = self._user_processes_snapshot(fingerprints)
        died = sorted(set(user_before) - set(user_after))
        for pid in died:
            # 中途自然关闭（用户自己的操作）不算违规；我们的 kill 清单必须与用户 PID 零交集。
            self.assertNotIn(
                pid,
                {p for e in evidence for p in e["killed_pids"]},
                f"用户进程 PID {pid} 出现在本测试的 kill 清单=违规",
            )
        self.assertTrue(evidence)
        print(
            f"\n[tbw2] {self.CYCLES} cycles ok; user chrome before={len(user_before)} "
            f"after={len(user_after)} natural_exits={died}"
        )
        print(f"[tbw2] evidence={evidence}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
