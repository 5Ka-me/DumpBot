"""[✅ N] / [↩️ N] under /today, /tasks and the digests (docs/features/iteration-3.md §2): the line closed or
reopened without the AI, the list re-rendered in place from its buttons with the same numbers; stale lines toast."""

from __future__ import annotations

from datetime import date, timedelta

from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, Update, User
from sqlalchemy import select

from dumpbot import clock
from dumpbot.ai.schemas import Extraction, When
from dumpbot.bot import lists
from dumpbot.bot.cbdata import ListCb
from dumpbot.db.models import Item, Occurrence
from dumpbot.domain.prefs import Prefs
from dumpbot.services import items

OWNER = 4242


async def render(seed, kind: str, day: date | None = None):
    async with seed.sessionmaker() as s:
        view = await lists.render(s, kind, day, Prefs(), clock.utcnow())
        await s.commit()
    return view


async def occ(seed, occ_id: int) -> Occurrence:
    async with seed.sessionmaker() as s:
        return await s.get(Occurrence, occ_id)


async def item(seed, item_id: int) -> Item:
    async with seed.sessionmaker() as s:
        return await s.get(Item, item_id)


def cb_of(kb, label: str) -> ListCb:
    """The ListCb of the button labelled ``label`` ("✅ 2")."""
    for row in kb.inline_keyboard:
        for b in row:
            if b.text == label:
                return ListCb.unpack(b.callback_data)
    raise AssertionError(f"no button {label!r}")


async def press(feed, updates, view, label: str, *, message_id: int = 900):
    text, kb = view
    await feed(updates.callback(cb_of(kb, label), text=text, reply_markup=kb, message_id=message_id))


def labels(kb) -> list[str]:
    return [b.text for row in kb.inline_keyboard for b in row] if kb is not None else []


async def today_items(seed):
    bread = await seed.item("Купить хлеб", when=When(text="сегодня в 12", time="12:00"))
    call = await seed.item("Позвонить", when=When(text="сегодня в 15", time="15:00"))
    return bread, call


async def test_done_strikes_the_line_through_and_undo_brings_it_back(feed, updates, seed, tg, ctx) -> None:
    bread, call = await today_items(seed)
    view = await render(seed, "t")
    assert labels(view[1]) == ["✅ 1", "✅ 2"]

    await press(feed, updates, view, "✅ 2")

    assert (await occ(seed, call.occurrence.id)).status == "done"
    assert (await item(seed, call.item.id)).status == "done"
    assert (await occ(seed, bread.occurrence.id)).status == "open"
    assert tg.toasts == ["✅ Закрыл «Позвонить»"]
    [edit] = tg.edits
    assert edit.message_id == 900
    assert edit.text.split("\n") == [
        "<b>📅 Сегодня, вт 6 окт</b>",
        "1. сегодня 12:00 · 📌 Купить хлеб",
        "2. ✅ сегодня 15:00 · 📌 <s>Позвонить</s>",  # in place, not moved to a "✅ Сделано" section
    ]
    assert labels(edit.reply_markup) == ["✅ 1", "↩️ 2"]
    assert cb_of(edit.reply_markup, "↩️ 2") == ListCb(
        a="undo", k="t", d="20261006", s="o", n=2, o=call.occurrence.id, i=call.item.id
    )
    assert ctx.scheduler_wakeup.is_set()
    async with seed.sessionmaker() as s:
        assert await items.pending_reminders(s, call.occurrence.id) == []

    tg.clear()
    ctx.scheduler_wakeup.clear()
    await press(feed, updates, (edit.text, edit.reply_markup), "↩️ 2")

    assert (await occ(seed, call.occurrence.id)).status == "open"
    assert (await item(seed, call.item.id)).status == "open"
    async with seed.sessionmaker() as s:
        [reminder] = await items.pending_reminders(s, call.occurrence.id)  # re-planned
    assert reminder.fire_at == call.occurrence.due_at
    assert tg.toasts == ["↩️ Снова открыто: «Позвонить»"]
    assert tg.edits[0].text.split("\n")[2] == "2. сегодня 15:00 · 📌 Позвонить"
    assert labels(tg.edits[0].reply_markup) == ["✅ 1", "✅ 2"]
    assert ctx.scheduler_wakeup.is_set()


async def test_a_line_closed_elsewhere_only_toasts(feed, updates, seed, tg, frozen) -> None:
    _, call = await today_items(seed)
    view = await render(seed, "t")
    async with seed.sessionmaker() as s:  # "Готово" in the reminder meanwhile
        await items.mark_done(s, call.occurrence.id, frozen.now, Prefs())
        await s.commit()

    await press(feed, updates, view, "✅ 2")

    assert tg.toasts == ["Уже сделано: «Позвонить»"]
    assert "2. ✅ сегодня 15:00 · 📌 <s>Позвонить</s>" in tg.edits[0].text
    assert labels(tg.edits[0].reply_markup) == ["✅ 1", "↩️ 2"]

    tg.clear()
    await press(feed, updates, view, "✅ 1")  # the other line of the same old message still works
    assert tg.toasts == ["✅ Закрыл «Купить хлеб»"]
    edit = tg.edits[0]
    assert labels(edit.reply_markup) == ["↩️ 1", "↩️ 2"]  # every line as it is now

    tg.clear()  # [↩️ 2] after "↩️ Не сделано" in the reminder
    async with seed.sessionmaker() as s:
        await items.mark_undone(s, call.occurrence.id, prefs=Prefs(), now=frozen.now)
        await s.commit()
    await press(feed, updates, (edit.text, edit.reply_markup), "↩️ 2")
    assert tg.toasts == ["Уже открыто: «Позвонить»"]
    assert labels(tg.edits[0].reply_markup) == ["↩️ 1", "✅ 2"]


async def test_a_deleted_line_disappears_and_the_numbers_stay(feed, updates, seed, tg, frozen) -> None:
    _, call = await today_items(seed)
    later = await seed.item("Спортзал", when=When(text="сегодня в 18", time="18:00"))
    view = await render(seed, "t")
    async with seed.sessionmaker() as s:
        await items.delete_item(s, call.item.id, frozen.now)
        await s.commit()

    await press(feed, updates, view, "✅ 2")

    assert tg.toasts == ["🗑 Запись удалена"]
    [edit] = tg.edits
    assert "Позвонить" not in edit.text
    assert edit.text.split("\n")[1:] == ["1. сегодня 12:00 · 📌 Купить хлеб", "3. сегодня 18:00 · 📌 Спортзал"]
    assert labels(edit.reply_markup) == ["✅ 1", "✅ 3"]
    assert (await occ(seed, later.occurrence.id)).status == "open"


async def test_series_closes_the_current_instance(feed, updates, seed, tg) -> None:
    pills = await seed.item("Таблетки", when=When(text="каждый день в 12", time="12:00"), rrule="FREQ=DAILY")
    view = await render(seed, "t")

    await press(feed, updates, view, "✅ 1")

    assert (await occ(seed, pills.occurrence.id)).status == "done"
    assert (await item(seed, pills.item.id)).status == "open"
    async with seed.sessionmaker() as s:
        stmt = select(Occurrence).where(Occurrence.item_id == pills.item.id, Occurrence.status == "open")
        [nxt] = (await s.scalars(stmt)).all()
    assert nxt.due_at == pills.occurrence.due_at + timedelta(days=1)
    assert tg.toasts == ["✅ Закрыл «Таблетки» · следующий раз завтра 12:00"]
    assert "1. ✅ сегодня 12:00 · 📌 <s>Таблетки</s> 🔁" in tg.edits[0].text


async def test_tasks_series_line_does_not_repeat_its_next_instance(feed, updates, seed, tg) -> None:
    await seed.item("Таблетки", when=When(text="каждый день в 12", time="12:00"), rrule="FREQ=DAILY")
    other = await seed.item("Отчёт")
    view = await render(seed, "k")
    assert view[0].split("\n")[1:] == ["1. сегодня 12:00 · 📌 Таблетки 🔁", "2. завтра 13:00 · 📌 Отчёт"]

    await press(feed, updates, view, "✅ 1")

    [edit] = tg.edits
    assert edit.text.split("\n") == [
        "<b>📋 Открытые задачи</b>",
        "1. ✅ сегодня 12:00 · 📌 <s>Таблетки</s> 🔁",
        "2. завтра 13:00 · 📌 Отчёт",
    ]
    assert labels(edit.reply_markup) == ["↩️ 1", "✅ 2"]
    assert (await item(seed, other.item.id)).status == "open"


async def test_parent_closes_with_its_open_steps_and_undo_reopens_them(feed, updates, seed, tg) -> None:
    spec = {
        "kind": "task",
        "title": "Переезд",
        "when": {"text": "в субботу", "day": "SA"},
        "steps": [{"title": "Упаковать вещи"}, {"title": "Отдать ключи"}],
    }
    async with seed.sessionmaker() as s:
        [parent] = await items.create_items(
            s,
            raw=None,
            extraction=Extraction.model_validate({"items": [spec]}),
            source="text",
            source_text=None,
            source_meta=None,
            urls=[],
            prefs=Prefs(),
            now=clock.utcnow(),
            model=None,
            prompt_version=None,
        )
        await s.commit()
        steps = list((await s.scalars(select(Item).where(Item.parent_id == parent.item.id))).all())
    view = await render(seed, "k")
    assert view[0].split("\n")[1] == "1. сб 10 окт 10:00 · 📌 Переезд (0/2)"

    await press(feed, updates, view, "✅ 1")

    assert (await item(seed, parent.item.id)).status == "done"
    assert [(await item(seed, step.id)).status for step in steps] == ["done", "done"]
    assert tg.toasts == ["✅ Закрыл «Переезд» и 2 шага"]
    assert tg.edits[0].text.split("\n")[1] == "1. ✅ сб 10 окт 10:00 · 📌 <s>Переезд</s> (2/2)"

    edit = tg.edits[0]
    tg.clear()
    await press(feed, updates, (edit.text, edit.reply_markup), "↩️ 1")

    assert (await item(seed, parent.item.id)).status == "open"
    assert [(await item(seed, step.id)).status for step in steps] == ["open", "open"]
    assert tg.edits[0].text.split("\n")[1] == "1. сб 10 окт 10:00 · 📌 Переезд (0/2)"


async def test_evening_digest_pressed_the_next_morning_keeps_its_day(feed, updates, seed, tg, frozen) -> None:
    tuesday = await seed.item("Задача вторника", when=When(text="сегодня в 15", time="15:00"))
    wednesday = await seed.item("Встреча", when=When(text="завтра в 11", day="+1", time="11:00"))
    frozen.advance(hours=11)  # Tue 21:00
    view = await render(seed, "e", date(2026, 10, 6))
    assert labels(view[1]) == ["✅ 1", "📅 Перенести невыполненное на завтра"]
    assert cb_of(view[1], "✅ 1").d == "20261006"
    frozen.advance(hours=11)  # Wed 08:00

    await press(feed, updates, view, "✅ 1")

    assert (await occ(seed, tuesday.occurrence.id)).status == "done"
    assert (await occ(seed, wednesday.occurrence.id)).status == "open"
    [edit] = tg.edits
    lines_ = edit.text.split("\n")
    assert lines_[:2] == ["🌙 <b>Итоги дня</b>, вт 6 окт", "Всё сделано 🎉"]
    assert "1. ✅ 15:00 · 📌 <s>Задача вторника</s>" in lines_ and "Встреча" not in edit.text
    assert labels(edit.reply_markup) == ["↩️ 1"]  # nothing left to move


async def test_morning_digest_numbers_overdue_too(feed, updates, seed, tg, frozen) -> None:
    async with seed.sessionmaker() as s:  # an overdue task of yesterday
        created = await items.create_items(
            s,
            raw=None,
            extraction=Extraction.model_validate(
                {"items": [{"kind": "task", "title": "Вчерашнее", "when": {"time": "18:00"}}]}
            ),
            source="text",
            source_text=None,
            source_meta=None,
            urls=[],
            prefs=Prefs(),
            now=frozen.now - timedelta(days=1),
            model=None,
            prompt_version=None,
        )
        await s.commit()
    today = await seed.item("Сегодняшнее", when=When(text="сегодня в 12", time="12:00"))
    view = await render(seed, "m", date(2026, 10, 6))
    assert labels(view[1]) == ["✅ 1", "✅ 2"]

    await press(feed, updates, view, "✅ 2")

    assert (await occ(seed, created[0].occurrence.id)).status == "done"
    assert "2. ✅ вчера 18:00 · 📌 <s>Вчерашнее</s>" in tg.edits[0].text
    assert (await occ(seed, today.occurrence.id)).status == "open"


async def test_an_inaccessible_message_gets_a_fresh_list(feed, updates, seed, tg) -> None:
    _, call = await today_items(seed)
    data = ListCb(a="done", k="t", d="20261006", s="o", n=2, o=call.occurrence.id, i=call.item.id).pack()
    query = CallbackQuery(
        id="cbx",
        from_user=User(id=OWNER, is_bot=False, first_name="Дима"),
        chat_instance="ci",
        data=data,
        message=InaccessibleMessage(chat=Chat(id=OWNER, type="private"), message_id=900, date=0),
    )

    await feed(Update(update_id=999, callback_query=query))

    assert (await occ(seed, call.occurrence.id)).status == "done"
    [edit] = tg.edits
    assert edit.text.split("\n") == [
        "<b>📅 Сегодня, вт 6 окт</b>",
        "1. сегодня 12:00 · 📌 Купить хлеб",
        "",
        "<b>✅ Сделано</b>",
        "• сегодня 15:00 · 📌 <s>Позвонить</s>",
    ]
    assert labels(edit.reply_markup) == ["✅ 1"]


async def test_unknown_list_buttons_are_stale(feed, updates, seed, tg) -> None:
    bread, _ = await today_items(seed)
    await feed(updates.callback(ListCb(a="flip", k="t", d="", s="o", n=1, o=0, i=bread.item.id)))
    await feed(updates.callback(ListCb(a="done", k="x", d="", s="o", n=1, o=0, i=bread.item.id)))
    await feed(updates.callback(ListCb(a="done", k="t", d="", s="o", n=1, o=0, i=10**9)))
    assert tg.toasts == ["Кнопка устарела", "Кнопка устарела", "Запись не найдена"]
    assert (await occ(seed, bread.occurrence.id)).status == "open"


async def test_a_line_without_an_occurrence_closes_the_item(feed, updates, seed, tg, frozen) -> None:
    note = await seed.item("Без времени", timed=False)
    async with seed.sessionmaker() as s:  # 🔕 and no time at all: the line is the item itself
        for o in (await s.scalars(select(Occurrence).where(Occurrence.item_id == note.item.id))).all():
            o.status = "cancelled"
        await s.commit()
    view = await render(seed, "k")
    assert cb_of(view[1], "✅ 1").o == 0

    await press(feed, updates, view, "✅ 1")

    assert (await item(seed, note.item.id)).status == "done"
    assert tg.edits[0].text.split("\n")[1] == "1. ✅ 📌 <s>Без времени</s>"
    edit = tg.edits[0]
    tg.clear()
    await press(feed, updates, (edit.text, edit.reply_markup), "↩️ 1")
    assert (await item(seed, note.item.id)).status == "open"
