"""API-layer tests for the read-only data-map route (DATAMAP-P1).

Covers the frozen ``courselens.data-map.v1`` contract surface: closed
three-domain set, the named outbound-host closed set cross-checked host by
host against the real code constants (SERVICE_PROBES, WebVPN redirect/target
hosts, distribution allowed_hosts), local counters aggregation with honest
degradation beyond the page cap, and the credential/course-identifier
exclusion guarantee.  All fixtures are synthetic; zero network.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

from src.runtime.data_map import (
    COUNTERS_PAGE_CAP,
    DATA_MAP_ACTIONS,
    DATA_MAP_DOMAINS,
    DATA_MAP_SCHEMA,
    OUTBOUND_HOSTS,
    local_counters,
)
from src.runtime.http_api import make_handler
from src.runtime.learning_store import LearningStore
from src.runtime.catalog_repository import CatalogRepository
from src.runtime.student_features import ensure_student_feature_schema
from src.runtime.task_store import TaskStore

# README「客户端会和哪些服务器通信」表的产品内闭集：恰好 10 主机，逐个具名。
EXPECTED_HOSTS = frozenset({
    "id.fudan.edu.cn",
    "webvpn.fudan.edu.cn",
    "icourse.fudan.edu.cn",
    "fdjwgl.fudan.edu.cn",
    "yjsxktest.fudan.sh.cn",
    "github.com",
    "api.github.com",
    "api.deepseek.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
})

_FORBIDDEN_KEY_PARTS = (
    "token", "password", "secret", "cookie", "credential", "api_key",
    "session", "course_id", "course_title", "student", "account",
)


class _DataService:
    """Narrow double exposing exactly the container paths the route touches."""

    def __init__(self, root: Path):
        self.root = root
        self.catalog_repository = CatalogRepository(root / "state.db")
        self.task_store = TaskStore(root / "state.db")
        self.learning_store = LearningStore(root / "learning.db")
        ensure_student_feature_schema(root / "learning.db")
        self.learning = SimpleNamespace(repository=self.learning_store)
        self._auth_state = "idle"
        self.auth_catalog = SimpleNamespace(
            catalog=self.catalog_repository,
            authentication_snapshot=lambda: {"state": self._auth_state},
        )
        self.tasks = SimpleNamespace(repository=self.task_store)

    def set_auth_state(self, state: str) -> None:
        self._auth_state = state

    def seed_course(self, course_id: str = "1", title: str = "线性代数") -> None:
        self.catalog_repository.upsert_course(course_id, title, teacher="张老师")
        self.catalog_repository.upsert_lecture(course_id, {
            "sub_id": f"{course_id}-a", "sub_title": "第一讲", "date": "2026-09-01",
        })

    def seed_learning_rows(self, course_id: str, sub_id: str) -> None:
        with closing(sqlite3.connect(self.learning_store.path)) as db, db:
            db.execute(
                "INSERT INTO watch_progress(sub_id,course_id,position_ms,duration_ms,completed,"
                "playback_rate,updated_at) VALUES(?,?,?,?,?,?,?)",
                (sub_id, course_id, 1000, 2000, 0, 1.0, time.time()),
            )
            db.execute(
                "INSERT INTO bookmarks(bookmark_id,course_id,sub_id,start_ms,end_ms,note,status,"
                "explanation_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (f"bm-{sub_id}", course_id, sub_id, 0, 10, "", "open", "{}", time.time(), time.time()),
            )


class _DataMapApiServer(ThreadingHTTPServer):
    """Join handler threads before the temp tree disappears (course-data 同款)."""

    daemon_threads = False


class DataMapApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.service = _DataService(root)
        self.root = root
        server = _DataMapApiServer(("127.0.0.1", 0), make_handler(self.service, root))
        self._server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{server.server_port}"

    def tearDown(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._tmp.cleanup()

    # -- helpers -------------------------------------------------------------

    def get_data_map(self):
        with urlopen(f"{self.base}/api/v3/data-map") as response:
            return response.status, json.loads(response.read())["data"]

    def flatten(self, value, path: str = "") -> list[tuple[str, object]]:
        rows: list[tuple[str, object]] = []
        if isinstance(value, dict):
            for key, item in value.items():
                child = f"{path}.{key}" if path else str(key)
                rows.append((child, item))
                rows.extend(self.flatten(item, child))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                child = f"{path}[{index}]"
                rows.append((child, item))
                rows.extend(self.flatten(item, child))
        return rows

    # -- frozen shape ---------------------------------------------------------

    def test_data_map_returns_frozen_shape(self):
        status, payload = self.get_data_map()
        self.assertEqual(status, 200)
        self.assertEqual(payload["schema"], DATA_MAP_SCHEMA)
        self.assertEqual(
            [domain["key"] for domain in payload["domains"]],
            list(DATA_MAP_DOMAINS),
            "three domains must render in the frozen order",
        )
        for domain in payload["domains"]:
            self.assertEqual(
                set(domain), {"key", "title", "where", "who", "retention", "items"},
            )
            self.assertTrue(domain["title"] and domain["where"] and domain["who"])
            self.assertTrue(domain["retention"])
            self.assertTrue(domain["items"])
        self.assertEqual(
            [host["host"] for host in payload["outbound_hosts"]],
            [entry["host"] for entry in OUTBOUND_HOSTS],
        )
        self.assertEqual(
            {action["key"] for action in payload["actions"]}, set(DATA_MAP_ACTIONS),
        )
        self.assertEqual(payload["retention"]["materials_days"], 30)
        self.assertEqual(payload["retention"]["state_days"], 90)

    def test_outbound_hosts_closed_set_matches_code_constants(self):
        """产品内具名清单必须逐主机对得上代码里的真实常量（审计口径产品化）。"""
        from src.api.webvpn import WebVPNSession
        from src.distribution import DEFAULT_REGISTRY
        from src.runtime.network import SERVICE_PROBES

        self.assertEqual(len(OUTBOUND_HOSTS), 10, "closed set is exactly 10 hosts")
        hosts = {entry["host"] for entry in OUTBOUND_HOSTS}
        self.assertEqual(hosts, EXPECTED_HOSTS)
        self.assertEqual(len(hosts), 10, "no duplicate host entries")
        # 探针常量逐一对账：SERVICE_PROBES 的每个目标主机必须在闭集内。
        from urllib.parse import urlparse

        for service, url in SERVICE_PROBES.items():
            self.assertIn(
                urlparse(url).hostname, hosts,
                f"SERVICE_PROBES[{service}] target must be declared in the data map",
            )
        # WebVPN 票据重定向与目标白名单逐一对账。
        for host in WebVPNSession.TICKET_REDIRECT_HOSTS | WebVPNSession.ALLOWED_TARGET_HOSTS:
            self.assertIn(host, hosts, f"webvpn allowed host {host} must be declared")
        # 更新链 allowed_hosts 逐一对账。
        for host in DEFAULT_REGISTRY["allowed_hosts"]:
            self.assertIn(host, hosts, f"distribution allowed host {host} must be declared")
        for entry in OUTBOUND_HOSTS:
            self.assertEqual(set(entry), {"host", "group", "purpose"})
            self.assertTrue(entry["purpose"], "every host carries a one-line purpose")

    def test_no_course_session_gate_diagnostic_read(self):
        """诊断类读面（同 campus-diagnostics）：未登录也如实呈现本机地图。"""
        self.service.set_auth_state("idle")
        status, payload = self.get_data_map()
        self.assertEqual(status, 200)
        self.assertEqual(payload["schema"], DATA_MAP_SCHEMA)
        self.service.set_auth_state("ready")
        status, _ = self.get_data_map()
        self.assertEqual(status, 200)

    def test_counters_aggregate_local_categories_without_course_ids(self):
        self.service.seed_course("1", "高等数学")
        self.service.seed_learning_rows("1", "1-a")
        status, payload = self.get_data_map()
        self.assertEqual(status, 200)
        counters = payload["counters"]
        self.assertEqual(counters["complete"], True)
        categories = {row["category"]: row["count"] for row in counters["categories"]}
        self.assertGreaterEqual(categories.get("progress", 0), 1)
        self.assertGreaterEqual(categories.get("bookmarks", 0), 1)
        self.assertGreater(counters["database_bytes"]["total"], 0)

    def test_payload_never_carries_credentials_or_course_identifiers(self):
        self.service.seed_course("1", "高等数学")
        self.service.seed_learning_rows("1", "1-a")
        _, payload = self.get_data_map()
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("高等数学", text, "course titles must never surface")
        for key_path, _ in self.flatten(payload):
            lowered = key_path.lower()
            for part in _FORBIDDEN_KEY_PARTS:
                self.assertNotIn(
                    part, lowered,
                    f"payload key {key_path} must not carry sensitive/identifier names",
                )

    def test_empty_store_reports_honest_zero_state(self):
        _, payload = self.get_data_map()
        counters = payload["counters"]
        self.assertEqual(counters["complete"], True)
        self.assertEqual(counters["categories"], [], "empty store stays empty, not fabricated")
        self.assertGreaterEqual(counters["database_bytes"]["total"], 0)


class _StubInventory:
    """Direct unit double for local_counters pagination honesty."""

    def __init__(self, total: int, page_rows: int):
        self.total = total
        self.page_rows = page_rows
        self.calls: list[dict] = []

    def summary(self, *, page: int, page_size: int, include_orphans: bool):
        self.calls.append({"page": page, "page_size": page_size, "include_orphans": include_orphans})
        start = (page - 1) * page_size
        rows = [
            {"categories": {"progress": {"count": 1, "text_bytes": 10}}}
            for _ in range(min(page_size, max(0, self.total - start)))
        ]
        return {
            "page": {"page": page, "page_size": page_size, "total": self.total},
            "database_bytes": {"state_db": 1, "learning_db": 1, "total": 2},
            "rows": rows,
            "unattributed": {"categories": {"tasks": {"count": 2, "text_bytes": 4}}},
        }


class LocalCountersTests(unittest.TestCase):
    def test_single_page_within_cap_is_complete(self):
        stub = _StubInventory(total=7, page_rows=7)
        counters = local_counters(stub)
        self.assertEqual(counters["complete"], True)
        self.assertEqual(len(stub.calls), 1, "one read-only pass, no page looping")
        self.assertEqual(stub.calls[0]["page_size"], COUNTERS_PAGE_CAP)
        self.assertEqual(stub.calls[0]["include_orphans"], True)
        by_category = {row["category"]: row["count"] for row in counters["categories"]}
        self.assertEqual(by_category["progress"], 7)
        self.assertEqual(by_category["tasks"], 2, "unattributed bucket joins the totals")
        self.assertEqual(counters["database_bytes"]["total"], 2)

    def test_rows_beyond_cap_degrade_honestly(self):
        stub = _StubInventory(total=COUNTERS_PAGE_CAP + 5, page_rows=COUNTERS_PAGE_CAP)
        counters = local_counters(stub)
        self.assertEqual(
            counters["complete"], False,
            "beyond the frozen page cap the map must say so, never fabricate",
        )
        by_category = {row["category"]: row["count"] for row in counters["categories"]}
        self.assertEqual(by_category["progress"], COUNTERS_PAGE_CAP)
        self.assertEqual(by_category["tasks"], 2)


if __name__ == "__main__":
    unittest.main()
