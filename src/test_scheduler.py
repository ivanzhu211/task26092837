"""Tests for the recurring event scheduler."""

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from recurrence import Rule
from scheduler import Scheduler

NY = "America/New_York"
UTC = timezone.utc


def dt(y, mo, d, h, mi, tz=UTC):
    return datetime(y, mo, d, h, mi, tzinfo=tz)


class SchedulerTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        os.unlink(self.db_path)
        self.sched = Scheduler(self.db_path)

    def tearDown(self):
        self.sched.close()
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def dump_db(self):
        out = {}
        conn = sqlite3.connect(self.db_path)
        try:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            for table in tables:
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                out[table] = sorted(tuple(r) for r in rows)
        finally:
            conn.close()
        return out

    def make_series(self, **kwargs):
        defaults = dict(
            tz=NY,
            first_local_start=datetime(2026, 1, 5, 9, 0),
            duration_minutes=60,
            freq="weekly",
            count=4,
        )
        defaults.update(kwargs)
        return self.sched.create_series(Rule(**defaults))


class SpringGapTests(SchedulerTestCase):
    def test_weekly_series_across_spring_gap(self):
        # DST starts 2026-03-08 in America/New_York; 02:30 does not exist.
        sid = self.make_series(
            first_local_start=datetime(2026, 3, 1, 2, 30), count=3
        )
        occs = self.sched.list_occurrences(sid)
        self.assertEqual([o["seq"] for o in occs], [1, 2, 3])
        expected = [
            # Mar 1, 02:30 EST (UTC-5)
            (dt(2026, 3, 1, 7, 30), dt(2026, 3, 1, 8, 30)),
            # Mar 8, 02:30 falls in the gap -> 03:30 EDT (UTC-4)
            (dt(2026, 3, 8, 7, 30), dt(2026, 3, 8, 8, 30)),
            # Mar 15, 02:30 EDT (UTC-4)
            (dt(2026, 3, 15, 6, 30), dt(2026, 3, 15, 7, 30)),
        ]
        for occ, (start, end) in zip(occs, expected):
            self.assertEqual(occ["start"], start)
            self.assertEqual(occ["end"], end)
            self.assertEqual(occ["status"], "scheduled")


class FallOverlapTests(SchedulerTestCase):
    def setUp(self):
        super().setUp()
        # DST ends 2026-11-01; 01:30 happens twice. Earlier instant = EDT.
        self.sid = self.make_series(
            first_local_start=datetime(2026, 10, 25, 1, 30), count=3
        )
        self.occs = self.sched.list_occurrences(self.sid)

    def test_weekly_series_across_fall_overlap(self):
        expected = [
            # Oct 25, 01:30 EDT (UTC-4)
            (dt(2026, 10, 25, 5, 30), dt(2026, 10, 25, 6, 30)),
            # Nov 1, ambiguous 01:30 -> earlier occurrence, EDT (UTC-4)
            (dt(2026, 11, 1, 5, 30), dt(2026, 11, 1, 6, 30)),
            # Nov 8, 01:30 EST (UTC-5)
            (dt(2026, 11, 8, 6, 30), dt(2026, 11, 8, 7, 30)),
        ]
        for occ, (start, end) in zip(self.occs, expected):
            self.assertEqual(occ["start"], start)
            self.assertEqual(occ["end"], end)

    def test_check_in_second_0130_occurrence_rejected(self):
        overlap_occ = self.occs[1]
        # Second time 01:45 happens that day: EST (UTC-5) -> 06:45 UTC.
        second_0145 = dt(2026, 11, 1, 1, 45, ZoneInfo(NY)).replace(fold=1)
        self.assertEqual(second_0145.utcoffset(), timedelta(hours=-5))
        self.assertFalse(self.sched.can_check_in(overlap_occ["id"], second_0145))
        # Same instant expressed in another timezone must give the same answer.
        self.assertFalse(
            self.sched.can_check_in(
                overlap_occ["id"], second_0145.astimezone(ZoneInfo("Asia/Tokyo"))
            )
        )
        # First time 01:45 happens: EDT (UTC-4) -> 05:45 UTC, inside window.
        first_0145 = dt(2026, 11, 1, 1, 45, ZoneInfo(NY)).replace(fold=0)
        self.assertTrue(self.sched.can_check_in(overlap_occ["id"], first_0145))

    def test_check_in_window_boundaries(self):
        occ = self.occs[0]  # starts 2026-10-25 05:30 UTC
        start = occ["start"]
        self.assertTrue(self.sched.can_check_in(occ["id"], start - timedelta(minutes=30)))
        self.assertTrue(self.sched.can_check_in(occ["id"], start + timedelta(minutes=30)))
        self.assertFalse(
            self.sched.can_check_in(occ["id"], start - timedelta(minutes=30, seconds=1))
        )
        self.assertFalse(
            self.sched.can_check_in(occ["id"], start + timedelta(minutes=30, seconds=1))
        )


class MonthlyTests(SchedulerTestCase):
    def test_monthly_from_jan_31(self):
        sid = self.make_series(
            tz="UTC",
            first_local_start=datetime(2026, 1, 31, 9, 0),
            duration_minutes=45,
            freq="monthly",
            count=5,
        )
        occs = self.sched.list_occurrences(sid)
        starts = [o["start"] for o in occs]
        self.assertEqual(
            starts,
            [
                dt(2026, 1, 31, 9, 0),
                dt(2026, 2, 28, 9, 0),  # clamped to last day of Feb
                dt(2026, 3, 31, 9, 0),  # back to the 31st
                dt(2026, 4, 30, 9, 0),
                dt(2026, 5, 31, 9, 0),
            ],
        )
        for occ in occs:
            self.assertEqual(occ["end"] - occ["start"], timedelta(minutes=45))


class OverrideTests(SchedulerTestCase):
    def test_cancel_blocks_register_and_check_in(self):
        sid = self.make_series()
        occ = self.sched.list_occurrences(sid)[1]
        self.sched.override(occ["id"], None)
        with self.assertRaises(ValueError):
            self.sched.register(occ["id"], "a@example.com")
        self.assertFalse(self.sched.can_check_in(occ["id"], occ["start"]))
        self.assertEqual(self.sched.list_occurrences(sid)[1]["status"], "cancelled")

    def test_reschedule_uses_series_tz_and_gap_rule(self):
        sid = self.make_series()
        occ = self.sched.list_occurrences(sid)[0]
        # 02:30 on gap day -> 03:30 EDT = 07:30 UTC; duration preserved.
        self.sched.override(occ["id"], datetime(2026, 3, 8, 2, 30))
        updated = self.sched.list_occurrences(sid)[0]
        self.assertEqual(updated["id"], occ["id"])
        self.assertEqual(updated["start"], dt(2026, 3, 8, 7, 30))
        self.assertEqual(updated["end"], dt(2026, 3, 8, 8, 30))

    def test_duplicate_registration_rejected(self):
        sid = self.make_series()
        occ = self.sched.list_occurrences(sid)[0]
        self.sched.register(occ["id"], "a@example.com")
        with self.assertRaises(ValueError):
            self.sched.register(occ["id"], "a@example.com")
        self.sched.register(occ["id"], "b@example.com")


class EditFromTests(SchedulerTestCase):
    def test_edit_from_preserves_ids_and_registrations(self):
        sid = self.make_series(count=5)
        before = self.sched.list_occurrences(sid)
        self.sched.register(before[2]["id"], "a@example.com")
        new_rule = Rule(
            tz=NY,
            first_local_start=datetime(2026, 2, 10, 18, 0),
            duration_minutes=90,
            freq="weekly",
            count=3,
        )
        self.sched.edit_from(sid, 3, new_rule)
        after = self.sched.list_occurrences(sid)
        self.assertEqual(len(after), 5)
        # seq < k untouched
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[1], before[1])
        # seq >= k: same ids, new times
        for old, new in zip(before[2:], after[2:]):
            self.assertEqual(old["id"], new["id"])
        self.assertEqual(after[2]["start"], dt(2026, 2, 10, 23, 0))  # 18:00 EST
        self.assertEqual(after[2]["end"] - after[2]["start"], timedelta(minutes=90))
        # registration followed the id
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT occurrence_id, email FROM registrations").fetchall()
        conn.close()
        self.assertEqual(rows, [(after[2]["id"], "a@example.com")])

    def test_edit_from_keeps_exceptions_untouched(self):
        sid = self.make_series(count=5)
        before = self.sched.list_occurrences(sid)
        self.sched.override(before[3]["id"], datetime(2026, 3, 3, 12, 0))
        exception = self.sched.list_occurrences(sid)[3]
        new_rule = Rule(
            tz=NY,
            first_local_start=datetime(2026, 2, 10, 18, 0),
            duration_minutes=60,
            freq="weekly",
            count=3,
        )
        self.sched.edit_from(sid, 3, new_rule)
        after = self.sched.list_occurrences(sid)
        self.assertEqual(after[3], exception)  # time and status unchanged
        self.assertNotEqual(after[2]["start"], before[2]["start"])  # others moved

    def test_edit_from_grows_series(self):
        sid = self.make_series(count=2)
        new_rule = Rule(
            tz=NY,
            first_local_start=datetime(2026, 1, 12, 10, 0),
            duration_minutes=30,
            freq="weekly",
            count=4,
        )
        self.sched.edit_from(sid, 2, new_rule)
        occs = self.sched.list_occurrences(sid)
        self.assertEqual([o["seq"] for o in occs], [1, 2, 3, 4, 5])

    def test_edit_from_beyond_52_rejected(self):
        sid = self.make_series(count=4)
        new_rule = Rule(
            tz=NY,
            first_local_start=datetime(2026, 1, 12, 10, 0),
            duration_minutes=30,
            freq="weekly",
            count=52,
        )
        with self.assertRaises(ValueError):
            self.sched.edit_from(sid, 2, new_rule)

    def test_shrink_with_registration_on_deleted_seq_fails_atomically(self):
        sid = self.make_series(count=5)
        occs = self.sched.list_occurrences(sid)
        self.sched.register(occs[3]["id"], "a@example.com")
        self.sched.register(occs[4]["id"], "b@example.com")
        before_dump = self.dump_db()
        new_rule = Rule(
            tz=NY,
            first_local_start=datetime(2026, 1, 12, 10, 0),
            duration_minutes=30,
            freq="weekly",
            count=1,  # total would shrink to 2; seqs 3..5 deleted
        )
        with self.assertRaises(ValueError):
            self.sched.edit_from(sid, 2, new_rule)
        self.assertEqual(self.dump_db(), before_dump)
        # series still fully intact
        self.assertEqual(self.sched.list_occurrences(sid), occs)

    def test_shrink_without_registrations_succeeds(self):
        sid = self.make_series(count=5)
        occs = self.sched.list_occurrences(sid)
        self.sched.register(occs[1]["id"], "a@example.com")
        new_rule = Rule(
            tz=NY,
            first_local_start=datetime(2026, 1, 12, 10, 0),
            duration_minutes=30,
            freq="weekly",
            count=2,
        )
        self.sched.edit_from(sid, 2, new_rule)
        after = self.sched.list_occurrences(sid)
        self.assertEqual([o["seq"] for o in after], [1, 2, 3])
        # registration on surviving occurrence intact
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT occurrence_id, email FROM registrations").fetchall()
        conn.close()
        self.assertEqual(rows, [(after[1]["id"], "a@example.com")])


class ValidationTests(SchedulerTestCase):
    def test_rule_validation(self):
        base = dict(
            tz=NY,
            first_local_start=datetime(2026, 1, 5, 9, 0),
            duration_minutes=60,
            freq="weekly",
            count=4,
        )
        with self.assertRaises(ValueError):
            self.make_series(count=0)
        with self.assertRaises(ValueError):
            self.make_series(count=53)
        with self.assertRaises(ValueError):
            self.make_series(freq="daily")
        with self.assertRaises(ValueError):
            self.make_series(duration_minutes=0)
        with self.assertRaises(Exception):
            self.make_series(tz="Not/AZone")
        self.make_series(**{})  # defaults are valid

    def test_check_in_requires_aware_datetime(self):
        sid = self.make_series()
        occ = self.sched.list_occurrences(sid)[0]
        with self.assertRaises(ValueError):
            self.sched.can_check_in(occ["id"], datetime(2026, 1, 5, 14, 0))


if __name__ == "__main__":
    unittest.main()
