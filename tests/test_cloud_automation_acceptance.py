import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from scripts.cloud_automation_acceptance import (
    STATE_SCHEMA,
    _advance,
    _fresh_state,
    _load_state,
    _next_schedule_time,
    _operation_id,
)


class CloudAutomationV3ContractTests(unittest.TestCase):
    """Lock the live acceptance driver (S09-L) to the v3 service contract."""

    def test_service_contract_exports_v3_fixed_schedule_and_disclosure(self):
        from src.runtime.automation import (
            CLOUD_DISCLOSURE_VERSION,
            CLOUD_PROTOCOL_VERSION,
            CLOUD_REQUIRED_SECRET_NAMES,
            CLOUD_SCHEDULE,
            CLOUD_VARIABLE_NAMES,
        )

        self.assertEqual(CLOUD_PROTOCOL_VERSION, "cloud-automation.v3")
        self.assertEqual(CLOUD_DISCLOSURE_VERSION, "cloud-custody-disclosure.v1")
        self.assertEqual(
            list(CLOUD_SCHEDULE["weekday_times"]),
            ["09:15", "10:10", "11:10", "12:05", "13:00",
             "14:45", "15:40", "16:40", "17:35", "18:30"],
        )
        self.assertEqual(list(CLOUD_SCHEDULE["daily_times"]), ["22:00"])
        self.assertEqual(CLOUD_SCHEDULE["timezone"], "Asia/Shanghai")
        self.assertNotIn("COURSELENS_CLOUD_CRON", CLOUD_VARIABLE_NAMES)
        self.assertEqual(
            set(CLOUD_REQUIRED_SECRET_NAMES),
            {
                "COURSELENS_CLOUD_STUDENT_ID",
                "COURSELENS_CLOUD_PASSWORD",
                "COURSELENS_CLOUD_RULES_JSON",
                "COURSELENS_CLOUD_STATE_KEY",
            },
        )

    def test_daily_workflow_declares_the_fixed_weekday_grid_and_nightly_fallback(self):
        import re

        workflow = (
            Path(__file__).resolve().parents[1]
            / "worker" / ".github" / "workflows" / "cloud-daily.yml"
        )
        crons = re.findall(r'- cron: "([^"]+)"', workflow.read_text(encoding="utf-8"))
        self.assertEqual(crons, [
            "15 1 * * 1-5", "10 2 * * 1-5", "10 3 * * 1-5", "5 4 * * 1-5",
            "0 5 * * 1-5", "45 6 * * 1-5", "40 7 * * 1-5", "40 8 * * 1-5",
            "35 9 * * 1-5", "30 10 * * 1-5", "0 14 * * *",
        ])


class CloudAutomationAcceptanceTests(unittest.TestCase):
    def test_schedule_uses_next_half_hour_with_twenty_minute_margin(self):
        zone = timezone(timedelta(hours=8))
        value, timestamp = _next_schedule_time(
            datetime(2026, 7, 27, 20, 53, tzinfo=zone)
        )
        self.assertEqual(value, "21:30")
        self.assertEqual(
            datetime.fromtimestamp(timestamp, zone),
            datetime(2026, 7, 27, 21, 30, tzinfo=zone),
        )

    def test_new_state_contains_only_opaque_session_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = _load_state(Path(directory) / "missing.json")
        self.assertEqual(state["schema"], STATE_SCHEMA)
        self.assertEqual(state["stage"], "new")
        self.assertRegex(state["session"], r"^[0-9a-f]{24}$")

    def test_clean_restart_retains_only_previous_attempt_digest(self):
        state = _fresh_state(previous_sha256="b" * 64)
        self.assertEqual(state["stage"], "new")
        self.assertEqual(state["previous_attempt_sha256"], "b" * 64)
        self.assertNotIn("schedule_time", state)

    def test_stage_updates_are_atomic_and_cannot_move_backwards(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = _load_state(path)
            _advance(path, state, "configured", course_hash="a" * 64)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["stage"], "configured")
            self.assertNotIn("account", persisted)
            with self.assertRaises(RuntimeError):
                _advance(path, state, "new")

    def test_manual_retry_uses_a_distinct_idempotency_key(self):
        state = _fresh_state()
        first = _operation_id(state, "run-now")
        state["manual_retry"] = 1
        state["operations"].pop("run-now")
        second = _operation_id(state, "run-now")
        self.assertNotEqual(first, second)
        self.assertTrue(second.endswith("-r1"))


if __name__ == "__main__":
    unittest.main()
