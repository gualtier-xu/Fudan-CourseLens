from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from src.runtime.timetable import (
    UNDERGRADUATE_PAGE,
    Semester,
    TimetableError,
    TimetableRuntime,
    TimetableStore,
    parse_postgraduate_payload,
    parse_undergraduate_payload,
    parse_undergraduate_semesters,
    reconcile_courses,
    render_ics,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class TimetableParserTests(unittest.TestCase):
    def test_undergraduate_semester_and_course_payloads(self):
        html = (FIXTURES / "timetable_semesters.html").read_text(encoding="utf-8")
        semesters, default_id = parse_undergraduate_semesters(html)
        self.assertEqual(default_id, "20262")
        self.assertEqual(semesters[0].start_date, "2026-02-23")
        payload = json.loads((FIXTURES / "timetable_undergraduate.json").read_text(encoding="utf-8"))
        courses = parse_undergraduate_payload(payload, semesters[0])
        self.assertEqual(len(courses), 2)
        self.assertEqual(courses[0]["semester_start_date"], "2026-02-23")
        math = next(item for item in courses if item["course_code"] == "MATH120001")
        self.assertEqual(math["meetings"], [{"weekday": 1, "start_unit": 1, "end_unit": 2}])

    def test_undergraduate_current_page_semester_injection(self):
        page = """
        <select id="allSemesters"></select>
        <script>
        const $allSemesters = document.querySelector('#allSemesters');
        const currentSemester = JSON.parse('{"id":20262,"name":"第二学期"}');
        const allSemesters = JSON.parse('[{"id":20261,"name":"第一学期"},{"id":20262,"name":"第二学期","startDate":"2026-02-22"}]');
        </script>
        """
        semesters, default_id = parse_undergraduate_semesters(page)
        self.assertEqual(default_id, "20262")
        self.assertEqual([item.semester_id for item in semesters], ["20261", "20262"])
        self.assertEqual(semesters[1].start_date, "2026-02-23")
        self.assertTrue(semesters[1].is_default)

    def test_undergraduate_semester_injection_fails_closed_on_mismatch(self):
        page = """
        const currentSemester = JSON.parse('{"id":20263}');
        const allSemesters = JSON.parse('[{"id":20262,"name":"第二学期"}]');
        """
        with self.assertRaisesRegex(TimetableError, "inconsistent"):
            parse_undergraduate_semesters(page)

    def test_postgraduate_adjacent_slots_are_merged(self):
        payload = json.loads((FIXTURES / "timetable_postgraduate.json").read_text(encoding="utf-8"))
        courses = parse_postgraduate_payload(
            payload, semester_id="20262", semester_label="2025-2026 第二学期",
            start_date="2026-02-23",
        )
        self.assertEqual(len(courses), 1)
        self.assertEqual(courses[0]["week_indexes"], [1, 2, 3, 4])
        self.assertEqual(courses[0]["meetings"], [{"weekday": 3, "start_unit": 6, "end_unit": 7}])

    def test_catalog_reconciliation_requires_term_title_and_teacher(self):
        course = {
            "semester_label": "2025-2026 第二学期", "semester_id": "20262",
            "title": "高等数学", "teachers": ["张老师"],
        }
        catalog = [
            {"course_id": "1", "title": "高等数学", "teacher": "张老师", "term": "2025-2026 第二学期"},
            {"course_id": "2", "title": "高等数学", "teacher": "李老师", "term": "2025-2026 第二学期"},
        ]
        linked = reconcile_courses([course], catalog)[0]["catalog_link"]
        self.assertEqual(linked, {"state": "linked", "course_id": "1", "candidate_course_ids": []})
        ambiguous = reconcile_courses([course], catalog + [
            {"course_id": "3", "title": "高等数学", "teacher": "张老师", "term": "2025-2026 第二学期"}
        ])[0]["catalog_link"]
        self.assertEqual(ambiguous["state"], "ambiguous")
        unlinked = reconcile_courses([course], [{**catalog[0], "term": "2024-2025 第二学期"}])[0]["catalog_link"]
        self.assertEqual(unlinked["state"], "unlinked")


class TimetableStoreTests(unittest.TestCase):
    class _Response:
        def __init__(self, *, status=200, text="", payload=None, content_type="application/json"):
            self.status_code = status
            self.text = text
            self._payload = payload
            self.headers = {"content-type": content_type}

        def json(self):
            if isinstance(self._payload, BaseException):
                raise self._payload
            return self._payload

    class _Vpn:
        def __init__(self, responses):
            self.responses = list(responses)
            self.urls = []

        def get_allowed(self, url, **_kwargs):
            self.urls.append(url)
            if not self.responses:
                raise AssertionError("unexpected timetable request")
            value = self.responses.pop(0)
            if isinstance(value, BaseException):
                raise value
            return value

    def test_refresh_keeps_undergraduate_data_when_postgraduate_source_fails(self):
        html = (FIXTURES / "timetable_semesters.html").read_text(encoding="utf-8")
        ug = json.loads((FIXTURES / "timetable_undergraduate.json").read_text(encoding="utf-8"))
        vpn = self._Vpn([
            self._Response(text=html), self._Response(payload=ug), RuntimeError("offline"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            runtime = TimetableRuntime(
                Path(tmp) / "state.db", identity_getter=lambda: "identity-a",
                auth_getter=lambda: {"state": "ready"}, vpn_getter=lambda: vpn,
                catalog_getter=lambda: [],
            )
            value = runtime.refresh()
            self.assertEqual(value["code"], "timetable_partial")
            self.assertEqual(value["counts"]["courses"], 2)
            self.assertEqual(value["partial_failures"][0]["source"], "fudan_postgraduate")

    def test_refresh_resolves_persisted_current_alias_to_default_semester(self):
        html = (FIXTURES / "timetable_semesters.html").read_text(encoding="utf-8")
        ug = json.loads((FIXTURES / "timetable_undergraduate.json").read_text(encoding="utf-8"))
        vpn = self._Vpn([
            self._Response(text=html), self._Response(payload=ug),
            self._Response(text="gateway error", content_type="text/html"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            runtime = TimetableRuntime(
                Path(tmp) / "state.db", identity_getter=lambda: "identity-a",
                auth_getter=lambda: {"state": "ready"}, vpn_getter=lambda: vpn,
                catalog_getter=lambda: [],
            )
            runtime.store.update_preferences("identity-a", selected_semester_id="current")
            value = runtime.refresh()
            self.assertEqual(value["code"], "timetable_partial")
            self.assertEqual(value["selected_semester"]["semester_id"], "20262")
            self.assertEqual(value["counts"]["courses"], 2)
            self.assertEqual(value["partial_failures"], [{
                "source": "fudan_postgraduate", "code": "timetable_upstream_unavailable",
            }])

    def test_refresh_follows_a_strict_undergraduate_sso_handoff(self):
        html = (FIXTURES / "timetable_semesters.html").read_text(encoding="utf-8")
        ug = json.loads((FIXTURES / "timetable_undergraduate.json").read_text(encoding="utf-8"))
        handoff = (
            "https://fdjwgl.fudan.edu.cn/student/sso/login?"
            "refer=https%3A%2F%2Ffdjwgl.fudan.edu.cn%2Fstudent%2Ffor-std%2Fcourse-table"
            "&ticket=ST-synthetic-ticket-used-only-by-tests"
        )
        vpn = self._Vpn([
            self._Response(text=f'<script>location.href="{handoff.replace("&", "&amp;")}"</script>'),
            self._Response(text="authenticated home"),
            self._Response(text=html),
            self._Response(payload=ug),
            RuntimeError("offline"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            runtime = TimetableRuntime(
                Path(tmp) / "state.db", identity_getter=lambda: "identity-a",
                auth_getter=lambda: {"state": "ready"}, vpn_getter=lambda: vpn,
                catalog_getter=lambda: [],
            )
            value = runtime.refresh()
            self.assertEqual(value["counts"]["courses"], 2)
            self.assertEqual(vpn.urls[1], handoff)
            self.assertEqual(vpn.urls[2], UNDERGRADUATE_PAGE)

    def test_undergraduate_sso_handoff_fails_closed_for_other_targets(self):
        with self.assertRaisesRegex(TimetableError, "handoff"):
            TimetableRuntime._undergraduate_sso_handoff(
                '<a href="https://example.invalid/student/sso/login?'
                'refer=https%3A%2F%2Ffdjwgl.fudan.edu.cn%2Fstudent%2Ffor-std%2Fcourse-table'
                '&ticket=ST-synthetic-ticket-used-only-by-tests">continue</a>'
            )

    def test_postgraduate_start_date_is_required_once_then_persisted(self):
        pg = json.loads((FIXTURES / "timetable_postgraduate.json").read_text(encoding="utf-8"))
        vpn = self._Vpn([
            self._Response(text="login page"), self._Response(payload=pg),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            runtime = TimetableRuntime(
                Path(tmp) / "state.db", identity_getter=lambda: "identity-a",
                auth_getter=lambda: {"state": "ready"}, vpn_getter=lambda: vpn,
                catalog_getter=lambda: [],
            )
            value = runtime.refresh()
            self.assertEqual(value["code"], "semester_start_required")
            updated = runtime.set_semester_start("current", "2026-02-23")
            self.assertNotEqual(updated["code"], "semester_start_required")
            self.assertEqual(updated["selected_semester"]["start_date"], "2026-02-23")

    def test_six_hour_freshness_marks_verified_cache_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = TimetableRuntime(
                Path(tmp) / "state.db", identity_getter=lambda: "identity-a",
                auth_getter=lambda: {"state": "ready"}, vpn_getter=lambda: None,
                catalog_getter=lambda: [],
            )
            semester = Semester("20262", "2025-2026 第二学期", "2026-02-23")
            runtime.store.put("identity-a", "fudan_undergraduate", "20262", {
                "source": "fudan_undergraduate", "semester": semester.public(),
                "semesters": [semester.public()], "courses": [],
                "observed_at": time.time() - 7 * 60 * 60,
            })
            runtime.store.update_preferences("identity-a", selected_semester_id="20262")
            self.assertEqual(runtime.snapshot("20262", 1)["code"], "timetable_stale")

    # ---- 合成快照状态复现：空/过期/双源/partial/全失败/HTML 授权页/JSON 漂移/未登录 ----

    def test_snapshot_without_stored_data_is_an_honest_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = TimetableRuntime(
                Path(tmp) / "state.db", identity_getter=lambda: "identity-a",
                auth_getter=lambda: {"state": "ready"}, vpn_getter=lambda: None,
                catalog_getter=lambda: [],
            )
            value = runtime.snapshot()
            self.assertEqual(value["code"], "timetable_not_loaded")
            self.assertEqual(value["state"], "empty")
            self.assertEqual(value["actions"], ["refresh"])
            self.assertEqual(value["days"], [])
            self.assertEqual(
                value["counts"],
                {"courses": 0, "meetings": 0, "conflicts": 0, "linked": 0, "ambiguous": 0, "unlinked": 0},
            )
            self.assertEqual(value["partial_failures"], [])

    def test_failed_refresh_over_expired_snapshot_is_not_masked_as_silent_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = TimetableRuntime(
                Path(tmp) / "state.db", identity_getter=lambda: "identity-a",
                auth_getter=lambda: {"state": "ready"}, vpn_getter=lambda: None,
                catalog_getter=lambda: [],
            )
            semester = Semester("20262", "2025-2026 第二学期", "2026-02-23")
            runtime.store.put("identity-a", "fudan_undergraduate", "20262", {
                "source": "fudan_undergraduate", "semester": semester.public(),
                "semesters": [semester.public()], "courses": [],
                "observed_at": time.time() - 31 * 24 * 60 * 60,
            })
            runtime.store.update_preferences("identity-a", selected_semester_id="20262")
            self.assertEqual(runtime.snapshot("20262", 1)["code"], "timetable_not_loaded")
            vpn = self._Vpn([RuntimeError("offline"), RuntimeError("offline")])
            runtime.vpn_getter = lambda: vpn
            value = runtime.refresh()
            self.assertEqual(value["code"], "timetable_not_loaded")
            self.assertEqual(value["state"], "empty")
            self.assertEqual(value["days"], [])
            self.assertEqual(
                [item["source"] for item in value["partial_failures"]],
                ["fudan_undergraduate", "fudan_postgraduate"],
            )
            for item in value["partial_failures"]:
                self.assertIn(item["code"], {
                    "timetable_upstream_unavailable", "timetable_session_expired",
                    "timetable_payload_invalid",
                })
            self.assertEqual(runtime.snapshot()["partial_failures"], [])

    def test_refresh_without_stored_data_and_both_sources_failed_raises_closed_set_code(self):
        vpn = self._Vpn([RuntimeError("offline"), RuntimeError("offline")])
        with tempfile.TemporaryDirectory() as tmp:
            runtime = TimetableRuntime(
                Path(tmp) / "state.db", identity_getter=lambda: "identity-a",
                auth_getter=lambda: {"state": "ready"}, vpn_getter=lambda: vpn,
                catalog_getter=lambda: [],
            )
            with self.assertRaises(TimetableError) as caught:
                runtime.refresh()
            self.assertEqual(caught.exception.code, "timetable_upstream_unavailable")
            self.assertEqual(runtime.store.list("identity-a"), [])

    def test_valid_dual_source_refresh_reports_verified(self):
        html = (FIXTURES / "timetable_semesters.html").read_text(encoding="utf-8")
        ug = json.loads((FIXTURES / "timetable_undergraduate.json").read_text(encoding="utf-8"))
        pg = json.loads((FIXTURES / "timetable_postgraduate.json").read_text(encoding="utf-8"))
        vpn = self._Vpn([self._Response(text=html), self._Response(payload=ug), self._Response(payload=pg)])
        with tempfile.TemporaryDirectory() as tmp:
            runtime = TimetableRuntime(
                Path(tmp) / "state.db", identity_getter=lambda: "identity-a",
                auth_getter=lambda: {"state": "ready"}, vpn_getter=lambda: vpn,
                catalog_getter=lambda: [],
            )
            value = runtime.refresh()
            self.assertEqual(value["code"], "timetable_verified")
            self.assertEqual(value["state"], "ready")
            self.assertEqual(
                set(value["source"].split("+")),
                {"fudan_undergraduate", "fudan_postgraduate"},
            )
            self.assertEqual(value["partial_failures"], [])

    def test_source_responses_map_to_closed_set_codes(self):
        response_json = TimetableRuntime._response_json
        for response, expected in (
            (self._Response(status=401, payload={}), "timetable_session_expired"),
            (self._Response(status=503, payload={}), "timetable_upstream_unavailable"),
            (self._Response(text="authorization page", content_type="text/html"), "timetable_upstream_unavailable"),
            (self._Response(payload=ValueError("invalid JSON body")), "timetable_payload_invalid"),
        ):
            with self.assertRaises(TimetableError) as caught:
                response_json(response, "fudan_undergraduate")
            self.assertEqual(caught.exception.code, expected)
        with self.assertRaises(TimetableError) as caught:
            parse_undergraduate_payload({"unexpected": []}, Semester("20262", "", "2026-02-23"))
        self.assertEqual(caught.exception.code, "timetable_payload_invalid")
        # 夜10-C T16：activities=None 曾以裸 TypeError 逃逸闭集——现 fail-closed。
        for broken in (
            {"studentTableVms": [{"activities": None}]},
            {"studentTableVms": [{"activities": "not-a-list"}]},
            {"studentTableVms": [{"activities": {"0": "dict-not-list"}}]},
        ):
            with self.assertRaises(TimetableError) as caught:
                parse_undergraduate_payload(broken, Semester("20262", "", "2026-02-23"))
            self.assertEqual(caught.exception.code, "timetable_payload_invalid")

    def test_refresh_and_snapshot_require_ready_authentication(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = TimetableRuntime(
                Path(tmp) / "state.db", identity_getter=lambda: "identity-a",
                auth_getter=lambda: {"state": "action_required"}, vpn_getter=lambda: None,
                catalog_getter=lambda: [],
            )
            with self.assertRaises(TimetableError) as caught:
                runtime.refresh()
            self.assertEqual(caught.exception.code, "timetable_login_required")
            self.assertEqual(runtime.snapshot()["code"], "timetable_login_required")

    def test_identity_scoped_snapshots_and_preferences(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TimetableStore(Path(tmp) / "state.db")
            store.put("identity-a", "fudan_undergraduate", "20262", {"observed_at": 10, "courses": [{"title": "A"}]})
            store.put("identity-b", "fudan_undergraduate", "20262", {"observed_at": 11, "courses": [{"title": "B"}]})
            self.assertEqual(store.list("identity-a")[0]["payload"]["courses"][0]["title"], "A")
            self.assertEqual(store.list("identity-b")[0]["payload"]["courses"][0]["title"], "B")
            value = store.update_preferences(
                "identity-a", selected_semester_id="20262", semester_id="20262", start_date="2026-02-23"
            )
            self.assertEqual(value["semester_start_dates"]["20262"], "2026-02-23")

    def test_snapshot_redacts_cache_until_authentication_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = {"state": "ready"}
            runtime = TimetableRuntime(
                Path(tmp) / "state.db",
                identity_getter=lambda: "identity-a",
                auth_getter=lambda: auth,
                vpn_getter=lambda: None,
                catalog_getter=lambda: [],
            )
            semester = Semester("20262", "2025-2026 第二学期", "2026-02-23")
            payload = json.loads((FIXTURES / "timetable_undergraduate.json").read_text(encoding="utf-8"))
            runtime.store.put("identity-a", "fudan_undergraduate", "20262", {
                "source": "fudan_undergraduate", "semester": semester.public(),
                "semesters": [semester.public()], "courses": parse_undergraduate_payload(payload, semester),
                "observed_at": time.time(),
            })
            runtime.store.update_preferences("identity-a", selected_semester_id="20262")
            self.assertEqual(runtime.snapshot("20262", 1)["counts"]["courses"], 2)
            auth["state"] = "action_required"
            redacted = runtime.snapshot("20262", 1)
            self.assertEqual(redacted["code"], "timetable_login_required")
            self.assertEqual(redacted["courses"], [])
            self.assertIsNone(redacted["counts"]["courses"])

    def test_conflicts_current_shape_and_deterministic_ics(self):
        semester = Semester("20262", "2025-2026 第二学期", "2026-02-23")
        payload = json.loads((FIXTURES / "timetable_undergraduate.json").read_text(encoding="utf-8"))
        courses = parse_undergraduate_payload(payload, semester)
        conflicting = {**courses[0], "timetable_course_id": "conflict", "title": "线性代数"}
        snapshot = {
            "observed_at": 1771804800,
            "courses": courses + [conflicting],
        }
        first = render_ics(snapshot)
        second = render_ics(snapshot)
        self.assertEqual(first, second)
        self.assertIn("TZID:Asia/Shanghai", first)
        self.assertIn("SUMMARY:高等数学", first)
        self.assertTrue(first.endswith("\r\n"))


if __name__ == "__main__":
    unittest.main()
