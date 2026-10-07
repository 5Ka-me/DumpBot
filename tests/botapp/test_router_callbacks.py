"""Callbacks router: card / reminder / question / candidate / digest buttons, edited in place with a toast."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from dumpbot.ai.schemas import When
from dumpbot.bot import views
from dumpbot.bot.cbdata import AskCb, DigestCb, ItemCb, KindCb, LeadCb, OccCb, PickCb
from dumpbot.bot.keyboards import button, markup
from dumpbot.db.models import Item, Occurrence, Reminder
from dumpbot.domain.prefs import Prefs
from dumpbot.services import digest, items


async def _item(seed, item_id: int) -> Item:
    async with seed.sessionmaker() as s:
        return await s.get(Item, item_id)


async def _occ(seed, occ_id: int) -> Occurrence:
    async with seed.sessionmaker() as s:
        return await s.get(Occurrence, occ_id)


async def _pending(seed, occ_id: int) -> list[Reminder]:
    async with seed.sessionmaker() as s:
        return await items.pending_reminders(s, occ_id)


async def _occurrences(seed, item_id: int) -> list[Occurrence]:
    async with seed.sessionmaker() as s:
        stmt = select(Occurrence).where(Occurrence.item_id == item_id).order_by(Occurrence.due_at)
        return list((await s.scalars(stmt)).all())


# --- done / undone / snooze --------------------------------------------------------------------------------------


async def test_done_on_a_card(feed, updates, seed, tg, ctx, get_buttons) -> None:
    created = await seed.item()
    await seed.bot_message(900, purpose="card", item_id=created.item.id, occurrence_id=created.occurrence.id)

    await feed(updates.callback(OccCb(a="done", id=created.occurrence.id), message_id=900))

    occ = await _occ(seed, created.occurrence.id)
    assert occ.status == "done"
    assert (await _item(seed, created.item.id)).status == "done"
    assert await _pending(seed, occ.id) == []
    assert tg.toasts == ["✅ Готово"]
    [edit] = tg.edits
    assert edit.message_id == 900
    assert "✅ сделано" in edit.text
    assert ("↩️ Не сделано", OccCb(a="undone", id=occ.id).pack()) in get_buttons(edit.reply_markup)
    assert ctx.scheduler_wakeup.is_set()


async def test_done_on_a_reminder_renders_the_reminder(feed, updates, seed, tg) -> None:
    created = await seed.item()
    await seed.bot_message(901, purpose="reminder", item_id=created.item.id, occurrence_id=created.occurrence.id)

    await feed(updates.callback(OccCb(a="done", id=created.occurrence.id), message_id=901))

    [edit] = tg.edits
    assert edit.text.startswith("✅ <s>Купить наушники</s>")


async def test_done_on_a_series_reports_the_next_time(feed, updates, seed, tg) -> None:
    created = await seed.item("Зарядка", when=When(text="каждый день в 9", time="09:00"), rrule="FREQ=DAILY")

    await feed(updates.callback(OccCb(a="done", id=created.occurrence.id)))

    [toast] = tg.toasts
    assert toast.startswith("✅ Готово · следующий раз")
    occs = await _occurrences(seed, created.item.id)
    assert [o.status for o in occs] == ["done", "open"]
    assert (await _item(seed, created.item.id)).status == "open"


async def test_undone_reopens_and_replans(feed, updates, seed, tg, frozen) -> None:
    created = await seed.item()
    async with seed.sessionmaker() as s:
        await items.mark_done(s, created.occurrence.id, frozen.now, Prefs())
        await s.commit()

    await feed(updates.callback(OccCb(a="undone", id=created.occurrence.id)))

    assert (await _occ(seed, created.occurrence.id)).status == "open"
    assert len(await _pending(seed, created.occurrence.id)) == 1
    assert tg.toasts == ["↩️ Не сделано"]
    assert "Купить наушники" in tg.edits[0].text


async def test_undone_on_a_reminder_shows_the_reminder_again(feed, updates, seed, tg, frozen, get_buttons) -> None:
    created = await seed.item()
    await seed.bot_message(902, purpose="reminder", item_id=created.item.id, occurrence_id=created.occurrence.id)
    async with seed.sessionmaker() as s:
        await items.mark_done(s, created.occurrence.id, frozen.now, Prefs())
        await s.commit()

    await feed(updates.callback(OccCb(a="undone", id=created.occurrence.id), message_id=902))

    [edit] = tg.edits
    assert edit.text.startswith("🔔 <b>Купить наушники</b>")
    assert ("⏰ 15 мин", OccCb(a="s15", id=created.occurrence.id).pack()) in get_buttons(edit.reply_markup)


@pytest.mark.parametrize(("action", "delta"), [("s15", timedelta(minutes=15)), ("s60", timedelta(hours=1))])
async def test_snooze_minutes(feed, updates, seed, tg, frozen, ctx, action, delta) -> None:
    created = await seed.item()  # tomorrow 13:00, reminder tomorrow 12:55

    await feed(updates.callback(OccCb(a=action, id=created.occurrence.id)))

    snoozed, planned = await _pending(seed, created.occurrence.id)
    assert snoozed.fire_at == frozen.now + delta
    assert planned.fire_at == created.reminders[0].fire_at  # a later reminder is not replaced by the snooze
    assert tg.toasts[0].startswith("⏰ Напомню сегодня")
    assert "💤 отложено" in tg.edits[0].text
    assert tg.edits[0].reply_markup is None  # no [✅ Готово]: the snoozed reminder comes with its own buttons
    assert ctx.scheduler_wakeup.is_set()


async def test_snooze_to_tomorrow(feed, updates, seed, tg, frozen) -> None:
    created = await seed.item(when=When(text="сегодня в 15", time="15:00"))

    await feed(updates.callback(OccCb(a="tmr", id=created.occurrence.id)))

    [reminder] = await _pending(seed, created.occurrence.id)
    assert reminder.fire_at == created.occurrence.due_at + timedelta(days=1)
    assert tg.toasts[0].startswith("⏰ Напомню завтра")


async def test_snooze_to_tomorrow_of_a_daily_instance_merges_into_tomorrows(feed, updates, seed, tg, frozen) -> None:
    created = await seed.item("Таблетки", when=When(text="каждый день в 11", time="11:00"), rrule="FREQ=DAILY")
    frozen.advance(hours=1, minutes=1)  # Tue 11:01: the reminder was sent, Wednesday's instance exists
    async with seed.sessionmaker() as s:
        await items.ensure_recurring(s, Prefs(), frozen.now)
        await s.commit()
    await seed.bot_message(903, purpose="reminder", item_id=created.item.id, occurrence_id=created.occurrence.id)

    await feed(updates.callback(OccCb(a="tmr", id=created.occurrence.id), message_id=903))

    tuesday, wednesday = await _occurrences(seed, created.item.id)
    assert (tuesday.status, wednesday.status) == ("cancelled", "open")
    [reminder] = await _pending(seed, wednesday.id)
    assert reminder.fire_at == wednesday.due_at
    # the snoozed message has no buttons: Wednesday's reminder comes with its own [✅ Готово] for Wednesday
    [edit] = tg.edits
    assert edit.text == "🔔 <b>Таблетки</b>\n💤 отложено · напомню завтра в 11:00"
    assert edit.reply_markup is None


async def test_snooze_of_a_done_occurrence_does_not_add_reminders(feed, updates, seed, tg, frozen) -> None:
    created = await seed.item()
    async with seed.sessionmaker() as s:
        await items.mark_done(s, created.occurrence.id, frozen.now, Prefs())
        await s.commit()

    await feed(updates.callback(OccCb(a="s15", id=created.occurrence.id)))

    assert await _pending(seed, created.occurrence.id) == []
    assert tg.toasts == ["Уже сделано"]


async def test_unknown_occurrence(feed, updates, seed, tg) -> None:
    await feed(updates.callback(OccCb(a="done", id=99999)))

    assert tg.toasts == ["Запись не найдена"]
    assert tg.edits == []


async def test_unknown_occ_action_is_stale(feed, updates, tg) -> None:
    await feed(updates.callback(OccCb(a="zzz", id=1)))
    assert tg.toasts == ["Кнопка устарела"]


# --- item card actions -------------------------------------------------------------------------------------------


async def test_delete_and_restore(feed, updates, seed, tg, ctx, get_buttons) -> None:
    created = await seed.item()
    item_id = created.item.id

    await feed(updates.callback(ItemCb(a="del", id=item_id)))

    item = await _item(seed, item_id)
    assert item.status == "deleted"
    assert await _pending(seed, created.occurrence.id) == []
    assert tg.edits[-1].text.startswith("🗑 Удалено")
    assert get_buttons(tg.edits[-1].reply_markup) == [("↩️ Вернуть", ItemCb(a="undel", id=item_id).pack())]
    assert tg.toasts == ["🗑 Удалено"]

    await feed(updates.callback(ItemCb(a="undel", id=item_id)))

    assert (await _item(seed, item_id)).status == "open"
    assert len(await _pending(seed, created.occurrence.id)) == 1
    assert tg.toasts[-1] == "↩️ Восстановлено"
    assert "Купить наушники" in tg.edits[-1].text and "Удалено" not in tg.edits[-1].text


async def test_buttons_of_a_deleted_item_show_it_deleted(feed, updates, seed, tg, frozen) -> None:
    created = await seed.item()
    async with seed.sessionmaker() as s:
        await items.delete_item(s, created.item.id, frozen.now)
        await s.commit()

    await feed(updates.callback(OccCb(a="done", id=created.occurrence.id)))
    await feed(updates.callback(ItemCb(a="norem", id=created.item.id)))

    assert tg.toasts == ["🗑 Запись удалена", "🗑 Запись удалена"]
    assert all(e.text.startswith("🗑 Удалено") for e in tg.edits)
    assert (await _occ(seed, created.occurrence.id)).status == "open"


async def test_no_reminders(feed, updates, seed, tg, get_buttons) -> None:
    created = await seed.item()

    await feed(updates.callback(ItemCb(a="norem", id=created.item.id)))

    assert await _pending(seed, created.occurrence.id) == []
    assert (await _item(seed, created.item.id)).lead_minutes == []
    assert tg.toasts == ["🔕 Напоминания выключены"]
    labels = [text for text, _ in get_buttons(tg.edits[0].reply_markup)]
    assert "🔕 Без напоминания" not in labels


async def test_stop_recurrence_on_a_card(feed, updates, seed, tg) -> None:
    created = await seed.item("Зарядка", when=When(text="каждый день в 9", time="09:00"), rrule="FREQ=DAILY")
    await seed.bot_message(910, purpose="card", item_id=created.item.id, occurrence_id=created.occurrence.id)

    await feed(updates.callback(ItemCb(a="stoprec", id=created.item.id), message_id=910))

    assert (await _item(seed, created.item.id)).rec_active is False
    assert tg.toasts == ["⏹ Повтор остановлен"]
    assert "повтор остановлен" in tg.edits[0].text


async def test_stop_recurrence_from_the_list_rerenders_the_list(feed, updates, seed, tg, get_buttons) -> None:
    first = await seed.item("Зарядка", when=When(text="каждый день в 9", time="09:00"), rrule="FREQ=DAILY")
    second = await seed.item("Планёрка", when=When(text="каждый пн в 10", time="10:00"), rrule="FREQ=WEEKLY;BYDAY=MO")
    await seed.bot_message(911, purpose="recurring")

    await feed(updates.callback(ItemCb(a="stoprec", id=first.item.id), message_id=911))

    [edit] = tg.edits
    assert "Повторяющиеся" in edit.text and "Планёрка" in edit.text and "Зарядка" not in edit.text
    assert get_buttons(edit.reply_markup) == [("⏹ 1. Планёрка", ItemCb(a="stoprec", id=second.item.id).pack())]


async def test_edit_kind_and_lead_menus_and_back(feed, updates, seed, tg, get_buttons) -> None:
    created = await seed.item()
    item_id = created.item.id

    await feed(updates.callback(ItemCb(a="edit", id=item_id)))
    await feed(updates.callback(ItemCb(a="kind", id=item_id)))
    await feed(updates.callback(ItemCb(a="lead", id=item_id)))
    await feed(updates.callback(ItemCb(a="back", id=item_id)))

    edit_menu, kind_menu, lead_menu, card = tg.edits
    assert "Что изменить?" in edit_menu.text
    assert ("🕒 Время", ItemCb(a="time", id=item_id).pack()) in get_buttons(edit_menu.reply_markup)
    assert len([b for b in get_buttons(kind_menu.reply_markup) if b[1].startswith("k:")]) == 9
    assert ("15 мин", LeadCb(id=item_id, m=15).pack()) in get_buttons(lead_menu.reply_markup)
    assert card.text.startswith("📌 <b>Купить наушники</b>")
    assert tg.toasts == [None, None, None, None]


async def test_kind_change_to_actionable_adds_the_default_reminder(feed, updates, seed, tg, ctx) -> None:
    created = await seed.item("Идея: сходить в музей", kind="idea", timed=False)
    assert created.occurrence is None

    await feed(updates.callback(KindCb(id=created.item.id, k="task")))

    item = await _item(seed, created.item.id)
    assert item.kind == "task"
    [occ] = await _occurrences(seed, item.id)
    assert occ.precision == "default"
    assert len(await _pending(seed, occ.id)) == 1
    assert tg.toasts == ["🏷 Задача"]
    assert tg.edits[0].text.startswith("📌")
    assert ctx.scheduler_wakeup.is_set()


async def test_kind_change_of_a_task_without_time_to_idea_drops_its_19_reminder(feed, updates, seed, tg) -> None:
    created = await seed.item("Открыть кофейню", timed=False)  # task without a time: today 19:00
    assert created.occurrence.precision == "default"

    await feed(updates.callback(KindCb(id=created.item.id, k="idea")))

    assert await _pending(seed, created.occurrence.id) == []
    assert (await _occ(seed, created.occurrence.id)).status == "cancelled"
    [card] = tg.edits
    assert card.text.startswith("💡 <b>Открыть кофейню</b>")
    assert "⏰" not in card.text and "19:00" not in card.text


async def test_unknown_kind_is_stale(feed, updates, seed, tg) -> None:
    created = await seed.item()
    await feed(updates.callback(KindCb(id=created.item.id, k="banana")))
    assert tg.toasts == ["Кнопка устарела"]
    assert (await _item(seed, created.item.id)).kind == "task"


async def test_lead_replans_reminders(feed, updates, seed, tg) -> None:
    created = await seed.item()

    await feed(updates.callback(LeadCb(id=created.item.id, m=30)))

    [reminder] = await _pending(seed, created.occurrence.id)
    assert reminder.fire_at == created.occurrence.due_at - timedelta(minutes=30)
    assert (await _item(seed, created.item.id)).lead_minutes == [30]
    assert tg.toasts == ["⏱ Напомню за 30 мин"]


async def test_lead_zero(feed, updates, seed, tg) -> None:
    created = await seed.item()
    await feed(updates.callback(LeadCb(id=created.item.id, m=0)))
    [reminder] = await _pending(seed, created.occurrence.id)
    assert reminder.fire_at == created.occurrence.due_at
    assert tg.toasts == ["⏱ Напомню в момент события"]


async def test_lead_on_an_item_without_an_exact_time_tells_the_real_reminder(feed, updates, seed, tg) -> None:
    created = await seed.item("Позвонить маме", when=When(text="завтра", day="+1"))  # bare date: 10:00, auto

    await feed(updates.callback(LeadCb(id=created.item.id, m=30)))

    [reminder] = await _pending(seed, created.occurrence.id)
    assert reminder.fire_at == created.occurrence.due_at
    assert tg.toasts == ["⏱ У записи нет точного времени — напомню завтра 10:00"]


async def test_lead_on_a_relative_time_is_honoured(feed, updates, seed, tg) -> None:
    created = await seed.item("Созвон", kind="event", when=When(text="через 2 часа", in_min=120))

    await feed(updates.callback(LeadCb(id=created.item.id, m=30)))

    [reminder] = await _pending(seed, created.occurrence.id)
    assert reminder.fire_at == created.occurrence.due_at - timedelta(minutes=30)
    assert tg.toasts == ["⏱ Напомню за 30 мин"]


async def test_item_done_without_occurrence(feed, updates, seed, tg) -> None:
    created = await seed.item("Купить молоко", kind="purchase", timed=False)
    async with seed.sessionmaker() as s:  # a purchase without time gets a default occurrence: drop it
        occs = (await s.scalars(select(Occurrence).where(Occurrence.item_id == created.item.id))).all()
        for occ in occs:
            await s.delete(occ)
        await s.commit()

    await feed(updates.callback(ItemCb(a="done", id=created.item.id)))

    assert (await _item(seed, created.item.id)).status == "done"
    assert tg.toasts == ["✅ Готово"]


async def test_unknown_item(feed, updates, seed, tg) -> None:
    await feed(updates.callback(ItemCb(a="del", id=424242)))
    await feed(updates.callback(KindCb(id=424242, k="idea")))
    await feed(updates.callback(LeadCb(id=424242, m=5)))

    assert tg.toasts == ["Запись не найдена"] * 3
    assert tg.edits == []


async def test_garbage_callback_data(feed, updates, tg) -> None:
    await feed(updates.callback("zz:old:format"))
    await feed(updates.callback(ItemCb(a="explode", id=1)))
    assert tg.toasts == ["Кнопка устарела", "Кнопка устарела"]


# --- questions and edit candidates -------------------------------------------------------------------------------


async def test_ask_answer_queues_clarify(feed, updates, seed, tg, ctx) -> None:
    raw = await seed.raw(60, text="созвон в 8")
    kb = markup([button("утра", AskCb(raw=raw.id, n=0))], [button("вечера", AskCb(raw=raw.id, n=1))])

    await feed(updates.callback(AskCb(raw=raw.id, n=1), text="🤔 В 8 утра или вечера?", reply_markup=kb))

    [job] = await seed.jobs()
    assert (job.kind, job.payload, job.dedupe_key) == ("clarify", {"raw_id": raw.id, "option": 1}, f"clarify:{raw.id}")
    assert ctx.jobs_wakeup.is_set()
    [edit] = tg.edits
    assert edit.text.endswith("✅ вечера")
    assert edit.reply_markup is None
    assert tg.toasts == ["Принято, уточняю…"]

    await feed(updates.callback(AskCb(raw=raw.id, n=0), reply_markup=kb))
    assert len(await seed.jobs()) == 1
    assert tg.toasts[-1] == "Ответ уже принят"


async def test_ask_for_an_unknown_raw(feed, updates, seed, tg) -> None:
    await feed(updates.callback(AskCb(raw=9999, n=0)))
    assert tg.toasts == ["Запись не найдена"]
    assert await seed.jobs() == []


async def test_pick_queues_an_edit_job(feed, updates, seed, tg, ctx) -> None:
    raw = await seed.raw(61, text="перенеси встречу на 16")
    first = await seed.item("Встреча с Олегом")
    second = await seed.item("Встреча с Анной")
    kb = markup(
        [button("1", PickCb(raw=raw.id, item=first.item.id)), button("2", PickCb(raw=raw.id, item=second.item.id))]
    )

    await feed(
        updates.callback(PickCb(raw=raw.id, item=second.item.id), text="✏️ Какую запись изменить?", reply_markup=kb)
    )

    [job] = await seed.jobs()
    assert job.kind == "edit"
    assert job.payload == {"raw_id": raw.id, "item_id": second.item.id}
    assert job.dedupe_key == f"pick:{raw.id}:{second.item.id}"
    assert tg.edits[0].text.endswith("✅ 2")
    assert tg.toasts == ["Применяю правку…"]
    assert ctx.jobs_wakeup.is_set()


async def test_pick_of_a_deleted_item(feed, updates, seed, tg, frozen) -> None:
    raw = await seed.raw(62)
    created = await seed.item()
    async with seed.sessionmaker() as s:
        await items.delete_item(s, created.item.id, frozen.now)
        await s.commit()

    await feed(updates.callback(PickCb(raw=raw.id, item=created.item.id)))

    assert await seed.jobs() == []
    assert tg.toasts == ["🗑 Запись удалена"]


# --- digest ------------------------------------------------------------------------------------------------------


async def test_digest_moves_not_done_to_tomorrow(feed, updates, seed, tg, frozen, ctx, get_buttons) -> None:
    open_today = await seed.item("Позвонить", when=When(text="сегодня в 12", time="12:00"))
    done_today = await seed.item("Сделанное", when=When(text="сегодня в 11", time="11:00"))
    later = await seed.item("Послезавтра", when=When(text="послезавтра", day="+2", time="12:00"))
    async with seed.sessionmaker() as s:
        await items.mark_done(s, done_today.occurrence.id, frozen.now, Prefs())
        await s.commit()
    evening = frozen.advance(hours=11)  # 21:00 local
    async with seed.sessionmaker() as s:
        data = await digest.build_digest(s, "evening", Prefs(), evening)
    text, kb = views.evening_digest(data, Prefs(), evening)
    assert get_buttons(kb) == [("📅 Перенести невыполненное на завтра", DigestCb(a="tmr", d="20261006").pack())]

    await feed(updates.callback(DigestCb(a="tmr"), text=text, date=evening, reply_markup=kb))

    moved = await _occ(seed, open_today.occurrence.id)
    assert moved.due_at == open_today.occurrence.due_at + timedelta(days=1)
    assert moved.status == "open"
    assert (await _occ(seed, done_today.occurrence.id)).status == "done"
    assert (await _occ(seed, later.occurrence.id)).due_at == later.occurrence.due_at
    assert tg.toasts == ["📅 Перенёс на завтра: 1"]
    [edit] = tg.edits
    assert "Перенесено на завтра: 1" in edit.text
    assert "Сделанное" in edit.text
    assert edit.reply_markup is None
    assert ctx.scheduler_wakeup.is_set()


async def test_digest_leaves_items_still_ahead_tonight(feed, updates, seed, tg, frozen) -> None:
    # 21:05: "Перенести невыполненное" postpones the afternoon task, not tonight's 22:00 pills and their reminder.
    afternoon = await seed.item("Позвонить", when=When(text="сегодня в 15", time="15:00"))
    pills = await seed.item("Таблетки", when=When(text="перед сном", time="night"), rrule="FREQ=DAILY")
    frozen.advance(hours=11, minutes=5)

    await feed(updates.callback(DigestCb(a="tmr", d="20261006"), date=frozen.now - timedelta(minutes=5)))

    assert (await _occ(seed, afternoon.occurrence.id)).due_at == afternoon.occurrence.due_at + timedelta(days=1)
    tonight = await _occ(seed, pills.occurrence.id)
    assert (tonight.due_at, tonight.status) == (pills.occurrence.due_at, "open")
    [reminder] = await _pending(seed, tonight.id)
    assert reminder.fire_at == pills.occurrence.due_at - timedelta(minutes=5)  # 21:55 tonight
    assert tg.toasts == ["📅 Перенёс на завтра: 1"]
    assert "Ещё сегодня" in tg.edits[0].text and "Таблетки" in tg.edits[0].text


async def test_digest_delayed_by_quiet_hours_moves_its_own_day(feed, updates, seed, tg, frozen) -> None:
    # Tuesday's digest arrives Wed 08:00 (evening digest time inside quiet hours): Wednesday's meeting stays put.
    tuesday = await seed.item("Задача вторника", when=When(text="сегодня в 15", time="15:00"))
    wednesday = await seed.item("Встреча", when=When(text="завтра в 11", day="+1", time="11:00"))
    wed_8 = frozen.advance(hours=22)

    await feed(updates.callback(DigestCb(a="tmr", d="20261006"), date=wed_8))

    moved = await _occ(seed, tuesday.occurrence.id)
    assert moved.due_at == tuesday.occurrence.due_at + timedelta(days=1)  # Wed 15:00
    meeting = await _occ(seed, wednesday.occurrence.id)
    assert meeting.due_at == wednesday.occurrence.due_at
    assert [r.fire_at for r in await _pending(seed, meeting.id)] == [wednesday.reminders[0].fire_at]
    assert tg.toasts == ["📅 Перенёс на завтра: 1"]
    assert tg.edits[0].text.startswith("🌙 <b>Итоги дня</b>, вт 6 окт")


async def test_digest_pressed_after_midnight_moves_to_the_day_that_has_started(feed, updates, seed, frozen) -> None:
    task = await seed.item("Позвонить", when=When(text="сегодня в 15", time="15:00"))
    frozen.advance(hours=14, minutes=30)  # Wed 00:30

    await feed(updates.callback(DigestCb(a="tmr", d="20261006"), date=frozen.now - timedelta(hours=3, minutes=30)))

    assert (await _occ(seed, task.occurrence.id)).due_at == task.occurrence.due_at + timedelta(days=1)  # Wed 15:00


async def test_old_digest_button_without_a_day_uses_the_message_date(feed, updates, seed, tg, frozen) -> None:
    task = await seed.item("Позвонить", when=When(text="сегодня в 12", time="12:00"))
    evening = frozen.advance(hours=11)

    await feed(updates.callback("d:tmr", date=evening))

    assert (await _occ(seed, task.occurrence.id)).due_at == task.occurrence.due_at + timedelta(days=1)
    assert tg.toasts == ["📅 Перенёс на завтра: 1"]


async def test_digest_with_nothing_to_move(feed, updates, seed, tg) -> None:
    await feed(updates.callback(DigestCb(a="tmr"), text="🌙 Итоги дня"))
    assert tg.toasts == ["Нечего переносить"]
    assert "Итоги дня" in tg.edits[0].text


async def test_unknown_digest_action_is_stale(feed, updates, tg) -> None:
    await feed(updates.callback(DigestCb(a="zzz")))
    assert tg.toasts == ["Кнопка устарела"]
