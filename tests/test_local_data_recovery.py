from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import time
from pathlib import Path

import pytest

from src.runtime import local_data_recovery as recovery


def _create_legacy_databases(root: Path, *, wal: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    state = sqlite3.connect(root / "state.db")
    learning = sqlite3.connect(root / "learning.db")
    if wal:
        state.execute("PRAGMA journal_mode=WAL")
        learning.execute("PRAGMA journal_mode=WAL")
    state.executescript(
        """
        CREATE TABLE tasks(task_id TEXT PRIMARY KEY, state TEXT NOT NULL);
        CREATE TABLE timetable_preferences(
            identity_scope TEXT PRIMARY KEY,
            selected_semester_id TEXT NOT NULL
        );
        CREATE TABLE settings(name TEXT PRIMARY KEY, value_json TEXT NOT NULL);
        INSERT INTO tasks VALUES('task-1','paused');
        INSERT INTO timetable_preferences VALUES('identity-hash','2025-2026-2');
        INSERT INTO settings VALUES('theme','"dark"');
        """
    )
    learning.executescript(
        """
        CREATE TABLE watch_progress(
            sub_id TEXT PRIMARY KEY,
            course_id TEXT NOT NULL,
            position_ms INTEGER NOT NULL
        );
        CREATE TABLE search_documents(
            doc_key TEXT PRIMARY KEY,
            sub_id TEXT NOT NULL,
            display_text TEXT NOT NULL
        );
        INSERT INTO watch_progress VALUES('lecture-1','course-1',42000);
        INSERT INTO search_documents VALUES('doc-1','lecture-1','private body');
        """
    )
    state.commit()
    learning.commit()
    state.close()
    learning.close()


def _state_migration(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE tasks ADD COLUMN resume_requested INTEGER NOT NULL DEFAULT 0")
    db.execute("CREATE INDEX idx_tasks_state ON tasks(state)")


def _learning_migration(db: sqlite3.Connection) -> None:
    db.execute("ALTER TABLE watch_progress ADD COLUMN completed INTEGER NOT NULL DEFAULT 0")
    db.execute("CREATE INDEX idx_progress_course ON watch_progress(course_id)")


def _migrate(root: Path, **kwargs: object) -> recovery.MigrationResult:
    return recovery.migrate_local_data(
        root,
        state_migration=_state_migration,
        learning_migration=_learning_migration,
        required_tables={
            "state.db": ("tasks", "timetable_preferences", "settings"),
            "learning.db": ("watch_progress", "search_documents"),
        },
        **kwargs,
    )


def _count(path: Path, table: str) -> int:
    with sqlite3.connect(path) as db:
        return int(db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _columns(path: Path, table: str) -> set[str]:
    with sqlite3.connect(path) as db:
        return {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}


def test_legacy_upgrade_preserves_critical_records_and_redacts_manifest(tmp_path: Path) -> None:
    _create_legacy_databases(tmp_path, wal=True)

    result = _migrate(tmp_path)

    assert result.status == "completed"
    assert _count(tmp_path / "state.db", "tasks") == 1
    assert _count(tmp_path / "state.db", "timetable_preferences") == 1
    assert _count(tmp_path / "state.db", "settings") == 1
    assert _count(tmp_path / "learning.db", "watch_progress") == 1
    assert _count(tmp_path / "learning.db", "search_documents") == 1
    assert "resume_requested" in _columns(tmp_path / "state.db", "tasks")
    assert "completed" in _columns(tmp_path / "learning.db", "watch_progress")
    manifest_text = result.manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["status"] == "completed"
    assert manifest["target_schema_version"] == recovery.CURRENT_SCHEMA_VERSION
    for secret in (
        "task-1",
        "identity-hash",
        "2025-2026-2",
        "course-1",
        "lecture-1",
        "private body",
        "dark",
        "cookie",
        "https://",
    ):
        assert secret not in manifest_text
    assert not list(result.backup_directory.glob("*-wal"))
    assert not list(result.backup_directory.glob("*-shm"))


@pytest.mark.parametrize(
    "boundary",
    [
        "backup.before.state.db",
        "backup.after.state.db",
        "backup.before.learning.db",
        "backup.after.learning.db",
        "manifest.backup_complete",
        "migration.begin.state.db",
        "migration.callback.state.db",
        "migration.schema_version.state.db",
        "migration.precommit.state.db",
        "migration.commit.state.db",
        "migration.validated.state.db",
        "migration.begin.learning.db",
        "migration.callback.learning.db",
        "migration.schema_version.learning.db",
        "migration.precommit.learning.db",
        "migration.commit.learning.db",
        "migration.validated.learning.db",
        "manifest.before_success",
        "manifest.after_success",
    ],
)
def test_fault_boundaries_restore_both_databases(tmp_path: Path, boundary: str) -> None:
    _create_legacy_databases(tmp_path, wal=True)
    originals = {
        name: (tmp_path / name).read_bytes() for name in recovery.DATABASE_NAMES
    }

    def fail(current: str) -> None:
        if current == boundary:
            raise RuntimeError("injected")

    with pytest.raises(recovery.LocalDataRecoveryError):
        _migrate(tmp_path, fault=fail)

    for name in recovery.DATABASE_NAMES:
        with sqlite3.connect(tmp_path / name) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # SQLite backup images can differ byte-for-byte while preserving rows.
        assert (tmp_path / name).stat().st_size > 0
    assert _count(tmp_path / "state.db", "tasks") == 1
    assert _count(tmp_path / "learning.db", "watch_progress") == 1
    assert "resume_requested" not in _columns(tmp_path / "state.db", "tasks")
    assert "completed" not in _columns(tmp_path / "learning.db", "watch_progress")
    assert all(originals.values())


def test_one_database_failure_never_claims_half_upgrade(tmp_path: Path) -> None:
    _create_legacy_databases(tmp_path)

    def fail_learning(_db: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("do not expose SQL")

    with pytest.raises(recovery.LocalDataRecoveryError) as caught:
        recovery.migrate_local_data(
            tmp_path,
            state_migration=_state_migration,
            learning_migration=fail_learning,
        )

    assert caught.value.code == recovery.ERROR_MIGRATION_FAILED
    manifests = list((tmp_path / recovery.BACKUP_DIRECTORY).glob("*/manifest.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text(encoding="utf-8"))["status"] == "restored"
    assert "resume_requested" not in _columns(tmp_path / "state.db", "tasks")


def test_unknown_newer_version_fails_before_backup_or_callback(tmp_path: Path) -> None:
    _create_legacy_databases(tmp_path)
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("CREATE TABLE local_data_schema_meta(key TEXT PRIMARY KEY,value TEXT)")
        db.execute(
            "INSERT INTO local_data_schema_meta VALUES('schema_version',?)",
            (str(recovery.CURRENT_SCHEMA_VERSION + 1),),
        )
    called = False

    def callback(_db: sqlite3.Connection) -> None:
        nonlocal called
        called = True

    with pytest.raises(recovery.LocalDataRecoveryError) as caught:
        recovery.migrate_local_data(
            tmp_path, state_migration=callback, learning_migration=callback
        )

    assert caught.value.code == recovery.ERROR_VERSION_TOO_NEW
    assert not called
    assert not list((tmp_path / recovery.BACKUP_DIRECTORY).glob("*/state.db"))


def test_corrupt_source_refuses_to_continue_writing(tmp_path: Path) -> None:
    _create_legacy_databases(tmp_path)
    (tmp_path / "learning.db").write_bytes(b"not a sqlite database")
    called = False

    def callback(_db: sqlite3.Connection) -> None:
        nonlocal called
        called = True

    with pytest.raises(recovery.LocalDataRecoveryError) as caught:
        recovery.migrate_local_data(
            tmp_path, state_migration=callback, learning_migration=callback
        )

    assert caught.value.code == recovery.ERROR_INTEGRITY
    assert not called


def test_corrupt_backup_refuses_explicit_restore(tmp_path: Path) -> None:
    _create_legacy_databases(tmp_path)
    result = _migrate(tmp_path)
    (result.backup_directory / "state.db").write_bytes(b"changed")

    with pytest.raises(recovery.LocalDataRecoveryError) as caught:
        recovery.restore_backup_set(tmp_path, result.backup_directory)

    assert caught.value.code == recovery.ERROR_BACKUP_UNAVAILABLE


def test_restore_is_idempotent_and_removes_wal_shm_residue(tmp_path: Path) -> None:
    _create_legacy_databases(tmp_path)
    result = _migrate(tmp_path)
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"state.db{suffix}").write_bytes(b"residue")

    recovery.restore_backup_set(tmp_path, result.backup_directory)
    recovery.restore_backup_set(tmp_path, result.backup_directory)

    assert not (tmp_path / "state.db-wal").exists()
    assert not (tmp_path / "state.db-shm").exists()
    assert "resume_requested" not in _columns(tmp_path / "state.db", "tasks")


def test_repeated_start_recovers_incomplete_migration_first(tmp_path: Path) -> None:
    _create_legacy_databases(tmp_path)
    result = _migrate(tmp_path)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "migrating"
    result.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    second = _migrate(tmp_path)

    assert second.status == "completed"
    assert _count(tmp_path / "state.db", "tasks") == 1
    assert _count(tmp_path / "learning.db", "watch_progress") == 1


def test_active_lock_fails_closed_and_stale_lock_is_recovered(tmp_path: Path) -> None:
    _create_legacy_databases(tmp_path)
    lock = tmp_path / recovery.LOCK_NAME
    lock.write_text("active", encoding="ascii")

    with pytest.raises(recovery.LocalDataRecoveryError) as caught:
        _migrate(tmp_path)
    assert caught.value.code == recovery.ERROR_BUSY

    stale = time.time() - recovery.STALE_LOCK_SECONDS - 10
    os.utime(lock, (stale, stale))
    assert _migrate(tmp_path).status == "completed"


def test_locked_windows_replace_returns_recovery_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_legacy_databases(tmp_path)
    original_replace = recovery._replace
    failed_migration = False

    def callback(_db: sqlite3.Connection) -> None:
        nonlocal failed_migration
        failed_migration = True
        raise RuntimeError("migration crash")

    def locked_replace(source: Path, target: Path) -> None:
        if failed_migration and ".restore." in source.name:
            raise PermissionError("locked")
        original_replace(source, target)

    monkeypatch.setattr(recovery, "_replace", locked_replace)
    with pytest.raises(recovery.LocalDataRecoveryError) as caught:
        recovery.migrate_local_data(
            tmp_path,
            state_migration=callback,
            learning_migration=_learning_migration,
        )

    assert caught.value.code == recovery.ERROR_RECOVERY_REQUIRED
    assert "PermissionError" not in str(caught.value)


def test_required_table_and_count_invariants_trigger_restore(tmp_path: Path) -> None:
    _create_legacy_databases(tmp_path)

    def destructive(db: sqlite3.Connection) -> None:
        db.execute("DELETE FROM tasks")

    with pytest.raises(recovery.LocalDataRecoveryError) as caught:
        recovery.migrate_local_data(
            tmp_path,
            state_migration=destructive,
            learning_migration=_learning_migration,
            required_tables={
                "state.db": ("tasks", "settings"),
                "learning.db": ("watch_progress",),
            },
        )

    assert caught.value.code == recovery.ERROR_INVARIANT
    assert _count(tmp_path / "state.db", "tasks") == 1


def test_backup_retention_keeps_three_sets(tmp_path: Path) -> None:
    _create_legacy_databases(tmp_path)
    for _ in range(recovery.MAX_BACKUP_SETS + 2):
        _migrate(tmp_path)

    manifests = list((tmp_path / recovery.BACKUP_DIRECTORY).glob("*/manifest.json"))
    assert len(manifests) == recovery.MAX_BACKUP_SETS


def test_size_limit_fails_before_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _create_legacy_databases(tmp_path)
    monkeypatch.setattr(recovery, "MAX_DATABASE_BYTES", 1)

    with pytest.raises(recovery.LocalDataRecoveryError) as caught:
        _migrate(tmp_path)

    assert caught.value.code == recovery.ERROR_DATABASE_TOO_LARGE


@pytest.mark.parametrize("operation", recovery.MIGRATION_OPERATIONS)
@pytest.mark.parametrize("phase", ("before", "after"))
def test_structural_operation_fault_boundaries_roll_back(
    tmp_path: Path, operation: str, phase: str
) -> None:
    _create_legacy_databases(tmp_path)

    def structural(db: sqlite3.Connection) -> None:
        def inject(boundary: str) -> None:
            if boundary == f"step.{phase}.{operation}":
                raise RuntimeError("injected")

        recovery.migration_step(
            operation,
            lambda: db.execute("CREATE TABLE migration_probe(value INTEGER)"),
            fault=inject,
        )

    with pytest.raises(recovery.LocalDataRecoveryError):
        recovery.migrate_local_data(
            tmp_path,
            state_migration=structural,
            learning_migration=_learning_migration,
        )

    with sqlite3.connect(tmp_path / "state.db") as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_probe'"
        ).fetchone()
    assert exists is None


def _seed_completed_backup(backup_root: Path, set_id: str, *, age_seconds: float) -> Path:
    directory = backup_root / set_id
    directory.mkdir(parents=True)
    manifest = {
        "schema": recovery.PROTOCOL_SCHEMA,
        "set_id": set_id,
        "status": "completed",
        "created_at": int(time.time() - age_seconds),
        "databases": {},
    }
    (directory / recovery.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(directory, (stamp, stamp))
    return directory


def test_prune_failure_after_file_removal_keeps_migration_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_legacy_databases(tmp_path)
    backup_root = tmp_path / recovery.BACKUP_DIRECTORY
    oldest = _seed_completed_backup(
        backup_root, "20250101T000000Z-aaaaaaaaaaaa", age_seconds=40 * 86400
    )
    _seed_completed_backup(backup_root, "20250102T000000Z-bbbbbbbbbbbb", age_seconds=30 * 86400)
    _seed_completed_backup(backup_root, "20250103T000000Z-cccccccccccc", age_seconds=20 * 86400)
    real_rmtree = shutil.rmtree
    sleep_calls: list[float] = []

    def locked_rmtree(path: object, *args: object, **kwargs: object) -> None:
        if Path(path) == oldest:  # type: ignore[arg-type]
            for child in oldest.iterdir():
                child.unlink()
            raise PermissionError("on-close scan handle")
        real_rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    restore_calls: list[tuple[object, ...]] = []

    def forbidden_restore(*args: object, **kwargs: object) -> None:
        restore_calls.append(args)
        raise AssertionError("restore must not run after a successful migration")

    monkeypatch.setattr(recovery.shutil, "rmtree", locked_rmtree)
    monkeypatch.setattr(recovery.time, "sleep", sleep_calls.append)
    monkeypatch.setattr(recovery, "restore_backup_set", forbidden_restore)

    result = _migrate(tmp_path)

    assert result.status == "completed"
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert "restored_at" not in manifest
    assert restore_calls == []
    for name in recovery.DATABASE_NAMES:
        with sqlite3.connect(tmp_path / name) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert _count(tmp_path / "state.db", "tasks") == 1
    assert _count(tmp_path / "learning.db", "watch_progress") == 1
    assert "resume_requested" in _columns(tmp_path / "state.db", "tasks")
    assert "completed" in _columns(tmp_path / "learning.db", "watch_progress")
    # The victim keeps only its empty directory; the retry is bounded.
    assert oldest.is_dir() and list(oldest.iterdir()) == []
    assert sleep_calls == [
        recovery.PRUNE_RETRY_SECONDS * attempt
        for attempt in range(1, recovery.PRUNE_ATTEMPTS)
    ]

    second = _migrate(tmp_path)

    assert second.status == "completed"
    assert not oldest.exists()
    assert len(list(backup_root.glob("*/manifest.json"))) == recovery.MAX_BACKUP_SETS


def test_transient_prune_lock_leaves_set_for_a_later_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _create_legacy_databases(tmp_path)
    backup_root = tmp_path / recovery.BACKUP_DIRECTORY
    oldest = _seed_completed_backup(
        backup_root, "20250101T000000Z-aaaaaaaaaaaa", age_seconds=40 * 86400
    )
    _seed_completed_backup(backup_root, "20250102T000000Z-bbbbbbbbbbbb", age_seconds=30 * 86400)
    _seed_completed_backup(backup_root, "20250103T000000Z-cccccccccccc", age_seconds=20 * 86400)
    real_rmtree = shutil.rmtree

    def locked_rmtree(path: object, *args: object, **kwargs: object) -> None:
        if Path(path) == oldest:  # type: ignore[arg-type]
            raise PermissionError("locked")
        real_rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(recovery.shutil, "rmtree", locked_rmtree)
    monkeypatch.setattr(recovery.time, "sleep", lambda _seconds: None)

    result = _migrate(tmp_path)

    assert result.status == "completed"
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert (oldest / recovery.MANIFEST_NAME).is_file()

    monkeypatch.setattr(recovery.shutil, "rmtree", real_rmtree)
    second = _migrate(tmp_path)

    assert second.status == "completed"
    assert not oldest.exists()
    assert len(list(backup_root.glob("*/manifest.json"))) == recovery.MAX_BACKUP_SETS


def test_prune_backups_retries_bounded_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_root = tmp_path / recovery.BACKUP_DIRECTORY
    backup_root.mkdir()
    sets = [
        _seed_completed_backup(
            backup_root,
            f"2025010{index}T000000Z-aaaaaaaaaaaa",
            age_seconds=(4 - index) * 86400,
        )
        for index in range(1, 5)
    ]
    victim = sets[0]
    keep = sets[-1]
    real_rmtree = shutil.rmtree

    def locked_rmtree(path: object, *args: object, **kwargs: object) -> None:
        if Path(path) == victim:  # type: ignore[arg-type]
            raise PermissionError("locked")
        real_rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(recovery.shutil, "rmtree", locked_rmtree)
    monkeypatch.setattr(recovery.time, "sleep", lambda _seconds: None)

    recovery._prune_backups(backup_root, keep)

    assert (victim / recovery.MANIFEST_NAME).is_file()
    assert sets[1].is_dir() and sets[2].is_dir()

    monkeypatch.setattr(recovery.shutil, "rmtree", real_rmtree)
    recovery._prune_backups(backup_root, keep)

    assert not victim.exists()
    assert sets[1].is_dir() and sets[2].is_dir() and keep.is_dir()


def test_empty_manifest_less_residue_is_pruned_but_data_left_alone(tmp_path: Path) -> None:
    _create_legacy_databases(tmp_path)
    backup_root = tmp_path / recovery.BACKUP_DIRECTORY
    empty_residue = backup_root / "20250101T000000Z-aaaaaaaaaaaa"
    empty_residue.mkdir(parents=True)
    stranded = backup_root / "20250102T000000Z-bbbbbbbbbbbb"
    stranded.mkdir(parents=True)
    (stranded / "state.db").write_bytes(b"stranded bytes")

    result = _migrate(tmp_path)

    assert result.status == "completed"
    assert not empty_residue.exists()
    assert (stranded / "state.db").read_bytes() == b"stranded bytes"


def test_restrict_files_keep_owner_only_mode_and_directories_stay_writable(
    tmp_path: Path,
) -> None:
    """R12 回归钉（MAC-WIRE-2）：_restrict 收紧文件、放行目录穿越。

    conftest 的 POSIX-aware shim 已随产品修复退役——本钉在全部平台直跑
    产品码本体：文件保持 owner-only 语义且内容完好；目录收紧后仍可立即
    写入（POSIX 上缺 traverse 位曾让迁移引擎读不回自己的备份集）。
    """
    file_path = tmp_path / "state.db"
    file_path.write_bytes(b"payload")
    recovery._restrict(file_path)
    assert file_path.read_bytes() == b"payload"
    if os.name == "posix":
        assert stat.S_IMODE(file_path.stat().st_mode) == (
            stat.S_IRUSR | stat.S_IWUSR
        )

    backup_dir = tmp_path / "20260101T000000Z-regressionpin"
    backup_dir.mkdir()
    recovery._restrict(backup_dir)
    probe = backup_dir / "manifest.json"
    probe.write_text("{}", encoding="utf-8")
    assert probe.read_text(encoding="utf-8") == "{}"


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory mode semantics")
def test_restrict_directory_mode_is_owner_rwx_on_posix(tmp_path: Path) -> None:
    """R12 位形钉（macOS 绿门跑）：目录=0o700，文件=0o600。"""
    directory = tmp_path / "backup-set"
    directory.mkdir()
    recovery._restrict(directory)
    assert stat.S_IMODE(directory.stat().st_mode) == (
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
    )
