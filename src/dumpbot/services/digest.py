"""Data for the morning / evening digests (architecture.md §4.15). Rendering lives in ``bot.views``."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession

from dumpbot.domain.prefs import Prefs
from dumpbot.dto import DigestData
from dumpbot.services import query

__all__ = ["DigestData", "build_digest"]

DigestKind = Literal["morning", "evening"]


async def build_digest(session: AsyncSession, kind: str, prefs: Prefs, now: datetime) -> DigestData | None:
    """Agenda of the local day of ``now``; None when there is nothing to say.

    morning: today's open occurrences + overdue + the running periods' items (skipped if all are empty);
    evening: done / not done today + overdue (skipped if all are empty).
    ``recurring_today`` counts today's occurrences (open or done) of active series.
    """
    if kind not in ("morning", "evening"):
        raise ValueError(f"unknown digest kind: {kind}")
    agenda = await query.agenda(session, prefs, now)
    if kind == "morning":
        empty = not agenda.open and not agenda.overdue and not agenda.period_items
    else:
        empty = not agenda.open and not agenda.done and not agenda.overdue
    if empty:
        return None
    recurring_today = sum(1 for row in (*agenda.open, *agenda.done) if row.rrule)
    return DigestData(kind=cast(DigestKind, kind), agenda=agenda, recurring_today=recurring_today)
