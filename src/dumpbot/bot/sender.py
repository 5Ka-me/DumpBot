"""Sending helpers: every bot message goes to the owner's chat in HTML and is recorded in ``bot_messages``.

``send`` retries Telegram flood control (``TelegramRetryAfter``) up to ``MAX_RETRIES`` times; other errors propagate
to the caller (the scheduler / job queue retries them). ``edit`` and ``react`` never fail on "nothing to do" cases.
Logs never contain message text or Telegram file URLs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
# "✅ Закрыл «…»" [↩️ Отменить]: an edit applied by description (item_id: a reply to it edits the item, like a card;
# not a "card", so a checklist change never redraws it into one).
PURPOSE_EDITED: Final = "edited"
# "🎤 «…» → 💡 Идея «…»": a voice note that gave only notes / ideas (iteration 3, §6). item_id when it is one item (a
# reply to it edits the item); not a "card", so a checklist change never redraws it into one.
PURPOSE_HEARD: Final = "heard"
# "🎤 Не разобрал голосовое…" [🔁 Ещё раз] (iteration 4, §1): nothing was saved (raw_message_id = the voice note).
PURPOSE_UNHEARD: Final = "unheard"

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
    quiet_checked: bool = False,
    purpose: str,
    item_id: int | None = None,
    occurrence_id: int | None = None,
    raw_message_id: int | None = None,
    later: list[Record] | None = None,
) -> int:
    """Send an HTML message to the owner and record it in ``bot_messages``. Returns the Telegram message_id.

    ``reply_to`` is a Telegram message_id in the owner's chat; the message is still sent if it was deleted.
    ``markup`` is inline buttons, or ``ForceReply`` for a prompt whose answer is a reply to it.
    ``quiet_checked``: the caller looked at the quiet hours itself (``silent`` is its decision), so
    ``bot.quiet_requests`` does not load the prefs again for this message.
    ``later``: the ``bot_messages`` row is not written now but added there, for ``record`` to write with the rows of
    the messages sent after it (several cards of one message: no database wait between them).
    """
    reply = ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
    # None lets bot.quiet_requests silence it in quiet hours; a reminder that reaches the sender is for a time the
    # user named (automatic ones are held back by the scheduler), so it rings unless told otherwise.
    ring = False if purpose == "reminder" or quiet_checked else None
    msg = await _with_retry(
        lambda: ctx.bot.send_message(
            chat_id=ctx.chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            reply_parameters=reply,
            disable_notification=True if silent else ring,
        ),
        "send_message",
    )
    sent = Record(msg.message_id, purpose, item_id, occurrence_id, raw_message_id)
    if later is not None:
        later.append(sent)
    else:
        await record(ctx, sent)
    log.debug("sent %s message %s (item=%s, occ=%s)", purpose, msg.message_id, item_id, occurrence_id)
    return msg.message_id


@dataclass(frozen=True)
class Record:
    """The ``bot_messages`` row of a sent message: what it is (``purpose``) and what it is about."""

    message_id: int
    purpose: str
    item_id: int | None = None
    occurrence_id: int | None = None
    raw_message_id: int | None = None


async def record(ctx: AppContext, *records: Record) -> None:
    """Store the message -> item mappings in one statement (no transaction around it). The messages are already
    out, so a DB error is only logged: failing here would make callers retry and send duplicates."""
    if not records:
        return
    rows = [
        {
            "chat_id": ctx.chat_id,
            "message_id": r.message_id,
            "purpose": r.purpose[:_PURPOSE_MAX],
            "item_id": r.item_id,
            "occurrence_id": r.occurrence_id,
            "raw_message_id": r.raw_message_id,
        }
        for r in records
    ]
    stmt = (
        insert(BotMessage)
        .values(rows)
        .on_conflict_do_nothing(index_elements=[BotMessage.chat_id, BotMessage.message_id])
    )
    try:
        async with ctx.autocommit() as session:
            await session.execute(stmt)
            await session.commit()
    except SQLAlchemyError:
        ids = ", ".join(str(r.message_id) for r in records)
        log.exception("could not record bot message(s) %s (purpose=%s)", ids, records[0].purpose)


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


def react_soon(ctx: AppContext, message_id: int, emoji: str) -> asyncio.Task[None]:
    """``react`` started now, without waiting for it (the capture router's 👀 while the job worker starts). A later
    ``react`` on the same message waits for this one first: Telegram keeps the reaction set last, and a slow 👀 must
    not replace the job's final 👌."""
    task = asyncio.ensure_future(react(ctx, message_id, emoji))
    ctx.reacting[message_id] = task

    def _forget(done: asyncio.Task[None]) -> None:
        if ctx.reacting.get(message_id) is done:
            del ctx.reacting[message_id]

    task.add_done_callback(_forget)
    return task


async def react(ctx: AppContext, message_id: int, emoji: str) -> None:
    """Best effort: set the bot's single reaction on a message (replaces the previous one). Never raises. One still
    on its way for the message (``react_soon``) goes first."""
    earlier = ctx.reacting.get(message_id)
    if earlier is not None and earlier is not asyncio.current_task() and not earlier.done():
        await asyncio.wait([earlier])
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
