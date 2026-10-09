"""Data for the morning / evening digests (architecture.md §4.15). Rendering lives in ``bot.views``."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from dumpbot.db.models import Occurrence
from dumpbot.domain import timeresolve
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import DigestData
from dumpbot.services import items, query

__all__ = ["DigestData", "build_digest", "day_moment", "digest_data", "move_not_done"]

DigestKind = Literal["morning", "evening"]


async def build_digest(session: AsyncSession, kind: str, prefs: Prefs, now: datetime) -> DigestData | None:
    """Agenda of the local day of ``now``; None when there is nothing to say.

    morning: today's open occurrences + overdue + the running periods' items (skipped if all are empty);
    evening: done / not done today + overdue (skipped if all are empty).
    ``recurring_today`` counts today's occurrences (open or done) of active series.
    """
    data = await digest_data(session, kind, prefs, now)
    agenda = data.agenda
    if kind == "morning":
        empty = not agenda.open and not agenda.overdue and not agenda.period_items
    else:
        empty = not agenda.open and not agenda.done and not agenda.overdue
    return None if empty else data


async def digest_data(session: AsyncSession, kind: str, prefs: Prefs, now: datetime) -> DigestData:
    """``build_digest`` without the emptiness check: a digest whose [✅ N] closed all of it is still re-rendered."""
    if kind not in ("morning", "evening"):
        raise ValueError(f"unknown digest kind: {kind}")
    agenda = await query.agenda(session, prefs, now)
    recurring_today = sum(1 for row in (*agenda.open, *agenda.done) if row.rrule)
    return DigestData(kind=cast(DigestKind, kind), agenda=agenda, recurring_today=recurring_today)


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
