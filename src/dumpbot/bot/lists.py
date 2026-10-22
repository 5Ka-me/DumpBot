"""Numbered lists with [✅ N] / [↩️ N] buttons: /today, /tasks, the digests (docs/features/iteration-3.md §2) and the
weekly review (docs/features/iteration-4.md §6).

A first render numbers the open lines (``views.number_lines``) and puts a ``ListCb`` button under each; those
buttons are the list's snapshot. After a press (the callbacks router closes or reopens the line, without the AI) the
message is rendered again from its buttons: every numbered line keeps its section and number and shows its state now
(✅ struck through with [↩️ N], or open with [✅ N]); a deleted item's line is gone; rows the list has now but the
snapshot lacks (added since, past the first 20) follow without a number. A message whose buttons the callback does
not carry (Telegram sends an inaccessible message for very old ones) is rendered afresh. A /tasks line stands for the
task: when its pinned instance of a series was left missed, it is the item's current instance now.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Final

from aiogram.types import Message

from dumpbot.bot import views
from dumpbot.bot.cbdata import LIST_EVENING, LIST_MORNING, LIST_TASKS, LIST_TODAY, LIST_WEEKLY, ListCb
from dumpbot.services import digest, query

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from aiogram.types import MaybeInaccessibleMessage
    from sqlalchemy.ext.asyncio import AsyncSession

    from dumpbot.domain.prefs import Prefs
    from dumpbot.dto import Row

__all__ = ["TASKS_LIMIT", "line_rows", "render", "snapshot"]

TASKS_LIMIT: Final = 30  # rows of /tasks (the first LIST_BUTTONS_MAX get a button)


def snapshot(message: MaybeInaccessibleMessage | None) -> list[ListCb]:
    """The numbered lines of a list message: the ``ListCb`` of its buttons, in order ([] when Telegram did not send
    the message, e.g. a very old one)."""
    if not isinstance(message, Message) or message.reply_markup is None:
        return []
    prefix = f"{ListCb.__prefix__}:"
    refs: list[ListCb] = []
    for row in message.reply_markup.inline_keyboard:
        for btn in row:
            if btn.callback_data and btn.callback_data.startswith(prefix):
                try:
                    refs.append(ListCb.unpack(btn.callback_data))
                except (TypeError, ValueError):  # not ours any more (an older layout): the line is left out
                    continue
    return refs


async def render(
    session: AsyncSession,
    kind: str,
    day: date | None,
    prefs: Prefs,
    now: datetime,
    refs: Sequence[ListCb] = (),
    *,
    drop: Collection[int] = (),
) -> views.View:
    """List ``kind`` of local ``day`` (None: of ``now``) as it is now; ``refs`` (a ``snapshot``) keep the numbers of
    the message being re-rendered; numbered lines whose occurrence is in ``drop`` (moved to tomorrow by the evening
    digest's button) are left out."""
    if kind == LIST_TASKS:
        rows = await query.open_tasks(session, prefs, now, limit=TASKS_LIMIT)
        lines = await _pinned(session, kind, views.tasks_sections(rows), refs, drop)
        return views.tasks_list(rows, prefs, now, lines=lines)
    at = digest.day_moment(day, prefs) if day is not None else now
    if kind == LIST_TODAY:
        agenda = await query.agenda(session, prefs, at)
        lines = await _pinned(session, kind, views.today_sections(agenda), refs, drop)
        return views.today_list(agenda, prefs, now, lines=lines)
    if kind == LIST_MORNING:
        data = await digest.digest_data(session, "morning", prefs, at)
        lines = await _pinned(session, kind, views.morning_sections(data), refs, drop)
        return views.morning_digest(data, prefs, now, lines=lines)
    if kind == LIST_EVENING:
        data = await digest.digest_data(session, "evening", prefs, at)
        lines = await _pinned(session, kind, views.evening_sections(data.agenda, now), refs, drop)
        return views.evening_digest(data, prefs, now, lines=lines)
    if kind == LIST_WEEKLY:
        weekly = await digest.weekly_data(session, prefs, at)
        lines = await _pinned(session, kind, views.weekly_sections(weekly), refs, drop)
        return views.weekly_review(weekly, prefs, now, lines=lines)
    raise ValueError(f"unknown list: {kind}")


async def line_rows(session: AsyncSession, kind: str, refs: Sequence[ListCb]) -> list[Row | None]:
    """The rows of these numbered lines of a ``kind`` list as they are now (``query.list_rows``)."""
    return await query.list_rows(session, [(ref.o or None, ref.i) for ref in refs], current=kind == LIST_TASKS)


async def _pinned(
    session: AsyncSession,
    kind: str,
    sections: Mapping[str, Sequence[Row]],
    refs: Sequence[ListCb],
    drop: Collection[int] = (),
) -> views.ListLines | None:
    """The lines of a re-render (None: a first render, numbered afresh)."""
    if not refs:
        return None
    rows = await line_rows(session, kind, refs)
    pinned = [
        (ref.s, ref.n, row)
        for ref, row in zip(refs, rows, strict=True)
        if row is not None and (row.occurrence_id is None or row.occurrence_id not in drop)
    ]
    return views.pin_lines(kind, sections, pinned)
