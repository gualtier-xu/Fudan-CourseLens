"""Shared SQLite connection policy for the concurrent learning database."""

from __future__ import annotations

import sqlite3
from pathlib import Path


SQLITE_BUSY_TIMEOUT_MS = 30_000


def connect_learning_db(path: str | Path) -> sqlite3.Connection:
    """Open a short-lived connection that waits for other client workers."""
    db = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    db.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    return db


__all__ = ["SQLITE_BUSY_TIMEOUT_MS", "connect_learning_db"]
