# AS6（第四十七案）任务消耗透镜定向钉：加性迁移/绝对落库幂等/本月累计/
# 快照透出/余额读数闭集态。任务行 NULL=无记录（历史任务不伪造）是本包核心
# 诚实语义，全部钉面围绕它展开。
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from src.application import CourseLensApplication
from src.runtime.http_api import public_task
from src.runtime.task_store import TaskStore


def _add_task(store: TaskStore, *, kind: str = "summary") -> str:
    task, _ = store.add_task(kind, "course-1", "sub-1", {})
    return str(task["task_id"])


class TaskUsageLensStoreTests(unittest.TestCase):
    """task_store：两列加性迁移 + record_task_usage 绝对覆盖语义。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state.db"
        self.store = TaskStore(self.path)

    def test_migration_adds_nullable_usage_columns_idempotently(self) -> None:
        with self.store._connect() as db:
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
        self.assertIn("deepseek_tokens", columns)
        self.assertIn("runner_seconds", columns)
        # 幂等：同一文件再开一次不抛不重建
        reopened = TaskStore(self.path)
        with reopened._connect() as db:
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
        self.assertIn("deepseek_tokens", columns)
        self.assertIn("runner_seconds", columns)

    def test_new_task_rows_have_null_usage(self) -> None:
        task_id = _add_task(self.store)
        task = self.store.get_task(task_id)
        self.assertIsNone(task.get("deepseek_tokens"))
        self.assertIsNone(task.get("runner_seconds"))

    def test_record_task_usage_absolute_overwrite_never_accumulates(self) -> None:
        task_id = _add_task(self.store)
        self.store.record_task_usage(task_id, deepseek_tokens=12000, runner_seconds=600.0)
        self.store.record_task_usage(task_id, deepseek_tokens=12000, runner_seconds=600.0)
        task = self.store.get_task(task_id)
        self.assertEqual(task["deepseek_tokens"], 12000)
        self.assertEqual(task["runner_seconds"], 600.0)
        # 重跑（重试）以最新一次为准：覆盖，不累加
        self.store.record_task_usage(task_id, deepseek_tokens=3000, runner_seconds=120.0)
        task = self.store.get_task(task_id)
        self.assertEqual(task["deepseek_tokens"], 3000)
        self.assertEqual(task["runner_seconds"], 120.0)

    def test_record_task_usage_none_fields_do_not_clobber(self) -> None:
        task_id = _add_task(self.store)
        self.store.record_task_usage(task_id, deepseek_tokens=500)
        self.store.record_task_usage(task_id, runner_seconds=30.0)
        task = self.store.get_task(task_id)
        self.assertEqual(task["deepseek_tokens"], 500)
        self.assertEqual(task["runner_seconds"], 30.0)

    def test_record_task_usage_ignores_blank_task_and_clamps_negatives(self) -> None:
        self.store.record_task_usage("", deepseek_tokens=10)
        # 不存在任务=静默无写；负值在存储层钳到 0
        self.store.record_task_usage("missing-task", deepseek_tokens=-5, runner_seconds=-1.0)
        self.assertIsNone(self.store.get_task("missing-task"))

    def test_usage_totals_since_only_counts_recorded_rows(self) -> None:
        recorded = _add_task(self.store)
        self.store.record_task_usage(recorded, deepseek_tokens=700, runner_seconds=90.0)
        # 历史行（NULL）不贡献合计
        _add_task(self.store, kind="subtitle")
        totals = self.store.usage_totals_since(0.0)
        self.assertEqual(totals["deepseek_tokens"], 700)
        self.assertEqual(totals["runner_seconds"], 90.0)
        future = time.time() + 10_000
        empty = self.store.usage_totals_since(future)
        self.assertEqual(empty["deepseek_tokens"], 0)
        self.assertEqual(empty["runner_seconds"], 0.0)


class TaskUsageLensPublicTaskTests(unittest.TestCase):
    """public_task：deepseek_tokens 加性透出（None=无记录）。"""

    def _task_view(self, store: TaskStore, **usage) -> dict:
        task, _ = store.add_task("summary", "course-1", "sub-1", {})
        task_id = str(task["task_id"])
        if usage:
            store.record_task_usage(task_id, **usage)
        store.update_task(task_id, state="completed")
        return public_task(store.get_task(task_id), store)

    def test_public_task_passes_recorded_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            view = self._task_view(store, deepseek_tokens=42000, runner_seconds=600.0)
            self.assertEqual(view["deepseek_tokens"], 42000)

    def test_public_task_returns_none_tokens_for_historical_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "state.db")
            view = self._task_view(store)
            self.assertIsNone(view["deepseek_tokens"])


class TaskUsageLensApplicationTests(unittest.TestCase):
    """应用层：导入漏斗接线 + task_usage_month + 余额快照闭集态。"""

    @staticmethod
    def _service(db_path: Path) -> CourseLensApplication:
        service = CourseLensApplication.__new__(CourseLensApplication)
        service.task_store = TaskStore(db_path)
        service.credentials = mock.Mock()
        service.credentials.has_secret.return_value = True
        service._lock = threading.RLock()
        service.catalog_repository = mock.Mock()
        service.learning_store = mock.Mock()
        service._request_search_refresh = mock.Mock()
        service._deepseek_api_key = ""
        service._deepseek_balance_cache = None
        return service

    def test_import_remote_summary_records_usage_for_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task_id = _add_task(service.task_store)
            result = {
                "input_hash": "hash-1",
                "metrics": {"elapsed_seconds": 120.0, "deepseek_tokens": 9000},
                "outputs": {"summary": {"markdown": "# 标题\n正文", "chapters": []}},
            }
            with mock.patch.object(service, "_export_summary_markdown"), \
                 mock.patch.object(service, "_run_assessment_radar"), \
                 mock.patch.object(service, "_rebuild_course_knowledge", return_value={"state": "skipped"}):
                service._import_remote_summary_result("course-1", "sub-1", result, task_id=task_id)
            task = service.task_store.get_task(task_id)
            self.assertEqual(task["deepseek_tokens"], 9000)
            self.assertEqual(task["runner_seconds"], 120.0)

    def test_import_remote_subtitle_records_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task_id = _add_task(service.task_store, kind="subtitle")
            result = {
                "metrics": {"elapsed_seconds": 60.0, "deepseek_tokens": 0},
                "outputs": {"subtitle": {
                    "srt": "1\n00:00:00,000 --> 00:00:01,000\n你好\n",
                    "vtt": "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n你好\n",
                    "segments": [{"start": 0.0, "end": 1.0, "text": "你好"}],
                }},
            }
            service.output_dir = Path(tmp)
            with mock.patch.object(service, "subtitle_segments", return_value={"segments": []}):
                service._import_remote_subtitle("course-1", "sub-1", result, task_id=task_id)
            task = service.task_store.get_task(task_id)
            # 字幕无 LLM 参与：tokens=0 是诚实值，elapsed 如实落库
            self.assertEqual(task["deepseek_tokens"], 0)
            self.assertEqual(task["runner_seconds"], 60.0)

    def test_automation_result_uses_three_arg_subtitle_import(self) -> None:
        # 相邻缺陷回归钉：automation 导入面绝不能再以 4 参调用旧签名
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            service._import_remote_subtitle = mock.Mock()
            result = {"input_hash": "hash", "metrics": {}, "outputs": {
                "cloud_catalog": {"course_id": "course-1", "lecture": {"sub_id": "sub-1"}},
                "subtitle": {"mode": "automatic"},
            }}
            service._import_automation_result(result)
            args, kwargs = service._import_remote_subtitle.call_args
            self.assertEqual(len(args), 3, "automation 导入必须走 3 参签名")
            self.assertEqual(kwargs.get("task_id", ""), "")

    def test_record_helper_tolerates_missing_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            # 无 metrics：零写入、零异常
            service._record_task_usage("whatever", {})
            service._record_task_usage("whatever", None)
            self.assertEqual(service.task_store.usage_totals_since(0.0)["deepseek_tokens"], 0)

    def test_task_usage_month_aggregates_current_month(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp) / "state.db")
            task_id = _add_task(service.task_store)
            service.task_store.record_task_usage(task_id, deepseek_tokens=24000, runner_seconds=900.0)
            usage = service.task_usage_month()
            self.assertEqual(usage["schema"], "courselens.task-usage-month.v1")
            self.assertEqual(usage["month"], time.strftime("%Y-%m"))
            self.assertEqual(usage["deepseek_tokens"], 24000)
            self.assertEqual(usage["runner_minutes"], 15.0)

    # -------------------------------------------------- 余额读数闭集态

    def _balance_service(self, key: str) -> CourseLensApplication:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = self._service(Path(tmp.name) / "balance.db")
        service._deepseek_api_key = key
        service.credentials.load_deepseek_key.side_effect = KeyError("no saved key")
        return service

    def test_balance_without_key_makes_no_network_call(self) -> None:
        service = self._balance_service("")
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("无 key 禁外联")):
            snapshot = service.deepseek_balance_snapshot()
        self.assertEqual(snapshot["state"], "no_key")
        self.assertEqual(snapshot["schema"], "courselens.deepseek-balance.v1")

    def test_balance_parses_official_shape_and_caches(self) -> None:
        service = self._balance_service("sk-test")
        payload = json.dumps({
            "is_available": True,
            "balance_infos": [{"currency": "CNY", "total_balance": "110.00"}],
        }).encode("utf-8")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload
        request_holder: dict = {}

        def fake_urlopen(request, timeout=None):  # noqa: ANN001
            request_holder["request"] = request
            request_holder["timeout"] = timeout
            return response

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            first = service.deepseek_balance_snapshot()
            second = service.deepseek_balance_snapshot()
        self.assertEqual(first, {
            "state": "ok", "is_available": True, "currency": "CNY", "total_balance": "110.00",
        })
        self.assertEqual(first, second, "TTL 内第二次读数必须走缓存")
        self.assertIn("Bearer sk-test", request_holder["request"].headers["Authorization"])
        self.assertTrue(
            str(request_holder["request"].full_url).startswith("https://api.deepseek.com/user/balance")
        )

    def test_balance_failure_is_closed_set_and_does_not_retry_within_ttl(self) -> None:
        service = self._balance_service("sk-test")
        calls = {"count": 0}

        def fake_urlopen(request, timeout=None):  # noqa: ANN001
            calls["count"] += 1
            raise OSError("network down")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            first = service.deepseek_balance_snapshot()
            second = service.deepseek_balance_snapshot()
        self.assertEqual(first["state"], "unavailable")
        self.assertEqual(second["state"], "unavailable")
        self.assertEqual(calls["count"], 1, "失败态短缓存：TTL 内不重试轰炸")

    def test_balance_reports_key_unavailable_honestly(self) -> None:
        service = self._balance_service("sk-test")
        payload = json.dumps({"is_available": False, "balance_infos": [
            {"currency": "CNY", "total_balance": "0.00"},
        ]}).encode("utf-8")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = payload
        with mock.patch("urllib.request.urlopen", return_value=response):
            snapshot = service.deepseek_balance_snapshot()
        self.assertEqual(snapshot["state"], "ok")
        self.assertFalse(snapshot["is_available"])
        self.assertEqual(snapshot["total_balance"], "0.00")

    def test_balance_malformed_payload_stays_unavailable(self) -> None:
        service = self._balance_service("sk-test")
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"not-json"
        with mock.patch("urllib.request.urlopen", return_value=response):
            snapshot = service.deepseek_balance_snapshot()
        self.assertEqual(snapshot["state"], "unavailable")

    # -------------------------------------------------- 路由接线源码钉

    def test_http_routes_wire_new_reads(self) -> None:
        import src.runtime.http_api as http_api_module

        source = Path(http_api_module.__file__).read_text(encoding="utf-8")
        self.assertIn('"task_usage_month": service.task_usage_month()', source)
        self.assertIn('route == "deepseek-balance"', source)
        self.assertIn("service.deepseek_balance_snapshot()", source)
        self.assertIn('"deepseek_tokens"', source)


if __name__ == "__main__":
    unittest.main()
