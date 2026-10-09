"""End-to-end: [✅ N] under /today and the digests, [🌙 Вечером] in reminders (docs/features/iteration-3.md §2, §4).

/today -> [✅ 2] -> the same list edited in place (the line struck through, [↩️ 2]) -> [↩️ 2] -> open again; the evening
digest's [✅ 1] next to [📅 Перенести невыполненное на завтра]; a reminder at 10:00 offers [🌙 Вечером], which moves the
item to 19:00, while the one at 18:30 offers [⏰ 15 мин].

Harness: conftest.py (``e2e``); the clock starts on Tue 2026-10-06 10:12 Europe/Warsaw.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")


def local(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=WARSAW).astimezone(UTC)


def wall(dt: datetime) -> str:
    return dt.astimezone(WARSAW).strftime("%Y-%m-%d %H:%M")


def task(title: str, when: dict[str, Any]) -> dict[str, Any]:
    return {"intent": "save", "items": [{"kind": "task", "title": title, "when": when}]}


def command(e2e: Any, text: str) -> int:
    name = text.split()[0]
    return e2e.tg.push_message(text, entities=[{"type": "bot_command", "offset": 0, "length": len(name)}])


def labels(call: Any) -> list[str]:
    return [text for text, _ in call.buttons]


async def save(e2e: Any, text: str, extraction: dict[str, Any]) -> None:
    e2e.ai.script(text, extraction)
    await e2e.card_for(e2e.tg.push_message(text))
    await e2e.idle()


async def press(e2e: Any, message: Any, data: str) -> Any:
    """Press a button and wait for the edit of that message; returns the edit."""
    before = len(e2e.tg.edits_of(message.message_id))
    e2e.tg.push_callback(message, data)
    edits = await e2e.wait(lambda: e2e.tg.edits_of(message.message_id)[before:], f"edit after {data}")
    return edits[-1]


async def test_done_from_today_and_from_the_evening_digest(e2e: Any) -> None:
    await save(e2e, "купить хлеб в 12", task("Купить хлеб", {"text": "в 12", "time": "12:00"}))
    await save(e2e, "позвонить маме в 15", task("Позвонить маме", {"text": "в 15", "time": "15:00"}))
    bread, mom = await e2e.item("Купить хлеб"), await e2e.item("Позвонить маме")

    command(e2e, "/today")
    today = await e2e.wait(lambda: next((c for c in e2e.tg.sent if c.text.startswith("<b>📅")), None), "/today")
    assert today.text.split("\n") == [
        "<b>📅 Сегодня, вт 6 окт</b>",
        "1. сегодня 12:00 · 📌 Купить хлеб",
        "2. сегодня 15:00 · 📌 Позвонить маме",
    ]
    assert labels(today) == ["✅ 1", "✅ 2"]

    # [✅ 2]: closed without the AI, the list edited in place
    calls_before = len(e2e.ai.chat_requests)
    edit = await press(e2e, today, today.button("ld:done:t:20261006:o:2:"))
    assert edit.text.split("\n")[1:] == [
        "1. сегодня 12:00 · 📌 Купить хлеб",
        "2. ✅ сегодня 15:00 · 📌 <s>Позвонить маме</s>",
    ]
    assert labels(edit) == ["✅ 1", "↩️ 2"]
    assert e2e.tg.toasts() == ["✅ Закрыл «Позвонить маме»"]
    assert len(e2e.ai.chat_requests) == calls_before
    [occ] = await e2e.occurrences(mom.id)
    assert occ.status == "done" and (await e2e.item("Позвонить маме")).status == "done"
    assert [r.status for r in await e2e.reminders(mom.id)] == ["cancelled"]

    # [↩️ 2] on the edited list: open again, its 15:00 reminder back
    edit = await press(e2e, edit, edit.button("ld:undo:t:20261006:o:2:"))
    assert edit.text.split("\n")[2] == "2. сегодня 15:00 · 📌 Позвонить маме"
    assert labels(edit) == ["✅ 1", "✅ 2"]
    assert e2e.tg.toasts()[-1] == "↩️ Снова открыто: «Позвонить маме»"
    assert [(r.status, wall(r.fire_at)) for r in await e2e.reminders(mom.id)] == [
        ("cancelled", "2026-10-06 15:00"),
        ("pending", "2026-10-06 15:00"),
    ]

    # 21:00: the evening digest numbers what is not done; [✅ 1] there, then the move button still works
    await e2e.tick(local(2026, 10, 6, 21, 0))
    evening = e2e.tg.sent[-1]
    assert evening.text.startswith("🌙 <b>Итоги дня</b>")
    assert "1. ⬜ 12:00 · 📌 Купить хлеб" in evening.text and "2. ⬜ 15:00 · 📌 Позвонить маме" in evening.text
    assert labels(evening) == ["✅ 1", "✅ 2", "📅 Перенести невыполненное на завтра"]

    e2e.clock.set(local(2026, 10, 6, 21, 2))
    edit = await press(e2e, evening, evening.button("ld:done:e:20261006:m:1:"))
    assert "Сделано 1 из 2" in edit.text
    assert "1. ✅ 12:00 · 📌 <s>Купить хлеб</s>" in edit.text
    assert labels(edit) == ["↩️ 1", "✅ 2", "📅 Перенести невыполненное на завтра"]
    assert (await e2e.occurrences(bread.id))[0].status == "done"

    edit = await press(e2e, edit, edit.button("d:tmr:20261006"))
    assert "📅 Перенесено на завтра: 1" in edit.text
    [occ] = await e2e.occurrences(mom.id)
    assert (wall(occ.due_at), occ.status) == ("2026-10-07 15:00", "open")
    assert (await e2e.occurrences(bread.id))[0].status == "done"  # the closed line stays closed


async def test_evening_button_on_a_morning_reminder(e2e: Any) -> None:
    await save(
        e2e,
        "завтра в 10 позвонить в банк",
        task("Позвонить в банк", {"text": "завтра в 10", "day": "+1", "time": "10:00"}),
    )
    await save(
        e2e,
        "завтра в 18:30 забрать посылку",
        task("Забрать посылку", {"text": "завтра в 18:30", "day": "+1", "time": "18:30"}),
    )
    bank = await e2e.item("Позвонить в банк")

    # Wed 10:00: the evening is far -> [⏰ 1 час] [🌙 Вечером] [📅 Завтра]
    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 7, 10, 0))
    [reminder] = [c for c in e2e.tg.sent[sent_before:] if c.text.startswith("🔔 ")]
    assert reminder.text == "🔔 <b>Позвонить в банк</b>"
    assert labels(reminder) == ["✅ Готово", "⏰ 1 час", "🌙 Вечером", "📅 Завтра"]

    # [🌙 Вечером] at 10:01: the item moves to 19:00 today
    e2e.clock.set(local(2026, 10, 7, 10, 1))
    edit = await press(e2e, reminder, reminder.button("o:eve:"))
    assert edit.text == "🔔 <b>Позвонить в банк</b>\n💤 отложено · напомню сегодня в 19:00"
    assert edit.buttons == []
    assert e2e.tg.toasts() == ["🌙 Напомню сегодня 19:00"]
    [occ] = await e2e.occurrences(bank.id)
    assert (wall(occ.due_at), occ.status) == ("2026-10-07 19:00", "open")

    # 18:30: less than an hour before the evening -> [⏰ 15 мин] [⏰ 1 час] [📅 Завтра]
    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 7, 18, 30))
    [parcel] = e2e.tg.sent[sent_before:]
    assert parcel.text == "🔔 <b>Забрать посылку</b>"
    assert labels(parcel) == ["✅ Готово", "⏰ 15 мин", "⏰ 1 час", "📅 Завтра"]

    # 19:00: the bank call comes again
    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 7, 19, 0))
    [again] = e2e.tg.sent[sent_before:]
    assert again.text == "🔔 <b>Позвонить в банк</b>"
    assert labels(again) == ["✅ Готово", "⏰ 15 мин", "⏰ 1 час", "📅 Завтра"]
    assert [(r.status, wall(r.fire_at)) for r in await e2e.reminders(bank.id)] == [
        ("sent", "2026-10-07 10:00"),
        ("sent", "2026-10-07 19:00"),
    ]
