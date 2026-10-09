"""End-to-end: nothing is lost after downtime, voice transcripts are shown (docs/features/iteration-3.md §5, §6).

The laptop sleeps from 19:00 to 23:19 (the clock jumps between two scheduler ticks; the second tick is a restarted
process, a new ``Scheduler``): one silent notice; the 20:00 reminder waits for the end of quiet hours and comes
silently at 08:00; the 21:00 evening digest goes into the 09:00 morning digest, whose [✅ N] closes yesterday's line in
place. A voice idea gets "🎤 «…» → 💡 Идея «…»", and [🗑 Не то — удалить] deletes it.

Harness: conftest.py (``e2e``); the clock starts on Tue 2026-10-06 10:12 Europe/Warsaw.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from dumpbot.workers.scheduler import Scheduler

WARSAW = ZoneInfo("Europe/Warsaw")
OGG = b"OggS\x00\x02" + bytes(range(64)) * 4


def local(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=WARSAW).astimezone(UTC)


def labels(call: Any) -> list[str]:
    return [text for text, _ in call.buttons]


async def test_sleep_from_19_to_23_19(e2e: Any) -> None:
    text = "позвонить маме в 20"
    e2e.ai.script(
        text, {"intent": "save", "items": [{"kind": "task", "title": "Позвонить маме", "when": {"time": "20:00"}}]}
    )
    await e2e.card_for(e2e.tg.push_message(text))
    await e2e.idle()
    mom = await e2e.item("Позвонить маме")
    sent_before = len(e2e.tg.sent)

    await e2e.tick(local(2026, 10, 6, 19, 0))  # the last tick before the lid closes
    assert len(e2e.tg.sent) == sent_before

    # 23:19: the PC wakes up, Docker restarts the bot — a new process finds the 19:00 tick in the DB
    e2e.clock.set(local(2026, 10, 6, 23, 19))
    restarted = Scheduler(e2e.ctx)
    await restarted.tick(local(2026, 10, 6, 23, 19))
    [notice] = e2e.tg.sent[sent_before:]
    assert notice.text == (
        "😴 Меня не было с 19:00 до 23:19 (компьютер спал?). Опоздало напоминаний: 1 — пришлю в 08:00. "
        "Итог дня — в утреннем дайджесте."
    )
    assert notice.params["disable_notification"] == "true"
    [reminder] = await e2e.reminders(mom.id)
    assert (reminder.status, reminder.fire_at, reminder.held_from) == (
        "pending",
        local(2026, 10, 7, 8, 0),
        local(2026, 10, 6, 20, 0),
    )
    await restarted.tick(local(2026, 10, 6, 23, 20))
    assert len(e2e.tg.sent) == sent_before + 1  # one notice

    # 08:00: the held reminder, silently, with its own time
    await e2e.tick(local(2026, 10, 7, 8, 0))
    held = e2e.tg.sent[-1]
    assert held.text.split("\n") == ["🔔 <b>Позвонить маме</b>", "⏰ вчера 20:00 · опоздал на 12 ч"]
    assert held.params["disable_notification"] == "true"
    assert labels(held)[0] == "✅ Готово"

    # 09:00: the morning digest with yesterday's summary
    await e2e.tick(local(2026, 10, 7, 9, 0))
    morning = e2e.tg.sent[-1]
    assert morning.text.split("\n") == [
        "☀️ <b>Доброе утро!</b> План на сегодня, ср 7 окт",
        "",
        "На сегодня ничего не запланировано.",
        "",
        "🌙 <b>Вчерашний итог</b> (не успел прислать)",
        "Сделано 0 из 1",
        "1. 20:00 · 📌 Позвонить маме",
    ]
    assert labels(morning) == ["✅ 1"]

    # [✅ 1]: yesterday's line closed in place, the section stays
    e2e.clock.set(local(2026, 10, 7, 9, 2))
    e2e.tg.push_callback(morning, morning.button("ld:done:m:20261007:y:1:"))
    [edit] = await e2e.wait(lambda: e2e.tg.edits_of(morning.message_id), "the morning digest edited")
    assert edit.text.split("\n")[4:] == [
        "🌙 <b>Вчерашний итог</b> (не успел прислать)",
        "Всё сделано 🎉",
        "1. ✅ 20:00 · 📌 <s>Позвонить маме</s>",
    ]
    assert labels(edit) == ["↩️ 1"]
    assert e2e.tg.toasts() == ["✅ Закрыл «Позвонить маме»"]
    assert (await e2e.item("Позвонить маме")).status == "done"


async def test_voice_idea_shows_the_transcript_and_can_be_deleted(e2e: Any) -> None:
    e2e.tg.add_file("voice-1", OGG, "voice/file_1.oga")
    e2e.ai.transcripts[OGG] = "идея бот сам предлагает подзадачи"
    e2e.ai.script(
        "идея бот сам предлагает подзадачи",
        {"intent": "save", "items": [{"kind": "idea", "title": "Бот предлагает подзадачи"}]},
    )
    voice = {"file_id": "voice-1", "file_unique_id": "uv1", "duration": 3, "mime_type": "audio/ogg", "file_size": 260}
    message_id = e2e.tg.push_message(voice=voice)
    msg = await e2e.card_for(message_id)
    await e2e.reaction(message_id, "👌")
    assert msg.text == "🎤 «идея бот сам предлагает подзадачи» → 💡 Идея «Бот предлагает подзадачи»"
    assert labels(msg) == ["✏️ Изменить", "🗑 Не то — удалить"]

    item = await e2e.item("Бот предлагает подзадачи")
    e2e.tg.push_callback(msg, msg.button(f"i:del:{item.id}"))
    [edit] = await e2e.wait(lambda: e2e.tg.edits_of(msg.message_id), "the message edited")
    assert edit.text == "🗑 Удалено: 💡 <s>Бот предлагает подзадачи</s>"
    assert labels(edit) == ["↩️ Вернуть"]
    assert (await e2e.item("Бот предлагает подзадачи")).status == "deleted"
