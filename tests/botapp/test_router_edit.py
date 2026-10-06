"""Edit dialogs: 🕒 Время -> job edit_time, ✏️ Название -> set_title, #️⃣ Теги -> set_tags, /cancel, expiry, and
messages that are not the answer (forwards, replies to other cards, "?" questions, voice, late notes)."""

from __future__ import annotations

from aiogram.types import Chat, MessageOriginChannel, Voice

from dumpbot.bot.cbdata import ItemCb
from dumpbot.bot.sender import REACT_SAVED, REACT_SEEN
from dumpbot.bot.states import EditStates
from dumpbot.db.models import Item
from dumpbot.services import items


async def _item(seed, item_id: int) -> Item:
    async with seed.sessionmaker() as s:
        return await s.get(Item, item_id)


async def _state(dp, bot) -> str | None:
    return await dp.fsm.get_context(bot, chat_id=4242, user_id=4242).get_state()


async def test_time_prompt_then_text_queues_edit_time(feed, updates, seed, tg, dp, bot, ctx, get_buttons) -> None:
    created = await seed.item()
    item_id = created.item.id

    await feed(updates.callback(ItemCb(a="time", id=item_id), message_id=920))

    assert await _state(dp, bot) == EditStates.time.state
    [prompt] = tg.edits
    assert prompt.message_id == 920
    assert "Когда?" in prompt.text and "/cancel" in prompt.text
    assert get_buttons(prompt.reply_markup) == [("✖️ Отмена", ItemCb(a="back", id=item_id).pack())]
    assert tg.toasts == [None]

    answer = updates.message("в пятницу в 18:00")
    await feed(answer)

    [job] = await seed.jobs()
    assert job.kind == "edit_time"
    assert job.payload == {
        "item_id": item_id,
        "text": "в пятницу в 18:00",
        "message_id": answer.message.message_id,
        "now": answer.message.date.isoformat(),
    }
    assert await seed.raws() == []  # not captured as a new note
    assert await _state(dp, bot) is None
    card = tg.edits[-1]
    assert card.message_id == 920 and card.text.startswith("📌 <b>Купить наушники</b>")
    assert tg.reactions == [(answer.message.message_id, REACT_SEEN)]
    assert ctx.jobs_wakeup.is_set()


async def test_title_dialog(feed, updates, seed, tg, dp, bot) -> None:
    created = await seed.item()

    await feed(updates.callback(ItemCb(a="title", id=created.item.id), message_id=921))
    assert await _state(dp, bot) == EditStates.title.state
    answer = updates.message("  Купить   наушники Sony ")
    await feed(answer)

    assert (await _item(seed, created.item.id)).title == "Купить наушники Sony"
    assert tg.edits[-1].text.startswith("📌 <b>Купить наушники Sony</b>")
    # The prompt may be far up the chat: the change is confirmed right under the answer.
    [done] = tg.sent
    assert done.text == "✏️ Название изменено: <b>Купить наушники Sony</b>"
    assert done.reply_parameters.message_id == answer.message.message_id
    assert tg.reactions == [(answer.message.message_id, REACT_SAVED)]
    assert await _state(dp, bot) is None
    assert await seed.raws() == []


async def test_title_must_not_be_empty(feed, updates, seed, tg, dp, bot) -> None:
    created = await seed.item()
    await feed(updates.callback(ItemCb(a="title", id=created.item.id)))

    await feed(updates.message("   "))

    assert "не может быть пустым" in tg.texts[-1]
    assert await _state(dp, bot) == EditStates.title.state
    assert (await _item(seed, created.item.id)).title == "Купить наушники"


async def test_tags_dialog_normalizes(feed, updates, seed, tg) -> None:
    created = await seed.item()

    await feed(updates.callback(ItemCb(a="tags", id=created.item.id)))
    assert "Сейчас: нет" in tg.edits[0].text
    await feed(updates.message("#Дом, работа  Работа;срочно"))

    assert (await _item(seed, created.item.id)).tags == ["дом", "работа", "срочно"]
    assert "#дом" in tg.edits[-1].text
    assert tg.texts == ["#️⃣ Теги для <b>Купить наушники</b>: #дом #работа #срочно"]


async def test_tags_dash_clears(feed, updates, seed) -> None:
    created = await seed.item()
    await feed(updates.callback(ItemCb(a="tags", id=created.item.id)))
    await feed(updates.message("a b"))
    await feed(updates.callback(ItemCb(a="tags", id=created.item.id)))
    await feed(updates.message("-"))

    assert (await _item(seed, created.item.id)).tags == []


async def test_cancel_command_restores_the_card(feed, updates, seed, tg, dp, bot) -> None:
    created = await seed.item()
    await feed(updates.callback(ItemCb(a="title", id=created.item.id), message_id=922))

    await feed(updates.message("/cancel"))

    assert await _state(dp, bot) is None
    assert tg.texts == ["Отменено."]
    assert tg.edits[-1].message_id == 922 and tg.edits[-1].text.startswith("📌")

    await feed(updates.message("просто заметка"))
    assert len(await seed.raws()) == 1  # captured normally again


async def test_cancel_button_leaves_the_dialog(feed, updates, seed, tg, dp, bot) -> None:
    created = await seed.item()
    await feed(updates.callback(ItemCb(a="time", id=created.item.id)))

    await feed(updates.callback(ItemCb(a="back", id=created.item.id)))

    assert await _state(dp, bot) is None
    assert tg.edits[-1].text.startswith("📌")


async def test_commands_still_work_inside_a_dialog(feed, updates, seed, tg, dp, bot) -> None:
    created = await seed.item()
    await feed(updates.callback(ItemCb(a="title", id=created.item.id)))

    await feed(updates.message("/help"))

    assert "/today" in tg.texts[-1]
    assert await _state(dp, bot) == EditStates.title.state


async def test_non_text_inside_a_dialog_is_captured(feed, updates, seed, dp, bot) -> None:
    created = await seed.item()
    await feed(updates.callback(ItemCb(a="title", id=created.item.id)))

    await feed(updates.message(photo=True, caption=None))

    [raw] = await seed.raws()
    assert raw.content_type == "photo"
    assert (await _item(seed, created.item.id)).title == "Купить наушники"
    assert await _state(dp, bot) is None  # the dialog is over: the next note is not taken for the title

    await feed(updates.message("купить молоко"))
    assert (await _item(seed, created.item.id)).title == "Купить наушники"
    assert len(await seed.raws()) == 2


async def test_forwarded_post_is_not_a_title(feed, updates, seed, tg, dp, bot, frozen) -> None:
    created = await seed.item("Созвон")
    await feed(updates.callback(ItemCb(a="title", id=created.item.id), message_id=930))
    frozen.advance(minutes=1)

    origin = MessageOriginChannel(date=frozen.now, chat=Chat(id=-1001, type="channel", title="Хабр"), message_id=7)
    await feed(updates.message("Длинный пост из канала про asyncio, сохранить почитать", forward_origin=origin))

    assert (await _item(seed, created.item.id)).title == "Созвон"
    [raw] = await seed.raws()
    assert raw.forward_origin["type"] == "channel"
    assert [j.kind for j in await seed.jobs()] == ["message"]
    assert await _state(dp, bot) is None
    assert tg.edits[-1].message_id == 930 and tg.edits[-1].text.startswith("📌 <b>Созвон</b>")


async def test_reply_to_another_card_edits_that_card(feed, updates, seed, dp, bot) -> None:
    first = await seed.item("Первая")
    second = await seed.item("Вторая")
    await seed.bot_message(950, item_id=second.item.id, occurrence_id=second.occurrence.id)
    await feed(updates.callback(ItemCb(a="title", id=first.item.id), message_id=949))

    await feed(updates.message("перенеси на пятницу", reply_to=950))

    assert (await _item(seed, first.item.id)).title == "Первая"
    [raw] = await seed.raws()
    assert raw.reply_item_id == second.item.id
    [job] = await seed.jobs()
    assert (job.kind, job.payload) == ("edit", {"raw_id": raw.id, "item_id": second.item.id})
    assert await _state(dp, bot) is None


async def test_question_is_not_tags(feed, updates, seed, dp, bot) -> None:
    created = await seed.item()
    await feed(updates.callback(ItemCb(a="tags", id=created.item.id)))

    await feed(updates.message("? что у меня завтра"))

    assert (await _item(seed, created.item.id)).tags == []
    [raw] = await seed.raws()
    assert raw.force_query is True
    assert await _state(dp, bot) is None


async def test_a_note_sent_minutes_later_is_not_the_answer_but_a_reply_to_the_prompt_is(
    feed, updates, seed, dp, bot, frozen
) -> None:
    created = await seed.item()
    await feed(updates.callback(ItemCb(a="title", id=created.item.id), message_id=931))
    frozen.advance(minutes=6)
    await feed(updates.message("хлеб купить"))

    assert (await _item(seed, created.item.id)).title == "Купить наушники"
    [raw] = await seed.raws()
    assert raw.text == "хлеб купить"
    assert await _state(dp, bot) is None

    await feed(updates.callback(ItemCb(a="title", id=created.item.id), message_id=931))
    frozen.advance(minutes=10)
    await feed(updates.message("Наушники Sony", reply_to=931))  # an explicit reply to the prompt still counts

    assert (await _item(seed, created.item.id)).title == "Наушники Sony"
    assert len(await seed.raws()) == 1


async def test_voice_answer_to_the_time_prompt_is_an_edit_of_the_item(feed, updates, seed, tg, dp, bot) -> None:
    created = await seed.item("Созвон")
    await feed(updates.callback(ItemCb(a="time", id=created.item.id), message_id=932))

    voice = updates.message(voice=Voice(file_id="v1", file_unique_id="uv1", duration=2))
    await feed(voice)

    [raw] = await seed.raws()
    assert (raw.content_type, raw.reply_item_id) == ("voice", created.item.id)
    [job] = await seed.jobs()
    assert (job.kind, job.payload) == ("edit", {"raw_id": raw.id, "item_id": created.item.id})
    assert await _state(dp, bot) is None
    assert tg.edits[-1].message_id == 932 and tg.edits[-1].text.startswith("📌 <b>Созвон</b>")
    assert tg.reactions == [(voice.message.message_id, REACT_SEEN)]

    await feed(updates.message("хлеб купить"))  # the dialog is closed: a new note, not "перенеси на: хлеб купить"
    assert [j.kind for j in await seed.jobs()] == ["edit", "message"]


async def test_expired_prompt_lets_the_text_through(feed, updates, seed, tg, dp, bot, frozen) -> None:
    created = await seed.item()
    await feed(updates.callback(ItemCb(a="title", id=created.item.id), message_id=923))
    frozen.advance(hours=2)

    await feed(updates.message("купить молоко"))

    assert (await _item(seed, created.item.id)).title == "Купить наушники"
    [raw] = await seed.raws()
    assert raw.text == "купить молоко"
    assert await _state(dp, bot) is None
    assert tg.edits[-1].message_id == 923 and tg.edits[-1].text.startswith("📌")


async def test_prompt_for_a_missing_item(feed, updates, tg, dp, bot) -> None:
    await feed(updates.callback(ItemCb(a="time", id=31337)))
    assert tg.toasts == ["Запись не найдена"]
    assert await _state(dp, bot) is None


async def test_item_deleted_while_in_dialog(feed, updates, seed, tg, frozen, dp, bot) -> None:
    created = await seed.item()
    await feed(updates.callback(ItemCb(a="time", id=created.item.id)))
    async with seed.sessionmaker() as s:
        await items.delete_item(s, created.item.id, frozen.now)
        await s.commit()

    await feed(updates.message("завтра"))

    assert await seed.jobs() == []
    assert tg.texts[-1] == "Запись не найдена"
    assert await _state(dp, bot) is None


async def test_a_new_prompt_restores_the_previous_one(feed, updates, seed, tg, dp, bot) -> None:
    first = await seed.item("Первая")
    second = await seed.item("Вторая")
    await feed(updates.callback(ItemCb(a="title", id=first.item.id), message_id=940))

    await feed(updates.callback(ItemCb(a="time", id=second.item.id), message_id=941))

    restored, prompt = tg.edits[-2:]
    assert restored.message_id == 940 and restored.text.startswith("📌 <b>Первая</b>")
    assert prompt.message_id == 941 and "Когда?" in prompt.text
    assert await _state(dp, bot) == EditStates.time.state


async def test_back_on_another_card_keeps_the_dialog(feed, updates, seed, dp, bot) -> None:
    first = await seed.item("Первая")
    second = await seed.item("Вторая")
    await feed(updates.callback(ItemCb(a="title", id=first.item.id), message_id=942))

    await feed(updates.callback(ItemCb(a="back", id=second.item.id), message_id=943))

    assert await _state(dp, bot) == EditStates.title.state
