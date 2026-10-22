"""Capture router and the owner whitelist: save raw -> enqueue -> 👀 -> wake the worker."""

from __future__ import annotations

from aiogram.methods import SendMessage
from sqlalchemy import select

from dumpbot.ai.schemas import ExtractedItem, Extraction
from dumpbot.bot.cbdata import PickCb
from dumpbot.bot.sender import PURPOSE_ASK, PURPOSE_PICK, REACT_ASK, REACT_SAVED, REACT_SEEN
from dumpbot.db.models import BotMessage, Item
from dumpbot.domain.prefs import Prefs
from dumpbot.services import ingest, items
from dumpbot.workers import jobs

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
    # that instance (the 18:00 reminder of "в 9:00 и 18:00": «готово» closes the 18:00, not this morning's)
    [job] = await seed.jobs()
    assert job.payload == {"raw_id": raw.id, "item_id": created.item.id, "occurrence_id": created.occurrence.id}


async def test_an_answer_to_not_heard_after_a_voice_edit_is_the_edit_again(feed, updates, seed) -> None:
    """ "🎤 Не разобрал голосовое — повтори или напиши текстом" after a voice reply to a card: the text (or voice)
    answer to it edits the item; it is no new message."""
    created = await seed.item()
    occ_id = created.occurrence.id
    await seed.bot_message(702, purpose="unheard", item_id=created.item.id, occurrence_id=occ_id)

    await feed(updates.message("перенеси на пятницу", reply_to=702))
    await feed(updates.message(voice={"file_id": "v1", "file_unique_id": "uv1", "duration": 2}, reply_to=702))

    raws = await seed.raws()
    assert [r.reply_item_id for r in raws] == [created.item.id] * 2
    jobs = await seed.jobs()
    assert [j.kind for j in jobs] == ["edit", "edit"]
    assert [j.payload["occurrence_id"] for j in jobs] == [occ_id, occ_id]


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


async def _items_from(seed, raw, *titles: str) -> list[int]:
    """Items saved from ``raw`` (as the processor would), kinds note."""
    async with seed.sessionmaker() as s:
        created = await items.create_items(
            s,
            raw=raw,
            extraction=Extraction(items=[ExtractedItem(kind="note", title=t) for t in titles]),
            source="text",
            source_text=None,
            source_meta=None,
            urls=[],
            prefs=Prefs(),
            now=raw.tg_date,
            model=None,
            prompt_version=None,
        )
        await s.commit()
    return [c.item.id for c in created]


async def test_reply_to_own_message_edits_the_item_saved_from_it(feed, updates, seed) -> None:
    # A note got only 👌 (no card): replying to the message itself is the way to fix it.
    raw = await seed.raw(600, text="позвонить в банк")
    [item_id] = await _items_from(seed, raw, "Позвонить в банк")

    await feed(updates.message("это задача, напомни вечером", reply_to=600, reply_to_bot=False))

    reply = (await seed.raws())[-1]
    assert reply.reply_item_id == item_id
    [job] = await seed.jobs()
    assert (job.kind, job.payload) == ("edit", {"raw_id": reply.id, "item_id": item_id})


async def test_reply_to_a_part_of_own_album_edits_its_item(feed, updates, seed) -> None:
    first = await seed.raw(610, text="чеки", media_group_id="g7")
    await seed.raw(611, text=None, media_group_id="g7")
    [item_id] = await _items_from(seed, first, "Чеки")

    await feed(updates.message("это покупки", reply_to=611, reply_to_bot=False))

    assert (await seed.raws())[-1].reply_item_id == item_id


async def test_reply_to_own_message_with_several_items_asks_which(feed, updates, seed, tg, get_buttons) -> None:
    raw = await seed.raw(620, text="купить хлеб и позвонить маме")
    bread, mum = await _items_from(seed, raw, "Купить хлеб", "Позвонить маме")

    update = updates.message("это задача", reply_to=620, reply_to_bot=False)
    await feed(update)

    reply = (await seed.raws())[-1]
    assert reply.status == "done" and reply.raw_json[ingest.CANDIDATES_KEY] == [bread, mum]
    assert await seed.jobs() == []
    [msg] = tg.sent
    assert "Какую запись изменить?" in msg.text and msg.reply_parameters.message_id == update.message.message_id
    assert get_buttons(msg.reply_markup) == [
        ("1", PickCb(raw=reply.id, item=bread).pack()),
        ("2", PickCb(raw=reply.id, item=mum).pack()),
    ]
    async with seed.sessionmaker() as s:
        [record] = (await s.scalars(select(BotMessage))).all()
    assert (record.purpose, record.raw_message_id) == (PURPOSE_PICK, reply.id)
    assert tg.reactions == [(update.message.message_id, REACT_ASK)]

    # A reply "2" to that list picks the second item.
    await feed(updates.message("2", reply_to=record.message_id))
    [job] = await seed.jobs()
    assert (job.kind, job.payload, job.dedupe_key) == (
        "edit",
        {"raw_id": reply.id, "item_id": mum},
        f"pick:{reply.id}:{mum}",
    )


async def test_reply_to_own_message_without_items_is_a_plain_message(feed, updates, seed) -> None:
    await seed.raw(630, text="? что у меня завтра")  # a question: no items

    await feed(updates.message("и послезавтра", reply_to=630, reply_to_bot=False))

    assert (await seed.raws())[-1].reply_item_id is None
    assert [j.kind for j in await seed.jobs()] == ["message"]


async def test_text_reply_to_the_clarifying_question_answers_it(feed, updates, seed, tg) -> None:
    asked = await seed.raw(640, text="встреча с Олегом в 8", raw_json={"_ask": {"q": "В 8 утра или вечера?"}})
    await seed.bot_message(710, purpose=PURPOSE_ASK, raw_message_id=asked.id)

    update = updates.message("вечера", reply_to=710)
    await feed(update)

    [job] = await seed.jobs()
    assert (job.kind, job.payload, job.dedupe_key) == (
        "clarify",
        {"raw_id": asked.id, "option": "вечера"},
        f"clarify:{asked.id}",
    )
    reply = (await seed.raws())[-1]
    assert (reply.text, reply.status) == ("вечера", "done")
    assert tg.reactions == [(update.message.message_id, REACT_SAVED)]
    [shown] = tg.edits  # the question shows the answer, like after a button
    assert shown.message_id == 710 and shown.text.endswith("✅ вечера") and shown.reply_markup is None


async def test_reply_to_an_already_answered_question_is_saved_as_a_message(feed, updates, seed) -> None:
    asked = await seed.raw(650, text="встреча в 8", raw_json={"_ask": {"q": "?"}})
    await seed.bot_message(711, purpose=PURPOSE_ASK, raw_message_id=asked.id)
    async with seed.sessionmaker() as s:  # the owner pressed a button first
        await jobs.enqueue(s, "clarify", {"raw_id": asked.id, "option": 0}, dedupe_key=f"clarify:{asked.id}")
        await s.commit()

    await feed(updates.message("вечера", reply_to=711))

    assert [j.kind for j in await seed.jobs()] == ["clarify", "message"]
    assert (await seed.raws())[-1].status == "new"


async def test_reply_to_the_candidates_list(feed, updates, seed) -> None:
    first = await seed.item("Встреча с Олегом")
    second = await seed.item("Встреча с Олегом по проекту")
    edit = await seed.raw(
        660, text="перенеси встречу с Олегом", raw_json={ingest.CANDIDATES_KEY: [first.item.id, second.item.id]}
    )
    await seed.bot_message(712, purpose=PURPOSE_PICK, raw_message_id=edit.id)

    await feed(updates.message("7", reply_to=712))  # not on the list: a message of its own
    await feed(updates.message("2", reply_to=712))

    message_job, pick = await seed.jobs()
    assert message_job.kind == "message"
    assert (pick.kind, pick.payload) == ("edit", {"raw_id": edit.id, "item_id": second.item.id})


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
