"""services.processor and speech-to-text: chutes / local Whisper routing by ``Prefs.stt_mode``, the budget checked only
before paid calls, audio files and video notes, the duration limit (docs/features/iteration-2.md, B)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from dumpbot.ai.client import AIModelNotFound, AIUnavailable
from dumpbot.db.models import AiUsage, RawMessage
from dumpbot.services import processor
from dumpbot.workers import jobs
from dumpbot.workers.jobs import RetryLater

# 2026-11-01 08:00 Europe/Warsaw (CET): the first morning of the next month after the default quiet hours.
WARSAW_NEXT_MONTH = datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
OGG = b"OGG-BYTES"
SAVE_NOTE = {"items": [{"kind": "note", "title": "Голосовая заметка"}]}


async def spend(sessionmaker, usd: str, when: datetime) -> None:
    async with sessionmaker() as session:
        session.add(AiUsage(created_at=when, purpose="extract", model="m", cost_usd=Decimal(usd)))
        await session.commit()


@pytest.fixture
async def voice(add_raw, bot):
    """A 5-second voice note whose file the fake bot serves."""
    bot.files["voice-1"] = OGG
    return await add_raw(None, content_type="voice", file_id="voice-1", mime_type="audio/ogg", duration=5)


# --- routing by mode ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "chutes_calls", "local_calls", "text"),
    [
        ("auto", 1, 0, "голосовой транскрипт"),
        ("local", 0, 1, "локальный транскрипт"),
        ("chutes", 1, 0, "голосовой транскрипт"),
    ],
)
async def test_mode_picks_the_first_backend(
    ctx, voice, fake_ai, stt, local_stt, with_local_stt, set_prefs, get, mode, chutes_calls, local_calls, text
) -> None:
    with_local_stt()
    await set_prefs(stt_mode=mode, stt_language="ru")
    fake_ai.push(SAVE_NOTE)

    await processor.handle_message(ctx, {"raw_id": voice.id})

    assert (len(stt.calls), len(local_stt.calls)) == (chutes_calls, local_calls)
    call = (stt.calls or local_stt.calls)[0]
    assert call == {"audio": OGG, "mime_type": "audio/ogg", "language": "ru"}
    [inp] = fake_ai.inputs
    assert (inp.text, inp.source) == (text, "voice")
    assert (await get(RawMessage, voice.id)).transcript == text


async def test_auto_mode_falls_back_to_the_local_whisper_when_chutes_is_down(
    ctx, voice, fake_ai, bot, stt, local_stt, with_local_stt, items_of_raw
) -> None:
    with_local_stt()
    stt.results = [AIUnavailable("HTTP 503: No instances available")]
    fake_ai.push(SAVE_NOTE)

    await processor.handle_message(ctx, {"raw_id": voice.id})

    assert len(stt.calls) == 1 and len(local_stt.calls) == 1
    assert fake_ai.inputs[0].text == "локальный транскрипт"
    [item] = await items_of_raw(voice.id)
    assert item.source_text == "локальный транскрипт"
    assert bot.reactions == [(voice.message_id, "👌")]


async def test_auto_mode_falls_back_when_the_stt_chute_is_gone(
    ctx, voice, fake_ai, stt, local_stt, with_local_stt
) -> None:
    # The AudioDojo chute belongs to one community member and may disappear (404).
    with_local_stt()
    stt.results = [AIModelNotFound("HTTP 404: No matching cord found!")]
    fake_ai.push(SAVE_NOTE)
    await processor.handle_message(ctx, {"raw_id": voice.id})
    assert fake_ai.inputs[0].text == "локальный транскрипт"


async def test_local_mode_falls_back_to_chutes(ctx, voice, fake_ai, stt, local_stt, with_local_stt, set_prefs) -> None:
    with_local_stt()
    await set_prefs(stt_mode="local")
    local_stt.results = [AIUnavailable("local STT unreachable (ClientConnectorError)")]
    fake_ai.push(SAVE_NOTE)

    await processor.handle_message(ctx, {"raw_id": voice.id})

    assert len(local_stt.calls) == 1 and len(stt.calls) == 1
    assert fake_ai.inputs[0].text == "голосовой транскрипт"


async def test_without_local_whisper_every_mode_is_chutes(ctx, voice, fake_ai, stt, local_stt, set_prefs) -> None:
    await set_prefs(stt_mode="local")  # LOCAL_STT_URL is empty: no local backend
    fake_ai.push(SAVE_NOTE)
    await processor.handle_message(ctx, {"raw_id": voice.id})
    assert len(stt.calls) == 1 and local_stt.calls == []


async def test_both_down_defers_the_message(ctx, voice, fake_ai, bot, stt, local_stt, with_local_stt, get) -> None:
    with_local_stt()
    stt.results = [AIUnavailable("HTTP 503")]
    local_stt.results = [AIUnavailable("timeout")]

    with pytest.raises(AIUnavailable):
        await processor.handle_message(ctx, {"raw_id": voice.id})

    stored = await get(RawMessage, voice.id)
    assert (stored.status, stored.transcript) == ("deferred", None)
    assert fake_ai.inputs == []
    assert bot.reactions == [(voice.message_id, "✍")]


# --- budget: checked only before paid calls -------------------------------------------------------------------------


async def test_exhausted_budget_still_transcribes_locally_then_defers(
    ctx, voice, fake_ai, bot, stt, local_stt, with_local_stt, set_prefs, sessionmaker, frozen, get
) -> None:
    with_local_stt()
    await set_prefs(stt_mode="local")
    await spend(sessionmaker, "5.00", frozen.now)

    with pytest.raises(RetryLater) as exc:
        await processor.handle_message(ctx, {"raw_id": voice.id})

    assert (exc.value.at, exc.value.count_attempt, exc.value.reason) == (WARSAW_NEXT_MONTH, False, jobs.BUDGET_REASON)
    assert len(local_stt.calls) == 1 and stt.calls == []  # the free STT ran, the paid one did not
    assert fake_ai.inputs == []  # extract is paid
    stored = await get(RawMessage, voice.id)
    assert (stored.status, stored.transcript) == ("deferred", "локальный транскрипт")
    assert bot.reactions == [(voice.message_id, "✍")]
    assert len([t for t in bot.texts if "исчерпан" in t]) == 1

    # Next month the stored transcript goes straight to extract: no STT call at all.
    frozen.set(WARSAW_NEXT_MONTH)
    fake_ai.push(SAVE_NOTE)
    await processor.handle_message(ctx, {"raw_id": voice.id})
    assert len(local_stt.calls) == 1 and stt.calls == []
    assert fake_ai.inputs[0].text == "локальный транскрипт"


async def test_exhausted_budget_blocks_the_paid_stt(ctx, voice, fake_ai, bot, stt, sessionmaker, frozen, get) -> None:
    await spend(sessionmaker, "5.00", frozen.now)
    with pytest.raises(RetryLater):
        await processor.handle_message(ctx, {"raw_id": voice.id})
    assert stt.calls == [] and fake_ai.inputs == []
    assert (await get(RawMessage, voice.id)).status == "deferred"
    assert bot.reactions == [(voice.message_id, "✍")]


async def test_exhausted_budget_in_auto_mode_stops_before_chutes(
    ctx, voice, fake_ai, stt, local_stt, with_local_stt, sessionmaker, frozen
) -> None:
    # "auto" starts with the paid chutes: the budget check before it defers the message (extract is paid anyway).
    with_local_stt()
    await spend(sessionmaker, "5.00", frozen.now)
    with pytest.raises(RetryLater):
        await processor.handle_message(ctx, {"raw_id": voice.id})
    assert stt.calls == [] and local_stt.calls == [] and fake_ai.inputs == []


async def test_silent_voice_note_needs_no_budget(
    ctx, voice, fake_ai, bot, local_stt, with_local_stt, set_prefs, sessionmaker, frozen, items_of_raw
) -> None:
    # Nothing was said: the local transcript is empty, so no paid call is needed and nothing is deferred.
    with_local_stt()
    await set_prefs(stt_mode="local")
    await spend(sessionmaker, "5.00", frozen.now)
    local_stt.results = [""]

    await processor.handle_message(ctx, {"raw_id": voice.id})

    assert fake_ai.inputs == []
    [item] = await items_of_raw(voice.id)
    assert (item.kind, item.title, item.ai_model) == ("other", "Голосовое сообщение", None)
    assert bot.reactions == [(voice.message_id, "👌")]


async def test_80_percent_notice_comes_once_with_paid_stt_and_extract(
    ctx, voice, fake_ai, bot, sessionmaker, frozen
) -> None:
    await spend(sessionmaker, "4.10", frozen.now)
    fake_ai.push(SAVE_NOTE)
    await processor.handle_message(ctx, {"raw_id": voice.id})
    assert len([t for t in bot.texts if "бюджета" in t]) == 1


async def test_voice_reply_edit_checks_the_budget_after_the_free_stt(
    ctx, add_raw, bot, fake_ai, local_stt, with_local_stt, set_prefs, make_items, t0, sessionmaker, frozen, get
) -> None:
    with_local_stt()
    await set_prefs(stt_mode="local")
    [created] = await make_items({"kind": "task", "title": "Отчёт", "when": {"day": "+1", "time": "15:00"}}, now=t0)
    bot.files["v-edit"] = OGG
    raw = await add_raw(None, content_type="voice", file_id="v-edit", duration=3, reply_item_id=created.item.id)
    await spend(sessionmaker, "5.00", frozen.now)

    with pytest.raises(RetryLater):
        await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})

    stored = await get(RawMessage, raw.id)
    assert (stored.status, stored.transcript) == ("deferred", "локальный транскрипт")
    assert fake_ai.inputs == []


# --- what gets transcribed ------------------------------------------------------------------------------------------


async def test_forwarded_audio_file_is_transcribed_like_a_voice_note(
    ctx, add_raw, bot, fake_ai, stt, local_stt, with_local_stt, set_prefs, items_of_raw
) -> None:
    with_local_stt()
    await set_prefs(stt_mode="local")
    origin = {"type": "user", "sender_name": "Петя", "date": "2026-10-06T08:00:00+00:00"}
    bot.files["a1"] = b"M4A-BYTES"
    raw = await add_raw(
        None,
        content_type="audio",
        file_id="a1",
        file_name="запись.m4a",
        mime_type="audio/mp4",
        duration=95,
        forward_origin=origin,
    )
    fake_ai.push({"items": [{"kind": "task", "title": "Перезвонить Пете"}]})

    await processor.handle_message(ctx, {"raw_id": raw.id})

    assert local_stt.calls == [{"audio": b"M4A-BYTES", "mime_type": "audio/mp4", "language": "ru"}]
    [inp] = fake_ai.inputs
    assert (inp.text, inp.source, inp.forward_from) == ("локальный транскрипт", "forward", "Петя")
    [item] = await items_of_raw(raw.id)
    assert item.source_text == "локальный транскрипт"


async def test_video_note_is_never_transcribed(
    ctx, add_raw, bot, fake_ai, stt, local_stt, with_local_stt, items_of_raw
) -> None:
    with_local_stt()
    raw = await add_raw(None, content_type="video_note", file_id="vn", duration=20)
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert stt.calls == [] and local_stt.calls == [] and bot.calls_of("download") == []
    [item] = await items_of_raw(raw.id)
    assert (item.kind, item.title) == ("other", "Кружочек")


@pytest.mark.parametrize("mode", ["auto", "local", "chutes"])
async def test_duration_limit_applies_to_both_backends(
    ctx, add_raw, bot, fake_ai, stt, local_stt, with_local_stt, set_prefs, items_of_raw, mode
) -> None:
    with_local_stt()
    await set_prefs(stt_mode=mode)
    limit = ctx.settings.stt_max_duration_s
    assert limit == 300  # 5 minutes

    too_long = await add_raw(None, content_type="audio", file_id="a", file_name="lecture.mp3", duration=limit + 1)
    await processor.handle_message(ctx, {"raw_id": too_long.id})
    assert stt.calls == [] and local_stt.calls == [] and bot.calls_of("download") == []
    [item] = await items_of_raw(too_long.id)
    assert (item.kind, item.title) == ("other", "Аудио: lecture.mp3")

    at_limit = await add_raw(None, content_type="voice", file_id="v", duration=limit)
    fake_ai.push(SAVE_NOTE)
    await processor.handle_message(ctx, {"raw_id": at_limit.id})
    assert len(stt.calls) + len(local_stt.calls) == 1
