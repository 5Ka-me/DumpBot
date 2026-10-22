"""Reminder buttons by item type (docs/features/iteration-4.md §2-3) through the dispatcher: [⏰ 3 часа], a span's
[➡️ След. неделя] / [➡️ След. месяц] / [💤 Когда-нибудь] / [📅 Выбрать день] (the 7-day picker, a day, [Другая дата…],
[← Назад]), a series' [⏹ Хватит повторять] and [⏹ Больше не повторять] after its ✅; each press is journaled. The clock
starts at Tue 2026-10-06 10:00 Europe/Warsaw."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from dumpbot.ai.schemas import When
from dumpbot.bot.cbdata import DayCb, ItemCb, OccCb
from dumpbot.db.models import Event, Item, Occurrence
from dumpbot.domain.prefs import Prefs
from dumpbot.services import items

WARSAW = ZoneInfo("Europe/Warsaw")
WEEK = When(text="на этой неделе", period="this_week")
MONTH = When(text="в этом месяце", period="this_month")


def local(*args: int) -> datetime:
    return datetime(*args, tzinfo=WARSAW).astimezone(UTC)


async def _item(seed, item_id: int) -> Item:
    async with seed.sessionmaker() as s:
        return await s.get(Item, item_id)


async def _occurrences(seed, item_id: int) -> list[Occurrence]:
    async with seed.sessionmaker() as s:
        stmt = select(Occurrence).where(Occurrence.item_id == item_id).order_by(Occurrence.due_at)
        return list((await s.scalars(stmt)).all())


async def _pending(seed, occ_id: int) -> list[datetime]:
    async with seed.sessionmaker() as s:
        return [r.fire_at for r in await items.pending_reminders(s, occ_id)]


async def _events(seed) -> list[Event]:
    async with seed.sessionmaker() as s:
        return list((await s.scalars(select(Event).order_by(Event.id))).all())


async def _reminder(seed, created, message_id: int = 950) -> int:
    await seed.bot_message(message_id, purpose="reminder", item_id=created.item.id, occurrence_id=created.occurrence.id)
    return message_id


# --- a span: next week / month, someday ---------------------------------------------------------------------------


async def test_next_week_makes_the_item_next_weeks(feed, updates, seed, tg, frozen, ctx, get_buttons) -> None:
    created = await seed.item("Посмотреть Raspberry Pi", when=WEEK)
    frozen.now = local(2026, 10, 9, 19, 0)  # Friday's reminder (2)
    message_id = await _reminder(seed, created)

    await feed(updates.callback(OccCb(a="nw", id=created.occurrence.id), message_id=message_id))

    item = await _item(seed, created.item.id)
    assert (item.period_kind, item.period_start, item.period_end) == ("week", date(2026, 10, 12), date(2026, 10, 18))
    [occ] = [o for o in await _occurrences(seed, item.id) if o.status == "open"]
    assert (occ.due_at, occ.precision) == (local(2026, 10, 16, 19, 0), "period")
    assert await _pending(seed, occ.id) == [local(2026, 10, 12, 10, 0), local(2026, 10, 16, 19, 0)]
    assert tg.toasts == ["➡️ Перенёс на следующую неделю"]
    [edit] = tg.edits
    assert edit.text == (
        "🔔 <b>Посмотреть Raspberry Pi</b>\n"
        "➡️ перенёс · 🗓 на следующей неделе (до вс 18 окт) · 🔔 пн 10:00, пт 16 окт 19:00"
    )
    assert edit.reply_markup is None
    assert ctx.scheduler_wakeup.is_set()
    [row] = await _events(seed)
    assert (row.source, row.data["action"], row.occurrence_id) == ("reminder", "o:nw", created.occurrence.id)


async def test_next_month_of_a_month_item(feed, updates, seed, tg, frozen) -> None:
    created = await seed.item("Поменять шины", when=MONTH)
    frozen.now = local(2026, 10, 30, 19, 0)

    await feed(updates.callback(OccCb(a="nm", id=created.occurrence.id), message_id=await _reminder(seed, created)))

    item = await _item(seed, created.item.id)
    assert (item.period_kind, item.period_start, item.period_end) == ("month", date(2026, 11, 1), date(2026, 11, 30))
    assert tg.toasts == ["➡️ Перенёс на следующий месяц"]


async def test_next_week_of_a_deadline_is_a_span(feed, updates, seed, tg, frozen) -> None:
    """ "до пятницы" not done by Friday: [➡️ След. неделя] makes it "на следующей неделе" (no longer a deadline)."""
    created = await seed.item("Сдать отчёт", when=When(text="до пятницы", day="FR", role="deadline"))
    frozen.now = local(2026, 10, 9, 10, 0)

    await feed(updates.callback(OccCb(a="nw", id=created.occurrence.id), message_id=await _reminder(seed, created)))

    item = await _item(seed, created.item.id)
    assert (item.period_kind, item.when_spec["role"]) == ("week", "event")


async def test_someday_takes_the_date_away(feed, updates, seed, tg, frozen) -> None:
    created = await seed.item("Выучить испанский", when=When(text="на днях", period="soon"))
    frozen.now = local(2026, 10, 8, 19, 0)

    await feed(updates.callback(OccCb(a="smd", id=created.occurrence.id), message_id=await _reminder(seed, created)))

    item = await _item(seed, created.item.id)
    assert (item.someday, item.status) == (True, "open")
    assert [o.status for o in await _occurrences(seed, item.id)] == ["cancelled"]
    assert tg.toasts == ["💤 Когда-нибудь"]
    assert tg.edits[0].text == "🔔 <b>Выучить испанский</b>\n💤 отложил на когда-нибудь — запись в /someday"


async def test_span_buttons_of_a_done_item_only_say_so(feed, updates, seed, tg, frozen) -> None:
    created = await seed.item("Посмотреть Raspberry Pi", when=WEEK)
    async with seed.sessionmaker() as s:
        await items.mark_done(s, created.occurrence.id, frozen.now, Prefs())
        await s.commit()
    for action in ("nw", "smd", "pick"):
        await feed(updates.callback(OccCb(a=action, id=created.occurrence.id)))
    assert tg.toasts == ["Уже сделано"] * 3
    assert (await _item(seed, created.item.id)).period_kind == "week"


# --- the day picker -----------------------------------------------------------------------------------------------


async def test_pick_a_day(feed, updates, seed, tg, frozen, get_buttons) -> None:
    created = await seed.item("Посмотреть Raspberry Pi", when=WEEK)
    occ_id = created.occurrence.id
    message_id = await _reminder(seed, created)

    await feed(updates.callback(OccCb(a="pick", id=occ_id), message_id=message_id))

    [picker] = tg.edits
    assert picker.text.endswith("📅 На какой день перенести?")
    labels = get_buttons(picker.reply_markup)
    assert labels[:7] == [
        (label, DayCb(id=occ_id, d=f"202610{day:02d}").pack())
        for label, day in [
            ("вт 6", 6),
            ("ср 7", 7),
            ("чт 8", 8),
            ("пт 9", 9),
            ("сб 10", 10),
            ("вс 11", 11),
            ("пн 12", 12),
        ]
    ]
    assert labels[7:] == [("Другая дата…", ItemCb(a="time", id=created.item.id).pack()), ("← Назад", f"o:rem:{occ_id}")]
    assert [o.id for o in await _occurrences(seed, created.item.id)] == [occ_id]  # nothing changed yet

    await feed(updates.callback(DayCb(id=occ_id, d="20261008"), message_id=message_id))

    item = await _item(seed, created.item.id)
    [occ] = [o for o in await _occurrences(seed, item.id) if o.status == "open"]
    assert (occ.due_at, occ.precision, item.period_kind) == (local(2026, 10, 8, 19, 0), "exact", None)
    assert await _pending(seed, occ.id) == [local(2026, 10, 8, 19, 0)]
    assert tg.toasts == [None, "📅 Перенёс на чт 8 окт"]
    assert tg.edits[-1].text == "🔔 <b>Посмотреть Raspberry Pi</b>\n➡️ перенёс · 🔔 чт 8 окт 19:00"
    rows = await _events(seed)
    assert [(e.data["action"], e.occurrence_id, e.item_id) for e in rows] == [
        ("o:pick", occ_id, created.item.id),
        ("dy", occ_id, created.item.id),
    ]


async def test_back_from_the_picker(feed, updates, seed, tg, get_buttons) -> None:
    created = await seed.item("Посмотреть Raspberry Pi", when=WEEK)
    await feed(updates.callback(OccCb(a="rem", id=created.occurrence.id)))
    [edit] = tg.edits
    assert edit.text == "🔔 <b>Посмотреть Raspberry Pi</b>\n🗓 на этой неделе (до вс 11 окт)"
    assert [label for label, _ in get_buttons(edit.reply_markup)] == [
        "✅ Готово",
        "➡️ След. неделя",
        "📅 Выбрать день",
        "💤 Когда-нибудь",
    ]


async def test_a_day_already_past_shows_the_picker_again(feed, updates, seed, tg, frozen, get_buttons) -> None:
    created = await seed.item("Посмотреть Raspberry Pi", when=WEEK)
    frozen.now = local(2026, 10, 7, 20, 0)  # Wednesday evening: an old picker's "вт 6"

    await feed(updates.callback(DayCb(id=created.occurrence.id, d="20261006")))

    assert tg.toasts == ["Этот день уже прошёл — выбери другой"]
    assert get_buttons(tg.edits[0].reply_markup)[0][0] == "чт 8"  # today's 19:00 has passed too
    assert (await _item(seed, created.item.id)).period_kind == "week"


async def test_a_bad_day_is_stale(feed, updates, tg) -> None:
    await feed(updates.callback("dy:5:2026-10-8"))
    assert tg.toasts == ["Кнопка устарела"]


async def test_other_date_asks_for_the_time_as_text(feed, updates, seed, tg) -> None:
    created = await seed.item("Посмотреть Raspberry Pi", when=WEEK)
    await feed(updates.callback(ItemCb(a="time", id=created.item.id)))
    [edit] = tg.edits
    assert edit.text.startswith("🕒 Когда? Напиши новое время для <b>Посмотреть Raspberry Pi</b>")


# --- a series: ⏹ ---------------------------------------------------------------------------------------------------


async def test_stop_on_a_series_reminder(feed, updates, seed, tg, frozen, ctx, get_buttons) -> None:
    """[⏹ Хватит повторять] on the 11:00 reminder: this time closed, tomorrow's (made in advance) cancelled with its
    reminder, nothing more comes."""
    created = await seed.item("Таблетки", when=When(text="каждый день в 11", time="11:00"), rrule="FREQ=DAILY")
    frozen.advance(hours=1, minutes=1)
    async with seed.sessionmaker() as s:
        await items.ensure_recurring(s, Prefs(), frozen.now)
        await s.commit()
    message_id = await _reminder(seed, created)

    await feed(updates.callback(OccCb(a="stop", id=created.occurrence.id), message_id=message_id))

    today, tomorrow = await _occurrences(seed, created.item.id)
    assert (today.status, tomorrow.status) == ("done", "cancelled")
    assert await _pending(seed, tomorrow.id) == []
    item = await _item(seed, created.item.id)
    assert (item.rec_active, item.status) == (False, "done")
    assert tg.toasts == ["⏹ Повтор остановлен"]
    [edit] = tg.edits
    assert edit.text == "✅ <s>Таблетки</s>\n<i>сегодня 11:00 · сделано</i>\n🔁 повтор остановлен"
    assert get_buttons(edit.reply_markup) == [("↩️ Не сделано", f"o:undone:{today.id}")]
    assert ctx.scheduler_wakeup.is_set()

    await feed(updates.callback(OccCb(a="stop", id=created.occurrence.id), message_id=message_id))
    assert tg.toasts[-1] == "Повтор уже остановлен"


async def test_done_then_no_more(feed, updates, seed, tg, frozen, get_buttons) -> None:
    created = await seed.item("Таблетки", when=When(text="каждый день в 11", time="11:00"), rrule="FREQ=DAILY")
    frozen.advance(hours=1, minutes=1)
    message_id = await _reminder(seed, created)
    occ_id = created.occurrence.id

    await feed(updates.callback(OccCb(a="done", id=occ_id), message_id=message_id))
    assert get_buttons(tg.edits[0].reply_markup) == [
        ("↩️ Не сделано", f"o:undone:{occ_id}"),
        ("⏹ Больше не повторять", f"o:stop:{occ_id}"),
    ]
    today, tomorrow = await _occurrences(seed, created.item.id)
    assert (today.status, tomorrow.status) == ("done", "open")

    await feed(updates.callback(OccCb(a="stop", id=occ_id), message_id=message_id))

    today, tomorrow = await _occurrences(seed, created.item.id)
    assert (today.status, tomorrow.status) == ("done", "cancelled")
    assert await _pending(seed, tomorrow.id) == []
    assert tg.toasts == ["✅ Готово · следующий раз завтра 11:00", "⏹ Повтор остановлен"]
    assert get_buttons(tg.edits[-1].reply_markup) == [("↩️ Не сделано", f"o:undone:{occ_id}")]


async def test_stop_of_an_instance_no_longer_open(feed, updates, seed, tg, frozen) -> None:
    created = await seed.item("Таблетки", when=When(text="каждый день в 11", time="11:00"), rrule="FREQ=DAILY")
    async with seed.sessionmaker() as s:
        occ = await s.get(Occurrence, created.occurrence.id)
        occ.status = "missed"
        await s.commit()
    frozen.advance(days=1)

    await feed(updates.callback(OccCb(a="stop", id=created.occurrence.id)))

    assert (await _item(seed, created.item.id)).rec_active is False
    assert tg.edits[0].text == "🔔 <b>Таблетки</b>\n⏹ повтор остановлен — больше не напомню"


async def test_old_evening_and_15_minute_buttons_still_work(feed, updates, seed, tg, frozen) -> None:
    """Reminders sent before iteration 4 carry [🌙 Вечером] and [⏰ 15 мин]."""
    created = await seed.item(when=When(text="сегодня в 10:30", time="10:30"))
    frozen.advance(minutes=31)
    await feed(updates.callback(OccCb(a="eve", id=created.occurrence.id)))
    await feed(updates.callback(OccCb(a="s15", id=created.occurrence.id)))
    assert tg.toasts[0] == "🌙 Напомню сегодня 19:00"
    assert tg.toasts[1].startswith("⏰ Напомню сегодня")
    [occ] = [o for o in await _occurrences(seed, created.item.id) if o.status == "open"]
    assert occ.due_at > frozen.now + timedelta(hours=8)  # 19:00: the 15 minutes do not move it earlier
