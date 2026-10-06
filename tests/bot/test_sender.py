"""bot.sender with a fake Bot and a real Postgres session (BotMessage rows)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReactionTypeEmoji, ReplyParameters
from sqlalchemy import func, select

from dumpbot.bot import sender
from dumpbot.db.models import BotMessage, Item, Occurrence, RawMessage

SECRET_TEXT = "секретный текст пользователя"
KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="x", callback_data="i:edit:1")]])
NOW = datetime(2026, 10, 6, 12, 5, tzinfo=UTC)


async def _seed(session) -> tuple[int, int, int]:
    """Rows the bot_messages foreign keys point to: (raw id, item id, occurrence id)."""
    raw = RawMessage(chat_id=1, message_id=1, content_type="text", tg_date=NOW)
    session.add(raw)
    await session.flush()
    item = Item(raw_message_id=raw.id, kind="task", title="t")
    session.add(item)
    await session.flush()
    occ = Occurrence(item_id=item.id, due_at=NOW, precision="exact")
    session.add(occ)
    await session.commit()
    return raw.id, item.id, occ.id


async def _bot_messages(session) -> list[BotMessage]:
    session.expire_all()
    return list((await session.scalars(select(BotMessage).order_by(BotMessage.message_id))).all())


# --- send --------------------------------------------------------------------------------------------------------


async def test_send_records_bot_message(make_ctx, fake_bot, sessionmaker, session):
    raw_id, item_id, occ_id = await _seed(session)
    ctx = make_ctx(sessionmaker)
    message_id = await sender.send(
        ctx,
        "<b>hi</b>",
        KB,
        reply_to=42,
        purpose="card",
        item_id=item_id,
        occurrence_id=occ_id,
        raw_message_id=raw_id,
    )
    [call] = fake_bot.calls_of("send_message")
    assert call["chat_id"] == ctx.chat_id
    assert call["text"] == "<b>hi</b>"
    assert call["parse_mode"] == "HTML"
    assert call["reply_markup"] is KB
    assert call["reply_parameters"] == ReplyParameters(message_id=42, allow_sending_without_reply=True)
    assert call["disable_notification"] is None

    [row] = await _bot_messages(session)
    assert (row.chat_id, row.message_id, row.purpose) == (ctx.chat_id, message_id, "card")
    assert (row.item_id, row.occurrence_id, row.raw_message_id) == (item_id, occ_id, raw_id)
    assert await sender.lookup(session, ctx.chat_id, message_id) == row


async def test_send_without_reply_and_silent(make_ctx, fake_bot, sessionmaker, session):
    ctx = make_ctx(sessionmaker)
    first = await sender.send(ctx, "a", purpose="digest", silent=True)
    second = await sender.send(ctx, "b", purpose="a_very_long_purpose_name")
    calls = fake_bot.calls_of("send_message")
    assert calls[0]["reply_parameters"] is None and calls[0]["reply_markup"] is None
    assert calls[0]["disable_notification"] is True
    rows = await _bot_messages(session)
    assert [(r.message_id, r.purpose, r.item_id) for r in rows] == [
        (first, "digest", None),
        (second, "a_very_long_purp", None),  # purpose column is String(16)
    ]


async def test_send_retries_flood_control(make_ctx, fake_bot, sessionmaker, session, retry_after, no_sleep):
    fake_bot.fail("send_message", retry_after(2), retry_after(3))
    message_id = await sender.send(make_ctx(sessionmaker), "x", purpose="reminder")
    assert len(fake_bot.calls_of("send_message")) == 3
    assert no_sleep == [2.5, 3.5]
    assert [r.message_id for r in await _bot_messages(session)] == [message_id]


async def test_send_gives_up_after_three_retries(make_ctx, fake_bot, sessionmaker, session, retry_after, no_sleep):
    fake_bot.fail("send_message", *(retry_after(1) for _ in range(4)))
    with pytest.raises(TelegramRetryAfter):
        await sender.send(make_ctx(sessionmaker), "x", purpose="reminder")
    assert len(fake_bot.calls_of("send_message")) == 4
    assert len(no_sleep) == 3
    assert await _bot_messages(session) == []


async def test_send_does_not_sleep_through_long_flood_waits(make_ctx, fake_bot, retry_after, no_sleep):
    fake_bot.fail("send_message", retry_after(600))
    with pytest.raises(TelegramRetryAfter):
        await sender.send(make_ctx(None), "x", purpose="reminder")
    assert no_sleep == []


async def test_send_propagates_other_errors(make_ctx, fake_bot, bad_request):
    fake_bot.fail("send_message", bad_request("Bad Request: can't parse entities"))
    with pytest.raises(TelegramBadRequest):
        await sender.send(make_ctx(None), "x", purpose="card")


async def test_send_survives_db_errors(make_ctx, sessionmaker, session, caplog):
    """The message is already delivered: a failed bookkeeping insert must not make callers resend it."""
    caplog.set_level(logging.INFO, logger="dumpbot.bot.sender")
    message_id = await sender.send(make_ctx(sessionmaker), SECRET_TEXT, purpose="card", item_id=987654)  # no item
    assert message_id > 0
    assert await _bot_messages(session) == []
    assert any("could not record" in r.getMessage() for r in caplog.records)
    assert all(SECRET_TEXT not in r.getMessage() for r in caplog.records)


async def test_lookup_missing(session):
    assert await sender.lookup(session, 1, 2) is None


# --- edit --------------------------------------------------------------------------------------------------------


async def test_edit(make_ctx, fake_bot):
    ctx = make_ctx(None)
    await sender.edit(ctx, 77, "<i>new</i>", KB)
    [call] = fake_bot.calls_of("edit_message_text")
    assert call == {
        "chat_id": ctx.chat_id,
        "message_id": 77,
        "text": "<i>new</i>",
        "parse_mode": "HTML",
        "reply_markup": KB,
    }
    await sender.edit(ctx, 77, "plain")
    assert fake_bot.calls_of("edit_message_text")[1]["reply_markup"] is None


async def test_edit_ignores_not_modified(make_ctx, fake_bot, bad_request):
    fake_bot.fail(
        "edit_message_text",
        bad_request("Bad Request: message is not modified: specified new message content and reply markup are"),
    )
    await sender.edit(make_ctx(None), 1, "same")


@pytest.mark.parametrize(
    "description",
    [
        "Bad Request: message to edit not found",
        "Bad Request: message can't be edited",
        "Bad Request: MESSAGE_ID_INVALID",
    ],
)
async def test_edit_skips_uneditable_messages(make_ctx, fake_bot, bad_request, caplog, description):
    caplog.set_level(logging.INFO, logger="dumpbot.bot.sender")
    fake_bot.fail("edit_message_text", bad_request(description))
    await sender.edit(make_ctx(None), 5, SECRET_TEXT)
    [record] = [r for r in caplog.records if r.name == "dumpbot.bot.sender"]
    assert record.levelno == logging.INFO
    assert SECRET_TEXT not in record.getMessage()


async def test_edit_raises_other_bad_requests(make_ctx, fake_bot, bad_request):
    fake_bot.fail("edit_message_text", bad_request("Bad Request: can't parse entities: unclosed tag"))
    with pytest.raises(TelegramBadRequest):
        await sender.edit(make_ctx(None), 1, "<b>")


async def test_edit_retries_flood_control(make_ctx, fake_bot, retry_after, no_sleep):
    fake_bot.fail("edit_message_text", retry_after(1))
    await sender.edit(make_ctx(None), 1, "x")
    assert len(fake_bot.calls_of("edit_message_text")) == 2
    assert no_sleep == [1.5]


# --- react -------------------------------------------------------------------------------------------------------


async def test_react(make_ctx, fake_bot):
    ctx = make_ctx(None)
    await sender.react(ctx, 9, sender.REACT_SAVED)
    [call] = fake_bot.calls_of("set_message_reaction")
    assert call == {"chat_id": ctx.chat_id, "message_id": 9, "reaction": [ReactionTypeEmoji(emoji="👌")]}


@pytest.mark.parametrize("error", ["bad", "retry", "other"])
async def test_react_is_best_effort(make_ctx, fake_bot, bad_request, retry_after, caplog, error):
    caplog.set_level(logging.INFO, logger="dumpbot.bot.sender")
    exc = {"bad": bad_request("Bad Request: REACTION_INVALID"), "retry": retry_after(5), "other": OSError("down")}
    fake_bot.fail("set_message_reaction", exc[error])
    await sender.react(make_ctx(None), 9, "🫡")
    assert any("reaction on message 9 failed" in r.getMessage() for r in caplog.records)


def test_reaction_constants_are_allowed_emoji():
    allowed = {"👀", "👌", "✍", "🤔", "😢"}
    assert {
        sender.REACT_SEEN,
        sender.REACT_SAVED,
        sender.REACT_DEFERRED,
        sender.REACT_ASK,
        sender.REACT_FAILED,
    } == allowed


async def test_bot_message_count_unchanged_by_edit_and_react(make_ctx, sessionmaker, session):
    ctx = make_ctx(sessionmaker)
    await sender.edit(ctx, 1, "x")
    await sender.react(ctx, 1, "👀")
    assert await session.scalar(select(func.count()).select_from(BotMessage)) == 0
