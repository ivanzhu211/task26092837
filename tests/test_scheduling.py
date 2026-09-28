"""Tests for the recurring-session scheduler."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from scheduling.db import SCHEMA_STATEMENTS, connect
from scheduling.errors import (
    EditRejectedError,
    NotFoundError,
    RegistrationError,
)
from scheduling.model import Rule, Status
from scheduling.scheduler import Scheduler

NY = "America/New_York"
UTC = timezone.utc
SH = ZoneInfo("Asia/Shanghai")


def dt(y, mo, d, h=0, mi=0, tz=NY):
    return datetime(y, mo, d, h, mi, tzinfo=tz)


@pytest.fixture
def scheduler(tmp_path):
    s = Scheduler(str(tmp_path / "sched.db"))
    yield s
    s.close()


# ---------------------------------------------------------------------------
# Required: weekly NYC series crossing the spring-forward gap
# ---------------------------------------------------------------------------
def test_weekly_spring_gap_utc_times(scheduler):
    rule = Rule(NY, datetime(2024, 3, 3, 2, 30), 60, "weekly", 4)
    sid = scheduler.create_series(rule)
    occs = scheduler.list_occurrences(sid)

    expected_starts = [
        datetime(2024, 3, 3, 7, 30, tzinfo=UTC),   # EST -05:00
        datetime(2024, 3, 10, 7, 30, tzinfo=UTC),  # 02:30 in gap -> 03:30 EDT
        datetime(2024, 3, 17, 6, 30, tzinfo=UTC),  # EDT -04:00
        datetime(2024, 3, 24, 6, 30, tzinfo=UTC),
    ]
    assert [o.seq for o in occs] == [1, 2, 3, 4]
    for occ, start in zip(occs, expected_starts):
        assert occ.start_utc == start
        assert occ.end_utc == start + timedelta(minutes=60)
        assert occ.status == Status.SCHEDULED.value


# ---------------------------------------------------------------------------
# Required: weekly NYC series at local 01:30 crossing the fall-back overlap
# ---------------------------------------------------------------------------
def _fall_rule():
    return Rule(NY, datetime(2024, 10, 27, 1, 30), 30, "weekly", 3)


def test_weekly_fall_overlap_utc_times(scheduler):
    sid = scheduler.create_series(_fall_rule())
    occs = scheduler.list_occurrences(sid)

    expected_starts = [
        datetime(2024, 10, 27, 5, 30, tzinfo=UTC),  # EDT -04:00
        datetime(2024, 11, 3, 5, 30, tzinfo=UTC),   # ambiguous 01:30 -> earlier
        datetime(2024, 11, 10, 6, 30, tzinfo=UTC),  # EST -05:00
    ]
    for occ, start in zip(occs, expected_starts):
        assert occ.start_utc == start
        assert occ.end_utc == start + timedelta(minutes=30)


def test_fall_overlap_checkin_window(scheduler):
    sid = scheduler.create_series(_fall_rule())
    overlap_id = scheduler.list_occurrences(sid)[1].id
    start = datetime(2024, 11, 3, 5, 30, tzinfo=UTC)

    # First 01:45 local (EDT, fold 0) -> 05:45 UTC, inside the window.
    first_0145 = datetime(2024, 11, 3, 1, 45, tzinfo=ZoneInfo(NY))
    assert scheduler.can_check_in(overlap_id, first_0145) is True

    # Second 01:45 local (EST, fold 1) -> 06:45 UTC, outside: rejected.
    second_0145 = first_0145.replace(fold=1)
    assert second_0145.astimezone(UTC) == datetime(2024, 11, 3, 6, 45, tzinfo=UTC)
    assert scheduler.can_check_in(overlap_id, second_0145) is False

    # Window edges are inclusive, and tz of the argument is irrelevant.
    edge_late = start + timedelta(minutes=30)
    edge_early = start - timedelta(minutes=30)
    assert scheduler.can_check_in(overlap_id, edge_late) is True
    assert scheduler.can_check_in(overlap_id, edge_late.astimezone(SH)) is True
    assert scheduler.can_check_in(overlap_id, edge_early) is True
    assert scheduler.can_check_in(
        overlap_id, edge_early.astimezone(ZoneInfo("Pacific/Auckland"))
    ) is True
    assert scheduler.can_check_in(
        overlap_id, edge_late + timedelta(seconds=1)
    ) is False


# ---------------------------------------------------------------------------
# Required: monthly series starting on Jan 31 (day clamping)
# ---------------------------------------------------------------------------
def test_monthly_jan31_day_clamping(scheduler):
    rule = Rule("UTC", datetime(2024, 1, 31, 10, 0), 45, "monthly", 12)
    sid = scheduler.create_series(rule)
    days = [(o.start_utc.month, o.start_utc.day)
            for o in scheduler.list_occurrences(sid)]
    assert days == [
        (1, 31), (2, 29), (3, 31), (4, 30), (5, 31), (6, 30),
        (7, 31), (8, 31), (9, 30), (10, 31), (11, 30), (12, 31),
    ]
    for o in scheduler.list_occurrences(sid):
        assert o.start_utc.hour == 10
        assert o.end_utc - o.start_utc == timedelta(minutes=45)


def test_monthly_non_leap_year_february(scheduler):
    rule = Rule("UTC", datetime(2023, 1, 31, 0, 0), 10, "monthly", 3)
    sid = scheduler.create_series(rule)
    days = [(o.start_utc.month, o.start_utc.day)
            for o in scheduler.list_occurrences(sid)]
    assert days == [(1, 31), (2, 28), (3, 31)]


def _dump_db(path):
    """Dump every table row, table-by-table, as a deterministic structure."""
    conn = connect(str(path))
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "ORDER BY name")]
        dump = {}
        for table in tables:
            cols = [c[1] for c in conn.execute(
                f"PRAGMA table_info({table})")]
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY {', '.join(cols)}"
            ).fetchall()
            dump[table] = [tuple(r) for r in rows]
        return dump
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Required: shortening with registrations on doomed occurrences is rejected,
# and the database is byte-for-byte row-identical before/after
# ---------------------------------------------------------------------------
def test_edit_from_shorten_blocked_by_registration(scheduler, tmp_path):
    db_path = tmp_path / "sched.db"
    rule = Rule(NY, datetime(2024, 1, 2, 9, 0), 60, "weekly", 5)
    sid = scheduler.create_series(rule)
    occs = scheduler.list_occurrences(sid)
    scheduler.register(occs[4].id, "doomed@example.com")

    before = _dump_db(db_path)
    with pytest.raises(EditRejectedError):
        scheduler.edit_from(
            sid, 4, Rule(NY, datetime(2024, 6, 4, 9, 0), 60, "weekly", 1)
        )
    after = _dump_db(db_path)
    assert before == after

    # Series is still intact: five occurrences, same ids and times.
    occs2 = scheduler.list_occurrences(sid)
    assert [o.seq for o in occs2] == [1, 2, 3, 4, 5]
    assert [o.id for o in occs2] == [o.id for o in occs]
    assert [o.start_utc for o in occs2] == [o.start_utc for o in occs]


def test_edit_from_keeps_ids_and_registrations(scheduler):
    rule = Rule(NY, datetime(2024, 1, 2, 9, 0), 60, "weekly", 4)
    sid = scheduler.create_series(rule)
    occs = scheduler.list_occurrences(sid)
    scheduler.register(occs[2].id, "keep@example.com")

    scheduler.edit_from(
        sid, 3, Rule(NY, datetime(2024, 5, 7, 10, 0), 60, "weekly", 2)
    )
    after = scheduler.list_occurrences(sid)
    assert [o.seq for o in after] == [1, 2, 3, 4]
    # Existing non-exception occurrences keep ids; only times change.
    assert after[2].id == occs[2].id
    assert after[3].id == occs[3].id
    assert after[2].start_utc == datetime(2024, 5, 7, 14, 0, tzinfo=UTC)
    assert after[3].start_utc == datetime(2024, 5, 14, 14, 0, tzinfo=UTC)
    assert after[0].start_utc == occs[0].start_utc  # before k untouched
    assert after[1].start_utc == occs[1].start_utc
    # Registration survived with its id.
    conn = scheduler._conn
    rows = conn.execute(
        "SELECT email FROM registrations WHERE occurrence_id = ?",
        (after[2].id,),
    ).fetchall()
    assert [r[0] for r in rows] == ["keep@example.com"]


def test_edit_from_can_extend_and_exception_is_pinned(scheduler):
    rule = Rule(NY, datetime(2024, 1, 2, 9, 0), 60, "weekly", 3)
    sid = scheduler.create_series(rule)
    occs = scheduler.list_occurrences(sid)

    # seq 2 becomes an exception (rescheduled); seq 3 is canceled.
    scheduler.override(occs[1].id, datetime(2024, 6, 15, 12, 0))
    scheduler.override(occs[2].id, None)
    ex_start = scheduler.list_occurrences(sid)[1].start_utc

    scheduler.edit_from(
        sid, 2, Rule(NY, datetime(2024, 7, 2, 9, 0), 60, "weekly", 3)
    )
    after = scheduler.list_occurrences(sid)
    assert [o.seq for o in after] == [1, 2, 3, 4]
    # Exception occurrences keep id, status and time despite series re-expansion.
    assert after[1].id == occs[1].id
    assert after[1].status == Status.RESCHEDULED.value
    assert after[1].start_utc == ex_start
    assert after[2].id == occs[2].id
    assert after[2].status == Status.CANCELED.value
    # Brand-new sequence gets a fresh occurrence.
    assert after[3].status == Status.SCHEDULED.value
    assert after[3].start_utc == datetime(2024, 7, 16, 13, 0, tzinfo=UTC)


def test_edit_from_rejects_total_over_52(scheduler):
    rule = Rule(NY, datetime(2024, 1, 2, 9, 0), 60, "weekly", 50)
    sid = scheduler.create_series(rule)
    with pytest.raises(ValueError):
        scheduler.edit_from(
            sid, 50, Rule(NY, datetime(2024, 7, 2, 9, 0), 60, "weekly", 4)
        )


# ---------------------------------------------------------------------------
# register / override / can_check_in rules
# ---------------------------------------------------------------------------
def test_register_duplicate_and_canceled(scheduler):
    sid = scheduler.create_series(Rule(NY, datetime(2024, 1, 2, 9, 0), 60,
                                      "weekly", 2))
    a, b = scheduler.list_occurrences(sid)
    scheduler.register(a.id, "a@example.com")
    with pytest.raises(RegistrationError):
        scheduler.register(a.id, "a@example.com")
    scheduler.register(a.id, "b@example.com")  # other email is fine
    scheduler.override(b.id, None)
    with pytest.raises(RegistrationError):
        scheduler.register(b.id, "c@example.com")
    assert scheduler.can_check_in(b.id, datetime(2024, 1, 9, 14, 0, tzinfo=UTC)) is False


def test_override_reschedule_uses_gap_rules_and_updates_window(scheduler):
    sid = scheduler.create_series(Rule(NY, datetime(2024, 3, 3, 2, 30), 60,
                                      "weekly", 1))
    occ = scheduler.list_occurrences(sid)[0]
    # Move it into the spring gap: naive 02:30 on 3/10 shifts to 03:30 EDT.
    scheduler.override(occ.id, datetime(2024, 3, 10, 2, 30))
    moved = scheduler.list_occurrences(sid)[0]
    assert moved.status == Status.RESCHEDULED.value
    assert moved.start_utc == datetime(2024, 3, 10, 7, 30, tzinfo=UTC)
    assert moved.end_utc == datetime(2024, 3, 10, 8, 30, tzinfo=UTC)
    # Window follows the new absolute start (07:00-08:00 UTC), not the old.
    assert scheduler.can_check_in(occ.id,
                                  datetime(2024, 3, 10, 7, 30, tzinfo=UTC)) is True
    assert scheduler.can_check_in(occ.id,
                                  datetime(2024, 3, 3, 7, 30, tzinfo=UTC)) is False


def test_missing_occurrence_and_series(scheduler):
    with pytest.raises(NotFoundError):
        scheduler.register("nope", "x@example.com")
    with pytest.raises(NotFoundError):
        scheduler.list_occurrences("nope")
    assert scheduler.can_check_in("nope", datetime(2024, 1, 1, tzinfo=UTC)) is False
    sid = scheduler.create_series(Rule(NY, datetime(2024, 1, 2, 9, 0), 60,
                                      "weekly", 2))
    with pytest.raises(NotFoundError):
        scheduler.edit_from(sid, 3, Rule(NY, datetime(2024, 1, 2, 9, 0), 60,
                                         "weekly", 1))


def test_invalid_rules_rejected(scheduler):
    with pytest.raises(ValueError):
        scheduler.create_series(Rule("Not/AZone", datetime(2024, 1, 2), 60,
                                     "weekly", 1))
    with pytest.raises(ValueError):
        scheduler.create_series(Rule(NY, datetime(2024, 1, 2), 60, "yearly", 1))
    with pytest.raises(ValueError):
        scheduler.create_series(Rule(NY, datetime(2024, 1, 2), 60, "weekly", 0))
    with pytest.raises(ValueError):
        scheduler.create_series(Rule(NY, datetime(2024, 1, 2), 60, "weekly", 53))
    with pytest.raises(ValueError):
        scheduler.create_series(Rule(NY, datetime(2024, 1, 2), 0, "weekly", 1))
    aware = datetime(2024, 1, 2, tzinfo=UTC)
    with pytest.raises(ValueError):
        scheduler.create_series(Rule(NY, aware, 60, "weekly", 1))


# ---------------------------------------------------------------------------
# Persistence: stored on disk in one SQLite file, IANA name preserved
# ---------------------------------------------------------------------------
def test_persistence_and_iana_storage(tmp_path):
    db_path = tmp_path / "p.db"
    s1 = Scheduler(str(db_path))
    rule = Rule("Asia/Shanghai", datetime(2024, 1, 15, 20, 0), 90, "biweekly", 3)
    sid = s1.create_series(rule)
    occs = s1.list_occurrences(sid)
    expected = [
        datetime(2024, 1, 15, 12, 0, tzinfo=UTC),
        datetime(2024, 1, 29, 12, 0, tzinfo=UTC),
        datetime(2024, 2, 12, 12, 0, tzinfo=UTC),
    ]
    assert [o.start_utc for o in occs] == expected
    assert [o.end_utc - o.start_utc for o in occs] == [timedelta(minutes=90)] * 3
    s1.close()

    raw = connect(str(db_path))
    try:
        tz_row = raw.execute("SELECT timezone FROM series").fetchone()
        assert tz_row[0] == "Asia/Shanghai"
        # Absolute instants are stored as ISO UTC strings.
        first = raw.execute(
            "SELECT start_utc FROM occurrences WHERE seq = 1").fetchone()[0]
        assert first == "2024-01-15T12:00:00+00:00"
    finally:
        raw.close()

    s2 = Scheduler(str(db_path))
    try:
        reopened = s2.list_occurrences(sid)
        assert [o.start_utc for o in reopened] == expected
        assert all(o.id for o in reopened)
    finally:
        s2.close()


def test_ids_are_stable_and_unique(scheduler):
    sid = scheduler.create_series(Rule(NY, datetime(2024, 1, 2, 9, 0), 60,
                                      "weekly", 3))
    ids_before = [o.id for o in scheduler.list_occurrences(sid)]
    assert len(set(ids_before)) == 3
    # A no-op style edit (same rule from k=1) keeps ids of nonexceptions.
    scheduler.edit_from(sid, 1, Rule(NY, datetime(2024, 1, 2, 9, 0), 60,
                                     "weekly", 3))
    ids_after = [o.id for o in scheduler.list_occurrences(sid)]
    assert ids_after == ids_before


def test_dst_elapsed_duration_spring(scheduler):
    # 23:30 local, 60-minute session crosses the 02:00 spring gap.
    sid = scheduler.create_series(Rule(NY, datetime(2024, 3, 9, 23, 30), 60,
                                      "weekly", 2))
    occs = scheduler.list_occurrences(sid)
    assert occs[0].start_utc == datetime(2024, 3, 10, 4, 30, tzinfo=UTC)
    assert occs[0].end_utc == datetime(2024, 3, 10, 5, 30, tzinfo=UTC)
    assert occs[1].start_utc == datetime(2024, 3, 17, 3, 30, tzinfo=UTC)
    assert occs[1].end_utc == datetime(2024, 3, 17, 4, 30, tzinfo=UTC)
