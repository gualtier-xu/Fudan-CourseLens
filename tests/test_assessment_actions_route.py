"""WIRING-FIX-1（BROWSERWALK-3 F3-P1-3）：assessment/actions 路由 HTTP 面。

处理器此前误挂 GET 分发器：前端唯一调用方式 POST 恒落尾 404
{"error": "v3 route not found"}，考核确认/忽略两钮端到端死；GET 打进来
则因 GET 分发器无 body 直接 NameError。分支挪入 POST 分发器后，此处用
真 HTTP 往返钉住：POST 动作族全通（本地台账面、无课程会话门）、闭集
错误族原样、GET 不再受理（防回归挪错）。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.runtime.assessment_radar import upsert_events
from src.runtime.http_api import make_handler
from src.runtime.learning_schema import ensure_assessment_schema, initialize_learning_schema
from tests.http_services import http_services


class _AssessmentApplication:
    """assessment/actions 只触 learning.repository（本地台账面，无会话门）。"""

    def __init__(self, root: Path):
        self.path = root / "learning.db"
        with closing(sqlite3.connect(self.path)) as db, db:
            initialize_learning_schema(db)
        ensure_assessment_schema(self.path)
        self.learning_store = type("_Store", (), {"path": self.path})()

    def start_search_index(self) -> None:
        return None

    def authentication_snapshot(self) -> dict:
        return {"state": "ready"}

    def seed_event(self) -> str:
        counts = upsert_events(self.learning_store, [{
            "course_id": "c1", "category": "assignment", "title": "作业",
            "title_norm": "作业#1", "due_at": "", "location": "",
            "source": "rule", "quote": "下周一交作业", "first_seen_sub_id": "s1",
        }])
        assert counts["inserted"] == 1
        from src.runtime.assessment_radar import assessment_events_scan

        payload = assessment_events_scan(self.learning_store, None, course_id="c1")
        assert len(payload["events"]) == 1
        return str(payload["events"][0]["event_id"])


class AssessmentActionsRouteTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = _AssessmentApplication(Path(tmp.name))
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(http_services(self.service), Path(tmp.name)),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _post(self, body):
        request = Request(
            f"{self.base}/api/v3/assessment/actions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def _get(self):
        request = Request(f"{self.base}/api/v3/assessment/actions", method="GET")
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def test_confirm_action_round_trip_over_real_http(self):
        event_id = self.service.seed_event()
        status, payload = self._post({"action": "confirm", "event_id": event_id})
        self.assertEqual(status, 200)
        value = payload["data"]
        self.assertEqual(value["status"], "confirmed")
        self.assertEqual(value["event_id"], event_id)

    def test_dismiss_action_round_trip_over_real_http(self):
        event_id = self.service.seed_event()
        status, payload = self._post({"action": "dismiss", "event_id": event_id})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["status"], "dismissed")

    def test_unknown_event_returns_closed_404(self):
        status, payload = self._post({"action": "confirm", "event_id": "no-such-event"})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error_code"], "assessment_event_missing")

    def test_invalid_action_returns_closed_400(self):
        event_id = self.service.seed_event()
        status, payload = self._post({"action": "delete", "event_id": event_id})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "assessment_action_invalid")

    def test_get_is_no_longer_routed(self):
        # 防回归挪错：分支若被挪回 GET 分发器，此处从 404 变回 NameError 500。
        status, payload = self._get()
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "v3 route not found")


class FreshInstallAssessmentTests(unittest.TestCase):
    """night14-R2 Fix-D：fresh 安装 boot 即建 assessment_events 表。

    旧链：该表只在首份总结工件钩子与 client-reset 建——fresh 壳首次进播放
    桌 GET /api/v3/assessment 撞 no such table 500（R2 真机 fresh install +
    进程内双实证，每桌必发 1-2 次）。boot 五连 ensure 补齐后启动立即可查；
    且 assessment_events_scan 对表缺席按「无台账」防御回空（纵深守卫，
    雷达特性永不 500）。"""

    def setUp(self) -> None:
        from path_utils import PROJECT_ROOT

        scratch = PROJECT_ROOT / "runtime" / "cache"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_boot_ensures_assessment_events_table(self) -> None:
        from src.application import CourseLensApplication

        app = CourseLensApplication(self.root)
        try:
            with closing(sqlite3.connect(app.learning_store.path)) as db:
                names = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
        finally:
            app.close()
        self.assertIn("assessment_events", names, "boot 后表立即可查（无需先导总结）")

    def test_scan_degrades_to_empty_on_missing_table(self) -> None:
        from src.runtime.assessment_radar import assessment_events_scan

        bare = self.root / "bare-learning.db"
        store = type("_Store", (), {"path": bare})()
        snapshot = assessment_events_scan(store, None)
        self.assertEqual(snapshot.get("events"), [], "表缺席=无台账优雅降级，绝不 500")


if __name__ == "__main__":
    unittest.main()
