"""End-to-end: voice notes with the local Whisper service configured (``LOCAL_STT_URL`` -> the fake on 127.0.0.1).

chutes STT down -> the local Whisper transcribes in the same job (no ✍); "💻 Локальный" chosen in /settings ->
chutes STT is not called at all; a transcript with a fuzzy period ("на этой неделе", §A) gets the period card.
Harness: conftest.py (``e2e``); reference time Tue 2026-10-06 10:12 Europe/Warsaw.
"""

from __future__ import annotations

import base64
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from dumpbot.db.models import AiUsage

OGG = b"OggS\x00\x02" + bytes(range(64)) * 4
HEADPHONES = {
    "intent": "save",
    "items": [{"kind": "purchase", "title": "Купить наушники", "when": {"text": "в обед", "time": "lunch"}}],
}
VOICE = {"file_id": "voice-1", "file_unique_id": "uv1", "duration": 3, "mime_type": "audio/ogg", "file_size": 260}
THIS_WEEK = {
    "intent": "save",
    "items": [
        {
            "kind": "task",
            "title": "Посмотреть Raspberry Pi",
            "when": {"text": "На этой неделе", "period": "this_week"},
        }
    ],
}


@pytest.fixture
def local_whisper() -> bool:
    return True


def say(e2e: Any, transcript: str, extraction: dict[str, Any] = HEADPHONES) -> int:
    """A voice note whose audio the fake local Whisper transcribes as ``transcript``; returns its message id."""
    e2e.tg.add_file("voice-1", OGG, "voice/file_1.oga")
    e2e.ai.local_transcripts[OGG] = transcript
    e2e.ai.script(transcript, extraction)
    return e2e.tg.push_message(voice=VOICE)


async def test_voice_note_falls_back_to_the_local_whisper_when_chutes_stt_is_down(e2e: Any) -> None:
    e2e.ai.stt_outage = 503  # the STT chute is down, the LLM works

    message_id = say(e2e, "Купить наушники в обед")
    card = await e2e.card_for(message_id)
    await e2e.reaction(message_id, "👌")

    assert e2e.tg.reactions(message_id) == ["👀", "👌"]  # no ✍: the fallback ran in the same job
    assert len(e2e.ai.stt_requests) == 1  # chutes was tried first ("auto")
    [local] = e2e.ai.local_requests
    assert base64.b64decode(local["audio_b64"]) == OGG and local["language"] == "ru"
    [req] = e2e.ai.extraction_requests()
    assert (req.msg, req.line("SRC ")) == ("Купить наушники в обед", "SRC voice")
    assert card.text.startswith("🛒 <b>Купить наушники</b>")
    assert "🔔 сегодня 13:00" in card.text.splitlines()
    assert (await e2e.raw(message_id)).transcript == "Купить наушники в обед"

    usage = await e2e.all(AiUsage)
    stt_rows = [(u.model.startswith("local:"), u.ok, u.cost_usd) for u in usage if u.purpose == "stt"]
    assert stt_rows == [(False, False, Decimal("0")), (True, True, Decimal("0"))]
    [local_row] = [u for u in usage if u.model.startswith("local:")]
    assert local_row.model == "local:small" and local_row.audio_seconds == Decimal("3.00")
    assert [u.purpose for u in usage if u.cost_usd > 0] == ["extract"]


async def test_local_mode_chosen_in_settings_skips_chutes_stt(e2e: Any) -> None:
    # /settings -> 🎙 Голос -> 💻 Локальный
    e2e.tg.push_message("/settings", entities=[{"type": "bot_command", "offset": 0, "length": 9}])
    menu = await e2e.wait(lambda: next(iter(e2e.tg.sent), None), "settings menu")
    e2e.tg.push_callback(menu, menu.button("s:menu:stt"))
    [voice_menu] = await e2e.wait(lambda: e2e.tg.edits_of(menu.message_id), "voice menu")
    assert voice_menu.text.startswith("🎙 <b>Голос</b>")
    assert "(не настроен)" not in voice_menu.text + str(voice_menu.buttons)
    e2e.tg.push_callback(voice_menu, voice_menu.button("s:stt_mode:local"))
    changed = await e2e.wait(lambda: e2e.tg.edits_of(menu.message_id)[1:], "the mode saved")
    assert "Распознавание: локальный" in changed[0].text

    message_id = say(e2e, "Купить наушники в обед")
    await e2e.card_for(message_id)
    await e2e.reaction(message_id, "👌")

    assert e2e.ai.stt_requests == [] and len(e2e.ai.local_requests) == 1
    usage = await e2e.all(AiUsage)
    assert sorted((u.purpose, u.model.startswith("local:")) for u in usage) == [("extract", False), ("stt", True)]


async def test_local_whisper_transcript_with_a_fuzzy_period_gets_the_period_card(e2e: Any) -> None:
    e2e.ai.stt_outage = 503  # chutes STT down -> the local Whisper; its transcript says "на этой неделе"

    message_id = say(e2e, "На этой неделе посмотреть Raspberry Pi", THIS_WEEK)
    card = await e2e.card_for(message_id)
    await e2e.reaction(message_id, "👌")

    assert len(e2e.ai.local_requests) == 1
    [req] = e2e.ai.extraction_requests()
    assert (req.msg, req.line("SRC ")) == ("На этой неделе посмотреть Raspberry Pi", "SRC voice")
    assert card.text.split("\n")[:2] == [
        "📌 <b>Посмотреть Raspberry Pi</b>",
        "🗓 на этой неделе (до вс 11 окт) · 🔔 пт 19:00",
    ]
    item = await e2e.item("Посмотреть Raspberry Pi")
    assert (item.period_kind, item.period_start, item.period_end) == ("week", date(2026, 10, 5), date(2026, 10, 11))
