"""End-to-end, iteration 4 §2-3 (docs/features/iteration-4.md): repetitions and reminder buttons by item type.

§2: "каждый день в 9:00 и 18:00" -> one series "🔁 каждый день в 09:00 и 18:00" -> the 09:00 reminder with [⏹ Хватит
повторять] -> ✅ -> [⏹ Больше не повторять] -> the 18:00 reminder comes, [⏹ Хватит повторять] on it -> nothing more,
not even the instance made in advance; "закрывай вообще" in reply to a reminder stops a series without the AI.
§3: a week's reminder -> [📅 Выбрать день] -> the 7-day picker -> a day -> the item is due that day at 19:00;
[➡️ След. неделя] makes it next week's.
Harness: conftest.py (``e2e``); reference time Tue 2026-10-06 10:12 Europe/Warsaw.
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


async def save(e2e: Any, text: str, extraction: dict[str, Any]) -> Any:
    e2e.ai.script(text, extraction)
    card = await e2e.card_for(e2e.tg.push_message(text))
    await e2e.idle()
    return card


async def press(e2e: Any, message: Any, data: str) -> Any:
    """Press a button and wait for the edit of that message; returns the edit."""
    before = len(e2e.tg.edits_of(message.message_id))
    e2e.tg.push_callback(message, data)
    edits = await e2e.wait(lambda: e2e.tg.edits_of(message.message_id)[before:], f"edit after {data}")
    return edits[-1]


async def reminders_at(e2e: Any, now: datetime) -> list[Any]:
    sent_before = len(e2e.tg.sent)
    await e2e.tick(now)
    return [c for c in e2e.tg.sent[sent_before:] if c.text.startswith("🔔 ")]


PILLS = "каждый день в 9:00 и 18:00 пить таблетки"
PILLS_ITEM = {
    "intent": "save",
    "items": [
        {
            "kind": "task",
            "title": "Выпить таблетки",
            "when": {"text": "каждый день в 9:00 и 18:00", "time": "09:00"},
            "rrule": "FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0",
        }
    ],
}


async def test_two_times_a_day_then_stop(e2e: Any) -> None:
    card = await save(e2e, PILLS, PILLS_ITEM)
    assert "🔁 каждый день в 09:00 и 18:00" in card.text.splitlines()
    assert "🔔 сегодня 18:00" in card.text.splitlines()  # 10:12: this evening's comes first
    item = await e2e.item("Выпить таблетки")

    # 18:00: the reminder of a series: [⏰ 1 час] [⏹ Хватит повторять]
    [evening] = await reminders_at(e2e, local(2026, 10, 6, 18, 0))
    assert evening.text == "🔔 <b>Выпить таблетки</b>\n🔁 каждый день в 09:00 и 18:00"
    assert [text for text, _ in evening.buttons] == ["✅ Готово", "⏰ 1 час", "⏹ Хватит повторять"]
    # ✅: tomorrow's 09:00 is made, and the message offers to stop
    edit = await press(e2e, evening, evening.button("o:done:"))
    assert [text for text, _ in edit.buttons] == ["↩️ Не сделано", "⏹ Больше не повторять"]
    open_ = [o for o in await e2e.occurrences(item.id) if o.status == "open"]
    assert [wall(o.due_at) for o in open_] == ["2026-10-07 09:00"]

    # Wednesday 09:00, then 18:00: both times come
    [morning] = await reminders_at(e2e, local(2026, 10, 7, 9, 0))
    assert morning.text.startswith("🔔 <b>Выпить таблетки</b>")
    [evening] = await reminders_at(e2e, local(2026, 10, 7, 18, 0))

    # [⏹ Хватит повторять] at 18:01: this time closed, Thursday's 09:00 (made in advance) cancelled
    e2e.clock.set(local(2026, 10, 7, 18, 1))
    edit = await press(e2e, evening, evening.button("o:stop:"))
    assert edit.text.endswith("🔁 повтор остановлен")
    assert e2e.tg.toasts()[-1] == "⏹ Повтор остановлен"
    item = await e2e.item("Выпить таблетки")
    # this morning's instance, never ticked, was missed: nothing is left open, the stopped series is done (not an
    # overdue item for ever)
    assert (item.rec_active, item.status) == (False, "done")
    statuses = {wall(o.due_at): o.status for o in await e2e.occurrences(item.id)}
    assert (statuses["2026-10-07 09:00"], statuses["2026-10-07 18:00"]) == ("missed", "done")
    assert "open" not in statuses.values()
    assert await reminders_at(e2e, local(2026, 10, 8, 9, 0)) == []
    assert await reminders_at(e2e, local(2026, 10, 8, 18, 0)) == []


async def test_closing_a_series_by_reply_needs_no_ai(e2e: Any) -> None:
    """The bug of 17.10: «закрывай вообще» did not stop the series, and the reminder made in advance still came."""
    text = "каждый день в 11 полить цветы"
    item_json = {
        "kind": "task",
        "title": "Полить цветы",
        "when": {"text": "в 11", "time": "11:00"},
        "rrule": "FREQ=DAILY",
    }
    await save(e2e, text, {"intent": "save", "items": [item_json]})
    [reminder] = await reminders_at(e2e, local(2026, 10, 6, 11, 0))
    await e2e.tick(local(2026, 10, 6, 11, 1))  # tomorrow's instance (and its reminder) is made now
    requests = len(e2e.ai.extraction_requests())

    e2e.clock.set(local(2026, 10, 6, 12, 0))
    reply = e2e.tg.push_message("Закрывай вообще", reply_to=reminder)
    [card] = await e2e.wait(lambda: e2e.tg.replies_to(reply), "the card after the stop")
    await e2e.reaction(reply, "👌")
    assert "🔁 повтор остановлен" in card.text
    assert len(e2e.ai.extraction_requests()) == requests  # no AI call for it
    item = await e2e.item("Полить цветы")
    assert (item.rec_active, item.status) == (False, "done")
    assert await reminders_at(e2e, local(2026, 10, 7, 11, 0)) == []


WEEK_ITEM = {
    "intent": "save",
    "items": [
        {"kind": "task", "title": "Посмотреть Raspberry Pi", "when": {"text": "на этой неделе", "period": "this_week"}}
    ],
}


async def test_a_week_item_picked_to_a_day(e2e: Any) -> None:
    await save(e2e, "на этой неделе посмотреть Raspberry Pi", WEEK_ITEM)
    item = await e2e.item("Посмотреть Raspberry Pi")

    # Friday 19:00: the span's reminder
    [reminder] = await reminders_at(e2e, local(2026, 10, 9, 19, 0))
    assert [text for text, _ in reminder.buttons] == [
        "✅ Готово",
        "➡️ След. неделя",
        "📅 Выбрать день",
        "💤 Когда-нибудь",
    ]
    e2e.clock.set(local(2026, 10, 9, 19, 5))
    picker = await press(e2e, reminder, reminder.button("o:pick:"))
    days = [text for text, _ in picker.buttons]
    assert days == ["сб 10", "вс 11", "пн 12", "вт 13", "ср 14", "чт 15", "пт 16", "Другая дата…", "← Назад"]

    # Monday
    edit = await press(e2e, reminder, dict((t, d) for t, d in picker.buttons)["пн 12"])
    assert edit.text == "🔔 <b>Посмотреть Raspberry Pi</b>\n➡️ перенёс · 🔔 пн 12 окт 19:00"
    [occ] = [o for o in await e2e.occurrences(item.id) if o.status == "open"]
    assert (wall(occ.due_at), occ.precision) == ("2026-10-12 19:00", "exact")
    [again] = await reminders_at(e2e, local(2026, 10, 12, 19, 0))
    assert [text for text, _ in again.buttons] == ["✅ Готово", "⏰ 1 час", "⏰ 3 часа", "📅 Завтра"]  # a plain day now


async def test_a_week_item_to_the_next_week(e2e: Any) -> None:
    await save(e2e, "на этой неделе посмотреть Raspberry Pi", WEEK_ITEM)
    item = await e2e.item("Посмотреть Raspberry Pi")
    [reminder] = await reminders_at(e2e, local(2026, 10, 9, 19, 0))

    edit = await press(e2e, reminder, reminder.button("o:nw:"))
    assert "🗓 на следующей неделе (до вс 18 окт)" in edit.text
    item = await e2e.item("Посмотреть Raspberry Pi")
    assert (str(item.period_start), str(item.period_end)) == ("2026-10-12", "2026-10-18")
    [monday] = await reminders_at(e2e, local(2026, 10, 12, 10, 0))  # (1) the first morning of the span
    assert monday.text.startswith("🔔 <b>Посмотреть Raspberry Pi</b>\n🗓 на этой неделе (до вс 18 окт)")
