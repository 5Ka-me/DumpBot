"""Done/undone, snooze, delete/restore, reminders off, field setters, recurrence and move_to_tomorrow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from dumpbot.db.models import Reminder
from dumpbot.domain.prefs import Prefs
from dumpbot.services import items
from dumpbot.services.settings_repo import save_prefs

DAILY_9 = {"kind": "task", "title": "Зарядка", "rrule": "FREQ=DAILY", "when": {"time": "09:00"}}
WEEKLY_MO_9 = {"kind": "task", "title": "Вынести мусор", "rrule": "FREQ=WEEKLY;BYDAY=MO", "when": {"time": "09:00"}}


# --- done / undone ---------------------------------------------------------------------------------------------


async def test_mark_done_one_off(session, make, reminders_of, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Оплатить счёт", "when": {"time": "15:00"}}, now=now)
    done_at = local(2026, 10, 6, 11, 0)
    occ = await items.mark_done(session, c.occurrence.id, done_at, prefs)
    assert occ.status == "done"
    assert occ.done_at == done_at
    assert c.item.status == "done"
    assert c.item.done_at == done_at
    assert await reminders_of(occ.id) == [(local(2026, 10, 6, 14, 55), "cancelled", 5, False)]


async def test_mark_done_keeps_sent_reminders(session, make, reminders_of, local, now, prefs):
    [c] = await make({"kind": "event", "title": "Врач", "when": {"time": "15:00"}, "lead": [60, 10]}, now=now)
    first = (await session.scalars(select(Reminder).where(Reminder.occurrence_id == c.occurrence.id))).all()
    early = min(first, key=lambda r: r.fire_at)
    early.status = "sent"
    await items.mark_done(session, c.occurrence.id, local(2026, 10, 6, 14, 10), prefs)
    assert await reminders_of(c.occurrence.id) == [
        (local(2026, 10, 6, 14, 0), "sent", 60, False),
        (local(2026, 10, 6, 14, 50), "cancelled", 10, False),
    ]


async def test_mark_done_is_idempotent(session, make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Оплатить счёт"}, now=now)
    first = local(2026, 10, 6, 11, 0)
    await items.mark_done(session, c.occurrence.id, first, prefs)
    await items.mark_done(session, c.occurrence.id, local(2026, 10, 6, 12, 0), prefs)
    assert c.occurrence.done_at == first


async def test_mark_done_recurring_creates_next(session, make, occurrences_of, reminders_of, local, now, prefs):
    [c] = await make(WEEKLY_MO_9, now=now)
    await items.mark_done(session, c.occurrence.id, local(2026, 10, 12, 9, 30), prefs)
    await session.commit()
    occs = await occurrences_of(c.item.id)
    assert [(o.due_at, o.status) for o in occs] == [
        (local(2026, 10, 12, 9, 0), "done"),
        (local(2026, 10, 19, 9, 0), "open"),
    ]
    assert c.item.status == "open"
    assert c.item.done_at is None
    assert await reminders_of(occs[1].id) == [(local(2026, 10, 19, 8, 55), "pending", 5, False)]
    # Doing it again does not add another occurrence.
    await items.mark_done(session, c.occurrence.id, local(2026, 10, 12, 9, 40), prefs)
    assert len(await occurrences_of(c.item.id)) == 2


async def test_mark_done_early_on_recurring_skips_to_following(session, make, occurrences_of, local, now, prefs):
    [c] = await make(WEEKLY_MO_9, now=now)
    await items.mark_done(session, c.occurrence.id, now, prefs)  # done on Tuesday for next Monday
    occs = await occurrences_of(c.item.id)
    assert [o.due_at for o in occs] == [local(2026, 10, 12, 9, 0), local(2026, 10, 19, 9, 0)]


async def test_mark_done_last_of_count_finishes_item(session, make, occurrences_of, local, now, prefs):
    spec = {"kind": "task", "title": "Таблетки", "rrule": "FREQ=DAILY;COUNT=1", "when": {"time": "21:00"}}
    [c] = await make(spec, now=now)
    await items.mark_done(session, c.occurrence.id, local(2026, 10, 6, 21, 5), prefs)
    assert c.item.rec_active is False
    assert c.item.status == "done"
    assert len(await occurrences_of(c.item.id)) == 1


async def test_mark_undone_replans_reminders(session, make, reminders_of, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Отчёт", "when": {"day": "+1", "time": "15:00"}}, now=now)
    await items.mark_done(session, c.occurrence.id, now, prefs)
    occ = await items.mark_undone(session, c.occurrence.id, prefs=prefs, now=now)
    assert occ.status == "open"
    assert occ.done_at is None
    assert c.item.status == "open"
    assert c.item.done_at is None
    assert await reminders_of(occ.id, "pending") == [(local(2026, 10, 7, 14, 55), "pending", 5, False)]


async def test_mark_undone_without_prefs_uses_stored_owner_prefs(session, make, reminders_of, local, now, prefs):
    await save_prefs(session, 42, Prefs(lead_minutes=10))
    [c] = await make({"kind": "task", "title": "Отчёт", "when": {"day": "+1", "time": "15:00"}}, now=now)
    await items.mark_done(session, c.occurrence.id, now, prefs)
    await items.mark_undone(session, c.occurrence.id)
    assert await reminders_of(c.occurrence.id, "pending") == [(local(2026, 10, 7, 14, 50), "pending", 10, False)]


async def test_mark_undone_on_open_occurrence_is_noop(session, make, reminders_of, now):
    [c] = await make({"kind": "task", "title": "Отчёт", "when": {"time": "15:00"}}, now=now)
    before = await reminders_of(c.occurrence.id)
    await items.mark_undone(session, c.occurrence.id, prefs=Prefs(), now=now)
    assert await reminders_of(c.occurrence.id) == before


# --- snooze ----------------------------------------------------------------------------------------------------


async def test_snooze_minutes(session, make, reminders_of, local, now, prefs):
    [c] = await make({"kind": "purchase", "title": "Купить наушники", "when": {"time": "lunch"}}, now=now)
    at = local(2026, 10, 6, 12, 56)
    reminder = await items.snooze(session, c.occurrence.id, minutes=15, prefs=prefs, now=at)
    assert reminder.id is not None
    assert (reminder.fire_at, reminder.auto, reminder.status) == (at + timedelta(minutes=15), False, "pending")
    assert await reminders_of(c.occurrence.id) == [
        (local(2026, 10, 6, 12, 55), "cancelled", 5, False),
        (local(2026, 10, 6, 13, 11), "pending", 0, False),
    ]


async def test_snooze_tomorrow_uses_due_time_of_day(session, make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Позвонить маме", "when": {"time": "13:00"}}, now=now)
    reminder = await items.snooze(session, c.occurrence.id, tomorrow=True, prefs=prefs, now=local(2026, 10, 6, 13, 5))
    assert reminder.fire_at == local(2026, 10, 7, 13, 0)


async def test_snooze_tomorrow_relative_to_now_even_if_due_is_later(session, make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Отчёт", "when": {"day": "+3", "time": "18:00"}}, now=now)
    reminder = await items.snooze(session, c.occurrence.id, tomorrow=True, prefs=prefs, now=now)
    assert reminder.fire_at == local(2026, 10, 7, 18, 0)


async def test_snooze_tomorrow_keeps_wall_time_across_dst(session, make, local, prefs):
    [c] = await make({"kind": "task", "title": "Отчёт", "when": {"time": "09:00"}}, now=local(2026, 10, 24, 8, 0))
    reminder = await items.snooze(session, c.occurrence.id, tomorrow=True, prefs=prefs, now=local(2026, 10, 24, 9, 1))
    assert reminder.fire_at == local(2026, 10, 25, 9, 0)
    assert reminder.fire_at.astimezone(UTC).hour == 8  # CET after the switch


@pytest.mark.parametrize("kwargs", [{}, {"minutes": 10, "tomorrow": True}, {"minutes": 0}])
async def test_snooze_needs_exactly_one_positive_option(session, make, now, prefs, kwargs):
    [c] = await make({"kind": "task", "title": "x"}, now=now)
    with pytest.raises(ValueError):
        await items.snooze(session, c.occurrence.id, prefs=prefs, now=now, **kwargs)


# --- delete / restore / reminders off --------------------------------------------------------------------------


async def test_delete_and_restore_one_off(session, make, reminders_of, local, now, prefs):
    [c] = await make({"kind": "event", "title": "Встреча", "when": {"time": "16:00"}}, now=now)
    await items.delete_item(session, c.item.id, now)
    assert c.item.status == "deleted"
    assert c.item.deleted_at == now
    assert await reminders_of(c.occurrence.id) == [(local(2026, 10, 6, 15, 55), "cancelled", 5, False)]
    await items.delete_item(session, c.item.id, now + timedelta(minutes=1))  # idempotent
    assert c.item.deleted_at == now

    item = await items.restore_item(session, c.item.id, now + timedelta(minutes=2), prefs)
    assert item.status == "open"
    assert item.deleted_at is None
    assert await reminders_of(c.occurrence.id, "pending") == [(local(2026, 10, 6, 15, 55), "pending", 5, False)]


async def test_restore_does_not_plan_past_reminders(session, make, reminders_of, local, now, prefs):
    [c] = await make({"kind": "event", "title": "Встреча", "when": {"time": "16:00"}}, now=now)
    await items.delete_item(session, c.item.id, now)
    await items.restore_item(session, c.item.id, local(2026, 10, 6, 17, 0), prefs)
    assert await reminders_of(c.occurrence.id, "pending") == []


async def test_restore_done_item_stays_done(session, make, now, prefs):
    [c] = await make({"kind": "task", "title": "Отчёт"}, now=now)
    await items.mark_done(session, c.occurrence.id, now, prefs)
    await items.delete_item(session, c.item.id, now)
    item = await items.restore_item(session, c.item.id, now, prefs)
    assert item.status == "done"


async def test_restore_of_not_deleted_item_is_noop(session, make, now, prefs):
    [c] = await make({"kind": "note", "title": "Заметка"}, now=now)
    assert (await items.restore_item(session, c.item.id, now, prefs)).status == "open"


async def test_delete_and_restore_recurring(session, make, occurrences_of, reminders_of, local, now, prefs):
    [c] = await make(WEEKLY_MO_9, now=now)
    await items.delete_item(session, c.item.id, now)
    assert c.item.rec_active is False
    assert await items.ensure_recurring(session, prefs, local(2026, 10, 12, 10, 0)) == 0

    later = local(2026, 10, 12, 10, 0)  # the Monday instance has passed while deleted
    await items.restore_item(session, c.item.id, later, prefs)
    assert c.item.rec_active is True
    occs = await occurrences_of(c.item.id)
    assert [o.due_at for o in occs] == [local(2026, 10, 12, 9, 0), local(2026, 10, 19, 9, 0)]
    assert await reminders_of(occs[1].id) == [(local(2026, 10, 19, 8, 55), "pending", 5, False)]


async def test_remove_reminders_silences_series(session, make, occurrences_of, reminders_of, local, now, prefs):
    [c] = await make(WEEKLY_MO_9, now=now)
    assert await items.remove_reminders(session, c.item.id) == 1
    assert c.item.lead_minutes == []
    assert await items.remove_reminders(session, c.item.id) == 0
    await items.mark_done(session, c.occurrence.id, local(2026, 10, 12, 9, 30), prefs)
    nxt = (await occurrences_of(c.item.id))[-1]
    assert nxt.due_at == local(2026, 10, 19, 9, 0)
    assert await reminders_of(nxt.id) == []


# --- setters ---------------------------------------------------------------------------------------------------


async def test_set_kind_keeps_reminders(session, make, reminders_of, now):
    [c] = await make({"kind": "task", "title": "Подумать о докладе", "when": {"time": "18:00"}}, now=now)
    before = await reminders_of(c.occurrence.id)
    item = await items.set_kind(session, c.item.id, "idea")
    assert item.kind == "idea"
    assert await reminders_of(c.occurrence.id) == before
    with pytest.raises(ValueError):
        await items.set_kind(session, c.item.id, "shopping")


async def test_set_title_and_tags(session, make, reminders_of, now):
    [c] = await make({"kind": "task", "title": "Старое", "tags": ["a"], "when": {"time": "18:00"}}, now=now)
    before = await reminders_of(c.occurrence.id)
    old_updated = c.item.updated_at
    assert (await items.set_title(session, c.item.id, "  Новое   название ")).title == "Новое название"
    item = await items.set_tags(session, c.item.id, ["#Работа", "работа", "срочно важно", ""])
    assert item.tags == ["работа", "срочно_важно"]
    assert item.updated_at >= old_updated
    assert await reminders_of(c.occurrence.id) == before
    with pytest.raises(ValueError):
        await items.set_title(session, c.item.id, "   ")


async def test_set_lead_replans_open_occurrences(session, make, reminders_of, local, now, prefs):
    [c] = await make({"kind": "event", "title": "Врач", "when": {"day": "+1", "time": "15:00"}}, now=now)
    result = await items.set_lead(session, c.item.id, [60, 1440, 60], prefs, now)
    assert c.item.lead_minutes == [1440, 60]
    assert result.occurrence.id == c.occurrence.id
    assert [(r.fire_at, r.lead_minutes) for r in result.reminders] == [
        (local(2026, 10, 6, 15, 0), 1440),
        (local(2026, 10, 7, 14, 0), 60),
    ]
    assert await reminders_of(c.occurrence.id, "cancelled") == [(local(2026, 10, 7, 14, 55), "cancelled", 5, False)]


async def test_set_lead_turns_reminders_back_on(session, make, reminders_of, local, now, prefs):
    [c] = await make({"kind": "event", "title": "Врач", "when": {"day": "+1", "time": "15:00"}}, now=now)
    await items.remove_reminders(session, c.item.id)
    await items.set_lead(session, c.item.id, [0], prefs, now)
    assert await reminders_of(c.occurrence.id, "pending") == [(local(2026, 10, 7, 15, 0), "pending", 0, False)]


# --- recurrence ------------------------------------------------------------------------------------------------


async def test_stop_recurrence(session, make, occurrences_of, local, now, prefs):
    [c] = await make(WEEKLY_MO_9, now=now)
    item = await items.stop_recurrence(session, c.item.id)
    assert item.rec_active is False
    assert item.rrule == "FREQ=WEEKLY;BYDAY=MO"
    assert c.occurrence.status == "open"
    assert await items.ensure_recurring(session, prefs, local(2026, 10, 12, 10, 0)) == 0
    await items.mark_done(session, c.occurrence.id, local(2026, 10, 12, 10, 0), prefs)
    assert item.status == "done"
    assert len(await occurrences_of(c.item.id)) == 1


async def test_ensure_recurring_is_idempotent_and_marks_superseded_missed(
    session, make, occurrences_of, reminders_of, local, now, prefs
):
    [c] = await make(DAILY_9, now=now)
    first = c.occurrence
    assert first.due_at == local(2026, 10, 7, 9, 0)
    assert await items.ensure_recurring(session, prefs, local(2026, 10, 7, 8, 0)) == 0  # future one exists

    tick = local(2026, 10, 7, 9, 30)
    assert await items.ensure_recurring(session, prefs, tick) == 1
    assert await items.ensure_recurring(session, prefs, tick) == 0
    await session.commit()
    occs = await occurrences_of(c.item.id)
    assert [(o.due_at, o.status) for o in occs] == [
        (local(2026, 10, 7, 9, 0), "open"),
        (local(2026, 10, 8, 9, 0), "open"),
    ]
    assert await reminders_of(occs[1].id) == [(local(2026, 10, 8, 8, 55), "pending", 5, False)]

    # Younger than 12 h: still open (the evening digest shows it as not done).
    await items.ensure_recurring(session, prefs, local(2026, 10, 7, 20, 59))
    assert first.status == "open"
    # Older than 12 h and superseded: missed, its pending reminder cancelled.
    assert await items.ensure_recurring(session, prefs, local(2026, 10, 7, 21, 1)) == 0
    assert first.status == "missed"
    assert await reminders_of(first.id) == [(local(2026, 10, 7, 8, 55), "cancelled", 5, False)]
    assert occs[1].status == "open"


async def test_ensure_recurring_does_not_backfill_the_past(session, make, occurrences_of, local, now, prefs):
    [c] = await make(DAILY_9, now=now)
    assert await items.ensure_recurring(session, prefs, local(2026, 10, 10, 12, 0)) == 1
    occs = await occurrences_of(c.item.id)
    assert [(o.due_at, o.status) for o in occs] == [
        (local(2026, 10, 7, 9, 0), "missed"),
        (local(2026, 10, 11, 9, 0), "open"),
    ]


async def test_ensure_recurring_deactivates_exhausted_series(session, make, local, now, prefs):
    spec = {"kind": "task", "title": "Курс", "rrule": "FREQ=DAILY;COUNT=2", "when": {"time": "09:00"}}
    # Created before 09:00: COUNT is counted from rec_dtstart_local (today 09:00), so that is instance 1.
    [c] = await make(spec, now=local(2026, 10, 6, 8, 0))
    assert c.occurrence.due_at == local(2026, 10, 6, 9, 0)
    assert await items.ensure_recurring(session, prefs, local(2026, 10, 6, 10, 0)) == 1  # the 2nd one
    assert await items.ensure_recurring(session, prefs, local(2026, 10, 7, 10, 0)) == 0
    assert c.item.rec_active is False
    assert c.item.status == "open"  # an instance is still open


async def test_ensure_recurring_keeps_wall_time_across_dst(session, make, occurrences_of, local, prefs):
    [c] = await make(DAILY_9, now=local(2026, 10, 24, 8, 0))
    assert c.occurrence.due_at == local(2026, 10, 24, 9, 0)
    await items.ensure_recurring(session, prefs, local(2026, 10, 24, 9, 1))
    nxt = (await occurrences_of(c.item.id))[-1]
    assert nxt.due_at == local(2026, 10, 25, 9, 0)
    assert nxt.due_at == datetime(2026, 10, 25, 8, 0, tzinfo=UTC)


async def test_ensure_recurring_default_time_series_uses_auto_reminders(
    session, make, occurrences_of, reminders_of, local, now, prefs
):
    [c] = await make({"kind": "task", "title": "Полить цветы", "rrule": "FREQ=DAILY"}, now=now)
    assert c.occurrence.due_at == local(2026, 10, 6, 19, 0)
    assert c.occurrence.precision == "default"
    await items.ensure_recurring(session, prefs, local(2026, 10, 6, 19, 1))
    nxt = (await occurrences_of(c.item.id))[-1]
    assert (nxt.due_at, nxt.precision) == (local(2026, 10, 7, 19, 0), "default")
    assert await reminders_of(nxt.id) == [(local(2026, 10, 7, 19, 0), "pending", 0, True)]


# --- move_to_tomorrow ------------------------------------------------------------------------------------------


async def test_move_to_tomorrow(session, make, reminders_of, local, now, prefs):
    created = await make(
        {"kind": "task", "title": "Прошедшая", "when": {"time": "13:00"}},
        {"kind": "task", "title": "Поздняя", "when": {"time": "23:00"}},
        {"kind": "task", "title": "Без времени"},
        now=now,
    )
    evening = local(2026, 10, 6, 21, 0)
    moved = await items.move_to_tomorrow(
        session, [c.occurrence.id for c in created] + [created[0].occurrence.id], prefs, evening
    )
    assert [o.due_at for o in moved] == [
        local(2026, 10, 7, 13, 0),
        local(2026, 10, 7, 23, 0),
        local(2026, 10, 7, 19, 0),
    ]
    assert [o.id for o in moved] == [c.occurrence.id for c in created]
    assert await reminders_of(created[0].occurrence.id, "pending") == [
        (local(2026, 10, 7, 12, 55), "pending", 5, False)
    ]
    assert await reminders_of(created[1].occurrence.id) == [
        (local(2026, 10, 6, 22, 55), "cancelled", 5, False),
        (local(2026, 10, 7, 22, 55), "pending", 5, False),
    ]
    assert await reminders_of(created[2].occurrence.id, "pending") == [(local(2026, 10, 7, 19, 0), "pending", 0, True)]


async def test_move_to_tomorrow_skips_done_and_overdue_goes_to_tomorrow(session, make, local, now, prefs):
    done, old = await make(
        {"kind": "task", "title": "Сделано", "when": {"time": "12:00"}},
        {"kind": "task", "title": "Давно", "when": {"time": "11:00"}},
        now=now,
    )
    await items.mark_done(session, done.occurrence.id, now, prefs)
    moved = await items.move_to_tomorrow(
        session, [done.occurrence.id, old.occurrence.id, 987654], prefs, local(2026, 10, 9, 21, 0)
    )
    assert [o.id for o in moved] == [old.occurrence.id]
    assert moved[0].due_at == local(2026, 10, 10, 11, 0)
    assert done.occurrence.due_at == local(2026, 10, 6, 12, 0)


async def test_move_to_tomorrow_merges_into_existing_series_instance(
    session, make, occurrences_of, reminders_of, local, now, prefs
):
    [c] = await make(DAILY_9, now=now)
    await items.ensure_recurring(session, prefs, local(2026, 10, 7, 9, 30))
    today, tomorrow = await occurrences_of(c.item.id)
    moved = await items.move_to_tomorrow(session, [today.id], prefs, local(2026, 10, 7, 21, 0))
    assert [o.id for o in moved] == [tomorrow.id]
    assert today.status == "cancelled"
    assert tomorrow.status == "open"
    assert await reminders_of(tomorrow.id, "pending") == [(local(2026, 10, 8, 8, 55), "pending", 5, False)]
