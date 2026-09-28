"""Pure scheduling logic: local wall-time resolution and series expansion.

Standard library only. Timezones are IANA names resolved via zoneinfo.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List
from zoneinfo import ZoneInfo

VALID_FREQS = ("weekly", "biweekly", "monthly")
MAX_OCCURRENCES = 52


@dataclass(frozen=True)
class Rule:
    """A series rule.

    tz: IANA timezone name.
    first_local_start: naive local wall-clock start of the first occurrence.
    duration_minutes: real elapsed duration.
    freq: "weekly" | "biweekly" | "monthly".
    count: number of occurrences (1..52).
    """

    tz: str
    first_local_start: datetime
    duration_minutes: int
    freq: str
    count: int

    def validate(self) -> None:
        if not isinstance(self.tz, str) or not self.tz:
            raise ValueError("tz must be a non-empty IANA timezone name")
        ZoneInfo(self.tz)  # raises if unknown
        if not isinstance(self.first_local_start, datetime):
            raise ValueError("first_local_start must be a datetime")
        if self.first_local_start.tzinfo is not None:
            raise ValueError("first_local_start must be naive local wall time")
        if not isinstance(self.duration_minutes, int) or self.duration_minutes <= 0:
            raise ValueError("duration_minutes must be a positive integer")
        if self.freq not in VALID_FREQS:
            raise ValueError(f"freq must be one of {VALID_FREQS}")
        if not isinstance(self.count, int) or not 1 <= self.count <= MAX_OCCURRENCES:
            raise ValueError(f"count must be between 1 and {MAX_OCCURRENCES}")


@dataclass(frozen=True)
class Slot:
    """One expanded occurrence: sequence number and absolute UTC bounds."""

    seq: int
    start_utc: datetime
    end_utc: datetime


def _round_trips(tz: ZoneInfo, naive: datetime, fold: int) -> bool:
    """True if ``naive`` with the given fold maps back to the same wall time."""
    aware = naive.replace(tzinfo=tz, fold=fold)
    back = aware.astimezone(timezone.utc).astimezone(tz)
    return back.replace(tzinfo=None) == naive


def resolve_local(tz: ZoneInfo, naive: datetime) -> datetime:
    """Resolve a naive local wall time to an aware datetime in ``tz``.

    Gap (nonexistent wall time): shift forward by the gap length, so a wall
    time of "02:30" in a one-hour gap becomes "03:30".
    Overlap (ambiguous wall time): pick the earlier UTC instant.
    """
    if naive.tzinfo is not None:
        raise ValueError("naive must be a naive datetime")
    candidates = [
        naive.replace(tzinfo=tz, fold=fold)
        for fold in (0, 1)
        if _round_trips(tz, naive, fold)
    ]
    if candidates:
        return min(candidates, key=lambda a: a.astimezone(timezone.utc))
    # Nonexistent wall time: fold=0 carries the pre-gap offset, fold=1 the
    # post-gap offset; their difference is the gap length.
    off_before = naive.replace(tzinfo=tz, fold=0).utcoffset()
    off_after = naive.replace(tzinfo=tz, fold=1).utcoffset()
    gap = off_after - off_before
    return resolve_local(tz, naive + gap)


def _add_months(year: int, month: int, months: int) -> tuple:
    total = (year * 12 + (month - 1)) + months
    return total // 12, total % 12 + 1


def _local_datetimes(rule: Rule) -> List[datetime]:
    """Naive local wall-clock datetimes for every occurrence of the rule."""
    first = rule.first_local_start
    tod = first.time()
    out = []
    for i in range(rule.count):
        if rule.freq == "weekly":
            day = first.date() + timedelta(weeks=i)
        elif rule.freq == "biweekly":
            day = first.date() + timedelta(weeks=2 * i)
        else:  # monthly: same day-of-month, clamped to the month's last day
            year, month = _add_months(first.year, first.month, i)
            last_day = calendar.monthrange(year, month)[1]
            day = first.date().replace(
                year=year, month=month, day=min(first.day, last_day)
            )
        out.append(datetime.combine(day, tod))
    return out


def expand(rule: Rule, first_seq: int = 1) -> List[Slot]:
    """Expand a rule into slots numbered from ``first_seq``."""
    rule.validate()
    tz = ZoneInfo(rule.tz)
    duration = timedelta(minutes=rule.duration_minutes)
    slots = []
    for i, naive in enumerate(_local_datetimes(rule)):
        start_utc = resolve_local(tz, naive).astimezone(timezone.utc)
        slots.append(Slot(seq=first_seq + i, start_utc=start_utc, end_utc=start_utc + duration))
    return slots
