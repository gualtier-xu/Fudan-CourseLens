from __future__ import annotations

import json
import base64
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.runtime.http_api import make_handler
from tests.http_services import http_services
from src.runtime.learning_store import LearningStore
from src.runtime.student_features import (
    clear_watch_events,
    ensure_student_feature_schema,
    insert_watch_events,
    list_watch_events,
)
from src.runtime.document_alignment import (
    align_document,
    delete_document,
    ensure_document_schema,
    get_document,
    list_documents,
    register_document,
    update_alignment,
)
from src.runtime.smart_playback import classify_segments, list_timeline, save_timeline, timeline_input_hash


class _Service:
    class _Manifest:
        @staticmethod
        def courses():
            return [{"course_id": "1", "title": "Course", "teacher": "Teacher", "term": "2026"}]
    class _Tasks:
        def __init__(self): self.values = {"daily_schedule": {"enabled": False}}
        def get_feature_flags(self): return {}
        def set_feature_flag(self, _name, _value): return True
        def list_tasks(self, **_kwargs): return []
        def get_app_state(self, key, default=None): return self.values.get(key, default)
        def set_app_state(self, key, value): self.values[key] = value
    def __init__(self, root):
        self.catalog_repository = self._Manifest(); self.task_store = self._Tasks()
        self.root = root
        self.learning_store = LearningStore(root / "learning.db")
        ensure_student_feature_schema(root / "learning.db")
        ensure_document_schema(root / "learning.db")
        self.bookmark = {
            "bookmark_id": "bookmark-1", "course_id": "1", "sub_id": "2",
            "resolution_status": "open", "explanation_state": "idle",
        }
    def list_question_bookmarks(self, _sub_id=""): return [dict(self.bookmark)]
    def explain_question_bookmark(self, _bookmark_id, retry=False):
        self.bookmark["explanation_state"] = "queued"
        return {
            "bookmark": dict(self.bookmark),
            "task": {"task_id": "question-1", "kind": "question", "course_id": "1", "sub_id": "2", "state": "queued"},
            "created": not retry,
        }
    def set_question_bookmark_resolution(self, _bookmark_id, resolved=False):
        self.bookmark["resolution_status"] = "resolved" if resolved else "open"
        return dict(self.bookmark)
    def delete_question_bookmark(self, bookmark_id):
        if str(bookmark_id) != self.bookmark["bookmark_id"]:
            raise KeyError("bookmark not found")
        self.bookmark["deleted"] = True
        return {"bookmark_id": str(bookmark_id), "deleted": True}
    def create_question_bookmark(self, course_id, sub_id, start_ms, end_ms, note=""):
        # RR-BOOKMARK-1：桩讲次 "contentless" 模拟零依据讲次——真实闸在
        # CourseLensApplication.create_question_bookmark（钉见
        # tests/test_bookmark_creation_gate.py），此处只供路由合同测试。
        if str(sub_id) == "contentless":
            raise ValueError("bookmark_evidence_unavailable")
        return {
            "bookmark_id": f"bookmark-{sub_id}-{start_ms}", "course_id": str(course_id),
            "sub_id": str(sub_id), "start_ms": int(start_ms), "end_ms": int(end_ms),
            "note": str(note), "resolution_status": "open", "explanation_state": "idle",
        }
    def cancel_question_explanation(self, _bookmark_id):
        self.bookmark["explanation_state"] = "canceling"
        return {"bookmark": dict(self.bookmark), "task": {"task_id": "question-1", "kind": "question", "state": "pausing"}}
    def cross_course_concepts(self, _course_id=""):
        return {"concepts": [{"concept_id": "c1", "name": "梯度下降"}], "edges": [], "courses": []}
    def analyze_cross_course_concepts(self, course_ids):
        return {"status": "completed", "course_count": len(course_ids), "concept_count": 1, "edge_count": 1}
    def update_cross_course_concept(self, edge_id, **values):
        return {"edge_id": edge_id, "status": "active", **values}
    def learning_analytics(self, _term=""):
        return {"settings": {"enabled": False}, "summary": None, "reason": "analytics_disabled"}
    def configure_learning_analytics(self, enabled, **_values):
        return {"enabled": bool(enabled), "timezone": "Asia/Shanghai"}
    def delete_learning_analytics(self, term):
        return {"deleted_events": 2, "term": term, "settings": {"enabled": True}}
    def subtitle_segments(self, _sub_id):
        return {"segments": [
            {"start_ms": 0, "end_ms": 15000, "text": "矩阵特征值和特征向量是本节重点"},
            {"start_ms": 60000, "end_ms": 75000, "text": "概率分布、期望和方差是考试重点"},
        ]}
    def import_learning_document(self, **kwargs):
        value = register_document(self.learning_store.path, self.root, **kwargs)
        align_document(self.learning_store.path, value["document_id"], self.subtitle_segments("")["segments"])
        return get_document(self.learning_store.path, value["document_id"])
    def list_learning_documents(self, course_id="", sub_id=""):
        return list_documents(self.learning_store.path, course_id=course_id, sub_id=sub_id)
    def learning_document(self, document_id): return get_document(self.learning_store.path, document_id)
    def align_learning_document(self, document_id):
        return align_document(self.learning_store.path, document_id, self.subtitle_segments("")["segments"])
    def update_document_alignment(self, alignment_id, **kwargs):
        return update_alignment(self.learning_store.path, alignment_id=alignment_id, **kwargs)
    def delete_learning_document(self, document_id):
        delete_document(self.learning_store.path, document_id)
        return True
    def smart_timeline(self, sub_id):
        transcript = self.subtitle_segments(sub_id)["segments"]
        return list_timeline(
            self.learning_store.path, sub_id=sub_id,
            current_input_hash=timeline_input_hash(transcript),
        )
    def classify_smart_timeline(self, course_id, sub_id):
        transcript = self.subtitle_segments(sub_id)["segments"]
        return save_timeline(
            self.learning_store.path,
            course_id=course_id,
            sub_id=sub_id,
            transcript_segments=transcript,
            classified=classify_segments(course_id=course_id, sub_id=sub_id, segments=transcript),
        )
    def record_watch_events(self, course_id, sub_id, events):
        return insert_watch_events(
            self.learning_store.path, course_id=str(course_id), sub_id=str(sub_id), events=list(events or []),
        )
    def list_watch_events(self, sub_id):
        return list_watch_events(self.learning_store.path, sub_id=str(sub_id))
    def clear_watch_events(self, sub_id=""):
        return clear_watch_events(self.learning_store.path, sub_id=str(sub_id or ""))
    def authentication_snapshot(self): return {"state": "ready"}
    def _identity_scope(self): return "test-identity"
    def authorized_catalog_snapshot(self, **filters):
        courses = self.catalog_repository.courses()
        teacher = str(filters.get("teacher") or "").casefold()
        term = str(filters.get("term") or "").casefold()
        if teacher:
            courses = [item for item in courses if teacher in str(item.get("teacher") or "").casefold()]
        if term:
            courses = [item for item in courses if term in str(item.get("term") or "").casefold()]
        return {"state": "ready", "source": "fixture", "courses": courses, "course_count": len(courses)}
    def configure_daily_schedule(self, value):
        value = {**value, "pipeline_version": "daily-schedule-v2"}
        self.task_store.set_app_state("daily_schedule", value)
        return value
    def run_daily_schedule(self, force=False): return {"state": "completed", "force": bool(force)}
    def timetable_snapshot(self, semester_id="", week=0):
        return {
            "state": "ready", "code": "timetable_verified", "source": "fixture",
            "observed_at": 1, "expires_at": 2,
            "selected_semester": {"semester_id": semester_id or "20262", "label": "2025-2026 第二学期", "start_date": "2026-02-23"},
            "selected_week": week or 1, "days": [], "courses": [],
            "current_meeting": None, "next_meeting": None,
            "counts": {"courses": 0, "meetings": 0, "conflicts": 0, "linked": 0, "ambiguous": 0, "unlinked": 0},
        }
    def timetable_action(self, action, *, semester_id="", start_date=""):
        value = self.timetable_snapshot(semester_id, 1)
        value["accepted_action"] = action
        value["start_date"] = start_date
        return value
    def timetable_ics(self, semester_id=""):
        return f"CourseLens-{semester_id or 'current'}.ics", "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"


class V3RouteTests(unittest.TestCase):
    def test_artifacts_missing_returns_empty_envelope_not_404(self):
        # C3（PB-1）：「尚无产物」改 200+artifact:null 空载荷信封，不再 404——
        # 每次打开讲次不再往浏览器 console 记结构性错误；artifact_not_found 码退役
        # （前端码表同笔删除，等集钉见 test_frontend_workbench）。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); service = _Service(root)

            def _missing(_sub_id, _kind="lecture_summary"):
                raise FileNotFoundError("AI artifact has not been generated")
            service.ai_artifact = _missing
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urlopen(f"{base}/api/v3/artifacts?sub_id=s1&kind=lecture_summary") as response:
                    self.assertEqual(response.status, 200)
                    value = json.loads(response.read())["data"]
                self.assertIsNone(value["artifact"])
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_timetable_routes_validate_week_are_idempotent_and_export_ics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); service = _Service(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urlopen(f"{base}/api/v3/timetable?semester_id=20262&week=3") as response:
                    value = json.loads(response.read())["data"]
                self.assertEqual(value["selected_week"], 3)
                body = json.dumps({
                    "action": "refresh", "semester_id": "20262",
                    "operation_id": "timetable-operation-1",
                }).encode()
                request = Request(
                    f"{base}/api/v3/timetable/actions", data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urlopen(request) as response:
                    first = json.loads(response.read())["data"]
                with urlopen(request) as response:
                    duplicate = json.loads(response.read())["data"]
                self.assertEqual(first, duplicate)
                with urlopen(f"{base}/api/v3/timetable/export.ics?semester_id=20262") as response:
                    self.assertEqual(response.headers.get_content_type(), "text/calendar")
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertIn("BEGIN:VCALENDAR", response.read().decode())
                with self.assertRaises(HTTPError) as error:
                    urlopen(f"{base}/api/v3/timetable?week=31")
                self.assertEqual(error.exception.code, 400)
                self.assertEqual(json.loads(error.exception.read())["error_code"], "timetable_week_invalid")
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_analytics_is_opt_in_through_backend_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); service = _Service(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urlopen(f"{base}/api/v3/analytics") as response:
                    value = json.loads(response.read())["data"]
                self.assertFalse(value["settings"]["enabled"])
                body = json.dumps({"action": "enable", "timezone": "Asia/Shanghai"}).encode()
                with urlopen(Request(
                    f"{base}/api/v3/analytics/actions", data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )) as response:
                    settings = json.loads(response.read())["data"]["settings"]
                self.assertTrue(settings["enabled"])
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_concept_analysis_and_actions_are_versioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); service = _Service(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urlopen(f"{base}/api/v3/concepts") as response:
                    graph = json.loads(response.read())["data"]
                self.assertEqual(graph["concepts"][0]["name"], "梯度下降")
                body = json.dumps({"action": "analyze", "course_ids": ["1", "2"]}).encode()
                with urlopen(Request(
                    f"{base}/api/v3/concepts/actions", data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )) as response:
                    analysis = json.loads(response.read())["data"]["analysis"]
                self.assertEqual(analysis["edge_count"], 1)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_bookmark_actions_return_backend_confirmed_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); service = _Service(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                body = json.dumps({"bookmark_id": "bookmark-1", "action": "explain"}).encode()
                with urlopen(Request(
                    f"{base}/api/v3/bookmarks/actions", data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )) as response:
                    data = json.loads(response.read())["data"]
                self.assertEqual(data["task"]["state"], "queued")
                self.assertEqual(data["bookmark"]["explanation_state"], "queued")
                body = json.dumps({"bookmark_id": "bookmark-1", "action": "resolve"}).encode()
                with urlopen(Request(
                    f"{base}/api/v3/bookmarks/actions", data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )) as response:
                    resolved = json.loads(response.read())["data"]["bookmark"]
                self.assertEqual(resolved["resolution_status"], "resolved")
                body = json.dumps({"bookmark_id": "bookmark-1", "action": "reopen"}).encode()
                with urlopen(Request(
                    f"{base}/api/v3/bookmarks/actions", data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )) as response:
                    reopened = json.loads(response.read())["data"]["bookmark"]
                self.assertEqual(reopened["resolution_status"], "open")
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_bookmark_delete_route_closed_set(self):
        """PLAYER-UX-1④：DELETE /api/v3/bookmarks 闭集——成功删/未知 id 404/缺 id 400。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); service = _Service(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                body = json.dumps({"bookmark_id": "bookmark-1"}).encode()
                with urlopen(Request(
                    f"{base}/api/v3/bookmarks", data=body,
                    headers={"Content-Type": "application/json"}, method="DELETE",
                )) as response:
                    data = json.loads(response.read())["data"]
                self.assertEqual(data, {"bookmark_id": "bookmark-1", "deleted": True})
                with self.assertRaises(HTTPError) as missing:
                    urlopen(Request(
                        f"{base}/api/v3/bookmarks", data=json.dumps({"bookmark_id": "no-such"}).encode(),
                        headers={"Content-Type": "application/json"}, method="DELETE",
                    ))
                self.assertEqual(missing.exception.code, 404)
                self.assertEqual(json.loads(missing.exception.read())["error_code"], "bookmark_not_found")
                with self.assertRaises(HTTPError) as invalid:
                    urlopen(Request(
                        f"{base}/api/v3/bookmarks", data=json.dumps({}).encode(),
                        headers={"Content-Type": "application/json"}, method="DELETE",
                    ))
                self.assertEqual(invalid.exception.code, 400)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_bookmark_create_route_rejects_blank_bookmarks(self):
        """RR-BOOKMARK-1（WINIT-1 实测误建空白书签）：POST /api/v3/bookmarks 闭集——
        缺课程/讲次身份 400 bookmark_request_invalid（此前静默落库）；零依据讲次
        400 bookmark_evidence_unavailable（复用 explain 链同源码，前端码表已映射
        人话）；有正文讲次 201 照常创建。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); service = _Service(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaises(HTTPError) as missing:
                    urlopen(Request(
                        f"{base}/api/v3/bookmarks", data=json.dumps({"start_ms": 1000}).encode(),
                        headers={"Content-Type": "application/json"}, method="POST",
                    ))
                self.assertEqual(missing.exception.code, 400)
                self.assertEqual(json.loads(missing.exception.read())["error_code"], "bookmark_request_invalid")
                body = json.dumps({"course_id": "1", "sub_id": "contentless", "start_ms": 61_000, "note": "没听懂"}).encode()
                with self.assertRaises(HTTPError) as blank:
                    urlopen(Request(
                        f"{base}/api/v3/bookmarks", data=body,
                        headers={"Content-Type": "application/json"}, method="POST",
                    ))
                self.assertEqual(blank.exception.code, 400)
                self.assertEqual(json.loads(blank.exception.read())["error_code"], "bookmark_evidence_unavailable")
                body = json.dumps({"course_id": "1", "sub_id": "2", "start_ms": 61_000, "note": "没听懂"}).encode()
                with urlopen(Request(
                    f"{base}/api/v3/bookmarks", data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )) as response:
                    self.assertEqual(response.status, 201)
                    bookmark = json.loads(response.read())["data"]["bookmark"]
                self.assertEqual(bookmark["bookmark_id"], "bookmark-2-61000")
                self.assertEqual(bookmark["note"], "没听懂")
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_schedule_and_catalog_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(_Service(root)), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urlopen(f"{base}/api/v3/catalog?teacher=teacher&term=2026") as response:
                    self.assertEqual(len(json.loads(response.read())["data"]["courses"]), 1)
                body = json.dumps({
                    "enabled": True,
                    "time": "07:30",
                    "course_ids": ["1"],
                    "outputs": ["subtitle", "summary"],
                    "catch_up": True,
                }).encode()
                with urlopen(Request(f"{base}/api/v3/schedules", data=body, headers={"Content-Type": "application/json"}, method="POST")):
                    pass
                with urlopen(f"{base}/api/v3/schedules") as response:
                    schedule = json.loads(response.read())["data"]["schedule"]
                    self.assertTrue(schedule["enabled"])
                    self.assertEqual(schedule["pipeline_version"], "daily-schedule-v2")
                    # One automatic subtitle quality policy: schedules carry no
                    # user-chosen subtitle mode anymore.
                    self.assertNotIn("subtitle_mode", schedule)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_document_import_alignment_and_manual_update_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(_Service(root)), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                body = json.dumps({
                    "action": "import",
                    "course_id": "1",
                    "sub_id": "lecture-1",
                    "title": "讲义",
                    "original_name": "notes.txt",
                    "media_type": "text/plain",
                    "content_base64": base64.b64encode("矩阵 特征值 特征向量 重点".encode()).decode(),
                }).encode()
                with urlopen(Request(f"{base}/api/v3/documents", data=body, headers={"Content-Type": "application/json"}, method="POST")) as response:
                    document = json.loads(response.read())["data"]["document"]
                self.assertEqual(document["page_count"], 1)
                self.assertTrue(document["pages"][0]["alignment_id"])
                with urlopen(f"{base}/api/v3/alignments?document_id={document['document_id']}") as response:
                    alignment = json.loads(response.read())["data"]["alignments"][0]
                update = json.dumps({
                    "action": "update",
                    "alignment_id": alignment["alignment_id"],
                    "start_ms": 12000,
                    "end_ms": 17000,
                    "status": "confirmed",
                }).encode()
                with urlopen(Request(f"{base}/api/v3/alignments", data=update, headers={"Content-Type": "application/json"}, method="POST")) as response:
                    saved = json.loads(response.read())["data"]["alignment"]
                self.assertEqual(saved["status"], "confirmed")
                self.assertEqual(saved["start_ms"], 12000)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_timeline_classification_route_returns_backend_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(_Service(root)), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                body = json.dumps({"course_id": "1", "sub_id": "lecture-1"}).encode()
                with urlopen(Request(f"{base}/api/v3/timeline/classify", data=body, headers={"Content-Type": "application/json"}, method="POST")) as response:
                    timeline = json.loads(response.read())["data"]
                self.assertTrue(timeline["segments"])
                self.assertTrue(timeline["segments"][0]["evidence"]["source_hash"])
                with urlopen(f"{base}/api/v3/timeline?sub_id=lecture-1") as response:
                    loaded = json.loads(response.read())["data"]
                self.assertFalse(loaded["stale"])
                self.assertEqual(loaded["meta"]["segment_count"], len(loaded["segments"]))
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_watch_events_routes_round_trip_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(_Service(root)), root))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                body = json.dumps({"course_id": "1", "sub_id": "lecture-1", "events": [
                    {"event": "seek_back", "position_ms": 12000, "occurred_at": 100.0},
                    {"event": "bogus-event", "position_ms": 1, "occurred_at": 101.0},
                ]}).encode()
                with urlopen(Request(f"{base}/api/v3/watch-events", data=body, headers={"Content-Type": "application/json"}, method="POST")) as response:
                    self.assertEqual(json.loads(response.read())["data"]["inserted"], 1)
                with urlopen(f"{base}/api/v3/watch-events?sub_id=lecture-1") as response:
                    events = json.loads(response.read())["data"]["events"]
                self.assertEqual([item["event"] for item in events], ["seek_back"])
                clear_body = json.dumps({"sub_id": "lecture-1"}).encode()
                with urlopen(Request(f"{base}/api/v3/watch-events/clear", data=clear_body, headers={"Content-Type": "application/json"}, method="POST")) as response:
                    self.assertEqual(json.loads(response.read())["data"]["deleted"], 1)
                with urlopen(f"{base}/api/v3/watch-events?sub_id=lecture-1") as response:
                    self.assertEqual(json.loads(response.read())["data"]["events"], [])
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
