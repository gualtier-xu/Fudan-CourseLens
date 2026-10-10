"""测试实例管理器钉测（测试台架件③M1；TB-INST-M1）。

覆盖任务书四类钉 + 变异验证靶：
- 并发 claim 互斥（线程级 + 跨进程级）与 10 车道并发零端口碰撞；
- 心跳过期 stale 判定与接管（含接管前进程存活核验、接管竞态）；
- 端口分配避开 registry 在册与实测占用；
- 真装只读边界守卫（真装端口/用户默认目录/产品仓内目录/名字逃逸）。

全部用注入的临时 registry 与小 stale 阈值，零真实 120s 等待、零真装触碰。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests.testbench.instances import (
    FORBIDDEN_PORT_RANGES,
    TEST_PORT_MAX,
    TEST_PORT_MIN,
    InstanceAlreadyClaimed,
    InstanceManagerError,
    InstanceNotClaimed,
    InvalidInstanceName,
    RealInstallProtectionError,
    TakeoverRefused,
    TestInstanceManager,
    detect_port_conflicts,
    ensure_test_data_dir,
    ensure_test_port,
    pid_alive,
    platform_default_data_dirs,
    product_repo_root,
)

_SUBPROCESS_CLAIM_CODE = """\
import sys
from tests.testbench.instances import TestInstanceManager, InstanceAlreadyClaimed

m = TestInstanceManager(sys.argv[1])
try:
    m.claim('race-target', owner_lane='sub-' + sys.argv[2], auto_heartbeat=False)
    print('WIN')
except InstanceAlreadyClaimed:
    print('LOSE')
"""


class InstanceManagerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="tbinstm1-")
        self.addCleanup(self._tmp.cleanup)
        self.registry = Path(self._tmp.name) / "instances"
        self.manager = TestInstanceManager(self.registry, stale_seconds=0.5)

    # -- 工具 ----------------------------------------------------------------

    def _age_claim(self, name: str, seconds: float = 100.0) -> None:
        path = self.registry / f"{name}.json"
        old = time.time() - seconds
        os.utime(path, (old, old))

    def _spawn_dead_pid(self) -> int:
        """产一个确定已死的 pid（spawn→terminate→wait），供 pid 实存核验钉测。"""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.terminate()
        proc.wait(timeout=30)
        self.assertFalse(pid_alive(proc.pid))
        return proc.pid


class ClaimBasicTest(InstanceManagerTestCase):
    def test_claim_creates_record_and_dirs(self) -> None:
        with self.manager.claim("lanea-backend", owner_lane="LANE-A", auto_heartbeat=False) as inst:
            self.assertTrue(TEST_PORT_MIN <= inst.port <= TEST_PORT_MAX)
            self.assertEqual(inst.data_dir, self.registry / "lanea-backend" / "data")
            self.assertTrue(inst.data_dir.is_dir())
            self.assertTrue(inst.chrome_profile_dir.is_dir())
            self.assertTrue(inst.scratch_dir.is_dir())
            record_path = self.registry / "lanea-backend.json"
            self.assertTrue(record_path.exists())
            entries = self.manager.list_instances()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["name"], "lanea-backend")
            self.assertEqual(entries[0]["owner_lane"], "LANE-A")
            self.assertFalse(entries[0]["stale"])
        self.assertFalse((self.registry / "lanea-backend.json").exists())

    def test_claim_rejects_bad_owner_and_kind(self) -> None:
        with self.assertRaises(InstanceManagerError):
            self.manager.claim("x1", owner_lane="  ")
        with self.assertRaises(InstanceManagerError):
            self.manager.claim("x2", owner_lane="LANE-A", kind="webserver")


class ClaimMutexTest(InstanceManagerTestCase):
    def test_concurrent_same_name_threads_exactly_one_winner(self) -> None:
        winner, losers = [], []
        barrier = threading.Barrier(8)

        def racer(index: int) -> None:
            barrier.wait()
            try:
                self.manager.claim(
                    "contested", owner_lane=f"LANE-{index}", auto_heartbeat=False
                )
                winner.append(index)
            except InstanceAlreadyClaimed:
                losers.append(index)

        threads = [threading.Thread(target=racer, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(winner), 1, "同名并发 claim 必须恰好一个胜者")
        self.assertEqual(len(losers), 7)

    def test_concurrent_same_name_cross_process_exactly_one_winner(self) -> None:
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _SUBPROCESS_CLAIM_CODE, str(self.registry), str(i)],
                cwd=str(product_repo_root()),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for i in range(4)
        ]
        outs = [proc.communicate(timeout=120)[0].strip() for proc in procs]
        wins = [out for out in outs if out == "WIN"]
        self.assertEqual(len(wins), 1, f"跨进程同名 claim 必须恰好一个胜者，实得 {outs}")
        self.assertTrue(
            all(out == "LOSE" for out in outs if out != "WIN"),
            f"败者必须以 InstanceAlreadyClaimed 失败，实得 {outs}",
        )

    def test_ten_lanes_concurrent_distinct_names_zero_port_collision(self) -> None:
        errors: list[Exception] = []
        handles = []
        barrier = threading.Barrier(10)

        def racer(index: int) -> None:
            barrier.wait()
            try:
                # 并发期间全部持有（禁 with 即用即放）：持有态撞端口分配才是真实场景。
                handles.append(
                    self.manager.claim(
                        f"lane{index:02d}-backend",
                        owner_lane=f"LANE-{index:02d}",
                        auto_heartbeat=False,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 钉测收证
                errors.append(exc)

        threads = [threading.Thread(target=racer, args=(i,)) for i in range(10)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            self.assertEqual(errors, [])
            self.assertEqual(len(handles), 10)
            ports = [handle.port for handle in handles]
            self.assertEqual(len(set(ports)), 10, f"端口必须两两不同，实得 {ports}")
            for port in ports:
                self.assertTrue(TEST_PORT_MIN <= port <= TEST_PORT_MAX)
            self.assertEqual(detect_port_conflicts(self.manager), [], "撞号哨兵必须恒空")
        finally:
            for handle in handles:
                handle.release()


class HeartbeatStaleTest(InstanceManagerTestCase):
    def test_heartbeat_api_refreshes_stale(self) -> None:
        """变异靶①：manager.heartbeat() 失效 → 本钉必红。"""
        self.manager.claim(
            "hb-api", owner_lane="LANE-A", pid=os.getpid(), auto_heartbeat=False
        )
        self._age_claim("hb-api")
        self.assertTrue(self.manager.is_stale("hb-api"), "心跳过期必须判 stale")
        self.manager.heartbeat("hb-api")
        self.assertFalse(self.manager.is_stale("hb-api"), "心跳续约后必须解除 stale")

    def test_heartbeat_thread_keeps_claim_fresh_and_release_stops_it(self) -> None:
        """变异靶②：抽掉心跳线程的续约（manager.heartbeat 变 no-op）→ 本钉必红。"""
        manager = TestInstanceManager(
            self.registry, stale_seconds=0.5, heartbeat_interval=0.1
        )
        inst = manager.claim("hb-thread", owner_lane="LANE-A", pid=os.getpid())
        try:
            self._age_claim("hb-thread")  # 模拟线程尚未接管续约前的陈旧态
            time.sleep(0.6)  # 线程周期 0.1s：真实现必然已续约 ≥3 次
            self.assertFalse(
                manager.is_stale("hb-thread"), "心跳线程必须把陈旧 claim 续回新鲜"
            )
        finally:
            inst.release()
        with self.assertRaises(InstanceNotClaimed):
            manager.heartbeat("hb-thread")

    def test_fresh_heartbeat_with_live_pid_not_stale(self) -> None:
        self.manager.claim("fresh", owner_lane="LANE-A", pid=os.getpid(), auto_heartbeat=False)
        self.assertFalse(self.manager.is_stale("fresh"))


class TakeoverTest(InstanceManagerTestCase):
    def test_takeover_after_holder_process_death(self) -> None:
        dead_pid = self._spawn_dead_pid()
        first = self.manager.claim(
            "dead-holder", owner_lane="LANE-A", pid=dead_pid, auto_heartbeat=False
        )
        original_data_dir = first.data_dir
        self.assertTrue(self.manager.is_stale("dead-holder"), "pid 实存核验失败必须立即判 stale")
        second = self.manager.takeover("dead-holder", new_owner_lane="LANE-B")
        try:
            self.assertEqual(second.record.owner_lane, "LANE-B")
            self.assertEqual(second.record.port, first.record.port, "接管沿用原端口")
            self.assertEqual(second.data_dir, original_data_dir, "接管沿用原数据目录")
            takeover_events = [e for e in second.record.events if e["type"] == "takeover"]
            self.assertEqual(len(takeover_events), 1, "takeover 必须留痕")
            self.assertEqual(takeover_events[0]["from_owner"], "LANE-A")
            with self.assertRaises(InstanceManagerError):
                self.manager.release("dead-holder", owner_lane="LANE-A")  # 老持有者无权释放
        finally:
            second.release()

    def test_takeover_refused_while_holder_process_alive(self) -> None:
        """接管前进程存活核验：心跳过期但进程活着 → 拒绝接管（防互踩）。"""
        self.manager.claim("alive-holder", owner_lane="LANE-A", pid=os.getpid(), auto_heartbeat=False)
        self._age_claim("alive-holder")
        self.assertTrue(self.manager.is_stale("alive-holder"))
        with self.assertRaises(TakeoverRefused):
            self.manager.takeover("alive-holder", new_owner_lane="LANE-B")

    def test_takeover_race_exactly_one_winner(self) -> None:
        dead_pid = self._spawn_dead_pid()
        self.manager.claim("race-takeover", owner_lane="LANE-A", pid=dead_pid, auto_heartbeat=False)
        winners, refusals = [], []
        barrier = threading.Barrier(4)

        def taker(index: int) -> None:
            barrier.wait()
            try:
                self.manager.takeover("race-takeover", new_owner_lane=f"LANE-{index}")
                winners.append(index)
            except (InstanceAlreadyClaimed, TakeoverRefused):
                # 两种非胜者形态都合法：撞上接管锁（InstanceAlreadyClaimed）
                # 或读到的已是他人接管后的新 claim（TakeoverRefused）。
                refusals.append(index)

        threads = [threading.Thread(target=taker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(refusals), 3)


class PortAllocationTest(InstanceManagerTestCase):
    def test_allocate_skips_registry_claimed_and_bound_ports(self) -> None:
        first = self.manager.claim("port-holder", owner_lane="LANE-A", auto_heartbeat=False)
        claimed_port = first.port
        # 临时占住另一个实测可用端口（不进 registry），分配器必须双查都避开。
        held_port = self.manager.allocate_port()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("0.0.0.0", held_port))
        try:
            allocated = self.manager.allocate_port()
            self.assertNotEqual(allocated, claimed_port, "分配器必须避开 registry 在册端口")
            self.assertNotEqual(allocated, held_port, "分配器必须避开 bind 实测占用端口")
            self.assertTrue(TEST_PORT_MIN <= allocated <= TEST_PORT_MAX)
        finally:
            sock.close()
            first.release()

    def test_claim_with_explicit_inuse_port_refused(self) -> None:
        holder = self.manager.claim("port-a", owner_lane="LANE-A", auto_heartbeat=False)
        try:
            with self.assertRaises(InstanceManagerError):
                self.manager.claim("port-b", owner_lane="LANE-B", port=holder.port)
        finally:
            holder.release()

    def test_claim_with_bind_occupied_port_refused(self) -> None:
        port = self.manager.allocate_port()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("0.0.0.0", port))
        try:
            with self.assertRaises(InstanceManagerError):
                self.manager.claim("port-c", owner_lane="LANE-A", port=port)
            self.assertFalse(
                (self.registry / "port-c.json").exists(), "被拒 claim 不得留下半注册状态"
            )
        finally:
            sock.close()


class RealInstallGuardTest(InstanceManagerTestCase):
    def test_guard_refuses_real_install_and_reserved_ports(self) -> None:
        for port in (6268, 7482):
            with self.assertRaises(RealInstallProtectionError, msg=f"端口 {port}"):
                ensure_test_port(port)
        for lo, hi, _why in FORBIDDEN_PORT_RANGES:
            with self.assertRaises(RealInstallProtectionError, msg=f"段 {lo}-{hi}"):
                ensure_test_port((lo + hi) // 2)
        for port in (TEST_PORT_MIN - 1, TEST_PORT_MAX + 1, 8790, 8775):
            with self.assertRaises(RealInstallProtectionError, msg=f"段外端口 {port}"):
                ensure_test_port(port)
        ensure_test_port(TEST_PORT_MIN)
        ensure_test_port(TEST_PORT_MAX)

    def test_guard_refuses_user_default_data_dirs_and_repo_dirs(self) -> None:
        for default_dir in platform_default_data_dirs():
            with self.assertRaises(RealInstallProtectionError):
                ensure_test_data_dir(default_dir)
            with self.assertRaises(RealInstallProtectionError):
                ensure_test_data_dir(default_dir / "sub" / "deeper")
        with self.assertRaises(RealInstallProtectionError):
            ensure_test_data_dir(product_repo_root() / "runtime" / "data")
        with self.assertRaises(RealInstallProtectionError):
            ensure_test_data_dir(product_repo_root() / "some" / "scratch")
        # 注册表内路径必须放行（实例数据目录的合法归宿）。
        ensure_test_data_dir(self.registry / "some-instance" / "data")

    def test_claim_refuses_real_install_port_and_leaves_no_state(self) -> None:
        with self.assertRaises(RealInstallProtectionError):
            self.manager.claim("guard-port", owner_lane="LANE-A", port=7482)
        self.assertFalse((self.registry / "guard-port.json").exists())

    def test_claim_refuses_name_escape(self) -> None:
        for bad in ("../evil", "a/b", "a\\b", "..", ".hidden"):
            with self.assertRaises(InvalidInstanceName, msg=f"非法名 {bad!r}"):
                self.manager.claim(bad, owner_lane="LANE-A")
        self.assertEqual(list(self.registry.glob("*.json")), [], "被拒名字不得留下任何 claim")


class ReleaseAndQueryTest(InstanceManagerTestCase):
    def test_release_semantics(self) -> None:
        inst = self.manager.claim("rel", owner_lane="LANE-A", auto_heartbeat=False)
        inst.release()
        self.assertFalse((self.registry / "rel.json").exists())
        with self.assertRaises(InstanceNotClaimed):
            self.manager.release("rel")
        with self.assertRaises(InstanceNotClaimed):
            self.manager.is_stale("rel")

    def test_list_is_machine_readable(self) -> None:
        first = self.manager.claim("q1", owner_lane="LANE-A", auto_heartbeat=False)
        dead_pid = self._spawn_dead_pid()
        second = self.manager.claim("q2", owner_lane="LANE-B", pid=dead_pid, auto_heartbeat=False)
        entries = {entry["name"]: entry for entry in self.manager.list_instances()}
        self.assertIn("q1", entries)
        self.assertIn("q2", entries)
        self.assertFalse(entries["q1"]["stale"])
        self.assertTrue(entries["q2"]["stale"], "pid 死亡的实例必须被标 stale")
        self.assertFalse(entries["q2"]["pid_alive"])
        first.release()
        second.release()
        self.assertEqual(self.manager.list_instances(), [])

    def test_wait_released(self) -> None:
        inst = self.manager.claim("waitme", owner_lane="LANE-A", auto_heartbeat=False)
        timer = threading.Timer(0.3, inst.release)
        timer.start()
        try:
            self.assertTrue(self.manager.wait_released("waitme", timeout=5.0))
            live = self.manager.claim("waitme2", owner_lane="LANE-A", auto_heartbeat=False)
            try:
                self.assertFalse(self.manager.wait_released("waitme2", timeout=0.3, poll=0.1))
            finally:
                live.release()
        finally:
            timer.join(timeout=5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
