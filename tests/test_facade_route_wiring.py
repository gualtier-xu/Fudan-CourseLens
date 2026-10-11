from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.runtime.http_api import make_handler
from tests.http_services import http_services


ROOT = Path(__file__).resolve().parents[1]

# 夜批15 T1 真机三证 500 的三条路由（F3/F4/F5）+ 静态候选面的请求级回归钉。
# R4 D-1 口径升格：提及级命中不算覆盖，必须构造真实 /api/v3 请求断行为。


class _Source:
    """最小合成源：只暴露三钉所需方法（http_services 适配层取同名属性）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.learning_store = SimpleNamespace()
        self.search_index = SimpleNamespace()
        self.credentials = SimpleNamespace()
        self.catalog_repository = SimpleNamespace()
        self.task_store = SimpleNamespace()

    def study_telemetry_summary(self):
        self.calls.append("study_telemetry_summary")
        return {"schema": "courselens.study-telemetry-summary.v1", "windows": []}

    def campus_diagnostics(self):
        self.calls.append("campus_diagnostics")
        return {"schema": "courselens.campus-diagnostics.v1", "checks": []}

    def course_flashcards(self, course_id):
        self.calls.append(f"course_flashcards:{course_id}")
        return {
            "view": "course_flashcards",
            "course_id": str(course_id),
            "counts": {"total": 0, "due": 0, "new": 0, "learning": 0, "reviewed_today": 0},
            "cards": [],
        }

    def course_flashcard_review(self, *args, **kwargs):
        self.calls.append("course_flashcard_review")
        return {"state": "ok"}

    def course_term_candidate_action(self, *args, **kwargs):
        self.calls.append("course_term_candidate_action")
        return {"ok": True}

    def request_quality_judge(self, sub_id):
        self.calls.append(f"request_quality_judge:{sub_id}")
        return {"state": "queued"}

    def record_study_event(self, *args, **kwargs):
        self.calls.append("record_study_event")
        return {"accepted": True}

    def delete_study_events(self, *args, **kwargs):
        self.calls.append("delete_study_events")
        return {"deleted": 0}

    def authentication_snapshot(self):
        return {"state": "ready"}


class FacadeRouteWiringTests(unittest.TestCase):
    def test_the_three_proven_routes_answer_real_requests_not_500(self):
        """F3/F4/F5 曾因 service.learning.<缺失门面字段> 真机 500；本钉以真实
        HTTP 请求断 200 + envelope，任何一处回断即红。"""
        with tempfile.TemporaryDirectory() as tmp:
            source = _Source()
            service = http_services(source)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), make_handler(service, Path(tmp) / "frontend")
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"

                with urlopen(f"{base}/api/v3/analytics/study-summary") as response:
                    body = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(body["data"]["schema"], "courselens.study-telemetry-summary.v1")

                with urlopen(f"{base}/api/v3/campus-diagnostics") as response:
                    body = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(body["data"]["schema"], "courselens.campus-diagnostics.v1")

                with urlopen(
                    f"{base}/api/v3/course-review/flashcards?course_id=crs-x"
                ) as response:
                    body = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(body["data"]["view"], "course_flashcards")

                self.assertIn("study_telemetry_summary", source.calls)
                self.assertIn("campus_diagnostics", source.calls)
                self.assertIn("course_flashcards:crs-x", source.calls)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_static_candidate_action_routes_reach_their_methods(self):
        """R4 静态候选五面中的动作面抽钉：POST 形状可达（此前只在服务缝测过、
        路由对象缝裸奔的族——SimpleNamespace 适配器掩盖过门面缺失）。"""
        with tempfile.TemporaryDirectory() as tmp:
            source = _Source()
            service = http_services(source)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), make_handler(service, Path(tmp) / "frontend")
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"

                def post(route: str, payload: dict):
                    request = Request(
                        f"{base}/api/v3/{route}",
                        data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    try:
                        with urlopen(request) as response:
                            return response.status, json.loads(response.read())
                    except HTTPError as error:
                        return error.code, json.loads(error.read())

                status, _ = post(
                    "quality-judge", {"sub_id": "sub-1", "operation_id": "op-qj-00000001"}
                )
                self.assertIn(status, (200, 202, 400), "quality-judge 路由必须可达（非 500）")
                self.assertTrue(
                    any(call.startswith("request_quality_judge") for call in source.calls),
                    "quality-judge 必须真到达门面方法",
                )
                for code in (500, 502):
                    self.assertNotEqual(status, code, "路由层异常=接线断线回潮")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_remaining_action_faces_reject_input_without_500(self):
        """其余动作面的接线性钉：载荷不全时路由必须以 4xx 拒绝（校验器说话），
        绝不 5xx（AttributeError=门面断线回潮）。断线曾让 flashcard 评分/
        术语候选确认驳回/热点记录清除三族全灭。"""
        with tempfile.TemporaryDirectory() as tmp:
            source = _Source()
            service = http_services(source)
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), make_handler(service, Path(tmp) / "frontend")
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"

                def post(route: str, payload: dict):
                    request = Request(
                        f"{base}/api/v3/{route}",
                        data=json.dumps(payload).encode(),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    try:
                        with urlopen(request) as response:
                            return response.status
                    except HTTPError as error:
                        return error.code

                probes = [
                    ("course-review/actions", {"action": "review_flashcard"}),
                    ("course-review/actions", {"action": "confirm_term"}),
                    ("analytics/study-events", {"events": []}),
                    ("analytics/actions", {"action": "delete-study-events"}),
                ]
                for route, payload in probes:
                    status = post(route, payload)
                    self.assertLess(status, 500, f"{route} {payload.get('action')} 5xx=接线断线回潮")
                    self.assertGreaterEqual(status, 400, f"{route} 需以 4xx 拒绝空载荷")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
