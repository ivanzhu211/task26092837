"""SQLite-backed recurring event scheduler.

All state lives in a single SQLite file. Standard library only.
Timestamps are stored as ISO-8601 UTC strings; timezones as IANA names.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from recurrence import MAX_OCCURRENCES, Rule, expand, resolve_local

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    id TEXT PRIMARY KEY,
    tz TEXT NOT NULL,
    first_local_start TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    freq TEXT NOT NULL,
    count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS occurrences (
    id TEXT PRIMARY KEY,
    series_id TEXT NOT NULL REFERENCES series(id),
    seq INTEGER NOT NULL,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',
    is_exception INTEGER NOT NULL DEFAULT 0,
    UNIQUE (series_id, seq)
);
CREATE TABLE IF NOT EXISTS registrations (
    occurrence_id TEXT NOT NULL REFERENCES occurrences(id),
    email TEXT NOT NULL,
    PRIMARY KEY (occurrence_id, email)
);
"""

CHECK_IN_WINDOW = timedelta(minutes=30)


def _to_utc_text(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _from_utc_text(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


class Scheduler:
    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Scheduler":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- queries ---------------------------------------------------------

    def _get_occurrence(self, occurrence_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM occurrences WHERE id = ?", (occurrence_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown occurrence id: {occurrence_id}")
        return row

    def _get_series(self, series_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM series WHERE id = ?", (series_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown series id: {series_id}")
        return row

    # -- operations ------------------------------------------------------

    def create_series(self, rule: Rule) -> str:
        """Expand a rule and persist the series plus all occurrences."""
        rule.validate()
        series_id = uuid.uuid4().hex
        slots = expand(rule)
        with self._conn:
            self._conn.execute(
                "INSERT INTO series (id, tz, first_local_start,"
                " duration_minutes, freq, count) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    series_id,
                    rule.tz,
                    rule.first_local_start.isoformat(),
                    rule.duration_minutes,
                    rule.freq,
                    rule.count,
                ),
            )
            self._conn.executemany(
                "INSERT INTO occurrences (id, series_id, seq, start_utc, end_utc)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (uuid.uuid4().hex, series_id, s.seq, _to_utc_text(s.start_utc),
                     _to_utc_text(s.end_utc))
                    for s in slots
                ],
            )
        return series_id

    def override(self, occurrence_id: str, new_local_start: Optional[datetime]) -> None:
        """Reschedule one occurrence (naive local time in the series tz).

        ``None`` cancels the occurrence. Either way it becomes an exception.
        """
        occ = self._get_occurrence(occurrence_id)
        series = self._get_series(occ["series_id"])
        with self._conn:
            if new_local_start is None:
                self._conn.execute(
                    "UPDATE occurrences SET status = 'cancelled', is_exception = 1"
                    " WHERE id = ?",
                    (occurrence_id,),
                )
            else:
                if new_local_start.tzinfo is not None:
                    raise ValueError("new_local_start must be naive local wall time")
                duration = _from_utc_text(occ["end_utc"]) - _from_utc_text(occ["start_utc"])
                start_utc = resolve_local(ZoneInfo(series["tz"]), new_local_start)
                self._conn.execute(
                    "UPDATE occurrences SET start_utc = ?, end_utc = ?,"
                    " is_exception = 1 WHERE id = ?",
                    (
                        _to_utc_text(start_utc),
                        _to_utc_text(start_utc.astimezone(timezone.utc) + duration),
                        occurrence_id,
                    ),
                )

    def edit_from(self, series_id: str, k: int, new_rule: Rule) -> None:
        """Re-plan occurrences with seq >= k according to ``new_rule``.

        Occurrences with seq < k are untouched. Exceptions keep their time
        and status but still occupy their sequence number. Ids (and their
        registrations) are preserved for non-exception occurrences that
        still exist. The only allowed rejection: shrinking would delete an
        occurrence that has registrations.
        """
        series = self._get_series(series_id)
        new_rule.validate()
        old = self._conn.execute(
            "SELECT * FROM occurrences WHERE series_id = ? ORDER BY seq",
            (series_id,),
        ).fetchall()
        old_by_seq = {row["seq"]: row for row in old}
        if not isinstance(k, int) or k not in old_by_seq:
            raise ValueError("k must be the sequence number of an existing occurrence")
        new_total = k - 1 + new_rule.count
        if new_total > MAX_OCCURRENCES:
            raise ValueError(f"series may not exceed {MAX_OCCURRENCES} occurrences")

        slots = expand(new_rule, first_seq=k)
        old_total = old[-1]["seq"]

        with self._conn:
            for slot in slots:
                existing = old_by_seq.get(slot.seq)
                if existing is None:
                    self._conn.execute(
                        "INSERT INTO occurrences (id, series_id, seq, start_utc,"
                        " end_utc) VALUES (?, ?, ?, ?, ?)",
                        (
                            uuid.uuid4().hex,
                            series_id,
                            slot.seq,
                            _to_utc_text(slot.start_utc),
                            _to_utc_text(slot.end_utc),
                        ),
                    )
                elif not existing["is_exception"]:
                    self._conn.execute(
                        "UPDATE occurrences SET start_utc = ?, end_utc = ?"
                        " WHERE id = ?",
                        (
                            _to_utc_text(slot.start_utc),
                            _to_utc_text(slot.end_utc),
                            existing["id"],
                        ),
                    )
                # exceptions keep their own time and status

            if new_total < old_total:
                doomed = [row for row in old if row["seq"] > new_total]
                registered = [
                    row["seq"]
                    for row in doomed
                    if self._conn.execute(
                        "SELECT 1 FROM registrations WHERE occurrence_id = ? LIMIT 1",
                        (row["id"],),
                    ).fetchone()
                ]
                if registered:
                    raise ValueError(
                        "cannot shorten series: occurrences with registrations"
                        f" would be deleted (seqs {registered})"
                    )
                self._conn.executemany(
                    "DELETE FROM occurrences WHERE id = ?",
                    [(row["id"],) for row in doomed],
                )

            self._conn.execute(
                "UPDATE series SET tz = ?, first_local_start = ?,"
                " duration_minutes = ?, freq = ?, count = ? WHERE id = ?",
                (
                    new_rule.tz,
                    new_rule.first_local_start.isoformat(),
                    new_rule.duration_minutes,
                    new_rule.freq,
                    new_rule.count,
                    series_id,
                ),
            )

    def register(self, occurrence_id: str, email: str) -> None:
        """Register an email for an occurrence (once per occurrence)."""
        occ = self._get_occurrence(occurrence_id)
        if occ["status"] == "cancelled":
            raise ValueError("cannot register for a cancelled occurrence")
        if not email or "@" not in email:
            raise ValueError("invalid email")
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO registrations (occurrence_id, email) VALUES (?, ?)",
                    (occurrence_id, email),
                )
        except sqlite3.IntegrityError:
            raise ValueError(
                f"{email} is already registered for occurrence {occurrence_id}"
            ) from None

    def can_check_in(self, occurrence_id: str, at: datetime) -> bool:
        """Window: [start-30min, start+30min] inclusive, absolute time."""
        if at.tzinfo is None:
            raise ValueError("at must be timezone-aware")
        occ = self._get_occurrence(occurrence_id)
        if occ["status"] == "cancelled":
            return False
        start = _from_utc_text(occ["start_utc"])
        moment = at.astimezone(timezone.utc)
        return start - CHECK_IN_WINDOW <= moment <= start + CHECK_IN_WINDOW

    def list_occurrences(self, series_id: str) -> List[dict]:
        """Return id, seq, UTC start/end and status for each occurrence."""
        self._get_series(series_id)
        rows = self._conn.execute(
            "SELECT * FROM occurrences WHERE series_id = ? ORDER BY seq",
            (series_id,),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "seq": row["seq"],
                "start": _from_utc_text(row["start_utc"]),
                "end": _from_utc_text(row["end_utc"]),
                "status": row["status"],
            }
            for row in rows
        ]
