"""Closed-set live occurrence selection for the monthly iCourse index.

Locks the shared private selector behind both live consumers
(``get_live_course_observation`` and ``get_live_stream_params``): a currently
live occurrence wins over earlier ended rows, otherwise the occurrence
bracketing or nearest to now is chosen from parseable time evidence,
preserved parent-day order carries selection when no row carries times, and
ambiguous or unknown evidence fails closed without inventing live state.
Synthetic payloads only; no real session or network request.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from src.api.icourse import (
    ICourseClient,
    _collect_live_occurrence_candidates,
    _parse_occurrence_span,
    _select_live_occurrence,
)
from src.runtime.live_room import LiveRoomError

NOW = datetime(2026, 9, 15, 14, 30, tzinfo=timezone.utc)
CURRENT_HLS = "https://live.invalid/hls/current.m3u8"
OLD_HLS = "https://live.invalid/hls/old.m3u8"


def _fmt(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _json_response(payload):
    return SimpleNamespace(
        status_code=200,
        raise_for_status=lambda: None,
        json=lambda: payload,
    )


class _RecordingTransport:
    """Replays one monthly index payload for every month request."""

    def __init__(self, month_payload, detail_payload=None, sub_info_payload=None):
        self.session = SimpleNamespace(cookies=[])
        self.calls: list[tuple[str, dict]] = []
        self._month_payload = month_payload
        self._detail_payload = detail_payload
        self._sub_info_payload = sub_info_payload

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "get-my-course-month" in url:
            return _json_response(self._month_payload)
        if "get-course-detail" in url and self._detail_payload is not None:
            return _json_response(self._detail_payload)
        if "get-sub-info" in url and self._sub_info_payload is not None:
            return _json_response(self._sub_info_payload)
        raise AssertionError("unexpected URL family")


def _client(month_payload) -> ICourseClient:
    return ICourseClient(_RecordingTransport(month_payload))


def _month_payload(*day_rows) -> dict:
    """Build a monthly index: each argument is one day container's rows."""
    return {
        "code": 0,
        "list": [{"course": list(rows)} for rows in day_rows],
    }


def _month_days_payload(*days) -> dict:
    """Build a monthly index from full day containers (with their date key)."""
    return {"code": 0, "list": list(days)}


def _row(sub_id="s01", *, course_id="c1", state="ended", start=None,
         end=None, hls="") -> dict:
    row = {
        "id": course_id,
        "title": "synthetic-course",
        "sub_id": sub_id,
        "sub_duration": 90,
        "live_status": state,
    }
    if start is not None:
        row["start_time"] = start
    if end is not None:
        row["end_time"] = end
    if hls:
        row["hls_url"] = hls
    return row


class LiveOccurrenceSelectionTests(unittest.TestCase):
    def test_currently_live_occurrence_wins_over_earlier_ended_rows(self):
        payload = _month_payload(
            [_row(sub_id="s01", state="ended",
                  start=_fmt(NOW - timedelta(days=14)),
                  end=_fmt(NOW - timedelta(days=14) + timedelta(hours=2)),
                  hls=OLD_HLS)],
            [_row(sub_id="s08", state="ended",
                  start=_fmt(NOW - timedelta(days=7)),
                  end=_fmt(NOW - timedelta(days=7) + timedelta(hours=2)))],
            [_row(sub_id="s15", state="live",
                  start=_fmt(NOW - timedelta(hours=1)),
                  end=_fmt(NOW + timedelta(hours=2)),
                  hls=CURRENT_HLS)],
        )
        observation = _client(payload).get_live_course_observation("c1", now=NOW)
        self.assertEqual(observation["state"], "live")
        self.assertEqual(observation["code"], "live_live_observed")
        self.assertEqual(observation["starts_at"], _fmt(NOW - timedelta(hours=1)))
        self.assertEqual(observation["ends_at"], _fmt(NOW + timedelta(hours=2)))

    def test_no_live_occurrence_selects_nearest_to_now_by_time_evidence(self):
        payload = _month_payload(
            [_row(sub_id="s01", state="ended",
                  start=_fmt(NOW - timedelta(days=14)),
                  end=_fmt(NOW - timedelta(days=14) + timedelta(hours=2)))],
            [_row(sub_id="s08", state="ended",
                  start=_fmt(NOW - timedelta(days=7)),
                  end=_fmt(NOW - timedelta(days=7) + timedelta(hours=2)))],
            [_row(sub_id="s22", state="upcoming",
                  start=_fmt(NOW + timedelta(days=7)),
                  end=_fmt(NOW + timedelta(days=7) + timedelta(hours=2)))],
        )
        client = _client(payload)
        nearest_past = client.get_live_course_observation("c1", now=NOW)
        self.assertEqual(nearest_past["state"], "ended")
        self.assertEqual(
            nearest_past["starts_at"], _fmt(NOW - timedelta(days=7))
        )
        nearest_future = client.get_live_course_observation(
            "c1", now=NOW + timedelta(days=6)
        )
        self.assertEqual(nearest_future["state"], "upcoming")
        self.assertEqual(
            nearest_future["starts_at"], _fmt(NOW + timedelta(days=7))
        )

    def test_schedule_bracketing_row_is_selected_but_keeps_declared_state(self):
        payload = _month_payload(
            [_row(sub_id="s01", state="ended",
                  start=_fmt(NOW - timedelta(days=14)),
                  end=_fmt(NOW - timedelta(days=14) + timedelta(hours=2)))],
            [_row(sub_id="s15", state="upcoming",
                  start=_fmt(NOW - timedelta(hours=1)),
                  end=_fmt(NOW + timedelta(hours=1)))],
        )
        observation = _client(payload).get_live_course_observation("c1", now=NOW)
        self.assertEqual(observation["state"], "upcoming")
        self.assertEqual(observation["code"], "live_upcoming_observed")
        self.assertEqual(observation["starts_at"], _fmt(NOW - timedelta(hours=1)))

    def test_order_only_evidence_uses_preserved_parent_day_order(self):
        payload = _month_payload(
            [_row(sub_id="s01", state="ended")],
            [_row(sub_id="s08", state="ended")],
            [_row(sub_id="s15", state="ended")],
        )
        candidates = _collect_live_occurrence_candidates(
            payload, "c1", timezone.utc
        )
        self.assertEqual(len(candidates), 3)
        selected = _select_live_occurrence(candidates, NOW)
        self.assertEqual(selected["row"]["sub_id"], "s15")
        self.assertEqual((selected["day_index"], selected["row_index"]), (2, 0))

        upcoming_payload = _month_payload(
            [_row(sub_id="s01", state="upcoming")],
            [_row(sub_id="s08", state="upcoming")],
        )
        upcoming_candidates = _collect_live_occurrence_candidates(
            upcoming_payload, "c1", timezone.utc
        )
        self.assertEqual(
            _select_live_occurrence(upcoming_candidates, NOW)["row"]["sub_id"],
            "s01",
        )

    def test_order_only_mixed_or_unknown_states_fail_closed(self):
        mixed_payload = _month_payload(
            [_row(sub_id="s01", state="ended")],
            [_row(sub_id="s15", state="upcoming")],
        )
        mixed = _collect_live_occurrence_candidates(
            mixed_payload, "c1", timezone.utc
        )
        self.assertIsNone(_select_live_occurrence(mixed, NOW))

        unknown_payload = _month_payload(
            [_row(sub_id="s01", state="mystery")],
            [_row(sub_id="s15", state="other")],
        )
        unknown = _collect_live_occurrence_candidates(
            unknown_payload, "c1", timezone.utc
        )
        self.assertIsNone(_select_live_occurrence(unknown, NOW))

        single_unknown = _collect_live_occurrence_candidates(
            _month_payload([_row(sub_id="s15", state="mystery")]),
            "c1",
            timezone.utc,
        )
        self.assertEqual(
            _select_live_occurrence(single_unknown, NOW)["row"]["sub_id"],
            "s15",
        )

    def test_unknown_state_reports_unknown_without_inventing_live(self):
        client = _client(
            _month_payload(
                [
                    _row(
                        sub_id="s15",
                        state="mystery",
                        start=_fmt(NOW - timedelta(hours=1)),
                        end=_fmt(NOW + timedelta(hours=1)),
                    )
                ]
            )
        )
        observation = client.get_live_course_observation("c1", now=NOW)
        self.assertEqual(observation["state"], "unknown")
        self.assertEqual(observation["code"], "live_unknown_observed")
        self.assertEqual(observation["starts_at"], _fmt(NOW - timedelta(hours=1)))

        competing = _client(
            _month_payload(
                [_row(sub_id="s01", state="ended",
                      start=_fmt(NOW - timedelta(days=14)),
                      end=_fmt(NOW - timedelta(days=14) + timedelta(hours=2)))],
                [_row(sub_id="s15", state="mystery",
                      start=_fmt(NOW - timedelta(hours=1)),
                      end=_fmt(NOW + timedelta(hours=1)))],
            )
        )
        nearest_unknown = competing.get_live_course_observation("c1", now=NOW)
        self.assertEqual(nearest_unknown["state"], "unknown")
        self.assertEqual(
            nearest_unknown["starts_at"], _fmt(NOW - timedelta(hours=1))
        )

    def test_no_matching_course_fails_closed(self):
        payload = _month_payload(
            [_row(sub_id="s01", state="live", course_id="c2",
                  start=_fmt(NOW - timedelta(hours=1)),
                  end=_fmt(NOW + timedelta(hours=1)), hls=CURRENT_HLS)],
        )
        client = _client(payload)
        observation = client.get_live_course_observation("c1", now=NOW)
        self.assertEqual(observation["state"], "unknown")
        self.assertEqual(observation["code"], "live_schedule_not_observed")
        # U⑰③：裸 FileNotFoundError 收编进闭集码族（原逃逸契约翻转）
        with self.assertRaises(LiveRoomError) as stream_error:
            client.get_live_stream_params("c1")
        self.assertEqual(stream_error.exception.code, "live_stream_unavailable")

    def test_multiple_live_occurrences_fail_closed(self):
        payload = _month_payload(
            [_row(sub_id="s08", state="live",
                  start=_fmt(NOW - timedelta(days=7)),
                  end=_fmt(NOW - timedelta(days=7) + timedelta(hours=2)))],
            [_row(sub_id="s15", state="live",
                  start=_fmt(NOW - timedelta(hours=1)),
                  end=_fmt(NOW + timedelta(hours=1)), hls=CURRENT_HLS)],
        )
        client = _client(payload)
        observation = client.get_live_course_observation("c1", now=NOW)
        self.assertEqual(observation["state"], "unknown")
        self.assertEqual(observation["code"], "live_occurrence_ambiguous")
        with self.assertRaises(LiveRoomError) as stream_error:
            client.get_live_stream_params("c1")
        self.assertEqual(stream_error.exception.code, "live_stream_unavailable")

    def test_status_and_stream_resolve_same_occurrence(self):
        real_now = datetime.now().astimezone()
        old_start = real_now - timedelta(days=7)
        live_start = real_now - timedelta(hours=1)
        live_end = real_now + timedelta(hours=2)
        payload = _month_payload(
            [_row(sub_id="s01", state="ended",
                  start=_fmt(old_start),
                  end=_fmt(old_start + timedelta(hours=2)), hls=OLD_HLS)],
            [_row(sub_id="s15", state="1",
                  start=_fmt(live_start), end=_fmt(live_end),
                  hls=CURRENT_HLS)],
        )
        client = _client(payload)
        observation = client.get_live_course_observation("c1")
        self.assertEqual(observation["state"], "live")
        self.assertEqual(observation["starts_at"], _fmt(live_start))
        _transport, url, headers = client.get_live_stream_params("c1")
        self.assertEqual(url, CURRENT_HLS)
        self.assertNotEqual(url, OLD_HLS)
        self.assertIn("Origin", headers)


TEACHER_HLS = "https://live.invalid/hls/teacher.m3u8"
STUDENT_HLS = "https://live.invalid/hls/student.m3u8"
AUDIO_HLS = "https://live.invalid/hls/audio.m3u8"


def _platform_row(sub_id="s15", *, course_id="c1", sub_status="1", start=None,
                  end=None, date_text=None, sub_title=None, live_url=None) -> dict:
    """One monthly-index row in the platform's real shape (LIVE-DIAG-1 §3)."""
    row = {
        "id": course_id,
        "title": "synthetic-course",
        "sub_id": sub_id,
        "sub_duration": 90,
        "sub_status": sub_status,
    }
    if start is not None:
        row["start_at"] = start
    if end is not None:
        row["end_at"] = end
    if date_text is not None:
        row["date"] = date_text
    if sub_title is not None:
        row["sub_title"] = sub_title
    if live_url is not None:
        row["live_url"] = live_url
    return row


class PlatformLiveRowShapeTests(unittest.TestCase):
    """Lock the platform-native field family: sub_status / start_at+end_at /
    bare times grounded by a date anchor, and the nested live_url dict.
    Regression: before LIVE-FIX-1 these rows all fell to
    live_occurrence_ambiguous because every state/time key went unrecognized.
    """

    def test_sub_status_live_row_yields_live_state(self):
        payload = _month_payload(
            [_row(sub_id="s01", state="ended",
                  start=_fmt(NOW - timedelta(days=7)),
                  end=_fmt(NOW - timedelta(days=7) + timedelta(hours=2)))],
            [_platform_row(sub_id="s15", sub_status="1",
                           start="14:00", end="15:40", date_text="2026-09-15")],
        )
        observation = _client(payload).get_live_course_observation("c1", now=NOW)
        self.assertEqual(observation["state"], "live")
        self.assertEqual(observation["code"], "live_live_observed")
        self.assertEqual(observation["starts_at"], "14:00")
        self.assertEqual(observation["ends_at"], "15:40")

    def test_sub_status_value_domain_maps_through_live_state_map(self):
        for sub_status, expected in (("2", "ended"), ("0", "upcoming")):
            with self.subTest(sub_status=sub_status):
                candidates = _collect_live_occurrence_candidates(
                    _month_payload([_platform_row(sub_status=sub_status)]),
                    "c1", timezone.utc,
                )
                self.assertEqual(candidates[0]["state"], expected)

    def test_begin_time_and_end_time_fallback_keys_are_recognized(self):
        candidates = _collect_live_occurrence_candidates(
            _month_payload([_platform_row(
                start=None, end=None, date_text="2026-09-15")]),
            "c1", timezone.utc,
        )
        candidates[0]["row"]["begin_time"] = "14:00"
        candidates[0]["row"]["end_time"] = "15:40"
        refreshed = _collect_live_occurrence_candidates(
            _month_payload([candidates[0]["row"]]), "c1", timezone.utc
        )
        interval = _candidate_interval(refreshed[0])
        self.assertEqual(interval[0], datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc))
        self.assertEqual(interval[1], datetime(2026, 9, 15, 15, 40, tzinfo=timezone.utc))

    def test_bare_time_without_date_anchor_contributes_no_evidence(self):
        span = _parse_occurrence_span("14:00", timezone.utc)
        self.assertIsNone(span)
        anchored = _parse_occurrence_span("14:00", timezone.utc, "2026-09-15")
        self.assertEqual(anchored[0], datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc))

    def test_bare_times_bracketing_now_select_one_occurrence(self):
        payload = _month_days_payload(
            {"date": "2026-09-08",
             "course": [_platform_row(sub_id="s08", sub_status="2",
                                      start="10:00", end="12:00")]},
            {"date": "2026-09-15",
             "course": [_platform_row(sub_id="s15", sub_status="0",
                                      start="14:00", end="15:40")]},
        )
        candidates = _collect_live_occurrence_candidates(payload, "c1", timezone.utc)
        selected = _select_live_occurrence(candidates, NOW)
        self.assertEqual(selected["row"]["sub_id"], "s15")
        self.assertEqual(selected["state"], "upcoming")

    def test_two_bare_time_live_rows_still_fail_closed(self):
        payload = _month_days_payload(
            {"date": "2026-09-08",
             "course": [_platform_row(sub_id="s08", start="10:00", end="12:00")]},
            {"date": "2026-09-15",
             "course": [_platform_row(sub_id="s15", start="14:00", end="15:40")]},
        )
        candidates = _collect_live_occurrence_candidates(payload, "c1", timezone.utc)
        self.assertIsNone(_select_live_occurrence(candidates, NOW))

    def test_sub_title_date_prefix_anchors_bare_times(self):
        row = _platform_row(sub_status="1", start="14:00", end="15:40",
                            sub_title="2026-09-15第6-8节")
        candidates = _collect_live_occurrence_candidates(
            _month_payload([row]), "c1", timezone.utc
        )
        interval = _candidate_interval(candidates[0])
        self.assertEqual(interval[0], datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc))

    def test_unknown_sub_status_still_fails_closed(self):
        observation = _client(_month_payload([
            _platform_row(sub_status="mystery", start="14:00", end="15:40",
                          date_text="2026-09-15"),
        ])).get_live_course_observation("c1", now=NOW)
        self.assertEqual(observation["state"], "unknown")
        self.assertEqual(observation["code"], "live_unknown_observed")

    def test_nested_live_url_defaults_to_student_picture(self):
        live_url = {
            "output": {"m3u8": TEACHER_HLS, "m3u8_audio": AUDIO_HLS},
            "output_student": {"m3u8": STUDENT_HLS, "m3u8_audio": AUDIO_HLS},
        }
        payload = _month_payload([_platform_row(live_url=live_url)])
        _transport, url, headers = _client(payload).get_live_stream_params("c1")
        self.assertEqual(url, STUDENT_HLS)
        self.assertNotEqual(url, TEACHER_HLS)
        self.assertIn("Origin", headers)

    def test_nested_live_url_audio_fallback_without_video_views(self):
        live_url = {"output_student": {"m3u8_audio": AUDIO_HLS}}
        payload = _month_payload([_platform_row(live_url=live_url)])
        _transport, url, _headers = _client(payload).get_live_stream_params("c1")
        self.assertEqual(url, AUDIO_HLS)

    def test_nested_live_url_wins_over_legacy_flat_keys(self):
        live_url = {"output_student": {"m3u8": STUDENT_HLS}}
        row = _platform_row(live_url=live_url)
        row["hls_url"] = CURRENT_HLS
        payload = _month_payload([row])
        _transport, url, _headers = _client(payload).get_live_stream_params("c1")
        self.assertEqual(url, STUDENT_HLS)

    def test_two_hop_sub_info_resolves_stream_without_row_live_url(self):
        today = date.today()
        detail_payload = {
            "code": 0,
            "data": {
                "title": "synthetic-course", "realname": "teacher",
                "sub_list": {"2026": {"09": {
                    f"{today.day:02d}": [{
                        "id": "s99", "sub_title": f"{today.isoformat()}第6-8节",
                        "lecturer_name": "teacher", "playback_status": 0,
                    }],
                }}},
            },
        }
        sub_info_payload = {
            "code": 0,
            "data": {
                "sub_status": "1",
                "live_url": {
                    "output": {"m3u8": TEACHER_HLS},
                    "output_student": {"m3u8": STUDENT_HLS},
                },
            },
        }
        transport = _RecordingTransport(
            _month_payload([_platform_row(sub_id="s99", sub_status="1")]),
            detail_payload=detail_payload, sub_info_payload=sub_info_payload,
        )
        _transport, url, _headers = ICourseClient(transport).get_live_stream_params("c1")
        self.assertEqual(url, STUDENT_HLS)
        self.assertTrue(any("get-course-detail" in call for call, _ in transport.calls))
        self.assertTrue(any("get-sub-info" in call for call, _ in transport.calls))

    def test_two_hop_refuses_non_live_sub_status_even_with_live_url(self):
        today = date.today()
        detail_payload = {
            "code": 0,
            "data": {"sub_list": {"2026": {"09": {
                f"{today.day:02d}": [{
                    "id": "s99", "sub_title": f"{today.isoformat()}第6-8节",
                }],
            }}}},
        }
        sub_info_payload = {
            "code": 0,
            "data": {"sub_status": "2",
                     "live_url": {"output_student": {"m3u8": STUDENT_HLS}}},
        }
        transport = _RecordingTransport(
            _month_payload([_platform_row(sub_id="s99", sub_status="1")]),
            detail_payload=detail_payload, sub_info_payload=sub_info_payload,
        )
        with self.assertRaises(LiveRoomError) as stream_error:
            ICourseClient(transport).get_live_stream_params("c1")
        self.assertEqual(stream_error.exception.code, "live_stream_unavailable")

    def test_two_hop_surfaces_7001_gate_as_closed_playback_not_open(self):
        today = date.today()
        detail_payload = {
            "code": 0,
            "data": {"sub_list": {"2026": {"09": {
                f"{today.day:02d}": [{
                    "id": "s99", "sub_title": f"{today.isoformat()}第6-8节",
                }],
            }}}},
        }
        for extra in ({}, {"sub_status": "1"}):
            with self.subTest(sub_status=extra.get("sub_status")):
                # 闸门只清顶层字段、嵌套 content.playback.url 幸存（LIVESTUDY-1 考古）：
                # 平台给出的是确定性答复，必须以闭集码上抛，不得吞成泛化 not_observed
                sub_info_payload = {
                    "code": 7001, "msg": "视频未到开放时间",
                    "data": {**extra, "content": {"playback": {"url": "https://live.invalid/vod/lecture.mp4"}}},
                }
                transport = _RecordingTransport(
                    _month_payload([_platform_row(sub_id="s99", sub_status="1")]),
                    detail_payload=detail_payload, sub_info_payload=sub_info_payload,
                )
                with self.assertRaises(LiveRoomError) as gate_error:
                    ICourseClient(transport).get_live_stream_params("c1")
                self.assertEqual(gate_error.exception.code, "live_playback_not_open")

    def test_two_hop_keeps_not_observed_contract_when_gate_payload_empty(self):
        today = date.today()
        detail_payload = {
            "code": 0,
            "data": {"sub_list": {"2026": {"09": {
                f"{today.day:02d}": [{
                    "id": "s99", "sub_title": f"{today.isoformat()}第6-8节",
                }],
            }}}},
        }
        sub_info_payload = {"code": 7001, "msg": "视频未到开放时间", "data": {}}
        transport = _RecordingTransport(
            _month_payload([_platform_row(sub_id="s99", sub_status="1")]),
            detail_payload=detail_payload, sub_info_payload=sub_info_payload,
        )
        with self.assertRaises(LiveRoomError) as stream_error:
            ICourseClient(transport).get_live_stream_params("c1")
        self.assertEqual(stream_error.exception.code, "live_stream_unavailable")


class ViewAwareStreamParamsTests(unittest.TestCase):
    """直播二期乙1（POPUP-LIVE-FE-1）：闭集视角取流。

    默认视角保持既有解析序不变（嵌套解析序+legacy 平键+两跳探测）；
    非默认视角只对月度行嵌套 live_url 做严格分支匹配——分支缺失即如实
    不可用（live_stream_unavailable），与 available_views 派生同源同界。
    """

    def _live_row_client(self, live_url):
        return _client(_month_payload([_platform_row(live_url=live_url)]))

    def test_default_view_keeps_legacy_audio_fallback(self):
        live_url = {"output_student": {"m3u8_audio": AUDIO_HLS}}
        _transport, url, _headers = self._live_row_client(live_url).get_live_stream_params("c1")
        self.assertEqual(url, AUDIO_HLS)

    def test_non_default_views_match_strict_branches(self):
        live_url = {
            "output": {"m3u8": TEACHER_HLS, "m3u8_audio": AUDIO_HLS},
            "output_student": {"m3u8": STUDENT_HLS, "m3u8_audio": AUDIO_HLS},
        }
        client = self._live_row_client(live_url)
        for view, expected in (
            ("teacher", TEACHER_HLS),
            ("teacher_audio", AUDIO_HLS),
            ("student_audio", AUDIO_HLS),
        ):
            with self.subTest(view=view):
                _transport, url, _headers = client.get_live_stream_params("c1", view=view)
                self.assertEqual(url, expected)

    def test_non_default_view_without_branch_fails_closed(self):
        live_url = {"output_student": {"m3u8": STUDENT_HLS}}
        with self.assertRaises(LiveRoomError) as caught:
            self._live_row_client(live_url).get_live_stream_params("c1", view="teacher")
        self.assertEqual(caught.exception.code, "live_stream_unavailable")

    def test_non_default_view_ignores_legacy_flat_keys(self):
        row = _platform_row(live_url={"output_student": {"m3u8": STUDENT_HLS}})
        row["hls_url"] = CURRENT_HLS
        with self.assertRaises(LiveRoomError) as caught:
            _client(_month_payload([row])).get_live_stream_params("c1", view="teacher")
        self.assertEqual(caught.exception.code, "live_stream_unavailable")

    def test_unknown_view_is_closed_live_view_unknown(self):
        with self.assertRaises(LiveRoomError) as caught:
            self._live_row_client(None).get_live_stream_params("c1", view="invented")
        self.assertEqual(caught.exception.code, "live_view_unknown")

    def test_observation_carries_url_free_available_views_from_row(self):
        live_url = {
            "output": {"m3u8": TEACHER_HLS, "m3u8_audio": AUDIO_HLS},
            "output_student": {"m3u8": STUDENT_HLS},
        }
        payload = _month_payload([_platform_row(
            sub_status="1", start="14:00", end="15:40",
            date_text="2026-09-15", live_url=live_url,
        )])
        observation = _client(payload).get_live_course_observation("c1", now=NOW)
        self.assertEqual(
            observation["available_views"],
            ["teacher", "student", "teacher_audio"],
        )
        self.assertNotIn("http", str(observation["available_views"]))

    def test_observation_without_live_url_omits_view_metadata(self):
        payload = _month_payload([_platform_row(
            sub_status="1", start="14:00", end="15:40", date_text="2026-09-15",
        )])
        observation = _client(payload).get_live_course_observation("c1", now=NOW)
        self.assertNotIn("available_views", observation)


def _candidate_interval(candidate: dict) -> tuple:
    start_span, end_span = candidate["start_span"], candidate["end_span"]
    low = start_span[0] if start_span is not None else end_span[0]
    high = end_span[1] if end_span is not None else start_span[1]
    return (low, high)


if __name__ == "__main__":
    unittest.main()