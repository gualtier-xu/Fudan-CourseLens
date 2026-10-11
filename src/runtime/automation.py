"""Opt-in cloud custody control plane for unattended CourseLens automation.

The ``cloud-automation.v3`` contract binds one explicit user disclosure to the
user's own managed GitHub Worker repository.  The service stores only
non-sensitive configuration and GitHub identifiers in SQLite; secret values are
loaded from the local CredentialStore only inside a user-triggered upload,
encrypted for the managed GitHub Environment, and never written to the local
database, log, operation result, or event stream.

v3 breaking changes from v2: a selected course is one fixed unattended bundle
(subtitle ASR, slide OCR, AI summary/chapters, Lecture IR/evidence); enabling a
course atomically captures a verified playable-lecture selection baseline so
only post-selection eligible lectures run; the completed-work identity drops
global config; and the cloud result carries a bounded ``courseware_plan``.
Legacy v1/v2 profiles migrate fail closed.
"""

from __future__ import annotations

import hashlib
import base64
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox
from nacl.signing import VerifyKey

from credentials import CredentialStore
from src.remote.github_app import GitHubAppClient, GitHubAppError
from src.remote.protocol import generate_box_keypair, open_result, sha256_hex
from src.runtime.task_store import TaskStore
from src.runtime.test_mode import fresh_operations_requested


CLOUD_PROTOCOL_VERSION = "cloud-automation.v3"
CLOUD_VERIFY_WORKFLOW = "cloud-verify.yml"
CLOUD_DAILY_WORKFLOW = "cloud-daily.yml"
# 第卅一案：requested 验证窗口（与三处置入点的 expires_at=now+90 同源），
# 过期由 reconcile 强制收敛为 failed+cloud_verification_timeout。
CLOUD_VERIFY_EXPIRY_SECONDS = 90.0
CLOUD_DISCLOSURE_VERSION = "cloud-custody-disclosure.v1"
OPERATION_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

# v3 selection baseline bounds.  Enabling one course snapshots the opaque
# identifiers of every currently playable lecture as that generation's
# baseline; the rules envelope stays inside the GitHub secret size budget,
# and an over-sized baseline is rejected, never truncated silently.
COURSE_BASELINE_MAX_LECTURES = 400
COURSE_BASELINE_ID_MAX_LENGTH = 64
BASELINE_TOTAL_MAX_ENTRIES = 2000

# The schedule is fixed for this stage, not user-configurable: on weekdays
# one window 30 minutes after each standard class period ends, plus a nightly
# 22:00 fallback every day.  GitHub Actions cron is evaluated in UTC and
# Beijing (Asia/Shanghai, UTC+8) has no DST, so the eleven cron expressions in
# CLOUD_CRON_EXPRESSIONS map 1:1 onto these windows year-round.
CLOUD_SCHEDULE = {
    "weekday_times": (
        "09:15", "10:10", "11:10", "12:05", "13:00",
        "14:45", "15:40", "16:40", "17:35", "18:30",
    ),
    "daily_times": ("22:00",),
    "timezone": "Asia/Shanghai",
}
CLOUD_CRON_EXPRESSIONS = (
    "15 1 * * 1-5", "10 2 * * 1-5", "10 3 * * 1-5", "5 4 * * 1-5",
    "0 5 * * 1-5", "45 6 * * 1-5", "40 7 * * 1-5", "40 8 * * 1-5",
    "35 9 * * 1-5", "30 10 * * 1-5", "0 14 * * *",
)
SHANGHAI = timezone(timedelta(hours=8))


def _cloud_schedule_times() -> tuple[str, ...]:
    """All fixed Beijing windows in chronological order (grid + nightly fallback)."""
    return tuple(dict.fromkeys((*CLOUD_SCHEDULE["weekday_times"], *CLOUD_SCHEDULE["daily_times"])))


def _cloud_windows_for_day(day) -> list[tuple[int, int]]:
    """Fixed windows for one Beijing date; the class grid runs on weekdays only."""
    times: tuple[str, ...] = CLOUD_SCHEDULE["daily_times"]
    if day.weekday() < 5:
        times = (*CLOUD_SCHEDULE["weekday_times"], *times)
    return [(int(part[0:2]), int(part[3:5])) for part in times]

# Cloud retention split: encrypted result artifacts keep the smallest
# coherent bounded window (30 days) and only the newest encrypted
# continuation-state artifact is kept up to the 90-day ceiling; a verified
# import deletes the exact result earlier.  The client surfaces the actual
# per-artifact deadline from the projected import `expires_at`.
CLOUD_RESULT_RETENTION_DAYS = 30
CLOUD_STATE_RETENTION_DAYS = 90
CLOUD_RETENTION_CEILING_DAYS = 90

# 第卅六案②：运行记录的超时收口窗。最后活动时间超过该窗且仍无结论的行收敛为
# 明确终态「已中止」——任务中心不再出现永远「进行中」的 ghost run（现场实证
# 五张卡全停在「进行中」）。判据是 updated_at（最后一次被观测到活跃），
# 不是 observed_at（每次对账重盖的观察时刻）；后来读到的 GitHub 真值照常覆盖。
AUTOMATION_RUN_STALE_SECONDS = 6 * 3600
AUTOMATION_RUN_ABANDONED_CONCLUSION = "abandoned"
AUTOMATION_RUN_ABANDONED_CODE = "run_abandoned"

CLOUD_SECRET_NAMES = (
    "COURSELENS_CLOUD_STUDENT_ID",
    "COURSELENS_CLOUD_PASSWORD",
    "COURSELENS_CLOUD_DEEPSEEK_API_KEY",
    "COURSELENS_CLOUD_RULES_JSON",
    "COURSELENS_CLOUD_STATE_KEY",
)
CLOUD_VARIABLE_NAMES = (
    "COURSELENS_CLOUD_ENABLED",
    "COURSELENS_CLOUD_CONFIG_HASH",
    "COURSELENS_CLOUD_PROTOCOL_VERSION",
    "COURSELENS_CLOUD_RESULT_PUBLIC_KEY",
)
CLOUD_REQUIRED_SECRET_NAMES = (
    "COURSELENS_CLOUD_STUDENT_ID",
    "COURSELENS_CLOUD_PASSWORD",
    "COURSELENS_CLOUD_RULES_JSON",
    "COURSELENS_CLOUD_STATE_KEY",
)

# Cloud actions accepted by the control plane (closed set).
CLOUD_ACTIONS = {
    "verify-cloud-credentials", "enable-cloud", "disable-cloud", "run-now",
    "update-account", "reset-circuit", "retry-import",
    "revoke-cloud-credentials", "erase-cloud-data",
}

# Verification evidence expires with its binding; a pending record from a
# crashed cloud run stays non-repeatable for one day, then becomes retryable.
CLOUD_PENDING_TTL_SECONDS = 24 * 3600

# The verify action returns as soon as the workflow is dispatched, while the
# GitHub Actions run needs tens of seconds; a back-to-back enable waits this
# bounded window instead of racing the runner into cloud_verification_required.
CLOUD_VERIFICATION_WAIT_SECONDS = 120.0
CLOUD_VERIFICATION_WAIT_INTERVAL_SECONDS = 5.0

# AS3（用户拍板 2026-09-23）：熔断器默认值与「使用期不设额度门」政策对齐——
# 恰在各校验范围上限（20 讲/日、2000 分钟/日、200 万 token/日）；
# 熔断器本身保留兜底，执行端只读规则配置值。
DEFAULT_BUDGET = {
    "max_lectures": 20,
    "max_runner_minutes": 2_000,
    "max_deepseek_tokens": 2_000_000,
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _iso_timestamp(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _beijing_now() -> datetime:
    return datetime.now(SHANGHAI)


def next_cloud_windows(*, now: datetime | None = None, count: int = 2) -> list[str]:
    """Return the next fixed Beijing windows as ISO strings with offset."""
    moment = (now or _beijing_now()).astimezone(SHANGHAI)
    candidates: list[datetime] = []
    # Three days, not two: on a weekend evening every remaining window of the
    # current day may already be past (Saturday night keeps only the nightly
    # fallback), so reaching across to the next weekday's class grid keeps
    # next_two at two honest windows.
    for offset in (0, 1, 2):
        day = (moment + timedelta(days=offset)).date()
        for hour, minute in _cloud_windows_for_day(day):
            candidates.append(moment.replace(
                year=day.year, month=day.month, day=day.day,
                hour=hour, minute=minute, second=0, microsecond=0,
            ))
    upcoming = sorted(value for value in candidates if value > moment)
    return [value.isoformat() for value in upcoming[:count]]


def _schedule_delay_code(created_local: datetime) -> str:
    """Compare one scheduled run against the nearest prior fixed window."""
    moment = created_local.astimezone(SHANGHAI)
    candidates: list[datetime] = []
    for offset in (-1, 0):
        day = (moment + timedelta(days=offset)).date()
        for hour, minute in _cloud_windows_for_day(day):
            candidates.append(moment.replace(
                year=day.year, month=day.month, day=day.day,
                hour=hour, minute=minute, second=0, microsecond=0,
            ))
    prior = [value for value in candidates if value <= moment]
    if not prior:
        return ""
    expected = max(prior)
    if (moment - expected).total_seconds() > 20 * 60:
        return "schedule_delayed"
    return ""


class AutomationError(RuntimeError):
    def __init__(self, message: str, *, code: str = "automation_failed"):
        super().__init__(message)
        self.code = code


class AutomationService:
    def __init__(
        self,
        store: TaskStore,
        credentials: CredentialStore,
        github_provider: Callable[[], GitHubAppClient],
        *,
        local_schedule_getter: Callable[[], dict[str, Any]],
        local_schedule_setter: Callable[[dict[str, Any]], dict[str, Any]],
        result_importer: Callable[[dict[str, Any]], None] | None = None,
        verified_catalog_getter: Callable[[], dict[str, list[str]]] | None = None,
    ):
        self.store = store
        self.credentials = credentials
        self.github_provider = github_provider
        self.local_schedule_getter = local_schedule_getter
        self.local_schedule_setter = local_schedule_setter
        self.result_importer = result_importer
        # Backend-provided verified playable-lecture catalog (course_id ->
        # sorted playable sub_ids) for the current account/session.  The
        # selection baseline is captured only through this getter; a
        # frontend-supplied baseline is never trusted.
        self.verified_catalog_getter = verified_catalog_getter
        # Bounded verification wait (tests shrink these); see _await_pending_cloud_verification.
        self.verification_wait_seconds = CLOUD_VERIFICATION_WAIT_SECONDS
        self.verification_wait_interval_seconds = CLOUD_VERIFICATION_WAIT_INTERVAL_SECONDS

    # ------------------------------------------------------------------
    # Profile access and v1 fail-closed migration
    # ------------------------------------------------------------------

    def _profile(self) -> dict[str, Any]:
        profile = self.store.get_automation_profile()
        if str(profile.get("protocol") or "") != CLOUD_PROTOCOL_VERSION:
            profile = self._migrate_legacy_profile(profile)
        return profile

    def _migrate_legacy_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        """Migrate a legacy (v1/v2 or empty) profile fail closed.

        Cloud stays disabled and the unverifiable config hash is discarded;
        the user must review the new disclosure and re-verify before any
        schedule can run again.  A still-enabled remote schedule is disabled
        best effort so a legacy flag cannot keep processing.  Stored course
        rules are dropped: v3 selection requires a fresh verified baseline.
        """
        if self.credentials.has_secret("github_worker_repo"):
            try:
                github = self.github_provider()
                github.put_worker_variable("COURSELENS_CLOUD_ENABLED", "false")
                github.set_workflow_enabled(CLOUD_DAILY_WORKFLOW, False)
            except (GitHubAppError, AutomationError):
                pass
        now = time.time()
        self.store.replace_automation_rules([])
        saved = self.store.save_automation_profile({
            "mode": "local", "state": "disabled",
            "budget": dict(profile.get("budget") or DEFAULT_BUDGET),
            "config_hash": "", "verified_config_hash": "",
            "verification": {
                "state": "migrated_fail_closed", "source": "local_migration",
                "code": "cloud_v1_profile_migrated", "observed_at": now,
            },
            "protocol": CLOUD_PROTOCOL_VERSION, "account_id": "",
            "binding": {"generation": 0},
            "local_schedule_was_enabled": bool(profile.get("local_schedule_was_enabled")),
            "observed_at": now, "expires_at": now + 90,
        })
        self._event("config", {"state": "migrated_fail_closed", "protocol": CLOUD_PROTOCOL_VERSION})
        return saved

    # ------------------------------------------------------------------
    # Validation and configuration binding
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_config(value: dict[str, Any]) -> dict[str, Any]:
        raw = dict(value or {})
        budget_raw = dict(raw.get("budget") or {})
        budget = {
            "max_lectures": int(budget_raw.get("max_lectures", DEFAULT_BUDGET["max_lectures"])),
            "max_runner_minutes": int(budget_raw.get("max_runner_minutes", DEFAULT_BUDGET["max_runner_minutes"])),
            "max_deepseek_tokens": int(budget_raw.get("max_deepseek_tokens", DEFAULT_BUDGET["max_deepseek_tokens"])),
        }
        if not 1 <= budget["max_lectures"] <= 20:
            raise AutomationError("Lecture budget is out of range", code="cloud_budget_invalid")
        if not 30 <= budget["max_runner_minutes"] <= 2_000:
            raise AutomationError("Runner budget is out of range", code="cloud_budget_invalid")
        if not 0 <= budget["max_deepseek_tokens"] <= 2_000_000:
            raise AutomationError("Token budget is out of range", code="cloud_budget_invalid")
        return {"budget": budget}

    def _validate_rules(self, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # v3 固定包：一条选中课程规则就是一个完整无人值守包（字幕 ASR + 幻灯片
        # OCR + AI 总结/章节 + Lecture IR/evidence）。课程级输出选择器不复存在；
        # quiz 保持手动。baseline/selection_generation 永远由后端捕获，请求里的
        # 同名字段一律丢弃。
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in list(values or []):
            value = dict(raw or {})
            course_id = str(value.get("course_id") or "").strip()
            if not course_id or len(course_id) > 80 or course_id in seen:
                continue
            seen.add(course_id)
            max_minutes = int(value.get("max_lecture_minutes") or 240)
            if not 5 <= max_minutes <= 600:
                raise AutomationError("Lecture duration limit is invalid", code="cloud_rule_invalid")
            output.append({
                "course_id": course_id,
                "priority": max(0, min(100, int(value.get("priority") or 50))),
                "max_lecture_minutes": max_minutes,
            })
        return output

    def _verified_playable_catalog(self) -> dict[str, list[str]]:
        getter = self.verified_catalog_getter
        if not callable(getter):
            return {}
        value = getter()
        if not isinstance(value, dict):
            return {}
        return {
            str(course_id): [str(item) for item in (lectures or [])]
            for course_id, lectures in value.items()
            if str(course_id or "").strip()
        }

    def _merge_selection_baselines(
        self, requested: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        """Atomically merge selection generations and baselines into rules.

        Retained courses keep their stored generation/baseline untouched
        (settings changes never reset them).  Newly added courses capture a
        fresh generation from the backend-verified playable catalog; any
        failure raises before a single rule row is written.  Returns the
        merged rules plus the advanced profile-level generation counter so a
        removed-and-re-added course always gets a strictly fresh generation.
        """
        stored = {
            str(item.get("course_id") or ""): dict(item)
            for item in self.store.list_automation_rules()
        }
        binding = dict((self._profile().get("binding") or {}))
        generation = max(
            [int((item.get("selection_generation") or 0)) for item in stored.values()]
            + [int(binding.get("selection_generation") or 0)]
        )
        total_baseline = sum(
            len(item.get("baseline") or []) for item in stored.values()
        )
        catalog: dict[str, list[str]] | None = None
        new_ids = [
            str(rule.get("course_id") or "") for rule in requested
            if str(rule.get("course_id") or "") not in stored
        ]
        if new_ids:
            catalog = self._verified_playable_catalog()
            if not catalog:
                raise AutomationError(
                    "A verified catalog is required to enable a course",
                    code="cloud_catalog_unavailable",
                )
        merged: list[dict[str, Any]] = []
        for rule in requested:
            course_id = str(rule.get("course_id") or "")
            if course_id in stored:
                prior = stored[course_id]
                rule["selection_generation"] = int(prior.get("selection_generation") or 0)
                rule["baseline"] = [
                    str(item) for item in (prior.get("baseline") or [])
                    if str(item or "").strip()
                ]
                merged.append(rule)
                continue
            assert catalog is not None
            playable = catalog.get(course_id)
            if playable is None:
                raise AutomationError(
                    "Course is not in the verified catalog", code="cloud_course_not_verified",
                )
            playable = sorted({
                str(item).strip() for item in playable
                if str(item or "").strip()
            })
            if any(len(item) > COURSE_BASELINE_ID_MAX_LENGTH for item in playable):
                raise AutomationError(
                    "A lecture identifier exceeds the bounded envelope",
                    code="cloud_rule_invalid",
                )
            if len(playable) > COURSE_BASELINE_MAX_LECTURES:
                raise AutomationError(
                    "Course baseline exceeds the bounded envelope",
                    code="cloud_baseline_too_large",
                )
            total_baseline += len(playable)
            if total_baseline > BASELINE_TOTAL_MAX_ENTRIES:
                raise AutomationError(
                    "Total selection baseline exceeds the bounded envelope",
                    code="cloud_baseline_too_large",
                )
            generation += 1
            rule["selection_generation"] = generation
            rule["baseline"] = playable
            merged.append(rule)
        return merged, generation

    def _selected_account(self, account_id: str) -> dict[str, Any]:
        account_id = str(account_id or "").strip()
        if not account_id or len(account_id) > 64:
            raise AutomationError("A saved account must be selected", code="cloud_account_required")
        accounts = {
            str(item.get("student_id") or ""): item
            for item in self.credentials.list_accounts()
            if isinstance(item, dict)
        }
        account = accounts.get(account_id)
        if account is None:
            raise AutomationError("Selected saved account no longer exists", code="cloud_account_missing")
        if bool(account.get("requires_rotation")):
            raise AutomationError(
                "Selected saved account requires a password update", code="cloud_account_rotation_required",
            )
        return account

    def _worker_binding(self) -> dict[str, Any]:
        github = self.github_provider()
        integrity = github.check_worker_integrity()
        if not integrity.get("trusted"):
            raise AutomationError("Worker tree is not trusted", code="worker_tree_drifted")
        return {
            "worker_repository": str(integrity.get("repository") or ""),
            "worker_tree": str(integrity.get("actual_tree") or ""),
            "worker_manifest": str(integrity.get("manifest_sha256") or ""),
            "trust_epoch": int(integrity.get("trust_epoch") or 0),
        }

    def _config_digest(
        self, binding: dict[str, Any], account_id: str, generation: int,
        budget: dict[str, Any], rules: list[dict[str, Any]],
    ) -> str:
        payload = {
            "schema": CLOUD_PROTOCOL_VERSION,
            "disclosure_version": CLOUD_DISCLOSURE_VERSION,
            "schedule": {
                "times": list(_cloud_schedule_times()),
                "timezone": CLOUD_SCHEDULE["timezone"],
                "weekday_times": list(CLOUD_SCHEDULE["weekday_times"]),
                "daily_times": list(CLOUD_SCHEDULE["daily_times"]),
            },
            "binding": binding,
            "account_id": str(account_id),
            "credential_generation": int(generation),
            "budget": budget,
            "rules": rules,
        }
        return hashlib.sha256(_canonical(payload)).hexdigest()

    def _binding_matches(self, profile: dict[str, Any], binding: dict[str, Any]) -> bool:
        stored = dict(profile.get("binding") or {})
        return all(
            str(stored.get(key) or "") == str(binding.get(key) or "")
            for key in ("worker_repository", "worker_tree", "worker_manifest")
        ) and int(stored.get("trust_epoch") or 0) == int(binding.get("trust_epoch") or 0)

    def _account_issue(self, profile: dict[str, Any]) -> str:
        """Return a closed code when the selected saved account is unusable."""
        account_id = str(profile.get("account_id") or "")
        if not account_id:
            return "cloud_account_missing"
        accounts = {
            str(item.get("student_id") or ""): item
            for item in self.credentials.list_accounts()
            if isinstance(item, dict)
        }
        account = accounts.get(account_id)
        if account is None:
            return "cloud_account_missing"
        if bool(account.get("requires_rotation")):
            return "cloud_account_rotation_required"
        return ""

    # ------------------------------------------------------------------
    # Configuration draft (stage 1: configure disabled)
    # ------------------------------------------------------------------

    def update_config(self, body: dict[str, Any]) -> dict[str, Any]:
        source = dict(body.get("config") or body)
        budget = self._validate_config(source).get("budget") or dict(DEFAULT_BUDGET)
        account_id = str(source.get("account_id") or "").strip()
        self._selected_account(account_id)
        requested = self._validate_rules(list(source.get("rules") or []))
        # Baselines merge atomically before any store write: an unavailable
        # catalog or an over-sized baseline fails closed with rules unchanged.
        rules, selection_generation = self._merge_selection_baselines(requested)
        # Any configuration change invalidates the previous verification.
        self.store.replace_automation_rules(rules)
        now = time.time()
        current_binding = dict(
            (self.store.get_automation_profile().get("binding") or {})
        )
        self.store.save_automation_profile({
            "mode": "local", "state": "disabled",
            "budget": budget, "account_id": account_id,
            "config_hash": "", "verified_config_hash": "",
            "verification": {"state": "draft", "source": "local", "observed_at": now},
            "protocol": CLOUD_PROTOCOL_VERSION,
            "binding": {
                **current_binding,
                "generation": int(current_binding.get("generation") or 0),
                "selection_generation": selection_generation,
            },
            "observed_at": now, "expires_at": now + 90,
        })
        self._event("config", {"state": "draft", "selected_courses": len(rules)})
        return self.snapshot(refresh=False)

    # ------------------------------------------------------------------
    # Encrypted upload (stage 2-3: disclosure + secret-name readback)
    # ------------------------------------------------------------------

    def upload_cloud_secrets(self, body: dict[str, Any]) -> dict[str, Any]:
        source = dict(body or {})
        self._require_disclosure(source)
        account_id = str(source.get("account_id") or "").strip()
        self._selected_account(account_id)
        profile = self._profile()
        return self._upload_core(profile, account_id)

    @staticmethod
    def _require_disclosure(source: dict[str, Any]) -> None:
        version = str(source.get("disclosure_version") or "").strip()
        confirmed = source.get("confirmed")
        if version != CLOUD_DISCLOSURE_VERSION or confirmed is not True:
            raise AutomationError(
                "Cloud custody disclosure was not acknowledged", code="cloud_disclosure_required",
            )

    def _upload_core(self, profile: dict[str, Any], account_id: str) -> dict[str, Any]:
        rules = self.store.list_automation_rules()
        budget = dict(profile.get("budget") or DEFAULT_BUDGET)
        github = self.github_provider()
        binding = self._worker_binding()
        # Fail closed first: schedules stop before any secret material moves.
        github.set_workflow_enabled(CLOUD_DAILY_WORKFLOW, False)
        github.put_worker_variable("COURSELENS_CLOUD_ENABLED", "false")
        # Load the exact saved account through the existing CredentialStore.
        _, password = self.credentials.load(account_id)
        values = {
            "COURSELENS_CLOUD_STUDENT_ID": str(account_id),
            "COURSELENS_CLOUD_PASSWORD": str(password),
        }
        # v3 fixed bundle: every selected course promises subtitle ASR, slide
        # OCR, and AI summary/chapters, so the AI key is required before any
        # course can be enabled.  A transient AI outage after work started
        # preserves completed ASR/OCR via the encrypted checkpoint instead.
        needs_ai = bool(rules)
        if needs_ai:
            if not self.credentials.has_deepseek_key():
                raise AutomationError("DeepSeek key is required by the selected rules", code="deepseek_key_missing")
            values["COURSELENS_CLOUD_DEEPSEEK_API_KEY"] = self.credentials.load_deepseek_key()
        private_name = "cloud_result_private_key"
        public_name = "cloud_result_public_key"
        if not self.credentials.has_secret(private_name) or not self.credentials.has_secret(public_name):
            private_key, public_key = generate_box_keypair()
            self.credentials.save_secret(private_name, private_key)
            self.credentials.save_secret(public_name, public_key)
        generation = int((profile.get("binding") or {}).get("generation") or 0) + 1
        digest = self._config_digest(binding, account_id, generation, budget, rules)
        values["COURSELENS_CLOUD_RULES_JSON"] = _canonical({
            "schema": CLOUD_PROTOCOL_VERSION,
            "config_hash": digest,
            "account_id": str(account_id),
            "budget": budget,
            "rules": rules,
        }).decode("utf-8")
        if self.credentials.has_secret("cloud_state_key"):
            state_key = self.credentials.load_secret("cloud_state_key")
        else:
            state_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
            self.credentials.save_secret("cloud_state_key", state_key)
        values["COURSELENS_CLOUD_STATE_KEY"] = state_key
        for name, value in values.items():
            if value:
                github.put_worker_secret(name, value)
            else:
                github.delete_worker_secret(name)
        github.put_worker_variable("COURSELENS_CLOUD_CONFIG_HASH", digest)
        github.put_worker_variable("COURSELENS_CLOUD_PROTOCOL_VERSION", CLOUD_PROTOCOL_VERSION)
        github.put_worker_variable(
            "COURSELENS_CLOUD_RESULT_PUBLIC_KEY", self.credentials.load_secret(public_name)
        )
        # Secret-name readback: GitHub confirms presence, never values.
        observed = github.list_worker_secrets()
        present = {item["name"] for item in observed}
        if not set(CLOUD_REQUIRED_SECRET_NAMES).issubset(present):
            raise AutomationError(
                "GitHub did not confirm all required secret names", code="cloud_secret_upload_incomplete",
            )
        now = time.time()
        self.store.save_automation_profile({
            "mode": "cloud", "state": "configuring", "account_id": str(account_id),
            "config_hash": digest, "verified_config_hash": "",
            "verification": {
                "state": "uploaded", "source": "github_api", "observed_at": now,
                "secret_names_confirmed": sorted(set(CLOUD_REQUIRED_SECRET_NAMES)),
            },
            "binding": {
                **binding, "generation": generation,
                "disclosure_version": CLOUD_DISCLOSURE_VERSION,
                "resume_after_verified": False,
            },
            "protocol": CLOUD_PROTOCOL_VERSION,
            "observed_at": now, "expires_at": now + 90,
        })
        self._event("secrets", {"state": "uploaded", "count": len(present)})
        return {"state": "uploaded", "observed_at": now}

    # ------------------------------------------------------------------
    # Idempotent actions
    # ------------------------------------------------------------------

    def action(self, action: str, *, operation_id: str) -> dict[str, Any]:
        action = str(action or "").strip().lower()
        if action not in CLOUD_ACTIONS:
            raise AutomationError("Automation action is unsupported", code="automation_action_invalid")
        if not OPERATION_RE.fullmatch(str(operation_id or "")):
            raise AutomationError("operation_id is invalid", code="operation_id_invalid")
        stored_id = f"automation:{operation_id}"
        operation, created = self.store.begin_remote_operation(stored_id, action, "automation")
        if not created and not fresh_operations_requested():
            if operation.get("action") != action:
                raise AutomationError("operation_id was already used", code="operation_id_conflict")
            return self._public_operation(operation, operation_id)
        try:
            result = getattr(self, "_action_" + action.replace("-", "_"))()
            operation = self.store.finish_remote_operation(
                stored_id, state="accepted", result={"state": str(result.get("state") or "accepted")}
            )
        except (AutomationError, GitHubAppError) as exc:
            code = str(getattr(exc, "code", "") or "automation_failed")
            self.store.finish_remote_operation(stored_id, state="failed", error_code=code)
            raise AutomationError("Automation operation failed", code=code) from exc
        return self._public_operation(operation, operation_id)

    @staticmethod
    def _public_operation(value: dict[str, Any], operation_id: str) -> dict[str, Any]:
        return {
            "operation_id": operation_id,
            "action": str(value.get("action") or ""),
            "state": str(value.get("state") or "unknown"),
            "error_code": str(value.get("error_code") or ""),
            "updated_at": float(value.get("updated_at") or 0),
        }

    def _action_verify_cloud_credentials(self) -> dict[str, Any]:
        profile = self._profile()
        if str(profile.get("mode")) != "cloud" or not profile.get("config_hash"):
            raise AutomationError("Cloud configuration is incomplete", code="cloud_config_incomplete")
        github = self.github_provider()
        integrity = github.check_worker_integrity()
        if not integrity.get("trusted"):
            raise AutomationError("Worker tree is not trusted", code="worker_tree_drifted")
        github.set_workflow_enabled(CLOUD_VERIFY_WORKFLOW, True)
        requested_at = time.time()
        github.dispatch_workflow(CLOUD_VERIFY_WORKFLOW, inputs={"config_hash": str(profile.get("config_hash") or "")})
        self.store.save_automation_profile({
            "state": "verifying", "verification": {
                "state": "requested", "source": "github_api", "requested_at": requested_at,
                "config_hash": str(profile.get("config_hash") or ""),
            }, "observed_at": requested_at, "expires_at": requested_at + 90,
        })
        self._event("verification", {"state": "requested"})
        return {"state": "verifying"}

    def _action_enable_cloud(self) -> dict[str, Any]:
        profile = self._profile()
        # Fail fast with the precise precondition code before reconciling.
        if not self._binding_matches(profile, self._worker_binding()):
            raise AutomationError("Cloud binding no longer matches the worker", code="cloud_binding_invalid")
        account_issue = self._account_issue(profile)
        if account_issue:
            raise AutomationError("Selected saved account is unavailable", code=account_issue)
        self.reconcile(force=True)
        profile = self._profile()
        self._await_pending_cloud_verification(profile)
        profile = self._profile()
        if not profile.get("config_hash") or profile.get("verified_config_hash") != profile.get("config_hash"):
            raise AutomationError("Current configuration has not passed verification", code="cloud_verification_required")
        github = self.github_provider()
        local = dict(self.local_schedule_getter() or {})
        # Transactional enable: each failed stage rolls the previous one back.
        github.put_worker_variable("COURSELENS_CLOUD_ENABLED", "true")
        try:
            github.set_workflow_enabled(CLOUD_DAILY_WORKFLOW, True)
        except GitHubAppError:
            try:
                github.put_worker_variable("COURSELENS_CLOUD_ENABLED", "false")
            except GitHubAppError:
                pass
            raise
        now = time.time()
        self.store.save_automation_profile({
            "state": "ready", "local_schedule_was_enabled": bool(local.get("enabled")),
            "observed_at": now, "expires_at": now + 90,
        })
        if local.get("enabled"):
            self.local_schedule_setter({**local, "enabled": False})
        self._event("mode", {"state": "ready", "mode": "cloud"})
        return {"state": "ready"}

    def _await_pending_cloud_verification(self, profile: dict[str, Any]) -> None:
        """Bounded wait while a dispatched verification run is still pending.

        Only a requested verification waits; any other state (verified /
        failed / unknown / invalidated) goes straight back to the caller's
        original fail-closed judgment. Never re-dispatches verification, and
        the sleep never overshoots the wait window. On timeout the closed-set
        code cloud_verification_timeout surfaces.
        """
        if str((dict(profile.get("verification") or {})).get("state") or "") != "requested":
            return
        deadline = time.monotonic() + float(self.verification_wait_seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AutomationError(
                    "Cloud verification did not conclude within the wait window",
                    code="cloud_verification_timeout",
                )
            time.sleep(min(float(self.verification_wait_interval_seconds), remaining))
            self.reconcile(force=True)
            verification = dict(self._profile().get("verification") or {})
            if str(verification.get("state") or "") != "requested":
                return

    def _action_disable_cloud(self) -> dict[str, Any]:
        github = self.github_provider()
        github.put_worker_variable("COURSELENS_CLOUD_ENABLED", "false")
        github.set_workflow_enabled(CLOUD_DAILY_WORKFLOW, False)
        profile = self._profile()
        binding = dict(profile.get("binding") or {})
        if binding.get("resume_after_verified"):
            binding["resume_after_verified"] = False
        local = dict(self.local_schedule_getter() or {})
        if profile.get("local_schedule_was_enabled") and not local.get("enabled"):
            self.local_schedule_setter({**local, "enabled": True})
        now = time.time()
        self.store.save_automation_profile({
            "state": "disabled", "binding": binding,
            "observed_at": now, "expires_at": now + 90,
        })
        self._event("mode", {"state": "paused", "mode": "cloud"})
        return {"state": "disabled"}

    def _action_run_now(self) -> dict[str, Any]:
        profile = self._profile()
        if profile.get("verified_config_hash") != profile.get("config_hash") or not profile.get("config_hash"):
            raise AutomationError("Current configuration has not passed verification", code="cloud_verification_required")
        if not self._binding_matches(profile, self._worker_binding()):
            raise AutomationError("Cloud binding no longer matches the worker", code="cloud_binding_invalid")
        github = self.github_provider()
        github.dispatch_workflow(CLOUD_DAILY_WORKFLOW, inputs={
            "trigger_kind": "manual", "config_hash": str(profile.get("config_hash") or "")
        })
        now = time.time()
        self.store.save_automation_profile({"state": "running", "observed_at": now, "expires_at": now + 30})
        self._event("run", {"state": "submitted"})
        return {"state": "running"}

    def _action_update_account(self) -> dict[str, Any]:
        """Re-upload the stored account, re-verify, and resume only on success."""
        profile = self._profile()
        account_id = str(profile.get("account_id") or "").strip()
        if not account_id or not profile.get("config_hash"):
            raise AutomationError("Cloud configuration is incomplete", code="cloud_config_incomplete")
        binding = dict(profile.get("binding") or {})
        if str(binding.get("disclosure_version") or "") != CLOUD_DISCLOSURE_VERSION:
            raise AutomationError(
                "Cloud custody disclosure was not acknowledged", code="cloud_disclosure_required",
            )
        self._selected_account(account_id)
        was_enabled = str(profile.get("state") or "") in {"ready", "running"}
        result = self._upload_core(profile, account_id)
        now = time.time()
        self.store.save_automation_profile({
            "verification": {
                "state": "requested", "source": "github_api", "requested_at": now,
                "config_hash": str(self._profile().get("config_hash") or ""),
            }, "state": "verifying", "observed_at": now, "expires_at": now + 90,
            "binding": {**dict(self._profile().get("binding") or {}), "resume_after_verified": was_enabled},
        })
        github = self.github_provider()
        github.set_workflow_enabled(CLOUD_VERIFY_WORKFLOW, True)
        github.dispatch_workflow(CLOUD_VERIFY_WORKFLOW, inputs={
            "config_hash": str(self._profile().get("config_hash") or "")
        })
        self._event("secrets", {"state": "update_requested", "resume_after_verified": was_enabled})
        return {"state": "verifying", "uploaded": str(result.get("state") or "")}

    def _action_reset_circuit(self) -> dict[str, Any]:
        profile = self._profile()
        github = self.github_provider()
        requested_at = time.time()
        github.set_workflow_enabled(CLOUD_VERIFY_WORKFLOW, True)
        github.dispatch_workflow(CLOUD_VERIFY_WORKFLOW, inputs={
            "config_hash": str(profile.get("config_hash") or ""),
            "reset_circuit": "true",
        })
        self.store.save_automation_profile({
            "state": "verifying", "verification": {
                "state": "requested", "source": "github_api", "requested_at": requested_at,
                "config_hash": str(profile.get("config_hash") or ""),
                "reset_circuit": True,
            }, "observed_at": requested_at, "expires_at": requested_at + 90,
        })
        self._event("circuit", {"state": "reset_requested"})
        return {"state": "verifying"}

    def _action_retry_import(self) -> dict[str, Any]:
        imports = self.store.list_automation_imports()
        for item in imports:
            if item.get("state") in {"failed", "cleanup_pending"}:
                self.store.upsert_automation_import(int(item["artifact_id"]), state="available", error_code="")
        self._event("imports", {"state": "retry_requested"})
        return {"state": "accepted"}

    def _action_revoke_cloud_credentials(self) -> dict[str, Any]:
        """Disable, stop active runs boundedly, then delete exact cloud data.

        A delete that fails ambiguously is never retried inside the same
        operation; the profile lands in a recoverable ``cleanup_pending``.
        """
        github = self.github_provider()
        now = time.time()
        self.store.save_automation_profile({"state": "revoking", "observed_at": now, "expires_at": now + 30})
        github.put_worker_variable("COURSELENS_CLOUD_ENABLED", "false")
        github.set_workflow_enabled(CLOUD_DAILY_WORKFLOW, False)
        cancelled_runs: set[int] = set()
        for workflow in (CLOUD_DAILY_WORKFLOW, CLOUD_VERIFY_WORKFLOW):
            for run in github.list_workflow_runs(workflow, limit=20):
                if run.get("status") in {"queued", "in_progress", "waiting", "requested", "pending"}:
                    run_id = int(run["id"])
                    github.cancel_workflow_run(run_id)
                    cancelled_runs.add(run_id)
        deadline = time.monotonic() + 20.0
        while cancelled_runs:
            active: set[int] = set()
            for workflow in (CLOUD_DAILY_WORKFLOW, CLOUD_VERIFY_WORKFLOW):
                for run in github.list_workflow_runs(workflow, limit=20):
                    run_id = int(run["id"])
                    if run_id in cancelled_runs and run.get("status") in {
                        "queued", "in_progress", "waiting", "requested", "pending",
                    }:
                        active.add(run_id)
            if not active:
                break
            if time.monotonic() >= deadline:
                now = time.time()
                self.store.save_automation_profile({
                    "state": "cleanup_pending", "observed_at": now, "expires_at": now + 90,
                    "verification": {
                        "state": "cleanup_pending", "source": "github_api", "observed_at": now,
                        "code": "cloud_run_cancel_pending", "remaining_count": len(active),
                    },
                })
                self._event("revoke", {"state": "cleanup_pending", "remaining_run_count": len(active)})
                raise AutomationError(
                    "Cloud runs are still stopping", code="cloud_run_cancel_pending"
                )
            time.sleep(1.0)
            cancelled_runs = active
        failures = []
        for name in CLOUD_SECRET_NAMES:
            try:
                github.delete_worker_secret(name)
            except GitHubAppError:
                failures.append(name)
        for name in CLOUD_VARIABLE_NAMES:
            try:
                github.delete_worker_variable(name)
            except GitHubAppError:
                failures.append(name)
        artifact_failures = 0
        try:
            for artifact in github.list_worker_artifacts(limit=100):
                if str(artifact.get("name") or "").startswith(("courselens-cloud-result-", "courselens-cloud-state-")):
                    try:
                        github.delete_worker_artifact(int(artifact["id"]))
                    except GitHubAppError:
                        artifact_failures += 1
        except GitHubAppError:
            artifact_failures += 1
        remaining_secrets = {item["name"] for item in github.list_worker_secrets()}
        remaining_variables = set(github.list_worker_variables())
        remaining = sorted(
            (set(CLOUD_SECRET_NAMES) & remaining_secrets)
            | (set(CLOUD_VARIABLE_NAMES) & remaining_variables)
        )
        state = "cleanup_pending" if failures or remaining or artifact_failures else "disabled"
        self.store.save_automation_profile({
            "state": state, "verified_config_hash": "", "config_hash": "", "account_id": "",
            "verification": {
                "state": "revoked" if state == "disabled" else "cleanup_pending",
                "source": "github_api", "observed_at": time.time(),
                "remaining_count": len(remaining) + artifact_failures,
            },
            "binding": {"generation": 0}, "protocol": CLOUD_PROTOCOL_VERSION,
            "observed_at": time.time(), "expires_at": time.time() + 90,
        })
        self._event("revoke", {"state": state, "remaining_count": len(remaining) + artifact_failures})
        if state == "cleanup_pending":
            raise AutomationError("Some cloud data could not be removed", code="cloud_cleanup_pending")
        return {"state": state}

    def _action_erase_cloud_data(self) -> dict[str, Any]:
        github = self.github_provider()
        failures = 0
        for artifact in github.list_worker_artifacts(limit=100):
            if str(artifact.get("name") or "").startswith(("courselens-cloud-result-", "courselens-cloud-state-")):
                try:
                    github.delete_worker_artifact(int(artifact["id"]))
                except GitHubAppError:
                    failures += 1
        if failures:
            raise AutomationError("Some cloud artifacts could not be removed", code="cloud_cleanup_pending")
        self.credentials.delete_secret("cloud_result_private_key")
        self.credentials.delete_secret("cloud_result_public_key")
        self.credentials.delete_secret("cloud_state_key")
        self._event("erase", {"state": "completed"})
        return {"state": "completed"}

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def reconcile(self, *, force: bool = False) -> dict[str, Any]:
        profile = self._profile()
        if not force and time.time() - float(profile.get("observed_at") or 0) < 15:
            return self.snapshot(refresh=False)
        if not self.credentials.has_secret("github_worker_repo"):
            return self.snapshot(refresh=False)
        github = self.github_provider()
        try:
            resources = github.inspect_managed_resources()
            secret_names = set(resources.get("secret_names") or [])
            variables = dict(resources.get("variables") or {})
            workflows = dict(resources.get("workflows") or {})
            now = time.time()
            verification = dict(profile.get("verification") or {})
            requested_at = float(verification.get("requested_at") or 0)
            # Import the newest encrypted state first so verification evidence
            # is available before run conclusions are applied.
            state_artifacts = [
                item for item in github.list_worker_artifacts(prefix="courselens-cloud-", limit=100)
                if str(item.get("name") or "").startswith("courselens-cloud-state-")
                and not item.get("expired")
            ]
            if state_artifacts and self.credentials.has_secret("cloud_state_key"):
                latest_state = max(state_artifacts, key=lambda item: str(item.get("created_at") or ""))
                self._import_cloud_state(github, int(latest_state["id"]))
            for workflow in (CLOUD_VERIFY_WORKFLOW, CLOUD_DAILY_WORKFLOW):
                for run in github.list_workflow_runs(workflow, limit=10):
                    run_id = int(run["id"])
                    # 第卅二案：定窗空转（conclusion=skipped，秒级无操作）不携带
                    # 任何证据——不入台账、不重盖章，且存量行在下方清扫。
                    if (
                        workflow == CLOUD_DAILY_WORKFLOW
                        and str(run.get("event") or "") == "schedule"
                        and str(run.get("conclusion") or "") == "skipped"
                    ):
                        continue
                    run_error = ""
                    if workflow == CLOUD_DAILY_WORKFLOW and run.get("event") == "schedule":
                        created = _iso_timestamp(str(run.get("created_at") or ""))
                        if created:
                            run_error = _schedule_delay_code(
                                datetime.fromtimestamp(created, SHANGHAI)
                            )
                    self.store.upsert_automation_run(
                        f"{workflow}:{run_id}:{int(run.get('run_attempt') or 1)}",
                        github_run_id=run_id, attempt=int(run.get("run_attempt") or 1),
                        workflow=workflow, trigger_kind=str(run.get("event") or "schedule"),
                        state=str(run.get("status") or "unknown"),
                        conclusion=str(run.get("conclusion") or ""),
                        config_hash=str(profile.get("config_hash") or ""),
                        error_code=run_error, observed_at=now,
                        # 第卅六案①：卡片时间只认 run 自身的时刻——开始=run_started_at
                        # （缺则 created_at），结束=有结论时的 updated_at；两者都没有
                        # 就留 0，前端不发明时间
                        started_at=_iso_timestamp(
                            str(run.get("run_started_at") or run.get("created_at") or "")
                        ),
                        concluded_at=(
                            _iso_timestamp(str(run.get("updated_at") or ""))
                            if str(run.get("conclusion") or "") else 0.0
                        ),
                    )
                    if (
                        workflow == CLOUD_VERIFY_WORKFLOW
                        and requested_at > 0
                        and _iso_timestamp(str(run.get("created_at") or "")) >= requested_at - 5
                        and run.get("status") == "completed"
                    ):
                        # E2E-2v4 定谳缺陷：conclusion 分支毒性写入不得丢
                        # requested_at——首拍收割先于 state artifact 上架
                        # （真机 listing 滞后）时判 evidence_missing，丢字段
                        # 后窗口门（requested_at>0）永久关死，迟到回执不可
                        # 翻转=毒化。语义同下方过期兜底（E2E-2v2 缺陷 A）：
                        # 本地判定不是高于 run 真值的终审。
                        if run.get("conclusion") == "success" and self._verification_evidence_matches(
                            str(profile.get("config_hash") or "")
                        ):
                            verification = {
                                "state": "verified", "source": "github_run", "run_id": run_id,
                                "observed_at": now, "config_hash": str(profile.get("config_hash") or ""),
                                "requested_at": requested_at,
                            }
                            profile["verified_config_hash"] = str(profile.get("config_hash") or "")
                        elif run.get("conclusion") != "success":
                            verification = {
                                "state": "failed", "source": "github_run", "run_id": run_id,
                                "observed_at": now, "code": "cloud_verification_failed",
                                "requested_at": requested_at,
                            }
                        else:
                            verification = {
                                "state": "failed", "source": "github_run", "run_id": run_id,
                                "observed_at": now, "code": "cloud_verification_evidence_missing",
                                "requested_at": requested_at,
                            }
            # 第卅一案：requested 的 90s 窗口此前只写不执行——dispatch 落空
            # （权限/网络/工作流异常）或结论永不到达时，胶囊被永久钉在
            # 「正在确认」。窗口过期即收敛为失败+闭集码；动作面既有
            # verify-cloud-credentials 重试原样保留（人话指引由设置页映射）。
            if (
                verification.get("state") == "requested"
                and requested_at > 0
                and now - requested_at > CLOUD_VERIFY_EXPIRY_SECONDS
            ):
                verification = {
                    "state": "failed", "source": "local_expiry", "observed_at": now,
                    "code": "cloud_verification_timeout",
                    # E2E-2v2 定谳缺陷 A：过期兜底不得丢 requested_at——run 结论
                    # 翻转（requested_at>0 且 created_at>=requested_at-5）依赖此
                    # 字段。真机实测 run 收尾（85s/105s/236s）常晚于 90s 窗，
                    # 丢弃后迟到结论永久不可翻转，enable 第 3 门被锁死。过期只
                    # 是本地展示的 fail-closed 兜底，不是高于 run 真值的终审。
                    "requested_at": requested_at,
                }
                self._event("verification", {"state": "timeout", "code": "cloud_verification_timeout"})
                print("[cloud] verification requested window expired: code=cloud_verification_timeout", flush=True)
            cloud_artifacts = github.list_worker_artifacts(prefix="courselens-cloud-", limit=100)
            for artifact in cloud_artifacts:
                name = str(artifact.get("name") or "")
                if name.startswith("courselens-cloud-result-"):
                    self.store.upsert_automation_import(
                        int(artifact["id"]), artifact_name=name,
                        run_id=int(artifact.get("workflow_run_id") or 0),
                        state="expired" if artifact.get("expired") else "available",
                        expires_at=_iso_timestamp(str(artifact.get("expires_at") or "")) or None,
                        observed_at=now,
                    )
            self._auto_import_available(github)
            required = set(CLOUD_REQUIRED_SECRET_NAMES)
            configured = required.issubset(secret_names)
            enabled = variables.get("COURSELENS_CLOUD_ENABLED", "false").lower() == "true"
            daily_workflow = dict(workflows.get(CLOUD_DAILY_WORKFLOW) or {})
            workflow_ready = bool(
                daily_workflow.get("exists") and daily_workflow.get("state") == "active"
            )
            # 第卅二案：排程冻结（fail-closed）——验证请求在途（requested）或
            # 本地配置摘要 ≠ worker 上传摘要（规则改动未落地/上传失败）时，
            # 定窗 daily workflow 必须保持关停；否则 worker 按旧配置继续处理
            # 旧课程，与课程级开关脱钩。verified 后的复启用既有
            # resume_after_verified 链（was_enabled 基线）。
            uploaded_config_hash = str(
                (variables.get("COURSELENS_CLOUD_CONFIG_HASH") or "")
            ).strip()
            local_config_hash = str(profile.get("config_hash") or "").strip()
            schedule_allowed = bool(
                verification.get("state") != "requested"
                and (not local_config_hash or uploaded_config_hash == local_config_hash)
            )
            if workflow_ready and not schedule_allowed:
                try:
                    github.set_workflow_enabled(CLOUD_DAILY_WORKFLOW, False)
                except GitHubAppError:
                    pass
                workflow_ready = False
                self._event("schedule", {"state": "frozen"})
            # 存量清扫：定窗空转行（skipped，秒级无操作）只制造「进行中」噪音，
            # 不携带任何证据，从台账移除（第卅二案：五条 schedule_delayed 实锤）。
            stale_skipped_keys = [
                str(item.get("run_key") or "")
                for item in self.store.list_automation_runs(limit=200)
                if str(item.get("workflow") or "") == CLOUD_DAILY_WORKFLOW
                and str(item.get("trigger_kind") or "") == "schedule"
                and str(item.get("conclusion") or "") == "skipped"
            ]
            if stale_skipped_keys:
                self.store.delete_automation_runs(stale_skipped_keys)
            verified = bool(
                profile.get("config_hash")
                and profile.get("verified_config_hash") == profile.get("config_hash")
            )
            # Any binding change invalidates verification and disables the
            # scheduled processing until the new config passes verification.
            if verified or enabled:
                binding_valid = self._binding_matches(
                    profile, self._worker_binding()
                ) and not self._account_issue(profile)
                if not binding_valid:
                    self._invalidate_binding(github)
                    verified = False
                    enabled = False
                    state = "configuring"
                    self.store.save_automation_profile({
                        "state": state, "verified_config_hash": "",
                        "verification": {
                            "state": "invalidated", "source": "local_binding_check",
                            "observed_at": now, "code": "cloud_binding_invalid",
                        },
                        "observed_at": now, "expires_at": now + 90,
                    })
                    self._event("binding", {"state": "invalidated"})
                    profile = self._profile()
                    verification = dict(profile.get("verification") or {})
            if verified and bool((dict(profile.get("binding") or {})).get("resume_after_verified")):
                self._resume_after_verified()
                profile = self._profile()
                enabled = True
                workflow_ready = True  # the resume just enabled the daily workflow
            state = str(profile.get("state") or "disabled")
            if enabled and configured and verified and workflow_ready:
                state = "running" if any(
                    run.get("workflow") == CLOUD_DAILY_WORKFLOW and run.get("state") in {"queued", "in_progress", "waiting"}
                    for run in self.store.list_automation_runs(limit=20)
                ) else "ready"
            elif enabled:
                state = "degraded"
            elif state not in {"revoking", "cleanup_pending"}:
                state = "verifying" if verification.get("state") == "requested" else (
                    "configuring" if configured else "disabled"
                )
            circuits = {item["circuit"]: item for item in self.store.list_automation_circuits()}
            if str((circuits.get("authentication") or {}).get("state") or "") == "open":
                state = "circuit_open"
            elif state in {"ready", "running"} and any(
                str((circuits.get(name) or {}).get("state") or "closed") != "closed"
                for name in ("authentication", "deepseek", "platform", "budget")
            ):
                state = "degraded"
            self.store.save_automation_profile({
                "state": state, "verified_config_hash": profile.get("verified_config_hash") or "",
                "verification": verification, "observed_at": now, "expires_at": now + 90,
            })
        except GitHubAppError as exc:
            now = time.time()
            self.store.save_automation_profile({
                "state": "unknown", "verification": {
                    "state": "unknown", "source": "github_api", "observed_at": now,
                    "code": str(exc.code or "github_unreachable"),
                }, "observed_at": now, "expires_at": now + 20,
            })
        return self.snapshot(refresh=False)

    def _invalidate_binding(self, github: GitHubAppClient) -> None:
        try:
            github.put_worker_variable("COURSELENS_CLOUD_ENABLED", "false")
            github.set_workflow_enabled(CLOUD_DAILY_WORKFLOW, False)
        except GitHubAppError:
            pass

    def _resume_after_verified(self) -> None:
        profile = self._profile()
        binding = dict(profile.get("binding") or {})
        binding["resume_after_verified"] = False
        github = self.github_provider()
        github.put_worker_variable("COURSELENS_CLOUD_ENABLED", "true")
        try:
            github.set_workflow_enabled(CLOUD_DAILY_WORKFLOW, True)
        except GitHubAppError:
            try:
                github.put_worker_variable("COURSELENS_CLOUD_ENABLED", "false")
            except GitHubAppError:
                pass
            raise
        now = time.time()
        self.store.save_automation_profile({
            "state": "ready", "binding": binding,
            "observed_at": now, "expires_at": now + 90,
        })
        self._event("mode", {"state": "ready", "mode": "cloud", "reason": "resume_after_verified"})

    def _verification_evidence_matches(self, config_hash: str) -> bool:
        """Check the encrypted verification receipt imported from cloud state."""
        if not config_hash:
            return False
        record = dict(self.store.get_app_state("automation_last_cloud_state", {}) or {})
        evidence = dict(record.get("verification") or {})
        if str(evidence.get("config_hash") or "") != config_hash:
            return False
        if str(evidence.get("protocol") or "") != CLOUD_PROTOCOL_VERSION:
            return False
        receipt = str(evidence.get("receipt") or "")
        if not receipt:
            return False
        if not self.credentials.has_secret("worker_signing_public_key"):
            return False
        message = _canonical({
            "config_hash": str(evidence.get("config_hash") or ""),
            "protocol": str(evidence.get("protocol") or ""),
            "verified_at": float(evidence.get("verified_at") or 0),
        })
        try:
            VerifyKey(base64.b64decode(
                self.credentials.load_secret("worker_signing_public_key").encode("ascii"), validate=True,
            )).verify(message, bytes.fromhex(receipt))
        except (
            ValueError, UnicodeEncodeError, CryptoError,
        ):
            return False
        return True

    def _import_cloud_state(self, github: GitHubAppClient, artifact_id: int) -> None:
        try:
            files = github.download_worker_artifact_files(artifact_id)
            raw = files.get("state.box.json")
            if not raw:
                return
            envelope = json.loads(raw.decode("utf-8"))
            ciphertext = base64.b64decode(
                str(envelope.get("ciphertext") or "").encode("ascii"), validate=True
            )
            if sha256_hex(ciphertext) != str(envelope.get("sha256") or ""):
                return
            key = base64.b64decode(
                self.credentials.load_secret("cloud_state_key").encode("ascii"), validate=True
            )
            value = json.loads(SecretBox(key).decrypt(ciphertext).decode("utf-8"))
            if value.get("schema") != "cloud.state.v1":
                return
            revision = int(value.get("revision") or 0)
            prior_state = dict(self.store.get_app_state("automation_last_cloud_state", {}) or {})
            if revision <= int(prior_state.get("revision") or 0):
                return
            budget = dict(value.get("budget") or {})
            budget_date = str(budget.get("date") or "")
            if budget_date:
                current = self.store.get_automation_budget(budget_date)
                self.store.update_automation_budget(
                    budget_date,
                    lectures=int(budget.get("lectures") or 0) - int(current.get("lectures") or 0),
                    runner_minutes=float(budget.get("runner_minutes") or 0) - float(current.get("runner_minutes") or 0),
                    deepseek_tokens=int(budget.get("deepseek_tokens") or 0) - int(current.get("deepseek_tokens") or 0),
                )
            for name, circuit in dict(value.get("circuits") or {}).items():
                if name not in {"authentication", "deepseek", "platform", "budget"}:
                    continue
                item = dict(circuit or {})
                failures = item.get("failures") or 0
                if isinstance(failures, list):
                    failures = len(failures)
                self.store.set_automation_circuit(
                    name,
                    state=str(item.get("state") or "closed"),
                    consecutive_failures=int(failures or 0),
                    retry_after=item.get("retry_after") or None,
                    last_error_code=str(item.get("last_error_code") or ""),
                    opened_at=float(value.get("updated_at") or 0) if item.get("state") == "open" else None,
                )
            last_run = dict(value.get("last_run") or {})
            self.store.set_app_state("automation_last_cloud_state", {
                "artifact_id": int(artifact_id),
                "revision": revision,
                "budget_date": budget_date,
                "code": str(last_run.get("code") or ""),
                "counts": dict(last_run.get("counts") or {}),
                "elapsed_seconds": float(last_run.get("elapsed_seconds") or 0),
                "verification": dict(value.get("verification") or {}),
                "observed_at": float(value.get("updated_at") or 0),
            })
        except (
            KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError,
            CryptoError, GitHubAppError,
        ):
            return

    def _auto_import_available(self, github: GitHubAppClient) -> None:
        if self.result_importer is None:
            return
        if not self.credentials.has_secret("cloud_result_private_key"):
            for item in self.store.list_automation_imports():
                if item.get("state") == "available":
                    self.store.upsert_automation_import(
                        int(item["artifact_id"]), state="unrecoverable",
                        error_code="cloud_result_key_missing", observed_at=time.time(),
                    )
            return
        if not self.credentials.has_secret("worker_signing_public_key"):
            return
        private_key = self.credentials.load_secret("cloud_result_private_key")
        signing_key = self.credentials.load_secret("worker_signing_public_key")
        imported_hashes = {
            str(item.get("result_hash") or "")
            for item in self.store.list_automation_imports()
            if item.get("state") in {"imported", "cleanup_pending"} and item.get("result_hash")
        }
        for item in self.store.list_automation_imports():
            if item.get("state") != "available":
                continue
            artifact_id = int(item["artifact_id"])
            try:
                self.store.upsert_automation_import(artifact_id, state="downloading", observed_at=time.time())
                files = github.download_worker_artifact_files(artifact_id)
                envelopes = [
                    raw for name, raw in sorted(files.items()) if name.endswith(".box.json")
                ]
                if not envelopes:
                    raise AutomationError("Cloud result artifact is empty", code="cloud_artifact_empty")
                digest = sha256_hex(b"".join(envelopes))
                if digest in imported_hashes:
                    self.store.upsert_automation_import(
                        artifact_id, state="imported", result_hash=digest,
                        error_code="", observed_at=time.time(),
                    )
                else:
                    self.store.upsert_automation_import(artifact_id, state="verifying", observed_at=time.time())
                    results = []
                    for raw in envelopes:
                        envelope = json.loads(raw.decode("utf-8"))
                        task_id = str(envelope.get("task_id") or "")
                        input_hash = str(envelope.get("input_hash") or "")
                        if not input_hash:
                            raise AutomationError("Cloud result has no input digest", code="cloud_result_invalid")
                        results.append(open_result(
                            envelope, private_key, signing_key,
                            expected_task_id=task_id, expected_input_hash=input_hash,
                        ))
                    self.store.upsert_automation_import(artifact_id, state="importing", observed_at=time.time())
                    for result in results:
                        self.result_importer(result)
                    self.store.upsert_automation_import(
                        artifact_id, state="imported", result_hash=digest,
                        error_code="", observed_at=time.time(),
                    )
                    imported_hashes.add(digest)
                try:
                    github.delete_worker_artifact(artifact_id)
                except GitHubAppError:
                    self.store.upsert_automation_import(
                        artifact_id, state="cleanup_pending", result_hash=digest,
                        error_code="cloud_cleanup_pending", observed_at=time.time(),
                    )
            except (AutomationError, GitHubAppError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                code = str(getattr(exc, "code", "") or "cloud_import_failed")
                self.store.upsert_automation_import(
                    artifact_id, state="failed", error_code=code, observed_at=time.time(),
                )

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    @staticmethod
    def _project_run(run: dict[str, Any]) -> dict[str, Any]:
        return {
            # 第卅六案①③：run_key 供终态记录删除；started_at/concluded_at 是 run
            # 自身时刻（0=没有该时刻），observed_at 只是观察时刻，前端不得用它
            # 冒充运行时间。
            "run_key": str(run.get("run_key") or ""),
            "workflow": str(run.get("workflow") or ""),
            "trigger_kind": str(run.get("trigger_kind") or ""),
            "state": str(run.get("state") or ""),
            "conclusion": str(run.get("conclusion") or ""),
            "error_code": str(run.get("error_code") or ""),
            "counts": dict(run.get("counts") or {}),
            "started_at": float(run.get("started_at") or 0),
            "concluded_at": float(run.get("concluded_at") or 0),
            "observed_at": float(run.get("observed_at") or 0),
            "updated_at": float(run.get("updated_at") or 0),
        }

    @staticmethod
    def _project_import(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "artifact_id": int(item.get("artifact_id") or 0),
            "state": str(item.get("state") or ""),
            "error_code": str(item.get("error_code") or ""),
            "expires_at": float(item.get("expires_at") or 0),
            "observed_at": float(item.get("observed_at") or 0),
        }

    @staticmethod
    def _project_rule(rule: dict[str, Any]) -> dict[str, Any]:
        # 规则投影不含 baseline 明细（有界不透明 ID 列表只在本地/加密信封里
        # 存在）；对外只暴露计数与代数，前端永远无法伪造基线。
        projected = {
            str(key): value
            for key, value in dict(rule or {}).items()
            if key not in {"baseline"}
        }
        projected["baseline_count"] = len(dict(rule or {}).get("baseline") or [])
        return projected

    def converge_stale_runs(self, *, now: float | None = None) -> list[str]:
        """第卅六案②：超过收口窗仍无结论的活动行收敛为明确终态「已中止」。

        判据是 ``updated_at``（最后一次被观测到活跃）——持续被对账刷新的真在飞
        任务永远不满足条件，只有不再被观测到的 ghost（远端早已消失/静默）才
        收口。收敛只写本地终态三格，``observed_at`` 保持原值（陈旧度与 7 天
        隐藏判据都读它）；GitHub 真值一旦到达即由 reconcile 覆盖本结论。
        """
        moment = time.time() if now is None else float(now)
        stale_keys = [
            str(run.get("run_key") or "")
            for run in self.store.list_automation_runs(limit=200)
            if not str(run.get("conclusion") or "")
            and str(run.get("state") or "") != "completed"
            and float(run.get("updated_at") or 0.0) > 0
            and moment - float(run.get("updated_at") or 0.0) > AUTOMATION_RUN_STALE_SECONDS
        ]
        for run_key in stale_keys:
            if not run_key:
                continue
            self.store.upsert_automation_run(
                run_key,
                state="completed",
                conclusion=AUTOMATION_RUN_ABANDONED_CONCLUSION,
                error_code=AUTOMATION_RUN_ABANDONED_CODE,
            )
        return [key for key in stale_keys if key]

    def snapshot(self, *, refresh: bool = False) -> dict[str, Any]:
        if refresh:
            return self.reconcile(force=True)
        self.converge_stale_runs()  # 第卅六案②：离线读也收口 ghost run
        profile = self._profile()
        rules = self.store.list_automation_rules()
        today = _beijing_now().strftime("%Y-%m-%d")
        budget = self.store.get_automation_budget(today)
        limits = dict(profile.get("budget") or DEFAULT_BUDGET)
        now = time.time()
        state = str(profile.get("state") or "disabled")
        stale = bool(profile.get("observed_at")) and float(profile.get("expires_at") or 0) < now
        if stale and state not in {"disabled", "cleanup_pending"}:
            state = "unknown"
        binding = dict(profile.get("binding") or {})
        verified = bool(profile.get("config_hash") and profile.get("verified_config_hash") == profile.get("config_hash"))
        circuits = self.store.list_automation_circuits()
        last_state = dict(self.store.get_app_state("automation_last_cloud_state", {}) or {})
        selected = [rule for rule in rules if str(rule.get("course_id") or "") and rule.get("selection_generation") is not None]
        target = str(binding.get("worker_repository") or "")
        # Quota numbers stay hidden in the normal UI state; they surface only
        # while a protection circuit is actually triggered, beside the
        # closed-set deferred reason that explains the pause.
        triggered = any(
            str(item.get("state") or "closed") != "closed" for item in circuits
        )
        return {
            "schema": "courselens.automation.v3",
            "protocol": CLOUD_PROTOCOL_VERSION,
            "disclosure_version": CLOUD_DISCLOSURE_VERSION,
            "state": state,
            "enabled": state in {"ready", "running"},
            "account_id": str(profile.get("account_id") or ""),
            "schedule": {
                "times": list(_cloud_schedule_times()),
                "timezone": CLOUD_SCHEDULE["timezone"],
                "weekday_times": list(CLOUD_SCHEDULE["weekday_times"]),
                "daily_times": list(CLOUD_SCHEDULE["daily_times"]),
                "next_two": next_cloud_windows(),
            },
            "rules": [self._project_rule(rule) for rule in rules],
            "selected_courses": len(selected),
            "outputs_count": sum(1 for _ in selected),
            "budget": {
                "date": today,
                "limits": limits,
                "used": {
                    "lectures": int(budget.get("lectures") or 0),
                    "runner_minutes": round(float(budget.get("runner_minutes") or 0), 2),
                    "deepseek_tokens": int(budget.get("deepseek_tokens") or 0),
                } if triggered else None,
                "source": "worker_state" if budget.get("updated_at") else "local_imports",
                "observed_at": float(budget.get("updated_at") or 0),
            },
            "retention": {
                "result_days": CLOUD_RESULT_RETENTION_DAYS,
                "state_days": CLOUD_STATE_RETENTION_DAYS,
                "ceiling_days": CLOUD_RETENTION_CEILING_DAYS,
            },
            "target": {"repository": target} if target else None,
            "binding_confirmed": bool(target),
            "verification": dict(profile.get("verification") or {}),
            "config_hash": str(profile.get("config_hash") or ""),
            "verified": verified,
            "ai_key_available": bool(self.credentials.has_deepseek_key()),
            "circuits": circuits,
            "last_cloud_run": {
                "code": str(last_state.get("code") or ""),
                "counts": dict(last_state.get("counts") or {}),
                "elapsed_seconds": float(last_state.get("elapsed_seconds") or 0),
                "observed_at": float(last_state.get("observed_at") or 0),
            },
            "runs": [self._project_run(run) for run in self.store.list_automation_runs(limit=20)],
            "imports": [self._project_import(item) for item in self.store.list_automation_imports(limit=50)],
            "observed_at": float(profile.get("observed_at") or 0),
            "expires_at": float(profile.get("expires_at") or 0),
            "stale": stale,
            "actions": self._actions(profile, state, verified, circuits),
        }

    @staticmethod
    def _actions(
        profile: dict[str, Any], state: str, verified: bool, circuits: list[dict[str, Any]],
    ) -> list[str]:
        configured = bool(profile.get("config_hash"))
        actions: list[str] = []
        if configured:
            actions += ["verify-cloud-credentials", "revoke-cloud-credentials", "update-account"]
        if state in {"ready", "running"}:
            actions += ["run-now", "disable-cloud"]
        elif verified and state in {"configuring", "verifying", "disabled", "degraded", "unknown"}:
            actions += ["enable-cloud"]
        resettable_circuit = any(
            str(item.get("circuit") or "") in {"authentication", "deepseek", "platform"}
            and str(item.get("state") or "closed") != "closed"
            for item in circuits
        )
        if state == "circuit_open" or resettable_circuit:
            actions.append("reset-circuit")
        if state == "cleanup_pending":
            actions += ["revoke-cloud-credentials", "erase-cloud-data"]
        return sorted(set(actions))

    def _event(self, entity_id: str, payload: dict[str, Any]) -> None:
        self.store.append_remote_event("automation", entity_id, payload)


__all__ = [
    "AutomationError", "AutomationService", "CLOUD_DAILY_WORKFLOW", "CLOUD_VERIFY_WORKFLOW",
    "CLOUD_SECRET_NAMES", "CLOUD_VARIABLE_NAMES", "CLOUD_PROTOCOL_VERSION",
    "CLOUD_DISCLOSURE_VERSION", "CLOUD_SCHEDULE", "CLOUD_CRON_EXPRESSIONS",
    "CLOUD_RESULT_RETENTION_DAYS", "CLOUD_RETENTION_CEILING_DAYS",
    "COURSE_BASELINE_MAX_LECTURES", "BASELINE_TOTAL_MAX_ENTRIES",
    "next_cloud_windows",
]
