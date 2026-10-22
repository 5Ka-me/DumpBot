"""services.processor and voice notes that are not speech (docs/features/iteration-4.md §1).

A rejected transcript is tried once more by another path; garbage twice -> no items, the transcript and the tries are
kept with the raw message, "🎤 Не разобрал голосовое — повтори или напиши текстом" with [🔁 Ещё раз] and 🤔;
[🔁 Ещё раз] (``processor.retry_unheard``) transcribes the same file again. A paused local Whisper is skipped.
Only a voice note that is all the message has gets "Не разобрал": an audio file, a caption or another part of an
album is something to go on, and the rejected transcript is only kept aside.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from dumpbot.ai import garbage, stt
from dumpbot.bot import sender, views
from dumpbot.bot.cbdata import AgainCb
from dumpbot.db.models import BotMessage, Item, RawMessage
from dumpbot.services import processor, query
from dumpbot.services.ingest import UNHEARD_KEY

OGG = b"OGG-BYTES"
NOISE = "ааааааааааааааааааааа"
CREDITS = "Субтитры сделал DimaTorzok"
SAVE_TASK = {"items": [{"kind": "task", "title": "Купить хлеб"}]}


def buttons(call: dict) -> list[tuple[str, str]]:
    markup = call.get("reply_markup")
    if markup is None:
        return []
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


@pytest.fixture
def voice(add_raw, bot):
    async def _add(**kw: Any) -> RawMessage:
        bot.files["voice-1"] = OGG
        values: dict[str, Any] = {
            "content_type": "voice",
            "file_id": "voice-1",
            "mime_type": "audio/ogg",
            "duration": 4,
        }
        values.update(kw)
        text = values.pop("text", None)
        return await add_raw(text, **values)

    return _add


@pytest.fixture
def wav(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """ffmpeg stand-in for the second try of the only backend."""
    converted: list[bytes] = []

    async def _convert(audio: bytes, mime_type: str | None = None) -> bytes:
        converted.append(audio)
        return b"WAV-BYTES"

    monkeypatch.setattr(stt, "convert_to_wav", _convert)
    return converted


async def test_garbage_twice_saves_nothing_and_asks_to_repeat(
    ctx, voice, bot, stt, local_stt, with_local_stt, fake_ai, items_of_raw, bot_messages, get
) -> None:
    with_local_stt()
    raw = await voice()
    stt.results = [NOISE]
    local_stt.results = [CREDITS]

    await processor.handle_message(ctx, {"raw_id": raw.id})

    assert fake_ai.inputs == []  # nothing to extract: no AI call
    assert await items_of_raw(raw.id, include_deleted=True) == []
    stored = await get(RawMessage, raw.id)
    assert (stored.status, stored.transcript) == ("done", CREDITS)  # kept for analysis
    assert stored.raw_json[UNHEARD_KEY] == {
        "tries": [
            {"stt": "chutes", "reason": garbage.REPEATED_LETTER, "text": NOISE},
            {"stt": "local", "reason": garbage.HALLUCINATION, "text": CREDITS},
        ],
        "job": {"kind": "message", "payload": {"raw_id": raw.id}},
    }
    [reply] = bot.sent
    assert reply["text"] == "🎤 Не разобрал голосовое — повтори или напиши текстом"
    assert reply["reply_parameters"].message_id == raw.message_id
    assert buttons(reply) == [("🔁 Ещё раз", AgainCb(raw=raw.id).pack())]
    [message] = await bot_messages()
    assert (message.purpose, message.raw_message_id, message.item_id) == (sender.PURPOSE_UNHEARD, raw.id, None)
    assert bot.reactions == [(raw.message_id, "🤔")]


async def test_a_usable_second_try_is_processed_as_usual(
    ctx, voice, bot, stt, local_stt, with_local_stt, fake_ai, items_of_raw, get
) -> None:
    with_local_stt()
    raw = await voice()
    stt.results = ["Продолжение следует..."]
    local_stt.results = ["Купить хлеб"]
    fake_ai.push(SAVE_TASK)

    await processor.handle_message(ctx, {"raw_id": raw.id})

    assert fake_ai.inputs[0].text == "Купить хлеб"
    [item] = await items_of_raw(raw.id)
    assert item.title == "Купить хлеб"
    stored = await get(RawMessage, raw.id)
    assert stored.transcript == "Купить хлеб" and UNHEARD_KEY not in stored.raw_json
    assert views.UNHEARD_TEXT not in bot.texts


async def test_the_only_backend_tries_again_as_wav(ctx, voice, bot, stt, wav, fake_ai, items_of_raw) -> None:
    raw = await voice()
    stt.results = [NOISE, "Купить хлеб"]
    fake_ai.push(SAVE_TASK)

    await processor.handle_message(ctx, {"raw_id": raw.id})

    assert wav == [OGG]
    assert stt.calls[1] == {"audio": b"WAV-BYTES", "mime_type": "audio/wav", "language": "ru"}
    assert [i.title for i in await items_of_raw(raw.id)] == ["Купить хлеб"]


async def test_too_much_text_for_a_short_note_is_garbage(ctx, voice, bot, stt, wav, fake_ai, items_of_raw) -> None:
    raw = await voice(duration=2)
    stt.results = ["Купить хлеб и молоко, " * 6] * 2  # 132 characters in (at most) 3 seconds, twice
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert await items_of_raw(raw.id) == [] and fake_ai.inputs == []
    assert bot.texts == [views.UNHEARD_TEXT]


async def test_a_rerun_of_the_job_does_not_transcribe_again(ctx, voice, bot, stt, fake_ai, items_of_raw) -> None:
    """The job died after the rejected transcript was committed: the re-run reads it from the raw message."""
    raw = await voice()
    async with ctx.sessionmaker() as session:
        stored = await session.get(RawMessage, raw.id)
        stored.transcript = NOISE
        stored.raw_json = {UNHEARD_KEY: {"tries": [{"stt": "chutes", "reason": "repeated letter", "text": NOISE}]}}
        await session.commit()

    await processor.handle_message(ctx, {"raw_id": raw.id})

    assert stt.calls == [] and bot.calls_of("download") == []
    assert await items_of_raw(raw.id) == []
    assert bot.texts == [views.UNHEARD_TEXT]


async def test_again_transcribes_the_same_file(ctx, voice, bot, stt, wav, fake_ai, items_of_raw, get, jobs_all) -> None:
    raw = await voice()
    stt.results = [NOISE, NOISE]
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert bot.texts == [views.UNHEARD_TEXT]

    async with ctx.sessionmaker() as session:
        assert await processor.retry_unheard(session, raw.id) == processor.RETRY_QUEUED
        await session.commit()
    stored = await get(RawMessage, raw.id)
    assert (stored.status, stored.transcript, UNHEARD_KEY in stored.raw_json) == ("new", None, False)
    [job] = await jobs_all()
    assert (job.kind, job.payload, job.status) == ("message", {"raw_id": raw.id}, "queued")

    async with ctx.sessionmaker() as session:  # a second press
        assert await processor.retry_unheard(session, raw.id) == processor.RETRY_ALREADY
        await session.commit()
    assert len(await jobs_all()) == 1

    stt.results = ["Купить хлеб"]
    fake_ai.push(SAVE_TASK)
    await processor.handle_message(ctx, job.payload)
    assert [c["audio"] for c in stt.calls] == [OGG, b"WAV-BYTES", OGG]  # the same file again
    assert [i.title for i in await items_of_raw(raw.id)] == ["Купить хлеб"]
    assert bot.reactions[-1] == (raw.message_id, "👌")


async def test_again_after_garbage_again_works_again(ctx, voice, bot, stt, wav, fake_ai, jobs_all) -> None:
    raw = await voice()
    stt.results = [NOISE] * 4
    for _ in range(2):
        await processor.handle_message(ctx, {"raw_id": raw.id})
        async with ctx.sessionmaker() as session:
            assert await processor.retry_unheard(session, raw.id) == processor.RETRY_QUEUED
            await session.commit()
    assert bot.texts == [views.UNHEARD_TEXT] * 2
    assert len(await jobs_all()) == 2


async def test_again_without_the_message(ctx) -> None:
    async with ctx.sessionmaker() as session:
        assert await processor.retry_unheard(session, 404) == processor.RETRY_MISSING


async def test_a_voice_reply_that_is_not_speech_changes_nothing(
    ctx, voice, bot, stt, wav, fake_ai, make_items, t0, get, jobs_all
) -> None:
    [created] = await make_items({"kind": "task", "title": "Отчёт", "when": {"day": "+1", "time": "15:00"}}, now=t0)
    item_id = created.item.id
    raw = await voice(reply_item_id=item_id)
    stt.results = [NOISE, NOISE]

    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": item_id})

    assert fake_ai.inputs == []
    item = await get(Item, item_id)
    assert (item.title, item.status) == ("Отчёт", "open")
    assert bot.texts == [views.UNHEARD_TEXT]
    stored = await get(RawMessage, raw.id)
    assert stored.raw_json[UNHEARD_KEY]["job"] == {"kind": "edit", "payload": {"raw_id": raw.id, "item_id": item_id}}

    async with ctx.sessionmaker() as session:
        assert await processor.retry_unheard(session, raw.id) == processor.RETRY_QUEUED
        await session.commit()
    [job] = await jobs_all()
    assert (job.kind, job.payload) == ("edit", {"raw_id": raw.id, "item_id": item_id})

    stt.results = ["Перенеси на пятницу"]
    fake_ai.push({"intent": "edit", "edit": {"title": "Отчёт за квартал"}})
    await processor.handle_edit(ctx, job.payload)
    assert (await get(Item, item_id)).title == "Отчёт за квартал"


MUSIC = "Динамичная музыка"


async def test_an_audio_file_that_is_not_speech_goes_on_its_caption(
    ctx, add_raw, bot, stt, wav, fake_ai, items_of_raw, get
) -> None:
    """A forwarded instrumental mp3 with a caption: "repeat" means nothing for a file — it is saved by its caption."""
    bot.files["mp3"] = b"MP3"
    raw = await add_raw(
        "Трек для монтажа ролика", content_type="audio", file_id="mp3", file_name="track.mp3", duration=150
    )
    stt.results = [MUSIC, MUSIC]
    fake_ai.push({"items": [{"kind": "link", "title": "Трек для монтажа ролика"}]})

    await processor.handle_message(ctx, {"raw_id": raw.id})

    assert fake_ai.inputs[0].text == "Трек для монтажа ролика"  # the caption, not the made-up words
    assert [i.title for i in await items_of_raw(raw.id)] == ["Трек для монтажа ролика"]
    assert views.UNHEARD_TEXT not in bot.texts and all(MUSIC not in t for t in bot.texts)
    stored = await get(RawMessage, raw.id)
    assert stored.status == "done" and stored.transcript == MUSIC  # kept for analysis
    assert [t["reason"] for t in stored.raw_json[UNHEARD_KEY]["tries"]] == [garbage.HALLUCINATION] * 2
    assert "job" not in stored.raw_json[UNHEARD_KEY]  # nothing to try again
    assert bot.reactions[-1] == (raw.message_id, "👌")
    async with ctx.sessionmaker() as session:  # a redrawn card shows no "🎤 «Динамичная музыка»" either
        assert await query.heard(session, raw.id) is None


async def test_an_audio_file_that_is_not_speech_without_a_caption_is_kept_as_audio(
    ctx, add_raw, bot, stt, wav, fake_ai, items_of_raw
) -> None:
    bot.files["mp3"] = b"MP3"
    raw = await add_raw(None, content_type="audio", file_id="mp3", file_name="track.mp3", duration=150)
    stt.results = [MUSIC, MUSIC]

    await processor.handle_message(ctx, {"raw_id": raw.id})

    assert fake_ai.inputs == []
    assert [(i.kind, i.title) for i in await items_of_raw(raw.id)] == [("other", "Аудио: track.mp3")]
    assert views.UNHEARD_TEXT not in bot.texts
    assert bot.reactions == [(raw.message_id, "👌")]


async def test_a_voice_note_with_a_caption_goes_on_the_caption(
    ctx, voice, bot, stt, wav, fake_ai, items_of_raw
) -> None:
    raw = await voice(text="купить батарейки")
    stt.results = [NOISE, NOISE]
    fake_ai.push({"items": [{"kind": "purchase", "title": "Купить батарейки"}]})

    await processor.handle_message(ctx, {"raw_id": raw.id})

    assert fake_ai.inputs[0].text == "купить батарейки"
    assert [i.title for i in await items_of_raw(raw.id)] == ["Купить батарейки"]
    assert views.UNHEARD_TEXT not in bot.texts and all(NOISE not in t for t in bot.texts)  # no "🎤 «ааа…»"


async def test_an_album_part_that_is_not_speech_leaves_the_others(
    ctx, add_raw, bot, stt, wav, fake_ai, items_of_raw, db
) -> None:
    for n, file_id in enumerate(("a1", "a2")):
        bot.files[file_id] = f"MP3-{n}".encode()
        await add_raw(None, content_type="audio", file_id=file_id, duration=30, media_group_id="G", message_id=500 + n)
    stt.results = ["Купить хлеб", MUSIC, MUSIC]
    fake_ai.push(SAVE_TASK)

    await processor.handle_album(ctx, {"media_group_id": "G", "chat_id": ctx.settings.owner_id})

    raws = await db(select(RawMessage).order_by(RawMessage.message_id))
    assert [r.status for r in raws] == ["done", "done"]
    assert fake_ai.inputs[0].text == "Купить хлеб"  # the usable part
    assert [i.title for i in await items_of_raw(raws[0].id)] == ["Купить хлеб"]
    assert UNHEARD_KEY not in raws[0].raw_json and UNHEARD_KEY in raws[1].raw_json
    assert views.UNHEARD_TEXT not in bot.texts
    assert bot.reactions == [(500, "👌"), (501, "👌")]


async def test_again_forgets_only_the_rejected_transcripts(ctx, add_raw, db, jobs_all) -> None:
    """An album answered "Не разобрал" before audio files went on their usable parts: [🔁 Ещё раз] transcribes the
    rejected part again, the usable one is not paid for twice."""
    album = {"content_type": "audio", "duration": 30, "media_group_id": "G", "status": "done"}
    job = {"kind": "album", "payload": {"media_group_id": "G", "chat_id": ctx.settings.owner_id}}
    first = await add_raw(None, message_id=500, transcript="Купить хлеб", raw_json={UNHEARD_KEY: {"job": job}}, **album)
    tries = [{"stt": "chutes", "reason": garbage.HALLUCINATION, "text": MUSIC}]
    await add_raw(None, message_id=501, transcript=MUSIC, raw_json={UNHEARD_KEY: {"tries": tries}}, **album)

    async with ctx.sessionmaker() as session:
        assert await processor.retry_unheard(session, first.id) == processor.RETRY_QUEUED
        await session.commit()

    raws = await db(select(RawMessage).order_by(RawMessage.message_id))
    assert [(r.status, r.transcript) for r in raws] == [("new", "Купить хлеб"), ("new", None)]
    assert all(UNHEARD_KEY not in r.raw_json for r in raws)
    [queued] = await jobs_all()
    assert (queued.kind, queued.payload) == ("album", job["payload"])


async def test_not_heard_after_a_voice_reply_to_a_reminder_is_about_its_item(
    ctx, voice, bot, stt, wav, fake_ai, make_items, t0, db, jobs_all
) -> None:
    """ "Не разобрал" answered by text (as it asks) or by voice is the edit of the item again, not a new message: the
    message is recorded with the item and the instance the voice note replied to."""
    [created] = await make_items({"kind": "task", "title": "Отчёт", "when": {"day": "+1", "time": "15:00"}}, now=t0)
    item_id, occ_id = created.item.id, created.occurrence.id
    raw = await voice(reply_item_id=item_id)
    stt.results = [NOISE, NOISE]

    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": item_id, "occurrence_id": occ_id})

    assert bot.texts == [views.UNHEARD_TEXT]
    [message] = await db(select(BotMessage))
    assert (message.purpose, message.item_id, message.occurrence_id) == (sender.PURPOSE_UNHEARD, item_id, occ_id)
    async with ctx.sessionmaker() as session:
        assert await processor.retry_unheard(session, raw.id) == processor.RETRY_QUEUED
        await session.commit()
    [again] = await jobs_all()
    assert again.payload == {"raw_id": raw.id, "item_id": item_id, "occurrence_id": occ_id}


async def test_a_paused_local_whisper_is_skipped(
    ctx, voice, bot, stt, local_stt, with_local_stt, set_prefs, fake_ai
) -> None:
    with_local_stt()
    await set_prefs(stt_mode="local")
    local_stt.paused_for = 300.0  # could not be connected to a moment ago
    raw = await voice()
    fake_ai.push(SAVE_TASK)

    await processor.handle_message(ctx, {"raw_id": raw.id})

    assert local_stt.calls == [] and len(stt.calls) == 1
    assert fake_ai.inputs[0].text == "голосовой транскрипт"
    assert ctx.stt.local_paused
