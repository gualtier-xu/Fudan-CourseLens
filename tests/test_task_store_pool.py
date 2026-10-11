"""夜10-C 补令②a：TaskStore 可写连接池化钉（opt-in，SAVEPOINT 嵌套语义）。"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.runtime.task_store import TaskStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TaskStorePoolTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.store = TaskStore(Path(self.temporary.name) / "state.db")
        self.store.enable_connection_pool()
        # addCleanup 后进先出：先关 store（释放池化句柄）再清临时目录
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.store.close)

    def test_pooled_connection_is_reused_per_thread(self) -> None:
        self.store.set_feature_flag("pool-probe", True)
        self.store.get_feature_flags()
        with self.store._connect() as first:
            with self.store._connect() as second:
                self.assertIs(first, second, "同线程池化复用同一连接")
        with self.store._connect() as again:
            self.assertIs(again, first, "跨调用仍复用")

    def test_nested_inner_failure_rolls_back_inner_only(self) -> None:
        # 外层写 + 内层失败被外层捕获：内层写入回滚，外层写入随外层提交存活
        # （与一次性连接的独立事务语义逐位一致）。
        self.store.set_feature_flag("outer-marker", True)
        try:
            with self.store._connect() as db:
                db.execute(
                    "INSERT INTO feature_flags(name,enabled,updated_at) VALUES('outer-row',1,0)"
                )
                try:
                    with self.store._connect() as inner:
                        inner.execute(
                            "INSERT INTO feature_flags(name,enabled,updated_at) VALUES('inner-row',1,0)"
                        )
                        raise RuntimeError("inner boom")
                except RuntimeError:
                    pass
        finally:
            pass
        check = sqlite3.connect(self.store.path)
        try:
            names = {row[0] for row in check.execute("SELECT name FROM feature_flags")}
        finally:
            check.close()
        self.assertIn("outer-row", names)
        self.assertNotIn("inner-row", names, "内层失败只回滚内层写入")

    def test_outer_failure_rolls_back_the_whole_transaction(self) -> None:
        try:
            with self.store._connect() as db:
                db.execute(
                    "INSERT INTO feature_flags(name,enabled,updated_at) VALUES('doomed',1,0)"
                )
                with self.store._connect() as inner:
                    inner.execute(
                        "INSERT INTO feature_flags(name,enabled,updated_at) VALUES('doomed-inner',1,0)"
                    )
                raise RuntimeError("outer boom")
        except RuntimeError:
            pass
        check = sqlite3.connect(self.store.path)
        try:
            names = {row[0] for row in check.execute("SELECT name FROM feature_flags")}
        finally:
            check.close()
        self.assertNotIn("doomed", names)
        self.assertNotIn("doomed-inner", names, "外层回滚连带嵌套写入")

    def test_close_releases_the_pooled_connection(self) -> None:
        self.store.set_feature_flag("pre-close", True)
        db = self.store._local.db
        self.assertIsNotNone(db)
        self.store.close()
        self.assertIsNone(self.store._local.db)
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")

    def test_pool_latency_benefit_smoke(self) -> None:
        # 收益冒烟（不做硬阈值断言）：池化下 200 次读明显快于逐次连接。
        self.store.set_feature_flag("latency-probe", True)
        start = time.perf_counter()
        for _ in range(200):
            self.store.get_feature_flags()
        pooled = time.perf_counter() - start
        self.assertLess(pooled, 2.0, "200 次池化读应在秒级内完成")

    def test_dead_thread_pooled_connections_get_swept(self) -> None:
        # 夜12-SOAK3 根因钉：服务端每连接一线程，短命线程（SSE 重连/媒体
        # range 请求）进池后退出；注册表若不逐出，sqlite 句柄/页缓存随进程
        # 无限滞留（长窗实测 PM +19MB/5min、句柄 +40/min）。
        def churn() -> None:
            self.store.set_feature_flag("churn-marker", True)

        threads = [threading.Thread(target=churn) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertGreaterEqual(
            len(self.store._pooled_registry), 5, "前置：短命线程连接已进池"
        )
        self.store._pool_sweep_threshold = 1  # 允许下一次 _connect 触发清扫
        self.store._pool_last_sweep = 0.0
        with self.store._connect():
            pass
        self.assertEqual(
            len(self.store._pooled_registry),
            1,
            "清扫后只应剩当前存活线程的连接",
        )

    def test_sweep_never_evicts_a_live_thread_connection(self) -> None:
        self.store.set_feature_flag("main-marker", True)
        holder_started = threading.Event()
        holder_release = threading.Event()

        def hold() -> None:
            with self.store._connect():
                holder_started.set()
                holder_release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            self.assertTrue(holder_started.wait(5))
            before = set(self.store._pooled_registry)
            self.store._pool_last_sweep = 0.0
            self.store._sweep_pooled_registry()
            after = set(self.store._pooled_registry)
            self.assertEqual(before, after, "全存活场景清扫不得逐出任何条目")
            self.assertIn(holder.ident, after)
        finally:
            holder_release.set()
            holder.join()

    def test_sweep_throttle_bounds_close_cost(self) -> None:
        self.store.set_feature_flag("prime-marker", True)  # 注册表已有本线程连接
        self.store._pool_sweep_threshold = 1
        self.store._pool_last_sweep = 0.0
        with self.store._connect():
            pass  # 达阈值 → 触发清扫并盖节流戳
        first = self.store._pool_last_sweep
        self.assertGreater(first, 0.0, "前置：清扫已触发并盖章")
        with self.store._connect():
            pass
        self.assertEqual(
            first,
            self.store._pool_last_sweep,
            "节流窗内不重复清扫",
        )


if __name__ == "__main__":
    unittest.main()
