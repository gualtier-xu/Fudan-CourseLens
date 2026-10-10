from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from nacl.secret import SecretBox

from path_utils import PROJECT_ROOT
from src.runtime.automation import (
    AutomationService,
    CLOUD_DISCLOSURE_VERSION,
    CLOUD_PROTOCOL_VERSION,
    CLOUD_SCHEDULE,
    next_cloud_windows,
)
from tests.frontend_family import family_text
from src.remote.github_app import GitHubAppError
from src.remote.protocol import generate_box_keypair, generate_signing_keypair, seal_result
from src.runtime.http_api import TASK_ERROR_CODES, make_handler
from tests.http_services import http_services
from src.runtime.task_store import TaskStore


class MemoryCredentials:
    """Synthetic CredentialStore: same surface the upload action consumes."""

    def __init__(self):
        self.values = {}
        self.accounts = {}

    def save_secret(self, name, value):
        self.values[name] = str(value)

    def load_secret(self, name):
        if name not in self.values:
            raise KeyError(name)
        return self.values[name]

    def has_secret(self, name):
        return name in self.values

    def delete_secret(self, name):
        return self.values.pop(name, None) is not None

    def list_accounts(self):
        return [
            {"student_id": student_id, "requires_rotation": bool(item.get("requires_rotation"))}
            for student_id, item in self.accounts.items()
        ]

    def save_account(self, student_id, password):
        self.accounts[student_id] = {"password": password, "requires_rotation": False}

    def mark_rotation(self, student_id):
        self.accounts[student_id]["requires_rotation"] = True

    def load(self, student_id):
        account = self.accounts[student_id]
        if account.get("requires_rotation"):
            raise RuntimeError("rotation required")
        return student_id, str(account.get("password") or "")

    def has_deepseek_key(self):
        return bool(self.values.get("deepseek_api_key"))

    def load_deepseek_key(self):
        return self.values["deepseek_api_key"]


class FakeGitHub:
    def __init__(self):
        self.secrets = {}
        self.variables = {}
        self.workflows = {"cloud-verify.yml": False, "cloud-daily.yml": False}
        self.dispatched = []
        self.artifacts = []
        self.artifact_files = {}
        self.tree = "a" * 40

    def check_worker_integrity(self):
        return {
            "trusted": True,
            "repository": "synthetic-owner/courselens-worker-synthetic",
            "actual_tree": self.tree,
            "manifest_sha256": "b" * 64,
            "trust_epoch": 3,
        }

    def set_workflow_enabled(self, workflow, enabled):
        self.workflows[workflow] = bool(enabled)

    def put_worker_variable(self, name, value):
        self.variables[name] = str(value)

    def delete_worker_variable(self, name):
        self.variables.pop(name, None)

    def list_worker_variables(self):
        return dict(self.variables)

    def put_worker_secret(self, name, value):
        self.secrets[name] = str(value)

    def delete_worker_secret(self, name):
        self.secrets.pop(name, None)

    def list_worker_secrets(self):
        return [{"name": name, "updated_at": "2026-09-11T00:00:00Z"} for name in self.secrets]

    def dispatch_workflow(self, workflow, *, inputs=None, ref="main"):
        self.dispatched.append((workflow, dict(inputs or {}), ref, time.time()))

    def inspect_managed_resources(self):
        return {
            "secret_names": list(self.secrets),
            "variables": dict(self.variables),
            "workflows": {
                name: {"exists": True, "state": "active" if enabled else "disabled_manually"}
                for name, enabled in self.workflows.items()
            },
        }

    def list_workflow_runs(self, workflow, limit=20):
        return []

    def cancel_workflow_run(self, run_id):
        return None

    def list_worker_artifacts(self, **_kwargs):
        return list(self.artifacts)

    def delete_worker_artifact(self, artifact_id):
        self.artifacts = [item for item in self.artifacts if item["id"] != artifact_id]

    def download_worker_artifact_files(self, artifact_id):
        return dict(self.artifact_files[int(artifact_id)])


class AutomationServiceTests(unittest.TestCase):
    def setUp(self):
        cache = PROJECT_ROOT / "runtime" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=cache)
        self.store = TaskStore(Path(self.tmp.name) / "state.db")
        self.credentials = MemoryCredentials()
        self.credentials.save_account("2020001", "synthetic-password")
        self.credentials.save_secret("github_worker_repo", "synthetic-owner/courselens-worker-synthetic")
        # v3 固定包默认带 AI Key；需要“无 Key”语义的测试自行删除。
        self.credentials.save_secret("deepseek_api_key", "sk-synthetic")
        self.github = FakeGitHub()
        self.local_schedule = {"enabled": True, "time": "07:30"}
        self.catalog = {"36941": ["l-old-1", "l-old-2"]}
        self.service = AutomationService(
            self.store,
            self.credentials,
            lambda: self.github,
            local_schedule_getter=lambda: dict(self.local_schedule),
            local_schedule_setter=self._set_local_schedule,
            verified_catalog_getter=lambda: dict(self.catalog),
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _set_local_schedule(self, value):
        self.local_schedule = dict(value)
        return dict(value)

    def _configure(self, **overrides):
        body = {
            "account_id": "2020001",
            # AS3：与 automation.DEFAULT_BUDGET 同口径（20 讲/2000 分钟/200 万 token）
            "budget": {"max_lectures": 20, "max_runner_minutes": 2000, "max_deepseek_tokens": 2000000},
            "rules": [{"course_id": "36941"}],
        }
        body.update(overrides)
        return self.service.update_config(body)

    def _upload(self, **overrides):
        body = {
            "account_id": "2020001",
            "disclosure_version": CLOUD_DISCLOSURE_VERSION,
            "confirmed": True,
        }
        body.update(overrides)
        return self.service.upload_cloud_secrets(body)

    # ---- v1 fail-closed migration ----

    def test_legacy_v1_profile_migrates_fail_closed(self):
        self.store.save_automation_profile({
            "mode": "cloud", "state": "ready", "schedule_time": "07:30",
            "config_hash": "legacy" * 8, "verified_config_hash": "legacy" * 8,
            "verification": {"state": "verified"}, "protocol": "cloud-automation.v1",
            "account_id": "",
        })
        self.github.variables["COURSELENS_CLOUD_ENABLED"] = "true"
        self.github.workflows["cloud-daily.yml"] = True
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["schema"], "courselens.automation.v3")
        self.assertEqual(snapshot["state"], "disabled")
        self.assertEqual(snapshot["config_hash"], "")
        self.assertFalse(snapshot["verified"])
        profile = self.store.get_automation_profile()
        self.assertEqual(profile["protocol"], CLOUD_PROTOCOL_VERSION)
        self.assertEqual(profile["verified_config_hash"], "")
        # Best-effort remote fail-closed: legacy enabled flag is switched off.
        self.assertEqual(self.github.variables["COURSELENS_CLOUD_ENABLED"], "false")
        self.assertFalse(self.github.workflows["cloud-daily.yml"])
        events = self.store.list_remote_events(topics=["automation"], limit=10)
        self.assertTrue(any(
            item.get("payload", {}).get("state") == "migrated_fail_closed" for item in events
        ))

    def test_legacy_v2_profile_migrates_fail_closed_and_drops_rules(self):
        self.store.replace_automation_rules([{
            "course_id": "36941", "discovery_only": False, "summary": True,
        }])
        self.store.save_automation_profile({
            "mode": "cloud", "state": "ready",
            "config_hash": "legacy" * 8, "verified_config_hash": "legacy" * 8,
            "verification": {"state": "verified"}, "protocol": "cloud-automation.v2",
            "account_id": "2020001",
        })
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["schema"], "courselens.automation.v3")
        self.assertEqual(snapshot["state"], "disabled")
        # v3 selection requires a fresh verified baseline: old rules cannot
        # survive the migration.
        self.assertEqual(self.store.list_automation_rules(), [])

    # ---- v3 selection baseline ----

    def test_enabling_course_captures_verified_playable_baseline(self):
        self._configure()
        rules = self.store.list_automation_rules()
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertEqual(rule["selection_generation"], 1)
        self.assertEqual(rule["baseline"], ["l-old-1", "l-old-2"])
        self.assertNotIn("discovery_only", rule)
        self.assertNotIn("summary", rule)
        # A second course captures the next generation.
        self.catalog["77777"] = ["l-new-9"]
        self._configure(rules=[
            {"course_id": "36941"},
            {"course_id": "77777"},
        ])
        by_course = {
            item["course_id"]: item for item in self.store.list_automation_rules()
        }
        self.assertEqual(by_course["36941"]["selection_generation"], 1)
        self.assertEqual(by_course["77777"]["selection_generation"], 2)
        self.assertEqual(by_course["77777"]["baseline"], ["l-new-9"])

    def test_settings_change_preserves_baseline_and_generation(self):
        self._configure()
        first = self.store.list_automation_rules()[0]
        # Budget/priority changes while the course stays enabled must not
        # reset the generation or make old lectures eligible again.
        self._configure(rules=[
            {"course_id": "36941", "priority": 90, "max_lecture_minutes": 300},
        ])
        second = self.store.list_automation_rules()[0]
        self.assertEqual(second["selection_generation"], first["selection_generation"])
        self.assertEqual(second["baseline"], first["baseline"])
        self.assertEqual(second["priority"], 90)

    def test_reenabled_course_captures_fresh_baseline_and_generation(self):
        self._configure()
        self._configure(rules=[])  # disable: discovery stops
        self.assertEqual(self.store.list_automation_rules(), [])
        # New material appeared while the course was disabled.
        self.catalog["36941"] = ["l-old-1", "l-old-2", "l-appeared"]
        self._configure()  # re-enable: fresh baseline, fresh generation
        rule = self.store.list_automation_rules()[0]
        self.assertEqual(rule["selection_generation"], 2)
        self.assertEqual(rule["baseline"], ["l-appeared", "l-old-1", "l-old-2"])

    def test_unavailable_catalog_fails_closed_with_rules_unchanged(self):
        self._configure()
        before = self.store.list_automation_rules()
        self.catalog = {}
        with self.assertRaises(Exception) as caught:
            self._configure(rules=[{"course_id": "36941"}, {"course_id": "88888"}])
        self.assertEqual(caught.exception.code, "cloud_catalog_unavailable")
        self.assertEqual(self.store.list_automation_rules(), before)

    def test_unverified_course_fails_closed(self):
        with self.assertRaises(Exception) as caught:
            self._configure(rules=[{"course_id": "00000"}])
        self.assertEqual(caught.exception.code, "cloud_course_not_verified")
        self.assertEqual(self.store.list_automation_rules(), [])

    def test_oversized_baseline_is_rejected_never_truncated(self):
        self.catalog = {"36941": [f"l-{index:04d}" for index in range(401)]}
        with self.assertRaises(Exception) as caught:
            self._configure()
        self.assertEqual(caught.exception.code, "cloud_baseline_too_large")
        self.assertEqual(self.store.list_automation_rules(), [])

    def test_frontend_supplied_baseline_is_never_trusted(self):
        self._configure(rules=[
            {"course_id": "36941", "baseline": ["bogus"], "selection_generation": 99},
        ])
        # The stored baseline is the backend capture, never the frontend claim.
        rule = self.store.list_automation_rules()[0]
        self.assertEqual(rule["baseline"], ["l-old-1", "l-old-2"])
        self.assertEqual(rule["selection_generation"], 1)

    def test_upload_envelope_carries_selection_generations(self):
        self.credentials.save_secret("deepseek_api_key", "sk-synthetic")
        self._configure()
        self._upload()
        uploaded = json.loads(self.github.secrets["COURSELENS_CLOUD_RULES_JSON"])
        self.assertEqual(uploaded["schema"], CLOUD_PROTOCOL_VERSION)
        rule = uploaded["rules"][0]
        self.assertEqual(rule["baseline"], ["l-old-1", "l-old-2"])
        self.assertEqual(rule["selection_generation"], 1)
        self.assertNotIn("title", rule)
        self.assertNotIn("course_name", rule)

    # ---- configuration draft ----

    def test_config_draft_requires_saved_account_and_keeps_cloud_disabled(self):
        with self.assertRaises(Exception) as caught:
            self.service.update_config({"account_id": "missing", "rules": []})
        self.assertEqual(caught.exception.code, "cloud_account_missing")
        with self.assertRaises(Exception) as caught:
            self.service.update_config({"rules": [{"course_id": "1"}], "account_id": ""})
        self.assertEqual(caught.exception.code, "cloud_account_required")
        snapshot = self._configure()
        self.assertEqual(snapshot["state"], "disabled")
        self.assertEqual(snapshot["config_hash"], "")
        self.assertEqual(
            snapshot["schedule"]["times"],
            ["09:15", "10:10", "11:10", "12:05", "13:00",
             "14:45", "15:40", "16:40", "17:35", "18:30", "22:00"],
        )
        self.assertEqual(
            snapshot["schedule"]["weekday_times"],
            ["09:15", "10:10", "11:10", "12:05", "13:00",
             "14:45", "15:40", "16:40", "17:35", "18:30"],
        )
        self.assertEqual(snapshot["schedule"]["daily_times"], ["22:00"])
        self.assertEqual(snapshot["schedule"]["timezone"], "Asia/Shanghai")

    # ---- disclosure and encrypted upload ----

    def test_upload_requires_explicit_disclosure_version_and_confirmation(self):
        self._configure()
        for body in (
            {"account_id": "2020001"},
            {"account_id": "2020001", "disclosure_version": CLOUD_DISCLOSURE_VERSION},
            {"account_id": "2020001", "confirmed": True},
            {"account_id": "2020001", "disclosure_version": "cloud-custody-disclosure.v0", "confirmed": True},
        ):
            with self.assertRaises(Exception) as caught:
                self.service.upload_cloud_secrets(body)
            self.assertEqual(caught.exception.code, "cloud_disclosure_required")

    def test_upload_loads_saved_account_and_reads_back_secret_names(self):
        self.credentials.save_secret("deepseek_api_key", "sk-synthetic")
        self._configure()
        self._upload()
        self.assertEqual(self.github.secrets["COURSELENS_CLOUD_STUDENT_ID"], "2020001")
        self.assertEqual(self.github.secrets["COURSELENS_CLOUD_PASSWORD"], "synthetic-password")
        # Fixed dual-window contract: no CRON selector variable anymore.
        self.assertNotIn("COURSELENS_CLOUD_CRON", self.github.variables)
        self.assertEqual(self.github.variables["COURSELENS_CLOUD_PROTOCOL_VERSION"], CLOUD_PROTOCOL_VERSION)
        self.assertEqual(self.github.variables["COURSELENS_CLOUD_ENABLED"], "false")
        self.assertFalse(self.github.workflows["cloud-daily.yml"])
        profile = self.store.get_automation_profile()
        binding = profile["binding"]
        self.assertEqual(binding["worker_tree"], self.github.tree)
        self.assertEqual(binding["worker_repository"], "synthetic-owner/courselens-worker-synthetic")
        self.assertEqual(binding["disclosure_version"], CLOUD_DISCLOSURE_VERSION)
        self.assertEqual(binding["generation"], 1)
        self.assertEqual(profile["state"], "configuring")
        self.assertIn("secret_names_confirmed", profile["verification"])

    def test_upload_without_secret_name_readback_fails_closed(self):
        self.credentials.save_secret("deepseek_api_key", "sk-synthetic")
        self._configure()

        def incomplete_readback():
            return [{"name": "COURSELENS_CLOUD_STUDENT_ID"}]

        self.github.list_worker_secrets = incomplete_readback
        with self.assertRaises(Exception) as caught:
            self._upload()
        self.assertEqual(caught.exception.code, "cloud_secret_upload_incomplete")
        self.assertEqual(self.store.get_automation_profile()["state"], "disabled")

    def test_fixed_bundle_requires_local_deepseek_key(self):
        """v3 固定包：任何选中课程都需要 AI Key（总结/章节固定在内）。"""
        self._configure()
        self.credentials.delete_secret("deepseek_api_key")
        with self.assertRaises(Exception) as caught:
            self._upload()
        self.assertEqual(caught.exception.code, "deepseek_key_missing")
        self.credentials.save_secret("deepseek_api_key", "sk-synthetic")
        self._upload()
        self.assertEqual(self.github.secrets["COURSELENS_CLOUD_DEEPSEEK_API_KEY"], "sk-synthetic")

    def test_rules_never_carry_output_selector_labels(self):
        """v3 固定包：请求里的课程级输出选择器一律丢弃，规则只描述课程。

        quiz 保持手动；选中课程在云端固定请求字幕 ASR + 幻灯片 OCR +
        AI 总结/章节，规则不再携带任何输出开关。
        """
        self._configure(rules=[
            {
                "course_id": "36941", "discovery_only": False,
                "ocr": True, "summary": True, "chapters": True,
                "quiz_after_import": True, "only_new": False,
                "subtitle_mode": "automatic",
            },
        ])
        stored = self.store.list_automation_rules()
        self.assertEqual(len(stored), 1)
        for gone in (
            "discovery_only", "ocr", "summary", "chapters",
            "quiz_after_import", "only_new", "subtitle_mode",
        ):
            self.assertNotIn(gone, stored[0])

    def test_database_and_events_never_store_secret_values(self):
        self._configure(rules=[{"course_id": "36941", "discovery_only": False, "summary": True}])
        self.credentials.save_secret("deepseek_api_key", "sk-synthetic")
        self._upload()
        database = (Path(self.tmp.name) / "state.db").read_bytes()
        self.assertNotIn(b"synthetic-password", database)
        self.assertNotIn(b"sk-synthetic", database)
        snapshot = self.service.snapshot()
        self.assertNotIn("synthetic-password", repr(snapshot))
        self.assertNotIn("sk-synthetic", repr(snapshot))
        events = self.store.list_remote_events(topics=["automation"], limit=50)
        self.assertNotIn("synthetic-password", repr(events))
        self.assertNotIn("sk-synthetic", repr(events))
        rules = json.loads(self.github.secrets["COURSELENS_CLOUD_RULES_JSON"])
        self.assertEqual(rules["schema"], CLOUD_PROTOCOL_VERSION)
        self.assertNotIn("synthetic-password", json.dumps(rules))

    # ---- transactional enable ----

    def _verify_binding(self, digest=None):
        profile = self.store.get_automation_profile()
        digest = digest or profile["config_hash"]
        self.store.save_automation_profile({
            "verified_config_hash": digest,
            "verification": {"state": "verified", "source": "github_run", "observed_at": time.time()},
        })

    def test_enable_requires_matching_verified_config(self):
        self._configure()
        self._upload()
        with self.assertRaises(Exception) as caught:
            self.service.action("enable-cloud", operation_id="enable-12345678")
        self.assertEqual(caught.exception.code, "cloud_verification_required")
        self._verify_binding()
        operation = self.service.action("enable-cloud", operation_id="enable-12345679")
        self.assertEqual(operation["state"], "accepted")
        self.assertEqual(self.github.variables["COURSELENS_CLOUD_ENABLED"], "true")
        self.assertTrue(self.github.workflows["cloud-daily.yml"])
        self.assertFalse(self.local_schedule["enabled"])

    def test_enable_rolls_back_remote_flag_when_workflow_enable_fails(self):
        self._configure()
        self._upload()
        self._verify_binding()
        original = self.github.set_workflow_enabled

        def fail_once(workflow, enabled):
            if workflow == "cloud-daily.yml" and enabled:
                raise GitHubAppError("failed", code="github_request_failed")
            return original(workflow, enabled)

        self.github.set_workflow_enabled = fail_once
        with self.assertRaises(Exception):
            self.service.action("enable-cloud", operation_id="enable-rollback-1234")
        self.assertTrue(self.local_schedule["enabled"])
        self.assertEqual(self.github.variables["COURSELENS_CLOUD_ENABLED"], "false")
        self.assertFalse(self.github.workflows["cloud-daily.yml"])

    def test_enable_fails_closed_when_worker_tree_or_account_changed(self):
        self._configure()
        self._upload()
        self._verify_binding()
        self.github.tree = "f" * 40
        with self.assertRaises(Exception) as caught:
            self.service.action("enable-cloud", operation_id="enable-drift-12345")
        self.assertEqual(caught.exception.code, "cloud_binding_invalid")
        self.github.tree = "a" * 40  # restore tree; now only the account is broken
        self.credentials.mark_rotation("2020001")
        self._verify_binding()
        with self.assertRaises(Exception) as caught:
            self.service.action("enable-cloud", operation_id="enable-rotate-12345")
        self.assertEqual(caught.exception.code, "cloud_account_rotation_required")

    def test_enable_waits_for_in_flight_verification_then_succeeds(self):
        """真机时序缺陷根修钉：verify 派发后背靠背 enable 不再必败。

        真实 Actions run 需数十秒才 completed，enable 有界等待在途验证
        （reconcile 轮询导入结论）后走原判定成功；等待期不重复派发 verify。
        """
        self._configure()
        self._upload()
        key = bytes(range(SecretBox.KEY_SIZE))
        self.credentials.save_secret("cloud_state_key", base64.b64encode(key).decode("ascii"))
        signing_private, signing_public = generate_signing_keypair()
        self.credentials.save_secret("worker_signing_public_key", signing_public)
        digest = self.store.get_automation_profile()["config_hash"]
        self.service.action("verify-cloud-credentials", operation_id="verify-wait-000001")
        requested = self.store.get_automation_profile()["verification"]["requested_at"]
        created_at = datetime.utcfromtimestamp(requested + 2).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {"config_hash": digest, "protocol": CLOUD_PROTOCOL_VERSION, "verified_at": time.time()}
        from nacl.signing import SigningKey
        record["receipt"] = SigningKey(base64.b64decode(signing_private)).sign(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).signature.hex()
        self._seal_state_artifact(12, {
            "schema": "cloud.state.v1", "revision": 1,
            "verification": record, "updated_at": time.time(),
        }, key)
        verify_calls = {"count": 0}

        def conclude_on_second_reconcile(workflow, limit=20):
            if workflow != "cloud-verify.yml":
                return []
            verify_calls["count"] += 1
            if verify_calls["count"] < 2:
                return []
            return [self._run_row(910, created_at)]

        self.github.list_workflow_runs = conclude_on_second_reconcile
        self.service.verification_wait_interval_seconds = 0.01
        operation = self.service.action("enable-cloud", operation_id="enable-wait-000001")
        self.assertEqual(operation["state"], "accepted")
        self.assertEqual(self.store.get_automation_profile()["state"], "ready")
        self.assertEqual(self.github.variables["COURSELENS_CLOUD_ENABLED"], "true")
        # 首次 reconcile 时 run 未完成，等待期内至少再 reconcile 一次才拿到结论
        self.assertGreaterEqual(verify_calls["count"], 2)
        verify_dispatches = [item for item in self.github.dispatched if item[0] == "cloud-verify.yml"]
        self.assertEqual(len(verify_dispatches), 1)

    def test_enable_times_out_with_closed_code_when_verification_never_concludes(self):
        """超时路径钉：等待窗内验证未结论 → 闭集新码 cloud_verification_timeout。"""
        self._configure()
        self._upload()
        self.service.action("verify-cloud-credentials", operation_id="verify-timeout-0001")
        self.service.verification_wait_seconds = 0.05
        self.service.verification_wait_interval_seconds = 0.01
        with self.assertRaises(Exception) as caught:
            self.service.action("enable-cloud", operation_id="enable-timeout-0001")
        self.assertEqual(caught.exception.code, "cloud_verification_timeout")
        verify_dispatches = [item for item in self.github.dispatched if item[0] == "cloud-verify.yml"]
        self.assertEqual(len(verify_dispatches), 1)

    def test_cloud_setup_required_is_closed_set_code_with_coded_exception(self):
        """单元二钉：未授权派发的异常带显式闭集码 cloud_setup_required，
        经 _task_error_code 直达任务面，不再被启发式贴成 remote_failed。"""
        from src.application import CloudSetupRequired
        from src.runtime.http_api import _task_error_code
        self.assertIn("cloud_setup_required", TASK_ERROR_CODES)
        error = CloudSetupRequired("在线计算尚未完成 GitHub 授权")
        self.assertEqual(error.code, "cloud_setup_required")
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(_task_error_code(error), "cloud_setup_required")

    def test_dispatch_gate_codes_map_to_cloud_setup_required(self):
        """LIVE-VALIDATE-1 LV1-3 钉：派发前授权/连接拒绝按同一条学生事实归位。

        连接组件 authorization_missing（preflight 拒绝）与「尚未创建专属
        Worker」（GitHubAppError 显式码）都翻译成任务面 cloud_setup_required，
        不再被「github」兜底启发式贴成 remote_failed。"""
        from src.application import CourseLensApplication
        from src.remote.connection import RemoteConnectionError
        from src.remote.github_app import GitHubAppError
        from src.runtime.http_api import _task_error_code
        self.assertEqual(
            _task_error_code(RemoteConnectionError(
                "authorization_missing", "GitHub online service is not ready for a secure dispatch",
            )),
            "cloud_setup_required",
        )
        self.assertEqual(
            _task_error_code(GitHubAppError("尚未创建专属 Worker", code="cloud_setup_required")),
            "cloud_setup_required",
        )
        coded = GitHubAppError("尚未创建专属 Worker", code="cloud_setup_required")
        self.assertEqual(
            CourseLensApplication._question_error_code(coded),
            "cloud_setup_required",
            "疑问解释 worker 的派发闸拒绝不再落泛码 remote_answer_failed",
        )
        self.assertEqual(
            CourseLensApplication._question_error_code(RemoteConnectionError(
                "authorization_missing", "GitHub online service is not ready for a secure dispatch",
            )),
            "cloud_setup_required",
        )

    # ---- idempotent actions ----

    def test_duplicate_operation_id_is_idempotent(self):
        self._configure()
        self._upload()
        first = self.service.action("verify-cloud-credentials", operation_id="verify-12345678")
        second = self.service.action("verify-cloud-credentials", operation_id="verify-12345678")
        self.assertEqual(first["operation_id"], second["operation_id"])
        self.assertEqual(len(self.github.dispatched), 1)

    def test_operation_id_cannot_switch_actions(self):
        self._configure()
        self._upload()
        self.service.action("verify-cloud-credentials", operation_id="shared-op-123456")
        with self.assertRaises(Exception) as caught:
            self.service.action("enable-cloud", operation_id="shared-op-123456")
        self.assertEqual(caught.exception.code, "operation_id_conflict")

    # ---- update account ----

    def test_update_account_reuploads_reverifies_and_flags_resume(self):
        self._configure()
        self._upload()
        self._verify_binding()
        self.service.action("enable-cloud", operation_id="enable-update-1234")
        dispatches = len(self.github.dispatched)
        self.github.secrets.pop("COURSELENS_CLOUD_PASSWORD")
        operation = self.service.action("update-account", operation_id="update-acct-12345")
        self.assertEqual(operation["state"], "accepted")
        self.assertEqual(self.github.secrets["COURSELENS_CLOUD_PASSWORD"], "synthetic-password")
        self.assertEqual(self.github.variables["COURSELENS_CLOUD_ENABLED"], "false")
        self.assertFalse(self.github.workflows["cloud-daily.yml"])
        verify_dispatches = [item for item in self.github.dispatched[dispatches:] if item[0] == "cloud-verify.yml"]
        self.assertEqual(len(verify_dispatches), 1)
        profile = self.store.get_automation_profile()
        self.assertTrue(profile["binding"]["resume_after_verified"])
        self.assertEqual(profile["state"], "verifying")

    def test_update_account_requires_prior_configuration(self):
        with self.assertRaises(Exception) as caught:
            self.service.action("update-account", operation_id="update-none-12345")
        self.assertEqual(caught.exception.code, "cloud_config_incomplete")

    # ---- verification evidence ----

    def _seal_state_artifact(self, artifact_id, state, key):
        ciphertext = bytes(SecretBox(key).encrypt(json.dumps(state, sort_keys=True).encode("utf-8")))
        envelope = {
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "sha256": hashlib.sha256(ciphertext).hexdigest(),
        }
        self.github.artifact_files[artifact_id] = {
            "state.box.json": json.dumps(envelope).encode("utf-8"),
        }
        self.github.artifacts.append({
            "id": artifact_id, "name": f"courselens-cloud-state-{artifact_id}-1",
            "expired": False, "created_at": "2026-09-11T05:00:00Z", "workflow_run_id": artifact_id,
        })

    @staticmethod
    def _run_row(run_id, created_at):
        return {
            "id": run_id, "status": "completed", "conclusion": "success",
            "event": "workflow_dispatch", "created_at": created_at, "run_attempt": 1,
        }

    def test_verification_requires_signed_matching_evidence(self):
        self._configure()
        self._upload()
        key = bytes(range(SecretBox.KEY_SIZE))
        self.credentials.save_secret("cloud_state_key", base64.b64encode(key).decode("ascii"))
        signing_private, signing_public = generate_signing_keypair()
        self.credentials.save_secret("worker_signing_public_key", signing_public)
        profile = self.store.get_automation_profile()
        digest = profile["config_hash"]
        self.service.action("verify-cloud-credentials", operation_id="verify-ev-1234567")
        requested = self.store.get_automation_profile()["verification"]["requested_at"]
        created_at = datetime.utcfromtimestamp(requested + 2).strftime("%Y-%m-%dT%H:%M:%SZ")
        record = {
            "config_hash": digest, "protocol": CLOUD_PROTOCOL_VERSION, "verified_at": time.time(),
        }
        from nacl.signing import SigningKey
        record["receipt"] = SigningKey(base64.b64decode(signing_private)).sign(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).signature.hex()
        self._seal_state_artifact(8, {
            "schema": "cloud.state.v1", "revision": 1,
            "verification": record, "updated_at": time.time(),
        }, key)
        self.github.list_workflow_runs = lambda workflow, limit=20: (
            [self._run_row(81, created_at)] if workflow == "cloud-verify.yml" else []
        )
        self.service.reconcile(force=True)
        profile = self.store.get_automation_profile()
        self.assertEqual(profile["verified_config_hash"], digest)

    def test_verification_run_success_without_evidence_stays_unverified(self):
        self._configure()
        self._upload()
        key = bytes(range(SecretBox.KEY_SIZE))
        self.credentials.save_secret("cloud_state_key", base64.b64encode(key).decode("ascii"))
        self.service.action("verify-cloud-credentials", operation_id="verify-noev-12345")
        requested = self.store.get_automation_profile()["verification"]["requested_at"]
        created_at = datetime.utcfromtimestamp(requested + 2).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.github.list_workflow_runs = lambda workflow, limit=20: (
            [self._run_row(82, created_at)] if workflow == "cloud-verify.yml" else []
        )
        self.service.reconcile(force=True)
        profile = self.store.get_automation_profile()
        self.assertEqual(profile["verified_config_hash"], "")

    def test_stale_requested_verification_expires_to_timeout(self):
        """第卅一案：requested 的 90s 窗口此前只写不执行——dispatch 落空时
        胶囊被永久钉在「正在确认」。过期必须收敛为失败+闭集码，重试动作保留。"""
        self._configure()
        self._upload()
        self.service.action("verify-cloud-credentials", operation_id="verify-exp-12345")
        stored = self.store.get_automation_profile()
        verification = dict(stored["verification"])
        verification["requested_at"] = float(verification["requested_at"]) - 91.0
        self.store.save_automation_profile({"verification": verification})
        self.github.list_workflow_runs = lambda workflow, limit=20: []
        snapshot = self.service.reconcile(force=True)
        profile = self.store.get_automation_profile()
        self.assertEqual(profile["verification"]["state"], "failed")
        self.assertEqual(profile["verification"]["code"], "cloud_verification_timeout")
        self.assertNotEqual(
            snapshot["state"], "verifying", "过期验证不得再呈「正在确认」",
        )
        self.assertIn(
            "verify-cloud-credentials", snapshot["actions"], "重试动作保留在动作面",
        )

    def test_fresh_requested_verification_stays_pending_within_window(self):
        """第卅一案：窗口内（<90s）的 requested 不得被提前翻转。"""
        self._configure()
        self._upload()
        self.service.action("verify-cloud-credentials", operation_id="verify-fresh-12345")
        self.github.list_workflow_runs = lambda workflow, limit=20: []
        snapshot = self.service.reconcile(force=True)
        profile = self.store.get_automation_profile()
        self.assertEqual(profile["verification"]["state"], "requested")
        self.assertEqual(snapshot["state"], "verifying")

    # ---- 90s 窗边界四点（E2E-2v2 定谳缺陷 A：过期兜底毒性写入丢 requested_at）----

    def _shift_requested_at(self, delta):
        """把 requested_at 平移 delta 秒，模拟真实墙钟流逝（既有窗口族套路）。"""
        stored = self.store.get_automation_profile()
        verification = dict(stored["verification"])
        verification["requested_at"] = float(verification["requested_at"]) + delta
        self.store.save_automation_profile({"verification": verification})
        return float(verification["requested_at"])

    def test_verification_window_0s_stays_requested(self):
        """边界点 0s：刚派发必须保持 requested，不提前翻转、字段不丢。"""
        self._configure()
        self._upload()
        self.service.action("verify-cloud-credentials", operation_id="verify-w0-1234567")
        self.github.list_workflow_runs = lambda workflow, limit=20: []
        self.service.reconcile(force=True)
        profile = self.store.get_automation_profile()
        self.assertEqual(profile["verification"]["state"], "requested")
        self.assertGreater(float(profile["verification"].get("requested_at") or 0), 0)

    def test_verification_window_89s_stays_requested(self):
        """边界点 89s：窗内（<90s）不得收敛，requested_at 原样保留。"""
        self._configure()
        self._upload()
        self.service.action("verify-cloud-credentials", operation_id="verify-w89-123456")
        self._shift_requested_at(-89.0)
        self.github.list_workflow_runs = lambda workflow, limit=20: []
        self.service.reconcile(force=True)
        profile = self.store.get_automation_profile()
        self.assertEqual(profile["verification"]["state"], "requested")
        self.assertGreater(float(profile["verification"].get("requested_at") or 0), 0)

    def test_verification_window_91s_expires_but_keeps_requested_at(self):
        """边界点 91s：过窗收敛 failed/local_expiry/cloud_verification_timeout，
        但 requested_at 必须保留——E2E-2v2 缺陷 A 的毒性写入钉：丢字段=迟到
        run 结论（真机 105s/236s）永久不可翻转，enable 第 3 门被锁死。"""
        self._configure()
        self._upload()
        self.service.action("verify-cloud-credentials", operation_id="verify-w91-123456")
        requested_at = self._shift_requested_at(-91.0)
        self.github.list_workflow_runs = lambda workflow, limit=20: []
        self.service.reconcile(force=True)
        profile = self.store.get_automation_profile()
        self.assertEqual(profile["verification"]["state"], "failed")
        self.assertEqual(profile["verification"]["source"], "local_expiry")
        self.assertEqual(profile["verification"]["code"], "cloud_verification_timeout")
        self.assertEqual(
            float(profile["verification"].get("requested_at") or 0), requested_at,
            "过期收敛不得丢 requested_at（run 结论翻转依赖此字段）",
        )

    def test_verification_window_150s_late_run_still_flips(self):
        """边界点 150s：run 收尾晚于 90s 窗时，迟到成功结论+匹配签名回执仍可
        翻转 verified——E2E-2v2 真机锁死 enable 第 3 门的两拍复现（先过窗
        收敛、后 run 完成），证明过期兜底不再毒化翻转条件链。"""
        self._configure()
        self._upload()
        key = bytes(range(SecretBox.KEY_SIZE))
        self.credentials.save_secret("cloud_state_key", base64.b64encode(key).decode("ascii"))
        signing_private, signing_public = generate_signing_keypair()
        self.credentials.save_secret("worker_signing_public_key", signing_public)
        digest = self.store.get_automation_profile()["config_hash"]
        self.service.action("verify-cloud-credentials", operation_id="verify-w150-12345")
        # 第一拍：run 未收尾即过窗（91s）→ 收敛 failed/local_expiry。
        requested_at = self._shift_requested_at(-91.0)
        self.github.list_workflow_runs = lambda workflow, limit=20: []
        self.service.reconcile(force=True)
        profile = self.store.get_automation_profile()
        self.assertEqual(profile["verification"]["code"], "cloud_verification_timeout")
        self.assertEqual(float(profile["verification"].get("requested_at") or 0), requested_at)
        # 第二拍（150s 级墙钟）：run 已收尾 success，签名回执与 config 匹配。
        record = {
            "config_hash": digest, "protocol": CLOUD_PROTOCOL_VERSION, "verified_at": time.time(),
        }
        from nacl.signing import SigningKey
        record["receipt"] = SigningKey(base64.b64decode(signing_private)).sign(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).signature.hex()
        self._seal_state_artifact(9, {
            "schema": "cloud.state.v1", "revision": 1,
            "verification": record, "updated_at": time.time(),
        }, key)
        created_at = datetime.utcfromtimestamp(requested_at + 2).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.github.list_workflow_runs = lambda workflow, limit=20: (
            [self._run_row(92, created_at)] if workflow == "cloud-verify.yml" else []
        )
        self.service.reconcile(force=True)
        profile = self.store.get_automation_profile()
        self.assertEqual(profile["verification"]["state"], "verified")
        self.assertEqual(profile["verified_config_hash"], digest)

    def test_evidence_missing_then_receipt_arrives_still_flips(self):
        """E2E-2v4 定谳缺陷：conclusion 分支毒性写入丢 requested_at——真机
        实证 run A success+回执 artifact 在场，但首拍收割先于 state artifact
        上架（listing 滞后）判 evidence_missing 且丢字段，此后窗口门
        （requested_at>0）永久关死，迟到回执不可翻转=毒化。语义同 E2E-2v2
        缺陷 A：本地判定不是高于 run 真值的终审，requested_at 必须穿透保留。"""
        self._configure()
        self._upload()
        key = bytes(range(SecretBox.KEY_SIZE))
        self.credentials.save_secret("cloud_state_key", base64.b64encode(key).decode("ascii"))
        signing_private, signing_public = generate_signing_keypair()
        self.credentials.save_secret("worker_signing_public_key", signing_public)
        digest = self.store.get_automation_profile()["config_hash"]
        self.service.action("verify-cloud-credentials", operation_id="verify-poison-12345")
        requested_at = float(
            self.store.get_automation_profile()["verification"]["requested_at"]
        )
        created_at = datetime.utcfromtimestamp(requested_at + 2).strftime("%Y-%m-%dT%H:%M:%SZ")
        # 第一拍：run A 已收尾 success，回执尚未在场（artifact 上架滞后）。
        self.github.list_workflow_runs = lambda workflow, limit=20: (
            [self._run_row(93, created_at)] if workflow == "cloud-verify.yml" else []
        )
        self.service.reconcile(force=True)
        profile = self.store.get_automation_profile()
        self.assertEqual(profile["verification"]["state"], "failed")
        self.assertEqual(
            profile["verification"]["code"], "cloud_verification_evidence_missing",
        )
        self.assertEqual(
            float(profile["verification"].get("requested_at") or 0), requested_at,
            "evidence_missing 收敛不得丢 requested_at（迟到回执翻转依赖此字段）",
        )
        # 第二拍：回执 artifact 到场（同 config_hash + 签名）→ 必须翻转
        # verified；丢字段时收割窗口门关死，毒化复现为永久 evidence_missing。
        record = {
            "config_hash": digest, "protocol": CLOUD_PROTOCOL_VERSION, "verified_at": time.time(),
        }
        from nacl.signing import SigningKey
        record["receipt"] = SigningKey(base64.b64decode(signing_private)).sign(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).signature.hex()
        self._seal_state_artifact(10, {
            "schema": "cloud.state.v1", "revision": 1,
            "verification": record, "updated_at": time.time(),
        }, key)
        self.service.reconcile(force=True)
        profile = self.store.get_automation_profile()
        self.assertEqual(profile["verification"]["state"], "verified")
        self.assertEqual(profile["verified_config_hash"], digest)

    def test_skipped_schedule_runs_are_never_imported_and_purged(self):
        """第卅二案①：定窗空转（skipped）不入台账且存量行被清扫——任务中心
        不再被「自动材料·计划窗口·进行中」的陈旧行占屏。"""
        self._configure()
        self._upload()
        self.store.upsert_automation_run(
            "cloud-daily.yml:1:1", github_run_id=1, workflow="cloud-daily.yml",
            trigger_kind="schedule", state="completed", conclusion="skipped",
        )
        created_at = datetime.utcfromtimestamp(time.time() - 3600).strftime("%Y-%m-%dT%H:%M:%SZ")
        skipped_row = {
            "id": 2, "status": "completed", "conclusion": "skipped",
            "event": "schedule", "created_at": created_at, "run_attempt": 1,
        }
        self.github.list_workflow_runs = lambda workflow, limit=20: (
            [skipped_row] if workflow == "cloud-daily.yml" else []
        )
        self.service.reconcile(force=True)
        rows = self.store.list_automation_runs(limit=50)
        self.assertNotIn(
            "cloud-daily.yml:1:1", [row["run_key"] for row in rows], "存量空转行必须被清扫",
        )
        self.assertFalse(
            any(row.get("conclusion") == "skipped" for row in rows), "空转 run 不得再入台账",
        )

    def test_verifying_freezes_daily_schedule(self):
        """第卅二案③：验证请求在途=定窗冻结（fail-closed），不得按旧配置夜跑。"""
        self._configure()
        self._upload()
        self.service.action("verify-cloud-credentials", operation_id="verify-freeze-123")
        self.github.workflows["cloud-daily.yml"] = True
        self.service.reconcile(force=True)
        self.assertFalse(
            self.github.workflows["cloud-daily.yml"], "requested 在途必须冻结定窗",
        )
        self.assertEqual(
            self.store.get_automation_profile()["verification"]["state"], "requested",
            "冻结只关排程，不篡改验证请求本身",
        )

    def test_config_hash_mismatch_freezes_daily_schedule(self):
        """第卅二案②④：本地规则改动未成功上传（worker 摘要≠本地摘要）时定窗
        保持冻结——worker 不得按旧课程配置夜跑；resume_after_verified 只在
        摘要一致且 verified 后复启（既有链）。"""
        self._configure()
        self._upload()
        self.github.variables["COURSELENS_CLOUD_CONFIG_HASH"] = "stale-worker-digest"
        self.github.workflows["cloud-daily.yml"] = True
        self.service.reconcile(force=True)
        self.assertFalse(
            self.github.workflows["cloud-daily.yml"], "摘要失配必须冻结定窗",
        )

    # ---- reconcile binding invalidation and resume ----

    def test_reconcile_disables_processing_when_binding_invalidated(self):
        self._configure()
        self._upload()
        self._verify_binding()
        self.github.variables["COURSELENS_CLOUD_ENABLED"] = "true"
        self.github.workflows["cloud-daily.yml"] = True
        self.github.tree = "c" * 40  # worker tree changed after verification
        self.service.reconcile(force=True)
        self.assertEqual(self.github.variables["COURSELENS_CLOUD_ENABLED"], "false")
        self.assertFalse(self.github.workflows["cloud-daily.yml"])
        profile = self.store.get_automation_profile()
        self.assertEqual(profile["verified_config_hash"], "")

    def test_reconcile_resumes_only_after_verified_evidence(self):
        self._configure()
        self._upload()
        self._verify_binding()
        self.service.action("enable-cloud", operation_id="enable-resume-1234")
        self.service.action("update-account", operation_id="update-res-123456")
        self.github.variables["COURSELENS_CLOUD_ENABLED"] = "false"
        self.github.workflows["cloud-daily.yml"] = False
        # Simulate the imported verification receipt for the new config hash.
        key = bytes(range(SecretBox.KEY_SIZE))
        self.credentials.save_secret("cloud_state_key", base64.b64encode(key).decode("ascii"))
        signing_private, signing_public = generate_signing_keypair()
        self.credentials.save_secret("worker_signing_public_key", signing_public)
        digest = self.store.get_automation_profile()["config_hash"]
        record = {"config_hash": digest, "protocol": CLOUD_PROTOCOL_VERSION, "verified_at": time.time()}
        from nacl.signing import SigningKey
        record["receipt"] = SigningKey(base64.b64decode(signing_private)).sign(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).signature.hex()
        self._seal_state_artifact(9, {
            "schema": "cloud.state.v1", "revision": 2,
            "verification": record, "updated_at": time.time(),
        }, key)
        requested = self.store.get_automation_profile()["verification"]["requested_at"]
        created_at = datetime.utcfromtimestamp(requested + 2).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.github.list_workflow_runs = lambda workflow, limit=20: (
            [self._run_row(91, created_at)] if workflow == "cloud-verify.yml" else []
        )
        self.service.reconcile(force=True)
        self.assertEqual(self.github.variables["COURSELENS_CLOUD_ENABLED"], "true")
        self.assertTrue(self.github.workflows["cloud-daily.yml"])
        self.assertEqual(self.store.get_automation_profile()["state"], "ready")

    # ---- run now / fixed schedule ----

    def test_run_now_requires_verified_config(self):
        self._configure()
        self._upload()
        with self.assertRaises(Exception) as caught:
            self.service.action("run-now", operation_id="run-now-12345678")
        self.assertEqual(caught.exception.code, "cloud_verification_required")
        self._verify_binding()
        self.service.action("run-now", operation_id="run-now-12345679")
        manual = [item for item in self.github.dispatched if item[0] == "cloud-daily.yml"]
        self.assertEqual(len(manual), 1)
        self.assertEqual(manual[0][1].get("trigger_kind"), "manual")

    def test_snapshot_next_two_windows_are_fixed_beijing_times(self):
        zone = timezone(timedelta(hours=8))
        # 2026-09-11 is a Friday: the weekday class grid is live.
        windows = next_cloud_windows(now=datetime(2026, 9, 11, 12, 0, tzinfo=zone))
        parsed = [datetime.fromisoformat(value) for value in windows]
        self.assertEqual(parsed[0].hour, 12)
        self.assertEqual(parsed[0].minute, 5)
        self.assertEqual(parsed[1].hour, 13)
        self.assertEqual(parsed[0].tzinfo.utcoffset(parsed[0]), timedelta(hours=8))
        later = next_cloud_windows(now=datetime(2026, 9, 11, 23, 0, tzinfo=zone))
        first = datetime.fromisoformat(later[0])
        self.assertEqual(first.hour, 22)
        self.assertEqual(first.day, 12)
        # Weekend: the class grid sleeps, only the nightly fallback remains.
        self.assertEqual(first.weekday(), 5)
        weekend = next_cloud_windows(now=datetime(2026, 9, 12, 10, 0, tzinfo=zone))
        weekend_parsed = [datetime.fromisoformat(value) for value in weekend]
        self.assertEqual(weekend_parsed[0].day, 12)
        self.assertEqual(weekend_parsed[0].hour, 22)
        self.assertEqual(weekend_parsed[1].day, 13)
        self.assertEqual(weekend_parsed[1].hour, 22)
        # Saturday night after the fallback: the only remaining window of the
        # day is past, yet next_two still reaches Monday's first grid point.
        saturday_night = next_cloud_windows(now=datetime(2026, 9, 12, 22, 30, tzinfo=zone))
        night_parsed = [datetime.fromisoformat(value) for value in saturday_night]
        self.assertEqual(len(night_parsed), 2)
        self.assertEqual(night_parsed[0].day, 13)
        self.assertEqual(night_parsed[0].hour, 22)
        self.assertEqual(night_parsed[1].day, 14)
        self.assertEqual(night_parsed[1].hour, 9)
        self.assertEqual(night_parsed[1].minute, 15)

    # ---- revoke ----

    def test_revoke_does_not_delete_secrets_until_active_run_is_terminal(self):
        self._configure()
        self._upload()
        self.github.secrets["COURSELENS_CLOUD_PASSWORD"] = "encrypted-by-github"

        def active_runs(workflow, limit=20):
            if workflow == "cloud-daily.yml":
                return [{"id": 95, "status": "in_progress"}]
            return []

        self.github.list_workflow_runs = active_runs
        with patch("src.runtime.automation.time.monotonic", side_effect=[0.0, 21.0]):
            with self.assertRaises(Exception):
                self.service.action(
                    "revoke-cloud-credentials", operation_id="revoke-wait-1234"
                )
        self.assertIn("COURSELENS_CLOUD_PASSWORD", self.github.secrets)
        self.assertEqual(self.service.snapshot()["state"], "cleanup_pending")

    def test_revoke_deletes_exact_names_artifacts_and_verifies_absence(self):
        self._configure()
        self._upload()
        self.github.artifacts = [{
            "id": 55, "name": "courselens-cloud-result-55-1", "expired": False,
        }]
        operation = self.service.action("revoke-cloud-credentials", operation_id="revoke-ok-123456")
        self.assertEqual(operation["state"], "accepted")
        self.assertEqual(self.github.secrets, {})
        self.assertNotIn("COURSELENS_CLOUD_ENABLED", self.github.variables)
        self.assertFalse(self.github.workflows["cloud-daily.yml"])
        self.assertEqual(self.github.artifacts, [])
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["state"], "disabled")
        self.assertEqual(snapshot["config_hash"], "")

    # ---- imports ----

    def test_available_cloud_result_is_verified_imported_and_deleted_idempotently(self):
        result_private, result_public = generate_box_keypair()
        signing_private, signing_public = generate_signing_keypair()
        self.credentials.save_secret("cloud_result_private_key", result_private)
        self.credentials.save_secret("worker_signing_public_key", signing_public)
        imported = []
        self.service.result_importer = lambda value: imported.append(value)
        result = {
            "schema": "result.v2", "protocol_version": "2",
            "task_id": "a" * 32, "job_kind": "learning_pack",
            "input_hash": "b" * 64, "pipeline_fingerprint": CLOUD_PROTOCOL_VERSION,
            "status": "completed", "outputs": {}, "metrics": {}, "warnings": [],
        }
        envelope = seal_result(result, result_public, signing_private)
        envelope["input_hash"] = result["input_hash"]
        self.github.artifacts = [{
            "id": 7, "name": "courselens-cloud-result-7-1", "expired": False,
            "created_at": "2026-09-11T05:00:00Z", "workflow_run_id": 7,
            "expires_at": "2026-12-10T05:00:00Z",
        }]
        self.github.artifact_files[7] = {
            "result.box.json": json.dumps(envelope).encode("utf-8")
        }
        self.store.upsert_automation_import(7, artifact_name=self.github.artifacts[0]["name"], state="available")
        self.service._auto_import_available(self.github)
        self.assertEqual(len(imported), 1)
        self.assertEqual(self.store.get_automation_import(7)["state"], "imported")
        self.assertEqual(self.github.artifacts, [])
        # History projection stays closed-set: no artifact names, no payloads.
        snapshot = self.service.snapshot()
        projected = [item for item in snapshot["imports"] if item["artifact_id"] == 7]
        self.assertEqual(projected[0]["state"], "imported")
        self.assertNotIn("artifact_name", projected[0])

    def test_replayed_cloud_state_revision_is_ignored(self):
        key = bytes(range(SecretBox.KEY_SIZE))
        self.credentials.save_secret("cloud_state_key", base64.b64encode(key).decode("ascii"))

        def encoded(revision, lectures):
            state = {
                "schema": "cloud.state.v1", "revision": revision,
                "budget": {
                    "date": "2026-09-11", "lectures": lectures,
                    "runner_minutes": 0, "deepseek_tokens": 0,
                },
                "circuits": {}, "last_run": {}, "updated_at": float(revision),
            }
            ciphertext = bytes(SecretBox(key).encrypt(json.dumps(state).encode("utf-8")))
            return json.dumps({
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                "sha256": hashlib.sha256(ciphertext).hexdigest(),
            }).encode("utf-8")

        self.github.artifact_files[8] = {"state.box.json": encoded(2, 2)}
        self.github.artifact_files[9] = {"state.box.json": encoded(1, 9)}
        self.service._import_cloud_state(self.github, 8)
        self.service._import_cloud_state(self.github, 9)
        self.assertEqual(self.store.get_automation_budget("2026-09-11")["lectures"], 2)
        self.assertEqual(
            self.store.get_app_state("automation_last_cloud_state")["revision"], 2
        )

    def test_fresh_chain_wallclock_revision_imports_verification_evidence(self):
        # REVOKE-REVISION-FIX 契约钉（E2E-2v5 砖化场景客户端面）：撤销删净仓内
        # state artifact 后，worker 新链首件以墙钟毫秒为 revision；历史 daily
        # 导入留下的计数器天花板（prior=2，app state 持久化跨重启存活）必须
        # 放行它，verify 回执落地可翻转。同链更小的毫秒 revision（旧件迟到）
        # 仍被天花板拒收——staleness 防护零弱化。
        key = bytes(range(SecretBox.KEY_SIZE))
        self.credentials.save_secret("cloud_state_key", base64.b64encode(key).decode("ascii"))
        signing_private, signing_public = generate_signing_keypair()
        self.credentials.save_secret("worker_signing_public_key", signing_public)
        digest = "d" * 64
        record = {"config_hash": digest, "protocol": CLOUD_PROTOCOL_VERSION, "verified_at": time.time()}
        from nacl.signing import SigningKey
        record["receipt"] = SigningKey(base64.b64decode(signing_private)).sign(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).signature.hex()
        fresh_revision = int(time.time() * 1000)
        self.store.set_app_state("automation_last_cloud_state", {
            "artifact_id": 1, "revision": 2, "budget_date": "2026-09-10",
            "code": "cloud_daily_completed", "counts": {}, "elapsed_seconds": 1.0,
            "verification": {}, "observed_at": time.time() - 3600,
        })
        self._seal_state_artifact(12, {
            "schema": "cloud.state.v1", "revision": fresh_revision,
            "budget": {"date": "2026-09-11", "lectures": 0, "runner_minutes": 0.0, "deepseek_tokens": 0},
            "circuits": {}, "last_run": {}, "verification": record,
            "updated_at": fresh_revision / 1000.0,
        }, key)
        self.service._import_cloud_state(self.github, 12)
        imported = self.store.get_app_state("automation_last_cloud_state")
        self.assertEqual(imported["revision"], fresh_revision)
        self.assertTrue(self.service._verification_evidence_matches(digest))
        # 同链 staleness 防护保持：更小的毫秒 revision 不入库、不覆盖回执。
        self._seal_state_artifact(13, {
            "schema": "cloud.state.v1", "revision": fresh_revision - 1,
            "budget": {"date": "2026-09-11", "lectures": 9, "runner_minutes": 0.0, "deepseek_tokens": 0},
            "circuits": {}, "last_run": {}, "verification": {}, "updated_at": 0.0,
        }, key)
        self.service._import_cloud_state(self.github, 13)
        self.assertEqual(
            self.store.get_app_state("automation_last_cloud_state")["revision"],
            fresh_revision,
        )

    def test_degraded_remote_circuit_exposes_explicit_reset_action(self):
        self._configure()
        self.store.set_automation_circuit(
            "deepseek", state="degraded", consecutive_failures=1,
            last_error_code="deepseek_transient",
        )
        self.assertIn("reset-circuit", self.service.snapshot()["actions"])

    def test_snapshot_exposes_closed_evidence_and_local_availability(self):
        self._configure()
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["schema"], "courselens.automation.v3")
        self.assertEqual(snapshot["disclosure_version"], CLOUD_DISCLOSURE_VERSION)
        self.assertEqual(snapshot["retention"], {"result_days": 30, "state_days": 90, "ceiling_days": 90})
        self.credentials.delete_secret("deepseek_api_key")
        self.assertFalse(self.service.snapshot()["ai_key_available"])
        self.assertIsNone(snapshot["target"])
        self.credentials.save_secret("deepseek_api_key", "sk-synthetic")
        self.assertIsNotNone(self.service.snapshot()["ai_key_available"])

    def test_snapshot_hides_quota_numbers_until_a_circuit_triggers(self):
        self._configure()
        snapshot = self.service.snapshot()
        self.assertIsNone(
            snapshot["budget"]["used"], "正常状态不得展示配额数字",
        )
        self.assertNotIn("runner_stop_threshold", snapshot["budget"])
        projected = snapshot["rules"][0]
        self.assertNotIn("baseline", projected, "规则投影不得携带 baseline 明细")
        self.assertEqual(projected["baseline_count"], 2)
        # A triggered circuit surfaces the used numbers beside the deferred
        # closed-set reason; the state still degrades instead of failing.
        self.store.set_automation_circuit(
            "budget", state="open", last_error_code="budget_exhausted",
        )
        triggered = self.service.snapshot()
        self.assertEqual(triggered["budget"]["used"]["lectures"], 0)
        self.assertTrue(
            any(item["circuit"] == "budget" and item["state"] == "open"
                for item in triggered["circuits"])
        )


class AutomationRunRecordTests(unittest.TestCase):
    """第卅六案①③：automation_runs 仅追加 started_at/concluded_at（幂等迁移）、
    ghost 收敛、run 自身时刻投影、终态记录删除。"""

    def setUp(self):
        cache = PROJECT_ROOT / "runtime" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=cache)
        self.path = Path(self.tmp.name) / "state.db"
        self.store = TaskStore(self.path)
        self.credentials = MemoryCredentials()
        self.github = FakeGitHub()
        self.local_schedule = {"enabled": True, "time": "07:30"}
        self.service = AutomationService(
            self.store,
            self.credentials,
            lambda: self.github,
            local_schedule_getter=lambda: dict(self.local_schedule),
            local_schedule_setter=lambda value: dict(value),
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _age_run(self, run_key, *, seconds):
        """模拟「不再被观测到」：把 updated_at/observed_at 推到过去。"""
        moment = time.time() - seconds
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE automation_runs SET updated_at=?, observed_at=? WHERE run_key=?",
                (moment, moment, run_key),
            )

    def test_legacy_table_gains_run_time_columns_and_stays_migratable(self):
        """旧表（无 run 时刻列）首次打开只追加两列，历史行原样保留；再打开幂等。"""
        legacy_key = "cloud-daily.yml:1:1"
        legacy_path = Path(self.tmp.name) / "legacy.db"
        legacy = sqlite3.connect(legacy_path)
        legacy.executescript(
            """
            CREATE TABLE automation_runs (
                run_key TEXT PRIMARY KEY,
                github_run_id INTEGER,
                attempt INTEGER NOT NULL DEFAULT 1,
                workflow TEXT NOT NULL,
                trigger_kind TEXT NOT NULL,
                state TEXT NOT NULL,
                conclusion TEXT NOT NULL DEFAULT '',
                config_hash TEXT NOT NULL DEFAULT '',
                counts_json TEXT NOT NULL DEFAULT '{}',
                budget_json TEXT NOT NULL DEFAULT '{}',
                error_code TEXT NOT NULL DEFAULT '',
                observed_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            INSERT INTO automation_runs(
                run_key,workflow,trigger_kind,state,conclusion,observed_at,updated_at
            ) VALUES('cloud-daily.yml:1:1','cloud-daily.yml','schedule','completed','success',10.0,10.0);
            """
        )
        legacy.commit()
        legacy.close()
        store = TaskStore(legacy_path)
        try:
            row = store.get_automation_run(legacy_key)
            self.assertEqual(row["conclusion"], "success", "历史行原样保留")
            self.assertEqual(row["started_at"], 0.0, "历史行没有 run 时刻 → 0，不发明时间")
            self.assertEqual(row["concluded_at"], 0.0)
            store.upsert_automation_run(
                "cloud-daily.yml:2:1", workflow="cloud-daily.yml", trigger_kind="schedule",
                state="completed", conclusion="success", started_at=100.0, concluded_at=160.0,
            )
            written = store.get_automation_run("cloud-daily.yml:2:1")
            self.assertEqual((written["started_at"], written["concluded_at"]), (100.0, 160.0))
        finally:
            store.close()
        reopened = TaskStore(legacy_path)  # 已迁移表重复启动：幂等
        try:
            again = reopened.get_automation_run("cloud-daily.yml:2:1")
            self.assertEqual((again["started_at"], again["concluded_at"]), (100.0, 160.0))
            self.assertEqual(len(reopened.list_automation_runs(limit=10)), 2)
        finally:
            reopened.close()

    def test_stale_active_run_converges_to_abandoned_without_touching_observed_at(self):
        """6h 无结论且不再被观测到 → 明确终态「已中止」；observed_at 不被改写。"""
        run_key = "cloud-daily.yml:9:1"
        self.store.upsert_automation_run(
            run_key, workflow="cloud-daily.yml", trigger_kind="schedule",
            state="in_progress", conclusion="",
        )
        self._age_run(run_key, seconds=10 * 3600)
        converged = self.service.converge_stale_runs()
        self.assertEqual(converged, [run_key])
        row = self.store.get_automation_run(run_key)
        self.assertEqual(row["state"], "completed")
        self.assertEqual(row["conclusion"], "abandoned")
        self.assertEqual(row["error_code"], "run_abandoned")
        self.assertAlmostEqual(
            row["observed_at"], time.time() - 10 * 3600, delta=5.0,
            msg="观察时刻保持原值（陈旧度判据仍读它）",
        )

    def test_fresh_active_run_is_never_converged(self):
        run_key = "cloud-daily.yml:10:1"
        self.store.upsert_automation_run(
            run_key, workflow="cloud-daily.yml", trigger_kind="schedule",
            state="in_progress", conclusion="",
        )
        self.assertEqual(self.service.converge_stale_runs(), [])
        self.assertEqual(self.store.get_automation_run(run_key)["conclusion"], "")

    def test_converged_row_is_overwritten_by_later_remote_truth(self):
        run_key = "cloud-daily.yml:11:1"
        self.store.upsert_automation_run(
            run_key, workflow="cloud-daily.yml", trigger_kind="schedule",
            state="in_progress", conclusion="",
        )
        self._age_run(run_key, seconds=10 * 3600)
        self.service.converge_stale_runs()
        self.store.upsert_automation_run(
            run_key, state="completed", conclusion="success",
        )
        row = self.store.get_automation_run(run_key)
        self.assertEqual((row["state"], row["conclusion"]), ("completed", "success"),
                         "后来读到的远端真值覆盖本地收敛结论")

    def test_snapshot_projection_carries_run_own_times_and_key(self):
        run_key = "cloud-daily.yml:12:1"
        self.store.upsert_automation_run(
            run_key, workflow="cloud-daily.yml", trigger_kind="schedule",
            state="completed", conclusion="success", started_at=100.0, concluded_at=160.0,
            observed_at=200.0,
        )
        projected = self.service.snapshot(refresh=False)["runs"][0]
        self.assertEqual(projected["run_key"], run_key)
        self.assertEqual(projected["started_at"], 100.0)
        self.assertEqual(projected["concluded_at"], 160.0)

    def test_delete_run_removes_terminal_rows_only(self):
        run_key = "cloud-daily.yml:1:1"
        self.store.upsert_automation_run(
            run_key, workflow="cloud-daily.yml", trigger_kind="schedule",
            state="completed", conclusion="success",
        )
        self.assertEqual(self.store.delete_automation_run(run_key), {"deleted": True, "run_key": run_key})
        self.assertIsNone(self.store.get_automation_run(run_key))
        with self.assertRaises(KeyError):
            self.store.delete_automation_run(run_key)
        active_key = "cloud-daily.yml:7:1"
        self.store.upsert_automation_run(
            active_key, workflow="cloud-daily.yml", trigger_kind="schedule",
            state="in_progress", conclusion="",
        )
        with self.assertRaises(ValueError):
            self.store.delete_automation_run(active_key)
        self.assertIsNotNone(self.store.get_automation_run(active_key), "活动记录不被删除")

    # ---- P57（AUTOMATION-RUN-TOMBSTONE-1）：删除持久，对账链不复活 ----------

    def _seed_terminal_run(self, run_key, *, conclusion="success"):
        self.store.upsert_automation_run(
            run_key, workflow="cloud-daily.yml", trigger_kind="schedule",
            state="completed", conclusion=conclusion,
        )

    def test_deleted_run_is_not_resurrected_by_reconcile_upsert(self):
        """用户删过的 run_key 再被对账循环观测（automation.py reconcile upsert
        形状）也不复活——upsert 命中墓碑干净 no-op，重启后同样不出现。"""
        run_key = "cloud-daily.yml:21:1"
        self._seed_terminal_run(run_key)
        self.assertEqual(self.store.delete_automation_run(run_key), {"deleted": True, "run_key": run_key})
        resurrected = self.store.upsert_automation_run(
            run_key, github_run_id=21, attempt=1, workflow="cloud-daily.yml",
            trigger_kind="schedule", state="completed", conclusion="failure",
            observed_at=time.time(),
        )
        self.assertIsNone(resurrected, "墓碑命中的 upsert 返回 None，不抛错")
        self.assertIsNone(self.store.get_automation_run(run_key))
        self.assertEqual(self.store.list_automation_runs(limit=10), [])
        other = "cloud-daily.yml:22:1"
        self.store.upsert_automation_run(
            other, workflow="cloud-daily.yml", trigger_kind="schedule",
            state="completed", conclusion="success",
        )
        self.assertIsNotNone(self.store.get_automation_run(other), "墓碑不误伤其他 run_key")

    def test_ghost_convergence_shape_is_noop_on_deleted_key(self):
        """ghost 收敛（automation.py:1396 upsert 形状）对墓碑 key 同样 no-op；
        删除后的 key 本就不在 list_automation_runs 里，双保险落在唯一入口。"""
        run_key = "cloud-daily.yml:23:1"
        self._seed_terminal_run(run_key)
        self.store.delete_automation_run(run_key)
        self.assertIsNone(self.store.upsert_automation_run(
            run_key, state="completed", conclusion="abandoned", error_code="run_abandoned",
        ))
        self.assertIsNone(self.store.get_automation_run(run_key))

    def test_internal_stale_sweep_batch_delete_tombstones(self):
        """AUTO-TOMBSTONE-R2 洞 B（C6 复现实证）：内部清扫（automation.py:1110
        走 delete_automation_runs 批量删）与单删同契约——实删的键同事务落墓碑。
        此前批量硬删不落墓碑，同键再观测即整体复活；reconcile 的
        schedule+skipped 导入滤镜只是行为性护栏，墓碑才是 P57 承诺的闸门本体。
        不存在的键无删除事实，不落墓碑（不封死未来同键合法记录）。"""
        run_key = "cloud-daily.yml:24:1"
        ghost_key = "cloud-daily.yml:25:1"
        self._seed_terminal_run(run_key)
        self.assertEqual(self.store.delete_automation_runs([run_key, ghost_key]), 1)
        self.assertIsNone(self.store.upsert_automation_run(
            run_key, workflow="cloud-daily.yml", trigger_kind="schedule",
            state="completed", conclusion="failure",
        ), "批量清扫实删的键落墓碑，远端再观测不复活")
        self.assertIsNone(self.store.get_automation_run(run_key))
        self.assertIsNotNone(self.store.upsert_automation_run(
            ghost_key, workflow="cloud-daily.yml", trigger_kind="schedule",
            state="completed", conclusion="failure",
        ), "未实删的键无墓碑，仍可正常观测")

    def test_concurrent_delete_cannot_slip_between_gate_and_insert(self):
        """AUTO-TOMBSTONE-R2 洞 A 竞态钉：删除不得插进「墓碑闸门→插行」窗口。

        旧实现闸门检查/插行各持锁一次，并发删除在窗口内提交后插行复活，
        产出一行墓碑尚在却永久可见的冻结复活卡。现实现闸门→插行全程同锁，
        并发删除被串行化到插行之后：终态=行死+墓碑在，再对账不复活。"""
        run_key = "cloud-daily.yml:26:1"
        self._seed_terminal_run(run_key)
        gate_seen = threading.Event()
        worker_at_lock = threading.Event()
        original_gate = self.store._automation_run_deleted

        def hooked_gate(candidate):
            result = original_gate(candidate)
            if candidate == run_key:
                gate_seen.set()
                # 给删除线程机会推进到锁竞争点；全程持锁下它只能阻塞在此。
                worker_at_lock.wait(5)
            return result

        def delete_in_window():
            gate_seen.wait(5)
            worker_at_lock.set()
            self.store.delete_automation_run(run_key)

        worker = threading.Thread(target=delete_in_window)
        worker.start()
        try:
            with patch.object(self.store, "_automation_run_deleted", hooked_gate):
                self.store.upsert_automation_run(
                    run_key, workflow="cloud-daily.yml", trigger_kind="schedule",
                    state="completed", conclusion="failure",
                )
        finally:
            worker.join(5)
        self.assertIsNone(
            self.store.get_automation_run(run_key),
            "删除与插行串行化：终态=行死，不产生冻结复活卡",
        )
        self.assertIsNone(self.store.upsert_automation_run(
            run_key, workflow="cloud-daily.yml", trigger_kind="schedule",
            state="completed", conclusion="success",
        ), "墓碑命中，对账链不再复活")

    def test_tombstone_table_is_bounded(self):
        """墓碑体量有界：超过上限裁最旧（deleted_at 升序淘汰），被淘汰的
        key 恢复可观测，留存的 key 仍然死亡。"""
        keys = [f"cloud-daily.yml:3{index}:1" for index in range(7)]
        for run_key in keys:
            self._seed_terminal_run(run_key)
            self.store.delete_automation_run(run_key)
        with sqlite3.connect(self.path) as db:
            for index, run_key in enumerate(keys):
                db.execute(
                    "UPDATE automation_run_tombstones SET deleted_at=? WHERE run_key=?",
                    (1000.0 + index, run_key),
                )
        with patch("src.runtime.task_store.AUTOMATION_RUN_TOMBSTONE_LIMIT", 5):
            extra = "cloud-daily.yml:39:1"
            self._seed_terminal_run(extra)
            self.store.delete_automation_run(extra)
        with sqlite3.connect(self.path) as db:
            kept = {str(row[0]) for row in db.execute("SELECT run_key FROM automation_run_tombstones")}
        self.assertEqual(len(kept), 5)
        self.assertEqual(kept, {extra, *keys[3:]}, "超限裁最旧，最新的 5 条留存")
        self.assertIsNotNone(self.store.upsert_automation_run(
            keys[0], workflow="cloud-daily.yml", trigger_kind="schedule",
            state="completed", conclusion="success",
        ), "被裁掉的墓碑对应的 key 恢复可观测（有界折衷）")
        self.assertIsNone(self.store.upsert_automation_run(
            keys[3], workflow="cloud-daily.yml", trigger_kind="schedule",
            state="completed", conclusion="success",
        ))
        self.assertIsNone(self.store.get_automation_run(keys[3]))

    def test_tombstone_survives_reopen_and_schema_is_idempotent(self):
        """墓碑跨启动持久；重复建表（幂等迁移）不炸。"""
        run_key = "cloud-daily.yml:31:1"
        self._seed_terminal_run(run_key)
        self.store.delete_automation_run(run_key)
        self.store.close()
        reopened = TaskStore(self.path)  # 重复执行 CREATE TABLE IF NOT EXISTS：幂等
        try:
            self.assertIsNone(reopened.upsert_automation_run(
                run_key, workflow="cloud-daily.yml", trigger_kind="schedule",
                state="completed", conclusion="success",
            ), "重启后对账链再观测同一 key 仍不复活")
            self.assertIsNone(reopened.get_automation_run(run_key))
        finally:
            reopened.close()

    def test_deleted_runs_zero_revival_across_full_chain_both_key_shapes(self):
        """AUTO-TOMBSTONE-R2 回归钉（复现剧本四链，AUTO-TOMBSTONE-R2 剧本）：
        两类键形（schedule 计划窗口 failure / workflow_dispatch 材料 success）
        删除后，①对账重灌（远端仍列同 run，live=_automation_monitor_loop
        45s 拍）②ghost 收敛 ③离线快照 ④重启后再对账——全链零复活；
        同 run_id 升 attempt=新键新记录（设计语义，非复活；旧键保持死亡）。"""
        schedule_key = "cloud-daily.yml:9001:1"
        dispatch_key = "cloud-daily.yml:9002:1"
        # reconcile 门槛：无 worker repo secret 会在对账入口早退（automation.py:960）。
        self.credentials.save_secret(
            "github_worker_repo", "synthetic-owner/courselens-worker-synthetic")
        schedule_run = {
            "id": 9001, "run_attempt": 1, "status": "completed", "conclusion": "failure",
            "event": "schedule", "head_sha": "c" * 40,
            "created_at": "2026-10-07T01:33:49Z", "run_started_at": "2026-10-07T01:33:49Z",
            "updated_at": "2026-10-07T01:33:49Z", "html_url": "",
        }
        dispatch_run = {
            "id": 9002, "run_attempt": 1, "status": "completed", "conclusion": "success",
            "event": "workflow_dispatch", "head_sha": "c" * 40,
            "created_at": "2026-10-06T16:47:05Z", "run_started_at": "2026-10-06T16:47:05Z",
            "updated_at": "2026-10-06T16:47:05Z", "html_url": "",
        }
        self.github.list_workflow_runs = lambda workflow, limit=20: (
            [dict(schedule_run), dict(dispatch_run)]
            if workflow == "cloud-daily.yml" else [])

        def alive():
            keys = {str(item["run_key"]) for item in self.store.list_automation_runs(limit=200)}
            return keys & {schedule_key, dispatch_key}

        self.service.reconcile(force=True)
        self.assertEqual(alive(), {schedule_key, dispatch_key}, "基线：两类卡都入台账")
        self.store.delete_automation_run(schedule_key)
        self.store.delete_automation_run(dispatch_key)
        self.assertEqual(alive(), set(), "删除生效")

        # 链①：对账重灌（远端列表不变）。
        self.service.reconcile(force=True)
        self.assertEqual(alive(), set(), "链①对账重灌零复活")
        # 链②：ghost 收敛。
        self.service.converge_stale_runs()
        self.assertEqual(alive(), set(), "链②ghost 收敛零复活")
        # 链③：离线快照。
        self.service.snapshot(refresh=False)
        self.assertEqual(alive(), set(), "链③离线快照零复活")
        # 链④：重启（新 TaskStore 同库）后再对账。
        self.store.close()
        self.store = TaskStore(self.path)
        self.service = AutomationService(
            self.store,
            self.credentials,
            lambda: self.github,
            local_schedule_getter=lambda: dict(self.local_schedule),
            local_schedule_setter=lambda value: dict(value),
        )
        self.service.reconcile(force=True)
        self.assertEqual(alive(), set(), "链④重启后再对账零复活")

        # 同 run_id 升 attempt=新键新记录；旧键墓碑不误伤新键。
        self.github.list_workflow_runs = lambda workflow, limit=20: (
            [dict(schedule_run, run_attempt=2)]
            if workflow == "cloud-daily.yml" else [])
        self.service.reconcile(force=True)
        all_keys = {str(item["run_key"]) for item in self.store.list_automation_runs(limit=200)}
        self.assertEqual(all_keys, {"cloud-daily.yml:9001:2"},
                         "attempt 升位=新键新记录（设计语义）")
        self.assertIsNone(self.store.get_automation_run(schedule_key),
                          "旧键保持死亡，新键不受墓碑误伤")


class AutomationApiTests(unittest.TestCase):
    class Service:
        def __init__(self, root):
            self.task_store = TaskStore(root / "state.db")
            self.updated = None
            self.enqueue_calls = []
            self.enqueue_error = None

        def app_shell_snapshot(self):
            return {"schema": "courselens.app-shell.v1", "observed_at": 1, "expires_at": 2}

        def authentication_snapshot(self):
            return {"state": "ready"}

        def enqueue_subtitle(self, course_id, sub_id, **kwargs):
            self.enqueue_calls.append((course_id, sub_id))
            if self.enqueue_error is not None:
                raise self.enqueue_error
            return {
                "task_id": "task-synthetic-1", "kind": "subtitle",
                "course_id": course_id, "sub_id": sub_id, "state": "queued",
                "progress": {}, "estimate": {},
            }

        def automation_snapshot(self, refresh=False):
            return {"schema": "courselens.automation.v2", "state": "disabled", "refresh": refresh}

        def update_automation_config(self, body):
            self.updated = body
            return {"state": "disabled"}

        def upload_automation_secrets(self, _body):
            return {"state": "uploaded", "observed_at": 1}

        def automation_action(self, action, *, operation_id):
            return {"action": action, "operation_id": operation_id, "state": "accepted"}

    def test_v3_put_and_get_routes(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "runtime" / "cache") as tmp:
            service = self.Service(Path(tmp))
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), PROJECT_ROOT / "frontend"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(f"{base}/api/v3/app-shell") as response:
                    shell = json.loads(response.read())
                self.assertEqual(shell["data"]["schema"], "courselens.app-shell.v1")
                body = json.dumps({
                    "operation_id": "config-12345678",
                    "account_id": "2020001",
                }).encode()
                request = Request(
                    f"{base}/api/v3/automation/config", data=body, method="PUT",
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(request) as response:
                    value = json.loads(response.read())
                self.assertEqual(value["data"]["operation"]["state"], "accepted")
                self.assertEqual(service.updated["account_id"], "2020001")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_put_config_requires_an_operation_id(self):
        """NIGHT2-W6 反钉：PUT automation/config 的 operation_id 必填，缺失
        一律 400 operation_id_invalid（真实装机 ⑬ 缺陷锁死后端合同面）。"""
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "runtime" / "cache") as tmp:
            service = self.Service(Path(tmp))
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), PROJECT_ROOT / "frontend"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                request = Request(
                    f"{base}/api/v3/automation/config", data=json.dumps({"account_id": "2020001"}).encode(),
                    method="PUT", headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(Exception) as caught:
                    urlopen(request)
                self.assertIn("400", str(caught.exception))
                self.assertEqual(json.loads(caught.exception.read())["error_code"], "operation_id_invalid")
                self.assertIsNone(service.updated, "校验失败绝不触碰配置")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_put_config_replays_are_idempotent(self):
        """NIGHT2-W6 重复 ID 语义钉：同 operation_id 重放直接回既有 operation，
        绝不重复落账 update_config；跨重启由 task_store 持久化承载。"""
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "runtime" / "cache") as tmp:
            service = self.Service(Path(tmp))
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), PROJECT_ROOT / "frontend"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                def put(payload):
                    request = Request(
                        f"{base}/api/v3/automation/config", data=json.dumps(payload).encode(),
                        method="PUT", headers={"Content-Type": "application/json"},
                    )
                    with urlopen(request) as response:
                        return json.loads(response.read())

                first = put({"operation_id": "config-replay-0001", "account_id": "2020001"})
                replay = put({"operation_id": "config-replay-0001", "account_id": "9999999"})
                self.assertEqual(first["data"]["operation"]["state"], "accepted")
                self.assertEqual(replay["data"]["operation"]["state"], "accepted")
                self.assertEqual(service.updated["account_id"], "2020001", "重放不重复落账")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_subtitle_enqueue_without_credentials_names_the_real_code(self):
        """NIGHT2-W7：未登录入队字幕 → 400 fudan_login_required（闭集显式码
        直达前端），不再被启发式粗映射吞成 task_failed。"""
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "runtime" / "cache") as tmp:
            service = self.Service(Path(tmp))
            error = RuntimeError("Sign in before starting remote subtitle computation")
            error.code = "fudan_login_required"
            service.enqueue_error = error
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), PROJECT_ROOT / "frontend"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                request = Request(
                    f"{base}/api/v3/tasks/enqueue",
                    data=json.dumps({"kind": "subtitle", "course_id": "c1", "sub_id": "s1"}).encode(),
                    method="POST", headers={"Content-Type": "application/json"},
                )
                with self.assertRaises(Exception) as caught:
                    urlopen(request)
                self.assertIn("400", str(caught.exception))
                self.assertEqual(json.loads(caught.exception.read())["error_code"], "fudan_login_required")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_task_failure_guidance_tracks_task_error_codes(self):
        """NIGHT2-W7：任务中心失败文案闭集键与后端 TASK_ERROR_CODES 严格相等。"""
        drawer = family_text("tasks-drawer")
        block = drawer[drawer.index("TASK_FAILURE_GUIDANCE = Object.freeze({"):]
        block = block[:block.index("\n});")]
        keys = set(re.findall(r"([a-z0-9_]+): \"", block))
        self.assertEqual(keys, set(TASK_ERROR_CODES))

    def test_shared_error_codes_speak_identical_copy_across_both_tables(self):
        """R3-09/N15-W1：api.js ERROR_MESSAGES 与 tasks-drawer
        TASK_FAILURE_GUIDANCE 同为通用任务死因面消费的同码文案必须逐字一致
        （cloud_setup_required 先例扩至 runtime_failed/task_failed），防措辞
        漂移让学生在同一码上看到两种说法。"""
        import re as _re

        api = (PROJECT_ROOT / "frontend" / "modules" / "api.js").read_text(encoding="utf-8")
        drawer = family_text("tasks-drawer")

        def copy_table(text, anchor):
            block = text[text.index(anchor):]
            block = block[:block.index("\n});")]
            return dict(_re.findall(r'([a-z0-9_]+): "([^"]*)"', block))

        api_table = copy_table(api, "const ERROR_MESSAGES = Object.freeze({")
        drawer_table = copy_table(drawer, "const TASK_FAILURE_GUIDANCE = Object.freeze({")
        for shared in ("cloud_setup_required", "runtime_failed", "task_failed"):
            self.assertIn(shared, api_table, f"api.js 缺 {shared}")
            self.assertIn(shared, drawer_table, f"tasks-drawer 缺 {shared}")
            self.assertEqual(
                api_table[shared], drawer_table[shared],
                f"{shared} 两表文案漂移",
            )

    def test_cloud_chain_config_to_enqueue_walks_the_real_routes(self):
        """NIGHT2-W6+W7 联合自验：云开关保存→加密上传→验证→启用→入队字幕，
        合成全链走真 HTTP 路由与闭集校验，每步均 202。"""
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "runtime" / "cache") as tmp:
            service = self.Service(Path(tmp))
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), PROJECT_ROOT / "frontend"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def call(method, route, payload):
                request = Request(
                    f"{base}/api/v3/{route}", data=json.dumps(payload).encode(),
                    method=method, headers={"Content-Type": "application/json"},
                )
                with urlopen(request) as response:
                    return response.status, json.loads(response.read())

            try:
                statuses = []
                status, _ = call("PUT", "automation/config", {
                    "operation_id": "chain-config-00001",
                    "config": {"account_id": "2020001", "rules": [{"course_id": "c1"}]},
                })
                statuses.append(status)
                status, _ = call("PUT", "automation/cloud-secrets", {
                    "operation_id": "chain-upload-0001", "account_id": "2020001",
                    "disclosure_version": "cloud-custody-disclosure.v1", "confirmed": True,
                })
                statuses.append(status)
                status, _ = call("POST", "automation/actions", {
                    "action": "verify-cloud-credentials", "operation_id": "chain-verify-0001",
                })
                statuses.append(status)
                status, _ = call("POST", "automation/actions", {
                    "action": "enable-cloud", "operation_id": "chain-enable-0001",
                })
                statuses.append(status)
                status, value = call("POST", "tasks/enqueue", {
                    "kind": "subtitle", "course_id": "c1", "sub_id": "s1",
                })
                statuses.append(status)
                self.assertEqual(statuses, [202, 202, 202, 202, 202])
                self.assertEqual(value["data"]["task"]["task_id"], "task-synthetic-1")
                self.assertEqual(service.enqueue_calls, [("c1", "s1")])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_idle_connections_are_reaped_but_media_is_exempt(self):
        """NIGHT2-W21：空闲连接按 Handler.timeout 回收（测试注入 0.2s）；
        直播媒体 Range 路由显式豁免——挂起连接存活远超回收阈值，学生暂停
        播放后恢复不断流（C9-H 修订验收：媒体存活+非媒体回收双断言）。"""
        import socket
        import time as time_module
        from types import SimpleNamespace

        from src.runtime import http_api as http_api_module

        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "runtime" / "cache") as tmp:
            service = self.Service(Path(tmp))

            def fetch_resource(session_id, resource_id, cookie_session="", range_header=""):
                service.media_fetches.append((session_id, resource_id, range_header))
                # 4MB 载荷：慢读客户端跨多个回收窗口仍有数据流动（豁免生效的直接证据）
                return SimpleNamespace(
                    status=206, content_type="video/mp4", body=b"x" * (4 * 1024 * 1024),
                    content_range="bytes 0-4194303/4194304", accept_ranges="bytes",
                )

            service.media_fetches = []
            service.fetch_live_room_resource = fetch_resource
            handler_cls = make_handler(http_services(service), PROJECT_ROOT / "frontend")
            handler_cls.timeout = 0.2
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_port
            try:
                # 非媒体：连上不发任何数据 → 空闲回收（EOF 远早于 2s）
                idle = socket.create_connection(("127.0.0.1", port), timeout=3)
                idle.settimeout(3)
                start = time_module.monotonic()
                try:
                    eof = idle.recv(1)
                except socket.timeout:
                    self.fail("空闲连接未被回收（recv 超时）")
                elapsed = time_module.monotonic() - start
                self.assertEqual(eof, b"", "空闲连接被服务端关闭")
                self.assertLess(elapsed, 2.0, f"回收发生在超时窗口附近而非 30s 默认（{elapsed:.2f}s）")
                idle.close()
                # 媒体：Range GET → 响应正常返回且连接存活远超回收阈值
                media = socket.create_connection(("127.0.0.1", port), timeout=3)
                request_line = (
                    f"GET /api/v3/live-room/play/{'a' * 24}/resource/{'b' * 20} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{port}\r\n"
                    "Range: bytes=0-4\r\n"
                    "Connection: keep-alive\r\n\r\n"
                )
                media.sendall(request_line.encode("ascii"))
                media.settimeout(3)
                first = media.recv(4096)
                self.assertIn(b"206", first.split(b"\r\n")[0], "媒体 Range 响应 206")
                # 慢读跨 0.2s 回收阈值多个窗口：豁免让媒体发送不受空闲超时影响
                # （无豁免时 0.2s socket 超时会掐断 sendall → 连接异常/EOF）
                received = len(first)
                media.settimeout(1.5)
                windows = 0
                try:
                    while received < 256 * 1024 and windows < 8:
                        chunk = media.recv(65536)
                        if not chunk:
                            break
                        received += len(chunk)
                        windows += 1
                        time_module.sleep(0.3)  # 慢读：单窗 0.3s > 0.2s 回收阈值
                except (socket.timeout, ConnectionError) as exc:
                    self.fail(f"媒体流被空闲超时切断（豁免失效）：{exc!r}")
                self.assertGreater(received, 128 * 1024, "慢读下媒体数据持续流动")
                self.assertLess(windows, 8, "读取窗口未耗尽（数据流健康）")
                media.close()
                self.assertEqual(service.media_fetches, [("a" * 24, "b" * 20, "bytes=0-4")])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_cloud_secrets_route_rejects_secret_values(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "runtime" / "cache") as tmp:
            service = self.Service(Path(tmp))
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), PROJECT_ROOT / "frontend"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                for payload in (
                    {"operation_id": "upload-12345678", "password": "synthetic-password"},
                    {"operation_id": "upload-12345679", "student_id": "2020001"},
                    {"operation_id": "upload-1234567a", "api_key": "sk-synthetic"},
                ):
                    request = Request(
                        f"{base}/api/v3/automation/cloud-secrets",
                        data=json.dumps(payload).encode(), method="PUT",
                        headers={"Content-Type": "application/json"},
                    )
                    with self.assertRaises(Exception) as caught:
                        urlopen(request)
                    self.assertIn("400", str(caught.exception))
                    self.assertNotIn("uploaded", str(caught.exception))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_automation_events_are_available_on_the_v3_sse_stream(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "runtime" / "cache") as tmp:
            service = self.Service(Path(tmp))
            service.task_store.append_remote_event(
                "automation", "config", {"state": "migrated_fail_closed"}
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), PROJECT_ROOT / "frontend"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(f"{base}/api/v3/events?topics=automation&after=0", timeout=3) as response:
                    chunk = b"".join(response.readline() for _ in range(4))
                self.assertIn(b"event: automation", chunk)
                self.assertIn(b"migrated_fail_closed", chunk)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


    def test_runs_delete_route_removes_terminal_records_only(self):
        """第卅六案③：运行记录删除走闭集路由——终态可删、活动态 409、未知键 404。"""
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "runtime" / "cache") as tmp:
            service = self.Service(Path(tmp))
            store = service.task_store
            store.upsert_automation_run(
                "cloud-daily.yml:1:1", workflow="cloud-daily.yml", trigger_kind="schedule",
                state="completed", conclusion="success",
            )
            store.upsert_automation_run(
                "cloud-daily.yml:2:1", workflow="cloud-daily.yml", trigger_kind="schedule",
                state="in_progress", conclusion="",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(http_services(service), PROJECT_ROOT / "frontend"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def post(run_key):
                request = Request(
                    f"{base}/api/v3/automation/runs-delete",
                    data=json.dumps({"run_key": run_key}).encode(),
                    method="POST", headers={"Content-Type": "application/json"},
                )
                with urlopen(request) as response:
                    return json.loads(response.read())

            try:
                value = post("cloud-daily.yml:1:1")
                self.assertTrue(value["data"]["deleted"])
                self.assertEqual(value["data"]["run_key"], "cloud-daily.yml:1:1")
                self.assertIsNone(store.get_automation_run("cloud-daily.yml:1:1"))
                with self.assertRaises(Exception) as active:
                    post("cloud-daily.yml:2:1")
                self.assertIn("409", str(active.exception))
                self.assertEqual(
                    json.loads(active.exception.read())["error_code"], "task_action_invalid"
                )
                with self.assertRaises(Exception) as missing:
                    post("cloud-daily.yml:9:9")
                self.assertIn("404", str(missing.exception))
                self.assertEqual(
                    json.loads(missing.exception.read())["error_code"], "task_not_found"
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                store.close()


class CoursewarePlanImportTests(unittest.TestCase):
    """v3 结果导入：计划校验 + 本地 PDF 任务独立排队（应用层闭环）。"""

    def _plan(self, *, course_id="c1", sub_id="s1", pipeline="cloud-automation.v3"):
        return {
            "schema": "courseware_plan.v1",
            "policy_version": 1,
            "pipeline": pipeline,
            "course_id": course_id,
            "sub_id": sub_id,
            "inventory_digest": "d" * 64,
            "entries": [{
                "output_position": 1, "record_id": "42", "capture_time": 30,
                "capture_ordinal": 0, "source_sha256": "a" * 64,
                "page_label": "1/2", "page_label_source": "ocr_text",
                "annotation": {"class": "unknown", "confidence": 0.0},
                "keep_reason": "distinct_capture", "version_of_position": 0,
                "duplicate_count": 0,
            }],
            "excluded": [],
            "ordering": {"mode": "page_label", "confidence": 0.9},
            "counts": {"input_events": 1, "recognized": 1, "kept": 1,
                       "exact_duplicates": 0, "skipped": 0},
        }

    def _result(self, plan, digest, *, course_id="c1", sub_id="s1"):
        return {
            "schema": "result.v2", "protocol_version": "2",
            "task_id": "a" * 32, "job_kind": "learning_pack",
            "input_hash": "b" * 64, "pipeline_fingerprint": "cloud-automation.v3",
            "status": "completed",
            "outputs": {
                "cloud_catalog": {
                    "course_id": course_id, "title": "课程", "teacher": "",
                    "term": "", "department": "",
                    "lecture": {"sub_id": sub_id, "has_playback": True},
                },
                "ppt_pages": [{"page_num": 1, "created_sec": 30}],
                "courseware_plan": plan,
                "courseware_plan_digest": digest,
            },
            "metrics": {}, "warnings": [],
        }

    def _service(self):
        import hashlib as hashlib_module
        from src.application import CourseLensApplication
        cache = PROJECT_ROOT / "runtime" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=cache)
        self.addCleanup(tmp.cleanup)
        service = CourseLensApplication(Path(tmp.name))
        self.addCleanup(service.close)
        service.catalog_repository.upsert_lecture("c1", {
            "sub_id": "s1", "sub_title": "第一讲", "lecturer_name": "",
            "date": "2026-09-14", "has_playback": True,
        })
        self._noop_worker(service)
        self._hashlib = hashlib_module
        return service

    @staticmethod
    def _digest(plan):
        from src.application import CourseLensApplication
        return CourseLensApplication._courseware_plan_digest(plan)

    @staticmethod
    def _noop_worker(service):
        service._ensure_courseware_pdf_worker = lambda: None

    def test_import_queues_plan_driven_pdf_task_independently(self):
        service = self._service()
        plan = self._plan()
        digest = self._digest(plan)
        service._import_automation_result(self._result(plan, digest))
        tasks = service.task_store.list_tasks(kinds=("courseware_pdf",), limit=10)
        self.assertEqual(len(tasks), 1)
        payload = dict(tasks[0].get("payload") or {})
        self.assertEqual(payload["courseware_plan_digest"], digest)
        self.assertEqual(payload["courseware_plan"]["sub_id"], "s1")
        self.assertNotIn("pptimgurl", json.dumps(payload))
        # The learning output import itself is not gated on the PDF task.
        self.assertIsNotNone(service.catalog_repository.get_lecture("s1"))

    def test_import_rejects_plan_bound_to_another_lecture(self):
        service = self._service()
        plan = self._plan(sub_id="s9")
        digest = self._digest(plan)
        with self.assertRaisesRegex(ValueError, "another course"):
            service._import_automation_result(self._result(plan, digest))
        self.assertEqual(
            service.task_store.list_tasks(kinds=("courseware_pdf",), limit=10), [],
        )

    def test_import_rejects_plan_from_another_pipeline_version(self):
        service = self._service()
        plan = self._plan(pipeline="cloud-automation.v2")
        digest = self._digest(plan)
        with self.assertRaisesRegex(ValueError, "pipeline"):
            service._import_automation_result(self._result(plan, digest))
        self.assertEqual(
            service.task_store.list_tasks(kinds=("courseware_pdf",), limit=10), [],
        )

    def test_import_rejects_plan_digest_mismatch(self):
        service = self._service()
        plan = self._plan()
        with self.assertRaisesRegex(ValueError, "digest"):
            service._import_automation_result(self._result(plan, "0" * 64))
        self.assertEqual(
            service.task_store.list_tasks(kinds=("courseware_pdf",), limit=10), [],
        )

    def test_import_leaves_existing_valid_pdf_untouched(self):
        service = self._service()
        _, pdf_path, manifest_path = service._courseware_pdf_paths("c1", "s1")
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(b"%PDF-1.4 synthetic")
        from src.runtime.courseware_pdf import MANIFEST_SCHEMA
        manifest_path.write_text(json.dumps({
            "schema": MANIFEST_SCHEMA, "generated_at": 1, "kept": 3,
            "events_total": 4, "duplicates": 1, "skipped": {},
        }), encoding="utf-8")
        plan = self._plan()
        digest = self._digest(plan)
        service._import_automation_result(self._result(plan, digest))
        self.assertEqual(
            service.task_store.list_tasks(kinds=("courseware_pdf",), limit=10), [],
            "已存在有效 PDF 时导入不得重新排队覆盖",
        )
        self.assertEqual(pdf_path.read_bytes(), b"%PDF-1.4 synthetic")

    def test_pdf_enqueue_failure_never_rolls_back_import(self):
        service = self._service()
        plan = self._plan()
        digest = self._digest(plan)

        def broken_enqueue(*args, **kwargs):
            raise RuntimeError("synthetic pdf queue failure")

        service.enqueue_courseware_pdf = broken_enqueue
        service._import_automation_result(self._result(plan, digest))
        # Import output survives; the failure is recorded as bounded pending.
        self.assertIsNotNone(service.catalog_repository.get_lecture("s1"))
        pending = dict(service.task_store.get_app_state("automation_courseware_pending", {}) or {})
        self.assertEqual(pending["s1"]["error_code"], "courseware_enqueue_failed")


if __name__ == "__main__":
    unittest.main()
