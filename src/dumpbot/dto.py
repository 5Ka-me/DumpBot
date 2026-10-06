"""Plain data objects shared between modules (contracts: docs/architecture.md §4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from dumpbot.db.models import Item, Occurrence, Reminder
    from dumpbot.domain.kinds import Precision


@dataclass(frozen=True)
class ReminderPlan:
    fire_at: datetime  # aware UTC
    lead_minutes: int  # 0 = exactly at the time
    auto: bool  # time chosen automatically -> shifted out of quiet hours


@dataclass(frozen=True)
class Resolution:
    due_at: datetime | None  # aware UTC; None -> no time, no reminders
    precision: Precision
    reminders: tuple[ReminderPlan, ...] = ()
    rrule: str | None = None
    rec_time_local: time | None = None
    rec_dtstart_local: datetime | None = None  # naive, local wall time of prefs.tz
    needs_confirm: bool = False
    warnings: tuple[str, ...] = ()


@dataclass
class CreatedItem:
    item: Item
    occurrence: Occurrence | None
    reminders: list[Reminder]
    resolution: Resolution


@dataclass
class PatchResult:
    item: Item
    occurrence: Occurrence | None  # current open occurrence
    reminders: list[Reminder]  # pending reminders of the current occurrence
    warnings: list[str] = field(default_factory=list)
    deleted: bool = False


@dataclass
class Row:
    item_id: int
    kind: str
    title: str
    tags: list[str]
    status: str  # item status
    due_at: datetime | None  # due of the current / matched occurrence
    occurrence_id: int | None
    occ_status: str | None
    rrule: str | None
    created_at: datetime
    snippet: str | None  # fragment of source_text/details (<= 200 chars)
    urls: list[str] = field(default_factory=list)


@dataclass
class Agenda:
    day: date  # local date
    open: list[Row]  # open occurrences due that day, by time
    overdue: list[Row]  # open occurrences due before that day (only for today), not older than 30 days
    done: list[Row]  # occurrences done that day


@dataclass
class UsageSummary:
    month: str  # "октябрь 2026"
    spent_usd: float
    limit_usd: float
    calls: int
    by_purpose: dict[str, tuple[int, float]]  # purpose -> (calls, usd)


@dataclass
class BudgetState:
    spent_usd: float
    limit_usd: float
    ratio: float
    exhausted: bool


@dataclass
class DigestData:
    kind: Literal["morning", "evening"]
    agenda: Agenda
    recurring_today: int = 0
