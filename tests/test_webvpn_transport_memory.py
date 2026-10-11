"""WebVPN 登录 last-success 候选记忆跨重启持久化（LOGIN-WARMUP-MEMORY-1）。

覆盖：持久化 round-trip、闭集校验拒绝坏值、代际不匹配惰性弃用、
hydrate 仅空值生效、落盘失败绝不抛出，以及「新一代进程装配后首登
直接从已知好候选开始」的端到端行为。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from path_utils import PROJECT_ROOT
import requests
from src import application as application_module
from src.application import CourseLensApplication
from src.runtime.network import NetworkSettings
from src.runtime.task_store import TaskStore

_MEMORY_KEY = "webvpn_ticket_memory"
_PROXY = "http://127.0.0.1:6268"


def _reset_memory() -> None:
    application_module._LAST_TICKET_SUCCESS.update(route=None, transport=None)


class TicketMemoryStoreTests(unittest.TestCase):
    """NetworkSettings 侧：闭集读写 + 代际惰性失效。"""

    def setUp(self):
        cache = PROJECT_ROOT / "runtime" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache)
        self.store = TaskStore(Path(self.temp.name) / "state.db")
        self.network = NetworkSettings(self.store)
        _reset_memory()

    def tearDown(self):
        _reset_memory()
        self.temp.cleanup()

    def test_save_then_load_round_trips_closed_categories(self):
        self.network.save_ticket_memory("proxy", "curl_h1")
        self.assertEqual(
            self.network.load_ticket_memory(),
            {"route": "proxy", "transport": "curl_h1"},
        )
        self.network.save_ticket_memory("direct", "curl_h2")
        self.assertEqual(
            self.network.load_ticket_memory(),
            {"route": "direct", "transport": "curl_h2"},
        )

    def test_saved_payload_is_closed_set_categories_only(self):
        self.network.save_ticket_memory("proxy", "curl_h2")
        saved = self.store.get_app_state(_MEMORY_KEY, {})
        self.assertEqual(
            set(saved), {"route", "transport", "generation", "saved_at"}
        )
        self.assertEqual(saved["route"], "proxy")
        self.assertEqual(saved["transport"], "curl_h2")
        self.assertEqual(saved["generation"], 0)
        self.assertIsInstance(saved["saved_at"], float)

    def test_out_of_closed_set_values_are_rejected_on_load_and_save(self):
        for bad in (
            {"route": "turbo", "transport": "curl_h2", "generation": 0},
            {"route": "direct", "transport": "requests", "generation": 0},
            {"route": None, "transport": "curl_h2", "generation": 0},
            {"route": "direct", "transport": "curl_h2"},  # 缺代际
            "not-a-dict",
        ):
            self.store.set_app_state(_MEMORY_KEY, bad)
            self.assertIsNone(self.network.load_ticket_memory())
        with self.assertRaises(ValueError):
            self.network.save_ticket_memory("turbo", "curl_h2")
        with self.assertRaises(ValueError):
            self.network.save_ticket_memory("direct", "requests")

    def test_generation_mismatch_deprecates_memory_lazily(self):
        self.network.save_ticket_memory("proxy", "curl_h1")
        self.assertIsNotNone(self.network.load_ticket_memory())
        # 路由代际事件（设置变更）推进后，旧记忆惰性弃用且不写回、不清理。
        self.network.update("direct")
        self.assertIsNone(self.network.load_ticket_memory())
        self.assertIsNotNone(self.store.get_app_state(_MEMORY_KEY, None))

    def test_reopening_store_keeps_memory_within_same_generation(self):
        self.network.save_ticket_memory("proxy", "curl_h1")
        reopened = TaskStore(Path(self.temp.name) / "state.db")
        self.assertEqual(
            NetworkSettings(reopened).load_ticket_memory(),
            {"route": "proxy", "transport": "curl_h1"},
        )


class TicketMemoryAppTests(unittest.TestCase):
    """应用侧：hydrate 仅空值生效、落盘尽力而为、新进程首登用已知好候选。"""

    def setUp(self):
        cache = PROJECT_ROOT / "runtime" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=cache)
        self.service = CourseLensApplication(Path(self.temp.name))
        _reset_memory()

    def tearDown(self):
        self.service.close()
        _reset_memory()
        self.temp.cleanup()

    def _login_once(self, app, outcomes, routes):
        """Run one real `_login_with_retry` against a stubbed WebVPNSession."""
        test = self
        ctor_kwargs: list[dict] = []

        class _RecordingVpn:
            def __init__(self, **kwargs):
                ctor_kwargs.append(dict(kwargs))
                self.session = Mock()
                self._transport_class = kwargs.get("transport")

            def login(self, **_kwargs):
                outcome = outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome

            def authenticate_icourse(self, **_kwargs):
                outcome = outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome

            def close(self):
                pass

        app.set_credentials("student", "password")
        with (
            patch("src.api.webvpn.WebVPNSession", _RecordingVpn),
            patch.object(
                app.network, "service_proxies", return_value=list(routes)
            ),
            patch("time.sleep"),
        ):
            test.assertIsNotNone(app._login_with_retry(max_attempts=3))
        return ctor_kwargs

    def test_hydrate_fills_empty_memory_from_persisted_candidate(self):
        self.service.network.save_ticket_memory("proxy", "curl_h1")
        _reset_memory()
        self.service._hydrate_ticket_memory()
        self.assertEqual(
            application_module._LAST_TICKET_SUCCESS,
            {"route": "proxy", "transport": "curl_h1"},
        )

    def test_hydrate_never_overrides_in_process_memory(self):
        self.service.network.save_ticket_memory("proxy", "curl_h1")
        application_module._LAST_TICKET_SUCCESS.update(
            route="direct", transport="curl_h2"
        )
        self.service._hydrate_ticket_memory()
        self.assertEqual(
            application_module._LAST_TICKET_SUCCESS,
            {"route": "direct", "transport": "curl_h2"},
        )

    def test_hydrate_ignores_stale_generation_and_bad_values(self):
        self.service.network.save_ticket_memory("proxy", "curl_h1")
        self.service.network.update("direct")  # 代际推进 → 持久记忆失效
        self.service.task_store.set_app_state(
            _MEMORY_KEY,
            {"route": "turbo", "transport": "curl_h2", "generation": 99},
        )
        _reset_memory()
        self.service._hydrate_ticket_memory()
        self.assertEqual(
            application_module._LAST_TICKET_SUCCESS,
            {"route": None, "transport": None},
        )

    def test_hydrate_silently_ignores_load_failures(self):
        with patch.object(
            NetworkSettings,
            "load_ticket_memory",
            side_effect=RuntimeError("boom"),
        ):
            self.service._hydrate_ticket_memory()
        self.assertEqual(
            application_module._LAST_TICKET_SUCCESS,
            {"route": None, "transport": None},
        )

    def test_login_success_persists_memory(self):
        ctor = self._login_once(self.service, [None, None], ["", _PROXY])
        self.assertEqual(ctor[0]["proxy_url"], "")
        self.assertEqual(
            application_module._LAST_TICKET_SUCCESS,
            {"route": "direct", "transport": "curl_h2"},
        )
        self.assertEqual(
            self.service.network.load_ticket_memory(),
            {"route": "direct", "transport": "curl_h2"},
        )

    def test_persist_failure_does_not_break_login(self):
        with patch.object(
            NetworkSettings,
            "save_ticket_memory",
            side_effect=RuntimeError("disk full"),
        ):
            ctor = self._login_once(self.service, [None, None], ["", _PROXY])
        self.assertIsNotNone(ctor)
        self.assertEqual(
            application_module._LAST_TICKET_SUCCESS,
            {"route": "direct", "transport": "curl_h2"},
        )
        self.assertIsNone(self.service.network.load_ticket_memory())

    def test_checkpoint_restore_reuses_the_same_hydrate(self):
        calls = []

        class _RestoreVpn:
            def __init__(self, **kwargs):
                self._transport_class = kwargs.get("transport")

            def restore_session_checkpoint(self, cookies):
                return False

            def close(self):
                pass

        self.service.set_credentials("student", "password")
        with (
            patch.object(
                self.service,
                "_hydrate_ticket_memory",
                side_effect=lambda: calls.append(1),
            ),
            patch("src.api.webvpn.WebVPNSession", _RestoreVpn),
            patch.object(
                self.service.credentials,
                "load_session_checkpoint",
                return_value={"k": "v"},
            ),
        ):
            self.service._restore_client_from_checkpoint()
        self.assertEqual(calls, [1])

    def test_restart_first_login_starts_from_persisted_candidate(self):
        # 第一代进程：直连传输失败一次 → 一次换路预算内代理路径成功 → 落盘。
        ctor = self._login_once(
            self.service,
            [requests.ConnectionError("direct down"), None, None],
            ["", _PROXY],
        )
        self.assertEqual(ctor[0]["proxy_url"], "")
        self.assertEqual(ctor[1]["proxy_url"], _PROXY)
        self.assertEqual(
            self.service.network.load_ticket_memory(),
            {"route": "proxy", "transport": "curl_h2"},
        )
        self.service.close()
        # 新一代进程：进程内记忆为空，装配 hydrate 后首登候选代理优先。
        _reset_memory()
        successor = CourseLensApplication(Path(self.temp.name))
        try:
            self.assertEqual(
                application_module._LAST_TICKET_SUCCESS,
                {"route": "proxy", "transport": "curl_h2"},
            )
            ctor2 = self._login_once(successor, [None, None], ["", _PROXY])
            self.assertEqual(ctor2[0]["proxy_url"], _PROXY)
            self.assertEqual(ctor2[0]["transport"], "curl_h2")
        finally:
            successor.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
