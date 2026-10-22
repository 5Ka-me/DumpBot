"""Data for the morning / evening digests and the weekly review (architecture.md §4.15). Rendering lives in
``bot.views``.

An evening digest the bot was down for goes into the next morning digest (docs/features/iteration-3.md §5): the
scheduler marks its day with ``folded_key`` (only when ``day_summary`` has something to say), and the morning digest
of the day after carries that day's summary as ``DigestData.yesterday`` ("Вчерашний итог (не успел прислать)").

Iteration 4 (docs/features/iteration-4.md): an item of a span ("на этой неделе", "в октябре") is in the morning
digest on the first day of its span (the day it was saved, when that came later), on the span's last working day and
on the day the stale question asks about it (``in_morning``, §4) — no longer every day while the span runs (/today
still lists them all). The weekly review (§6, ``weekly_data``): the spans of this and next week / month, a few
"когда-нибудь" items, the week's reminders nobody reacted to, open tasks without a date.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Final, Literal, cast

from dateutil.relativedelta import relativedelta
from sqlalchemy.ext.asyncio import AsyncSession

from dumpbot.db.models import Occurrence, OnceKey
from dumpbot.domain import timeresolve
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import Agenda, DigestData, Row, WeeklyData
from dumpbot.services import items, query, stale

__all__ = [
    "DigestData",
    "WeeklyData",
    "build_digest",
    "build_weekly",
    "day_moment",
    "day_summary",
    "digest_data",
    "folded_key",
    "has_summary",
    "in_morning",
    "missed_evening",
    "move_not_done",
    "weekly_data",
]

DigestKind = Literal["morning", "evening"]
WEEKLY_SOMEDAY: Final = 5  # "когда-нибудь" items in a review
WEEKLY_UNANSWERED: Final = 10
WEEKLY_UNDATED: Final = 15
WEEKLY_PERIOD: Final = 20


async def build_digest(
    session: AsyncSession, kind: str, prefs: Prefs, now: datetime, *, ask: int | None = None
) -> DigestData | None:
    """Agenda of the local day of ``now``; None when there is nothing to say.

    morning: today's open occurrences + overdue + the spans' items due a mention today (``in_morning``) +
    yesterday's summary when its evening digest was folded into this one (skipped if all are empty); ``ask``: the
    item the stale question is about to ask about (shown even mid-span);
    evening: done / not done today + overdue (skipped if all are empty).
    ``recurring_today`` counts today's occurrences (open or done) of active series.
    """
    data = await digest_data(session, kind, prefs, now, ask=ask)
    agenda = data.agenda
    if kind == "morning":
        yesterday = data.yesterday
        empty = not agenda.open and not agenda.overdue and not agenda.period_items
        empty = empty and (yesterday is None or not has_summary(yesterday))
    else:
        empty = not agenda.open and not agenda.done and not agenda.overdue
    return None if empty else data


async def digest_data(
    session: AsyncSession, kind: str, prefs: Prefs, now: datetime, *, ask: int | None = None
) -> DigestData:
    """``build_digest`` without the emptiness check: a digest whose [✅ N] closed all of it is still re-rendered."""
    if kind not in ("morning", "evening"):
        raise ValueError(f"unknown digest kind: {kind}")
    agenda = await query.agenda(session, prefs, now)
    recurring_today = sum(1 for row in (*agenda.open, *agenda.done) if row.rrule)
    yesterday = None
    if kind == "morning":
        yesterday = await missed_evening(session, agenda.day, prefs)
        asked = await stale.asked_on(session, agenda.day, prefs) if agenda.period_items else set()
        if ask is not None:
            asked.add(ask)
        shown = [r for r in agenda.period_items if in_morning(r, agenda.day, prefs, asked)]
        agenda = replace(agenda, period_items=shown)
    return DigestData(kind=cast(DigestKind, kind), agenda=agenda, recurring_today=recurring_today, yesterday=yesterday)


def in_morning(row: Row, day: date, prefs: Prefs, asked: Collection[int] = ()) -> bool:
    """An item of a running span is in the morning digest of local ``day`` on the first day of the span (or the day
    it was saved, when that came later), on the span's last working day (its last day, when it has none) and on the
    day the stale question asks about it (``asked``)."""
    if row.item_id in asked or row.period_start is None or row.period_end is None:
        return True
    saved = row.created_at.astimezone(prefs.zone).date()
    first = max(row.period_start, saved)
    last = timeresolve.last_workday(row.period_start, row.period_end) or row.period_end
    return day in (first, last)


def folded_key(day: date) -> str:
    """``once_keys`` marker: the evening digest of local ``day`` was not sent (the bot was down) and goes into the
    morning digest of the day after."""
    return f"digest:evening-folded:{day.isoformat()}"


async def missed_evening(session: AsyncSession, day: date, prefs: Prefs) -> Agenda | None:
    """``day_summary`` of the day before ``day`` when its evening digest was folded into ``day``'s morning digest
    (``folded_key``), else None."""
    before = day - timedelta(days=1)
    if await session.get(OnceKey, folded_key(before)) is None:
        return None
    return await day_summary(session, before, prefs)


async def day_summary(session: AsyncSession, day: date, prefs: Prefs) -> Agenda:
    """The agenda of local ``day`` as its evening summary: ``open`` what is still not done, ``done`` what was done
    that day or, due that day, since then (its reminder came late, in the morning, and was ticked there)."""
    agenda = await query.agenda(session, prefs, day_moment(day, prefs))
    later = await query.done_after_day(session, prefs, day)
    return replace(agenda, done=[*agenda.done, *later])


def has_summary(agenda: Agenda) -> bool:
    """A day's summary says something: something was due that day (done or not) or done on it."""
    return bool(agenda.open or agenda.done)


def day_moment(day: date, prefs: Prefs) -> datetime:
    """A moment of the local ``day`` (its noon): ``query.agenda`` of that day."""
    return timeresolve.localize(day, time(12, 0), prefs.zone)


async def move_not_done(session: AsyncSession, day: date, prefs: Prefs, now: datetime) -> list[Occurrence]:
    """[📅 Перенести невыполненное на завтра] of the evening digest (and of the Mini App's «Сегодня»): what is already
    due and not done on the local ``day`` (open or missed, plus the overdue) goes to ``day`` + 1 at its time of day
    (``items.move_to_tomorrow``). Items still ahead keep their time and reminder — and so does one whose stated time
    is still ahead ("за день" before tomorrow's appointment notified today). Returns the moved occurrences."""
    agenda = await query.agenda(session, prefs, day_moment(day, prefs))
    ids = [
        r.occurrence_id
        for r in (*agenda.open, *agenda.overdue)
        if r.occurrence_id is not None and r.due_at is not None and r.due_at <= now
    ]
    if not ids:
        return []
    return await items.move_to_tomorrow(session, ids, prefs, now, day=day + timedelta(days=1), keep_ahead=True)


# --- the weekly review (docs/features/iteration-4.md §6) -----------------------------------------------------------


async def build_weekly(session: AsyncSession, prefs: Prefs, now: datetime) -> WeeklyData | None:
    """``weekly_data``; None when it is empty (an empty review is not sent)."""
    data = await weekly_data(session, prefs, now)
    return None if data.empty else data


async def weekly_data(session: AsyncSession, prefs: Prefs, now: datetime) -> WeeklyData:
    """The weekly review of the local day of ``now`` (its week Mon–Sun): items of a span of this or next week (a
    week) / this or next month (a month, a season); up to ``WEEKLY_SOMEDAY`` "когда-нибудь" items (the ones reviews
    before that day showed least recently first: a re-render lists the same ones); the reminders sent this week that
    got no reaction and whose instance is still not done (``stale.unanswered_reminders``), by time; the open tasks
    without a date. An item is listed once, in the first of these sections."""
    tz = prefs.zone
    day = now.astimezone(tz).date()
    monday = day - timedelta(days=day.weekday())
    first = day.replace(day=1)
    period = await query.review_period_rows(
        session,
        weeks=(monday, monday + timedelta(days=13)),
        months=(first, first + relativedelta(months=2) - timedelta(days=1)),
        limit=WEEKLY_PERIOD,
    )
    midnight = timeresolve.localize(day, time(0), tz).astimezone(UTC)
    someday, someday_total = await query.someday_for_review(session, WEEKLY_SOMEDAY, before=midnight)
    start = timeresolve.localize(monday, time(0), tz).astimezone(UTC)
    end = timeresolve.localize(monday + timedelta(days=7), time(0), tz).astimezone(UTC)
    refs = await stale.unanswered_reminders(session, start, end)
    unanswered = [r for r in await query.list_rows(session, refs) if r is not None]
    unanswered.sort(key=lambda r: (r.due_at is None, r.due_at or end, r.item_id))
    undated = await query.undated_tasks(session, WEEKLY_UNDATED)
    seen: set[int] = set()
    sections = [_once(rows, seen) for rows in (period, someday, unanswered[:WEEKLY_UNANSWERED], undated)]
    return WeeklyData(day, sections[0], sections[1], someday_total, sections[2], sections[3])


def _once(rows: list[Row], seen: set[int]) -> list[Row]:
    """``rows`` whose items no earlier section listed (``seen`` takes theirs)."""
    fresh = [r for r in rows if r.item_id not in seen]
    seen.update(r.item_id for r in fresh)
    return fresh
