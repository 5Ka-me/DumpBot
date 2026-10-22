"""Iteration 4 (docs/features/iteration-4.md §2-3) in the item services: stopping a series cancels every instance
still ahead with its reminders (the bug of 17.10: one made in advance still came) and closes the one already due when
the owner asked to; a stop of a one-off is "done"; several times a day in one series; moving to a day picked in a
reminder. Reference time: Tue 2026-10-06 10:00 Europe/Warsaw (services conftest)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time

import pytest
from dateutil.rrule import rrulestr
from sqlalchemy import select

from dumpbot.ai.schemas import EditPatch, When
from dumpbot.db.models import Item, Occurrence
from dumpbot.domain import recurrence
from dumpbot.services import items

DAILY_9 = {"kind": "task", "title": "Зарядка", "rrule": "FREQ=DAILY", "when": {"time": "09:00"}}
PILLS = {
    "kind": "task",
    "title": "Выпить таблетки",
    "rrule": "FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0",
    "when": {"text": "каждый день в 9:00 и 18:00", "time": "09:00"},
}
WEEK = {"text": "на этой неделе", "period": "this_week"}


# --- stop -----------------------------------------------------------------------------------------------------------


async def test_stop_cancels_every_instance_ahead_with_its_reminders(session, make, occurrences_of, reminders_of, local):
    """At 08:00, before today's 09:00: nothing of the series comes any more — today's instance (made in advance) is
    cancelled too, and with nothing open left the item is done."""
    [c] = await make(DAILY_9, now=local(2026, 10, 6, 7, 0))
    assert c.occurrence.due_at == local(2026, 10, 6, 9, 0)
    item = await items.stop_recurrence(session, c.item.id, local(2026, 10, 6, 8, 0))
    [occ] = await occurrences_of(c.item.id)
    assert (item.rec_active, occ.status, item.status) == (False, "cancelled", "done")
    assert await reminders_of(occ.id, "pending") == []
    assert item.rrule == "FREQ=DAILY"  # the rule stays, stopped


async def test_stop_keeps_the_instance_already_due(session, make, occurrences_of, reminders_of, local, prefs):
    [c] = await make(DAILY_9, now=local(2026, 10, 6, 8, 0))
    await items.ensure_recurring(session, prefs, local(2026, 10, 6, 9, 1))  # Wed 09:00 exists now
    item = await items.stop_recurrence(session, c.item.id, local(2026, 10, 6, 12, 0))
    today, tomorrow = await occurrences_of(c.item.id)
    assert (today.status, tomorrow.status, item.status) == ("open", "cancelled", "open")  # today's is still to do


async def test_stop_and_close_the_due_instance(session, make, occurrences_of, reminders_of, local, prefs):
    """A stop phrase / ⏹ in the reminder: today's 09:00 is done, tomorrow's (made in advance) cancelled, item done."""
    [c] = await make(DAILY_9, now=local(2026, 10, 6, 8, 0))
    await items.ensure_recurring(session, prefs, local(2026, 10, 6, 9, 1))
    noon = local(2026, 10, 6, 12, 0)
    item = await items.stop_recurrence(session, c.item.id, noon, close=True, prefs=prefs)
    today, tomorrow = await occurrences_of(c.item.id)
    assert (today.status, today.done_at, tomorrow.status) == ("done", noon, "cancelled")
    assert await reminders_of(tomorrow.id, "pending") == []
    assert (item.rec_active, item.status, item.done_at) == (False, "done", noon)
    assert await items.ensure_recurring(session, prefs, local(2026, 10, 9, 12, 0)) == 0


async def test_stop_closes_the_pressed_instance_even_when_snoozed_ahead(session, make, occurrences_of, local, prefs):
    """[⏹ Хватит повторять] on a reminder whose instance was snoozed to 13:00 (ahead): that one is closed, not
    cancelled."""
    [c] = await make(DAILY_9, now=local(2026, 10, 6, 8, 0))
    await items.snooze(session, c.occurrence.id, minutes=60, prefs=prefs, now=local(2026, 10, 6, 12, 0))
    item = await items.stop_recurrence(
        session, c.item.id, local(2026, 10, 6, 12, 5), close=True, occurrence_id=c.occurrence.id, prefs=prefs
    )
    statuses = {o.id: o.status for o in await occurrences_of(c.item.id)}
    assert statuses.pop(c.occurrence.id) == "done"
    assert set(statuses.values()) <= {"cancelled"}
    assert item.status == "done"


async def test_stop_after_done_cancels_the_next_instance(session, make, occurrences_of, reminders_of, local, prefs):
    """✅ then [⏹ Больше не повторять]: the next instance (made by the ✅) is cancelled with its reminder."""
    [c] = await make(DAILY_9, now=local(2026, 10, 6, 8, 0))
    await items.mark_done(session, c.occurrence.id, local(2026, 10, 6, 9, 5), prefs)
    today, tomorrow = await occurrences_of(c.item.id)
    assert (today.status, tomorrow.status) == ("done", "open")
    await items.stop_recurrence(
        session, c.item.id, local(2026, 10, 6, 9, 6), close=True, occurrence_id=today.id, prefs=prefs
    )
    assert (today.status, tomorrow.status) == ("done", "cancelled")
    assert await reminders_of(tomorrow.id, "pending") == []
    item = await items.get_item(session, c.item.id)
    assert (item.status, item.done_at) == ("done", local(2026, 10, 6, 9, 6))


async def test_stop_patch_closes_the_due_instance(session, make, occurrences_of, local, prefs):
    """ "Закрывай вообще" as an edit (iteration 4, §2): stop + close this time."""
    [c] = await make(DAILY_9, now=local(2026, 10, 6, 8, 0))
    await items.ensure_recurring(session, prefs, local(2026, 10, 6, 9, 1))
    result = await items.apply_patch(
        session, c.item.id, EditPatch(action="stop_recurrence"), prefs, local(2026, 10, 6, 12, 0)
    )
    assert [o.status for o in await occurrences_of(c.item.id)] == ["done", "cancelled"]
    assert (result.item.rec_active, result.item.status, result.occurrence.status) == (False, "done", "done")


async def test_stop_patch_of_a_one_off_is_done(session, make, now, prefs):
    [c] = await make({"kind": "task", "title": "Позвонить", "when": {"time": "15:00"}}, now=now)
    result = await items.apply_patch(session, c.item.id, EditPatch(action="stop_recurrence"), prefs, now)
    assert (result.item.status, result.occurrence.status) == ("done", "done")


async def test_stop_patch_of_a_stopped_series_is_done(session, make, local, prefs):
    [c] = await make(DAILY_9, now=local(2026, 10, 6, 8, 0))
    await items.stop_recurrence(session, c.item.id, local(2026, 10, 6, 8, 30))  # today's 09:00 cancelled, done
    await items.mark_undone(session, c.occurrence.id, prefs=prefs, now=local(2026, 10, 6, 8, 40))
    result = await items.apply_patch(
        session, c.item.id, EditPatch(action="stop_recurrence"), prefs, local(2026, 10, 6, 9, 30)
    )
    assert (result.item.status, result.occurrence.status) == ("done", "done")


async def _pills_at_evening(session, make, occurrences_of, local, prefs):
    """ "в 9:00 и 18:00", this morning's never ticked (43 % of reminders get no reaction), the 18:00 reminder came and
    tomorrow's 09:00 is made already."""
    [c] = await make(PILLS, now=local(2026, 10, 6, 8, 0))
    await items.ensure_recurring(session, prefs, local(2026, 10, 6, 9, 1))  # the 18:00 one
    await items.ensure_recurring(session, prefs, local(2026, 10, 6, 18, 1))  # tomorrow's 09:00
    _, evening, tomorrow = await occurrences_of(c.item.id)
    assert (evening.due_at, tomorrow.due_at) == (local(2026, 10, 6, 18, 0), local(2026, 10, 7, 9, 0))
    return c, evening


@pytest.mark.parametrize("replied", [True, False])
async def test_a_stop_reply_to_the_evening_reminder_closes_the_evening_one(
    session, make, occurrences_of, local, prefs, replied
):
    """«хватит повторять» in reply to the 18:00 reminder (its instance passed along) — or with no instance known: the
    latest one due — closes the 18:00, not this morning's; the morning's is missed, tomorrow's cancelled, the item is
    done (nothing is left open and overdue for ever)."""
    c, evening = await _pills_at_evening(session, make, occurrences_of, local, prefs)
    result = await items.apply_patch(
        session,
        c.item.id,
        EditPatch(action="stop_recurrence"),
        prefs,
        local(2026, 10, 6, 18, 5),
        occurrence_id=evening.id if replied else None,
    )
    await session.commit()
    assert [o.status for o in await occurrences_of(c.item.id)] == ["missed", "done", "cancelled"]
    assert (result.item.rec_active, result.item.status) == (False, "done")
    assert await items.ensure_recurring(session, prefs, local(2026, 10, 8, 12, 0)) == 0


async def test_done_in_reply_to_the_evening_reminder_closes_the_evening_one(
    session, make, occurrences_of, local, prefs
):
    """«выпил» in reply to the 18:00 reminder: the 18:00 is done; the series goes on."""
    c, evening = await _pills_at_evening(session, make, occurrences_of, local, prefs)
    result = await items.apply_patch(
        session, c.item.id, EditPatch(action="done"), prefs, local(2026, 10, 6, 18, 5), occurrence_id=evening.id
    )
    assert [o.status for o in await occurrences_of(c.item.id)] == ["open", "done", "open"]
    assert (result.item.rec_active, result.item.status) == (True, "open")


async def test_stop_button_on_the_evening_reminder_finishes_the_item(session, make, occurrences_of, local, prefs):
    """[⏹ Хватит повторять] on the 18:00 reminder with the morning's never ticked: the morning's is missed, so
    nothing open is left and the item is done (not left in /tasks and the weekly review)."""
    c, evening = await _pills_at_evening(session, make, occurrences_of, local, prefs)
    item = await items.stop_recurrence(
        session, c.item.id, local(2026, 10, 6, 18, 5), close=True, occurrence_id=evening.id, prefs=prefs
    )
    assert [o.status for o in await occurrences_of(c.item.id)] == ["missed", "done", "cancelled"]
    assert (item.rec_active, item.status) == (False, "done")


async def test_a_stopped_series_is_done_once_its_last_open_instance_is_missed(
    session, make, occurrences_of, local, prefs
):
    """Stopped from the card (an instance due stays on today's list), the 18:00 ticked, the 09:00 never: when the
    09:00 is marked missed, the stopped item has nothing more to do and is done."""
    c, evening = await _pills_at_evening(session, make, occurrences_of, local, prefs)
    await items.stop_recurrence(session, c.item.id, local(2026, 10, 6, 18, 5))
    await items.mark_done(session, evening.id, local(2026, 10, 6, 18, 10), prefs)
    assert (await session.get(Item, c.item.id)).status == "open"  # this morning's is still open
    await items.ensure_recurring(session, prefs, local(2026, 10, 7, 7, 0))  # 22 h after it: superseded
    assert [o.status for o in await occurrences_of(c.item.id)] == ["missed", "done", "cancelled"]
    assert (await session.get(Item, c.item.id)).status == "done"


async def test_a_stop_under_way_while_the_next_instance_is_made(sessionmaker, make, local, prefs):
    """[⏹] pressed while the scheduler makes the next instance: the scheduler picked the series as active, but the
    stop holds the item's row — the series is skipped this time (and seen stopped the next), no instance of a stopped
    series with a reminder still to come."""
    [c] = await make(DAILY_9, now=local(2026, 10, 6, 8, 0))
    now = local(2026, 10, 6, 9, 30)

    async def tick(at) -> int:
        async with sessionmaker() as s:
            made = await items.ensure_recurring(s, prefs, at)
            await s.commit()
            return made

    async with sessionmaker() as press:
        await items.stop_recurrence(press, c.item.id, now, close=True, occurrence_id=c.occurrence.id, prefs=prefs)
        running = asyncio.create_task(tick(now))  # the stop is not committed yet: the series still looks active
        await asyncio.sleep(0.5)
        await press.commit()
    assert await running == 0
    assert await tick(local(2026, 10, 6, 9, 31)) == 0
    assert await _statuses(sessionmaker, c.item.id) == ["done"]  # today's, closed by the stop; nothing for tomorrow


async def test_a_stop_right_after_the_next_instance_was_made_cancels_it(sessionmaker, make, local, prefs):
    """The other order: the scheduler made tomorrow's instance (not committed yet) when [⏹] is pressed — the stop
    waits for it and then cancels it with its reminder."""
    [c] = await make(DAILY_9, now=local(2026, 10, 6, 8, 0))
    now = local(2026, 10, 6, 9, 30)

    async def press() -> None:
        async with sessionmaker() as s:
            await items.stop_recurrence(s, c.item.id, now, close=True, occurrence_id=c.occurrence.id, prefs=prefs)
            await s.commit()

    async with sessionmaker() as tick:
        assert await items.ensure_recurring(tick, prefs, now) == 1
        pressing = asyncio.create_task(press())
        await asyncio.sleep(0.5)
        assert not pressing.done()  # it waits for the scheduler's lock on the item
        await tick.commit()
    await pressing
    assert await _statuses(sessionmaker, c.item.id) == ["done", "cancelled"]
    async with sessionmaker() as s:
        tomorrow = (await s.scalars(select(Occurrence).where(Occurrence.item_id == c.item.id))).all()
        pending = [r for o in tomorrow for r in await items.pending_reminders(s, o.id)]
    assert pending == []


async def _statuses(sessionmaker, item_id: int) -> list[str]:
    async with sessionmaker() as s:
        stmt = select(Occurrence.status).where(Occurrence.item_id == item_id).order_by(Occurrence.due_at)
        return list((await s.scalars(stmt)).all())


# --- several times a day --------------------------------------------------------------------------------------------


async def test_two_times_a_day_series_goes_on(session, make, occurrences_of, reminders_of, local, prefs):
    [c] = await make(PILLS, now=local(2026, 10, 6, 8, 0))
    assert (c.item.rrule, c.occurrence.due_at, c.occurrence.precision) == (
        "FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0",
        local(2026, 10, 6, 9, 0),
        "exact",
    )
    assert await items.ensure_recurring(session, prefs, local(2026, 10, 6, 9, 1)) == 1  # the 18:00 one
    morning, evening = await occurrences_of(c.item.id)
    assert evening.due_at == local(2026, 10, 6, 18, 0)
    assert await reminders_of(evening.id) == [(local(2026, 10, 6, 18, 0), "pending", 0, False)]
    await items.mark_done(session, evening.id, local(2026, 10, 6, 18, 5), prefs)
    nxt = await items.current_occurrence(session, c.item.id)
    assert nxt.due_at == local(2026, 10, 6, 9, 0)  # this morning's, not done, is the earliest open
    await items.mark_done(session, morning.id, local(2026, 10, 6, 18, 6), prefs)
    nxt = await items.current_occurrence(session, c.item.id)
    assert (nxt.due_at, nxt.precision) == (local(2026, 10, 7, 9, 0), "exact")


async def test_a_reply_gives_a_task_two_times_a_day(session, make, local, now, prefs):
    """ "каждый день в 9 и 18" in reply to a card: the series names both times, starts at the next one."""
    [c] = await make({"kind": "task", "title": "Таблетки", "when": {"time": "15:00"}}, now=now)
    when = When(text="каждый день в 9 и 18", time="09:00")
    result = await items.apply_patch(
        session, c.item.id, EditPatch(rrule="FREQ=DAILY;BYHOUR=9,18", when=when), prefs, now
    )
    item = result.item
    assert (item.rrule, item.rec_active) == ("FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0", True)
    assert item.rec_dtstart_local == datetime(2026, 10, 6, 9, 0)
    assert result.occurrence.due_at == local(2026, 10, 6, 18, 0)  # 10:00 now: this evening's
    assert recurrence.rule_times(item.rrule) == (time(9, 0), time(18, 0))


async def test_count_starts_at_the_first_time_still_ahead(session, make, local, now, prefs):
    """ "каждый день в 9 и 18, 4 раза" in reply at noon: the past 09:00 of today does not use one up — today 18:00
    and the next two days."""
    [c] = await make({"kind": "task", "title": "Таблетки", "when": {"time": "15:00"}}, now=now)
    noon = local(2026, 10, 6, 12, 0)
    when = When(text="каждый день в 9 и 18, 4 раза", time="09:00")
    patch = EditPatch(rrule="FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0;COUNT=4", when=when)
    result = await items.apply_patch(session, c.item.id, patch, prefs, noon)
    item = result.item
    assert result.occurrence.due_at == local(2026, 10, 6, 18, 0)
    instances = list(rrulestr(item.rrule, dtstart=item.rec_dtstart_local))
    assert instances == [
        datetime(2026, 10, 6, 18, 0),
        datetime(2026, 10, 7, 9, 0),
        datetime(2026, 10, 7, 18, 0),
        datetime(2026, 10, 8, 9, 0),
    ]


# --- [📅 Выбрать день] ------------------------------------------------------------------------------------------------


async def test_picked_day_of_a_span_is_that_day_at_the_default_time(session, make, local, now, prefs, reminders_of):
    [c] = await make({"kind": "task", "title": "Посмотреть Raspberry Pi", "when": WEEK}, now=now)
    assert items.day_time(c.item, c.occurrence, prefs) == prefs.task_default_time
    result = await items.move_to_day(session, c.occurrence.id, date(2026, 10, 8), prefs, now)
    assert (result.occurrence.due_at, result.occurrence.precision) == (local(2026, 10, 8, 19, 0), "exact")
    assert (result.item.period_kind, result.item.period_start) == (None, None)  # a plain dated item now
    assert await reminders_of(result.occurrence.id, "pending") == [(local(2026, 10, 8, 19, 0), "pending", 0, False)]


async def test_picked_day_keeps_the_time_and_the_lead(session, make, local, now, prefs, reminders_of):
    """ "Встреча (15:00)" notified at 14:30 ("за 30 минут"): the picked day keeps 15:00 and the lead."""
    spec = {"kind": "event", "title": "Встреча (15:00)", "when": {"day": "+1", "time": "15:00"}, "lead": [30]}
    [c] = await make(spec, now=now)
    assert items.day_time(c.item, c.occurrence, prefs) == time(15, 0)
    result = await items.move_to_day(session, c.occurrence.id, date(2026, 10, 9), prefs, now)
    assert result.occurrence.due_at == local(2026, 10, 9, 14, 30)
    assert (result.item.title, result.item.lead_minutes) == ("Встреча (15:00)", [30])


async def test_picked_day_of_a_deadline_stays_a_deadline(session, make, local, now, prefs, reminders_of):
    spec = {"kind": "task", "title": "Сдать отчёт", "when": {"text": "до пятницы", "day": "FR", "role": "deadline"}}
    [c] = await make(spec, now=now)
    result = await items.move_to_day(session, c.occurrence.id, date(2026, 10, 13), prefs, now)
    assert result.occurrence.due_at == local(2026, 10, 13, 19, 0)
    assert result.item.when_spec["role"] == "deadline"
    fires = [fire for fire, *_ in await reminders_of(result.occurrence.id, "pending")]
    assert fires == [local(2026, 10, 12, 19, 0), local(2026, 10, 13, 19, 0)]  # the evening before and the day


async def test_picker_days_start_today_while_the_time_is_ahead(make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Посмотреть Raspberry Pi", "when": WEEK}, now=now)
    days = items.picker_days(c.item, c.occurrence, prefs, now)  # 10:00: today's 19:00 is ahead
    assert days == [date(2026, 10, 6 + i) for i in range(7)]
    late = items.picker_days(c.item, c.occurrence, prefs, local(2026, 10, 6, 19, 30))
    assert late[0] == date(2026, 10, 7) and len(late) == 7
