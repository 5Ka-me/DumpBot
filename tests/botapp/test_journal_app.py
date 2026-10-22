"""The journal through the real dispatcher (docs/features/iteration-4.md §5): commands and button presses become
``events`` rows after they were handled; [🔁 Ещё раз] of a voice note that was not understood (§1); the voice
settings while the local Whisper is paused."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from dumpbot.ai.stt import RoutedSTT
from dumpbot.bot.cbdata import AgainCb, OccCb, SetCb
from dumpbot.db.models import Event, RawMessage
from dumpbot.services.ingest import UNHEARD_KEY

OWNER = 4242
T0 = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)


async def _events(seed) -> list[Event]:
    async with seed.sessionmaker() as s:
        return list((await s.scalars(select(Event).order_by(Event.id))).all())


async def test_a_command_is_journaled_by_name_only(feed, updates, seed, tg) -> None:
    await feed(updates.message("/today"))
    await feed(updates.message("/search наушники sony"))

    assert len(tg.sent) == 2  # answered as usual
    rows = await _events(seed)
    assert [(e.type, e.source, e.data) for e in rows] == [
        ("command", None, {"name": "today"}),
        ("command", None, {"name": "search"}),  # the search text is message text: not kept
    ]


async def test_a_plain_message_is_no_command(feed, updates, seed) -> None:
    await feed(updates.message("купить хлеб"))
    assert await _events(seed) == []


async def test_a_button_press_names_its_action_item_and_message(feed, updates, seed, tg) -> None:
    created = await seed.item("Купить хлеб")
    occ = created.occurrence
    await seed.bot_message(901, purpose="reminder", item_id=created.item.id, occurrence_id=occ.id)

    await feed(updates.callback(OccCb(a="done", id=occ.id), message_id=901))

    assert tg.toasts == ["✅ Готово"]
    [row] = await _events(seed)
    assert (row.type, row.source, row.item_id, row.occurrence_id) == ("button", "reminder", created.item.id, occ.id)
    assert row.data == {"action": "o:done", "cb": f"o:done:{occ.id}"}


async def test_settings_and_stale_buttons_are_journaled_too(feed, updates, seed) -> None:
    await feed(updates.callback(SetCb(key="menu", v="stt"), message_id=902))
    await feed(updates.callback("l:5:30", message_id=903))  # the removed "⏱ Напомнить за…"
    rows = await _events(seed)
    assert [(e.type, e.data["action"], e.source) for e in rows] == [("button", "s:menu", None), ("button", "l:5", None)]


async def _unheard_voice(seed, message_id: int = 60) -> RawMessage:
    """A voice note answered with "🎤 Не разобрал…" (what the processor leaves behind)."""
    async with seed.sessionmaker() as s:
        raw = RawMessage(
            chat_id=OWNER,
            message_id=message_id,
            content_type="voice",
            file_id="v1",
            duration=3,
            tg_date=T0,
            status="done",
            transcript="ааааааааааааааа",
        )
        s.add(raw)
        await s.flush()
        tries = [{"stt": "chutes", "reason": "repeated letter", "text": "ааааааааааааааа"}]
        raw.raw_json = {UNHEARD_KEY: {"tries": tries, "job": {"kind": "message", "payload": {"raw_id": raw.id}}}}
        await s.commit()
    return raw


async def test_again_queues_the_voice_note_once(feed, updates, seed, tg, ctx) -> None:
    raw = await _unheard_voice(seed)
    await seed.bot_message(910, purpose="unheard", raw_message_id=raw.id)

    await feed(updates.callback(AgainCb(raw=raw.id), message_id=910, text="🎤 Не разобрал голосовое"))
    await feed(updates.callback(AgainCb(raw=raw.id), message_id=910, text="🎤 Распознаю ещё раз…"))

    assert tg.toasts == ["🔁 Распознаю ещё раз", "Уже распознаю ещё раз…"]
    [edit] = tg.edits
    assert (edit.message_id, edit.text, edit.reply_markup) == (910, "🎤 Распознаю ещё раз…", None)
    assert tg.reactions == [(60, "👀")]
    [job] = await seed.jobs()
    assert (job.kind, job.payload, job.status) == ("message", {"raw_id": raw.id}, "queued")
    [stored] = await seed.raws()
    assert (stored.status, stored.transcript) == ("new", None)
    assert ctx.jobs_wakeup.is_set()
    rows = await _events(seed)
    assert [(e.data["action"], e.source) for e in rows] == [("vr", "unheard"), ("vr", "unheard")]


async def test_again_for_a_message_that_is_gone(feed, updates, tg) -> None:
    await feed(updates.callback(AgainCb(raw=999), message_id=911))
    assert tg.toasts == ["Кнопка устарела"] and tg.edits == []


class _Backend:
    def __init__(self, paused_for: float = 0.0) -> None:
        self.paused_for = paused_for

    def pause_left(self) -> float:
        return self.paused_for

    async def transcribe(self, audio: bytes, **kwargs: Any) -> str:  # pragma: no cover - not called here
        return ""


async def test_voice_menu_marks_a_paused_local_whisper(feed, updates, tg, ctx, settings_factory, get_buttons) -> None:
    ctx.settings = settings_factory(local_stt_url="http://whisper:9000")
    local = _Backend(paused_for=420.0)
    ctx.stt = RoutedSTT(_Backend(), local)  # type: ignore[arg-type]

    await feed(updates.callback(SetCb(key="menu", v="stt"), message_id=937))
    local.paused_for = 0.0  # the pause is over
    await feed(updates.callback(SetCb(key="menu", v="stt"), message_id=938))

    paused, back = tg.edits
    local_button = next(text for text, data in get_buttons(paused.reply_markup) if data == "s:stt_mode:local")
    assert local_button.endswith("(недоступен)")
    assert "Локальный Whisper сейчас недоступен" in paused.text
    local_button = next(text for text, data in get_buttons(back.reply_markup) if data == "s:stt_mode:local")
    assert not local_button.endswith(")") and "недоступен" not in back.text
