"""Sending helpers: every bot message goes to the owner's chat in HTML and is recorded in ``bot_messages``.

``send`` retries Telegram flood control (``TelegramRetryAfter``) up to ``MAX_RETRIES`` times; other errors propagate
to the caller (the scheduler / job queue retries them). ``edit`` and ``react`` never fail on "nothing to do" cases.
Logs never contain message text or Telegram file URLs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Final

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import ForceReply, InlineKeyboardMarkup, ReactionTypeEmoji, ReplyParameters
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from dumpbot.db.models import BotMessage

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from dumpbot.context import AppContext

log = logging.getLogger(__name__)

MAX_RETRIES: Final = 3
# Longer flood waits are not slept through here: the caller's queue retries later instead of blocking.
MAX_RETRY_WAIT_S: Final = 60.0

# Reaction emoji (all from Telegram's allowed ReactionTypeEmoji list).
REACT_SEEN: Final = "👀"  # received
REACT_SAVED: Final = "👌"  # saved
REACT_DEFERRED: Final = "✍"  # saved, AI unavailable / budget exhausted -> processed later
REACT_ASK: Final = "🤔"  # needs an answer
REACT_FAILED: Final = "😢"  # could not be processed

# bot_messages.purpose of messages that wait for the owner's answer: a text reply to them is routed by the capture
# router (not saved as a new note).
PURPOSE_ASK: Final = "ask"  # clarifying question 🤔 (raw_message_id = the asked message; its raw_json[ASK_KEY])
PURPOSE_PICK: Final = "pick"  # "Какую запись изменить?" (raw_message_id = the edit; its raw_json[CANDIDATES_KEY])
PURPOSE_SEARCH: Final = "search_prompt"  # /search without text: the reply is the search text
PURPOSE_QUESTION: Final = "ask_prompt"  # /ask without text: the reply is the question

_NOT_MODIFIED: Final = "message is not modified"
_CANT_EDIT: Final[tuple[str, ...]] = (
    "message to edit not found",
    "message can't be edited",
    "message_id_invalid",
    "message not found",
)
_PURPOSE_MAX: Final = 16  # bot_messages.purpose is String(16)


async def _sleep(seconds: float) -> None:  # separate hook so tests can skip real waiting
    await asyncio.sleep(seconds)


async def _with_retry[T](call: Callable[[], Awaitable[T]], what: str) -> T:
    """Run ``call``; on flood control wait ``retry_after`` and retry up to MAX_RETRIES times."""
    attempt = 0
    while True:
        try:
            return await call()
        except TelegramRetryAfter as e:
            attempt += 1
            if attempt > MAX_RETRIES or e.retry_after > MAX_RETRY_WAIT_S:
                raise
            log.warning("%s: flood control, retry %d/%d in %s s", what, attempt, MAX_RETRIES, e.retry_after)
            await _sleep(e.retry_after + 0.5)


async def send(
    ctx: AppContext,
    text: str,
    markup: InlineKeyboardMarkup | ForceReply | None = None,
    *,
    reply_to: int | None = None,
    silent: bool = False,
    purpose: str,
    item_id: int | None = None,
    occurrence_id: int | None = None,
    raw_message_id: int | None = None,
) -> int:
    """Send an HTML message to the owner and record it in ``bot_messages``. Returns the Telegram message_id.

    ``reply_to`` is a Telegram message_id in the owner's chat; the message is still sent if it was deleted.
    ``markup`` is inline buttons, or ``ForceReply`` for a prompt whose answer is a reply to it.
    """
    reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
    msg = await _with_retry(
        lambda: ctx.bot.send_message(
            chat_id=ctx.chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            reply_parameters=reply,
            disable_notification=True if silent else None,
        ),
        "send_message",
    )
    await _record(
        ctx,
        message_id=msg.message_id,
        purpose=purpose,
        item_id=item_id,
        occurrence_id=occurrence_id,
        raw_message_id=raw_message_id,
    )
    log.debug("sent %s message %s (item=%s, occ=%s)", purpose, msg.message_id, item_id, occurrence_id)
    return msg.message_id


async def _record(
    ctx: AppContext,
    *,
    message_id: int,
    purpose: str,
    item_id: int | None,
    occurrence_id: int | None,
    raw_message_id: int | None,
) -> None:
    """Store the message -> item mapping in its own session. The message is already out, so a DB error is only
    logged: failing here would make callers retry and send a duplicate."""
    stmt = (
        insert(BotMessage)
        .values(
            chat_id=ctx.chat_id,
            message_id=message_id,
            purpose=purpose[:_PURPOSE_MAX],
            item_id=item_id,
            occurrence_id=occurrence_id,
            raw_message_id=raw_message_id,
        )
        .on_conflict_do_nothing(index_elements=[BotMessage.chat_id, BotMessage.message_id])
    )
    try:
        async with ctx.sessionmaker() as session:
            await session.execute(stmt)
            await session.commit()
    except SQLAlchemyError:
        log.exception("could not record bot message %s (purpose=%s)", message_id, purpose)


async def edit(ctx: AppContext, message_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit a bot message in place (``markup=None`` removes the keyboard).

    "message is not modified" is ignored; a message that is gone or can no longer be edited is logged at INFO and
    skipped. Other Telegram errors propagate.
    """
    try:
        await _with_retry(
            lambda: ctx.bot.edit_message_text(
                chat_id=ctx.chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            ),
            "edit_message_text",
        )
    except TelegramBadRequest as e:
        desc = (e.message or "").lower()
        if _NOT_MODIFIED in desc:
            log.debug("message %s not modified", message_id)
            return
        if any(s in desc for s in _CANT_EDIT):
            log.info("message %s can't be edited: %s", message_id, e.message)
            return
        raise


async def react(ctx: AppContext, message_id: int, emoji: str) -> None:
    """Best effort: set the bot's single reaction on a message (replaces the previous one). Never raises."""
    try:
        await ctx.bot.set_message_reaction(
            chat_id=ctx.chat_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception as e:  # reactions are cosmetic: never fail the caller
        log.info("reaction on message %s failed: %s", message_id, type(e).__name__)


async def lookup(session: AsyncSession, chat_id: int, message_id: int) -> BotMessage | None:
    """The recorded bot message (purpose, item, occurrence) for a Telegram message, if any."""
    return await session.get(BotMessage, (chat_id, message_id))
