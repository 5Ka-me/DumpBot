"""End-to-end: fuzzy time (docs/features/iteration-2.md §A) through the whole app — the fake chutes returns a period
extraction -> card -> the morning digest's period section every day of the week -> the Friday 19:00 reminder ->
overdue after the week; a deadline with its two reminders; "когда-нибудь" in /someday, moved to next week by a reply.

Harness: conftest.py (``e2e``). The owner's live example: "На этой неделе посмотреть расбери пай", Tue 2026-10-06
13:36 Europe/Warsaw.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")
LIVE_TEXT = "На этой неделе посмотреть расбери пай"
LIVE = {
    "intent": "save",
    "items": [
        {
            "kind": "task",
            "title": "Посмотреть Raspberry Pi",
            "tags": ["raspberry_pi"],
            "when": {"text": "На этой неделе", "period": "this_week"},
        }
    ],
}
SECTION = "🗓 <b>На этой неделе</b> (до вс 11 окт)\n1. 📌 Посмотреть Raspberry Pi"


def local(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    """Europe/Warsaw wall time -> aware UTC."""
    return datetime(y, mo, d, h, mi, tzinfo=WARSAW).astimezone(UTC)


def wall(dt: datetime) -> str:
    return dt.astimezone(WARSAW).strftime("%Y-%m-%d %H:%M")


def command(e2e: Any, text: str) -> int:
    name = text.split()[0]
    return e2e.tg.push_message(text, entities=[{"type": "bot_command", "offset": 0, "length": len(name)}])


async def morning(e2e: Any, day: int) -> str | None:
    """Tick at 09:00 on that October day; the digest text, or None when nothing was sent."""
    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, day, 9, 0))
    new = e2e.tg.sent[sent_before:]
    assert len(new) <= 1
    return new[0].text if new else None


async def test_this_week_card_digest_section_and_friday_reminder(e2e: Any) -> None:
    e2e.clock.set(local(2026, 10, 6, 13, 36))
    e2e.ai.script(LIVE_TEXT, LIVE)
    message_id = e2e.tg.push_message(LIVE_TEXT)
    card = await e2e.card_for(message_id)
    await e2e.reaction(message_id, "👌")

    [request] = e2e.ai.extraction_requests()
    assert request.line("NOW ") == "NOW 2026-10-06T13:36 Tue Europe/Warsaw"
    assert "period" in request.body["messages"][0]["content"]  # the v2 prompt explains periods

    # the card: the span in words, the end of the week, the Friday 19:00 reminder
    assert card.text.split("\n") == [
        "📌 <b>Посмотреть Raspberry Pi</b>",
        "🗓 на этой неделе (до вс 11 окт) · 🔔 пт 19:00",
        "🏷 Задача · #raspberry_pi",
    ]
    assert [text for text, _ in card.buttons] == ["✏️ Изменить", "🔕 Без напоминания", "🗑 Удалить", "🪄 Разбить на шаги"]
    item = await e2e.item("Посмотреть Raspberry Pi")
    assert (item.period_kind, item.period_start, item.period_end) == ("week", date(2026, 10, 5), date(2026, 10, 11))
    [occ] = await e2e.occurrences(item.id)
    assert (occ.precision, wall(occ.due_at)) == ("period", "2026-10-09 19:00")
    [reminder] = await e2e.reminders(item.id)
    assert (reminder.status, wall(reminder.fire_at), reminder.auto) == ("pending", "2026-10-09 19:00", True)

    # Tuesday evening: nothing due today -> no evening digest
    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 6, 21, 0))
    assert len(e2e.tg.sent) == sent_before

    # Wednesday and Thursday mornings: the period section, nothing planned for the day itself
    for day in (7, 8):
        text = await morning(e2e, day)
        assert text is not None and SECTION in text
        assert "На сегодня ничего не запланировано." in text and "Просрочено" not in text

    # Friday morning: still the section, not a numbered "19:00" item of the day
    text = await morning(e2e, 9)
    assert text is not None and SECTION in text and "1. 19:00" not in text

    # Friday 18:59 nothing, 19:00 the reminder
    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 9, 18, 59))
    assert len(e2e.tg.sent) == sent_before
    await e2e.tick(local(2026, 10, 9, 19, 0))
    [reminder_msg] = e2e.tg.sent[sent_before:]
    assert reminder_msg.text == "🔔 <b>Посмотреть Raspberry Pi</b>\n🗓 на этой неделе (до вс 11 окт)"
    assert [text for text, _ in reminder_msg.buttons] == ["✅ Готово", "⏰ 15 мин", "⏰ 1 час", "📅 Завтра"]
    [reminder] = await e2e.reminders(item.id)
    assert (reminder.status, reminder.tg_message_id) == ("sent", reminder_msg.message_id)

    # Friday evening: the week is still running, so it is not "не сделано" -> no evening digest
    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 9, 21, 0))
    assert len(e2e.tg.sent) == sent_before

    # Saturday and Sunday: still in the section until the week ends
    for day in (10, 11):
        text = await morning(e2e, day)
        assert text is not None and SECTION in text and "Просрочено" not in text

    # Monday: the week is over -> overdue, no section
    text = await morning(e2e, 12)
    assert text is not None
    assert "⚠️ <b>Просрочено</b>\n1. на неделе с 5 окт · 📌 Посмотреть Raspberry Pi" in text
    assert "На этой неделе" not in text


async def test_done_on_friday_leaves_the_section(e2e: Any) -> None:
    e2e.clock.set(local(2026, 10, 6, 13, 36))
    e2e.ai.script(LIVE_TEXT, LIVE)
    await e2e.card_for(e2e.tg.push_message(LIVE_TEXT))
    await e2e.idle()

    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 9, 19, 0))
    [reminder_msg] = e2e.tg.sent[sent_before:]
    occ_id = (await e2e.occurrences((await e2e.item("Посмотреть Raspberry Pi")).id))[0].id
    e2e.clock.set(local(2026, 10, 9, 19, 5))
    e2e.tg.push_callback(reminder_msg, reminder_msg.button(f"o:done:{occ_id}"))
    [edit] = await e2e.wait(lambda: e2e.tg.edits_of(reminder_msg.message_id), "the reminder edited")
    assert edit.text.split("\n") == [
        "✅ <s>Посмотреть Raspberry Pi</s>",
        "<i>на этой неделе (до вс 11 окт) · сделано</i>",
    ]

    # Friday evening: done today; Saturday: nothing left for the morning
    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 9, 21, 0))
    [evening] = e2e.tg.sent[sent_before:]
    assert "Всё сделано 🎉" in evening.text and "✅ 📌 <s>Посмотреть Raspberry Pi</s>" in evening.text
    assert await morning(e2e, 10) is None


async def test_deadline_reminds_the_evening_before_and_in_the_morning(e2e: Any) -> None:
    e2e.ai.script(
        "до пятницы сдать отчёт",
        {
            "intent": "save",
            "items": [
                {
                    "kind": "task",
                    "title": "Сдать отчёт",
                    "when": {"text": "до пятницы", "day": "FR", "role": "deadline"},
                }
            ],
        },
    )
    message_id = e2e.tg.push_message("до пятницы сдать отчёт")
    card = await e2e.card_for(message_id)
    assert "⏳ до пт 9 окт · 🔔 чт 19:00, пт 10:00" in card.text.split("\n")
    item = await e2e.item("Сдать отчёт")
    assert [(wall(r.fire_at), r.auto) for r in await e2e.reminders(item.id)] == [
        ("2026-10-08 19:00", True),
        ("2026-10-09 10:00", True),
    ]

    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 8, 19, 0))
    [eve] = e2e.tg.sent[sent_before:]
    assert eve.text == "🔔 <b>Сдать отчёт</b>\n⏳ срок: завтра"

    await e2e.tick(local(2026, 10, 9, 9, 0))  # Friday's morning digest lists it for today
    await e2e.tick(local(2026, 10, 9, 10, 0))
    digest_msg, day_of = e2e.tg.sent[sent_before + 1 :]
    assert "1. 10:00 · 📌 Сдать отчёт" in digest_msg.text
    assert day_of.text == "🔔 <b>Сдать отчёт</b>\n⏳ срок: сегодня"


async def test_someday_lives_in_its_own_list_until_it_gets_a_week(e2e: Any) -> None:
    e2e.ai.script(
        "когда-нибудь выучить испанский",
        {
            "intent": "save",
            "items": [
                {"kind": "task", "title": "Выучить испанский", "when": {"text": "когда-нибудь", "period": "someday"}}
            ],
        },
    )
    message_id = e2e.tg.push_message("когда-нибудь выучить испанский")
    card = await e2e.card_for(message_id)
    assert card.text.split("\n")[1] == "🌙 когда-нибудь"
    assert [text for text, _ in card.buttons] == ["✏️ Изменить", "🗑 Удалить", "🪄 Разбить на шаги"]
    item = await e2e.item("Выучить испанский")
    assert item.someday and await e2e.occurrences(item.id) == []

    for text in ("/someday", "/tasks"):
        count = len(e2e.tg.sent)
        command(e2e, text)
        await e2e.wait(lambda count=count: len(e2e.tg.sent) > count, f"answer to {text}")
    someday_list, tasks = (c.text for c in e2e.tg.sent[-2:])
    assert someday_list.startswith("<b>🌙 Когда-нибудь</b>") and "• когда-нибудь · 📌 Выучить испанский" in someday_list
    assert "испанский" not in tasks

    # a reply to the card gives it a week: both reminders, out of /someday
    e2e.ai.script(
        "давай на следующей неделе",
        {"intent": "edit", "edit": {"when": {"text": "на следующей неделе", "period": "next_week"}}},
    )
    reply = e2e.tg.push_message("давай на следующей неделе", reply_to=card)
    new_card = await e2e.card_for(reply)
    replying = e2e.ai.extraction_requests()[-1].line("REPLYING TO ITEM ")
    assert replying is not None and '"someday": true' in replying
    assert "🗓 на следующей неделе (до вс 18 окт) · 🔔 пн 10:00, пт 16 окт 19:00" in new_card.text
    item = await e2e.item("Выучить испанский")
    assert (item.someday, item.period_kind) == (False, "week")
    assert [wall(r.fire_at) for r in await e2e.reminders(item.id)] == ["2026-10-12 10:00", "2026-10-16 19:00"]

    count = len(e2e.tg.sent)
    command(e2e, "/someday")
    await e2e.wait(lambda: len(e2e.tg.sent) > count, "answer to /someday")
    assert "📌 Выучить испанский" not in e2e.tg.sent[-1].text

    # Monday 10:00: reminder (1) of the week
    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 12, 10, 0))
    [first_day] = [c for c in e2e.tg.sent[sent_before:] if c.text.startswith("🔔")]
    assert first_day.text == "🔔 <b>Выучить испанский</b>\n🗓 на этой неделе (до вс 18 окт)"
