"""services.processor: the job handlers end to end (DB + fake bot / AI / STT)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetFile
from sqlalchemy import select

from dumpbot.ai.client import AIConfigError, AIUnavailable
from dumpbot.bot.cbdata import AskCb, PickCb
from dumpbot.db.models import AiUsage, Item, Job, OnceKey, RawMessage
from dumpbot.services import processor
from dumpbot.workers import jobs
from dumpbot.workers.jobs import JobWorker, RetryLater

OWNER = 4242
WARSAW_NEXT_MONTH = datetime(2026, 10, 31, 23, 0, tzinfo=UTC)  # 2026-11-01 00:00 Europe/Warsaw (CET)


def buttons(call: dict) -> list[tuple[str, str]]:
    markup = call.get("reply_markup")
    if markup is None:
        return []
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def reply_to(call: dict) -> int | None:
    params = call.get("reply_parameters")
    return params.message_id if params is not None else None


async def spend(sessionmaker, usd: str, when: datetime) -> None:
    async with sessionmaker() as session:
        session.add(AiUsage(created_at=when, purpose="extract", model="m", cost_usd=Decimal(usd)))
        await session.commit()


# --- save -----------------------------------------------------------------------------------------------------------


async def test_task_message_gets_card_and_reaction(
    ctx, add_raw, fake_ai, bot, items_of_raw, get, bot_messages, occurrences_of, reminders_of, local
) -> None:
    raw = await add_raw("завтра в 15 созвон с Олегом")
    fake_ai.push(
        {
            "items": [
                {
                    "kind": "event",
                    "title": "Созвон с Олегом",
                    "when": {"text": "завтра в 15", "day": "+1", "time": "15:00"},
                }
            ]
        }
    )
    await processor.handle_message(ctx, {"raw_id": raw.id})

    [item] = await items_of_raw(raw.id)
    assert (item.kind, item.title, item.source_text, item.source_kind) == (
        "event",
        "Созвон с Олегом",
        "завтра в 15 созвон с Олегом",
        "text",
    )
    assert (item.ai_model, item.prompt_version) == ("fake-model", "v1")
    [occ] = await occurrences_of(item.id)
    assert occ.due_at == local(2026, 10, 7, 15, 0)
    assert [r.fire_at for r in await reminders_of(occ.id)] == [local(2026, 10, 7, 14, 55)]

    [card] = bot.sent
    assert "Созвон с Олегом" in card["text"]
    assert reply_to(card) == raw.message_id
    assert any(text.startswith("✅") for text, _ in buttons(card))
    assert bot.reactions == [(raw.message_id, "👌")]
    [msg] = await bot_messages()
    assert (msg.purpose, msg.item_id, msg.occurrence_id, msg.raw_message_id) == ("card", item.id, occ.id, raw.id)

    stored = await get(RawMessage, raw.id)
    assert stored.status == "done" and stored.processed_at is not None
    [inp] = fake_ai.inputs
    assert (inp.text, inp.source, inp.now, inp.images, inp.force_query) == (raw.text, "text", raw.tg_date, [], False)
    assert ctx.scheduler_wakeup.is_set()

    # A redelivered job does nothing.
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert len(fake_ai.inputs) == 1 and len(await items_of_raw(raw.id)) == 1


async def test_note_gets_only_a_reaction(ctx, add_raw, fake_ai, bot, items_of_raw) -> None:
    raw = await add_raw("мысль: хорошо бы выучить польский")
    fake_ai.push({"items": [{"kind": "idea", "title": "Выучить польский", "tags": ["языки"]}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [item] = await items_of_raw(raw.id)
    assert item.kind == "idea" and item.tags == ["языки"]
    assert bot.sent == []
    assert bot.reactions == [(raw.message_id, "👌")]


async def test_several_items_from_one_message(ctx, add_raw, fake_ai, bot, items_of_raw) -> None:
    raw = await add_raw("купить хлеб; идея: подкаст")
    fake_ai.push({"items": [{"kind": "purchase", "title": "Купить хлеб"}, {"kind": "idea", "title": "Подкаст"}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert [i.kind for i in await items_of_raw(raw.id)] == ["purchase", "idea"]
    assert len(bot.sent) == 1 and "Купить хлеб" in bot.texts[0]  # only the actionable one gets a card


async def test_nothing_extracted_keeps_the_text_as_a_note(ctx, add_raw, fake_ai, bot, items_of_raw) -> None:
    raw = await add_raw("  \nпросто\nтекст без смысла")
    fake_ai.push({"items": []})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [item] = await items_of_raw(raw.id)
    assert (item.kind, item.title) == ("note", "просто")
    assert item.source_text == "просто\nтекст без смысла"
    assert bot.reactions == [(raw.message_id, "👌")]


async def test_links_get_titles_and_link_source(ctx, add_raw, fake_ai, items_of_raw, get) -> None:
    url = "https://habr.com/ru/articles/1/"
    fake_ai.titles = {url: "Статья про Postgres"}
    raw = await add_raw(f"почитать {url}", urls=[url])
    fake_ai.push({"items": [{"kind": "link", "title": "Статья про Postgres"}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [inp] = fake_ai.inputs
    assert inp.source == "link" and inp.link_titles == {url: "Статья про Postgres"}
    assert (await get(RawMessage, raw.id)).link_titles == {url: "Статья про Postgres"}
    [item] = await items_of_raw(raw.id)
    assert item.urls == [url]
    assert item.source == {"link_titles": {url: "Статья про Postgres"}}


async def test_forwarded_post_keeps_its_origin(ctx, add_raw, fake_ai, items_of_raw) -> None:
    origin = {"type": "channel", "title": "Хабр", "username": "habr", "url": "https://t.me/habr/5", "date": "x"}
    raw = await add_raw("интересный пост", forward_origin=origin)
    fake_ai.push({"items": [{"kind": "link", "title": "Пост Хабра"}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [inp] = fake_ai.inputs
    assert inp.source == "forward" and inp.forward_from == "канал «Хабр»"
    [item] = await items_of_raw(raw.id)
    assert item.source_kind == "forward"
    assert item.source == {"forward_origin": origin}


# --- voice / photo / album ------------------------------------------------------------------------------------------


async def test_voice_transcript_is_persisted_before_extract_and_reused(
    ctx, add_raw, fake_ai, bot, stt, get, items_of_raw, set_prefs
) -> None:
    await set_prefs(stt_language="ru")
    raw = await add_raw(None, content_type="voice", file_id="voice-1", mime_type="audio/ogg", duration=5)
    bot.files["voice-1"] = b"OGG-BYTES"
    stt.results = ["напомни купить хлеб"]
    fake_ai.push(AIUnavailable("503"))

    with pytest.raises(AIUnavailable):
        await processor.handle_message(ctx, {"raw_id": raw.id})
    stored = await get(RawMessage, raw.id)
    assert stored.transcript == "напомни купить хлеб"
    assert stored.status == "deferred"
    assert stt.calls == [{"audio": b"OGG-BYTES", "mime_type": "audio/ogg", "language": "ru"}]

    fake_ai.push({"items": [{"kind": "purchase", "title": "Купить хлеб"}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert len(stt.calls) == 1  # not paid twice
    assert [(i.text, i.source) for i in fake_ai.inputs] == [("напомни купить хлеб", "voice")] * 2
    [item] = await items_of_raw(raw.id)
    assert item.source_text == "напомни купить хлеб" and item.source_kind == "voice"
    assert bot.reactions == [(raw.message_id, "✍"), (raw.message_id, "👌")]


async def test_photo_goes_to_vision_and_ocr_is_stored(ctx, add_raw, fake_ai, bot, get, items_of_raw) -> None:
    raw = await add_raw("чек", content_type="photo", file_id="ph-1", mime_type="image/jpeg")
    bot.files["ph-1"] = b"\xff\xd8JPEG"
    fake_ai.push(
        {
            "items": [{"kind": "note", "title": "Чек Biedronka"}],
            "image": {"text": "BIEDRONKA 12,50 PLN", "description": "Чек из магазина"},
        }
    )
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [inp] = fake_ai.inputs
    assert inp.images == [(b"\xff\xd8JPEG", "image/jpeg")] and inp.source == "photo"
    assert (await get(RawMessage, raw.id)).image_text == "BIEDRONKA 12,50 PLN\nЧек из магазина"
    [item] = await items_of_raw(raw.id)
    assert item.source_text == "чек\n\nBIEDRONKA 12,50 PLN\nЧек из магазина"


async def test_unsupported_picture_without_text_is_saved_without_ai(ctx, add_raw, fake_ai, bot, items_of_raw) -> None:
    raw = await add_raw(None, content_type="image_document", file_id="heic", mime_type="image/heic")
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert fake_ai.inputs == [] and bot.calls_of("download") == []
    [item] = await items_of_raw(raw.id)
    assert (item.kind, item.title) == ("other", "Картинка")


async def test_album_is_one_extraction_with_up_to_four_pictures(ctx, add_raw, fake_ai, bot, items_of_raw, db) -> None:
    parts = [
        await add_raw(None, content_type="photo", file_id="p0", media_group_id="G", message_id=200),
        await add_raw("отпуск, фото для мамы", content_type="photo", file_id="p1", media_group_id="G", message_id=201),
        await add_raw(
            None, content_type="image_document", file_id="d2", mime_type="image/png", media_group_id="G", message_id=202
        ),
        await add_raw(None, content_type="photo", file_id="p3", media_group_id="G", message_id=203),
        await add_raw(None, content_type="photo", file_id="p4", media_group_id="G", message_id=204),
    ]
    other_group = await add_raw("чужой", content_type="photo", file_id="x", media_group_id="H", message_id=205)
    fake_ai.push({"items": [{"kind": "note", "title": "Фото из отпуска"}]})
    await processor.handle_album(ctx, {"media_group_id": "G", "chat_id": OWNER})

    [inp] = fake_ai.inputs
    assert inp.text == "отпуск, фото для мамы"
    assert [mime for _, mime in inp.images] == ["image/jpeg", "image/jpeg", "image/png", "image/jpeg"]
    assert inp.images[0][0] == b"bytes-of-p0"
    [item] = await items_of_raw(parts[0].id)
    assert item.title == "Фото из отпуска"
    raws = await db(select(RawMessage).order_by(RawMessage.message_id))
    assert [r.status for r in raws] == ["done"] * 5 + ["new"]
    assert raws[-1].id == other_group.id
    assert bot.reactions == [(200, "👌")]

    await processor.handle_album(ctx, {"media_group_id": "G", "chat_id": OWNER})  # all parts done: no-op
    assert len(fake_ai.inputs) == 1


@pytest.mark.parametrize(
    ("kwargs", "title"),
    [
        ({"content_type": "sticker", "file_id": "s", "raw_json": {"sticker": {"emoji": "😀"}}}, "Стикер 😀"),
        ({"content_type": "video", "file_id": "v"}, "Видео"),
        ({"content_type": "video_note", "file_id": "vn"}, "Кружочек"),
        ({"content_type": "animation", "file_id": "a"}, "GIF"),
        ({"content_type": "location", "raw_json": {"location": {"latitude": 1, "longitude": 2}}}, "Геолокация"),
        ({"content_type": "location", "raw_json": {"venue": {"title": "Кафе"}}}, "Место: Кафе"),
        (
            {"content_type": "contact", "raw_json": {"contact": {"first_name": "Оля", "last_name": "К"}}},
            "Контакт: Оля К",
        ),
        ({"content_type": "document", "file_id": "d", "file_name": "cv.pdf"}, "Документ: cv.pdf"),
        ({"content_type": "other"}, "Сообщение"),
    ],
)
async def test_media_without_text_is_saved_as_other_without_ai(
    ctx, add_raw, fake_ai, bot, items_of_raw, get, kwargs, title
) -> None:
    raw = await add_raw(None, **kwargs)
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert fake_ai.inputs == []
    [item] = await items_of_raw(raw.id)
    assert (item.kind, item.title, item.source_kind, item.ai_model) == ("other", title, "other", None)
    assert bot.sent == [] and bot.reactions == [(raw.message_id, "👌")]
    assert (await get(RawMessage, raw.id)).status == "done"


async def test_forwarded_video_without_caption_keeps_origin(ctx, add_raw, fake_ai, items_of_raw) -> None:
    origin = {"type": "channel", "title": "Мемы", "url": "https://t.me/c/1/2"}
    raw = await add_raw(None, content_type="video", file_id="v", forward_origin=origin)
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [item] = await items_of_raw(raw.id)
    assert item.source_kind == "forward" and item.source == {"forward_origin": origin}
    assert fake_ai.inputs == []


async def test_media_with_caption_goes_to_ai_as_other(ctx, add_raw, fake_ai, bot) -> None:
    raw = await add_raw("посмотреть вечером", content_type="video", file_id="v")
    fake_ai.push({"items": [{"kind": "task", "title": "Посмотреть видео"}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [inp] = fake_ai.inputs
    assert (inp.text, inp.source, inp.images) == ("посмотреть вечером", "other", [])
    assert bot.calls_of("download") == []


async def test_download_errors_retry_later_and_too_big_is_skipped(ctx, add_raw, fake_ai, bot, items_of_raw) -> None:
    raw = await add_raw(None, content_type="photo", file_id="ph")
    bot.fail(
        "download",
        TelegramNetworkError(method=GetFile(file_id="ph"), message="https://api.telegram.org/file/bot1:SECRET/x"),
    )
    with pytest.raises(RetryLater) as exc:
        await processor.handle_message(ctx, {"raw_id": raw.id})
    assert exc.value.delay == timedelta(minutes=5) and exc.value.count_attempt
    assert "SECRET" not in exc.value.reason

    bot.fail("download", TelegramBadRequest(method=GetFile(file_id="ph"), message="Bad Request: file is too big"))
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert fake_ai.inputs == []
    [item] = await items_of_raw(raw.id)
    assert (item.kind, item.title) == ("other", "Фото")


# --- query ----------------------------------------------------------------------------------------------------------


async def test_query_list(ctx, add_raw, fake_ai, bot, make_items, items_of_raw, t0, frozen) -> None:
    await make_items({"kind": "task", "title": "Спорт", "when": {"day": "+1", "time": "18:00"}}, now=t0)
    raw = await add_raw("?что у меня завтра", force_query=True)
    fake_ai.push({"intent": "query", "query": {"range": "tomorrow", "answer": "list"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})

    [inp] = fake_ai.inputs
    assert inp.text == "что у меня завтра" and inp.force_query
    [answer] = bot.sent
    assert "Спорт" in answer["text"] and "18:00" in answer["text"]
    assert reply_to(answer) == raw.message_id
    assert await items_of_raw(raw.id) == []
    assert bot.reactions == [(raw.message_id, "👌")]


async def test_query_summary_is_escaped_plain_text(ctx, add_raw, fake_ai, bot, make_items, t0, bot_messages) -> None:
    await make_items({"kind": "event", "title": "Созвон", "when": {"day": "+1", "time": "10:00"}}, now=t0)
    raw = await add_raw("что у меня завтра?")
    fake_ai.push({"intent": "query", "query": {"range": "tomorrow", "answer": "summary"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [call] = fake_ai.answer_calls
    assert call["question"] == "что у меня завтра?"
    assert [r["title"] for r in call["rows"]] == ["Созвон"]
    assert bot.texts == ["Завтра &lt;созвон&gt; &amp; спорт"]
    assert [m.purpose for m in await bot_messages()] == ["answer"]


async def test_query_with_nothing_found(ctx, add_raw, fake_ai, bot) -> None:
    raw = await add_raw("какие у меня рецепты?")
    fake_ai.push({"intent": "query", "query": {"text": "рецепт"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert "Ничего не нашёл" in bot.texts[0]


# --- edit -----------------------------------------------------------------------------------------------------------

EDIT_TO_16 = {"intent": "edit", "edit": {"target": "встреча Олег", "when": {"day": "+1", "time": "16:00"}}}


async def test_edit_by_target_single_candidate(
    ctx, add_raw, fake_ai, bot, make_items, t0, occurrences_of, local, get
) -> None:
    [created] = await make_items(
        {"kind": "event", "title": "Встреча с Олегом", "when": {"day": "+1", "time": "15:00"}}, now=t0
    )
    raw = await add_raw("перенеси встречу с Олегом на 16")
    fake_ai.push(EDIT_TO_16)
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [occ] = [o for o in await occurrences_of(created.item.id) if o.status == "open"]
    assert occ.due_at == local(2026, 10, 7, 16, 0)
    [card] = bot.sent
    assert "Встреча с Олегом" in card["text"] and reply_to(card) == raw.message_id
    assert bot.reactions == [(raw.message_id, "👌")]
    assert (await get(RawMessage, raw.id)).status == "done"


async def test_edit_by_target_many_candidates_offers_a_pick(ctx, add_raw, fake_ai, bot, make_items, t0) -> None:
    created = await make_items(
        {"kind": "event", "title": "Встреча с Олегом", "when": {"day": "+1", "time": "15:00"}},
        {"kind": "event", "title": "Встреча с Олегом по проекту", "when": {"day": "+2", "time": "11:00"}},
        now=t0,
    )
    raw = await add_raw("перенеси встречу с Олегом на 16")
    fake_ai.push(EDIT_TO_16)
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [msg] = bot.sent
    picks = {data for _, data in buttons(msg)}
    assert picks == {PickCb(raw=raw.id, item=c.item.id).pack() for c in created}
    assert bot.reactions == [(raw.message_id, "🤔")]


async def test_edit_by_target_without_candidates(ctx, add_raw, fake_ai, bot) -> None:
    raw = await add_raw("перенеси отпуск")
    fake_ai.push({"intent": "edit", "edit": {"target": "отпуск в Испании", "when": {"day": "+1"}}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert bot.texts == [processor.NOT_FOUND_TEXT]


async def test_reply_edit_applies_once(ctx, add_raw, fake_ai, bot, make_items, t0, get) -> None:
    [created] = await make_items({"kind": "task", "title": "Купить наушники"}, now=t0)
    item_id = created.item.id
    raw = await add_raw("назови это «Купить AirPods»", reply_item_id=item_id, message_id=700)
    fake_ai.push({"intent": "edit", "edit": {"title": "Купить AirPods"}})
    payload = {"raw_id": raw.id, "item_id": item_id}
    await processor.handle_edit(ctx, payload)

    [inp] = fake_ai.inputs
    assert inp.reply_item is not None and inp.reply_item["id"] == item_id
    assert inp.reply_item["title"] == "Купить наушники"
    assert (await get(Item, item_id)).title == "Купить AirPods"
    [card] = bot.sent
    assert "Купить AirPods" in card["text"] and reply_to(card) == 700
    assert bot.reactions == [(700, "👌")]
    assert await get(OnceKey, f"edit:{raw.id}:{item_id}") is not None

    await processor.handle_edit(ctx, payload)  # redelivered: applied once only
    assert len(fake_ai.inputs) == 1 and len(bot.sent) == 1


async def test_reply_edit_delete_shows_deleted_card(ctx, add_raw, fake_ai, bot, make_items, t0, get) -> None:
    [created] = await make_items({"kind": "idea", "title": "Подкаст"}, now=t0)
    raw = await add_raw("удали", reply_item_id=created.item.id)
    fake_ai.push({"intent": "edit", "edit": {"action": "delete"}})
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})
    assert (await get(Item, created.item.id)).status == "deleted"
    assert "Удалено" in bot.texts[0]


async def test_picked_candidate_runs_the_edit_for_an_already_done_raw(
    ctx, add_raw, fake_ai, bot, make_items, t0, occurrences_of, local
) -> None:
    [created] = await make_items({"kind": "event", "title": "Встреча", "when": {"day": "+1", "time": "15:00"}}, now=t0)
    raw = await add_raw("перенеси встречу на 16", status="done")
    fake_ai.push({"intent": "edit", "edit": {"when": {"day": "+1", "time": "16:00"}}})
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})
    [occ] = [o for o in await occurrences_of(created.item.id) if o.status == "open"]
    assert occ.due_at == local(2026, 10, 7, 16, 0)


async def test_edit_time_job(ctx, fake_ai, bot, make_items, t0, occurrences_of, local) -> None:
    [created] = await make_items({"kind": "task", "title": "Отчёт", "when": {"day": "+1", "time": "15:00"}}, now=t0)
    fake_ai.push({"intent": "edit", "edit": {"when": {"text": "в пятницу 18", "day": "FR", "time": "18:00"}}})
    await processor.handle_edit_time(ctx, {"item_id": created.item.id, "text": "в пятницу 18", "message_id": 555})
    [inp] = fake_ai.inputs
    assert inp.text == "перенеси на: в пятницу 18" and inp.reply_item["id"] == created.item.id
    [occ] = [o for o in await occurrences_of(created.item.id) if o.status == "open"]
    assert occ.due_at == local(2026, 10, 9, 18, 0)
    assert reply_to(bot.sent[0]) == 555
    assert bot.reactions == [(555, "👌")]


# --- clarification --------------------------------------------------------------------------------------------------


async def test_ask_then_clarify_replaces_items(ctx, add_raw, fake_ai, bot, items_of_raw, get) -> None:
    raw = await add_raw("в 8 позвонить маме")
    fake_ai.push(
        {
            "items": [{"kind": "task", "title": "Позвонить маме", "when": {"time": "08:00"}}],
            "ask": {"item": 0, "q": "В 8 утра или вечера?", "options": ["утра", "вечера"]},
        }
    )
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [ask] = bot.sent
    assert "В 8 утра или вечера?" in ask["text"]
    assert [data for _, data in buttons(ask)] == [AskCb(raw=raw.id, n=0).pack(), AskCb(raw=raw.id, n=1).pack()]
    assert bot.reactions == [(raw.message_id, "🤔")]
    [first] = await items_of_raw(raw.id)
    assert (await get(RawMessage, raw.id)).raw_json[processor.ASK_KEY]["options"] == ["утра", "вечера"]

    fake_ai.push({"items": [{"kind": "task", "title": "Позвонить маме", "when": {"time": "20:00"}}]})
    await processor.handle_clarify(ctx, {"raw_id": raw.id, "option": 1})
    assert fake_ai.inputs[-1].clarification == "вечера"
    [second] = await items_of_raw(raw.id)
    assert second.id != first.id
    assert (await get(Item, first.id)).status == "deleted"
    assert "Позвонить маме" in bot.texts[-1]
    assert bot.reactions[-1] == (raw.message_id, "👌")


async def test_clarify_accepts_option_text(ctx, add_raw, fake_ai) -> None:
    raw = await add_raw("в 8 позвонить маме", status="done")
    fake_ai.push({"items": [{"kind": "task", "title": "Позвонить маме"}]})
    await processor.handle_clarify(ctx, {"raw_id": raw.id, "option": "утра"})
    assert fake_ai.inputs[-1].clarification == "утра"


# --- budget / AI errors ---------------------------------------------------------------------------------------------


async def test_budget_80_percent_notice_once(ctx, add_raw, fake_ai, bot, sessionmaker, frozen, items_of_raw) -> None:
    await spend(sessionmaker, "4.10", frozen.now)
    for text in ("идея раз", "идея два"):
        raw = await add_raw(text)
        fake_ai.push({"items": [{"kind": "idea", "title": text}]})
        await processor.handle_message(ctx, {"raw_id": raw.id})
        assert len(await items_of_raw(raw.id)) == 1
    notices = [t for t in bot.texts if "бюджета" in t]
    assert len(notices) == 1 and "82%" in notices[0] and "$4.10 из $5.00" in notices[0]


async def test_budget_exhausted_defers_until_next_month(
    ctx, add_raw, fake_ai, bot, sessionmaker, frozen, get, jobs_all
) -> None:
    await spend(sessionmaker, "5.00", frozen.now)
    first = await add_raw("раз")
    with pytest.raises(RetryLater) as exc:
        await processor.handle_message(ctx, {"raw_id": first.id})
    assert exc.value.at == WARSAW_NEXT_MONTH
    assert exc.value.count_attempt is False and exc.value.reason == jobs.BUDGET_REASON
    assert (await get(RawMessage, first.id)).status == "deferred"
    assert fake_ai.inputs == []
    assert bot.reactions == [(first.message_id, "✍")]
    [notice] = bot.texts
    assert "исчерпан" in notice and "вс 1 ноя" in notice

    # Through the queue: the attempt is not used up; a second message gets no second notice.
    second = await add_raw("два")
    async with sessionmaker() as session:
        await jobs.enqueue(session, "message", {"raw_id": second.id}, dedupe_key=f"msg:{second.id}")
        await session.commit()
    await JobWorker(ctx, processor.HANDLERS).run_once()
    [job] = await jobs_all()
    assert (job.status, job.attempts, job.run_after, job.last_error) == (
        "queued",
        0,
        WARSAW_NEXT_MONTH,
        jobs.BUDGET_REASON,
    )
    assert len(bot.texts) == 1
    assert bot.reactions[-1] == (second.message_id, "✍")

    # The owner raises the budget: the settings router calls requeue_deferred.
    async with sessionmaker() as session:
        assert await jobs.requeue_deferred(session, frozen.now) == 1
        await session.commit()


async def test_ai_unavailable_reacts_once_and_backs_off(
    ctx, add_raw, fake_ai, bot, sessionmaker, frozen, jobs_all, get
):
    raw = await add_raw("заметка")
    async with sessionmaker() as session:
        await jobs.enqueue(session, "message", {"raw_id": raw.id})
        await session.commit()
    fake_ai.push(AIUnavailable("timeout"), AIUnavailable("timeout"))
    worker = JobWorker(ctx, processor.HANDLERS)
    await worker.run_once()
    [job] = await jobs_all()
    assert job.status == "queued" and job.run_after == frozen.now + timedelta(minutes=2)
    frozen.set(job.run_after)
    await worker.run_once()
    assert bot.reactions == [(raw.message_id, "✍")]  # once
    assert (await get(RawMessage, raw.id)).status == "deferred"


async def test_ai_config_error_notifies_once_a_day(ctx, add_raw, fake_ai, bot, get) -> None:
    raws = [await add_raw("раз"), await add_raw("два")]
    fake_ai.push(AIConfigError("HTTP 402: no balance"), AIConfigError("HTTP 402: no balance"))
    for raw in raws:
        with pytest.raises(RetryLater) as exc:
            await processor.handle_message(ctx, {"raw_id": raw.id})
        assert exc.value.delay == timedelta(hours=1) and exc.value.count_attempt is False
        assert (await get(RawMessage, raw.id)).status == "deferred"
    [notice] = bot.texts
    assert "chutes.ai" in notice and "402" in notice
    assert [emoji for _, emoji in bot.reactions] == ["✍", "✍"]


# --- on_dead --------------------------------------------------------------------------------------------------------


async def test_on_dead_for_album_and_edit_time(ctx, add_raw, bot, get) -> None:
    a = await add_raw(None, content_type="photo", media_group_id="G", message_id=300)
    b = await add_raw(None, content_type="photo", media_group_id="G", message_id=301)
    job = Job(id=1, kind="album", payload={"media_group_id": "G"}, last_error="ValueError at x.py:1")
    await processor.on_dead(ctx, job)
    assert [(await get(RawMessage, r.id)).status for r in (a, b)] == ["failed", "failed"]
    assert bot.reactions == [(300, "😢")]

    await processor.on_dead(ctx, Job(id=2, kind="edit_time", payload={"item_id": 1, "message_id": 9}))
    assert "Не смог применить изменение" in bot.texts[-1]
    assert bot.reactions[-1] == (9, "😢")
