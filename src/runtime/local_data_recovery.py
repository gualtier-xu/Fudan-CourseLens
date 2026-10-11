"""Fail-closed backup, migration, validation, and restore for local SQLite data.

This module deliberately does not wire itself into application startup.  The
application lifecycle must first stop database users, then call
``migrate_local_data`` with the schema migration callbacks, and only start
services after a successful result.

The backup manifest contains database names, sizes, hashes, schema versions,
table names, and row counts only.  It must never contain row values, SQL
parameters, course titles, document text, URLs, cookies, or credentials.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence, TypeVar


PROTOCOL_SCHEMA = "courselens.local-data-recovery.v1"
SUPPORTED_SCHEMA_VERSIONS = (0, 1)
CURRENT_SCHEMA_VERSION = 1
MIGRATION_STEPS = (
    "0->1: establish protocol metadata after application schema callbacks",
)
DATABASE_NAMES = ("state.db", "learning.db")
BACKUP_DIRECTORY = "migration-backups"
MANIFEST_NAME = "manifest.json"
LOCK_NAME = ".local-data-migration.lock"
MAX_DATABASE_BYTES = 512 * 1024 * 1024
MAX_BACKUP_SETS = 3
STALE_LOCK_SECONDS = 15 * 60
REPLACE_ATTEMPTS = 8
REPLACE_RETRY_SECONDS = 0.05
PRUNE_ATTEMPTS = 4
PRUNE_RETRY_SECONDS = 0.05

ERROR_BUSY = "LOCAL_DATA_E_BUSY"
ERROR_DATABASE_TOO_LARGE = "LOCAL_DATA_E_SIZE_LIMIT"
ERROR_DISK_FULL = "LOCAL_DATA_E_DISK_FULL"
ERROR_INTEGRITY = "LOCAL_DATA_E_INTEGRITY"
ERROR_INVARIANT = "LOCAL_DATA_E_INVARIANT"
ERROR_VERSION_TOO_NEW = "LOCAL_DATA_E_VERSION_TOO_NEW"
ERROR_BACKUP_UNAVAILABLE = "LOCAL_DATA_E_BACKUP_UNAVAILABLE"
ERROR_MIGRATION_FAILED = "LOCAL_DATA_E_MIGRATION_FAILED"
ERROR_RESTORE_FAILED = "LOCAL_DATA_E_RESTORE_FAILED"
ERROR_RECOVERY_REQUIRED = "LOCAL_DATA_E_RECOVERY_REQUIRED"

FaultInjector = Callable[[str], None]
MigrationCallback = Callable[[sqlite3.Connection], None]
StepResult = TypeVar("StepResult")
MIGRATION_OPERATIONS = ("create_table", "create_index", "deduplicate", "delete", "rename")


@dataclass(frozen=True)
class MigrationResult:
    status: str
    backup_directory: Path
    manifest_path: Path
    schema_version: int


class LocalDataRecoveryError(RuntimeError):
    """Stable, redacted error suitable for a local support UI."""

    def __init__(self, code: str, instruction: str):
        self.code = code
        self.instruction = instruction
        super().__init__(f"{code}: {instruction}")


def _noop_fault(_boundary: str) -> None:
    return None


def migration_step(
    operation: str,
    action: Callable[[], StepResult],
    *,
    fault: FaultInjector | None = None,
) -> StepResult:
    """Run one schema operation with injectable before/after boundaries.

    Migration callbacks should wrap every destructive or structural operation
    with this helper. The surrounding database transaction remains responsible
    for rollback.
    """
    if operation not in MIGRATION_OPERATIONS:
        raise ValueError("unsupported migration operation")
    injector = fault or _noop_fault
    injector(f"step.before.{operation}")
    result = action()
    injector(f"step.after.{operation}")
    return result


def _instruction(backup_root: Path) -> str:
    return (
        "Close CourseLens and retry. If the error remains, keep the database "
        f"directory unchanged and use the newest validated backup under {backup_root}."
    )


def _version_too_new_instruction() -> str:
    """FAULT-MATRIX-1 F-VER-1: a downgrade must never be told to restore a backup.

    The generic instruction points at the newest validated backup — restoring
    it over a directory written by a NEWER CourseLens would destroy the
    student's newer data. The honest guidance for this closed set is to
    upgrade the app and touch nothing else.
    """
    return (
        "This data directory was last written by a newer version of "
        "CourseLens. Update CourseLens to the newest version to open it; "
        "your course data was left unchanged."
    )


def _disk_full_instruction() -> str:
    return (
        "The drive holding CourseLens data is out of free space. Free up space "
        "on that drive, then reopen CourseLens; existing course data was left "
        "unchanged."
    )


def _is_disk_full(exc: BaseException) -> bool:
    """Recognize ENOSPC/SQLITE_FULL without inventing causes for other errors."""
    if isinstance(exc, sqlite3.OperationalError):
        return "database or disk is full" in str(exc).lower()
    return isinstance(exc, OSError) and exc.errno == errno.ENOSPC


@contextmanager
def _disk_full_guard(backup_root: Path):
    """Convert a genuine disk-full OSError into the stable recovery code.

    FAULT-MATRIX-1 F-DISK-1: before this guard, an ENOSPC during startup
    bookkeeping (backup directory creation, migration lock file, first
    manifest write) escaped as a raw OSError, which surfaced to the student
    as a generic startup failure instead of the actionable recovery path.
    Non-ENOSPC errors keep propagating unchanged.
    """
    try:
        yield
    except OSError as exc:
        if _is_disk_full(exc):
            raise LocalDataRecoveryError(
                ERROR_DISK_FULL, _disk_full_instruction()
            ) from exc
        raise


def _fail(code: str, backup_root: Path) -> LocalDataRecoveryError:
    return LocalDataRecoveryError(code, _instruction(backup_root))


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        uri = path.resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True, timeout=30)
    else:
        db = sqlite3.connect(path, timeout=30)
    db.execute("PRAGMA busy_timeout=30000")
    return db


def _fsync_file(path: Path) -> None:
    # Windows' CRT rejects fsync on a read-only descriptor.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _restrict(path: Path) -> None:
    """Best-effort owner-only mode; inherited Windows ACL remains authoritative."""
    mode = stat.S_IRUSR | stat.S_IWUSR
    if path.is_dir():
        # POSIX directories need the owner traverse bit, or the very next
        # write/list inside the backup set fails with Errno 13 (R12: the
        # migration engine could never read back its own backup set on
        # macOS). Windows chmod ignores these bits, so behavior there is
        # bit-for-bit unchanged.
        mode |= stat.S_IXUSR
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise LocalDataRecoveryError(
            ERROR_BACKUP_UNAVAILABLE,
            "Could not restrict access to the local migration backup.",
        ) from exc


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        _restrict(temporary)
        _fsync_file(temporary)
        _replace(temporary, path)
        _restrict(path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace(source: Path, target: Path) -> None:
    last_error: OSError | None = None
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            last_error = exc
            if attempt + 1 < REPLACE_ATTEMPTS:
                time.sleep(REPLACE_RETRY_SECONDS * (attempt + 1))
    assert last_error is not None
    raise last_error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _schema_version(db: sqlite3.Connection) -> int:
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_data_schema_meta'"
    ).fetchone()
    if not exists:
        return 0
    row = db.execute(
        "SELECT value FROM local_data_schema_meta WHERE key='schema_version'"
    ).fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid schema version") from exc


def _write_schema_version(db: sqlite3.Connection, version: int) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS local_data_schema_meta "
        "(key TEXT PRIMARY KEY,value TEXT NOT NULL)"
    )
    db.execute(
        "INSERT INTO local_data_schema_meta(key,value) VALUES('schema_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(version),),
    )


def _tables_and_counts(db: sqlite3.Connection) -> dict[str, int]:
    tables = [
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        if not str(row[0]).startswith("search_fts_")
    ]
    result: dict[str, int] = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        result[table] = int(db.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
    return result


def _validate_database(
    path: Path,
    *,
    expected_counts: Mapping[str, int] | None = None,
    required_tables: Sequence[str] = (),
) -> dict[str, object]:
    try:
        with closing(_connect(path, read_only=True)) as db:
            quick = str(db.execute("PRAGMA quick_check").fetchone()[0])
            integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
            if quick != "ok" or integrity != "ok":
                raise ValueError("integrity")
            counts = _tables_and_counts(db)
            missing = sorted(set(required_tables).difference(counts))
            if missing:
                raise KeyError("required tables")
            if expected_counts is not None:
                for table, before in expected_counts.items():
                    if table in counts and counts[table] < int(before):
                        raise ArithmeticError("row count decreased")
            return {
                "schema_version": _schema_version(db),
                "tables": sorted(counts),
                "counts": counts,
                "quick_check": "ok",
                "integrity_check": "ok",
            }
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise LocalDataRecoveryError(
            ERROR_INTEGRITY,
            "A local database or backup failed SQLite integrity validation.",
        ) from exc
    except (KeyError, ArithmeticError) as exc:
        raise LocalDataRecoveryError(
            ERROR_INVARIANT,
            "A local database failed a required table or record-count invariant.",
        ) from exc


def _backup_database(source: Path, target: Path) -> dict[str, object]:
    if source.stat().st_size > MAX_DATABASE_BYTES:
        raise LocalDataRecoveryError(
            ERROR_DATABASE_TOO_LARGE,
            "A local database exceeds the supported migration backup size.",
        )
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with closing(_connect(source, read_only=True)) as source_db:
            quick = str(source_db.execute("PRAGMA quick_check").fetchone()[0])
            integrity = str(source_db.execute("PRAGMA integrity_check").fetchone()[0])
            if quick != "ok" or integrity != "ok":
                raise LocalDataRecoveryError(
                    ERROR_INTEGRITY,
                    "A local database failed pre-migration integrity validation.",
                )
            with closing(_connect(temporary)) as target_db:
                source_db.backup(target_db)
        _restrict(temporary)
        _fsync_file(temporary)
        _replace(temporary, target)
        _restrict(target)
        evidence = _validate_database(target)
        for suffix in ("-wal", "-shm"):
            target.with_name(target.name + suffix).unlink(missing_ok=True)
        return {
            "file": target.name,
            "bytes": target.stat().st_size,
            "sha256": _sha256(target),
            "schema_version": evidence["schema_version"],
            "tables": evidence["tables"],
            "counts": evidence["counts"],
            "quick_check": "ok",
            "integrity_check": "ok",
        }
    finally:
        temporary.unlink(missing_ok=True)


class _MigrationLock:
    def __init__(self, data_root: Path):
        self.path = data_root / LOCK_NAME
        self._owned = False

    def __enter__(self) -> "_MigrationLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    stat.S_IRUSR | stat.S_IWUSR,
                )
                with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                    handle.write(f"{os.getpid()} {time.time():.6f}\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self._owned = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0
                if age <= STALE_LOCK_SECONDS:
                    break
                self.path.unlink(missing_ok=True)
        raise LocalDataRecoveryError(
            ERROR_BUSY,
            "Another local data migration is active; close other CourseLens processes.",
        )

    def __exit__(self, *_args: object) -> None:
        if self._owned:
            self.path.unlink(missing_ok=True)


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalDataRecoveryError(
            ERROR_BACKUP_UNAVAILABLE,
            "A migration backup manifest is unreadable.",
        ) from exc
    if not isinstance(value, dict) or value.get("schema") != PROTOCOL_SCHEMA:
        raise LocalDataRecoveryError(
            ERROR_BACKUP_UNAVAILABLE,
            "A migration backup manifest is not supported.",
        )
    return value


def _validate_backup_set(backup_dir: Path, manifest: Mapping[str, object]) -> None:
    databases = manifest.get("databases")
    if not isinstance(databases, dict):
        raise LocalDataRecoveryError(
            ERROR_BACKUP_UNAVAILABLE, "A migration backup manifest is incomplete."
        )
    for name in DATABASE_NAMES:
        item = databases.get(name)
        if not isinstance(item, dict):
            raise LocalDataRecoveryError(
                ERROR_BACKUP_UNAVAILABLE, "A migration backup manifest is incomplete."
            )
        path = backup_dir / name
        if not path.is_file() or _sha256(path) != item.get("sha256"):
            raise LocalDataRecoveryError(
                ERROR_BACKUP_UNAVAILABLE, "A migration backup is missing or has changed."
            )
        _validate_database(path)
        for suffix in ("-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)


def _restore_database(backup: Path, target: Path, fault: FaultInjector) -> None:
    temporary = target.with_name(f".{target.name}.restore.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(backup, temporary)
        _restrict(temporary)
        _fsync_file(temporary)
        fault(f"restore.replace.{target.name}")
        _replace(temporary, target)
        for suffix in ("-wal", "-shm"):
            target.with_name(target.name + suffix).unlink(missing_ok=True)
        _validate_database(target)
    finally:
        temporary.unlink(missing_ok=True)


def restore_backup_set(
    data_root: str | Path,
    backup_dir: str | Path,
    *,
    fault: FaultInjector | None = None,
) -> None:
    """Idempotently restore both databases from one validated backup set."""
    root = Path(data_root)
    backup = Path(backup_dir)
    injector = fault or _noop_fault
    manifest_path = backup / MANIFEST_NAME
    manifest = _load_manifest(manifest_path)
    _validate_backup_set(backup, manifest)
    try:
        for name in DATABASE_NAMES:
            _restore_database(backup / name, root / name, injector)
        restored = dict(manifest)
        restored["status"] = "restored"
        restored["restored_at"] = int(time.time())
        _atomic_json(manifest_path, restored)
    except (OSError, sqlite3.Error, LocalDataRecoveryError) as exc:
        if isinstance(exc, LocalDataRecoveryError) and exc.code == ERROR_INTEGRITY:
            raise
        raise _fail(ERROR_RESTORE_FAILED, root / BACKUP_DIRECTORY) from exc


def _recover_incomplete(root: Path, backup_root: Path, fault: FaultInjector) -> None:
    if not backup_root.exists():
        return
    for directory in sorted(backup_root.iterdir()):
        manifest_path = directory / MANIFEST_NAME
        if not directory.is_dir() or not manifest_path.is_file():
            continue
        manifest = _load_manifest(manifest_path)
        if manifest.get("status") in {"backup_complete", "migrating", "restoring"}:
            restore_backup_set(root, directory, fault=fault)


def _retry_removal(remove: Callable[[], None]) -> None:
    """Run one deletion with a small bounded retry for transient file locks.

    Windows can keep an on-close handle on a just-read backup file (for
    example an antivirus scan), which makes the deletion fail after the
    directory's files are already gone.  Removal is housekeeping, so the
    bounded retry gives up silently and leaves any residue for a later run.
    """
    for attempt in range(PRUNE_ATTEMPTS):
        try:
            remove()
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt + 1 < PRUNE_ATTEMPTS:
                time.sleep(PRUNE_RETRY_SECONDS * (attempt + 1))


def _prune_manifest_less_residue(directory: Path) -> None:
    """Best-effort removal of an already-empty manifest-less backup directory.

    A pruning attempt whose files were removed but whose directory deletion
    failed leaves exactly this contentless residue; every scan path skips it.
    ``rmdir`` refuses non-empty directories, so data-bearing leftovers are
    never touched.
    """
    try:
        if next(directory.iterdir(), None) is not None:
            return
    except OSError:
        return
    _retry_removal(directory.rmdir)


def _prune_backups(backup_root: Path, keep: Path) -> None:
    completed: list[Path] = []
    for directory in backup_root.iterdir():
        if directory == keep or not directory.is_dir():
            continue
        manifest_path = directory / MANIFEST_NAME
        if not manifest_path.is_file():
            _prune_manifest_less_residue(directory)
            continue
        try:
            status = _load_manifest(manifest_path).get("status")
        except LocalDataRecoveryError:
            continue
        if status in {"completed", "restored"}:
            completed.append(directory)
    completed.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    for directory in completed[MAX_BACKUP_SETS - 1 :]:
        _retry_removal(lambda target=directory: shutil.rmtree(target))


def migrate_local_data(
    data_root: str | Path,
    *,
    state_migration: MigrationCallback,
    learning_migration: MigrationCallback,
    required_tables: Mapping[str, Sequence[str]] | None = None,
    fault: FaultInjector | None = None,
) -> MigrationResult:
    """Back up, migrate, validate, and atomically claim success for both DBs.

    Callbacks receive an open connection with an active ``BEGIN IMMEDIATE``
    transaction.  They must not commit, close the connection, or log row data.
    """
    root = Path(data_root)
    backup_root = root / BACKUP_DIRECTORY
    injector = fault or _noop_fault
    callbacks = {"state.db": state_migration, "learning.db": learning_migration}
    required = dict(required_tables or {})

    with _disk_full_guard(backup_root):
        root.mkdir(parents=True, exist_ok=True)
        backup_root.mkdir(parents=True, exist_ok=True)
        _restrict(backup_root)

        with _MigrationLock(root):
            _recover_incomplete(root, backup_root, injector)
            for name in DATABASE_NAMES:
                if not (root / name).is_file():
                    with closing(_connect(root / name)) as db:
                        db.commit()
                evidence = _validate_database(root / name)
                if int(evidence["schema_version"]) > CURRENT_SCHEMA_VERSION:
                    raise LocalDataRecoveryError(
                        ERROR_VERSION_TOO_NEW, _version_too_new_instruction()
                    )

            set_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:12]
            backup_dir = backup_root / set_id
            backup_dir.mkdir()
            _restrict(backup_dir)
            manifest_path = backup_dir / MANIFEST_NAME
            manifest: dict[str, object] = {
                "schema": PROTOCOL_SCHEMA,
                "set_id": set_id,
                "status": "creating",
                "created_at": int(time.time()),
                "target_schema_version": CURRENT_SCHEMA_VERSION,
                "migration_steps": list(MIGRATION_STEPS),
                "databases": {},
            }
            _atomic_json(manifest_path, manifest)

            try:
                database_evidence: dict[str, object] = {}
                for name in DATABASE_NAMES:
                    injector(f"backup.before.{name}")
                    database_evidence[name] = _backup_database(root / name, backup_dir / name)
                    injector(f"backup.after.{name}")
                manifest["databases"] = database_evidence
                manifest["status"] = "backup_complete"
                _atomic_json(manifest_path, manifest)
                _validate_backup_set(backup_dir, manifest)
                injector("manifest.backup_complete")

                before_counts = {
                    name: dict(database_evidence[name]["counts"])  # type: ignore[index]
                    for name in DATABASE_NAMES
                }
                manifest["status"] = "migrating"
                _atomic_json(manifest_path, manifest)

                for name in DATABASE_NAMES:
                    path = root / name
                    injector(f"migration.begin.{name}")
                    with closing(_connect(path)) as db:
                        version = _schema_version(db)
                        if version > CURRENT_SCHEMA_VERSION:
                            raise LocalDataRecoveryError(
                                ERROR_VERSION_TOO_NEW,
                                _version_too_new_instruction(),
                            )
                        if version < CURRENT_SCHEMA_VERSION:
                            db.execute("BEGIN IMMEDIATE")
                            try:
                                injector(f"migration.callback.{name}")
                                callbacks[name](db)
                                injector(f"migration.schema_version.{name}")
                                _write_schema_version(db, CURRENT_SCHEMA_VERSION)
                                injector(f"migration.precommit.{name}")
                                db.commit()
                                injector(f"migration.commit.{name}")
                            except BaseException:
                                db.rollback()
                                raise
                    _validate_database(
                        path,
                        expected_counts=before_counts[name],
                        required_tables=required.get(name, ()),
                    )
                    injector(f"migration.validated.{name}")

                # Revalidate both only after both callbacks have committed.  The
                # manifest is the cross-database success claim.
                for name in DATABASE_NAMES:
                    _validate_database(
                        root / name,
                        expected_counts=before_counts[name],
                        required_tables=required.get(name, ()),
                    )
                injector("manifest.before_success")
                manifest["status"] = "completed"
                manifest["completed_at"] = int(time.time())
                _atomic_json(manifest_path, manifest)
                injector("manifest.after_success")
                result = MigrationResult("completed", backup_dir, manifest_path, CURRENT_SCHEMA_VERSION)
            except BaseException as exc:
                try:
                    if manifest.get("databases"):
                        manifest["status"] = "restoring"
                        _atomic_json(manifest_path, manifest)
                        restore_backup_set(root, backup_dir, fault=injector)
                except BaseException as restore_exc:
                    raise _fail(ERROR_RECOVERY_REQUIRED, backup_root) from restore_exc
                if isinstance(exc, LocalDataRecoveryError):
                    raise
                if _is_disk_full(exc):
                    raise _fail(ERROR_DISK_FULL, backup_root) from exc
                raise _fail(ERROR_MIGRATION_FAILED, backup_root) from exc
            # The manifest above is the success claim.  Pruning is optional
            # housekeeping and must never convert a completed migration into an
            # error or a restore, so it runs outside the guarded block.
            try:
                _prune_backups(backup_root, backup_dir)
            except OSError:
                pass
            return result


__all__ = [
    "BACKUP_DIRECTORY",
    "CURRENT_SCHEMA_VERSION",
    "DATABASE_NAMES",
    "ERROR_BACKUP_UNAVAILABLE",
    "ERROR_BUSY",
    "ERROR_DATABASE_TOO_LARGE",
    "ERROR_DISK_FULL",
    "ERROR_INTEGRITY",
    "ERROR_INVARIANT",
    "ERROR_MIGRATION_FAILED",
    "ERROR_RECOVERY_REQUIRED",
    "ERROR_RESTORE_FAILED",
    "ERROR_VERSION_TOO_NEW",
    "LocalDataRecoveryError",
    "MAX_BACKUP_SETS",
    "MIGRATION_OPERATIONS",
    "MIGRATION_STEPS",
    "MigrationResult",
    "PROTOCOL_SCHEMA",
    "SUPPORTED_SCHEMA_VERSIONS",
    "migrate_local_data",
    "migration_step",
    "restore_backup_set",
]
