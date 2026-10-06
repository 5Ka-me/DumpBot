"""Capture router and the owner whitelist: save raw -> enqueue -> 👀 -> wake the worker."""

from __future__ import annotations

from aiogram.methods import SendMessage

from dumpbot.bot.sender import REACT_SEEN
from dumpbot.db.models import Item

STRANGER = 777  # not the owner (conftest's OWNER is 4242)


async def test_text_message_is_saved_queued_and_marked_seen(feed, updates, seed, tg, ctx) -> None:
    update = updates.message("купить молоко завтра")
    await feed(update)

    [raw] = await seed.raws()
    assert raw.text == "купить молоко завтра"
    assert raw.content_type == "text"
    assert raw.force_query is False
    assert raw.reply_item_id is None
    [job] = await seed.jobs()
    assert (job.kind, job.payload, job.dedupe_key) == ("message", {"raw_id": raw.id}, f"msg:{raw.id}")
    assert tg.reactions == [(update.message.message_id, REACT_SEEN)]
    assert ctx.jobs_wakeup.is_set()
    assert tg.sent == []


async def test_question_mark_forces_query_and_keeps_text(feed, updates, seed) -> None:
    await feed(updates.message("  ? что у меня завтра"))

    [raw] = await seed.raws()
    assert raw.force_query is True
    assert raw.text == "  ? что у меня завтра"


async def test_photo_caption_with_question_mark_forces_query(feed, updates, seed) -> None:
    await feed(updates.message(photo=True, caption="?что это"))

    [raw] = await seed.raws()
    assert (raw.content_type, raw.force_query, raw.file_id) == ("photo", True, "ph1")


async def test_reply_to_a_card_becomes_an_edit_job(feed, updates, seed, tg) -> None:
    created = await seed.item()
    await seed.bot_message(700, purpose="card", item_id=created.item.id, occurrence_id=created.occurrence.id)

    await feed(updates.message("перенеси на пятницу", reply_to=700))

    [raw] = await seed.raws()
    assert raw.reply_item_id == created.item.id
    [job] = await seed.jobs()
    assert job.kind == "edit"
    assert job.payload == {"raw_id": raw.id, "item_id": created.item.id}


async def test_reply_to_a_reminder_edits_its_item(feed, updates, seed) -> None:
    created = await seed.item()
    await seed.bot_message(701, purpose="reminder", item_id=created.item.id, occurrence_id=created.occurrence.id)

    await feed(updates.message("отложи на час", reply_to=701))

    [raw] = await seed.raws()
    assert raw.reply_item_id == created.item.id


async def test_reply_to_a_bot_message_without_item_is_a_plain_message(feed, updates, seed) -> None:
    await seed.bot_message(702, purpose="digest")
    await feed(updates.message("спасибо", reply_to=702))
    await feed(updates.message("а это мой ответ себе", reply_to=555))  # unknown message

    raws = await seed.raws()
    assert [r.reply_item_id for r in raws] == [None, None]
    assert [j.kind for j in await seed.jobs()] == ["message", "message"]


async def test_reply_to_own_message_is_not_looked_up(feed, updates, seed) -> None:
    created = await seed.item()
    # Even if an id collides with a recorded bot message, the user's own message is not a card.
    await seed.bot_message(703, purpose="card", item_id=created.item.id)
    await feed(updates.message("дополнение", reply_to=703, reply_to_bot=False))

    [raw] = await seed.raws()
    assert raw.reply_item_id is None


async def test_question_reply_to_card_keeps_both_flags(feed, updates, seed) -> None:
    created = await seed.item()
    await seed.bot_message(704, purpose="card", item_id=created.item.id)
    await feed(updates.message("?когда это", reply_to=704))

    [raw] = await seed.raws()
    assert (raw.force_query, raw.reply_item_id) == (True, created.item.id)


async def test_redelivered_update_is_saved_once_and_not_reacted_again(feed, updates, seed, tg) -> None:
    update = updates.message("одна и та же", message_id=333)
    await feed(update)
    await feed(update)

    assert len(await seed.raws()) == 1
    assert len(await seed.jobs()) == 1
    assert tg.reactions == [(333, REACT_SEEN)]


async def test_album_parts_share_one_job(feed, updates, seed) -> None:
    await feed(updates.message(photo=True, caption="чеки", media_group_id="g1"))
    await feed(updates.message(photo=True, media_group_id="g1"))

    assert len(await seed.raws()) == 2
    [job] = await seed.jobs()
    assert job.kind == "album"
    assert job.payload["media_group_id"] == "g1"


async def test_service_message_is_ignored(feed, updates, seed, tg) -> None:
    pinned = updates.message("закреп").message
    await feed(updates.message(pinned_message=pinned))

    assert await seed.raws() == []
    assert tg.requests == []


async def test_group_chat_messages_are_ignored(feed, updates, seed, tg) -> None:
    await feed(updates.message("в группе", chat_id=-100500, chat_type="supergroup"))

    assert await seed.raws() == []
    assert tg.requests == []


async def test_edited_messages_are_not_handled(dp, bot, updates, seed, tg) -> None:
    update = updates.message("исправленный текст")
    edited = update.model_copy(update={"message": None, "edited_message": update.message})
    await dp.feed_update(bot, edited)

    assert await seed.raws() == []
    assert "edited_message" not in dp.resolve_used_update_types()


# --- owner whitelist ---------------------------------------------------------------------------------------------


async def test_messages_from_strangers_are_dropped(feed, updates, seed, tg, ctx) -> None:
    await feed(updates.message("привет", from_id=STRANGER))
    await feed(updates.message("/start", from_id=STRANGER))
    await feed(updates.message("/today", from_id=STRANGER))

    assert await seed.raws() == []
    assert tg.requests == []
    assert not ctx.jobs_wakeup.is_set()


async def test_callbacks_from_strangers_are_dropped(feed, updates, seed, tg) -> None:
    created = await seed.item()
    await feed(updates.callback(f"i:del:{created.item.id}", from_id=STRANGER))

    assert tg.requests == []
    async with seed.sessionmaker() as s:
        item = await s.get(Item, created.item.id)
    assert item.status == "open"


async def test_unknown_command_gets_a_hint_and_is_not_saved(feed, updates, seed, tg) -> None:
    await feed(updates.message("/foo bar"))

    assert await seed.raws() == []
    [msg] = tg.of(SendMessage)
    assert "/help" in msg.text
