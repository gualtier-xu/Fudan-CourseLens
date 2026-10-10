"""Adapters that run the existing Store/Schema migrations transactionally."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from src.runtime.catalog_repository import CatalogRepository
from src.runtime.learning_schema import initialize_learning_schema
from src.runtime.local_data_recovery import MigrationResult, migrate_local_data
from src.runtime.task_store import TaskStore


STATE_REQUIRED_TABLES = ("tasks", "app_state", "catalog_courses", "catalog_lectures")
LEARNING_REQUIRED_TABLES = (
    "learning_schema_meta",
    "watch_progress",
    "ai_artifacts",
    "learning_documents",
)


class _TransactionalConnection:
    """Keep legacy schema code inside the recovery transaction.

    sqlite3 ``executescript`` commits an existing transaction. This adapter
    executes complete statements one at a time and suppresses nested
    commit/close calls while delegating every query to the owned connection.
    """

    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

    def execute(self, *args, **kwargs):
        return self._db.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._db.executemany(*args, **kwargs)

    def executescript(self, script: str):
        statement = ""
        cursor = None
        for line in str(script).splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                if statement.strip():
                    cursor = self._db.execute(statement)
                statement = ""
        if statement.strip():
            cursor = self._db.execute(statement)
        return cursor

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _state_migration(db: sqlite3.Connection) -> None:
    controlled = _TransactionalConnection(db)

    class MigratingTaskStore(TaskStore):
        @contextmanager
        def _connect(self):
            yield controlled

    class MigratingCatalogRepository(CatalogRepository):
        def _connect(self):
            return controlled

    MigratingTaskStore(Path("."))
    MigratingCatalogRepository(Path("."))


def _learning_migration(db: sqlite3.Connection) -> None:
    initialize_learning_schema(_TransactionalConnection(db))


def coordinate_local_data(data_root: str | Path, *, fault=None) -> MigrationResult:
    """Recover, migrate, and validate both databases before any store opens."""
    return migrate_local_data(
        data_root,
        state_migration=_state_migration,
        learning_migration=_learning_migration,
        required_tables={
            "state.db": STATE_REQUIRED_TABLES,
            "learning.db": LEARNING_REQUIRED_TABLES,
        },
        fault=fault,
    )


__all__ = [
    "LEARNING_REQUIRED_TABLES",
    "STATE_REQUIRED_TABLES",
    "coordinate_local_data",
]
