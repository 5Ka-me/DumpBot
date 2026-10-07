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
    lead_minutes: int  # how long before the occurrence's time ("за день" of "за день и за час": 1380); 0 = at it
    auto: bool  # time chosen automatically -> shifted out of quiet hours


@dataclass(frozen=True)
class Resolution:
    # aware UTC; None -> no time, no reminders. The item's time IS its notification time: "встреча в 15" is due at
    # 15:00, "напомни за 30 минут до встречи в 15" at 14:30 (a deadline stays at the deadline).
    due_at: datetime | None
    precision: Precision
    reminders: tuple[ReminderPlan, ...] = ()
    rrule: str | None = None
    rec_time_local: time | None = None
    rec_dtstart_local: datetime | None = None  # naive, local wall time of prefs.tz
    needs_confirm: bool = False
    warnings: tuple[str, ...] = ()
    # Fuzzy time ("на этой неделе", "на днях", "когда-нибудь"): domain.kinds.PeriodKind and the local dates of the
    # span, both inclusive (None for someday). A deadline "до конца недели" is a plain date, not a period.
    period_kind: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    # The explicit leads ("за день и за час" -> (1440, 60)) still in effect, largest first: what ``Item.lead_minutes``
    # keeps. ``due_at`` is the stated time minus the smallest one (the last notification); the others notify earlier.
    # None: the stated time itself (no lead, or every lead moment had passed).
    leads: tuple[int, ...] | None = None
    # The time the message stated when a lead moved the notification before it ("(15:00)" for the title).
    stated_at: datetime | None = None


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
    period_kind: str | None = None  # Item.period_kind
    period_start: date | None = None
    period_end: date | None = None
    someday: bool = False
    # Checklists (docs/features/iteration-2.md §C2): a step names its parent ("(из «Переезд»)"), a parent counts its
    # steps ("2/5"; deleted steps are not counted).
    parent_id: int | None = None
    parent_title: str | None = None
    steps_done: int = 0
    steps_total: int = 0


@dataclass(frozen=True)
class Step:
    """One line of a parent's checklist: "☑ 2. Заказать машину · чт 8 окт 15:00"."""

    item_id: int
    position: int
    title: str
    done: bool
    due_at: datetime | None = None  # due of the step's current occurrence (a step with a time of its own)
    precision: str | None = None
    # The step's fuzzy span (Item.period_kind/start/end): "на следующей неделе" shows as words, not as the date of
    # its reminder (2).
    period_kind: str | None = None
    period_start: date | None = None
    period_end: date | None = None


@dataclass(frozen=True)
class RelatedRef:
    """An item the AI found related to this one (``item_links``, either direction)."""

    item_id: int
    kind: str
    title: str
    created_at: datetime


@dataclass(frozen=True)
class CardExtras:
    """What an item card shows besides the item itself: its checklist, related items, its parent."""

    steps: tuple[Step, ...] = ()
    related: tuple[RelatedRef, ...] = ()  # newest first, all of them (the card names two)
    parent_title: str | None = None  # the item is a step of this parent


@dataclass
class Agenda:
    day: date  # local date
    open: list[Row]  # open occurrences due that day, by time
    overdue: list[Row]  # open occurrences due before that day (only for today), not older than 30 days
    done: list[Row]  # occurrences done that day
    # Open items with a calendar period (week / month / season) that contains the day, the shortest span first.
    # While its period runs such an item is listed here, not in open / overdue.
    period_items: list[Row] = field(default_factory=list)


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
