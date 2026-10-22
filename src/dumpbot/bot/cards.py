"""Item cards with everything they show, and the follow-ups of checklist changes (routers and the job processor).

``views.item_card`` is pure; these helpers load its ``CardExtras`` (checklist, related items, parent) with
``query.card_extras``. ``after_steps`` runs after a commit that changed checklists (``items.take_parent_events``):
other cards of the parent are redrawn and "🎉 Все шаги выполнены — «…» закрыт" is sent (docs/features/iteration-2.md
§C2).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from dumpbot.bot import sender, views
from dumpbot.db.models import BotMessage
from dumpbot.services import items, query

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from dumpbot.context import AppContext
    from dumpbot.db.models import Item, Occurrence, Reminder
    from dumpbot.domain.prefs import Prefs
    from dumpbot.dto import PatchResult

__all__ = ["after_steps", "card_view", "patch_card", "render_card"]

log = logging.getLogger(__name__)

View = views.View


async def card_view(
    session: AsyncSession, item: Item, prefs: Prefs, now: datetime, *, heard: str | None = None
) -> View:
    """Fresh card of an item: current occurrence and its pending reminders (deleted -> ``deleted_card``); ``heard``:
    see ``render_card``."""
    if item.status == "deleted":
        return views.deleted_card(item)
    occ = await items.current_occurrence(session, item.id)
    reminders = await items.pending_reminders(session, occ.id) if occ is not None else []
    return await render_card(session, item, occ, reminders, prefs, now, heard=heard)


async def render_card(
    session: AsyncSession,
    item: Item,
    occ: Occurrence | None,
    reminders: Sequence[Reminder],
    prefs: Prefs,
    now: datetime,
    warnings: Sequence[str] = (),
    *,
    heard: str | None = None,
) -> View:
    """``views.item_card`` with what the card shows besides the item (checklist, related items, parent, the transcript
    of the voice note it came from); ``heard`` replaces that transcript (the card answers another voice note)."""
    extras = await query.card_extras(session, item, with_heard=heard is None)
    if heard is not None:
        extras = replace(extras, heard=heard)
    return views.item_card(item, occ, reminders, prefs, now, warnings=warnings, extras=extras)


async def patch_card(
    session: AsyncSession, result: PatchResult, prefs: Prefs, now: datetime, *, heard: str | None = None
) -> View:
    """Card after ``items.apply_patch`` (``deleted_card`` for a deleted item); ``heard``: see ``render_card``."""
    if result.deleted or result.item.status == "deleted":
        return views.deleted_card(result.item)
    return await render_card(
        session, result.item, result.occurrence, result.reminders, prefs, now, warnings=result.warnings, heard=heard
    )


async def after_steps(
    ctx: AppContext,
    events: items.ParentEvents,
    prefs: Prefs,
    now: datetime,
    *,
    shown: int | None = None,
    silent: bool = False,
) -> None:
    """After a commit that changed checklists: redraw the newest card of each parent (not message ``shown``, which
    the caller redraws itself) and send "🎉 Все шаги выполнены — «…» закрыт" for each parent its last step closed.
    Best effort: the change is committed already, a failure here is only logged."""
    if not events.changed and not events.closed:
        return
    try:
        async with ctx.sessionmaker() as session:
            redraw: list[tuple[int, View]] = []
            for parent_id in sorted(events.changed):
                message_id = await _latest_card(session, ctx, parent_id)
                parent = await items.get_item(session, parent_id)
                if message_id is not None and message_id != shown and parent is not None:
                    redraw.append((message_id, await card_view(session, parent, prefs, now)))
            closed = [p for p in [await items.get_item(session, i) for i in events.closed] if p is not None]
            await session.commit()
        for message_id, (text, markup) in redraw:
            await sender.edit(ctx, message_id, text, markup)
        for parent in closed:
            text = views.steps_closed_text(parent.title)
            await sender.send(ctx, text, purpose="notice", item_id=parent.id, silent=silent)
    except Exception as e:  # the change is committed; only its echo failed
        log.warning("checklist follow-up failed: %s", type(e).__name__)


async def _latest_card(session: AsyncSession, ctx: AppContext, item_id: int) -> int | None:
    """Message id of the newest card of the item in the owner's chat."""
    stmt = (
        select(BotMessage.message_id)
        .where(BotMessage.chat_id == ctx.chat_id, BotMessage.item_id == item_id, BotMessage.purpose == "card")
        .order_by(BotMessage.created_at.desc(), BotMessage.message_id.desc())
        .limit(1)
    )
    return await session.scalar(stmt)
