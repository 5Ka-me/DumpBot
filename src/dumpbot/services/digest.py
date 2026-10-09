"""Data for the morning / evening digests (architecture.md §4.15). Rendering lives in ``bot.views``.

An evening digest the bot was down for goes into the next morning digest (docs/features/iteration-3.md §5): the
scheduler marks its day with ``folded_key``, and the morning digest of the day after carries that day's agenda as
``DigestData.yesterday`` ("Вчерашний итог (не успел прислать)").
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from dumpbot.db.models import Occurrence, OnceKey
from dumpbot.domain import timeresolve
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import Agenda, DigestData
from dumpbot.services import items, query

__all__ = ["DigestData", "build_digest", "day_moment", "digest_data", "folded_key", "missed_evening", "move_not_done"]

DigestKind = Literal["morning", "evening"]


async def build_digest(session: AsyncSession, kind: str, prefs: Prefs, now: datetime) -> DigestData | None:
    """Agenda of the local day of ``now``; None when there is nothing to say.

    morning: today's open occurrences + overdue + the running periods' items + yesterday's summary when its evening
    digest was folded into this one (skipped if all are empty);
    evening: done / not done today + overdue (skipped if all are empty).
    ``recurring_today`` counts today's occurrences (open or done) of active series.
    """
    data = await digest_data(session, kind, prefs, now)
    agenda = data.agenda
    if kind == "morning":
        yesterday = data.yesterday
        empty = not agenda.open and not agenda.overdue and not agenda.period_items
        empty = empty and (yesterday is None or (not yesterday.open and not yesterday.done))
    else:
        empty = not agenda.open and not agenda.done and not agenda.overdue
    return None if empty else data


async def digest_data(session: AsyncSession, kind: str, prefs: Prefs, now: datetime) -> DigestData:
    """``build_digest`` without the emptiness check: a digest whose [✅ N] closed all of it is still re-rendered."""
    if kind not in ("morning", "evening"):
        raise ValueError(f"unknown digest kind: {kind}")
    agenda = await query.agenda(session, prefs, now)
    recurring_today = sum(1 for row in (*agenda.open, *agenda.done) if row.rrule)
    yesterday = await missed_evening(session, agenda.day, prefs) if kind == "morning" else None
    return DigestData(kind=cast(DigestKind, kind), agenda=agenda, recurring_today=recurring_today, yesterday=yesterday)


def folded_key(day: date) -> str:
    """``once_keys`` marker: the evening digest of local ``day`` was not sent (the bot was down) and goes into the
    morning digest of the day after."""
    return f"digest:evening-folded:{day.isoformat()}"


async def missed_evening(session: AsyncSession, day: date, prefs: Prefs) -> Agenda | None:
    """The agenda of the day before ``day`` when its evening digest was folded into ``day``'s morning digest
    (``folded_key``), else None. Its ``open`` rows are what was not done (``done``: what was)."""
    before = day - timedelta(days=1)
    if await session.get(OnceKey, folded_key(before)) is None:
        return None
    return await query.agenda(session, prefs, day_moment(before, prefs))


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
