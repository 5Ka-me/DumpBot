"""services.processor: the job handlers end to end (DB + fake bot / AI / STT)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetFile
from sqlalchemy import select

from dumpbot.ai.client import AIBadResponse, AIConfigError, AIUnavailable
from dumpbot.ai.extract import ExtractOutput
from dumpbot.ai.schemas import EditPatch, Extraction
from dumpbot.bot.cbdata import AskCb, PickCb, UndoCb
from dumpbot.db.models import AiUsage, Item, Job, OnceKey, RawMessage
from dumpbot.domain.prefs import Prefs
from dumpbot.services import ingest, items, processor
from dumpbot.workers import jobs
from dumpbot.workers.jobs import JobWorker, RetryLater

OWNER = 4242
# 2026-11-01 08:00 Europe/Warsaw (CET): the new month starts at 00:00, inside the default 23:00-08:00 quiet hours.
WARSAW_NEXT_MONTH = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)


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
    assert [r.fire_at for r in await reminders_of(occ.id)] == [local(2026, 10, 7, 15, 0)]  # no "за 5 минут"

    [card] = bot.sent
    assert "Созвон с Олегом" in card["text"]
    assert reply_to(card) == raw.message_id
    assert not any(text.startswith("✅") for text, _ in buttons(card))  # done lives in the reminder and ✏️ menu
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


async def test_messages_processed_in_quiet_hours_are_silent(ctx, add_raw, fake_ai, bot, frozen, local) -> None:
    # Deferred at 20:00 (AI down), processed by a retry at 02:00: the card must not ring at night.
    raw = await add_raw("завтра в 15 созвон", tg_date=local(2026, 10, 6, 20, 0))
    frozen.set(local(2026, 10, 7, 2, 0))
    fake_ai.push({"items": [{"kind": "event", "title": "Созвон", "when": {"day": "+1", "time": "15:00"}}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})

    frozen.set(local(2026, 10, 7, 9, 0))
    raw = await add_raw("завтра в 16 спорт")
    fake_ai.push({"items": [{"kind": "event", "title": "Спорт", "when": {"day": "+1", "time": "16:00"}}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})

    night, day = bot.sent
    assert (night["disable_notification"], day["disable_notification"]) == (True, None)


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


async def test_forwarded_post_is_never_applied_as_an_edit(
    ctx, add_raw, fake_ai, bot, make_items, t0, get, items_of_raw
):
    # A friend's "Встреча с Олегом отменяется, удали из календаря", forwarded to keep it.
    [meeting] = await make_items({"kind": "event", "title": "Встреча с Олегом", "when": {"time": "15:00"}}, now=t0)
    origin = {"type": "user", "sender_name": "Петя"}
    raw = await add_raw("Встреча с Олегом отменяется, удали из календаря", forward_origin=origin)
    fake_ai.push({"intent": "edit", "edit": {"target": "встреча Олег", "action": "delete"}})

    await processor.handle_message(ctx, {"raw_id": raw.id})

    assert (await get(Item, meeting.item.id)).status == "open"
    [saved] = await items_of_raw(raw.id)
    assert (saved.kind, saved.title, saved.source_kind) == (
        "note",
        "Встреча с Олегом отменяется, удали из календаря",
        "forward",
    )
    assert not any("Удалено" in text for text in bot.texts)
    assert (await get(RawMessage, raw.id)).status == "done"


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


async def test_long_audio_is_not_transcribed(ctx, add_raw, fake_ai, bot, stt, items_of_raw) -> None:
    # A forwarded 40-minute lecture: STT bills GPU seconds and would time out, then retry for a week.
    podcast = await add_raw(
        None, content_type="audio", file_id="a1", file_name="podcast.mp3", mime_type="audio/mpeg", duration=2400
    )
    await processor.handle_message(ctx, {"raw_id": podcast.id})
    assert stt.calls == [] and bot.calls_of("download") == [] and fake_ai.inputs == []
    [item] = await items_of_raw(podcast.id)
    assert (item.kind, item.title) == ("other", "Аудио: podcast.mp3")
    assert bot.reactions == [(podcast.message_id, "👌")]

    lecture = await add_raw("лекция про asyncio, послушать", content_type="audio", file_id="a2", duration=2400)
    fake_ai.push({"items": [{"kind": "task", "title": "Послушать лекцию про asyncio"}]})
    await processor.handle_message(ctx, {"raw_id": lecture.id})
    assert stt.calls == []
    [inp] = fake_ai.inputs
    assert inp.text == "лекция про asyncio, послушать"

    short = await add_raw(None, content_type="voice", file_id="v1", duration=ctx.settings.stt_max_duration_s)
    fake_ai.push({"items": [{"kind": "note", "title": "Голосовая заметка"}]})
    await processor.handle_message(ctx, {"raw_id": short.id})
    assert len(stt.calls) == 1


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
    # Every part got 👀 from the capture router: every part gets the final reaction, not only the first.
    assert bot.reactions == [(m, "👌") for m in range(200, 205)]

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


async def test_huge_picture_is_not_downloaded_or_sent(ctx, add_raw, fake_ai, bot, items_of_raw) -> None:
    # A 20 MB PNG sent as a file: ~27 MB of base64 would outlast llm_timeout_s on a home uplink.
    raw = await add_raw(
        None, content_type="image_document", file_id="big", mime_type="image/png", file_size=20 * 1024 * 1024
    )
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert bot.calls_of("download") == [] and fake_ai.inputs == []
    [item] = await items_of_raw(raw.id)
    assert (item.kind, item.title) == ("other", "Картинка")


async def test_album_pictures_are_capped_in_size(ctx, add_raw, fake_ai, bot) -> None:
    mb = 1024 * 1024
    bot.files = {"p1": b"1" * (7 * mb), "p3": b"3" * 100, "p4": b"4" * (9 * mb)}
    await add_raw(None, content_type="photo", file_id="p0", file_size=9 * mb, media_group_id="G", message_id=200)
    await add_raw(
        None, content_type="image_document", file_id="p1", mime_type="image/png", media_group_id="G", message_id=201
    )
    await add_raw(None, content_type="photo", file_id="p2", file_size=6 * mb, media_group_id="G", message_id=202)
    await add_raw("скриншоты", content_type="photo", file_id="p3", media_group_id="G", message_id=203)
    await add_raw(None, content_type="photo", file_id="p4", media_group_id="G", message_id=204)  # size unknown
    fake_ai.push({"items": [{"kind": "note", "title": "Скриншоты"}]})

    await processor.handle_album(ctx, {"media_group_id": "G", "chat_id": OWNER})

    [inp] = fake_ai.inputs
    # p0 is over the per-picture limit, p2 no longer fits next to p1, p4 turns out too big after the download.
    assert [data[:1] for data, _ in inp.images] == [b"1", b"3"]
    assert [call["file"] for call in bot.calls_of("download")] == ["p1", "p3", "p4"]


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


@pytest.mark.parametrize("error", [AIBadResponse("empty answer from m"), AIUnavailable("503")])
async def test_query_summary_falls_back_to_the_list(
    ctx, add_raw, fake_ai, bot, make_items, t0, get, sessionmaker, jobs_all, error
) -> None:
    # The rows are found already: a retry would pay for the extraction again just for the prose.
    await make_items({"kind": "event", "title": "Созвон", "when": {"day": "+1", "time": "10:00"}}, now=t0)
    raw = await add_raw("что у меня завтра?")
    async with sessionmaker() as session:
        await jobs.enqueue(session, "message", {"raw_id": raw.id})
        await session.commit()
    fake_ai.push({"intent": "query", "query": {"range": "tomorrow", "answer": "summary"}})
    fake_ai.answers = [error]

    await JobWorker(ctx, processor.HANDLERS).run_once()

    assert [j.status for j in await jobs_all()] == ["done"]
    assert len(fake_ai.inputs) == 1 and len(fake_ai.answer_calls) == 1
    [answer] = bot.texts
    assert "Созвон" in answer and "10:00" in answer
    assert bot.reactions == [(raw.message_id, "👌")]
    assert (await get(RawMessage, raw.id)).status == "done"


# --- edit -----------------------------------------------------------------------------------------------------------

EDIT_TO_16 = {"intent": "edit", "edit": {"target": "встреча Олег", "when": {"day": "+1", "time": "16:00"}}}


async def test_edit_by_target_single_candidate(
    ctx, add_raw, fake_ai, bot, make_items, t0, occurrences_of, local, get, bot_messages
) -> None:
    [created] = await make_items(
        {"kind": "event", "title": "Встреча с Олегом", "when": {"day": "+1", "time": "15:00"}}, now=t0
    )
    raw = await add_raw("перенеси встречу с Олегом на 16")
    fake_ai.push(EDIT_TO_16)
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [occ] = [o for o in await occurrences_of(created.item.id) if o.status == "open"]
    assert occ.due_at == local(2026, 10, 7, 16, 0)
    [reply] = bot.sent
    assert reply["text"] == "🕒 Перенёс «Встреча с Олегом» на завтра 16:00"
    assert reply_to(reply) == raw.message_id
    assert buttons(reply) == [("↩️ Отменить", UndoCb(raw=raw.id, item=created.item.id).pack())]
    [record] = await bot_messages()  # a reply to it edits the item, like a reply to a card
    assert (record.purpose, record.item_id, record.raw_message_id) == ("edited", created.item.id, raw.id)
    assert bot.reactions == [(raw.message_id, "👌")]
    stored = await get(RawMessage, raw.id)
    assert stored.status == "done" and str(created.item.id) in stored.raw_json[ingest.UNDO_KEY]


async def test_edit_by_target_many_candidates_offers_a_pick(
    ctx, add_raw, fake_ai, bot, make_items, t0, get, bot_messages
) -> None:
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
    [record] = await bot_messages()
    assert (record.purpose, record.raw_message_id) == ("pick", raw.id)
    # The list order is kept with the raw: a text reply "2" picks the second item; and so is the edit itself.
    data = (await get(RawMessage, raw.id)).raw_json
    stored = data[processor.CANDIDATES_KEY]
    assert sorted(stored) == sorted(c.item.id for c in created) and len(stored) == 2
    assert data[ingest.EDIT_KEY] == EditPatch.model_validate(EDIT_TO_16["edit"]).model_dump(
        mode="json", exclude_none=True
    )


async def test_edit_by_target_loose_single_match_asks_first(
    ctx, add_raw, fake_ai, bot, make_items, t0, get, bot_messages
) -> None:
    # "удалить приложение банка" read as an edit: only the word "банк" matches an unrelated task.
    [credit] = await make_items({"kind": "task", "title": "Оплатить кредит в банке"}, now=t0)
    raw = await add_raw("удалить приложение банка")
    fake_ai.push({"intent": "edit", "edit": {"target": "приложение банка", "action": "delete"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})

    assert (await get(Item, credit.item.id)).status == "open"
    [msg] = bot.sent
    assert buttons(msg) == [("1", PickCb(raw=raw.id, item=credit.item.id).pack())]
    assert bot.reactions == [(raw.message_id, "🤔")]


@pytest.mark.parametrize(
    ("action", "text"),
    [
        ("delete", "🗑 Удалил «Встреча с Олегом»"),
        ("done", "✅ Закрыл «Встреча с Олегом»"),
        ("no_reminder", "🔕 Выключил напоминания «Встреча с Олегом»"),
        ("stop_recurrence", "⏹ Остановил повтор «Встреча с Олегом»"),
    ],
)
async def test_edit_by_target_applies_any_action_to_a_single_exact_match(
    ctx, add_raw, fake_ai, bot, make_items, t0, get, action, text
) -> None:
    """Iteration 3, §3: one exact match among the open items -> applied at once, delete / done too, with an undo."""
    spec = {"kind": "event", "title": "Встреча с Олегом", "when": {"time": "15:00"}}
    if action == "stop_recurrence":
        spec |= {"rrule": "FREQ=DAILY"}
    [created] = await make_items(spec, now=t0)
    raw = await add_raw("удали встречу с Олегом")
    fake_ai.push({"intent": "edit", "edit": {"target": "встреча Олег", "action": action}})
    await processor.handle_message(ctx, {"raw_id": raw.id})

    item = await get(Item, created.item.id)
    changed = {
        "delete": item.status == "deleted",
        "done": item.status == "done",
        "no_reminder": item.lead_minutes == [],
        "stop_recurrence": item.rec_active is False,
    }
    assert changed[action]
    [msg] = bot.sent
    assert msg["text"].startswith(text)
    assert buttons(msg) == [("↩️ Отменить", UndoCb(raw=raw.id, item=created.item.id).pack())]
    assert len(fake_ai.inputs) == 1
    assert bot.reactions == [(raw.message_id, "👌")]


async def test_edit_by_target_picks_the_single_open_match_among_exact_ones(
    ctx, add_raw, fake_ai, bot, make_items, t0, get
) -> None:
    """ "Закрой задачу про документацию": an old one done last week matches too, the open one is meant."""
    old, current = await make_items(
        {"kind": "task", "title": "Написать документацию"},
        {"kind": "task", "title": "Обновить документацию", "when": {"time": "17:00"}},
        now=t0,
    )
    async with ctx.sessionmaker() as session:
        await items.apply_patch(session, old.item.id, EditPatch(action="done"), Prefs(), t0)
        await session.commit()
    raw = await add_raw("закрой задачу про документацию")
    fake_ai.push({"intent": "edit", "edit": {"target": "документацию", "action": "done"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})

    assert (await get(Item, current.item.id)).status == "done"
    assert bot.texts == ["✅ Закрыл «Обновить документацию»"]

    # "верни": among the done ones
    raw = await add_raw("верни задачу про документацию")
    fake_ai.push({"intent": "edit", "edit": {"target": "документацию", "action": "undone"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert bot.texts[-1].startswith("✏️ Какую запись изменить?")  # both are done now: which one?


async def test_edit_by_target_done_of_a_series_names_the_next_time(ctx, add_raw, fake_ai, bot, make_items, t0) -> None:
    await make_items({"kind": "task", "title": "Зарядка", "rrule": "FREQ=DAILY", "when": {"time": "09:00"}}, now=t0)
    raw = await add_raw("сделал зарядку")
    fake_ai.push({"intent": "edit", "edit": {"target": "Зарядка", "action": "done"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert bot.texts == ["✅ Закрыл «Зарядка» · следующий раз чт 8 окт 09:00"]


@pytest.mark.parametrize("action", ["delete", "done", "no_reminder", "stop_recurrence"])
async def test_edit_by_target_from_the_app_still_asks_before_destructive_actions(
    ctx, add_raw, fake_ai, bot, make_items, t0, get, action
) -> None:
    """The Mini App has no [↩️ Отменить]: a destructive edit waits for the pick there, even for one exact match."""
    [created] = await make_items({"kind": "event", "title": "Встреча с Олегом", "when": {"time": "15:00"}}, now=t0)
    raw = await add_raw("удали встречу с Олегом", source="app", message_id=-1)
    fake_ai.push({"intent": "edit", "edit": {"target": "встреча Олег", "action": action}})
    await processor.handle_message(ctx, {"raw_id": raw.id})

    item = await get(Item, created.item.id)
    assert (item.status, item.updated_at) == ("open", created.item.updated_at)
    assert (await get(RawMessage, raw.id)).raw_json[processor.CANDIDATES_KEY] == [created.item.id]
    assert bot.calls == []


async def test_edit_by_target_without_candidates_keeps_the_message(ctx, add_raw, fake_ai, bot, items_of_raw) -> None:
    raw = await add_raw("перенеси отпуск")
    fake_ai.push({"intent": "edit", "edit": {"target": "отпуск в Испании", "when": {"day": "+1"}}})
    await processor.handle_message(ctx, {"raw_id": raw.id})

    [item] = await items_of_raw(raw.id)
    assert (item.kind, item.title) == ("note", "перенеси отпуск")
    [card] = bot.texts
    assert card.startswith(processor.NOT_FOUND_TEXT) and "перенеси отпуск" in card
    assert "i:edit:" in str(buttons(bot.sent[0]))  # fixable with ✏️ (category, time)
    assert bot.reactions == [(raw.message_id, "👌")]


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


async def test_pick_applies_the_edit_read_from_the_message_without_the_ai(
    ctx, add_raw, fake_ai, bot, make_items, t0, get, occurrences_of, local, bot_messages
) -> None:
    """Iteration 3, §3: two matches -> "Какую запись изменить?"; the pick (button or "2") gets the edit already read,
    no second extract call, and the same short reply with [↩️ Отменить]; a redelivered job changes nothing."""
    first, second = await make_items(
        {"kind": "event", "title": "Встреча с Олегом", "when": {"day": "+1", "time": "15:00"}},
        {"kind": "event", "title": "Встреча с Олегом по проекту", "when": {"day": "+2", "time": "11:00"}},
        now=t0,
    )
    raw = await add_raw("перенеси встречу с Олегом на 16")
    fake_ai.push(EDIT_TO_16)
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert bot.texts[0].startswith("✏️ Какую запись изменить?")

    payload = {"raw_id": raw.id, "item_id": second.item.id}
    await processor.handle_edit(ctx, payload)
    assert len(fake_ai.inputs) == 1  # no second extract call
    [occ] = [o for o in await occurrences_of(second.item.id) if o.status == "open"]
    assert occ.due_at == local(2026, 10, 7, 16, 0)
    [_, reply] = bot.sent
    assert reply["text"] == "🕒 Перенёс «Встреча с Олегом по проекту» на завтра 16:00"
    assert reply_to(reply) == raw.message_id
    assert buttons(reply) == [("↩️ Отменить", UndoCb(raw=raw.id, item=second.item.id).pack())]
    assert [o.due_at for o in await occurrences_of(first.item.id)] == [local(2026, 10, 7, 15, 0)]  # untouched
    assert str(second.item.id) in (await get(RawMessage, raw.id)).raw_json[ingest.UNDO_KEY]
    assert (await get(OnceKey, f"edit:{raw.id}:{second.item.id}")) is not None

    await processor.handle_edit(ctx, payload)  # redelivered
    assert len(bot.sent) == 2 and len(fake_ai.inputs) == 1


async def test_pick_of_a_destructive_edit_applies_it_without_the_ai(
    ctx, add_raw, fake_ai, bot, make_items, t0, get
) -> None:
    first, second = await make_items(
        {"kind": "task", "title": "Купить молоко"}, {"kind": "task", "title": "Купить молоко козье"}, now=t0
    )
    raw = await add_raw("удали молоко")
    fake_ai.push({"intent": "edit", "edit": {"target": "молоко", "action": "delete"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert bot.texts[0].startswith("✏️ Какую запись изменить?")
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": first.item.id})
    assert (await get(Item, first.item.id)).status == "deleted"
    assert (await get(Item, second.item.id)).status == "open"
    assert bot.texts[1] == "🗑 Удалил «Купить молоко»" and len(fake_ai.inputs) == 1


async def test_pick_of_an_item_deleted_meanwhile(ctx, add_raw, fake_ai, bot, make_items, t0, get) -> None:
    first, _second = await make_items(
        {"kind": "task", "title": "Купить молоко"}, {"kind": "task", "title": "Купить молоко козье"}, now=t0
    )
    raw = await add_raw("купил молоко")
    fake_ai.push({"intent": "edit", "edit": {"target": "молоко", "action": "done"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    async with ctx.sessionmaker() as session:
        await items.delete_item(session, first.item.id, t0)
        await session.commit()
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": first.item.id})
    assert bot.texts[-1] == processor.ITEM_GONE_TEXT
    assert (await get(Item, first.item.id)).status == "deleted" and len(fake_ai.inputs) == 1


async def test_pick_with_an_unreadable_stored_edit_asks_the_ai_again(
    ctx, add_raw, fake_ai, bot, make_items, t0, occurrences_of, local
) -> None:
    [created] = await make_items({"kind": "event", "title": "Встреча", "when": {"day": "+1", "time": "15:00"}}, now=t0)
    raw = await add_raw(
        "перенеси встречу на 16",
        status="done",
        raw_json={processor.CANDIDATES_KEY: [created.item.id], ingest.EDIT_KEY: {"action": "explode"}},
    )
    fake_ai.push({"intent": "edit", "edit": {"when": {"day": "+1", "time": "16:00"}}})
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})
    [inp] = fake_ai.inputs
    assert inp.reply_item is not None and inp.reply_item["id"] == created.item.id
    [occ] = [o for o in await occurrences_of(created.item.id) if o.status == "open"]
    assert occ.due_at == local(2026, 10, 7, 16, 0)
    assert "<b>Встреча</b>" in bot.texts[0]  # the old reply: the item's card


async def test_edit_by_target_that_changes_nothing_offers_no_undo(ctx, add_raw, fake_ai, bot, make_items, t0) -> None:
    [created] = await make_items({"kind": "note", "title": "Идея подкаста"}, now=t0)
    async with ctx.sessionmaker() as session:
        await items.apply_patch(session, created.item.id, EditPatch(action="done"), Prefs(), t0)
        await session.commit()
    raw = await add_raw("готово: идея подкаста")
    fake_ai.push({"intent": "edit", "edit": {"target": "Идея подкаста", "action": "done"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [msg] = bot.sent
    assert msg["text"] == "✅ «Идея подкаста» — уже сделано" and buttons(msg) == []


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


@pytest.mark.parametrize(
    "edit",
    [
        {},  # the model found nothing
        {"title": "Как обычно"},  # a title, no time
        {"action": "delete"},  # "отмена" read as "удали"
        {"when": {"text": "30 февраля", "day": "2026-02-30"}},  # no such date
    ],
)
async def test_edit_time_without_a_time_changes_nothing(ctx, fake_ai, bot, make_items, t0, get, edit) -> None:
    [created] = await make_items({"kind": "task", "title": "Отчёт", "when": {"day": "+1", "time": "15:00"}}, now=t0)
    fake_ai.push({"intent": "edit", "edit": edit})
    await processor.handle_edit_time(ctx, {"item_id": created.item.id, "text": "как обычно", "message_id": 557})

    item = await get(Item, created.item.id)
    assert (item.title, item.status, item.updated_at) == ("Отчёт", "open", created.item.updated_at)
    assert bot.texts == [processor.TIME_NOT_UNDERSTOOD_TEXT]
    assert reply_to(bot.sent[0]) == 557
    assert bot.reactions == [(557, "🤔")]


async def test_edit_time_applies_only_the_time(ctx, fake_ai, bot, make_items, t0, get, occurrences_of, local) -> None:
    [created] = await make_items({"kind": "task", "title": "Отчёт", "when": {"day": "+1", "time": "15:00"}}, now=t0)
    edit = {"title": "Пятница", "kind": "idea", "action": "done", "when": {"day": "FR", "time": "18:00"}, "lead": [30]}
    fake_ai.push({"intent": "edit", "edit": edit})
    await processor.handle_edit_time(ctx, {"item_id": created.item.id, "text": "в пятницу 18", "message_id": 558})

    item = await get(Item, created.item.id)
    # the model's title is dropped; the service names the stated 18:00, since the item's time is 17:30 now
    assert (item.title, item.kind, item.status, item.lead_minutes) == ("Отчёт (18:00)", "task", "open", [30])
    [occ] = [o for o in await occurrences_of(created.item.id) if o.status == "open"]
    assert occ.due_at == local(2026, 10, 9, 17, 30)  # "за 30 минут" before Friday 18:00
    assert bot.reactions == [(558, "👌")]


async def test_edit_time_job_is_applied_once(ctx, fake_ai, bot, make_items, t0, get) -> None:
    [created] = await make_items({"kind": "task", "title": "Отчёт", "when": {"day": "+1", "time": "15:00"}}, now=t0)
    fake_ai.push({"intent": "edit", "edit": {"when": {"text": "в пятницу 18", "day": "FR", "time": "18:00"}}})
    payload = {"item_id": created.item.id, "text": "в пятницу 18", "message_id": 556}
    await processor.handle_edit_time(ctx, payload)
    assert await get(OnceKey, f"edit-time-done:{created.item.id}:556") is not None

    await processor.handle_edit_time(ctx, payload)  # re-run after a restart: no second AI call or card
    assert len(fake_ai.inputs) == 1 and len(bot.sent) == 1


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


async def test_clarify_is_applied_once(ctx, add_raw, fake_ai, bot, items_of_raw, get) -> None:
    raw = await add_raw(
        "в 8 позвонить маме", status="done", raw_json={processor.ASK_KEY: {"options": ["утра", "вечера"]}}
    )
    fake_ai.push({"items": [{"kind": "task", "title": "Позвонить маме", "when": {"time": "20:00"}}]})
    await processor.handle_clarify(ctx, {"raw_id": raw.id, "option": 1})
    [first] = await items_of_raw(raw.id)
    assert await get(OnceKey, f"clarify-done:{raw.id}") is not None

    # The job runs again (SIGTERM after the commit, while the cards were being sent).
    await processor.handle_clarify(ctx, {"raw_id": raw.id, "option": 1})
    assert len(fake_ai.inputs) == 1 and len(bot.sent) == 1
    assert [i.id for i in await items_of_raw(raw.id)] == [first.id]


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


async def test_missing_primary_model_is_reported_once_a_day_while_the_fallback_works(
    ctx, add_raw, fake_ai, bot, items_of_raw, frozen
) -> None:
    def on_fallback(title: str) -> ExtractOutput:
        extraction = Extraction.model_validate({"items": [{"kind": "idea", "title": title}]})
        return ExtractOutput(extraction, "Qwen/Qwen3-32B-TEE", "v1", missing_model="google/gemma-gone")

    raws = [await add_raw("идея раз"), await add_raw("идея два")]
    fake_ai.push(on_fallback("Раз"), on_fallback("Два"))
    for raw in raws:
        await processor.handle_message(ctx, {"raw_id": raw.id})
        [item] = await items_of_raw(raw.id)
        assert item.ai_model == "Qwen/Qwen3-32B-TEE"
    [notice] = bot.texts
    assert "google/gemma-gone" in notice and "Qwen/Qwen3-32B-TEE" in notice and "LLM_MODEL" in notice
    assert [emoji for _, emoji in bot.reactions] == ["👌", "👌"]

    frozen.advance(days=1)
    raw = await add_raw("идея три")
    fake_ai.push(on_fallback("Три"))
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert len(bot.texts) == 2


async def test_missing_fallback_model_is_reported_once_a_day(ctx, add_raw, fake_ai, bot, items_of_raw) -> None:
    # The primary answers; the fallback answered 404 earlier (it would be no help in the next outage).
    def on_primary(title: str) -> ExtractOutput:
        extraction = Extraction.model_validate({"items": [{"kind": "idea", "title": title}]})
        return ExtractOutput(extraction, "google/gemma", "v1", missing_fallback="Qwen/qwen-gone")

    raws = [await add_raw("идея раз"), await add_raw("идея два")]
    fake_ai.push(on_primary("Раз"), on_primary("Два"))
    for raw in raws:
        await processor.handle_message(ctx, {"raw_id": raw.id})
        [item] = await items_of_raw(raw.id)
        assert item.ai_model == "google/gemma"
    [notice] = bot.texts
    assert "Qwen/qwen-gone" in notice and "LLM_FALLBACK_MODEL" in notice
    assert [emoji for _, emoji in bot.reactions] == ["👌", "👌"]


# --- unusable AI results (AIBadResponse): no retries ---------------------------------------------------------------


async def test_unusable_ai_result_keeps_the_text_as_a_note_without_retries(
    ctx, add_raw, fake_ai, bot, sessionmaker, jobs_all, items_of_raw, get
) -> None:
    # temperature 0 + fixed seed: 10 retries over 14 h would pay for the same unusable answer every time.
    raw = await add_raw("напомни завтра купить молоко\nи хлеб")
    async with sessionmaker() as session:
        await jobs.enqueue(session, "message", {"raw_id": raw.id})
        await session.commit()
    fake_ai.push(AIBadResponse("no valid extraction from the primary and fallback models"))

    await JobWorker(ctx, processor.HANDLERS).run_once()

    [job] = await jobs_all()
    assert (job.status, job.attempts) == ("done", 1)
    assert len(fake_ai.inputs) == 1
    [item] = await items_of_raw(raw.id)
    assert (item.kind, item.title, item.ai_model) == ("note", "напомни завтра купить молоко", None)
    assert item.source_text == "напомни завтра купить молоко\nи хлеб"
    assert (await get(RawMessage, raw.id)).status == "done"
    assert bot.texts == [processor.FAILED_MESSAGE_TEXT]
    assert bot.reactions == [(raw.message_id, "😢")]


async def test_unusable_ai_result_for_a_picture_or_voice_keeps_it_by_type(
    ctx, add_raw, fake_ai, bot, stt, items_of_raw
) -> None:
    photo = await add_raw(None, content_type="photo", file_id="ph")
    fake_ai.push(AIBadResponse("HTTP 413: Request Entity Too Large"))
    await processor.handle_message(ctx, {"raw_id": photo.id})
    [item] = await items_of_raw(photo.id)
    assert (item.kind, item.title) == ("other", "Фото")

    voice = await add_raw(None, content_type="voice", file_id="vo")
    stt.results = [AIBadResponse("malformed STT response (KeyError)")]
    await processor.handle_message(ctx, {"raw_id": voice.id})
    [item] = await items_of_raw(voice.id)
    assert (item.kind, item.title) == ("other", "Голосовое сообщение")
    assert len(fake_ai.inputs) == 1  # the voice message never reached extract
    assert bot.reactions == [(photo.message_id, "😢"), (voice.message_id, "😢")]


async def test_unusable_ai_result_for_a_clarification_keeps_the_items(ctx, add_raw, fake_ai, bot, items_of_raw) -> None:
    raw = await add_raw("в 8 позвонить маме", raw_json={processor.ASK_KEY: {"options": ["утра", "вечера"]}})
    fake_ai.push({"items": [{"kind": "task", "title": "Позвонить маме", "when": {"time": "20:00"}}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [first] = await items_of_raw(raw.id)

    fake_ai.push(AIBadResponse("no valid extraction"))
    await processor.handle_clarify(ctx, {"raw_id": raw.id, "option": 1})

    assert [i.id for i in await items_of_raw(raw.id)] == [first.id]
    assert bot.texts[-1] == processor.FAILED_MESSAGE_TEXT
    assert bot.reactions[-1] == (raw.message_id, "😢")


async def test_unusable_ai_result_for_a_reply_edit_fails_at_once(
    ctx, add_raw, fake_ai, bot, make_items, t0, sessionmaker, jobs_all, get
) -> None:
    [created] = await make_items({"kind": "task", "title": "Купить наушники"}, now=t0)
    raw = await add_raw("сделай что-нибудь", reply_item_id=created.item.id, message_id=701)
    async with sessionmaker() as session:
        await jobs.enqueue(session, "edit", {"raw_id": raw.id, "item_id": created.item.id})
        await session.commit()
    fake_ai.push(AIBadResponse("intent=edit requires 'edit'"))

    await JobWorker(ctx, processor.HANDLERS).run_once()

    [job] = await jobs_all()
    assert (job.status, job.attempts) == ("done", 1)
    assert (await get(Item, created.item.id)).title == "Купить наушники"
    stored = await get(RawMessage, raw.id)
    assert stored.status == "failed" and "AIBadResponse" in stored.error
    assert bot.texts == [processor.FAILED_EDIT_TEXT]
    assert bot.reactions == [(701, "😢")]


async def test_unusable_ai_result_for_edit_time_fails_at_once(ctx, fake_ai, bot, make_items, t0, get) -> None:
    [created] = await make_items({"kind": "task", "title": "Отчёт", "when": {"day": "+1", "time": "15:00"}}, now=t0)
    fake_ai.push(AIBadResponse("no valid extraction"))
    await processor.handle_edit_time(ctx, {"item_id": created.item.id, "text": "когда-нибудь", "message_id": 557})
    assert bot.texts == [processor.FAILED_EDIT_TEXT] and reply_to(bot.sent[0]) == 557
    assert bot.reactions == [(557, "😢")]


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


# --- edits by description: what the item itself is about (review fixes) ---------------------------------------------

MILK_AND_BREAD = {
    "items": [{"kind": "purchase", "title": "Купить молоко"}, {"kind": "purchase", "title": "Купить хлеб"}]
}


async def milk_and_bread(ctx, add_raw, fake_ai, items_of_raw, bot) -> tuple[Item, Item]:
    """Two purchases from one message: both keep "купить молоко и хлеб" as their source text."""
    raw = await add_raw("купить молоко и хлеб")
    fake_ai.push(MILK_AND_BREAD)
    await processor.handle_message(ctx, {"raw_id": raw.id})
    milk, bread = await items_of_raw(raw.id)
    assert milk.source_text == bread.source_text == "купить молоко и хлеб"
    bot.calls.clear()
    return milk, bread


async def test_bought_x_closes_the_one_item_of_a_shopping_message_at_once(
    ctx, add_raw, fake_ai, bot, items_of_raw, get
) -> None:
    milk, bread = await milk_and_bread(ctx, add_raw, fake_ai, items_of_raw, bot)
    raw = await add_raw("купил молоко")
    fake_ai.push({"intent": "edit", "edit": {"target": "молоко", "action": "done"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})

    [msg] = bot.sent
    assert msg["text"] == "✅ Закрыл «Купить молоко»"  # not "Какую запись изменить?": bread only shares the message
    assert [(await get(Item, i.id)).status for i in (milk, bread)] == ["done", "open"]


async def test_an_edit_never_lands_on_an_item_that_only_shares_the_message(
    ctx, add_raw, fake_ai, bot, items_of_raw, get
) -> None:
    milk, bread = await milk_and_bread(ctx, add_raw, fake_ai, items_of_raw, bot)
    async with ctx.sessionmaker() as session:  # milk closed with a button
        await items.apply_patch(session, milk.id, EditPatch(action="done"), Prefs(), milk.created_at)
        await session.commit()
    for text, action in (("закрой молоко", "done"), ("удали молоко", "delete")):
        raw = await add_raw(text)
        fake_ai.push({"intent": "edit", "edit": {"target": "молоко", "action": action}})
        await processor.handle_message(ctx, {"raw_id": raw.id})
    assert "хлеб" not in " ".join(bot.texts)
    assert (await get(Item, bread.id)).status == "open"
    assert (await get(Item, milk.id)).status == "deleted"  # the only item about milk


async def test_a_word_inside_another_word_is_no_exact_match(ctx, add_raw, fake_ai, bot, make_items, t0, get) -> None:
    tea, note = await make_items(
        {"kind": "purchase", "title": "Купить чай"}, {"kind": "note", "title": "Случайно разбил кружку"}, now=t0
    )
    async with ctx.sessionmaker() as session:
        await items.apply_patch(session, tea.item.id, EditPatch(action="done"), Prefs(), t0)
        await session.commit()
    raw = await add_raw("удали чай")
    fake_ai.push({"intent": "edit", "edit": {"target": "чай", "action": "delete"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert bot.texts == ["🗑 Удалил «Купить чай»"]  # "случайно" holds "чай", but it is not the word
    assert (await get(Item, note.item.id)).status == "open"


async def test_done_does_not_close_an_open_note_instead_of_a_done_task(
    ctx, add_raw, fake_ai, bot, make_items, t0, get
) -> None:
    tea, idea = await make_items(
        {"kind": "purchase", "title": "Купить чай"}, {"kind": "idea", "title": "Попробовать чай улун"}, now=t0
    )
    async with ctx.sessionmaker() as session:
        await items.apply_patch(session, tea.item.id, EditPatch(action="done"), Prefs(), t0)
        await session.commit()
    raw = await add_raw("купил чай")
    fake_ai.push({"intent": "edit", "edit": {"target": "чай", "action": "done"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert bot.texts[0].startswith("✏️ Какую запись изменить?")
    assert (await get(Item, idea.item.id)).status == "open"


async def test_new_items_said_with_an_edit_are_saved_too(ctx, add_raw, fake_ai, bot, make_items, t0, items_of_raw, get):
    [milk] = await make_items({"kind": "purchase", "title": "Купить молоко"}, now=t0)
    raw = await add_raw("купил молоко, хлеба не было — купить завтра")
    fake_ai.push(
        {
            "intent": "edit",
            "edit": {"target": "молоко", "action": "done"},
            "items": [{"kind": "purchase", "title": "Купить хлеб", "when": {"text": "завтра", "day": "+1"}}],
        }
    )
    await processor.handle_message(ctx, {"raw_id": raw.id})

    assert bot.texts[0] == "✅ Закрыл «Купить молоко»"
    assert (await get(Item, milk.item.id)).status == "done"
    [bread] = await items_of_raw(raw.id)
    assert (bread.kind, bread.title, bread.status) == ("purchase", "Купить хлеб", "open")
    assert any("Купить хлеб" in text for text in bot.texts[1:])  # its card
    assert bot.reactions[-1] == (raw.message_id, "👌")
