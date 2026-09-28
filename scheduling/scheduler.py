"""Public scheduler API backed by a single SQLite file."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import List

from .db import connect, init_db, transaction
from .errors import (
    EditRejectedError,
    NotFoundError,
    RegistrationError,
)
from .model import OccurrenceView, Rule, Status
from .timeutil import UTC, expand_occurrences, get_zone, localize


def _validate_rule(rule: Rule) -> None:
    if not isinstance(rule, Rule):
        raise TypeError("rule must be a Rule instance")
    get_zone(rule.timezone)  # raises ValueError for unknown IANA names
    first = rule.first_local_start
    if not isinstance(first, datetime):
        raise TypeError("first_local_start must be a datetime")
    if first.tzinfo is not None:
        raise ValueError("first_local_start must be naive (local wall clock)")
    if (
        not isinstance(rule.duration_minutes, int)
        or isinstance(rule.duration_minutes, bool)
        or rule.duration_minutes <= 0
    ):
        raise ValueError("duration_minutes must be a positive integer")
    if rule.period not in ("weekly", "biweekly", "monthly"):
        raise ValueError("period must be one of weekly, biweekly, monthly")
    if (
        not isinstance(rule.count, int)
        or isinstance(rule.count, bool)
        or not 1 <= rule.count <= 52
    ):
        raise ValueError("count must be an integer between 1 and 52")


def _parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _format_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _row_to_view(row) -> OccurrenceView:
    return OccurrenceView(
        id=row["id"],
        seq=row["seq"],
        start_utc=_parse_utc(row["start_utc"]),
        end_utc=_parse_utc(row["end_utc"]),
        status=row["status"],
    )


class Scheduler:
    """Schedule recurring sessions into one SQLite file."""

    def __init__(self, db_path: str) -> None:
        self._conn = connect(db_path)
        init_db(self._conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Scheduler":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # create_series
    # ------------------------------------------------------------------ #
    def create_series(self, rule: Rule) -> str:
        """Expand ``rule`` into occurrences and persist them."""
        _validate_rule(rule)
        series_id = uuid.uuid4().hex
        with transaction(self._conn) as cur:
            cur.execute(
                "INSERT INTO series VALUES (?, ?, ?, ?, ?, ?)",
                (
                    series_id,
                    rule.timezone,
                    rule.first_local_start.replace(tzinfo=None).isoformat(),
                    rule.duration_minutes,
                    rule.period,
                    rule.count,
                ),
            )
            for seq, start_utc, end_utc in expand_occurrences(rule):
                cur.execute(
                    "INSERT INTO occurrences ("
                    "id, series_id, seq, start_utc, end_utc, status"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        series_id,
                        seq,
                        _format_utc(start_utc),
                        _format_utc(end_utc),
                        Status.SCHEDULED.value,
                    ),
                )
        return series_id

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _get_occurrence(self, cur, occurrence_id: str):
        row = cur.execute(
            "SELECT * FROM occurrences WHERE id = ?", (occurrence_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"no occurrence with id {occurrence_id!r}")
        return row

    def _get_series_row(self, cur, series_id: str):
        row = cur.execute(
            "SELECT * FROM series WHERE id = ?", (series_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"no series with id {series_id!r}")
        return row

    # ------------------------------------------------------------------ #
    # override
    # ------------------------------------------------------------------ #
    def override(self, occurrence_id: str, new_local_start) -> None:
        """Reschedule one occurrence, or cancel it with ``None``.

        ``new_local_start`` is a naive local wall-clock time interpreted in
        the series' IANA zone (gap/overlap rules apply). The occurrence
        becomes an exception forever after.
        """
        if new_local_start is not None:
            if not isinstance(new_local_start, datetime):
                raise TypeError("new_local_start must be a naive datetime or None")
            if new_local_start.tzinfo is not None:
                raise ValueError("new_local_start must be naive (local wall clock)")

        with transaction(self._conn) as cur:
            row = self._get_occurrence(cur, occurrence_id)
            series = self._get_series_row(cur, row["series_id"])

            if new_local_start is None:
                cur.execute(
                    "UPDATE occurrences SET status = ?, "
                    "exception_start_utc = NULL, exception_end_utc = NULL "
                    "WHERE id = ?",
                    (Status.CANCELED.value, occurrence_id),
                )
                return

            start = localize(
                new_local_start, series["timezone"]
            ).astimezone(UTC)
            end = start + timedelta(minutes=series["duration_minutes"])
            cur.execute(
                "UPDATE occurrences SET status = ?, "
                "exception_start_utc = ?, exception_end_utc = ? "
                "WHERE id = ?",
                (
                    Status.RESCHEDULED.value,
                    _format_utc(start),
                    _format_utc(end),
                    occurrence_id,
                ),
            )

    # ------------------------------------------------------------------ #
    # edit_from
    # ------------------------------------------------------------------ #
    def edit_from(self, series_id: str, k: int, new_rule: Rule) -> None:
        """Re-expand from occurrence ``k`` onward using ``new_rule``.

        Occurrences before ``k`` are untouched. Existing non-exception
        occurrences at sequences that still exist keep their ids (only
        their times change); exception occurrences are never moved. The
        only rejection is when truncating would delete an occurrence that
        has registrations.
        """
        _validate_rule(new_rule)
        if not isinstance(k, int) or isinstance(k, bool) or k < 1:
            raise ValueError("k must be a positive integer")

        with transaction(self._conn) as cur:
            series = self._get_series_row(cur, series_id)
            old_count = series["count"]
            if k > old_count:
                raise NotFoundError(
                    f"series has {old_count} occurrences; cannot edit from {k}"
                )

            new_total = k - 1 + new_rule.count
            if new_total > 52:
                raise ValueError(
                    f"new total would be {new_total} occurrences (max 52)"
                )

            expanded = list(expand_occurrences(new_rule))
            assert len(expanded) == new_rule.count

            existing = {
                r["seq"]: r
                for r in cur.execute(
                    "SELECT * FROM occurrences WHERE series_id = ? AND seq >= ? "
                    "ORDER BY seq",
                    (series_id, k),
                ).fetchall()
            }

            # Only registrations on occurrences that actually disappear can
            # be orphaned; times/id changes preserve registrations.
            for seq, old in existing.items():
                if seq > new_total:
                    n = cur.execute(
                        "SELECT COUNT(*) FROM registrations WHERE occurrence_id = ?",
                        (old["id"],),
                    ).fetchone()[0]
                    if n:
                        raise EditRejectedError(
                            f"occurrence seq={seq} has {n} registration(s); "
                            "cannot shorten the series"
                        )

            cur.execute("DELETE FROM occurrences WHERE series_id = ? AND seq > ?",
                        (series_id, new_total))

            for index, (_, start_utc, end_utc) in enumerate(expanded):
                seq = k + index
                old = existing.get(seq)
                if old is not None and old["status"] != Status.SCHEDULED.value:
                    continue  # exception: keep its time and status forever
                if old is not None:
                    cur.execute(
                        "UPDATE occurrences SET start_utc = ?, end_utc = ?, "
                        "status = ? WHERE id = ?",
                        (
                            _format_utc(start_utc),
                            _format_utc(end_utc),
                            Status.SCHEDULED.value,
                            old["id"],
                        ),
                    )
                else:
                    cur.execute(
                        "INSERT INTO occurrences ("
                        "id, series_id, seq, start_utc, end_utc, status"
                        ") VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            uuid.uuid4().hex,
                            series_id,
                            seq,
                            _format_utc(start_utc),
                            _format_utc(end_utc),
                            Status.SCHEDULED.value,
                        ),
                    )

            cur.execute(
                "UPDATE series SET timezone = ?, first_local_start = ?, "
                "duration_minutes = ?, period = ?, count = ? WHERE id = ?",
                (
                    new_rule.timezone,
                    new_rule.first_local_start.replace(tzinfo=None).isoformat(),
                    new_rule.duration_minutes,
                    new_rule.period,
                    new_total,
                    series_id,
                ),
            )

    # ------------------------------------------------------------------ #
    # register
    # ------------------------------------------------------------------ #
    def register(self, occurrence_id: str, email: str) -> None:
        """Register an email for one occurrence.

        One email can register only once per occurrence; canceled
        occurrences reject registration.
        """
        if not isinstance(email, str) or not email.strip():
            raise ValueError("email must be a non-empty string")
        with transaction(self._conn) as cur:
            row = self._get_occurrence(cur, occurrence_id)
            if row["status"] == Status.CANCELED.value:
                raise RegistrationError("occurrence is canceled")
            dup = cur.execute(
                "SELECT 1 FROM registrations WHERE occurrence_id = ? AND email = ?",
                (occurrence_id, email),
            ).fetchone()
            if dup is not None:
                raise RegistrationError("email already registered for occurrence")
            cur.execute(
                "INSERT INTO registrations (occurrence_id, email, created_at) "
                "VALUES (?, ?, ?)",
                (occurrence_id, email, datetime.now(UTC).isoformat()),
            )

    # ------------------------------------------------------------------ #
    # can_check_in
    # ------------------------------------------------------------------ #
    def can_check_in(self, occurrence_id: str, at: datetime) -> bool:
        """Return whether ``at`` is within the check-in window.

        Window: start-30min inclusive to start+30min inclusive. Canceled or
        missing occurrences always return False. The judgment uses absolute
        instants only, so the tzinfo of ``at`` is irrelevant.
        """
        if not isinstance(at, datetime) or at.tzinfo is None:
            raise ValueError("at must be a timezone-aware datetime")
        row = self._conn.execute(
            "SELECT * FROM occurrences WHERE id = ?", (occurrence_id,)
        ).fetchone()
        if row is None or row["status"] == Status.CANCELED.value:
            return False
        if row["status"] == Status.RESCHEDULED.value:
            start = _parse_utc(row["exception_start_utc"])
        else:
            start = _parse_utc(row["start_utc"])
        instant = at.astimezone(UTC)
        return start - timedelta(minutes=30) <= instant <= start + timedelta(
            minutes=30
        )

    # ------------------------------------------------------------------ #
    # list_occurrences
    # ------------------------------------------------------------------ #
    def list_occurrences(self, series_id: str) -> List[OccurrenceView]:
        """Return all occurrences of a series with effective times."""
        with transaction(self._conn) as cur:
            self._get_series_row(cur, series_id)
            rows = cur.execute(
                "SELECT id, seq, status, "
                "COALESCE(exception_start_utc, start_utc) AS start_utc, "
                "COALESCE(exception_end_utc, end_utc) AS end_utc "
                "FROM occurrences WHERE series_id = ? ORDER BY seq",
                (series_id,),
            ).fetchall()
        return [_row_to_view(r) for r in rows]
