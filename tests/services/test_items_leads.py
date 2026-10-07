"""Explicit leads ("напомни за час", "за день и за час") through the item's life: the occurrence's time is its last
notification and the stated time is always due + the smallest lead, whatever re-plans it — undone, restore, 🔕, a new
lead of a series, the evening digest's move, a snooze (review fixes of the 2026-10-07 notification-time change).
Reference time: Tue 2026-10-06 10:00 Europe/Warsaw."""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select

from dumpbot.ai.schemas import EditPatch
from dumpbot.db.models import Reminder
from dumpbot.services import digest, items, query

DENTIST = {"kind": "event", "title": "Стоматолог (10:30)", "when": {"day": "FR", "time": "10:30"}}


def patch(**kwargs) -> EditPatch:
    return EditPatch.model_validate(kwargs)


async def _send_due(session, occurrence_id, now) -> None:
    """What the scheduler does: the pending reminders due by ``now`` are sent."""
    stmt = select(Reminder).where(
        Reminder.occurrence_id == occurrence_id, Reminder.status == "pending", Reminder.fire_at <= now
    )
    for reminder in (await session.scalars(stmt)).all():
        reminder.status = "sent"
    await session.flush()


# --- several leads: the later notification survives re-planning ------------------------------------------------


async def test_done_then_undone_on_the_day_before_keeps_the_hour_before(session, make, reminders_of, local, now, prefs):
    # «за день и за час до стоматолога в пт 10:30»: Thu 10:30 and Fri 09:30. ✅ on Thursday's reminder, then
    # "↩️ Не сделано": Friday's 09:30 is planned again.
    [c] = await make({**DENTIST, "lead": [1440, 60]}, now=now)
    assert c.occurrence.due_at == local(2026, 10, 9, 9, 30)
    await _send_due(session, c.occurrence.id, local(2026, 10, 8, 10, 30))
    await items.mark_done(session, c.occurrence.id, local(2026, 10, 8, 10, 32), prefs)
    assert await reminders_of(c.occurrence.id, "pending") == []
    await items.mark_undone(session, c.occurrence.id, prefs=prefs, now=local(2026, 10, 8, 10, 33))
    assert c.occurrence.due_at == local(2026, 10, 9, 9, 30)
    assert await reminders_of(c.occurrence.id, "pending") == [(local(2026, 10, 9, 9, 30), "pending", 0, False)]


async def test_delete_then_restore_on_the_day_before_keeps_the_hour_before(
    session, make, reminders_of, local, now, prefs
):
    [c] = await make({**DENTIST, "lead": [1440, 60]}, now=now)
    await _send_due(session, c.occurrence.id, local(2026, 10, 8, 10, 30))
    await items.delete_item(session, c.item.id, local(2026, 10, 8, 10, 31))
    await items.restore_item(session, c.item.id, local(2026, 10, 8, 10, 35), prefs)
    assert await reminders_of(c.occurrence.id, "pending") == [(local(2026, 10, 9, 9, 30), "pending", 0, False)]


async def test_the_day_of_the_appointment_lists_it_and_the_day_before_does_not(session, make, local, now, prefs):
    # Thursday's evening digest has nothing "not done" to move; Friday's plan has it today, not as overdue.
    [c] = await make({**DENTIST, "lead": [1440, 60]}, now=now)
    thursday = await query.agenda(session, prefs, local(2026, 10, 8, 21, 0))
    assert [r.item_id for r in (*thursday.open, *thursday.overdue)] == []
    assert await digest.move_not_done(session, thursday.day, prefs, local(2026, 10, 8, 21, 0)) == []
    friday = await query.agenda(session, prefs, local(2026, 10, 9, 9, 0))
    assert [(r.item_id, r.due_at) for r in friday.open] == [(c.item.id, local(2026, 10, 9, 9, 30))]
    assert friday.overdue == []


async def test_the_evening_move_keeps_an_item_whose_stated_time_is_ahead(
    session, make, reminders_of, local, now, prefs
):
    # «за день до стоматолога в пт 10:30»: notified on Thursday at 10:30, still open at 21:00. The appointment is
    # tomorrow, so "перенести невыполненное на завтра" leaves it be (the move would notify at 10:30 itself).
    [c] = await make({**DENTIST, "lead": [1440]}, now=now)
    assert c.occurrence.due_at == local(2026, 10, 8, 10, 30)
    thursday = local(2026, 10, 8, 21, 0)
    assert await digest.move_not_done(session, thursday.astimezone(prefs.zone).date(), prefs, thursday) == []
    assert (c.occurrence.due_at, c.item.lead_minutes) == (local(2026, 10, 8, 10, 30), [1440])
    # Friday evening, the appointment past and not marked: moved, and the new time is its one notification.
    friday = local(2026, 10, 9, 21, 0)
    moved = await digest.move_not_done(session, friday.astimezone(prefs.zone).date(), prefs, friday)
    assert [o.id for o in moved] == [c.occurrence.id]
    assert (c.occurrence.due_at, c.item.lead_minutes) == (local(2026, 10, 10, 10, 30), None)
    assert await reminders_of(c.occurrence.id, "pending") == [(local(2026, 10, 10, 10, 30), "pending", 0, False)]


async def test_move_to_tomorrow_of_several_leads_notifies_once_at_the_new_time(
    session, make, reminders_of, local, now, prefs
):
    # Not done on Friday: Saturday 09:30, one notification — no "за день" relative to it, nothing on Sunday.
    [c] = await make({**DENTIST, "lead": [1440, 60]}, now=now)
    await items.move_to_tomorrow(session, [c.occurrence.id], prefs, local(2026, 10, 9, 21, 0))
    assert (c.occurrence.due_at, c.item.lead_minutes) == (local(2026, 10, 10, 9, 30), None)
    assert await reminders_of(c.occurrence.id, "pending") == [(local(2026, 10, 10, 9, 30), "pending", 0, False)]


# --- 🔕 and a new lead of a series: no duplicate instance ------------------------------------------------------


async def _open_dues(session, occurrences_of, item_id):
    return [o.due_at for o in await occurrences_of(item_id) if o.status == "open"]


async def test_reminders_off_on_a_series_with_a_lead_makes_no_duplicate(
    session, make, occurrences_of, reminders_of, local, now, prefs
):
    # «каждый день в 10, напомни за 15 минут»: Wed 09:45. 🔕 on Tuesday evening: Wednesday's instance goes back to
    # 10:00, and the next one follows on from it — not a second Wednesday at 10:00.
    spec = {"kind": "task", "title": "Планёрка", "rrule": "FREQ=DAILY", "when": {"time": "10:00"}, "lead": [15]}
    [c] = await make(spec, now=now)
    assert c.occurrence.due_at == local(2026, 10, 7, 9, 45)
    await items.remove_reminders(session, c.item.id, prefs=prefs, now=local(2026, 10, 6, 18, 0))
    assert c.item.lead_minutes == []
    assert await _open_dues(session, occurrences_of, c.item.id) == [local(2026, 10, 7, 10, 0)]
    for tick in (local(2026, 10, 7, 9, 46), local(2026, 10, 7, 10, 1), local(2026, 10, 8, 10, 1)):
        await items.ensure_recurring(session, prefs, tick)
    dues = [o.due_at for o in await occurrences_of(c.item.id)]
    assert dues == [local(2026, 10, 7, 10, 0), local(2026, 10, 8, 10, 0), local(2026, 10, 9, 10, 0)]
    assert Counter(d.date() for d in dues).most_common(1)[0][1] == 1


async def test_reminders_off_on_a_one_off_with_a_lead_goes_back_to_the_stated_time(session, make, local, now, prefs):
    [c] = await make({"kind": "event", "title": "Встреча (15:00)", "when": {"time": "15:00"}, "lead": [30]}, now=now)
    assert c.occurrence.due_at == local(2026, 10, 6, 14, 30)
    result = await items.apply_patch(session, c.item.id, patch(action="no_reminder"), prefs, now)
    assert (result.occurrence.due_at, result.reminders, c.item.lead_minutes) == (local(2026, 10, 6, 15, 0), [], [])


async def test_a_new_lead_from_a_series_reply_moves_its_other_instances_too(
    session, make, occurrences_of, reminders_of, local, prefs
):
    # «каждый день в 10, напомни за час»: Tue 09:00 and Wed 09:00 exist. At Tue 09:05 a reply to Tuesday's card
    # «перенеси на 11, напомни за 15 минут»: Tuesday 10:45; the series' lead is 15 minutes now, so Wednesday's
    # instance is 09:45 — and the next one made is Thursday's, not a second Wednesday.
    spec = {"kind": "task", "title": "Планёрка", "rrule": "FREQ=DAILY", "when": {"time": "10:00"}, "lead": [60]}
    [c] = await make(spec, now=local(2026, 10, 5, 12, 0))
    assert c.occurrence.due_at == local(2026, 10, 6, 9, 0)
    await items.ensure_recurring(session, prefs, local(2026, 10, 6, 9, 1))
    reply = patch(when={"text": "на 11", "time": "11:00"}, lead=[15])
    await items.apply_patch(session, c.item.id, reply, prefs, local(2026, 10, 6, 9, 5))
    assert c.item.lead_minutes == [15]
    assert await _open_dues(session, occurrences_of, c.item.id) == [
        local(2026, 10, 6, 10, 45),
        local(2026, 10, 7, 9, 45),
    ]
    for tick in (local(2026, 10, 6, 10, 46), local(2026, 10, 7, 9, 46)):
        await items.ensure_recurring(session, prefs, tick)
    occs = await occurrences_of(c.item.id)
    assert [o.due_at for o in occs] == [
        local(2026, 10, 6, 10, 45),
        local(2026, 10, 7, 9, 45),
        local(2026, 10, 8, 9, 45),
    ]
    wednesday = occs[1]
    assert [f for f, *_ in await reminders_of(wednesday.id, "pending")] == [local(2026, 10, 7, 9, 45)]


async def test_a_series_sent_after_todays_lead_moment_keeps_todays_instance(
    session, make, occurrences_of, reminders_of, local, prefs
):
    # 14:05: «каждый день в 14:30, 3 раза, напомни за час» — today notifies at 14:30; then tomorrow and the day after.
    spec = {
        "kind": "event",
        "title": "Созвон",
        "rrule": "FREQ=DAILY;COUNT=3",
        "when": {"text": "каждый день в 14:30, 3 раза", "time": "14:30"},
        "lead": [60],
    }
    [c] = await make(spec, now=local(2026, 10, 6, 14, 5))
    assert c.item.title == "Созвон (14:30)"
    assert await reminders_of(c.occurrence.id, "pending") == [(local(2026, 10, 6, 14, 30), "pending", 0, False)]
    for tick in (local(2026, 10, 6, 14, 6), local(2026, 10, 7, 13, 31), local(2026, 10, 8, 13, 31)):
        await items.ensure_recurring(session, prefs, tick)
    assert [o.due_at for o in await occurrences_of(c.item.id)] == [
        local(2026, 10, 6, 13, 30),
        local(2026, 10, 7, 13, 30),
        local(2026, 10, 8, 13, 30),
    ]
    assert c.item.rec_active is False  # COUNT=3: all three were real instances


# --- snooze never pulls an item earlier -------------------------------------------------------------------------


async def test_snooze_of_an_item_not_due_yet_keeps_its_time(session, make, reminders_of, local, now, prefs):
    # The Mini App's "Отложить 15 мин" on tomorrow's 13:00 at 10:00 today: one more reminder at 10:15, the item
    # stays tomorrow at 13:00 with its own reminder.
    [c] = await make({"kind": "task", "title": "Позвонить", "when": {"day": "+1", "time": "13:00"}}, now=now)
    reminder = await items.snooze(session, c.occurrence.id, minutes=15, prefs=prefs, now=now)
    assert reminder.fire_at == local(2026, 10, 6, 10, 15)
    assert c.occurrence.due_at == local(2026, 10, 7, 13, 0)
    assert [f for f, *_ in await reminders_of(c.occurrence.id, "pending")] == [
        local(2026, 10, 6, 10, 15),
        local(2026, 10, 7, 13, 0),
    ]
    # "завтра" lands on its own time: nothing moves, one reminder there
    reminder = await items.snooze(session, c.occurrence.id, tomorrow=True, prefs=prefs, now=now)
    assert (reminder.fire_at, c.occurrence.due_at) == (local(2026, 10, 7, 13, 0), local(2026, 10, 7, 13, 0))
    # an item on Friday snoozed "1 час" on Wednesday keeps Friday
    [f] = await make({"kind": "task", "title": "Отчёт", "when": {"day": "FR", "time": "18:00"}}, now=now)
    await items.snooze(session, f.occurrence.id, minutes=60, prefs=prefs, now=local(2026, 10, 7, 12, 0))
    assert f.occurrence.due_at == local(2026, 10, 9, 18, 0)


# --- the stated time in the title ---------------------------------------------------------------------------------


async def test_a_new_time_from_the_picker_drops_the_old_time_from_the_title(session, make, local, now, prefs):
    # "Встреча с Олегом (15:00)" moved to 16:00 in the Mini App: the card says 16:00, the title no longer 15:00.
    [c] = await make({"kind": "event", "title": "Встреча с Олегом (15:00)", "when": {"time": "15:00"}}, now=now)
    when = {"text": "2026-10-06T16:00", "day": "2026-10-06", "time": "16:00"}
    await items.apply_patch(session, c.item.id, patch(when=when), prefs, now)
    assert (c.item.title, c.occurrence.due_at) == ("Встреча с Олегом", local(2026, 10, 6, 16, 0))
    # the same time on another day keeps it; other brackets are not touched
    [d] = await make({"kind": "event", "title": "Встреча (15:00)", "when": {"time": "15:00"}, "lead": [30]}, now=now)
    await items.apply_patch(session, d.item.id, patch(when={"day": "+1", "time": "15:00"}), prefs, now)
    assert d.item.title == "Встреча (15:00)"
    [e] = await make({"kind": "task", "title": "Купить батарейки (2 шт)", "when": {"time": "15:00"}}, now=now)
    await items.apply_patch(session, e.item.id, patch(when={"time": "17:00"}), prefs, now)
    assert e.item.title == "Купить батарейки (2 шт)"


async def test_a_new_time_with_a_lead_names_it_in_the_title(session, make, local, now, prefs):
    # «перенеси на 16, напомни за полчаса» on "Созвон (15:00)" notified at 14:30: 15:30, and the title says 16:00.
    [c] = await make({"kind": "event", "title": "Созвон (15:00)", "when": {"time": "15:00"}, "lead": [30]}, now=now)
    await items.apply_patch(session, c.item.id, patch(when={"time": "16:00"}, lead=[30]), prefs, now)
    assert (c.item.title, c.occurrence.due_at) == ("Созвон (16:00)", local(2026, 10, 6, 15, 30))
    # a day lead from a reply names the day as well
    [d] = await make({"kind": "event", "title": "Стоматолог", "when": {"day": "FR", "time": "10:30"}}, now=now)
    await items.apply_patch(session, d.item.id, patch(lead=[1440]), prefs, now)
    assert (d.item.title, d.occurrence.due_at) == ("Стоматолог (пт 9 окт 10:30)", local(2026, 10, 8, 10, 30))
