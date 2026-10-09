"""[✅ N] / [↩️ N] under /today, /tasks and the digests (docs/features/iteration-3.md §2): the line closed or
reopened without the AI, the list re-rendered in place from its buttons with the same numbers; stale lines toast."""

from __future__ import annotations

from datetime import date, timedelta

from aiogram.types import CallbackQuery, Chat, InaccessibleMessage, Update, User
from sqlalchemy import select

from dumpbot import clock
from dumpbot.ai.schemas import EditPatch, Extraction, When
from dumpbot.bot import lists
from dumpbot.bot.cbdata import DigestCb, ListCb
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


# --- review fixes -------------------------------------------------------------------------------------------------


async def test_a_period_line_given_an_exact_time_keeps_its_number_and_button(feed, updates, seed, tg, frozen) -> None:
    bread = await seed.item("Купить хлеб", when=When(text="сегодня в 12", time="12:00"))
    pi = await seed.item("Посмотреть Raspberry Pi", when=When(text="на этой неделе", period="this_week"))
    view = await render(seed, "t")
    assert view[0].split("\n")[-1] == "2. 📌 Посмотреть Raspberry Pi"
    async with seed.sessionmaker() as s:  # "сделаю сегодня в 20": the span becomes an exact time
        await items.apply_patch(
            s, pi.item.id, EditPatch(when=When(text="сегодня в 20", day="+0", time="20:00")), Prefs(), frozen.now
        )
        await s.commit()
    assert (await item(seed, pi.item.id)).period_kind is None

    await press(feed, updates, view, "✅ 1")

    edit = tg.edits[0]
    assert tg.toasts == ["✅ Закрыл «Купить хлеб»"]
    assert edit.text.split("\n")[-1] == "2. сегодня 20:00 · 📌 Посмотреть Raspberry Pi"  # still there, not "\n\n"
    assert not edit.text.endswith("\n")
    assert labels(edit.reply_markup) == ["↩️ 1", "✅ 2"]

    tg.clear()
    await press(feed, updates, (edit.text, edit.reply_markup), "✅ 2")

    assert tg.toasts == ["✅ Закрыл «Посмотреть Raspberry Pi»"]
    assert tg.edits[0].text.split("\n")[-1] == "2. ✅ сегодня 20:00 · 📌 <s>Посмотреть Raspberry Pi</s>"
    assert labels(tg.edits[0].reply_markup) == ["↩️ 1", "↩️ 2"]  # the accidental ✅ can be taken back
    assert (await occ(seed, bread.occurrence.id)).status == "done"


async def test_tasks_series_line_pressed_a_day_later_closes_the_current_instance(
    feed, updates, seed, tg, frozen
) -> None:
    pills = await seed.item("Таблетки", when=When(text="каждый день в 12", time="12:00"), rrule="FREQ=DAILY")
    view = await render(seed, "k")  # Tue 10:00: "1. сегодня 12:00 · 📌 Таблетки 🔁", pinned to Tuesday's instance
    for hours in (2, 25):  # the scheduler's ticks: Tue 12:01 (Wednesday's instance), Wed 13:01 (Thursday's)
        frozen.advance(hours=hours, minutes=1 if hours == 2 else 0)
        async with seed.sessionmaker() as s:
            await items.ensure_recurring(s, Prefs(), frozen.now)
            await s.commit()
    async with seed.sessionmaker() as s:
        stmt = select(Occurrence).where(Occurrence.item_id == pills.item.id).order_by(Occurrence.due_at)
        tuesday, wednesday, thursday = (await s.scalars(stmt)).all()
    # Wed 13:01: Tuesday's instance was left missed, Wednesday's is the current one
    assert (tuesday.id, tuesday.status, wednesday.status, thursday.status) == (
        pills.occurrence.id,
        "missed",
        "open",
        "open",
    )

    await press(feed, updates, view, "✅ 1")

    assert (await occ(seed, tuesday.id)).status == "missed"  # yesterday stays as it was
    assert (await occ(seed, wednesday.id)).status == "done"
    assert tg.toasts == ["✅ Закрыл «Таблетки» · следующий раз завтра 12:00"]
    edit = tg.edits[0]
    assert edit.text.split("\n")[1] == "1. ✅ сегодня 12:00 · 📌 <s>Таблетки</s> 🔁"
    assert cb_of(edit.reply_markup, "↩️ 1").o == wednesday.id

    tg.clear()
    await press(feed, updates, (edit.text, edit.reply_markup), "↩️ 1")
    assert (await occ(seed, wednesday.id)).status == "open"


async def test_moving_the_evening_digest_keeps_the_numbers_and_the_undo(feed, updates, seed, tg, frozen) -> None:
    morning = await seed.item("Утро", when=When(text="сегодня в 11", time="11:00"))
    lunch = await seed.item("Обед", when=When(text="сегодня в 13", time="13:00"))
    await seed.item("Поздно", when=When(text="сегодня в 22", time="22:00"))
    frozen.advance(hours=11)  # Tue 21:00
    view = await render(seed, "e", date(2026, 10, 6))
    assert labels(view[1]) == ["✅ 1", "✅ 2", "✅ 3", "📅 Перенести невыполненное на завтра"]
    await press(feed, updates, view, "✅ 1")
    closed = tg.edits[0]
    tg.clear()

    move = DigestCb(a="tmr", d="20261006")
    await feed(updates.callback(move, text=closed.text, reply_markup=closed.reply_markup, date=frozen.now))

    assert (await occ(seed, morning.occurrence.id)).status == "done"
    assert (await occ(seed, lunch.occurrence.id)).due_at == lunch.occurrence.due_at + timedelta(days=1)
    assert tg.toasts == ["📅 Перенёс на завтра: 1"]
    [edit] = tg.edits
    lines_ = edit.text.split("\n")
    assert "1. ✅ 11:00 · 📌 <s>Утро</s>" in lines_  # its number and [↩️ 1] stay
    assert "3. 🕒 22:00 · 📌 Поздно" in lines_  # not renumbered
    assert "Обед" not in edit.text  # moved: gone from this day's summary
    assert lines_[-1] == "📅 Перенесено на завтра: 1"
    assert labels(edit.reply_markup) == ["↩️ 1", "✅ 3"]

    tg.clear()
    await press(feed, updates, (edit.text, edit.reply_markup), "↩️ 1")
    assert (await occ(seed, morning.occurrence.id)).status == "open"


async def test_rows_added_after_the_list_was_sent_get_no_first_20_hint(feed, updates, seed, tg) -> None:
    await today_items(seed)
    view = await render(seed, "t")
    await seed.item("Новое", when=When(text="сегодня в 18", time="18:00"))

    await press(feed, updates, view, "✅ 1")

    text = tg.edits[0].text
    assert "• сегодня 18:00 · 📌 Новое" in text  # shown, without a button
    assert "Кнопки" not in text  # the list had 2 lines: no "first 20" to speak of
    assert labels(tg.edits[0].reply_markup) == ["↩️ 1", "✅ 2"]


async def test_lines_past_the_first_20_keep_the_hint_after_a_press(feed, updates, seed, tg) -> None:
    for n in range(21):
        await seed.item(f"Дело {n:02d}", when=When(text="сегодня в 18", time=f"18:{n:02d}"))
    view = await render(seed, "t")
    assert "Кнопки — у первых 20 дел" in view[0]

    await press(feed, updates, view, "✅ 1")

    assert "Кнопки — у первых 20 дел" in tg.edits[0].text


async def test_undo_of_a_step_closed_by_its_parent_takes_the_whole_close_back(feed, updates, seed, tg) -> None:
    spec = {
        "kind": "task",
        "title": "Переезд",
        "when": {"text": "сегодня в 18", "time": "18:00"},
        "steps": [{"title": "Упаковать", "when": {"text": "в 12", "time": "12:00"}}, {"title": "Ключи"}],
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
        pack, keys = (await s.scalars(select(Item).where(Item.parent_id == parent.item.id).order_by(Item.id))).all()
    view = await render(seed, "t")
    assert [line for line in view[0].split("\n") if line[:2] in ("1.", "2.")] == [
        "1. сегодня 12:00 · 📌 Упаковать (из «Переезд»)",
        "2. сегодня 18:00 · 📌 Переезд (0/2)",
    ]
    await press(feed, updates, view, "✅ 2")
    assert tg.toasts == ["✅ Закрыл «Переезд» и 2 шага"]
    edit = tg.edits[0]
    assert labels(edit.reply_markup) == ["↩️ 1", "↩️ 2"]
    tg.clear()

    await press(feed, updates, (edit.text, edit.reply_markup), "↩️ 1")  # the ✅ 2 was a mistake

    assert [(await item(seed, i)).status for i in (parent.item.id, pack.id, keys.id)] == ["open", "open", "open"]
    assert tg.toasts == ["↩️ Снова открыто: «Переезд» и 2 шага"]
    assert labels(tg.edits[0].reply_markup) == ["✅ 1", "✅ 2"]
    assert "2. сегодня 18:00 · 📌 Переезд (0/2)" in tg.edits[0].text


async def test_undo_of_a_step_ticked_on_its_own_reopens_only_it(feed, updates, seed, tg, frozen) -> None:
    spec = {
        "kind": "task",
        "title": "Переезд",
        "when": {"text": "сегодня в 18", "time": "18:00"},
        "steps": [{"title": "Упаковать", "when": {"text": "в 12", "time": "12:00"}}, {"title": "Ключи"}],
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
        pack, keys = (await s.scalars(select(Item).where(Item.parent_id == parent.item.id).order_by(Item.id))).all()
        await items.toggle_step(s, keys.id, done=True, prefs=Prefs(), now=clock.utcnow())
        await s.commit()
    view = await render(seed, "t")
    frozen.advance(minutes=5)
    await press(feed, updates, view, "✅ 1")  # the last open step: the parent closes with it
    assert (await item(seed, parent.item.id)).status == "done"
    edit = tg.edits[0]
    tg.clear()

    await press(feed, updates, (edit.text, edit.reply_markup), "↩️ 1")

    assert tg.toasts == ["↩️ Снова открыто: «Упаковать»"]
    assert [(await item(seed, i)).status for i in (parent.item.id, pack.id, keys.id)] == ["open", "open", "done"]
