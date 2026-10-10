"""SRC-CLEANUP-1 U5: greeting 今日结束钟点 60s 记忆化——行为钉+稳态性能门。

全 store 扫描+整学期展开每次 greeting 请求重算代价高（合成基准 ~20ms、
生产行累积下 175-264ms）；记忆化后稳态为缓存命中（<20ms 门）。
"""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from src.runtime.http_api import (
    _GREETING_ENDS_CACHE,
    _GREETING_ENDS_CACHE_TTL_SECONDS,
    _greeting_today_meeting_ends,
)
from src.runtime.timetable import SHANGHAI


def _fake_service(counter: dict[str, int], *, with_courses: bool = True):
    today = datetime.now(SHANGHAI).date().isoformat()
    courses = [{
        "timetable_course_id": f"c{j}", "title": f"course{j}", "source": "webvpn",
        "semester_id": "S1", "semester_start_date": today,
        "week_indexes": [1, 2],
        "meetings": [{"weekday": 1, "start_unit": 1, "end_unit": 2}],
        "catalog_link": {"course_id": f"cat{j}"}, "teachers": [], "room": "", "course_code": "",
    } for j in range(4)]

    class FakeStore:
        def preferences(self, identity):
            return {"selected_semester_id": "S1"}

        def list(self, identity, semester=None):
            counter["list_calls"] += 1
            if semester is None:
                return [{
                    "semester_id": "S1", "observed_at": time.time(),
                    "payload": {"semesters": [
                        {"semester_id": "S1", "is_default": True, "start_date": today}
                    ]},
                }]
            if not with_courses:
                return [{"semester_id": semester, "observed_at": time.time(),
                         "payload": {"courses": []}}]
            return [{"semester_id": semester, "observed_at": time.time(),
                     "payload": {"courses": courses}}]

    class FakeService:
        pass

    service = FakeService()
    service.auth_catalog = type("C", (), {"identity_scope": lambda self: "ident-1"})()
    service.timetable = type("T", (), {"runtime": type("R", (), {"store": FakeStore()})()})()
    return service


class GreetingEndsCacheTests(unittest.TestCase):
    def setUp(self):
        _GREETING_ENDS_CACHE.clear()

    def test_repeated_calls_hit_cache_and_compute_once(self):
        counter = {"list_calls": 0}
        service = _fake_service(counter)
        first = _greeting_today_meeting_ends(service)
        second = _greeting_today_meeting_ends(service)
        self.assertTrue(first)
        self.assertEqual(first, second)
        self.assertEqual(counter["list_calls"], 2)  # 单次计算：scan+selected 各一次
        self.assertEqual(len(_GREETING_ENDS_CACHE), 1)

    def test_date_key_rollover_recomputes(self):
        counter = {"list_calls": 0}
        service = _fake_service(counter)
        self.assertTrue(_greeting_today_meeting_ends(service))
        yesterday = (datetime.now(SHANGHAI) - timedelta(days=1)).date().isoformat()
        identity = "ident-1"
        date_key, stamp, ends = _GREETING_ENDS_CACHE[identity]
        _GREETING_ENDS_CACHE[identity] = (yesterday, stamp, ends)
        _greeting_today_meeting_ends(service)
        self.assertEqual(_GREETING_ENDS_CACHE[identity][0], datetime.now(SHANGHAI).date().isoformat())
        self.assertEqual(counter["list_calls"], 4)  # 跨天重新计算

    def test_ttl_expiry_recomputes(self):
        counter = {"list_calls": 0}
        service = _fake_service(counter)
        self.assertTrue(_greeting_today_meeting_ends(service))
        identity = "ident-1"
        date_key, _stamp, ends = _GREETING_ENDS_CACHE[identity]
        _GREETING_ENDS_CACHE[identity] = (
            date_key, time.monotonic() - _GREETING_ENDS_CACHE_TTL_SECONDS - 1.0, ends
        )
        _greeting_today_meeting_ends(service)
        self.assertEqual(counter["list_calls"], 4)

    def test_none_result_is_cached_too(self):
        counter = {"list_calls": 0}
        service = _fake_service(counter, with_courses=False)
        self.assertIsNone(_greeting_today_meeting_ends(service))
        self.assertIsNone(_greeting_today_meeting_ends(service))
        self.assertEqual(counter["list_calls"], 2)

    def test_warm_call_meets_20ms_budget(self):
        counter = {"list_calls": 0}
        service = _fake_service(counter)
        _greeting_today_meeting_ends(service)
        started = time.perf_counter()
        for _ in range(50):
            _greeting_today_meeting_ends(service)
        warm_ms = (time.perf_counter() - started) * 1000 / 50
        self.assertLess(warm_ms, 20.0)


if __name__ == "__main__":
    unittest.main()
