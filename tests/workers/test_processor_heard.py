"""services.processor and voice notes (docs/features/iteration-3.md §6): every reply to a voice note shows what was
heard ("🎤 «…»"); one that gave only notes / ideas gets "🎤 «…» → 💡 Идея «…»" with [✏️ Изменить] [🗑 Не то — удалить].

The voice notes are stored with their transcript already (as after the STT step), so no speech-to-text runs here.
"""

from __future__ import annotations

from typing import Any

import pytest

from dumpbot.bot import sender
from dumpbot.bot.cbdata import ItemCb, UndoCb
from dumpbot.db.models import Item
from dumpbot.services import processor, query

HEADPHONES = {"items": [{"kind": "purchase", "title": "Купить наушники", "when": {"text": "в обед", "time": "lunch"}}]}


def buttons(call: dict) -> list[tuple[str, str]]:
    markup = call.get("reply_markup")
    if markup is None:
        return []
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


@pytest.fixture
def add_voice(add_raw):
    async def _add(transcript: str, **kw: Any):
        return await add_raw(None, content_type="voice", file_id="v", duration=3, transcript=transcript, **kw)

    return _add


async def test_voice_card_shows_what_was_heard(ctx, add_voice, fake_ai, bot, bot_messages, items_of_raw) -> None:
    raw = await add_voice("Купить наушники в обед")
    fake_ai.push(HEADPHONES)
    await processor.handle_message(ctx, {"raw_id": raw.id})

    [card] = bot.sent
    lines = card["text"].split("\n")
    assert lines[:2] == ["🛒 <b>Купить наушники</b>", "🔔 сегодня 13:00"]
    assert "🎤 «Купить наушники в обед»" in lines
    assert [m.purpose for m in await bot_messages()] == ["card"]
    assert bot.reactions == [(raw.message_id, "👌")]

    # the card keeps the line when it is drawn again (a button press): it comes with the item
    async with ctx.sessionmaker() as session:
        [item] = await items_of_raw(raw.id)
        extras = await query.card_extras(session, item)
    assert extras.heard == "Купить наушники в обед"


async def test_voice_idea_gets_a_short_message_with_buttons(
    ctx, add_voice, fake_ai, bot, bot_messages, items_of_raw
) -> None:
    raw = await add_voice("идея бот который сам предлагает подзадачи")
    fake_ai.push({"items": [{"kind": "idea", "title": "Бот, который предлагает подзадачи"}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})

    [item] = await items_of_raw(raw.id)
    [msg] = bot.sent
    assert msg["text"] == "🎤 «идея бот который сам предлагает подзадачи» → 💡 Идея «Бот, который предлагает подзадачи»"
    assert buttons(msg) == [
        ("✏️ Изменить", ItemCb(a="edit", id=item.id).pack()),
        ("🗑 Не то — удалить", ItemCb(a="del", id=item.id).pack()),
    ]
    assert msg["reply_parameters"].message_id == raw.message_id
    [bm] = await bot_messages()
    assert (bm.purpose, bm.item_id, bm.raw_message_id) == (sender.PURPOSE_HEARD, item.id, raw.id)
    assert bot.reactions == [(raw.message_id, "👌")]  # still saved with 👌


async def test_garbled_voice_note_kept_as_a_note_shows_the_transcript(ctx, add_voice, fake_ai, bot) -> None:
    raw = await add_voice("ааа ммм шшш")
    fake_ai.push({"items": []})  # nothing understood: kept as a note with the transcript
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [msg] = bot.sent
    assert msg["text"] == "🎤 «ааа ммм шшш» → 📝 Заметка «ааа ммм шшш»"
    assert [text for text, _ in buttons(msg)] == ["✏️ Изменить", "🗑 Не то — удалить"]


async def test_voice_with_two_notes_lists_both(ctx, add_voice, fake_ai, bot, bot_messages, items_of_raw) -> None:
    raw = await add_voice("заметка про отпуск и идея про подкаст")
    fake_ai.push({"items": [{"kind": "note", "title": "Отпуск"}, {"kind": "idea", "title": "Подкаст"}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    first, second = await items_of_raw(raw.id)
    [msg] = bot.sent
    assert msg["text"].split("\n") == [
        "🎤 «заметка про отпуск и идея про подкаст» →",
        "1. 📝 Заметка «Отпуск»",
        "2. 💡 Идея «Подкаст»",
    ]
    assert [data for _, data in buttons(msg)] == [
        ItemCb(a="edit", id=first.id).pack(),
        ItemCb(a="del", id=first.id).pack(),
        ItemCb(a="edit", id=second.id).pack(),
        ItemCb(a="del", id=second.id).pack(),
    ]
    [bm] = await bot_messages()
    assert bm.item_id is None


async def test_voice_with_a_task_and_a_note_gets_only_the_card(ctx, add_voice, fake_ai, bot) -> None:
    raw = await add_voice("купить наушники в обед и заметка про отпуск")
    fake_ai.push({"items": [*HEADPHONES["items"], {"kind": "note", "title": "Отпуск"}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [card] = bot.sent  # as before: the note just gets 👌
    assert card["text"].startswith("🛒 <b>Купить наушники</b>")
    assert "🎤 «купить наушники в обед и заметка про отпуск»" in card["text"]


async def test_typed_idea_still_gets_only_a_reaction(ctx, add_raw, fake_ai, bot) -> None:
    raw = await add_raw("идея: подкаст")
    fake_ai.push({"items": [{"kind": "idea", "title": "Подкаст"}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert bot.sent == []
    assert bot.reactions == [(raw.message_id, "👌")]


async def test_voice_question_answer_shows_what_was_heard(ctx, add_voice, fake_ai, bot, make_items, t0) -> None:
    await make_items({"kind": "task", "title": "Спорт", "when": {"day": "+1", "time": "18:00"}}, now=t0)
    raw = await add_voice("что у меня завтра")
    fake_ai.push({"intent": "query", "query": {"range": "tomorrow", "answer": "list"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [answer] = bot.sent
    lines = answer["text"].split("\n")
    assert lines[0] == "🎤 «что у меня завтра»"
    assert "Спорт" in answer["text"]


async def test_voice_question_to_ask_back_shows_what_was_heard(ctx, add_voice, fake_ai, bot) -> None:
    raw = await add_voice("в восемь позвонить маме")
    fake_ai.push(
        {
            "items": [{"kind": "task", "title": "Позвонить маме", "when": {"time": "08:00"}}],
            "ask": {"item": 0, "q": "В 8 утра или вечера?", "options": ["утра", "вечера"]},
        }
    )
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [ask] = bot.sent
    assert ask["text"].split("\n") == ["🎤 «в восемь позвонить маме»", "🤔 В 8 утра или вечера?"]


async def test_voice_edit_by_description_shows_what_was_heard(ctx, add_voice, fake_ai, bot, make_items, t0, get):
    [created] = await make_items({"kind": "task", "title": "Обновить документацию", "when": {"time": "17:00"}}, now=t0)
    raw = await add_voice("закрой задачу про документацию")
    fake_ai.push({"intent": "edit", "edit": {"target": "документацию", "action": "done"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [msg] = bot.sent
    assert msg["text"].split("\n") == ["🎤 «закрой задачу про документацию»", "✅ Закрыл «Обновить документацию»"]
    assert buttons(msg) == [("↩️ Отменить", UndoCb(raw=raw.id, item=created.item.id).pack())]
    assert (await get(Item, created.item.id)).status == "done"


async def test_voice_reply_edit_card_shows_the_edits_transcript(ctx, add_voice, fake_ai, bot, items_of_raw) -> None:
    first = await add_voice("Купить наушники в обед")
    fake_ai.push(HEADPHONES)
    await processor.handle_message(ctx, {"raw_id": first.id})
    [item] = await items_of_raw(first.id)

    edit = await add_voice("лучше вечером", reply_item_id=item.id)
    fake_ai.push({"intent": "edit", "edit": {"when": {"slot": "evening", "time": "evening"}}})
    await processor.handle_edit(ctx, {"raw_id": edit.id, "item_id": item.id})
    card = bot.sent[-1]
    assert "🎤 «лучше вечером»" in card["text"]
    assert "Купить наушники в обед" not in card["text"]  # the card answers the edit, not the first note


async def test_voice_reply_delete_shows_what_was_heard(ctx, add_voice, fake_ai, bot, make_items, t0) -> None:
    [created] = await make_items({"kind": "idea", "title": "Подкаст"}, now=t0)
    raw = await add_voice("удали", reply_item_id=created.item.id)
    fake_ai.push({"intent": "edit", "edit": {"action": "delete"}})
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})
    [card] = bot.sent
    assert card["text"].split("\n") == ["🎤 «удали»", "🗑 Удалено: 💡 <s>Подкаст</s>"]


async def test_heard_of_an_album_joins_its_voice_parts(add_raw, sessionmaker) -> None:
    photo = await add_raw("подпись", content_type="photo", media_group_id="g1")
    await add_raw(None, content_type="audio", media_group_id="g1", transcript="первая часть")
    await add_raw(None, content_type="audio", media_group_id="g1", transcript=" вторая ")
    text = await add_raw("просто текст")
    async with sessionmaker() as session:
        assert await query.heard(session, photo.id) == "первая часть вторая"
        assert await query.heard(session, text.id) is None
        assert await query.heard(session, None) is None
        assert await query.heard(session, 10**12) is None
