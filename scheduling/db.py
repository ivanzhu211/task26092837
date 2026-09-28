"""SQLite persistence (single file, stdlib only)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS series (
        id TEXT PRIMARY KEY,
        timezone TEXT NOT NULL,
        first_local_start TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL,
        period TEXT NOT NULL,
        count INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS occurrences (
        id TEXT PRIMARY KEY,
        series_id TEXT NOT NULL REFERENCES series(id),
        seq INTEGER NOT NULL,
        start_utc TEXT NOT NULL,
        end_utc TEXT NOT NULL,
        status TEXT NOT NULL,
        exception_start_utc TEXT,
        exception_end_utc TEXT,
        UNIQUE (series_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS registrations (
        occurrence_id TEXT NOT NULL REFERENCES occurrences(id),
        email TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (occurrence_id, email)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_occurrences_series_seq "
    "ON occurrences(series_id, seq)",
]


def connect(db_path: str) -> sqlite3.Connection:
    """Open a SQLite file (``:memory:`` allowed) with sane defaults."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema if it does not yet exist."""
    cursor = conn.cursor()
    try:
        for statement in SCHEMA_STATEMENTS:
            cursor.execute(statement)
        conn.commit()
    finally:
        cursor.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Cursor]:
    """Context manager that commits on success and rolls back on failure."""
    cursor = conn.cursor()
    try:
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
