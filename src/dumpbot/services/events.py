"""The events journal (docs/features/iteration-4.md §5): what the owner did and what the bot showed.

One ``events`` row per thing that happened (``db.models.Event``):

=================  ======================================  =========================================================
type               written by                              source / ids / data
=================  ======================================  =========================================================
``command``        ``bot.journal.CommandJournal``          data ``{"name": "today"}`` (the command only, no text)
``button``         ``bot.journal.ButtonJournal``           source = the purpose of the bot message the button is on
                                                           (``reminder`` / ``digest`` / ``list`` / ``card`` / …);
                                                           item / occurrence from the callback data;
                                                           data ``{"action": "o:done", "cb": "o:done:12"}``
``miniapp``        ``web.server.journal_middleware``       source ``miniapp``; item = ``/api/items/{id}``,
                                                           ``/api/steps/{id}``; data ``{"route": "POST
                                                           /api/items/{id}/done", "status": 200}``
``reminder_sent``  ``workers.scheduler``                   source ``reminder``; item, occurrence;
                                                           data ``{"reminder": id, "late_min": N, "held": bool}``
``digest_item``    ``workers.scheduler``                   source ``digest``; a line of a sent digest: item,
                                                           occurrence; data ``{"digest": "morning", "sec": "o"}``
                                                           (``bot.views.SEC_*``)
``stale_ask``      §4 (the "висит 15 дней" question)
=================  ======================================  =========================================================

``data`` holds codes and ids only, never message text. Writing is best effort and comes after the action it
describes (the reply, the toast, the sent reminder): ``record`` uses its own session, and any failure is logged by
type and swallowed — the journal never breaks or delays what it journals.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dumpbot import clock
from dumpbot.db.models import BotMessage, Event, Occurrence

__all__ = [
    "BUTTON",
    "COMMAND",
    "DIGEST_ITEM",
    "MINIAPP",
    "REMINDER_SENT",
    "SRC_DIGEST",
    "SRC_MINIAPP",
    "SRC_REMINDER",
    "STALE_ASK",
    "Entry",
    "record",
]

log = logging.getLogger(__name__)

COMMAND: Final = "command"
BUTTON: Final = "button"
MINIAPP: Final = "miniapp"
REMINDER_SENT: Final = "reminder_sent"
DIGEST_ITEM: Final = "digest_item"
STALE_ASK: Final = "stale_ask"

SRC_REMINDER: Final = "reminder"
SRC_DIGEST: Final = "digest"
SRC_MINIAPP: Final = "miniapp"

_SHORT: Final = 16  # events.type / events.source are String(16)


@dataclass(frozen=True)
class Entry:
    """One row to write. ``message``: ``(chat_id, message_id)`` of a bot message whose ``bot_messages.purpose``
    becomes the source (a button press: where the button was); an occurrence without an item gets the occurrence's
    item — both looked up by the insert itself."""

    type: str
    source: str | None = None
    item_id: int | None = None
    occurrence_id: int | None = None
    data: dict[str, Any] | None = None
    message: tuple[int, int] | None = None


async def record(sessionmaker: async_sessionmaker[AsyncSession], *entries: Entry) -> None:
    """Write ``entries`` in one transaction of its own. Never raises an ``Exception``: a failure (the table missing,
    the DB down) is only logged."""
    if not entries:
        return
    try:
        async with sessionmaker() as session:
            await _insert(session, entries)
            await session.commit()
    except Exception as e:
        log.warning("could not write %d journal event(s) (%s): %s", len(entries), entries[0].type, type(e).__name__)


async def _insert(session: AsyncSession, entries: Sequence[Entry]) -> None:
    """One statement for plain entries (a digest's lines); one per entry, in order, when values are looked up."""
    now = clock.utcnow()
    if not any(_looks_up(e) for e in entries):
        await session.execute(insert(Event), [_values(e, now) for e in entries])
        return
    for entry in entries:
        values = _values(entry, now)
        if entry.message is not None and entry.source is None:
            chat_id, message_id = entry.message
            values["source"] = (
                select(BotMessage.purpose)
                .where(BotMessage.chat_id == chat_id, BotMessage.message_id == message_id)
                .scalar_subquery()
            )
        if not entry.item_id and entry.occurrence_id:  # the item of the occurrence, so that it is found by item
            values["item_id"] = select(Occurrence.item_id).where(Occurrence.id == entry.occurrence_id).scalar_subquery()
        await session.execute(insert(Event).values(**values))


def _looks_up(entry: Entry) -> bool:
    """The row needs a value from another table: the purpose of the bot message, the item of the occurrence."""
    return (entry.message is not None and entry.source is None) or (not entry.item_id and bool(entry.occurrence_id))


def _values(entry: Entry, now: Any) -> dict[str, Any]:
    return {
        "created_at": now,
        "type": entry.type[:_SHORT],
        "source": entry.source[:_SHORT] if entry.source else None,
        "item_id": entry.item_id or None,
        "occurrence_id": entry.occurrence_id or None,
        "data": entry.data or None,
    }
