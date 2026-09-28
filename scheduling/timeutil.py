"""Time-zone interpretation and occurrence expansion (stdlib only)."""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Iterator, Tuple
from zoneinfo import ZoneInfo


VALID_PERIODS = ("weekly", "biweekly", "monthly")
UTC = timezone.utc


@lru_cache(maxsize=None)
def get_zone(name: str) -> ZoneInfo:
    """Return a cached IANA time zone; raise ValueError for an unknown name."""
    if not isinstance(name, str) or not name:
        raise ValueError("timezone name must be a non-empty IANA string")
    try:
        return ZoneInfo(name)
    except Exception as exc:  # zoneinfo raises ZoneInfoNotFoundError (KeyError)
        raise ValueError(f"unknown IANA time zone: {name!r}") from exc


def localize(wall: datetime, tz_name: str) -> datetime:
    """Interpret a naive local wall-clock time in the given IANA zone.

    - A time inside a spring-forward gap is shifted forward by the gap length
      (e.g. ``02:30`` in a one-hour gap becomes ``03:30``).
    - An ambiguous time during a fall-back overlap resolves to the earlier
      absolute instant (fold == 0).
    """
    if not isinstance(wall, datetime):
        raise TypeError("wall time must be a datetime")
    if wall.tzinfo is not None:
        raise ValueError("wall time must be naive (no tzinfo)")
    zone = get_zone(tz_name)

    dt_early = wall.replace(tzinfo=zone)
    roundtrip_naive = dt_early.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if roundtrip_naive == wall:
        return dt_early

    # Non-existent local time: fold=1 exposes the post-transition offset,
    # and their difference is exactly the gap length.
    dt_late = wall.replace(tzinfo=zone, fold=1)
    gap = dt_late.utcoffset() - dt_early.utcoffset()
    shifted_wall = wall + gap
    return shifted_wall.replace(tzinfo=zone)


def _monthly_wall(first: datetime, index: int) -> datetime:
    base = first.year * 12 + (first.month - 1)
    total = base + index
    year, month1 = divmod(total, 12)
    month = month1 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = min(first.day, last_day)
    return first.replace(year=year, month=month, day=day)


def iter_local_starts(
    first: datetime, period: str, count: int
) -> Iterator[datetime]:
    """Yield naive local wall-clock datetimes, one per occurrence.

    Every occurrence keeps the first occurrence's wall-clock time.
    Monthly keeps the first day-of-month, clamped to the month's last day
    when that day does not exist; later months return to the original day.
    """
    if first.tzinfo is not None:
        raise ValueError("first local start must be naive (no tzinfo)")
    if period not in VALID_PERIODS:
        raise ValueError(f"period must be one of {VALID_PERIODS}, got {period!r}")
    if not isinstance(count, int) or isinstance(count, bool):
        raise TypeError("count must be an int")
    if count < 1 or count > 52:
        raise ValueError("count must be between 1 and 52")

    for i in range(count):
        if period == "weekly":
            yield first + timedelta(weeks=i)
        elif period == "biweekly":
            yield first + timedelta(weeks=2 * i)
        else:
            yield _monthly_wall(first, i)


def expand_occurrences(rule) -> Iterator[Tuple[int, datetime, datetime]]:
    """Yield ``(seq, absolute_start_utc, absolute_end_utc)`` for a rule.

    Durations count elapsed time, so the end instant is the start instant
    plus the duration regardless of any DST transition in between.
    """
    duration = timedelta(minutes=rule.duration_minutes)
    for seq, wall in enumerate(
        iter_local_starts(
            rule.first_local_start, rule.period, rule.count
        ),
        start=1,
    ):
        start = localize(wall, rule.timezone).astimezone(UTC)
        yield seq, start, start + duration
