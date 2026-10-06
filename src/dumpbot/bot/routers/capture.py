"""Catch-all: every other owner message is saved as a raw message and queued for AI processing.

Fast and AI-free: save (idempotent per chat/message id) + enqueue in one transaction -> react 👀 -> wake the job
worker. A leading "?" forces query mode (the text is stored as is; the processor strips the "?"); a reply to a bot
message that is linked to an item (card, reminder) becomes an edit of that item. Edited messages are not handled
at all (editing the source message in Telegram does not change the item).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from aiogram import F, Router
from aiogram.enums import ChatType, ContentType
from aiogram.types import Message

from dumpbot import clock
from dumpbot.bot import sender
from dumpbot.services import ingest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from dumpbot.context import AppContext

log = logging.getLogger(__name__)

# Content worth saving; service messages (pins, auto-delete timer changes…) are ignored. "unknown" (a type newer
# than this aiogram) is kept: better an "other" item than a lost message.
CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        ContentType.TEXT,
        ContentType.PHOTO,
        ContentType.DOCUMENT,
        ContentType.VOICE,
        ContentType.AUDIO,
        ContentType.VIDEO,
        ContentType.VIDEO_NOTE,
        ContentType.ANIMATION,
        ContentType.STICKER,
        ContentType.LOCATION,
        ContentType.VENUE,
        ContentType.CONTACT,
        ContentType.POLL,
        ContentType.DICE,
        ContentType.STORY,
        ContentType.LIVE_PHOTO,
        ContentType.PAID_MEDIA,
        ContentType.CHECKLIST,
        ContentType.RICH_MESSAGE,
        ContentType.GAME,
        ContentType.UNKNOWN,
    }
)


def make_router() -> Router:
    router = Router(name="capture")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(capture, F.content_type.in_(CONTENT_TYPES))
    return router


def is_force_query(message: Message) -> bool:
    return (message.text or message.caption or "").lstrip().startswith("?")


async def capture(message: Message, ctx: AppContext) -> None:
    force_query = is_force_query(message)
    async with ctx.sessionmaker() as session:
        reply_item_id = await reply_item(session, ctx, message)
        raw, created = await ingest.save_incoming(
            session, message, force_query=force_query, reply_item_id=reply_item_id
        )
        if created:
            await ingest.enqueue_processing(session, raw, clock.utcnow())
        await session.commit()
    if not created:  # a redelivered update: it was queued (and reacted to) the first time
        log.info("message %s was already saved", message.message_id)
        return
    await sender.react(ctx, message.message_id, sender.REACT_SEEN)
    ctx.jobs_wakeup.set()


async def reply_item(session: AsyncSession, ctx: AppContext, message: Message) -> int | None:
    """Item of the bot message this one replies to (cards and reminders are recorded with their item)."""
    replied = message.reply_to_message
    if replied is None or (replied.from_user is not None and not replied.from_user.is_bot):
        return None
    row = await sender.lookup(session, ctx.chat_id, replied.message_id)
    return row.item_id if row is not None else None
