"""Domain types for the recurring-session scheduler."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List


class Period(str, Enum):
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"


class Status(str, Enum):
    SCHEDULED = "scheduled"
    RESCHEDULED = "rescheduled"
    CANCELED = "canceled"


@dataclass(frozen=True)
class Rule:
    timezone: str
    first_local_start: datetime
    duration_minutes: int
    period: str
    count: int


@dataclass(frozen=True)
class OccurrenceView:
    id: str
    seq: int
    start_utc: datetime
    end_utc: datetime
    status: str


@dataclass(frozen=True)
class SeriesView:
    id: str
    rule: Rule
    occurrences: List[OccurrenceView] = field(default_factory=list)
