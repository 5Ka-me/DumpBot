"""End-to-end: checklists and related items (docs/features/iteration-2.md §C) through the whole app.

"Переезд в субботу, распиши по шагам" -> one card with the checklist -> a timed step in /tasks, the morning plan and
its own reminder -> every step ticked (buttons and the step's reminder) -> "🎉 Все шаги выполнены" -> unticking opens
the task again. Also [🪄 Разбить на шаги], "разбей на шаги" in reply to a card and "🔗 похоже на …".

Harness: conftest.py (``e2e``); the clock starts on Tue 2026-10-06 10:12 Europe/Warsaw.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from dumpbot.db.models import AiUsage, Item, ItemLink

WARSAW = ZoneInfo("Europe/Warsaw")
MOVE_TEXT = "Переезд в субботу, распиши по шагам"
MOVE = {
    "intent": "save",
    "items": [
        {
            "kind": "task",
            "title": "Переезд",
            "when": {"text": "в субботу", "day": "SA"},
            "steps": [
                {"title": "Упаковать вещи"},
                {"title": "Заказать машину", "when": {"text": "в четверг в 15", "day": "TH", "time": "15:00"}},
                {"title": "Перевезти вещи"},
                {"title": "Отдать ключи"},
            ],
        }
    ],
}
CLOSED = "🎉 Все шаги выполнены — «Переезд» закрыт"


def local(y: int, mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=WARSAW).astimezone(UTC)


def command(e2e: Any, text: str) -> int:
    name = text.split()[0]
    return e2e.tg.push_message(text, entities=[{"type": "bot_command", "offset": 0, "length": len(name)}])


async def steps_of(e2e: Any, parent_id: int) -> list[Item]:
    async with e2e.ctx.sessionmaker() as s:
        stmt = select(Item).where(Item.parent_id == parent_id).order_by(Item.position)
        return list((await s.scalars(stmt)).all())


async def press(e2e: Any, message: Any, data: str) -> Any:
    """Press a button and wait for the edit of that message; returns the edit."""
    before = len(e2e.tg.edits_of(message.message_id))
    e2e.tg.push_callback(message, data)
    edits = await e2e.wait(lambda: e2e.tg.edits_of(message.message_id)[before:], f"edit after {data}")
    return edits[-1]


def labels(call: Any) -> list[str]:
    return [text for text, _ in call.buttons]


async def test_move_checklist_to_all_steps_done(e2e: Any) -> None:
    e2e.ai.script(MOVE_TEXT, MOVE)
    message_id = e2e.tg.push_message(MOVE_TEXT)
    card = await e2e.card_for(message_id)
    await e2e.reaction(message_id, "👌")
    await e2e.idle()

    # one card: the task with its checklist (the steps get no cards of their own)
    assert len(e2e.tg.replies_to(message_id)) == 1
    assert card.text.split("\n") == [
        "📌 <b>Переезд</b>",
        "🔔 сб 10 окт 10:00",
        "📋 Шаги: 0/4",
        "☐ 1. Упаковать вещи",
        "☐ 2. Заказать машину · чт 8 окт 15:00",
        "☐ 3. Перевезти вещи",
        "☐ 4. Отдать ключи",
        "🏷 Задача",
    ]
    assert labels(card) == ["☐ 1", "☐ 2", "☐ 3", "☐ 4", "✏️ Изменить", "🔕 Без напоминания", "🗑 Удалить"]
    parent = await e2e.item("Переезд")
    steps = await steps_of(e2e, parent.id)
    assert [s.title for s in steps] == ["Упаковать вещи", "Заказать машину", "Перевезти вещи", "Отдать ключи"]
    # only the step with a time has an occurrence and a reminder
    assert [len(await e2e.occurrences(s.id)) for s in steps] == [0, 1, 0, 0]
    assert [r.status for r in await e2e.reminders(steps[1].id)] == ["pending"]

    # /tasks: the timed step as a task of its own, the parent with its progress; untimed steps are not listed
    count = len(e2e.tg.sent)
    command(e2e, "/tasks")
    await e2e.wait(lambda: len(e2e.tg.sent) > count, "/tasks")
    tasks = e2e.tg.sent[-1].text
    assert "1. чт 8 окт 15:00 · 📌 Заказать машину (из «Переезд»)" in tasks
    assert "2. сб 10 окт 10:00 · 📌 Переезд (0/4)" in tasks
    assert "Упаковать" not in tasks and "Отдать ключи" not in tasks

    # tick a step on the card: the card is redrawn in place
    edit = await press(e2e, card, card.button(f"c:done:{steps[0].id}"))
    assert "☑ 1. Упаковать вещи" in edit.text and "📋 Шаги: 1/4" in edit.text
    assert labels(edit)[:4] == ["☑ 1", "☐ 2", "☐ 3", "☐ 4"]
    assert e2e.tg.toasts()[-1] == "☑ Упаковать вещи"

    # Thursday: the morning plan lists the timed step with its parent, its reminder names the parent too
    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 8, 9, 0))
    [plan] = e2e.tg.sent[sent_before:]
    assert "1. 15:00 · 📌 Заказать машину (из «Переезд»)" in plan.text
    await e2e.tick(local(2026, 10, 8, 15, 0))
    [reminder] = e2e.tg.sent[sent_before + 1 :]
    assert reminder.text.split("\n") == [
        "🔔 <b>Заказать машину</b>",
        "↳ шаг из «Переезд»",
    ]

    # done from the reminder: the reminder says so and the task's card follows
    [occ] = await e2e.occurrences(steps[1].id)
    e2e.clock.set(local(2026, 10, 8, 15, 1))
    cards_before = len(e2e.tg.edits_of(card.message_id))
    await press(e2e, reminder, reminder.button(f"o:done:{occ.id}"))
    redrawn = await e2e.wait(lambda: e2e.tg.edits_of(card.message_id)[cards_before:], "the card redrawn")
    assert "☑ 2. Заказать машину · сегодня 15:00" in redrawn[-1].text

    # the last two steps: the task closes itself, the card says so and 🎉 comes as a separate message
    await press(e2e, card, card.button(f"c:done:{steps[2].id}"))
    sent_before = len(e2e.tg.sent)
    edit = await press(e2e, card, card.button(f"c:done:{steps[3].id}"))
    [closed] = await e2e.wait(lambda: e2e.tg.sent[sent_before:], "the 🎉 message")
    assert closed.text == CLOSED
    assert edit.text.split("\n")[:3] == ["📌 <b>Переезд</b>", "⏰ сб 10 окт", "✅ сделано"]
    assert "📋 Шаги: 4/4" in edit.text
    assert labels(edit)[:5] == ["☑ 1", "☑ 2", "☑ 3", "☑ 4", "↩️ Не сделано"]
    parent = await e2e.item("Переезд")
    assert parent.status == "done"
    assert [r.status for r in await e2e.reminders(parent.id)] == ["cancelled"]

    # /tasks: nothing left
    count = len(e2e.tg.sent)
    command(e2e, "/tasks")
    await e2e.wait(lambda: len(e2e.tg.sent) > count, "/tasks")
    assert "Переезд" not in e2e.tg.sent[-1].text

    # unticking a step opens the task again (and its Saturday reminder), without another 🎉
    sent_before = len(e2e.tg.sent)
    edit = await press(e2e, card, edit.button(f"c:undo:{steps[3].id}"))
    assert "✅ сделано" not in edit.text and "☐ 4. Отдать ключи" in edit.text
    assert "🔔 сб 10 окт 10:00" in edit.text.split("\n")
    parent = await e2e.item("Переезд")
    assert parent.status == "open"
    assert [r.status for r in await e2e.reminders(parent.id)] == ["cancelled", "pending"]
    await e2e.idle()
    assert e2e.tg.sent[sent_before:] == []


async def test_split_button_turns_the_card_into_a_checklist(e2e: Any) -> None:
    e2e.ai.script(
        "сделать ремонт на кухне",
        {"intent": "save", "items": [{"kind": "task", "title": "Ремонт на кухне", "details": "до Нового года"}]},
    )
    e2e.ai.script_split(
        "Ремонт на кухне",
        {"steps": [{"title": "Выбрать плитку"}, {"title": "Найти мастера"}, {"title": "Купить материалы"}]},
    )
    card = await e2e.card_for(e2e.tg.push_message("сделать ремонт на кухне"))
    assert labels(card) == ["✏️ Изменить", "🔕 Без напоминания", "🗑 Удалить", "🪄 Разбить на шаги"]

    edit = await press(e2e, card, card.button("i:split:"))
    assert e2e.tg.toasts()[0] == "🪄 Разбиваю на шаги…"
    lines = edit.text.split("\n")
    assert lines[2:6] == ["📋 Шаги: 0/3", "☐ 1. Выбрать плитку", "☐ 2. Найти мастера", "☐ 3. Купить материалы"]
    assert "🪄 Разбить на шаги" not in labels(edit)
    [request] = e2e.ai.split_requests()
    assert request.task == {"kind": "task", "title": "Ремонт на кухне", "details": "до Нового года"} | {
        "due_local": "2026-10-06 19:00"
    }
    assert request.line("NOTE ") is None
    async with e2e.ctx.sessionmaker() as s:
        purposes = (await s.scalars(select(AiUsage.purpose).order_by(AiUsage.id))).all()
    assert list(purposes) == ["extract", "split"]

    # a second press on the old button: the item has steps already
    await press(e2e, card, card.button("i:split:"))
    assert e2e.tg.toasts()[-1] == "У задачи уже есть шаги"
    assert len(e2e.ai.split_requests()) == 1


async def test_split_by_a_reply_to_the_card(e2e: Any) -> None:
    e2e.ai.script("подготовиться к отпуску", {"intent": "save", "items": [{"kind": "task", "title": "Отпуск"}]})
    e2e.ai.script("разбей на шаги, начни с билетов", {"intent": "edit", "edit": {"action": "split"}})
    e2e.ai.script_split("Отпуск", {"steps": [{"title": "Купить билеты"}, {"title": "Забронировать отель"}]})
    card = await e2e.card_for(e2e.tg.push_message("подготовиться к отпуску"))
    await e2e.idle()  # the card is recorded with its item

    reply = e2e.tg.push_message("разбей на шаги, начни с билетов", reply_to=card)
    new_card = await e2e.card_for(reply)
    await e2e.reaction(reply, "👌")
    assert "☐ 1. Купить билеты" in new_card.text and "☐ 2. Забронировать отель" in new_card.text
    [request] = e2e.ai.split_requests()
    assert request.line("NOTE ") == "NOTE разбей на шаги, начни с билетов"
    assert (await e2e.raw(reply)).status == "done"


async def test_related_earlier_item_line_and_button(e2e: Any) -> None:
    e2e.ai.script(
        "купить наушники Sony", {"intent": "save", "items": [{"kind": "purchase", "title": "Купить наушники Sony"}]}
    )
    first = await e2e.card_for(e2e.tg.push_message("купить наушники Sony"))
    await e2e.idle()
    old = await e2e.item("Купить наушники Sony")
    assert e2e.ai.extraction_requests()[0].line("RELATED CANDIDATES") is None  # nothing earlier yet

    def related(req: Any) -> dict[str, Any]:
        candidates = json.loads(req.line("RELATED CANDIDATES ").removeprefix("RELATED CANDIDATES "))
        ids = [c["id"] for c in candidates] + [999999]  # an id it was not shown is dropped
        return {
            "intent": "save",
            "items": [{"kind": "purchase", "title": "Купить чехол для наушников Sony", "related": ids}],
        }

    e2e.clock.set(local(2026, 10, 7, 12, 0))
    e2e.ai.script("купить чехол для наушников Sony", related)
    card = await e2e.card_for(e2e.tg.push_message("купить чехол для наушников Sony"))
    line = e2e.ai.extraction_requests()[1].line("RELATED CANDIDATES ")
    shown = [{"id": old.id, "kind": "purchase", "title": "Купить наушники Sony", "date": "2026-10-06"}]
    assert line == "RELATED CANDIDATES " + json.dumps(shown, ensure_ascii=False, separators=(",", ":"))
    assert "🔗 похоже на: «Купить наушники Sony» (6 окт)" in card.text.split("\n")
    assert labels(card) == ["✏️ Изменить", "🔕 Без напоминания", "🗑 Удалить", "🔗 Похожее"]

    sent_before = len(e2e.tg.sent)
    e2e.tg.push_callback(card, card.button("i:rel:"))
    [related_card] = await e2e.wait(lambda: e2e.tg.sent[sent_before:], "the related card")
    assert related_card.text.startswith("🛒 <b>Купить наушники Sony</b>")
    assert related_card.reply_to == card.message_id
    # the earlier item's card names the new one as well (the link works both ways)
    assert "🔗 похоже на: «Купить чехол для наушников Sony» (7 окт)" in related_card.text
    assert first.text != related_card.text


async def test_related_only_between_open_items_about_the_same_thing(e2e: Any) -> None:
    """The owner's report: «Прогуглить про визу в Черногорию» came with "похоже на «Покушать»" (they share only
    "напомни" and "час") and the question "Напомнить в 17:00 или 05:00?"; the next «Покушать» was "похоже на" the done
    «Покушать». Now: no candidates from generic words or done items, the model's id is not trusted, no AM/PM question
    about "в 17"; an open item about the same thing is still linked."""
    eat_text = "напомни сегодня в 11 покушать"
    eat_spec = {"kind": "task", "title": "Покушать", "when": {"text": "сегодня в 11", "day": "+0", "time": "11:00"}}
    e2e.ai.script(eat_text, {"intent": "save", "items": [eat_spec]})
    await e2e.card_for(e2e.tg.push_message(eat_text))
    e2e.ai.script("купить наушники", {"intent": "save", "items": [{"kind": "purchase", "title": "Купить наушники"}]})
    await e2e.card_for(e2e.tg.push_message("купить наушники"))
    await e2e.idle()
    eat, headphones = await e2e.item("Покушать"), await e2e.item("Купить наушники")

    # 11:00 the reminder, [✅ Готово] at 11:01: «Покушать» is done
    sent_before = len(e2e.tg.sent)
    await e2e.tick(local(2026, 10, 6, 11, 0))
    [reminder] = [c for c in e2e.tg.sent[sent_before:] if c.text.startswith("🔔 ")]  # (and the morning digest)
    assert reminder.text.startswith("🔔 <b>Покушать</b>")
    e2e.clock.set(local(2026, 10, 6, 11, 1))
    e2e.tg.push_callback(reminder, reminder.button("o:done:"))
    await e2e.wait(lambda: e2e.tg.edits_of(reminder.message_id), "the reminder edited")
    assert (await e2e.item("Покушать")).status == "done"

    # the live message: what the model answered then (an AM/PM question about "в 17", a related id it was not shown)
    visa_text = "Напомни часов в 17 прогуглить про визу в черногорию"
    visa = {
        "kind": "task",
        "title": "Прогуглить про визу в Черногорию",
        "when": {"text": "в 17", "time": "17:00", "role": "event"},
        "conf": "high",
        "related": [eat.id, headphones.id],
    }
    ask = {"q": "Напомнить в 17:00 или 05:00?", "options": ["17:00", "05:00"]}
    e2e.ai.script(visa_text, {"intent": "save", "items": [visa], "ask": ask})
    message_id = e2e.tg.push_message(visa_text)
    card = await e2e.card_for(message_id)
    await e2e.reaction(message_id, "👌")
    await e2e.idle()
    assert e2e.ai.extraction_requests()[-1].line("RELATED CANDIDATES") is None  # "напомни", "часов": generic
    assert card.text.split("\n")[:2] == [
        "📌 <b>Прогуглить про визу в Черногорию</b>",
        "🔔 сегодня 17:00",
    ]
    assert "🔗" not in card.text and "⚠️" not in card.text
    assert labels(card) == ["✏️ Изменить", "🔕 Без напоминания", "🗑 Удалить", "🪄 Разбить на шаги"]
    assert e2e.tg.replies_to(message_id) == [card]  # no question
    assert "🤔" not in e2e.tg.reactions(message_id)
    assert (await e2e.raw(message_id)).status == "done"

    # «Покушать» again: the done one is no candidate, the model's id for it is dropped
    e2e.ai.script("покушать", {"intent": "save", "items": [{"kind": "task", "title": "Покушать", "related": [eat.id]}]})
    card = await e2e.card_for(e2e.tg.push_message("покушать"))
    await e2e.idle()
    assert e2e.ai.extraction_requests()[-1].line("RELATED CANDIDATES") is None
    assert "🔗" not in card.text

    # an open item about the same thing is still a candidate and linked
    def sony(req: Any) -> dict[str, Any]:
        candidates = json.loads(req.line("RELATED CANDIDATES ").removeprefix("RELATED CANDIDATES "))
        return {
            "intent": "save",
            "items": [{"kind": "purchase", "title": "Купить наушники Sony", "related": [c["id"] for c in candidates]}],
        }

    e2e.ai.script("купить наушники Sony", sony)
    card = await e2e.card_for(e2e.tg.push_message("купить наушники Sony"))
    shown = [{"id": headphones.id, "kind": "purchase", "title": "Купить наушники", "date": "2026-10-06"}]
    line = e2e.ai.extraction_requests()[-1].line("RELATED CANDIDATES ")
    assert line == "RELATED CANDIDATES " + json.dumps(shown, ensure_ascii=False, separators=(",", ":"))
    assert "🔗 похоже на: «Купить наушники» (6 окт)" in card.text.split("\n")
    async with e2e.ctx.sessionmaker() as s:
        links = (await s.execute(select(ItemLink.item_id, ItemLink.related_id))).all()
    assert [tuple(r) for r in links] == [((await e2e.item("Купить наушники Sony")).id, headphones.id)]
