"""End-to-end, iteration 4 (docs/features/iteration-4.md): a voice note that is not speech (§1) and the events
journal (§5), with the local Whisper configured (``LOCAL_STT_URL`` -> the fake on 127.0.0.1).

§1: chutes hears subtitle credits, the local Whisper (the second try) a loop of one letter -> no items, "🎤 Не
разобрал голосовое…" with [🔁 Ещё раз] and 🤔, both STT calls in ai_usage with ok=false; [🔁 Ещё раз] after the
speech is clear -> the usual card in reply to the voice note.
§5: a command, a reminder sent, a button pressed on it and the lines of the evening digest end up in ``events``.
Harness: conftest.py (``e2e``); reference time Tue 2026-10-06 10:12 Europe/Warsaw.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from dumpbot.db.models import AiUsage, Event, Item

WARSAW = ZoneInfo("Europe/Warsaw")
OGG = b"OggS\x00\x02" + bytes(range(64)) * 4
VOICE = {"file_id": "voice-1", "file_unique_id": "uv1", "duration": 3, "mime_type": "audio/ogg", "file_size": 260}
SPEECH = "Купить наушники в обед"
HEADPHONES = {
    "intent": "save",
    "items": [{"kind": "purchase", "title": "Купить наушники", "when": {"text": "в обед", "time": "lunch"}}],
}


@pytest.fixture
def local_whisper() -> bool:
    return True


def local(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=WARSAW).astimezone(UTC)


async def _events(e2e: Any) -> list[Event]:
    async with e2e.ctx.sessionmaker() as s:
        return list((await s.scalars(select(Event).order_by(Event.id))).all())


async def test_a_voice_note_that_is_not_speech_is_not_saved_and_can_be_retried(e2e: Any) -> None:
    e2e.tg.add_file("voice-1", OGG, "voice/file_1.oga")
    e2e.ai.transcripts[OGG] = "Субтитры сделал DimaTorzok"
    e2e.ai.local_transcripts[OGG] = "ааааааааааааааааааааааа"
    message_id = e2e.tg.push_message(voice=VOICE)

    [reply] = await e2e.wait(lambda: e2e.tg.replies_to(message_id), "the reply to the voice note")
    await e2e.reaction(message_id, "🤔")
    raw = await e2e.raw(message_id)
    assert reply.text == "🎤 Не разобрал голосовое — повтори или напиши текстом"
    assert reply.buttons == [("🔁 Ещё раз", f"vr:{raw.id}")]
    assert e2e.tg.reactions(message_id) == ["👀", "🤔"]
    assert e2e.ai.extraction_requests() == []  # nothing to extract
    assert await e2e.all(Item) == []
    assert (raw.status, raw.transcript) == ("done", "ааааааааааааааааааааааа")
    [first, second] = e2e.ai.stt_requests + e2e.ai.local_requests
    assert first["language"] == second["language"] == "ru"
    usage = await e2e.all(AiUsage)
    assert [(u.model.startswith("local:"), u.ok, u.error) for u in usage] == [
        (False, False, "garbage transcript"),
        (True, False, "garbage transcript"),
    ]

    # The owner tries again once the noise is gone (here: the fake now hears speech).
    e2e.ai.transcripts[OGG] = SPEECH
    e2e.ai.script(SPEECH, HEADPHONES)
    e2e.tg.push_callback(reply, reply.button("vr:"))
    await e2e.wait(lambda: e2e.tg.toasts() == ["🔁 Распознаю ещё раз"], "the toast")
    [edit] = await e2e.wait(lambda: e2e.tg.edits_of(reply.message_id), "the reply turning into «Распознаю…»")
    assert edit.text == "🎤 Распознаю ещё раз…" and "reply_markup" not in edit.params
    card = await e2e.wait(lambda: e2e.tg.replies_to(message_id)[1:], "the card")
    await e2e.reaction(message_id, "👌")
    assert card[0].text.startswith("🛒 <b>Купить наушники</b>")
    assert "🎤 «Купить наушники в обед»" in card[0].text
    assert e2e.tg.reactions(message_id) == ["👀", "🤔", "👀", "👌"]
    assert (await e2e.raw(message_id)).transcript == SPEECH
    rows = await e2e.wait_db(lambda: _events(e2e), "the journal of the press")
    assert [(e.type, e.source, e.data["action"]) for e in rows] == [("button", "unheard", "vr")]


async def test_the_journal_of_a_day(e2e: Any) -> None:
    e2e.tg.push_message("/today", entities=[{"type": "bot_command", "offset": 0, "length": 6}])
    await e2e.wait(lambda: e2e.tg.sent, "the /today answer")

    e2e.ai.script(SPEECH, HEADPHONES)
    message_id = e2e.tg.push_message(SPEECH)
    await e2e.card_for(message_id)
    item = await e2e.item("Купить наушники")
    [occ] = await e2e.occurrences(item.id)

    await e2e.tick(local(2026, 10, 6, 13, 0))
    [reminder] = [c for c in e2e.tg.sent if c.text.startswith("🔔")]
    e2e.tg.push_callback(reminder, reminder.button(f"o:done:{occ.id}"))
    await e2e.wait(lambda: e2e.tg.toasts() == ["✅ Готово"], "the toast")

    await e2e.tick(local(2026, 10, 6, 21, 0))  # the evening digest: done 1 of 1
    assert any("Итоги дня" in c.text for c in e2e.tg.sent)

    def summary(rows: list[Event]) -> list[tuple[Any, ...]]:
        return [(e.type, e.source, e.item_id, e.occurrence_id, (e.data or {}).get("name")) for e in rows]

    async def complete() -> list[Event] | None:
        rows = await _events(e2e)
        return rows if len(rows) >= 4 else None

    rows = await e2e.wait_db(complete, "the journal")
    assert summary(rows) == [
        ("command", None, None, None, "today"),
        ("reminder_sent", "reminder", item.id, occ.id, None),
        ("button", "reminder", item.id, occ.id, None),
        ("digest_item", "digest", item.id, occ.id, None),
    ]
    assert rows[2].data == {"action": "o:done", "cb": f"o:done:{occ.id}"}
    assert rows[3].data == {"digest": "evening", "sec": "d"}
