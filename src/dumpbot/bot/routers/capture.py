"""Catch-all: every other owner message is saved as a raw message and queued for AI processing.

Fast and AI-free: save (idempotent per chat/message id) + enqueue in one transaction -> react 👀 -> wake the job
worker. A leading "?" forces query mode (the text is stored as is; the processor strips the "?"). A reply goes by
what it answers (``reply_target``):
- a bot message linked to an item (card, reminder) -> an edit of that item (job "edit");
- the owner's own message -> an edit of the item saved from it; several items -> "Какую запись изменить?";
- the clarifying question 🤔 (text) -> its answer (job "clarify", like an option button);
- "Какую запись изменить?" with a number -> that item (job "edit", like its button);
- the /search prompt (text) -> the search; the /ask prompt -> the question (query mode).
An answer that no longer applies (the question was answered with a button) is saved like any message. Edited
messages are not handled at all (editing the source message in Telegram does not change the item).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from aiogram import F, Router
from aiogram.enums import ChatType, ContentType
from aiogram.types import Message

from dumpbot import clock
from dumpbot.bot import sender, views
from dumpbot.bot.routers import commands
from dumpbot.services import ingest, query
from dumpbot.workers import jobs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from dumpbot.context import AppContext
    from dumpbot.db.models import RawMessage
    from dumpbot.dto import Row

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


@dataclass(frozen=True)
class Reply:
    """What a reply answers; the default (nothing) means a plain new message."""

    item_id: int | None = None  # edit this item (job "edit")
    candidates: tuple[Row, ...] = ()  # the owner's own message gave several items: ask which one
    clarify_raw: int | None = None  # answer to the clarifying question about this raw message
    pick: tuple[int, int] | None = None  # (raw id of the edit, item id) picked from the candidates by number
    search: bool = False  # the /search prompt: the text is the search
    question: bool = False  # the /ask prompt: the message is a question


def make_router() -> Router:
    router = Router(name="capture")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(capture, F.content_type.in_(CONTENT_TYPES))
    return router


def is_force_query(message: Message) -> bool:
    return (message.text or message.caption or "").lstrip().startswith("?")


async def capture(message: Message, ctx: AppContext) -> None:
    async with ctx.sessionmaker() as session:
        reply = await reply_target(session, ctx, message)
        await session.commit()
    if reply.search:
        await commands.send_search(ctx, (message.text or "").strip())
        return
    if reply.clarify_raw is not None or reply.pick is not None:
        if await _answer(ctx, message, reply):
            return
    elif len(reply.candidates) > 1:
        await _ask_which(ctx, message, reply.candidates)
        return
    await save(ctx, message, force_query=reply.question, reply_item_id=reply.item_id)


async def save(
    ctx: AppContext, message: Message, *, force_query: bool = False, reply_item_id: int | None = None
) -> None:
    """Save the message (``?`` at the start forces query mode too), queue its job, react 👀, wake the worker."""
    async with ctx.sessionmaker() as session:
        raw, created = await ingest.save_incoming(
            session, message, force_query=force_query or is_force_query(message), reply_item_id=reply_item_id
        )
        if created:
            await ingest.enqueue_processing(session, raw, clock.utcnow())
        await session.commit()
    if not created:  # a redelivered update: it was queued (and reacted to) the first time
        log.info("message %s was already saved", message.message_id)
        return
    await sender.react(ctx, message.message_id, sender.REACT_SEEN)
    ctx.jobs_wakeup.set()


async def reply_target(session: AsyncSession, ctx: AppContext, message: Message) -> Reply:
    """What the message replies to: an item (cards and reminders are recorded with their item; the owner's own
    message by the items saved from it), or a bot message waiting for an answer (``bot_messages.purpose``)."""
    replied = message.reply_to_message
    if replied is None:
        return Reply()
    if replied.from_user is not None and not replied.from_user.is_bot:
        rows = await query.items_of_raw(session, await ingest.message_raw_ids(session, ctx.chat_id, replied.message_id))
        if len(rows) == 1:
            return Reply(item_id=rows[0].item_id)
        return Reply(candidates=tuple(rows))
    row = await sender.lookup(session, ctx.chat_id, replied.message_id)
    if row is None:
        return Reply()
    if row.item_id is not None:
        return Reply(item_id=row.item_id)
    if row.purpose == sender.PURPOSE_QUESTION:
        return Reply(question=True)
    text = (message.text or "").strip()
    if not text or is_force_query(message):  # a photo, a "?" question: a message of its own
        return Reply()
    if row.purpose == sender.PURPOSE_SEARCH:
        return Reply(search=True)
    if row.raw_message_id is None:
        return Reply()
    if row.purpose == sender.PURPOSE_ASK and await ingest.was_asked(session, row.raw_message_id):
        return Reply(clarify_raw=row.raw_message_id)
    if row.purpose == sender.PURPOSE_PICK:
        item_id = await ingest.picked_item(session, row.raw_message_id, text)
        if item_id is not None:
            return Reply(pick=(row.raw_message_id, item_id))
    return Reply()


async def _answer(ctx: AppContext, message: Message, reply: Reply) -> bool:
    """A typed answer to the bot's question or candidate list: queue the job its button would queue (same dedupe
    key: one answer wins), keep the reply as a done raw message, mark the answer on the question and react 👌.
    False when the question was answered already: the message is then saved as a new one."""
    text = (message.text or "").strip()
    if reply.clarify_raw is not None:
        raw_id = reply.clarify_raw
        kind, payload, key = "clarify", {"raw_id": raw_id, "option": text}, f"clarify:{raw_id}"
    elif reply.pick is not None:
        raw_id, item_id = reply.pick
        # The same key as PickCb's (not "edit:{raw_id}", which belongs to a reply-to-card job of that raw).
        kind, payload, key = "edit", {"raw_id": raw_id, "item_id": item_id}, f"pick:{raw_id}:{item_id}"
    else:
        return False
    async with ctx.sessionmaker() as session:
        if await jobs.enqueue(session, kind, payload, dedupe_key=key) is None:
            return False  # rolled back with the session
        raw, created = await ingest.save_incoming(session, message)
        if created:
            _done(raw)
        await session.commit()
    if created:
        replied = message.reply_to_message
        if replied is not None and (replied.text or replied.caption):
            await sender.edit(ctx, replied.message_id, views.fit(f"{replied.html_text}\n\n✅ {views.q(text)}"))
        await sender.react(ctx, message.message_id, sender.REACT_SAVED)
        ctx.jobs_wakeup.set()
    return True


async def _ask_which(ctx: AppContext, message: Message, rows: tuple[Row, ...]) -> None:
    """A reply to the owner's own message that gave several items: "Какую запись изменить?" for this reply (its
    buttons and a number reply queue job "edit" of this raw message with the chosen item)."""
    ids = [r.item_id for r in rows[: views.CANDIDATES_MAX]]
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        raw, created = await ingest.save_incoming(session, message)
        if created:
            raw.raw_json = {**(raw.raw_json or {}), ingest.CANDIDATES_KEY: ids}
            _done(raw)
        await session.commit()
    if not created:  # a redelivered update: asked the first time
        return
    text, markup = views.edit_candidates(rows, raw.id, prefs, clock.utcnow())
    await sender.send(
        ctx, text, markup, reply_to=message.message_id, purpose=sender.PURPOSE_PICK, raw_message_id=raw.id
    )
    await sender.react(ctx, message.message_id, sender.REACT_ASK)


def _done(raw: RawMessage) -> None:
    raw.status = "done"
    raw.processed_at = clock.utcnow()
