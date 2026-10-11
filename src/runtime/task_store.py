"""Durable SQLite task state with short-lived Windows-safe connections."""

from __future__ import annotations

import json
import hashlib
import math
import sqlite3
import statistics
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ACTIVE_STATES = ("queued", "running", "pausing", "paused")
TERMINAL_STATES = ("completed", "failed", "canceled")
# CLIENT-STATE-R1（任务区死任务残余根修）：终态呈现保留窗（天）。completed/
# failed/canceled 且终态时刻早于该窗的行从 list_tasks 窗口剔除——只影响
# 呈现窗口，数据不删=持久化语义零变化；与隐私说明「任务状态记录最长保留
# 90 天」同口径。自动化缺省：默认生效、不设用户开关；需要全量历史的恢复/
# 证据类调用点（如启动核真、旧用户证据判定）显式传 terminal_retention_days=None。
TERMINAL_RETENTION_DAYS = 90.0
USER_PAUSE_INTENT_KEY = "user_pause_intent"
# AS10（第五十案）：busy 退避计数的 app_state 键前缀；常量住本层使
# mark_terminal 能在任务终态时统一清键（应用层仅 import，不重复定义）。
REMOTE_BUSY_STATE_KEY_PREFIX = "remote_busy_cycle."
# DEAD-TASK-PURGE（顽固残留任务根修）：两个用户可见的诚实死因闭集码。
# zombie_session_cleanup=A 启动僵尸清扫（上会话崩溃残留的在途行）；
# user_cleared_stuck=B 用户面「清除卡住的任务」。前端 TASK_FAILURE_GUIDANCE
# 与 http_api TASK_ERROR_CODES 同笔带键（等集钉 test_automation 严校）。
ZOMBIE_SESSION_CLEANUP_REASON = "zombie_session_cleanup"
USER_CLEARED_STUCK_REASON = "user_cleared_stuck"
# A 判定窗：上会话在途行距本次启动 >5 分钟无任何进展才算崩溃残留（快速
# 重启窗口内不动，交给既有 recover/paused 恢复扫描）。
ZOMBIE_STARTUP_STALE_SECONDS = 300.0
# B 判定窗：在途行 30 分钟无任何进展（updated_at）才算卡住（排除真活跃）。
STUCK_TASK_STALE_SECONDS = 1800.0
# P57（AUTOMATION-RUN-TOMBSTONE-1）：用户显式删除的自动材料运行记录以墓碑
# 防复活——对账链（reconcile / ghost 收敛）再观测到同一 run_key 时一律
# no-op。墓碑体量有界：只保留最近 N 条，最旧先淘汰；被淘汰的 key 重新可被
# 远端观测，属显式接受的有界折衷。
AUTOMATION_RUN_TOMBSTONE_LIMIT = 500

# --- Worker queue timing evidence (Stage 09-C) -------------------------------
# Queue samples reuse the existing estimate-history mechanism: one row per
# profile key in ``estimate_samples`` (unit_cost = queued seconds for one
# run), summarized by ``estimate_profiles`` median/MAD.  Age and count caps
# keep ancient platform behavior from dominating the published range.
QUEUE_SAMPLE_MAX_AGE_SECONDS = 30 * 24 * 3600.0
QUEUE_SAMPLE_MAX_SECONDS = 24 * 3600.0
QUEUE_WORKER_PROFILE = "queue:worker"
QUEUE_OBSERVATION_SCHEMA = "courselens.queue-observation.v1"
PREDICTION_OUTCOME_SCHEMA = "courselens.task-prediction-outcome.v1"
# Closed managed-Worker workflow → runner class map.  Every trusted workflow
# runs on the standard public runner pool; the class exists so the
# hierarchical profile can pool across workflows of the same runner class.
WORKFLOW_RUNNER_CLASSES = {
    "process.yml": "public-runner",
    "echo.yml": "public-runner",
    "cloud-verify.yml": "public-runner",
    "cloud-daily.yml": "public-runner",
}
# Conclusions that never contribute queue evidence: the run either never
# queued real work or its timing is not comparable to a normal wait.
_QUEUE_EXCLUDED_CONCLUSIONS = {
    "cancelled", "canceled", "skipped", "startup_failure", "stale",
}


def queue_profile_keys(workflow: str, *, pipeline: str = "") -> list[str]:
    """Hierarchical queue profile keys, most specific first.

    L1 exact workflow + runner class + pipeline fingerprint; L2 workflow +
    runner class; L3 Worker-wide history.  Unknown workflows still fall back
    to the Worker-wide key so history accumulates honestly.
    """
    runner_class = WORKFLOW_RUNNER_CLASSES.get(str(workflow or ""))
    keys: list[str] = []
    if runner_class:
        base = f"queue:{workflow}:{runner_class}"
        if pipeline:
            keys.append(f"{base}:{pipeline}")
        keys.append(base)
    keys.append(QUEUE_WORKER_PROFILE)
    return keys


def _queue_timestamp(value: object) -> float:
    """Parse one GitHub ISO-8601 instant into epoch seconds (0.0 = invalid)."""
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    epoch = moment.timestamp()
    return epoch if math.isfinite(epoch) and epoch > 0 else 0.0


def trusted_queue_observation(
    run: dict[str, Any], *, workflow: str, pipeline: str, expected_head: str,
) -> dict[str, Any] | None:
    """Filter one sanitized workflow-run view into a trusted queue sample.

    A sample is trusted only when the run is completed with a comparable
    conclusion, its head matches the pinned trusted Worker commit, both
    timestamps are finite and ordered, and the queue wait is inside the
    sane bound.  Anything ambiguous returns ``None`` — never a guess.
    """
    try:
        run_id = int(run.get("id") or 0)
    except (TypeError, ValueError):
        return None
    if run_id <= 0:
        return None
    if str(run.get("status") or "") != "completed":
        return None
    conclusion = str(run.get("conclusion") or "")
    if not conclusion or conclusion.lower() in _QUEUE_EXCLUDED_CONCLUSIONS:
        return None
    expected = str(expected_head or "").strip().lower()
    actual = str(run.get("head_sha") or "").strip().lower()
    if len(expected) != 40 or actual != expected:
        return None
    created = _queue_timestamp(run.get("created_at"))
    started = _queue_timestamp(run.get("run_started_at"))
    if created <= 0 or started <= 0 or started < created:
        return None
    queued_seconds = started - created
    if queued_seconds > QUEUE_SAMPLE_MAX_SECONDS:
        return None
    return {
        "schema": QUEUE_OBSERVATION_SCHEMA,
        "run_id": run_id,
        "workflow": str(workflow),
        "queued_seconds": queued_seconds,
        "profile_keys": queue_profile_keys(workflow, pipeline=str(pipeline or "")),
    }


REMOTE_RUN_RECOVERABLE_STATES = (
    "created", "queued", "awaiting_payload", "running", "canceling",
    "artifact_ready", "downloading_result",
)
REMOTE_RUN_STARTUP_RECOVERY_STATES = (
    "queued", "awaiting_payload", "running", "canceling", "downloading_result",
)
REMOTE_RUN_LIFECYCLE_STATES = REMOTE_RUN_RECOVERABLE_STATES + (
    "rerun_preparing", "rerun_payload_ready", "rerun_armed", "rerun_running",
)
# connection 面（src/remote/connection.py）现行使用名——别名仍在服务，不可删。
ACTIVE_REMOTE_STATES = REMOTE_RUN_RECOVERABLE_STATES
_TERMINAL_FAILURE_CONCLUSIONS = (
    "failure", "timed_out", "action_required", "stale", "startup_failure", "skipped",
)


class TaskStore:
    def __init__(self, path: str | Path, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = bool(read_only)
        if self.read_only and not self.path.is_file():
            raise FileNotFoundError("read-only task store is unavailable")
        if self.read_only and any(
            Path(str(self.path) + suffix).exists() for suffix in ("-wal", "-shm")
        ):
            # A live or uncheckpointed WAL cannot be inspected with a durable,
            # non-mutating snapshot.  Refuse the preflight rather than let
            # SQLite recover, delete, or ignore sidecar state.
            raise RuntimeError("read-only task store has active SQLite sidecars")
        if not self.read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # 夜10-C：可写连接按线程池化（thread-local），_connect 以深度计数+
        # SAVEPOINT 保持与一次性连接逐位等价的嵌套事务语义。
        # 默认关闭（一次一连），由 enable_connection_pool() 在应用启动期启用：
        # 测试/工具依赖「零显式关闭+临时目录生命周期」契约，池化句柄会锁文件。
        self._local = threading.local()
        self._pool_enabled = False
        # 夜10-C：池化连接注册表（check_same_thread=False + RLock 串行化，
        # 使放弃线程的连接可被 close() 跨线程关停，杜绝 Windows 句柄锁）。
        self._pooled_registry: "dict[int, sqlite3.Connection]" = {}
        self._pooled_registry_lock = threading.Lock()
        # 夜12-SOAK3：服务端每连接一线程（ThreadingHTTPServer），SSE 自截止
        # 重连与媒体 range 请求持续供给短命线程；线程退出后 thread-local 随
        # GC 消失，但注册表仍持有该连接 —— sqlite 句柄/WAL 映射/页缓存将
        # 滞留到进程退出（长窗实测 PM +19MB/5min、句柄 +40/min）。池深超
        # 阈值时按最小间隔节流清扫，只逐出已死线程的条目。
        self._pool_sweep_threshold = 32
        self._pool_sweep_min_interval = 30.0
        self._pool_last_sweep = 0.0
        if self.read_only:
            with self._connect() as db:
                required = {"tasks", "remote_runs", "remote_run_attempts", "remote_token_leases"}
                present = {str(row[0]) for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                if not required.issubset(present):
                    raise RuntimeError("read-only task store schema is unavailable")
            return
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY, kind TEXT NOT NULL, course_id TEXT NOT NULL,
                    sub_id TEXT NOT NULL, config_key TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
                    resume_requested INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT NOT NULL DEFAULT '{}', progress_json TEXT NOT NULL DEFAULT '{}',
                    estimate_json TEXT NOT NULL DEFAULT '{}', checkpoint_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '', sequence INTEGER NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    started_at REAL, finished_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_lane ON tasks(kind,state,sequence);
                CREATE INDEX IF NOT EXISTS idx_tasks_lecture ON tasks(kind,sub_id,config_key,state);
                -- WP-3（N9-H #4 销项）：任务抽屉/恢复扫描走 ORDER BY sequence LIMIT ?，
                -- 无此索引时全表 SCAN+TEMP B-TREE（30k 行实测 3.3ms→索引序直读）。
                CREATE INDEX IF NOT EXISTS idx_tasks_sequence ON tasks(sequence);
                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feature_flags (
                    name TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS estimate_samples (
                    sample_id INTEGER PRIMARY KEY AUTOINCREMENT, profile_key TEXT NOT NULL,
                    unit_cost REAL NOT NULL, units REAL NOT NULL, elapsed_seconds REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_estimate_profile
                    ON estimate_samples(profile_key,sample_id DESC);
                CREATE TABLE IF NOT EXISTS estimate_profiles (
                    profile_key TEXT PRIMARY KEY, sample_count INTEGER NOT NULL,
                    median_unit_cost REAL NOT NULL, mad_unit_cost REAL NOT NULL,
                    device_fingerprint_json TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS remote_runs (
                    task_id TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    workflow TEXT NOT NULL,
                    run_id INTEGER,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    issue_number INTEGER,
                    artifact_id INTEGER,
                    remote_state TEXT NOT NULL DEFAULT 'created',
                    input_hash TEXT NOT NULL DEFAULT '',
                    pipeline_version TEXT NOT NULL DEFAULT '',
                    checkpoint_json TEXT NOT NULL DEFAULT '{}',
                    dispatched_at REAL,
                    started_at REAL,
                    finished_at REAL,
                    imported_at REAL,
                    updated_at REAL NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS task_metadata_v3 (
                    task_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL DEFAULT '',
                    dedupe_key TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'queued',
                    privacy_state TEXT NOT NULL DEFAULT 'sealed',
                    requested_outputs_json TEXT NOT NULL DEFAULT '[]',
                    cost_json TEXT NOT NULL DEFAULT '{}', estimate_json TEXT NOT NULL DEFAULT '{}',
                    resources_json TEXT NOT NULL DEFAULT '{}', input_hash TEXT NOT NULL DEFAULT '',
                    result_notices_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_task_metadata_dedupe ON task_metadata_v3(dedupe_key);
                CREATE INDEX IF NOT EXISTS idx_remote_runs_run_id
                    ON remote_runs(repository,run_id);
                CREATE INDEX IF NOT EXISTS idx_remote_runs_state
                    ON remote_runs(remote_state,updated_at);
                -- WP-3（N9-H #4 销项）：对账/导入面 list_remote_runs 走
                -- ORDER BY updated_at DESC LIMIT ?，无此索引时全表 SCAN
                -- （30k 行实测 16.6ms→反向索引序直读）。
                CREATE INDEX IF NOT EXISTS idx_remote_runs_updated
                    ON remote_runs(updated_at);
                CREATE TABLE IF NOT EXISTS remote_observations (
                    component TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    source TEXT NOT NULL,
                    code TEXT NOT NULL DEFAULT '',
                    actions_json TEXT NOT NULL DEFAULT '[]',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    observed_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS remote_run_attempts (
                    task_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    repository TEXT NOT NULL DEFAULT '',
                    workflow TEXT NOT NULL DEFAULT '',
                    run_id INTEGER,
                    github_status TEXT NOT NULL DEFAULT '',
                    conclusion TEXT NOT NULL DEFAULT '',
                    worker_status TEXT NOT NULL DEFAULT '',
                    worker_stage TEXT NOT NULL DEFAULT '',
                    completed INTEGER,
                    total INTEGER,
                    last_control_sequence INTEGER NOT NULL DEFAULT 0,
                    last_heartbeat_at REAL,
                    artifact_id INTEGER,
                    import_state TEXT NOT NULL DEFAULT '',
                    cleanup_state TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    observed_at REAL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(task_id, attempt)
                );
                CREATE INDEX IF NOT EXISTS idx_remote_attempts_run
                    ON remote_run_attempts(repository,run_id,attempt);
                CREATE INDEX IF NOT EXISTS idx_remote_attempts_active
                    ON remote_run_attempts(github_status,updated_at);
                CREATE TABLE IF NOT EXISTS remote_token_leases (
                    task_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    expires_at REAL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS remote_supervisor_leases (
                    task_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS remote_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    entity_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_remote_events_topic
                    ON remote_events(topic,sequence);
                CREATE TABLE IF NOT EXISTS remote_operations (
                    operation_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    target_id TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_profiles (
                    profile_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL DEFAULT 'local',
                    state TEXT NOT NULL DEFAULT 'disabled',
                    schedule_time TEXT NOT NULL DEFAULT '07:30',
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                    budget_json TEXT NOT NULL DEFAULT '{}',
                    config_hash TEXT NOT NULL DEFAULT '',
                    verified_config_hash TEXT NOT NULL DEFAULT '',
                    verification_json TEXT NOT NULL DEFAULT '{}',
                    local_schedule_was_enabled INTEGER NOT NULL DEFAULT 0,
                    protocol TEXT NOT NULL DEFAULT '',
                    account_id TEXT NOT NULL DEFAULT '',
                    binding_json TEXT NOT NULL DEFAULT '{}',
                    observed_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_course_rules (
                    profile_id TEXT NOT NULL,
                    course_id TEXT NOT NULL,
                    rule_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(profile_id, course_id)
                );
                CREATE TABLE IF NOT EXISTS automation_runs (
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
                    started_at REAL NOT NULL DEFAULT 0,
                    concluded_at REAL NOT NULL DEFAULT 0,
                    observed_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_automation_runs_updated
                    ON automation_runs(updated_at DESC);
                CREATE TABLE IF NOT EXISTS automation_run_tombstones (
                    run_key TEXT PRIMARY KEY,
                    deleted_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_imports (
                    artifact_id INTEGER PRIMARY KEY,
                    artifact_name TEXT NOT NULL DEFAULT '',
                    run_id INTEGER,
                    state TEXT NOT NULL,
                    result_hash TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    expires_at REAL,
                    observed_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_automation_imports_state
                    ON automation_imports(state,updated_at DESC);
                CREATE TABLE IF NOT EXISTS automation_budget_ledger (
                    budget_date TEXT PRIMARY KEY,
                    lectures INTEGER NOT NULL DEFAULT 0,
                    runner_minutes REAL NOT NULL DEFAULT 0,
                    deepseek_tokens INTEGER NOT NULL DEFAULT 0,
                    reserved_runner_minutes REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_circuit_state (
                    circuit TEXT PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT 'closed',
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    opened_at REAL,
                    retry_after REAL,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_budget_reservations (
                    task_id TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    predicted_minutes REAL NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_budget_settlements (
                    task_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    day TEXT NOT NULL,
                    actual_minutes REAL NOT NULL,
                    settled_at REAL NOT NULL,
                    PRIMARY KEY(task_id, attempt)
                );
                """
            )
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(tasks)").fetchall()}
            if "resume_requested" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN resume_requested INTEGER NOT NULL DEFAULT 0")
            # AS6（第四十七案）任务消耗透镜：两列仅追加、幂等；NULL=没有该
            # 字段（历史任务不显示、不伪造），绝不拿 0 冒充已记录。
            if "deepseek_tokens" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN deepseek_tokens INTEGER")
            if "runner_seconds" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN runner_seconds REAL")
            reservation_columns = {
                str(row[1]) for row in db.execute("PRAGMA table_info(task_budget_reservations)").fetchall()
            }
            if reservation_columns and "attempt" not in reservation_columns:
                db.execute(
                    "ALTER TABLE task_budget_reservations ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1"
                )
            settlement_columns = {
                str(row[1]) for row in db.execute("PRAGMA table_info(task_budget_settlements)").fetchall()
            }
            if settlement_columns and "attempt" not in settlement_columns:
                # Pre-attempt intermediate builds keyed settlements on task_id
                # alone; rebuild so each task attempt can settle once.
                db.executescript(
                    """
                    CREATE TABLE task_budget_settlements_with_attempts (
                        task_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL DEFAULT 1,
                        day TEXT NOT NULL,
                        actual_minutes REAL NOT NULL,
                        settled_at REAL NOT NULL,
                        PRIMARY KEY(task_id, attempt)
                    );
                    INSERT INTO task_budget_settlements_with_attempts(
                        task_id,attempt,day,actual_minutes,settled_at)
                    SELECT task_id,1,day,actual_minutes,settled_at FROM task_budget_settlements;
                    DROP TABLE task_budget_settlements;
                    ALTER TABLE task_budget_settlements_with_attempts RENAME TO task_budget_settlements;
                    """
                )
            metadata_columns = {
                str(row[1]) for row in db.execute("PRAGMA table_info(task_metadata_v3)").fetchall()
            }
            if "result_notices_json" not in metadata_columns:
                db.execute(
                    "ALTER TABLE task_metadata_v3 ADD COLUMN result_notices_json TEXT NOT NULL DEFAULT '{}'"
                )
            automation_columns = {
                str(row[1]) for row in db.execute("PRAGMA table_info(automation_profiles)").fetchall()
            }
            if "notification_json" in automation_columns:
                db.executescript(
                    """
                    CREATE TABLE automation_profiles_without_notifications (
                        profile_id TEXT PRIMARY KEY,
                        mode TEXT NOT NULL DEFAULT 'local',
                        state TEXT NOT NULL DEFAULT 'disabled',
                        schedule_time TEXT NOT NULL DEFAULT '07:30',
                        timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                        budget_json TEXT NOT NULL DEFAULT '{}',
                        config_hash TEXT NOT NULL DEFAULT '',
                        verified_config_hash TEXT NOT NULL DEFAULT '',
                        verification_json TEXT NOT NULL DEFAULT '{}',
                        local_schedule_was_enabled INTEGER NOT NULL DEFAULT 0,
                        observed_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    INSERT INTO automation_profiles_without_notifications(
                        profile_id,mode,state,schedule_time,timezone,budget_json,
                        config_hash,verified_config_hash,verification_json,
                        local_schedule_was_enabled,observed_at,expires_at,updated_at
                    )
                    SELECT profile_id,mode,state,schedule_time,timezone,budget_json,
                           config_hash,verified_config_hash,verification_json,
                           local_schedule_was_enabled,observed_at,expires_at,updated_at
                    FROM automation_profiles;
                    DROP TABLE automation_profiles;
                    ALTER TABLE automation_profiles_without_notifications RENAME TO automation_profiles;
                    """
                )
            db.execute("DELETE FROM automation_circuit_state WHERE circuit='smtp'")
            # Stage 09 cloud-automation v2: profile rows gain protocol/account
            # binding columns; legacy rows keep protocol='' so the service can
            # migrate them fail closed on first read.
            for column in ("protocol", "account_id"):
                if column not in automation_columns:
                    db.execute(
                        f"ALTER TABLE automation_profiles ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            if "binding_json" not in automation_columns:
                db.execute(
                    "ALTER TABLE automation_profiles ADD COLUMN binding_json TEXT NOT NULL DEFAULT '{}'"
                )
            # 第卅六案①（CLOUD-CONSENT-AUTO-1 U3）：运行记录必须带 run 自身的
            # 时刻，卡片才不必拿刷新时刻（observed_at）冒充运行时间。两列
            # 仅追加、幂等；历史行为 0=没有该时刻，前端不发明时间。
            run_columns = {
                str(row[1]) for row in db.execute("PRAGMA table_info(automation_runs)").fetchall()
            }
            for column in ("started_at", "concluded_at"):
                if column not in run_columns:
                    db.execute(
                        f"ALTER TABLE automation_runs ADD COLUMN {column} REAL NOT NULL DEFAULT 0"
                    )
            db.execute(
                """INSERT INTO schema_meta(key,value,updated_at)
                   VALUES('automation_schema_version','3',?)
                   ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value,updated_at=excluded.updated_at""",
                (time.time(),),
            )

    def readonly_snapshot(self) -> tuple[tuple[str, bool, int, int, str], ...]:
        """Fingerprint the main database and both sidecars without opening SQLite."""
        if not self.read_only:
            raise RuntimeError("read-only snapshot requires a read-only task store")
        values = []
        for candidate in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            if not candidate.exists():
                values.append((str(candidate), False, 0, 0, ""))
                continue
            stat = candidate.stat()
            values.append((str(candidate), True, int(stat.st_size), int(stat.st_mtime_ns), hashlib.sha256(candidate.read_bytes()).hexdigest()))
        return tuple(values)

    def _pooled_connection(self) -> sqlite3.Connection:
        """夜10-C：当前线程的可写连接复用（创建一次，随线程存活）。

        连接创建开销实测 1.30ms/次 vs 复用 0.005ms；WAL+synchronous=NORMAL
        语义不变。跨线程不可见（thread-local）；close() 只关本线程连接，
        其余见 _sweep_pooled_registry（夜12-SOAK3 起死线程条目按节流逐出，
        不再「随进程生命周期同界」）。
        """
        if len(self._pooled_registry) >= self._pool_sweep_threshold:
            self._sweep_pooled_registry()
        db = getattr(self._local, "db", None)
        if db is not None:
            # close() 可能已跨线程关停本连接（注册表被清）→ 弃用陈旧引用重建
            with self._pooled_registry_lock:
                if self._pooled_registry.get(threading.get_ident()) is db:
                    return db
            self._local.db = None
            self._local.depth = 0
        db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        self._local.db = db
        self._local.depth = 0
        with self._pooled_registry_lock:
            self._pooled_registry[threading.get_ident()] = db
        return db

    def _sweep_pooled_registry(self) -> None:
        """夜12-SOAK3：逐出已死线程的池化连接（节流）。

        ThreadingHTTPServer 每连接一线程：SSE 45s 自截止重连与媒体 range
        请求持续供给短命线程，线程退出后 thread-local 随 GC 消失，但
        _pooled_registry 仍以 ident 持有该连接 —— sqlite 句柄（db+wal+shm）、
        WAL/SHM 映射与页缓存全部滞留到进程退出。此处只逐出「当前不存在
        同名存活线程」的条目，活线程连接零触碰。ident 复用属自愈孤儿：
        新线程 _pooled_connection 会覆盖同键旧条目，旧连接失去引用后由
        GC 关停，不随清扫误伤。
        """
        now = time.monotonic()
        if now - self._pool_last_sweep < self._pool_sweep_min_interval:
            return
        self._pool_last_sweep = now
        alive = {t.ident for t in threading.enumerate()}
        with self._pooled_registry_lock:
            evicted = [
                self._pooled_registry.pop(tid)
                for tid in list(self._pooled_registry)
                if tid not in alive
            ]
        for db in evicted:
            try:
                db.close()
            except sqlite3.Error:
                pass

    def enable_connection_pool(self) -> None:
        """夜10-C：启用可写连接按线程池化（应用启动期一次性调用）。

        默认关闭（一次一连）：测试/工具依赖「store 存活期 ≤ 临时目录生命
        周期且零显式关闭」的既有契约（Windows 下打开的连接句柄会锁住
        state.db，池化后临时目录清理将撞 WinError 32）。生产应用在启动期
        显式启用，读路径热面收益 1.30ms→0.005ms/次。
        """
        self._pool_enabled = True

    @contextmanager
    def _connect(self):
        if self.read_only:
            # __init__ has already rejected WAL/SHM sidecars, so immutable is
            # safe here and prevents SQLite from creating fresh sidecars while
            # an audit only reads the durable main database.
            db = sqlite3.connect(f"file:{self.path.resolve().as_posix()}?mode=ro&immutable=1", uri=True, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            try:
                yield db
            finally:
                db.close()
            return
        if not self._pool_enabled:
            # 夜10-C：默认一次一连（测试/工具兼容临时目录生命周期契约）。
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
            return
        db = self._pooled_connection()
        depth = self._local.depth = getattr(self._local, "depth", 0) + 1
        savepoint = None
        if depth > 1:
            # 嵌套调用：SAVEPOINT 精确保留「内层失败只回滚内层写入」的
            # 一次性连接语义；最外层仍以 commit/rollback 收口整事务。
            savepoint = f"courselens_sp_{depth}"
            db.execute(f"SAVEPOINT {savepoint}")
        try:
            yield db
            if depth > 1:
                db.execute(f"RELEASE {savepoint}")
            else:
                db.commit()
        except BaseException:
            if depth > 1:
                db.execute(f"ROLLBACK TO {savepoint}")
                db.execute(f"RELEASE {savepoint}")
            else:
                db.rollback()
            raise
        finally:
            self._local.depth -= 1

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load(value: str | None) -> Any:
        try:
            return json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}

    def close(self) -> None:
        """夜10-C：关停池化可写连接（本线程+全部注册连接）。

        check_same_thread=False + 所有方法持 self._lock 串行化 → 放弃线程
        的连接可安全跨线程关停；正在事务中的连接（锁未得）跳过，随进程
        回收。重复调用安全。
        """
        self._close_current_thread_connection()
        if not self._pooled_registry:
            return
        # 有界等锁：拿不到=有线程正持连接执行中，跳过其余连接（放弃路径
        # 本就允许超时放弃）。
        acquired = self._lock.acquire(timeout=2.0)
        try:
            with self._pooled_registry_lock:
                items = list(self._pooled_registry.items())
                self._pooled_registry.clear()
            for _ident, conn in items:
                try:
                    conn.commit()
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
        finally:
            if acquired:
                self._lock.release()

    def _close_current_thread_connection(self) -> None:
        db = getattr(self._local, "db", None)
        if db is None:
            return
        self._local.db = None
        self._local.depth = 0
        with self._pooled_registry_lock:
            self._pooled_registry.pop(threading.get_ident(), None)
        try:
            db.commit()
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        try:
            db.close()
        except sqlite3.Error:
            pass

    def set_feature_flag(self, name: str, enabled: bool) -> bool:
        name = str(name or "").strip()
        if not name or len(name) > 80 or not all(ch.isalnum() or ch in "._-" for ch in name):
            raise ValueError("feature flag name is invalid")
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO feature_flags(name,enabled,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at",
                (name, int(bool(enabled)), time.time()),
            )
        return bool(enabled)

    def get_feature_flags(self) -> dict[str, bool]:
        with self._connect() as db:
            return {str(row[0]): bool(row[1]) for row in db.execute("SELECT name,enabled FROM feature_flags")}

    def upsert_v3_metadata(self, task_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {"parent_id", "dedupe_key", "stage", "privacy_state", "requested_outputs",
                   "cost", "estimate", "resources", "input_hash"}
        current = self.get_v3_metadata(task_id) or {}
        current.update({key: value for key, value in fields.items() if key in allowed})
        dedupe = str(current.get("dedupe_key") or "").strip()
        if not dedupe:
            raise ValueError("dedupe_key is required")
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO task_metadata_v3(
                task_id,parent_id,dedupe_key,stage,privacy_state,requested_outputs_json,
                cost_json,estimate_json,resources_json,input_hash,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET parent_id=excluded.parent_id,
                dedupe_key=excluded.dedupe_key,stage=excluded.stage,privacy_state=excluded.privacy_state,
                requested_outputs_json=excluded.requested_outputs_json,cost_json=excluded.cost_json,
                estimate_json=excluded.estimate_json,resources_json=excluded.resources_json,
                input_hash=excluded.input_hash,updated_at=excluded.updated_at""",
                (str(task_id), str(current.get("parent_id") or ""), dedupe,
                 str(current.get("stage") or "queued"), str(current.get("privacy_state") or "sealed"),
                 self._dump(current.get("requested_outputs") or []), self._dump(current.get("cost") or {}),
                 self._dump(current.get("estimate") or {}), self._dump(current.get("resources") or {}),
                 str(current.get("input_hash") or ""), float(current.get("created_at") or now), now),
            )
        return self.get_v3_metadata(task_id) or {}

    def get_v3_metadata(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM task_metadata_v3 WHERE task_id=?", (str(task_id),)).fetchone()
        if row is None:
            return None
        value = dict(row)
        for key in ("requested_outputs", "cost", "estimate", "resources"):
            value[key] = self._load(value.pop(f"{key}_json", "{}" if key != "requested_outputs" else "[]"))
        value["result_notices"] = self._load(value.pop("result_notices_json", "{}"))
        return value

    def record_prediction_outcome(
        self, task_id: str, *, initial_minutes: float, actual_minutes: float, attempt: int = 1,
    ) -> dict[str, Any] | None:
        """Record actual-vs-initial prediction on one terminal task attempt.

        The outcome lands in the task's v3 metadata ``estimate`` slot under a
        closed schema.  Non-finite or negative inputs are rejected rather
        than clamped into a misleading calibration record; ``delta_minutes``
        is positive when the task finished earlier than predicted.
        """
        try:
            initial = float(initial_minutes)
            actual = float(actual_minutes)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(initial) or initial <= 0 or not math.isfinite(actual) or actual < 0:
            return None
        # Calibration display is best-effort: tasks without v3 metadata
        # (ungated or pre-upgrade rows) have no metadata slot to annotate.
        current = self.get_v3_metadata(str(task_id))
        if not current or not str(current.get("dedupe_key") or ""):
            return None
        return self.upsert_v3_metadata(
            str(task_id),
            estimate={
                "schema": PREDICTION_OUTCOME_SCHEMA,
                "initial_minutes": round(initial, 2),
                "actual_minutes": round(actual, 2),
                "delta_minutes": round(initial - actual, 2),
                "attempt": max(1, int(attempt)),
                "recorded_at": time.time(),
            },
        )

    def prediction_outcome(self, task_id: str) -> dict[str, Any] | None:
        """Validated actual-vs-initial record for one task (None when absent)."""
        metadata = self.get_v3_metadata(str(task_id))
        value = dict((metadata or {}).get("estimate") or {})
        if value.get("schema") != PREDICTION_OUTCOME_SCHEMA:
            return None
        try:
            initial = float(value["initial_minutes"])
            actual = float(value["actual_minutes"])
            delta = float(value["delta_minutes"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (math.isfinite(initial) and math.isfinite(actual) and math.isfinite(delta)):
            return None
        return {
            "initial_minutes": initial,
            "actual_minutes": actual,
            "delta_minutes": delta,
            "attempt": max(1, int(value.get("attempt") or 1)),
            "recorded_at": float(value.get("recorded_at") or 0.0),
        }


    def set_result_notices(self, task_id: str, notices: dict[str, Any]) -> None:
        """Attach worker result warnings (for example slides_skipped) to the task card.

        A conditional update on purpose: tasks without v3 metadata (canary/echo)
        have no task-center card to annotate, so there is nothing to do.
        """
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE task_metadata_v3 SET result_notices_json=?, updated_at=? WHERE task_id=?",
                (self._dump(dict(notices or {})), time.time(), str(task_id)),
            )

    def upsert_remote_run(self, task_id: str, **fields: Any) -> dict[str, Any]:
        """Create or update GitHub run metadata without exposing job content."""
        allowed = {
            "repository", "workflow", "run_id", "attempt", "issue_number",
            "artifact_id", "remote_state", "input_hash", "pipeline_version",
            "checkpoint", "dispatched_at", "started_at", "finished_at",
            "imported_at", "last_error",
        }
        current = self.get_remote_run(task_id)
        values = dict(current or {})
        values.update({key: value for key, value in fields.items() if key in allowed})
        repository = str(values.get("repository") or "")
        workflow = str(values.get("workflow") or "")
        if not repository or not workflow:
            raise ValueError("repository and workflow are required for a remote run")
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO remote_runs(
                       task_id,repository,workflow,run_id,attempt,issue_number,
                       artifact_id,remote_state,input_hash,pipeline_version,
                       checkpoint_json,dispatched_at,started_at,finished_at,
                       imported_at,updated_at,last_error
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                       repository=excluded.repository,workflow=excluded.workflow,
                       run_id=excluded.run_id,attempt=excluded.attempt,
                       issue_number=excluded.issue_number,artifact_id=excluded.artifact_id,
                       remote_state=excluded.remote_state,input_hash=excluded.input_hash,
                       pipeline_version=excluded.pipeline_version,
                       checkpoint_json=excluded.checkpoint_json,
                       dispatched_at=excluded.dispatched_at,started_at=excluded.started_at,
                       finished_at=excluded.finished_at,imported_at=excluded.imported_at,
                       updated_at=excluded.updated_at,last_error=excluded.last_error""",
                (
                    str(task_id), repository, workflow, values.get("run_id"),
                    max(1, int(values.get("attempt") or 1)), values.get("issue_number"),
                    values.get("artifact_id"), str(values.get("remote_state") or "created"),
                    str(values.get("input_hash") or ""), str(values.get("pipeline_version") or ""),
                    self._dump(values.get("checkpoint") or {}), values.get("dispatched_at"),
                    values.get("started_at"), values.get("finished_at"),
                    values.get("imported_at"), now, str(values.get("last_error") or ""),
                ),
            )
        result = self.get_remote_run(task_id)
        assert result is not None
        return result

    def get_remote_run(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM remote_runs WHERE task_id=?", (str(task_id),)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["checkpoint"] = self._load(value.pop("checkpoint_json", "{}"))
        return value

    def list_remote_runs(self, *, limit: int = 10, workflow: str | None = None) -> list[dict[str, Any]]:
        """Return recent sanitized remote-run metadata for the local UI."""
        bounded = max(1, min(50, int(limit)))
        query = "SELECT * FROM remote_runs"
        parameters: list[Any] = []
        if workflow:
            query += " WHERE workflow=?"
            parameters.append(str(workflow))
        query += " ORDER BY updated_at DESC LIMIT ?"
        parameters.append(bounded)
        with self._lock, self._connect() as db:
            rows = db.execute(query, parameters).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["checkpoint"] = self._load(value.pop("checkpoint_json", "{}"))
            values.append(value)
        return values

    def active_remote_run_count(self) -> int:
        """Return an unbounded fail-closed count of nonterminal remote runs."""
        with self._lock, self._connect() as db:
            row = db.execute(
                """SELECT COUNT(*) AS count
                     FROM remote_runs
                    WHERE remote_state IS NULL
                       OR remote_state NOT IN ('completed','imported','failed','canceled')"""
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def active_task_count(self) -> int:
        """Return an unbounded fail-closed count of nonterminal local tasks."""
        with self._lock, self._connect() as db:
            row = db.execute(
                """SELECT COUNT(*) AS count
                     FROM tasks
                    WHERE state IS NULL OR state NOT IN ('completed','failed','canceled')"""
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def active_remote_attempt_count(self) -> int:
        """Return an unbounded fail-closed count of attempts without terminal parents.

        The parent remote-run lifecycle is authoritative: a terminal parent
        makes all of its exact task/run attempts historical.  Global artifact,
        Mailbox, cleanup, key, and marker gates independently catch residue.
        """
        with self._lock, self._connect() as db:
            row = db.execute(
                """SELECT COUNT(*) AS count FROM remote_run_attempts
                   WHERE NOT EXISTS (
                       SELECT 1 FROM remote_runs AS run
                        WHERE run.task_id=remote_run_attempts.task_id
                          AND run.run_id=remote_run_attempts.run_id
                          AND run.remote_state IN ('completed','imported','failed','canceled')
                   )"""
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def active_automation_import_count(self) -> int:
        """Return an unbounded count of auto-import records still in flight."""
        with self._lock, self._connect() as db:
            row = db.execute(
                """SELECT COUNT(*) AS count FROM automation_imports
                    WHERE state IS NULL OR state NOT IN ('imported','failed','unrecoverable')"""
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def has_active_remote_run(self, states: Iterable[str] = REMOTE_RUN_LIFECYCLE_STATES) -> bool:
        """Return whether any durable remote run is in a caller-approved active state."""
        values = tuple(str(state) for state in states)
        if not values:
            return False
        placeholders = ",".join("?" for _ in values)
        with self._lock, self._connect() as db:
            row = db.execute(
                f"SELECT EXISTS(SELECT 1 FROM remote_runs WHERE remote_state IN ({placeholders}))",
                values,
            ).fetchone()
        return bool(row[0] if row is not None else False)

    def cancel_unrecoverable_remote_recovery(self, task_id: str) -> dict[str, Any] | None:
        """Atomically abandon only a paused task stopped for missing recovery material."""
        now = time.time()
        with self._lock, self._connect() as db:
            task = db.execute(
                "SELECT state,error,payload_json FROM tasks WHERE task_id=?", (str(task_id),)
            ).fetchone()
            remote = db.execute(
                "SELECT remote_state FROM remote_runs WHERE task_id=?", (str(task_id),)
            ).fetchone()
            if (
                task is None
                or task["state"] != "paused"
                or task["error"] != "remote_recovery_material_unavailable"
                or remote is None
                or str(remote["remote_state"] or "") not in REMOTE_RUN_STARTUP_RECOVERY_STATES
            ):
                return None
            payload = dict(self._load(task["payload_json"]))
            payload["cancel_requested"] = True
            remote_updated = db.execute(
                """UPDATE remote_runs
                      SET remote_state='failed', last_error='remote_recovery_material_unavailable', updated_at=?
                    WHERE task_id=? AND remote_state=?""",
                (now, str(task_id), str(remote["remote_state"])),
            )
            task_updated = db.execute(
                """UPDATE tasks
                      SET state='canceled', resume_requested=0, payload_json=?, error='', finished_at=?, updated_at=?
                    WHERE task_id=? AND state='paused' AND error='remote_recovery_material_unavailable'""",
                (self._dump(payload), now, now, str(task_id)),
            )
            if remote_updated.rowcount != 1 or task_updated.rowcount != 1:
                raise RuntimeError("remote_recovery_cancel_not_safe")
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (str(task_id),)).fetchone()
        return self._row(row) if row is not None else None

    def arm_process_canary_rerun(self, task_id: str, *, run_id: int) -> bool:
        """Atomically and permanently consume one prepared canary rerun budget.

        GitHub's rerun endpoint has no idempotency key.  The durable state is
        therefore advanced *before* the external POST.  A caller that loses
        the POST response must only observe the same run for attempt 2; it may
        never call this transition or the endpoint again.
        """
        now = time.time()
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """UPDATE remote_runs
                      SET remote_state='rerun_armed', updated_at=?, last_error=''
                    WHERE task_id=? AND run_id=? AND attempt=1
                      AND workflow='process.yml'
                      AND remote_state='rerun_payload_ready'""",
                (now, str(task_id), int(run_id)),
            )
        return int(cursor.rowcount or 0) == 1

    def upsert_remote_observation(
        self,
        component: str,
        *,
        state: str,
        source: str,
        code: str = "",
        actions: list[str] | None = None,
        evidence: dict[str, Any] | None = None,
        observed_at: float | None = None,
        expires_at: float | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        observed = float(observed_at or now)
        expires = float(expires_at or observed)
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO remote_observations(
                       component,state,source,code,actions_json,evidence_json,
                       observed_at,expires_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(component) DO UPDATE SET
                       state=excluded.state,source=excluded.source,code=excluded.code,
                       actions_json=excluded.actions_json,evidence_json=excluded.evidence_json,
                       observed_at=excluded.observed_at,expires_at=excluded.expires_at,
                       updated_at=excluded.updated_at""",
                (
                    str(component), str(state), str(source), str(code),
                    self._dump(list(actions or [])), self._dump(dict(evidence or {})),
                    observed, expires, now,
                ),
            )
        return self.get_remote_observation(component) or {}

    def get_remote_observation(self, component: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM remote_observations WHERE component=?", (str(component),)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["actions"] = self._load(value.pop("actions_json", "[]"))
        value["evidence"] = self._load(value.pop("evidence_json", "{}"))
        return value

    def list_remote_observations(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM remote_observations ORDER BY component").fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["actions"] = self._load(value.pop("actions_json", "[]"))
            value["evidence"] = self._load(value.pop("evidence_json", "{}"))
            values.append(value)
        return values

    def upsert_remote_attempt(self, task_id: str, attempt: int, **fields: Any) -> dict[str, Any]:
        allowed = {
            "repository", "workflow", "run_id", "github_status", "conclusion",
            "worker_status", "worker_stage", "completed", "total",
            "last_control_sequence", "last_heartbeat_at", "artifact_id",
            "import_state", "cleanup_state", "error_code", "observed_at",
        }
        current = self.get_remote_attempt(task_id, attempt) or {}
        current.update({key: value for key, value in fields.items() if key in allowed})
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO remote_run_attempts(
                       task_id,attempt,repository,workflow,run_id,github_status,
                       conclusion,worker_status,worker_stage,completed,total,
                       last_control_sequence,last_heartbeat_at,artifact_id,
                       import_state,cleanup_state,error_code,observed_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(task_id,attempt) DO UPDATE SET
                       repository=excluded.repository,workflow=excluded.workflow,
                       run_id=excluded.run_id,github_status=excluded.github_status,
                       conclusion=excluded.conclusion,worker_status=excluded.worker_status,
                       worker_stage=excluded.worker_stage,completed=excluded.completed,
                       total=excluded.total,last_control_sequence=excluded.last_control_sequence,
                       last_heartbeat_at=excluded.last_heartbeat_at,
                       artifact_id=excluded.artifact_id,import_state=excluded.import_state,
                       cleanup_state=excluded.cleanup_state,error_code=excluded.error_code,
                       observed_at=excluded.observed_at,updated_at=excluded.updated_at""",
                (
                    str(task_id), max(1, int(attempt)), str(current.get("repository") or ""),
                    str(current.get("workflow") or ""), current.get("run_id"),
                    str(current.get("github_status") or ""), str(current.get("conclusion") or ""),
                    str(current.get("worker_status") or ""), str(current.get("worker_stage") or ""),
                    current.get("completed"), current.get("total"),
                    max(0, int(current.get("last_control_sequence") or 0)),
                    current.get("last_heartbeat_at"), current.get("artifact_id"),
                    str(current.get("import_state") or ""), str(current.get("cleanup_state") or ""),
                    str(current.get("error_code") or ""), current.get("observed_at"), now,
                ),
            )
        return self.get_remote_attempt(task_id, attempt) or {}

    def recover_failed_cancellation_attempt_one(
        self, task_id: str, *, repository: str, workflow: str, run_id: int,
    ) -> bool:
        """Atomically tombstone one externally proven failed cancellation.

        This is deliberately local-only: callers provide the already-reviewed
        terminal fact, and this method neither discovers nor changes remote
        state.  ``False`` means the exact terminal tombstone was already
        recorded; every other mismatch fails closed.
        """
        task_id, repository, workflow = str(task_id), str(repository), str(workflow)
        run_id = int(run_id)
        if not task_id or not repository or workflow != "process.yml" or run_id <= 0:
            raise ValueError("failed cancellation recovery binding is invalid")
        terminal_attempt = {
            "repository": repository, "workflow": workflow, "run_id": run_id,
            "github_status": "completed", "conclusion": "failure",
            "worker_status": "failed", "worker_stage": "", "completed": None,
            "total": None, "last_control_sequence": 0, "last_heartbeat_at": None,
            "artifact_id": None, "import_state": "not_started", "cleanup_state": "complete",
            "error_code": "", "observed_at": None,
        }
        with self._lock, self._connect() as db:
            remote = db.execute(
                """SELECT repository,workflow,run_id,attempt,remote_state,issue_number,artifact_id,
                          input_hash,pipeline_version,checkpoint_json FROM remote_runs WHERE task_id=?""",
                (task_id,),
            ).fetchone()
            if remote is None or any((
                str(remote["repository"]) != repository,
                str(remote["workflow"]) != workflow,
                int(remote["run_id"] or 0) != run_id,
                int(remote["attempt"] or 0) != 1,
                int(remote["issue_number"] or 0) != 0,
                int(remote["artifact_id"] or 0) != 0,
                str(remote["input_hash"] or "") != "",
                str(remote["pipeline_version"] or "") != "",
                str(remote["checkpoint_json"] or "") != "{}",
            )):
                raise RuntimeError("failed cancellation recovery binding drifted")
            attempt = db.execute(
                "SELECT * FROM remote_run_attempts WHERE task_id=? AND attempt=1", (task_id,)
            ).fetchone()
            if str(remote["remote_state"]) == "failed":
                if attempt is None or any(attempt[key] != value for key, value in terminal_attempt.items()):
                    raise RuntimeError("failed cancellation recovery terminal record drifted")
                return False
            if str(remote["remote_state"]) != "canceling" or attempt is not None:
                raise RuntimeError("failed cancellation recovery state drifted")
            now = time.time()
            changed = db.execute(
                """UPDATE remote_runs SET remote_state='failed', updated_at=?
                     WHERE task_id=? AND repository=? AND workflow=? AND run_id=?
                       AND attempt=1 AND remote_state='canceling'
                       AND COALESCE(issue_number,0)=0 AND COALESCE(artifact_id,0)=0
                       AND COALESCE(input_hash,'')='' AND COALESCE(pipeline_version,'')=''
                       AND COALESCE(checkpoint_json,'{}')='{}'""",
                (now, task_id, repository, workflow, run_id),
            )
            if int(changed.rowcount or 0) != 1:
                raise RuntimeError("failed cancellation recovery compare-and-set failed")
            db.execute(
                """INSERT INTO remote_run_attempts(
                       task_id,attempt,repository,workflow,run_id,github_status,conclusion,
                       worker_status,import_state,cleanup_state,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, 1, repository, workflow, run_id, "completed", "failure",
                 "failed", "not_started", "complete", now),
            )
        return True

    def reconcile_process_canary_attempt_one(
        self, task_id: str, *, repository: str, run_id: int,
        github_status: str, conclusion: str,
    ) -> bool:
        """Atomically record live truth only for the exact prepared rerun pair."""
        now = time.time()
        with self._lock, self._connect() as db:
            # These EXISTS guards are intentionally inside the one statement:
            # no remote metadata is touched and a stale/missing attempt two
            # makes the status reconciliation a complete no-op.
            attempt = db.execute(
                """UPDATE remote_run_attempts
                      SET github_status=?, conclusion=?, updated_at=?
                    WHERE task_id=? AND attempt=1 AND repository=?
                      AND workflow='process.yml' AND run_id=?
                      AND EXISTS (
                          SELECT 1 FROM remote_runs
                           WHERE task_id=? AND repository=? AND workflow='process.yml'
                             AND run_id=? AND attempt=1 AND remote_state='rerun_preparing'
                             AND COALESCE(issue_number, 0)=0 AND COALESCE(artifact_id, 0)=0
                             AND input_hash='' AND pipeline_version='' AND checkpoint_json='{}'
                      )
                      AND EXISTS (
                          SELECT 1 FROM remote_run_attempts AS attempt_two
                           WHERE attempt_two.task_id=? AND attempt_two.attempt=2
                             AND attempt_two.repository=? AND attempt_two.workflow='process.yml'
                             AND attempt_two.run_id=? AND attempt_two.github_status='not_requested'
                             AND attempt_two.worker_status='planned'
                             AND attempt_two.worker_stage='payload_preparing'
                             AND attempt_two.import_state='not_started'
                             AND attempt_two.cleanup_state='not_started'
                      )""",
                (str(github_status), str(conclusion), now, str(task_id), str(repository),
                 int(run_id), str(task_id), str(repository), int(run_id), str(task_id),
                 str(repository), int(run_id)),
            )
        return int(attempt.rowcount or 0) == 1

    def get_remote_attempt(self, task_id: str, attempt: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM remote_run_attempts WHERE task_id=? AND attempt=?",
                (str(task_id), max(1, int(attempt))),
            ).fetchone()
        return None if row is None else dict(row)

    def list_remote_attempts(self, *, task_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(200, int(limit)))
        query = "SELECT * FROM remote_run_attempts"
        params: list[Any] = []
        if task_id:
            query += " WHERE task_id=?"
            params.append(str(task_id))
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(bounded)
        with self._lock, self._connect() as db:
            return [dict(row) for row in db.execute(query, params).fetchall()]

    def set_remote_token_lease(self, task_id: str, *, state: str, expires_at: float | None = None) -> None:
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO remote_token_leases(task_id,state,expires_at,updated_at)
                   VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
                   state=excluded.state,expires_at=excluded.expires_at,updated_at=excluded.updated_at""",
                (str(task_id), str(state), expires_at, now),
            )

    def delete_remote_token_lease(self, task_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM remote_token_leases WHERE task_id=?", (str(task_id),))

    def list_remote_token_leases(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM remote_token_leases ORDER BY updated_at DESC"
            ).fetchall()]

    def acquire_remote_supervisor_lease(
        self, task_id: str, *, owner: str, ttl_seconds: float
    ) -> bool:
        """One live supervisor per task; expired leases are preemptible.

        The same owner re-acquiring is a renewal.  A crashed supervisor's
        lease simply expires, so recovery never depends on process liveness.
        """
        now = time.time()
        expires_at = now + float(ttl_seconds)
        with self._lock, self._connect() as db:
            db.execute(
                "DELETE FROM remote_supervisor_leases WHERE task_id=? AND expires_at<=?",
                (str(task_id), now),
            )
            row = db.execute(
                "SELECT owner FROM remote_supervisor_leases WHERE task_id=?",
                (str(task_id),),
            ).fetchone()
            if row is not None and str(row["owner"]) != str(owner):
                return False
            db.execute(
                """INSERT INTO remote_supervisor_leases(task_id,owner,acquired_at,expires_at)
                   VALUES(?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
                   owner=excluded.owner,acquired_at=excluded.acquired_at,
                   expires_at=excluded.expires_at""",
                (str(task_id), str(owner), now, expires_at),
            )
            return True

    def renew_remote_supervisor_lease(
        self, task_id: str, *, owner: str, ttl_seconds: float
    ) -> bool:
        now = time.time()
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """UPDATE remote_supervisor_leases SET expires_at=?
                   WHERE task_id=? AND owner=? AND expires_at>?""",
                (now + float(ttl_seconds), str(task_id), str(owner), now),
            )
            return bool(cursor.rowcount)

    def release_remote_supervisor_lease(self, task_id: str, *, owner: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "DELETE FROM remote_supervisor_leases WHERE task_id=? AND owner=?",
                (str(task_id), str(owner)),
            )

    def migration_cleanup_pending_count(self) -> int:
        """Count every local cleanup gate without truncating diagnostic lists."""
        with self._lock, self._connect() as db:
            remote_attempts = int(db.execute(
                """SELECT COUNT(*) FROM remote_run_attempts
                   WHERE cleanup_state IN ('pending','cleanup_pending')
                      OR import_state='cleanup_pending'"""
            ).fetchone()[0])
            remote_runs = int(db.execute(
                "SELECT COUNT(*) FROM remote_runs WHERE last_error='remote_cleanup_pending'"
            ).fetchone()[0])
            automation_imports = int(db.execute(
                "SELECT COUNT(*) FROM automation_imports WHERE state='cleanup_pending'"
            ).fetchone()[0])
        return remote_attempts + remote_runs + automation_imports

    def append_remote_event(self, topic: str, entity_id: str, payload: dict[str, Any]) -> int:
        now = time.time()
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "INSERT INTO remote_events(topic,entity_id,payload_json,created_at) VALUES(?,?,?,?)",
                (str(topic), str(entity_id), self._dump(payload), now),
            )
            sequence = int(cursor.lastrowid)
            cutoff = now - 7 * 24 * 60 * 60
            db.execute("DELETE FROM remote_events WHERE created_at < ?", (cutoff,))
            db.execute(
                """DELETE FROM remote_events WHERE sequence IN (
                       SELECT sequence FROM remote_events ORDER BY sequence DESC LIMIT -1 OFFSET 10000
                   )"""
            )
        return sequence

    def list_remote_events(
        self, *, after_sequence: int = 0, topics: Iterable[str] | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(500, int(limit)))
        values = [str(item) for item in (topics or []) if str(item)]
        query = "SELECT * FROM remote_events WHERE sequence>?"
        params: list[Any] = [max(0, int(after_sequence))]
        if values:
            query += " AND topic IN (" + ",".join("?" for _ in values) + ")"
            params.extend(values)
        query += " ORDER BY sequence LIMIT ?"
        params.append(bounded)
        with self._lock, self._connect() as db:
            rows = db.execute(query, params).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["payload"] = self._load(item.pop("payload_json", "{}"))
            output.append(item)
        return output

    def begin_remote_operation(self, operation_id: str, action: str, target_id: str = "") -> tuple[dict[str, Any], bool]:
        now = time.time()
        with self._lock, self._connect() as db:
            existing = db.execute(
                "SELECT * FROM remote_operations WHERE operation_id=?", (str(operation_id),)
            ).fetchone()
            if existing is not None:
                value = dict(existing)
                value["result"] = self._load(value.pop("result_json", "{}"))
                return value, False
            db.execute(
                """INSERT INTO remote_operations(
                       operation_id,action,target_id,state,result_json,error_code,created_at,updated_at
                   ) VALUES(?,?,?,'running','{}','',?,?)""",
                (str(operation_id), str(action), str(target_id), now, now),
            )
        value = self.get_remote_operation(operation_id)
        assert value is not None
        return value, True

    def finish_remote_operation(
        self, operation_id: str, *, state: str, result: dict[str, Any] | None = None, error_code: str = ""
    ) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            db.execute(
                """UPDATE remote_operations SET state=?,result_json=?,error_code=?,updated_at=?
                   WHERE operation_id=?""",
                (str(state), self._dump(dict(result or {})), str(error_code), time.time(), str(operation_id)),
            )
        value = self.get_remote_operation(operation_id)
        if value is None:
            raise KeyError("remote operation not found")
        return value

    def get_remote_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM remote_operations WHERE operation_id=?", (str(operation_id),)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["result"] = self._load(value.pop("result_json", "{}"))
        return value

    def save_automation_profile(self, profile: dict[str, Any], *, profile_id: str = "default") -> dict[str, Any]:
        now = time.time()
        current = self.get_automation_profile(profile_id)
        merged = {**current, **dict(profile or {})}
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO automation_profiles(
                       profile_id,mode,state,schedule_time,timezone,budget_json,
                       config_hash,verified_config_hash,verification_json,local_schedule_was_enabled,
                       protocol,account_id,binding_json,observed_at,expires_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(profile_id) DO UPDATE SET
                       mode=excluded.mode,state=excluded.state,schedule_time=excluded.schedule_time,
                       timezone=excluded.timezone,budget_json=excluded.budget_json,
                       config_hash=excluded.config_hash,
                       verified_config_hash=excluded.verified_config_hash,
                       verification_json=excluded.verification_json,
                       local_schedule_was_enabled=excluded.local_schedule_was_enabled,
                       protocol=excluded.protocol,account_id=excluded.account_id,
                       binding_json=excluded.binding_json,
                       observed_at=excluded.observed_at,expires_at=excluded.expires_at,
                       updated_at=excluded.updated_at""",
                (
                    str(profile_id), str(merged.get("mode") or "local"),
                    str(merged.get("state") or "disabled"),
                    str(merged.get("schedule_time") or "07:30"),
                    str(merged.get("timezone") or "Asia/Shanghai"),
                    self._dump(merged.get("budget") or {}),
                    str(merged.get("config_hash") or ""),
                    str(merged.get("verified_config_hash") or ""),
                    self._dump(merged.get("verification") or {}),
                    int(bool(merged.get("local_schedule_was_enabled"))),
                    str(merged.get("protocol") or ""),
                    str(merged.get("account_id") or ""),
                    self._dump(merged.get("binding") or {}),
                    float(merged.get("observed_at") or now),
                    float(merged.get("expires_at") or now), now,
                ),
            )
        return self.get_automation_profile(profile_id)

    def get_automation_profile(self, profile_id: str = "default") -> dict[str, Any]:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM automation_profiles WHERE profile_id=?", (str(profile_id),)
            ).fetchone()
        if row is None:
            return {
                "profile_id": str(profile_id), "mode": "local", "state": "disabled",
                "schedule_time": "07:30", "timezone": "Asia/Shanghai",
                # O2（AS3 对齐）：回显兜底与 automation.DEFAULT_BUDGET 同值
                # （20 讲/2000 分钟/200 万 token）；task_store 不得反向 import
                # automation（层级单向），此处为同值镜像，改默认须同笔两处。
                "budget": {"max_lectures": 20, "max_runner_minutes": 2_000, "max_deepseek_tokens": 2_000_000},
                "config_hash": "", "verified_config_hash": "", "verification": {},
                "local_schedule_was_enabled": False, "protocol": "", "account_id": "",
                "binding": {}, "observed_at": 0.0,
                "expires_at": 0.0, "updated_at": 0.0,
            }
        value = dict(row)
        value["budget"] = self._load(value.pop("budget_json", "{}"))
        value["verification"] = self._load(value.pop("verification_json", "{}"))
        value["binding"] = self._load(value.pop("binding_json", "{}"))
        value["local_schedule_was_enabled"] = bool(value.get("local_schedule_was_enabled"))
        return value

    def automation_profile_ids(self) -> list[str]:
        """Distinct profile ids that currently own automation rules."""
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT profile_id FROM automation_course_rules ORDER BY profile_id"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def release_stuck_lifecycle_rows(self) -> dict[str, int]:
        """Force-release every stuck lifecycle row (U5/M16 unlock path).

        针对数据页报得出却清不掉的残存类：长期挂起的任务（含长期 paused）、
        remote_state 为 NULL 或非终态的远端运行、在途自动导入、cleanup_pending
        门。只碰闭集探测会当成阻塞的行，终态历史一行不动。远端运行标
        canceled 与外部真实运行无关——那类行本就该由用户显式解锁后重建。
        """
        now = time.time()
        released: dict[str, int] = {}
        with self._lock, self._connect() as db:
            released["tasks"] = int(db.execute(
                """UPDATE tasks SET state='canceled', finished_at=?, resume_requested=0
                    WHERE state IS NULL OR state NOT IN ('completed','failed','canceled')""",
                (now,),
            ).rowcount)
            released["remote_runs"] = int(db.execute(
                """UPDATE remote_runs SET remote_state='canceled', updated_at=?
                    WHERE remote_state IS NULL
                       OR remote_state NOT IN ('completed','imported','failed','canceled')""",
                (now,),
            ).rowcount)
            released["automation_imports"] = int(db.execute(
                """UPDATE automation_imports SET state='unrecoverable', updated_at=?
                    WHERE state IS NULL OR state NOT IN ('imported','failed','unrecoverable')""",
                (now,),
            ).rowcount)
            released["cleanup_gates"] = int(db.execute(
                """UPDATE remote_run_attempts
                      SET cleanup_state=CASE WHEN cleanup_state IN ('pending','cleanup_pending')
                                             THEN 'complete' ELSE cleanup_state END,
                          import_state=CASE WHEN import_state='cleanup_pending'
                                            THEN 'unrecoverable' ELSE import_state END
                    WHERE cleanup_state IN ('pending','cleanup_pending')
                       OR import_state='cleanup_pending'""",
            ).rowcount)
        return released

    def replace_automation_rules(self, rules: Iterable[dict[str, Any]], *, profile_id: str = "default") -> list[dict[str, Any]]:
        now = time.time()
        normalized = []
        for raw in rules:
            rule = dict(raw or {})
            course_id = str(rule.get("course_id") or "").strip()
            if not course_id:
                continue
            rule["course_id"] = course_id
            normalized.append(rule)
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM automation_course_rules WHERE profile_id=?", (str(profile_id),))
            db.executemany(
                "INSERT INTO automation_course_rules(profile_id,course_id,rule_json,updated_at) VALUES(?,?,?,?)",
                [(str(profile_id), rule["course_id"], self._dump(rule), now) for rule in normalized],
            )
        return self.list_automation_rules(profile_id=profile_id)

    def list_automation_rules(self, *, profile_id: str = "default") -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT rule_json FROM automation_course_rules WHERE profile_id=? ORDER BY course_id",
                (str(profile_id),),
            ).fetchall()
        return [dict(self._load(row[0]) or {}) for row in rows]

    def upsert_automation_run(self, run_key: str, **fields: Any) -> dict[str, Any] | None:
        key = str(run_key)
        now = time.time()
        # AUTO-TOMBSTONE-R2（洞 A 根修）：墓碑检查→读旧行→插行全程持有同一把
        # 锁。此前的「查墓碑→读行→插行」跨三次独立锁获取，与并发
        # delete_automation_run(_s)（同锁串行化）存在 check-then-insert 竞态：
        # 检查已过、删除提交、插行复活——产出一行墓碑尚在却永久可见的冻结
        # 复活卡。锁内串行化后，删除只可能发生在插行之前（闸门命中=no-op）
        # 或之后（行死+墓碑在），复活交错不复存在。
        with self._lock:
            if self._automation_run_deleted(key):
                # P57（AUTOMATION-RUN-TOMBSTONE-1）：用户删过的 run_key 不再合法
                # 复活。reconcile（automation.py:964）与 ghost 收敛（automation.py
                # :1396）都经此唯一入口，命中墓碑即干净 no-op，不抛错。
                return None
            current = self.get_automation_run(key) or {}
            merged = {**current, **fields}
            with self._connect() as db:
                db.execute(
                    """INSERT INTO automation_runs(
                           run_key,github_run_id,attempt,workflow,trigger_kind,state,conclusion,
                           config_hash,counts_json,budget_json,error_code,started_at,concluded_at,
                           observed_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(run_key) DO UPDATE SET github_run_id=excluded.github_run_id,
                           attempt=excluded.attempt,workflow=excluded.workflow,trigger_kind=excluded.trigger_kind,
                           state=excluded.state,conclusion=excluded.conclusion,config_hash=excluded.config_hash,
                           counts_json=excluded.counts_json,budget_json=excluded.budget_json,
                           error_code=excluded.error_code,started_at=excluded.started_at,
                           concluded_at=excluded.concluded_at,observed_at=excluded.observed_at,
                           updated_at=excluded.updated_at""",
                    (
                        key, merged.get("github_run_id"), int(merged.get("attempt") or 1),
                        str(merged.get("workflow") or ""), str(merged.get("trigger_kind") or "manual"),
                        str(merged.get("state") or "queued"), str(merged.get("conclusion") or ""),
                        str(merged.get("config_hash") or ""), self._dump(merged.get("counts") or {}),
                        self._dump(merged.get("budget") or {}), str(merged.get("error_code") or ""),
                        float(merged.get("started_at") or 0.0), float(merged.get("concluded_at") or 0.0),
                        float(merged.get("observed_at") or now), now,
                    ),
                )
        value = self.get_automation_run(key)
        assert value is not None
        return value

    def _automation_run_deleted(self, run_key: str) -> bool:
        try:
            with self._lock, self._connect() as db:
                row = db.execute(
                    "SELECT 1 FROM automation_run_tombstones WHERE run_key=?", (str(run_key),)
                ).fetchone()
        except sqlite3.OperationalError:
            # 尚未建墓碑表的存量库按旧语义放行（只读审计路径本就不可写）。
            return False
        return row is not None

    def get_automation_run(self, run_key: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM automation_runs WHERE run_key=?", (str(run_key),)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["counts"] = self._load(value.pop("counts_json", "{}"))
        value["budget"] = self._load(value.pop("budget_json", "{}"))
        return value

    def list_automation_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT run_key FROM automation_runs ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(200, int(limit))),),
            ).fetchall()
        return [value for row in rows if (value := self.get_automation_run(str(row[0]))) is not None]

    def delete_automation_run(self, run_key: str) -> dict[str, Any]:
        """Remove one *terminal* run record (第卅六案③：只删记录，不触产物）。

        KeyError=记录不存在；ValueError=记录仍在飞（活动态不允许删除）。
        P57：硬删行 + 同事务落墓碑——对账链再观测到该 key 时经
        upsert_automation_run 入口 no-op，删除因此持久（重启后不复活）。
        """
        key = str(run_key or "").strip()
        run = self.get_automation_run(key) if key else None
        if run is None:
            raise KeyError("automation run not found")
        terminal = str(run.get("state") or "") == "completed" or bool(
            str(run.get("conclusion") or "")
        )
        if not terminal:
            raise ValueError("only terminal run records can be deleted")
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM automation_runs WHERE run_key=?", (key,))
            db.execute(
                "INSERT INTO automation_run_tombstones(run_key,deleted_at) VALUES(?,?) "
                "ON CONFLICT(run_key) DO UPDATE SET deleted_at=excluded.deleted_at",
                (key, now),
            )
            db.execute(
                """DELETE FROM automation_run_tombstones WHERE run_key NOT IN (
                       SELECT run_key FROM automation_run_tombstones
                       ORDER BY deleted_at DESC, run_key DESC LIMIT ?
                   )""",
                (int(AUTOMATION_RUN_TOMBSTONE_LIMIT),),
            )
        return {"deleted": True, "run_key": key}

    def delete_automation_runs(self, run_keys: list[str]) -> int:
        """Remove exact run rows (SRC-SYNDROME-1 追加D：陈旧空转行清扫)。

        AUTO-TOMBSTONE-R2（洞 B 根修，C6 复现实证）：批量删与单删同契约——
        实删的键在同一事务落墓碑（体量走既有 LRU 有界）。此前批量硬删不落
        墓碑，被清扫的 key 一旦再被任何重建链观测到即整体复活。现网滤镜
        （reconcile 跳过 schedule+skipped 导入）只是行为性护栏，墓碑才是
        P57 承诺的闸门本体。只为实删的键落墓碑：不存在的键不产生删除事实，
        也不得凭空封死未来同键的合法运行记录。
        """
        keys = [str(key) for key in run_keys if str(key)]
        if not keys:
            return 0
        now = time.time()
        with self._lock, self._connect() as db:
            marks = ",".join("?" for _ in keys)
            existing = [
                str(row[0]) for row in db.execute(
                    f"SELECT run_key FROM automation_runs WHERE run_key IN ({marks})",
                    keys,
                ).fetchall()
            ]
            if not existing:
                return 0
            del_marks = ",".join("?" for _ in existing)
            cursor = db.execute(
                f"DELETE FROM automation_runs WHERE run_key IN ({del_marks})",
                existing,
            )
            deleted = int(cursor.rowcount or 0)
            db.executemany(
                "INSERT INTO automation_run_tombstones(run_key,deleted_at) VALUES(?,?) "
                "ON CONFLICT(run_key) DO UPDATE SET deleted_at=excluded.deleted_at",
                [(key, now) for key in existing],
            )
            db.execute(
                """DELETE FROM automation_run_tombstones WHERE run_key NOT IN (
                       SELECT run_key FROM automation_run_tombstones
                       ORDER BY deleted_at DESC, run_key DESC LIMIT ?
                   )""",
                (int(AUTOMATION_RUN_TOMBSTONE_LIMIT),),
            )
            return deleted

    def upsert_automation_import(self, artifact_id: int, **fields: Any) -> dict[str, Any]:
        current = self.get_automation_import(artifact_id) or {}
        merged = {**current, **fields}
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO automation_imports(
                       artifact_id,artifact_name,run_id,state,result_hash,error_code,expires_at,observed_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(artifact_id) DO UPDATE SET artifact_name=excluded.artifact_name,
                       run_id=excluded.run_id,state=excluded.state,result_hash=excluded.result_hash,
                       error_code=excluded.error_code,expires_at=excluded.expires_at,
                       observed_at=excluded.observed_at,updated_at=excluded.updated_at""",
                (
                    int(artifact_id), str(merged.get("artifact_name") or ""), merged.get("run_id"),
                    str(merged.get("state") or "available"), str(merged.get("result_hash") or ""),
                    str(merged.get("error_code") or ""), merged.get("expires_at"),
                    float(merged.get("observed_at") or now), now,
                ),
            )
        value = self.get_automation_import(artifact_id)
        assert value is not None
        return value

    def get_automation_import(self, artifact_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM automation_imports WHERE artifact_id=?", (int(artifact_id),)).fetchone()
        return None if row is None else dict(row)

    def list_automation_imports(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM automation_imports ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_automation_budget(self, budget_date: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM automation_budget_ledger WHERE budget_date=?", (str(budget_date),)
            ).fetchone()
        if row is None:
            return {"budget_date": str(budget_date), "lectures": 0, "runner_minutes": 0.0,
                    "deepseek_tokens": 0, "reserved_runner_minutes": 0.0, "updated_at": 0.0}
        return dict(row)

    def update_automation_budget(self, budget_date: str, **increments: Any) -> dict[str, Any]:
        allowed = {"lectures", "runner_minutes", "deepseek_tokens", "reserved_runner_minutes"}
        current = self.get_automation_budget(budget_date)
        for key in allowed:
            if key in increments:
                current[key] = max(0, current.get(key, 0) + increments[key])
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO automation_budget_ledger(
                       budget_date,lectures,runner_minutes,deepseek_tokens,reserved_runner_minutes,updated_at
                   ) VALUES(?,?,?,?,?,?) ON CONFLICT(budget_date) DO UPDATE SET
                       lectures=excluded.lectures,runner_minutes=excluded.runner_minutes,
                       deepseek_tokens=excluded.deepseek_tokens,
                       reserved_runner_minutes=excluded.reserved_runner_minutes,updated_at=excluded.updated_at""",
                (str(budget_date), int(current["lectures"]), float(current["runner_minutes"]),
                 int(current["deepseek_tokens"]), float(current["reserved_runner_minutes"]), now),
            )
        return self.get_automation_budget(budget_date)

    def set_automation_circuit(self, circuit: str, **fields: Any) -> dict[str, Any]:
        current = self.get_automation_circuit(circuit)
        merged = {**current, **fields}
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO automation_circuit_state(
                       circuit,state,consecutive_failures,opened_at,retry_after,last_error_code,updated_at
                   ) VALUES(?,?,?,?,?,?,?) ON CONFLICT(circuit) DO UPDATE SET state=excluded.state,
                       consecutive_failures=excluded.consecutive_failures,opened_at=excluded.opened_at,
                       retry_after=excluded.retry_after,last_error_code=excluded.last_error_code,
                       updated_at=excluded.updated_at""",
                (str(circuit), str(merged.get("state") or "closed"),
                 int(merged.get("consecutive_failures") or 0), merged.get("opened_at"),
                 merged.get("retry_after"), str(merged.get("last_error_code") or ""), now),
            )
        return self.get_automation_circuit(circuit)

    def get_automation_circuit(self, circuit: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM automation_circuit_state WHERE circuit=?", (str(circuit),)
            ).fetchone()
        return dict(row) if row is not None else {
            "circuit": str(circuit), "state": "closed", "consecutive_failures": 0,
            "opened_at": None, "retry_after": None, "last_error_code": "", "updated_at": 0.0,
        }

    def list_automation_circuits(self) -> list[dict[str, Any]]:
        return [self.get_automation_circuit(name) for name in ("authentication", "deepseek", "platform", "budget")]

    def get_app_state(self, key: str, default: Any = None) -> Any:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT value_json FROM app_state WHERE key=?", (key,)).fetchone()
        return default if row is None else self._load(row[0])

    def set_app_state(self, key: str, value: Any) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """INSERT INTO app_state(key,value_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (key, self._dump(value), time.time()),
            )

    @property
    def global_paused(self) -> bool:
        return bool(self.get_app_state("global_paused", False))

    def set_global_paused(self, paused: bool) -> None:
        self.set_app_state("global_paused", bool(paused))

    def recover_for_startup(self) -> int:
        now = time.time()
        with self._lock, self._connect() as db:
            cur = db.execute(
                """UPDATE tasks SET state='paused',resume_requested=0,updated_at=?
                   WHERE state IN ('queued','running','pausing')""",
                (now,),
            )
            db.execute("UPDATE tasks SET resume_requested=0,updated_at=? WHERE state='paused' AND resume_requested!=0", (now,))
            unfinished = int(db.execute("SELECT COUNT(*) FROM tasks WHERE state='paused'").fetchone()[0])
            if unfinished:
                db.execute(
                    """INSERT INTO app_state(key,value_json,updated_at) VALUES('global_paused','true',?)
                       ON CONFLICT(key) DO UPDATE SET value_json='true',updated_at=excluded.updated_at""",
                    (now,),
                )
            else:
                db.execute(
                    """INSERT INTO app_state(key,value_json,updated_at) VALUES('global_paused','false',?)
                       ON CONFLICT(key) DO UPDATE SET value_json='false',updated_at=excluded.updated_at""",
                    (now,),
                )
            return int(cur.rowcount or 0)

    def checkpoint_for_shutdown(self) -> int:
        """Atomically leave every non-terminal task in a resumable state."""
        now = time.time()
        with self._lock, self._connect() as db:
            cur = db.execute(
                """UPDATE tasks SET state='paused',resume_requested=0,updated_at=?
                   WHERE state IN ('queued','running','pausing')""",
                (now,),
            )
            db.execute(
                """INSERT INTO app_state(key,value_json,updated_at)
                   VALUES('global_paused','true',?)
                   ON CONFLICT(key) DO UPDATE SET
                       value_json='true',updated_at=excluded.updated_at""",
                (now,),
            )
            return int(cur.rowcount or 0)

    def add_task(self, kind: str, course_id: str, sub_id: str, payload: dict[str, Any], *,
                 config_key: str = "", start_paused: bool | None = None) -> tuple[dict[str, Any], bool]:
        with self._lock, self._connect() as db:
            row = db.execute(
                """SELECT * FROM tasks WHERE kind=? AND sub_id=? AND config_key=?
                   AND state IN ('queued','running','pausing','paused') ORDER BY sequence DESC LIMIT 1""",
                (kind, str(sub_id), config_key),
            ).fetchone()
            if row is not None:
                return self._row(row), False
            sequence = int(db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM tasks").fetchone()[0])
            if start_paused is None:
                state_row = db.execute("SELECT value_json FROM app_state WHERE key='global_paused'").fetchone()
                paused = bool(self._load(state_row[0])) if state_row else False
            else:
                paused = bool(start_paused)
            task_id = uuid.uuid4().hex
            now = time.time()
            db.execute(
                """INSERT INTO tasks(task_id,kind,course_id,sub_id,config_key,state,payload_json,
                   sequence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (task_id, kind, str(course_id), str(sub_id), config_key,
                 "paused" if paused else "queued", self._dump(payload), sequence, now, now),
            )
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return self._row(row), True

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._row(row) if row is not None else None

    def find_active(self, kind: str, sub_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                """SELECT * FROM tasks WHERE kind=? AND sub_id=?
                   AND state IN ('queued','running','pausing','paused') ORDER BY sequence DESC LIMIT 1""",
                (kind, str(sub_id)),
            ).fetchone()
        return self._row(row) if row is not None else None

    def list_tasks(self, *, kinds: Iterable[str] | None = None, states: Iterable[str] | None = None,
                   limit: int = 500, newest_first: bool = False,
                   terminal_retention_days: float | None = TERMINAL_RETENTION_DAYS) -> list[dict[str, Any]]:
        """List tasks in ``sequence`` order (ascending by default).

        ``newest_first=True`` flips to ``ORDER BY sequence DESC`` so a bounded
        ``limit`` window covers the most recent tasks instead of the oldest
        ones — the dead-task residue root cause (CLIENT-STATE-R1).

        ``terminal_retention_days`` hides terminal-state rows whose terminal
        timestamp (``finished_at``, falling back to ``updated_at`` for legacy
        rows) is older than the window. Presentation-only: rows stay on disk.
        Pass ``None`` to see the full history (recovery/evidence callers).
        """
        clauses: list[str] = []
        args: list[Any] = []
        if kinds:
            values = tuple(kinds)
            clauses.append(f"kind IN ({','.join('?' for _ in values)})")
            args.extend(values)
        if states:
            values = tuple(states)
            clauses.append(f"state IN ({','.join('?' for _ in values)})")
            args.extend(values)
        if terminal_retention_days is not None:
            placeholders = ",".join("?" for _ in TERMINAL_STATES)
            clauses.append(
                f"(state NOT IN ({placeholders}) OR COALESCE(finished_at, updated_at) >= ?)"
            )
            args.extend(TERMINAL_STATES)
            args.append(time.time() - max(0.0, float(terminal_retention_days)) * 86400.0)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(max(1, int(limit)))
        order = "ORDER BY sequence DESC" if newest_first else "ORDER BY sequence"
        with self._lock, self._connect() as db:
            rows = db.execute(f"SELECT * FROM tasks{where} {order} LIMIT ?", args).fetchall()
        return [self._row(row) for row in rows]

    def public_task_context(self, task_ids: Iterable[str]) -> dict[str, dict[str, dict[str, Any]]]:
        """Read task-center metadata in three bounded queries, not one query per card."""
        ids = list(dict.fromkeys(str(value) for value in task_ids if str(value)))[:200]
        result: dict[str, dict[str, dict[str, Any]]] = {
            "metadata": {}, "remote": {}, "attempt": {},
        }
        if not ids:
            return result
        placeholders = ",".join("?" for _ in ids)
        with self._lock, self._connect() as db:
            metadata_rows = db.execute(
                f"SELECT * FROM task_metadata_v3 WHERE task_id IN ({placeholders})", ids
            ).fetchall()
            remote_rows = db.execute(
                f"SELECT * FROM remote_runs WHERE task_id IN ({placeholders})", ids
            ).fetchall()
            attempt_rows = db.execute(
                f"SELECT * FROM remote_run_attempts WHERE task_id IN ({placeholders})", ids
            ).fetchall()
        for row in metadata_rows:
            value = dict(row)
            for key in ("requested_outputs", "cost", "estimate", "resources"):
                value[key] = self._load(value.pop(f"{key}_json", "{}" if key != "requested_outputs" else "[]"))
            value["result_notices"] = self._load(value.pop("result_notices_json", "{}"))
            result["metadata"][str(value["task_id"])] = value
        for row in remote_rows:
            value = dict(row)
            value.pop("checkpoint_json", None)
            result["remote"][str(value["task_id"])] = value
        remote_attempts = {
            task_id: max(1, int(value.get("attempt") or 1))
            for task_id, value in result["remote"].items()
        }
        for row in attempt_rows:
            value = dict(row)
            task_id = str(value["task_id"])
            if int(value.get("attempt") or 0) == remote_attempts.get(task_id):
                result["attempt"][task_id] = value
        # Queue forecasts are one bounded lookup per distinct workflow, not
        # per card; queued cards share the hierarchical answer.
        workflows = {
            str(value.get("workflow") or "")
            for value in result["remote"].values()
            if value.get("workflow")
        }
        result["queue_forecast"] = {
            workflow: self.queue_forecast_for_workflow(workflow)
            for workflow in sorted(workflows)
        }
        return result

    def update_task(self, task_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "state", "resume_requested", "payload", "progress", "estimate", "checkpoint",
            "error", "started_at", "finished_at",
        }
        json_fields = {"payload", "progress", "estimate", "checkpoint"}
        assignments: list[str] = []
        values: list[Any] = []
        for name, value in fields.items():
            if name not in allowed:
                continue
            assignments.append(f"{name + '_json' if name in json_fields else name}=?")
            values.append(self._dump(value) if name in json_fields else (int(bool(value)) if name == "resume_requested" else value))
        if not assignments:
            return self.get_task(task_id)
        assignments.append("updated_at=?")
        values.extend((time.time(), task_id))
        with self._lock, self._connect() as db:
            db.execute(f"UPDATE tasks SET {','.join(assignments)} WHERE task_id=?", values)
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._row(row) if row is not None else None

    def mark_terminal(self, task_id: str, state: str, *, error: str = "") -> dict[str, Any] | None:
        if state not in TERMINAL_STATES:
            raise ValueError(f"invalid terminal state: {state}")
        # AS10：任务终态即清 busy 退避计数（app_state KV 不随任务无限累积）
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM app_state WHERE key=?", (f"{REMOTE_BUSY_STATE_KEY_PREFIX}{task_id}",))
        return self.update_task(
            task_id, state=state, resume_requested=False, error=error, finished_at=time.time()
        )

    def retry_failed_task(self, task_id: str) -> dict[str, Any] | None:
        """Requeue a failed task without changing its identity or remote run."""
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (str(task_id),)).fetchone()
            if row is None:
                return None
            if row["state"] != "failed":
                return self._row(row)
            payload = dict(self._load(row["payload_json"]))
            payload.pop(USER_PAUSE_INTENT_KEY, None)
            db.execute(
                """UPDATE tasks SET state='queued',resume_requested=0,error='',finished_at=NULL,
                   payload_json=?,updated_at=? WHERE task_id=?""",
                (self._dump(payload), now, str(task_id)),
            )
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._row(row)

    def record_task_usage(
        self, task_id: str, *,
        deepseek_tokens: int | None = None,
        runner_seconds: float | None = None,
    ) -> None:
        """AS6 消耗透镜：按任务行落**绝对**消耗值（非累加）。

        只写显式提供的字段；NULL=无该字段（历史行不显示、不伪造）。
        重导入/幂等路径重复写同值无害——绝对覆盖语义，绝不重复累加。
        """
        assignments: dict[str, Any] = {}
        if deepseek_tokens is not None:
            assignments["deepseek_tokens"] = max(0, int(deepseek_tokens))
        if runner_seconds is not None:
            assignments["runner_seconds"] = max(0.0, float(runner_seconds))
        if not assignments:
            return
        clean_id = str(task_id or "").strip()
        if not clean_id:
            return
        with self._lock, self._connect() as db:
            db.execute(
                f"UPDATE tasks SET {','.join(f'{name}=?' for name in assignments)} WHERE task_id=?",
                [*assignments.values(), clean_id],
            )

    def usage_totals_since(self, since: float) -> dict[str, float | int]:
        """AS6：本机累计消耗合计（只聚合已记录行；NULL 不计入，无行=0）。"""
        with self._lock, self._connect() as db:
            row = db.execute(
                """SELECT COALESCE(SUM(deepseek_tokens),0),COALESCE(SUM(runner_seconds),0)
                   FROM tasks WHERE created_at>=?""",
                (float(since),),
            ).fetchone()
        return {"deepseek_tokens": int(row[0] or 0), "runner_seconds": float(row[1] or 0.0)}

    def delete_task(self, task_id: str) -> dict[str, Any]:
        """Delete one terminal task record (U4 用户显式动作，无自动清理).

        Row-only scope: the record and its directly coupled detail rows go
        together; output artifacts live outside this store and are never
        touched, and the retired budget history tables stay as inert
        history.  Raises KeyError when the record is missing and ValueError
        when it is not terminal (active records keep their lifecycle locks).
        """
        task_id = str(task_id)
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = self._row(row)
            if str(task.get("state") or "") not in TERMINAL_STATES:
                raise ValueError("only terminal task records can be deleted")
            self._delete_task_details(db, [task_id])
            db.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        return task

    def delete_failed_tasks(self) -> int:
        """Bulk delete of every failed record; same row-only scope as
        ``delete_task``.  Returns the number of records removed."""
        with self._lock, self._connect() as db:
            ids = [
                str(row["task_id"])
                for row in db.execute("SELECT task_id FROM tasks WHERE state='failed'").fetchall()
            ]
            if not ids:
                return 0
            self._delete_task_details(db, ids)
            placeholders = ",".join("?" for _ in ids)
            cursor = db.execute(f"DELETE FROM tasks WHERE task_id IN ({placeholders})", ids)
            return int(cursor.rowcount or 0)

    def clear_stuck_tasks(
        self, *, stale_seconds: float = STUCK_TASK_STALE_SECONDS, now: float | None = None,
    ) -> list[str]:
        """DEAD-TASK-PURGE B：清除卡住的任务（用户显式动作，幂等）。

        判据双门，宁漏勿杀：①在途态（queued/running/pausing）且
        ``updated_at`` 距当下超过 ``stale_seconds``（30 分钟无任何进展；
        每次进度/检查点写入都会刷新 updated_at，真活跃到不了这条线）；
        ②远端无活跃 run（remote_state 在 REMOTE_RUN_LIFECYCLE_STATES 白名
        单外）——远端仍在跑的行由核真链收敛，绝不在这里误杀。命中行标
        ``failed``（closed-set 原因 user_cleared_stuck），自然落入 90 天终
        态保留窗；AS10 同款清 busy 退避键。返回被清 task_id 列表；重复调
        用命不中已清行=幂等。行级语义与 delete_failed_tasks 同族：只动记
        录行，绝不触产物文件。paused 行不进候选（用户暂停/全局暂停是合法
        待恢复态，恢复入口在卡片上）。
        """
        current = float(now if now is not None else time.time())
        cutoff = current - max(0.0, float(stale_seconds))
        lifecycle = ",".join("?" for _ in REMOTE_RUN_LIFECYCLE_STATES)
        cleared: list[str] = []
        with self._lock, self._connect() as db:
            rows = db.execute(
                f"""SELECT t.task_id FROM tasks t
                    LEFT JOIN remote_runs r ON r.task_id = t.task_id
                    WHERE t.state IN ('queued','running','pausing')
                      AND t.updated_at < ?
                      AND (r.task_id IS NULL OR r.remote_state IS NULL
                           OR r.remote_state NOT IN ({lifecycle}))""",
                (cutoff, *REMOTE_RUN_LIFECYCLE_STATES),
            ).fetchall()
            for row in rows:
                task_id = str(row["task_id"])
                cursor = db.execute(
                    """UPDATE tasks SET state='failed',resume_requested=0,error=?,
                       finished_at=?,updated_at=?
                       WHERE task_id=? AND state IN ('queued','running','pausing')""",
                    (USER_CLEARED_STUCK_REASON, current, current, task_id),
                )
                if int(cursor.rowcount or 0):
                    cleared.append(task_id)
            if cleared:
                db.executemany(
                    "DELETE FROM app_state WHERE key=?",
                    [(f"{REMOTE_BUSY_STATE_KEY_PREFIX}{task_id}",) for task_id in cleared],
                )
        return cleared

    def _delete_task_details(self, db: sqlite3.Connection, task_ids: list[str]) -> None:
        """Delete rows directly coupled to the given task records.

        The declared FK cascades (remote_runs / task_metadata_v3) are not
        enforced on these connections, so the coupled rows go explicitly.
        Deliberately out of scope: output artifacts (they live outside this
        store), remote_events (append-only diagnostics log), and the retired
        budget history tables (task_budget_reservations /
        task_budget_settlements, kept as inert history).
        """
        placeholders = ",".join("?" for _ in task_ids)
        for table in (
            "remote_runs",
            "task_metadata_v3",
            "remote_run_attempts",
            "remote_token_leases",
            "remote_supervisor_leases",
        ):
            db.execute(f"DELETE FROM {table} WHERE task_id IN ({placeholders})", task_ids)

    def pause_task(self, task_id: str) -> dict[str, Any] | None:
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row["state"] in TERMINAL_STATES:
                return self._row(row) if row is not None else None
            state = "pausing" if row["state"] in {"running", "pausing"} else "paused"
            payload = dict(self._load(row["payload_json"]))
            payload[USER_PAUSE_INTENT_KEY] = True
            db.execute(
                """UPDATE tasks SET state=?,resume_requested=0,payload_json=?,updated_at=?
                   WHERE task_id=?""",
                (state, self._dump(payload), now, task_id),
            )
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._row(row)

    def resume_task(
        self,
        task_id: str,
        *,
        clear_global_pause: bool = False,
    ) -> dict[str, Any] | None:
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row["state"] in TERMINAL_STATES:
                return self._row(row) if row is not None else None
            payload = dict(self._load(row["payload_json"]))
            payload.pop(USER_PAUSE_INTENT_KEY, None)
            if clear_global_pause:
                db.execute(
                    """INSERT INTO app_state(key,value_json,updated_at) VALUES('global_paused','false',?)
                       ON CONFLICT(key) DO UPDATE SET value_json='false',updated_at=excluded.updated_at""",
                    (now,),
                )
            if row["state"] == "paused":
                db.execute(
                    """UPDATE tasks SET state='queued',resume_requested=0,error='',payload_json=?,updated_at=?
                       WHERE task_id=?""",
                    (self._dump(payload), now, task_id),
                )
            elif row["state"] == "pausing":
                db.execute(
                    """UPDATE tasks SET resume_requested=1,error='',payload_json=?,updated_at=?
                       WHERE task_id=?""",
                    (self._dump(payload), now, task_id),
                )
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._row(row)

    def acknowledge_pause(self, task_id: str) -> dict[str, Any] | None:
        """Finalize a worker pause, or requeue it when resume won the race."""
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None or row["state"] in TERMINAL_STATES:
                return self._row(row) if row is not None else None
            state_row = db.execute("SELECT value_json FROM app_state WHERE key='global_paused'").fetchone()
            global_paused = bool(self._load(state_row[0])) if state_row else False
            should_resume = bool(row["resume_requested"]) and not global_paused
            db.execute(
                """UPDATE tasks SET state=?,resume_requested=0,error='',updated_at=?
                   WHERE task_id=?""",
                ("queued" if should_resume else "paused", now, task_id),
            )
            row = db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return self._row(row)

    def pause_all(self) -> list[str]:
        now = time.time()
        with self._lock, self._connect() as db:
            rows = db.execute(
                """SELECT task_id,state,resume_requested FROM tasks
                   WHERE state IN ('queued','running','pausing','paused')"""
            ).fetchall()
            affected: list[str] = []
            for row in rows:
                state = "pausing" if row["state"] in {"running", "pausing"} else "paused"
                if state != row["state"] or bool(row["resume_requested"]):
                    affected.append(str(row["task_id"]))
                db.execute(
                    "UPDATE tasks SET state=?,resume_requested=0,updated_at=? WHERE task_id=?",
                    (state, now, row["task_id"]),
                )
            db.execute(
                """INSERT INTO app_state(key,value_json,updated_at) VALUES('global_paused','true',?)
                   ON CONFLICT(key) DO UPDATE SET value_json='true',updated_at=excluded.updated_at""", (now,)
            )
        return affected

    def resume_all(self) -> list[str]:
        now = time.time()
        with self._lock, self._connect() as db:
            rows = db.execute(
                """SELECT task_id,state,resume_requested FROM tasks
                   WHERE state IN ('paused','pausing')"""
            ).fetchall()
            affected = [
                str(row["task_id"])
                for row in rows
                if row["state"] == "paused" or not bool(row["resume_requested"])
            ]
            db.execute(
                """UPDATE tasks SET state='queued',resume_requested=0,error='',updated_at=?
                   WHERE state='paused'""",
                (now,),
            )
            db.execute(
                """UPDATE tasks SET resume_requested=1,error='',updated_at=?
                   WHERE state='pausing'""",
                (now,),
            )
            db.execute(
                """INSERT INTO app_state(key,value_json,updated_at) VALUES('global_paused','false',?)
                   ON CONFLICT(key) DO UPDATE SET value_json='false',updated_at=excluded.updated_at""", (now,)
            )
        return affected

    def count(self, kind: str | None = None, states: Iterable[str] = ACTIVE_STATES) -> int:
        values = tuple(states)
        clauses = [f"state IN ({','.join('?' for _ in values)})"]
        args: list[Any] = list(values)
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        with self._lock, self._connect() as db:
            return int(db.execute(f"SELECT COUNT(*) FROM tasks WHERE {' AND '.join(clauses)}", args).fetchone()[0])

    def count_tasks_created_since(self, kind: str, since: float, config_key_prefix: str = "") -> int:
        """当日新建任务计数（N5A-P4 日预算的本地计数面，只读）。

        config_key_prefix 非空时按前缀过滤（P3 H1：palette: 前缀=深度回答）；
        缺省空串=旧行为逐字。"""
        if config_key_prefix:
            with self._lock, self._connect() as db:
                row = db.execute(
                    "SELECT COUNT(*) FROM tasks WHERE kind=? AND created_at>=? AND config_key LIKE ?",
                    (str(kind), float(since), f"{str(config_key_prefix)}%"),
                ).fetchone()
            return int(row[0] if row else 0)
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM tasks WHERE kind=? AND created_at>=?",
                (str(kind), float(since)),
            ).fetchone()
        return int(row[0] if row else 0)

    def add_estimate_sample(self, profile_key: str, unit_cost: float, units: float, elapsed: float,
                            *, device_fingerprint: dict[str, Any] | None = None) -> None:
        if unit_cost <= 0 or units <= 0 or elapsed <= 0:
            return
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO estimate_samples(profile_key,unit_cost,units,elapsed_seconds,created_at) VALUES(?,?,?,?,?)",
                       (profile_key, float(unit_cost), float(units), float(elapsed), time.time()))
            db.execute(
                """DELETE FROM estimate_samples WHERE profile_key=? AND sample_id NOT IN
                   (SELECT sample_id FROM estimate_samples WHERE profile_key=? ORDER BY sample_id DESC LIMIT 20)""",
                (profile_key, profile_key),
            )
            rows = db.execute(
                "SELECT unit_cost FROM estimate_samples WHERE profile_key=? ORDER BY sample_id DESC LIMIT 20",
                (profile_key,),
            ).fetchall()
            costs = [float(row[0]) for row in rows]
            median = statistics.median(costs)
            mad = statistics.median(abs(value - median) for value in costs) if costs else 0.0
            db.execute(
                """INSERT INTO estimate_profiles(profile_key,sample_count,median_unit_cost,mad_unit_cost,
                   device_fingerprint_json,updated_at) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(profile_key) DO UPDATE SET sample_count=excluded.sample_count,
                   median_unit_cost=excluded.median_unit_cost,mad_unit_cost=excluded.mad_unit_cost,
                   device_fingerprint_json=excluded.device_fingerprint_json,updated_at=excluded.updated_at""",
                (profile_key, len(costs), median, mad, self._dump(device_fingerprint or {}), time.time()),
            )

    def estimate_samples(self, profile_key: str, limit: int = 20) -> list[float]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT unit_cost FROM estimate_samples WHERE profile_key=? ORDER BY sample_id DESC LIMIT ?",
                              (profile_key, max(1, int(limit)))).fetchall()
        return [float(row[0]) for row in rows]

    def record_queue_sample(self, profile_keys: Iterable[str], queued_seconds: float) -> bool:
        """Store one trusted queue observation under each hierarchical key.

        Reuses the existing estimate-history mechanism (20-sample cap and
        median/MAD profile summary per key).  Non-finite, non-positive, or
        over-bound waits are rejected: a corrupt sample must never enter the
        history that later forecasts are named from.
        """
        try:
            value = float(queued_seconds)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value) or value <= 0 or value > QUEUE_SAMPLE_MAX_SECONDS:
            return False
        keys = [str(key) for key in dict.fromkeys(profile_keys) if str(key)]
        if not keys:
            return False
        for key in keys:
            self.add_estimate_sample(key, value, 1.0, value)
        return True

    def _fresh_queue_samples(self, profile_key: str, *, now: float) -> list[float]:
        """Queue sample values for one key inside the age cap (newest first)."""
        cutoff = float(now) - QUEUE_SAMPLE_MAX_AGE_SECONDS
        with self._lock, self._connect() as db:
            rows = db.execute(
                """SELECT unit_cost, created_at FROM estimate_samples
                    WHERE profile_key=? ORDER BY sample_id DESC LIMIT 20""",
                (profile_key,),
            ).fetchall()
        return [
            float(row[0]) for row in rows
            if float(row[1] or 0.0) >= cutoff and float(row[0]) > 0
        ]

    def queue_forecast(self, profile_keys: Iterable[str], *, now: float | None = None) -> dict[str, Any] | None:
        """Best available queue forecast walking the hierarchy most-specific first.

        Publishing below the sample floor is forbidden, so a sparse exact
        bucket falls through to the runner-class bucket, then Worker-wide
        history, then ``None`` (no queue prediction).  ``level`` names which
        bucket answered without leaking the raw profile key.
        """
        from .progress import QUEUE_SAMPLE_FLOOR, queue_forecast_from_samples

        moment = float(now if now is not None else time.time())
        levels = ("exact", "runner", "worker")
        for index, key in enumerate(profile_keys):
            samples = self._fresh_queue_samples(str(key), now=moment)
            if len(samples) < QUEUE_SAMPLE_FLOOR:
                continue
            forecast = queue_forecast_from_samples(samples)
            if forecast is None:
                continue
            return {**forecast, "level": levels[min(index, len(levels) - 1)]}
        return None

    def queue_forecast_for_workflow(self, workflow: str, *, pipeline: str = "", now: float | None = None) -> dict[str, Any] | None:
        return self.queue_forecast(
            queue_profile_keys(workflow, pipeline=str(pipeline or "")), now=now
        )

    # --- course-data inventory aggregates (read-only, append-only) ----------

    def course_task_aggregates(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Read-only per-lecture task counts for the course-data inventory.

        One GROUP BY over identity/timestamp columns only; payload, progress,
        estimate, and checkpoint JSON are never read.  ``kind`` is carried as a
        read-only identity column so consumers can route cross-course records
        (comma-joined course ids); entries mixing several kinds report
        ``kind: ""`` rather than inventing one.
        """
        with self._lock, self._connect() as db:
            rows = db.execute(
                """SELECT course_id,sub_id,kind,COUNT(*) AS count,MAX(updated_at) AS last_updated_at
                   FROM tasks GROUP BY course_id,sub_id,kind"""
            ).fetchall()
        aggregates: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row["course_id"]), str(row["sub_id"]))
            entry = aggregates.get(key)
            if entry is None:
                aggregates[key] = {
                    "kind": str(row["kind"] or ""),
                    "count": int(row["count"] or 0),
                    "last_updated_at": float(row["last_updated_at"] or 0.0),
                }
                continue
            entry["count"] += int(row["count"] or 0)
            entry["last_updated_at"] = max(
                entry["last_updated_at"], float(row["last_updated_at"] or 0.0)
            )
            if entry["kind"] != str(row["kind"] or ""):
                entry["kind"] = ""
        return aggregates

    def automation_course_rule_counts(self) -> dict[str, dict[str, Any]]:
        """Read-only per-course automation rule counts for the inventory."""
        with self._lock, self._connect() as db:
            rows = db.execute(
                """SELECT course_id,COUNT(*) AS count,MAX(updated_at) AS last_updated_at
                   FROM automation_course_rules GROUP BY course_id"""
            ).fetchall()
        return {
            str(row["course_id"]): {
                "count": int(row["count"] or 0),
                "last_updated_at": float(row["last_updated_at"] or 0.0),
            }
            for row in rows
        }

    def _row(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["resume_requested"] = bool(result.get("resume_requested", 0))
        for field in ("payload", "progress", "estimate", "checkpoint"):
            result[field] = self._load(result.pop(f"{field}_json", "{}"))
        return result
