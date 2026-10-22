"""Done/undone, snooze, delete/restore, reminders off, field setters, recurrence and move_to_tomorrow. An item's
time is its notification time (2026-10-07): reminders sit at the due itself, a snooze moves the due."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from dumpbot.ai.schemas import EditPatch
from dumpbot.db.models import Reminder
from dumpbot.domain import timeresolve
from dumpbot.domain.prefs import Prefs
from dumpbot.services import items, query
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
    assert await reminders_of(occ.id) == [(local(2026, 10, 6, 15, 0), "cancelled", 0, False)]


async def test_mark_done_keeps_sent_reminders(session, make, reminders_of, local, now, prefs):
    [c] = await make({"kind": "event", "title": "Врач", "when": {"time": "15:00"}, "lead": [60, 10]}, now=now)
    first = (await session.scalars(select(Reminder).where(Reminder.occurrence_id == c.occurrence.id))).all()
    early = min(first, key=lambda r: r.fire_at)
    early.status = "sent"
    await items.mark_done(session, c.occurrence.id, local(2026, 10, 6, 14, 10), prefs)
    assert c.occurrence.due_at == local(2026, 10, 6, 14, 50)  # "за час и за 10 минут до 15:00": due at the last
    assert await reminders_of(c.occurrence.id) == [
        (local(2026, 10, 6, 14, 0), "sent", 50, False),
        (local(2026, 10, 6, 14, 50), "cancelled", 0, False),
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
    assert await reminders_of(occs[1].id) == [(local(2026, 10, 19, 9, 0), "pending", 0, False)]
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
    assert await reminders_of(occ.id, "pending") == [(local(2026, 10, 7, 15, 0), "pending", 0, False)]


async def test_mark_undone_without_prefs_uses_stored_owner_prefs(session, make, reminders_of, local, now, prefs):
    await save_prefs(session, 42, Prefs(quiet_start=time(9, 0), quiet_end=time(11, 0)))
    [c] = await make({"kind": "task", "title": "Отчёт", "when": {"day": "+1"}}, now=now)  # tomorrow 10:00, auto
    await items.mark_done(session, c.occurrence.id, now, prefs)
    # the test's "now", not the wall clock: on the real 2026-10-07 after 10:00 the reminder would be in the past
    await items.mark_undone(session, c.occurrence.id, now=now)
    # the owner's quiet hours (09:00-11:00), not the defaults: the automatic 10:00 waits until 11:00
    assert await reminders_of(c.occurrence.id, "pending") == [(local(2026, 10, 7, 11, 0), "pending", 0, True)]


async def test_mark_undone_on_open_occurrence_is_noop(session, make, reminders_of, now):
    [c] = await make({"kind": "task", "title": "Отчёт", "when": {"time": "15:00"}}, now=now)
    before = await reminders_of(c.occurrence.id)
    await items.mark_undone(session, c.occurrence.id, prefs=Prefs(), now=now)
    assert await reminders_of(c.occurrence.id) == before


# --- snooze ----------------------------------------------------------------------------------------------------


async def test_snooze_minutes_moves_the_time(session, make, reminders_of, local, now, prefs):
    [c] = await make({"kind": "purchase", "title": "Купить наушники", "when": {"time": "lunch"}}, now=now)
    at = local(2026, 10, 6, 13, 1)  # the 13:00 reminder came, "⏰ 15 мин"
    reminder = await items.snooze(session, c.occurrence.id, minutes=15, prefs=prefs, now=at)
    assert reminder.id is not None
    assert (reminder.fire_at, reminder.auto, reminder.status) == (at + timedelta(minutes=15), False, "pending")
    assert (c.occurrence.due_at, c.occurrence.precision) == (local(2026, 10, 6, 13, 16), "slot")
    assert await reminders_of(c.occurrence.id) == [
        (local(2026, 10, 6, 13, 0), "cancelled", 0, False),
        (local(2026, 10, 6, 13, 16), "pending", 0, False),
    ]


async def test_snooze_shows_the_new_time_in_the_agenda(session, make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Позвонить маме", "when": {"time": "13:00"}}, now=now)
    await items.snooze(session, c.occurrence.id, minutes=60, prefs=prefs, now=local(2026, 10, 6, 13, 2))
    await session.flush()
    agenda = await query.agenda(session, prefs, local(2026, 10, 6, 13, 5))
    [row] = agenda.open
    assert (row.item_id, row.due_at) == (c.item.id, local(2026, 10, 6, 14, 2))
    await items.snooze(session, c.occurrence.id, tomorrow=True, prefs=prefs, now=local(2026, 10, 6, 14, 3))
    await session.flush()
    assert (await query.agenda(session, prefs, local(2026, 10, 6, 14, 5))).open == []
    [row] = (await query.agenda(session, prefs, local(2026, 10, 6, 14, 5), day_offset=1)).open
    assert row.due_at == local(2026, 10, 7, 14, 2)


async def test_snooze_tomorrow_uses_due_time_of_day(session, make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Позвонить маме", "when": {"time": "13:00"}}, now=now)
    reminder = await items.snooze(session, c.occurrence.id, tomorrow=True, prefs=prefs, now=local(2026, 10, 6, 13, 5))
    assert reminder.fire_at == local(2026, 10, 7, 13, 0)
    assert c.occurrence.due_at == local(2026, 10, 7, 13, 0)


async def test_snooze_to_this_evening_moves_the_time(session, make, reminders_of, local, prefs):
    # [🌙 Вечером] on the 10:00 reminder (iteration 3, §4): the item is due tonight at the evening slot
    [c] = await make({"kind": "task", "title": "Позвонить маме", "when": {"time": "10:00"}}, now=local(2026, 10, 6, 9))
    at = local(2026, 10, 6, 10, 2)
    reminder = await items.snooze(session, c.occurrence.id, at=timeresolve.tonight(at, prefs), prefs=prefs, now=at)
    assert (reminder.fire_at, reminder.auto, reminder.occurrence_id) == (local(2026, 10, 6, 19), False, c.occurrence.id)
    assert (c.occurrence.due_at, c.occurrence.status) == (local(2026, 10, 6, 19), "open")
    assert await reminders_of(c.occurrence.id) == [
        (local(2026, 10, 6, 10), "cancelled", 0, False),
        (local(2026, 10, 6, 19), "pending", 0, False),
    ]
    await session.flush()
    [row] = (await query.agenda(session, prefs, at)).open  # /today and the digests show it at 19:00
    assert row.due_at == local(2026, 10, 6, 19)


async def test_snooze_to_this_evening_never_moves_a_later_item_earlier(session, make, reminders_of, local, now, prefs):
    # snoozed to tonight, an item due tomorrow keeps its time: one more reminder tonight
    [c] = await make({"kind": "task", "title": "Встреча", "when": {"day": "+1", "time": "13:00"}}, now=now)
    reminder = await items.snooze(session, c.occurrence.id, at=local(2026, 10, 6, 19), prefs=prefs, now=now)
    assert reminder.fire_at == local(2026, 10, 6, 19)
    assert c.occurrence.due_at == local(2026, 10, 7, 13)
    pending = [fire for fire, *_ in await reminders_of(c.occurrence.id, "pending")]
    assert pending == [local(2026, 10, 6, 19), local(2026, 10, 7, 13)]


async def test_snooze_needs_exactly_one_target_ahead(session, make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Отчёт", "when": {"time": "15:00"}}, now=now)
    with pytest.raises(ValueError, match="ahead"):
        await items.snooze(session, c.occurrence.id, at=now, prefs=prefs, now=now)
    with pytest.raises(ValueError, match="exactly one"):
        await items.snooze(session, c.occurrence.id, minutes=15, at=local(2026, 10, 6, 19), prefs=prefs, now=now)


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
    assert await reminders_of(c.occurrence.id) == [(local(2026, 10, 6, 16, 0), "cancelled", 0, False)]
    await items.delete_item(session, c.item.id, now + timedelta(minutes=1))  # idempotent
    assert c.item.deleted_at == now

    item = await items.restore_item(session, c.item.id, now + timedelta(minutes=2), prefs)
    assert item.status == "open"
    assert item.deleted_at is None
    assert await reminders_of(c.occurrence.id, "pending") == [(local(2026, 10, 6, 16, 0), "pending", 0, False)]


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
    assert await reminders_of(occs[1].id) == [(local(2026, 10, 19, 9, 0), "pending", 0, False)]


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


def test_there_is_no_lead_setter_any_more():
    # "⏱ Напомнить за…" is gone (2026-10-07); "напомни за N" from a reply goes through apply_patch.
    assert not hasattr(items, "set_lead")


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
    assert await reminders_of(occs[1].id) == [(local(2026, 10, 8, 9, 0), "pending", 0, False)]

    # Younger than 12 h: still open (the evening digest shows it as not done).
    await items.ensure_recurring(session, prefs, local(2026, 10, 7, 20, 59))
    assert first.status == "open"
    # Older than 12 h and superseded: missed, its pending reminder cancelled.
    assert await items.ensure_recurring(session, prefs, local(2026, 10, 7, 21, 1)) == 0
    assert first.status == "missed"
    assert await reminders_of(first.id) == [(local(2026, 10, 7, 9, 0), "cancelled", 0, False)]
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
    assert await reminders_of(created[0].occurrence.id, "pending") == [(local(2026, 10, 7, 13, 0), "pending", 0, False)]
    assert await reminders_of(created[1].occurrence.id) == [
        (local(2026, 10, 6, 23, 0), "cancelled", 0, False),
        (local(2026, 10, 7, 23, 0), "pending", 0, False),
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
    assert await reminders_of(tomorrow.id, "pending") == [(local(2026, 10, 8, 9, 0), "pending", 0, False)]


# --- review fixes: snooze, stop, night cutoff, concurrency -------------------------------------------------------


async def test_snooze_leaves_exactly_one_notification(session, make, reminders_of, local, now, prefs):
    # "встреча завтра в 15:00, напомни за день и за 30 минут": the item's time is tomorrow 14:30. "+15 мин" on the
    # day-before reminder does not pull tomorrow's meeting to today: it reminds again at 15:15, 14:30 stays.
    spec = {"kind": "event", "title": "Встреча", "when": {"day": "+1", "time": "15:00"}, "lead": [1440, 30]}
    [c] = await make(spec, now=now)
    assert c.occurrence.due_at == local(2026, 10, 7, 14, 30)
    day_before = local(2026, 10, 6, 15, 0)
    await items.snooze(session, c.occurrence.id, minutes=15, prefs=prefs, now=day_before)
    assert (c.occurrence.due_at, c.item.lead_minutes) == (local(2026, 10, 7, 14, 30), [1440, 30])
    assert [f for f, *_ in await reminders_of(c.occurrence.id, "pending")] == [
        local(2026, 10, 6, 15, 15),
        local(2026, 10, 7, 14, 30),
    ]
    # Snoozed at its time, the item moves there and notifies once (the owner's decision: snooze moves the time).
    await items.snooze(session, c.occurrence.id, minutes=15, prefs=prefs, now=local(2026, 10, 7, 14, 30))
    assert c.occurrence.due_at == local(2026, 10, 7, 14, 45)
    assert c.item.lead_minutes is None
    assert await reminders_of(c.occurrence.id, "pending") == [(local(2026, 10, 7, 14, 45), "pending", 0, False)]


async def test_snooze_of_a_deadlines_evening_reminder_keeps_the_deadline(
    session, make, reminders_of, local, now, prefs
):
    # "до пятницы": Thu 19:00 and Fri 10:00. "⏰ 1 час" on Thursday's reminder: Thu 20:00, Friday stays.
    [c] = await make({"kind": "task", "title": "Отчёт", "when": {"day": "FR", "role": "deadline"}}, now=now)
    await items.snooze(session, c.occurrence.id, minutes=60, prefs=prefs, now=local(2026, 10, 8, 19, 0))
    assert c.occurrence.due_at == local(2026, 10, 9, 10, 0)
    assert [f for f, *_ in await reminders_of(c.occurrence.id, "pending")] == [
        local(2026, 10, 8, 20, 0),
        local(2026, 10, 9, 10, 0),
    ]
    # the deadline's own reminder snoozed: now the time itself moves
    await items.snooze(session, c.occurrence.id, minutes=60, prefs=prefs, now=local(2026, 10, 9, 10, 0))
    assert c.occurrence.due_at == local(2026, 10, 9, 11, 0)
    assert [f for f, *_ in await reminders_of(c.occurrence.id, "pending")] == [local(2026, 10, 9, 11, 0)]


async def test_snooze_of_a_span_moves_it_to_that_time(session, make, reminders_of, local, now, prefs):
    when = {"text": "на этой неделе", "period": "this_week"}
    [c] = await make({"kind": "task", "title": "Посмотреть Raspberry Pi", "when": when}, now=now)
    assert (c.occurrence.precision, c.item.period_kind) == ("period", "week")
    await items.snooze(session, c.occurrence.id, tomorrow=True, prefs=prefs, now=local(2026, 10, 9, 19, 1))
    assert (c.occurrence.due_at, c.occurrence.precision) == (local(2026, 10, 10, 19, 0), "exact")
    assert c.item.period_kind is None  # a plain dated item now, like "📅 Перенести на завтра"


async def test_snooze_cancels_reminders_up_to_the_snoozed_time(session, make, reminders_of, local, now, prefs):
    spec = {"kind": "event", "title": "Врач", "when": {"time": "15:00"}, "lead": [60, 5]}
    [c] = await make(spec, now=now)  # 14:00 and 14:55
    await items.snooze(session, c.occurrence.id, minutes=60, prefs=prefs, now=local(2026, 10, 6, 14, 0))
    assert await reminders_of(c.occurrence.id, "pending") == [(local(2026, 10, 6, 15, 0), "pending", 0, False)]


async def test_snooze_tomorrow_after_midnight_is_the_day_that_has_started(session, make, local, now, prefs):
    [c] = await make({"kind": "event", "title": "Созвон", "when": {"time": "23:55"}}, now=now)
    reminder = await items.snooze(session, c.occurrence.id, tomorrow=True, prefs=prefs, now=local(2026, 10, 7, 0, 10))
    assert reminder.fire_at == local(2026, 10, 7, 23, 55)  # Wednesday, not Thursday


async def test_snooze_tomorrow_after_midnight_never_fires_in_the_past(session, make, local, now, prefs):
    [c] = await make({"kind": "event", "title": "Созвон", "when": {"day": "+1", "time": "00:20"}}, now=now)
    reminder = await items.snooze(session, c.occurrence.id, tomorrow=True, prefs=prefs, now=local(2026, 10, 7, 0, 30))
    assert reminder.fire_at == local(2026, 10, 8, 0, 20)


async def test_snoozed_series_instance_survives_until_its_reminder(
    session, make, occurrences_of, reminders_of, local, now, prefs
):
    # Weekly MO 09:00: "📅 Завтра" at Mon 09:01 must still remind on Tuesday 09:00.
    [c] = await make(WEEKLY_MO_9, now=now)
    monday = c.occurrence
    await items.ensure_recurring(session, prefs, local(2026, 10, 12, 9, 0) + timedelta(seconds=30))
    reminder = await items.snooze(session, monday.id, tomorrow=True, prefs=prefs, now=local(2026, 10, 12, 9, 1))
    assert reminder.fire_at == local(2026, 10, 13, 9, 0)
    assert await items.ensure_recurring(session, prefs, local(2026, 10, 12, 21, 30)) == 0
    occs = await occurrences_of(c.item.id)
    assert [(o.due_at, o.status) for o in occs] == [
        (local(2026, 10, 13, 9, 0), "open"),  # moved with the snooze, not missed at Monday 21:00
        (local(2026, 10, 19, 9, 0), "open"),
    ]
    assert await reminders_of(monday.id, "pending") == [(local(2026, 10, 13, 9, 0), "pending", 0, False)]


async def test_superseded_instance_with_a_pending_snooze_stays_open(session, make, reminders_of, local, now, prefs):
    [c] = await make(DAILY_9, now=now)  # Wed 09:00
    first = c.occurrence
    await items.ensure_recurring(session, prefs, local(2026, 10, 7, 9, 30))
    # "⏰ 1 час" pressed late in the evening on the morning's reminder: the instance moves to 21:45.
    await items.snooze(session, first.id, minutes=60, prefs=prefs, now=local(2026, 10, 7, 20, 45))
    await items.ensure_recurring(session, prefs, local(2026, 10, 7, 21, 30))
    assert (first.status, first.due_at) == ("open", local(2026, 10, 7, 21, 45))
    assert await reminders_of(first.id, "pending") == [(local(2026, 10, 7, 21, 45), "pending", 0, False)]
    # It is due at 21:45 now: superseded as usual only 12 h after that.
    await items.ensure_recurring(session, prefs, local(2026, 10, 7, 21, 46))
    assert first.status == "open"
    await items.ensure_recurring(session, prefs, local(2026, 10, 8, 9, 46))
    assert first.status == "missed"


async def test_snooze_tomorrow_of_a_daily_instance_merges_into_tomorrows(
    session, make, occurrences_of, reminders_of, local, now, prefs
):
    [c] = await make(DAILY_9, now=now)  # Wed 09:00
    await items.ensure_recurring(session, prefs, local(2026, 10, 7, 9, 0) + timedelta(seconds=30))
    wednesday, thursday = await occurrences_of(c.item.id)
    reminder = await items.snooze(session, wednesday.id, tomorrow=True, prefs=prefs, now=local(2026, 10, 7, 9, 1))
    assert reminder.occurrence_id == thursday.id
    assert (wednesday.status, thursday.status) == ("cancelled", "open")
    pending = [r for o in await occurrences_of(c.item.id) for r in await reminders_of(o.id, "pending")]
    assert pending == [(local(2026, 10, 8, 9, 0), "pending", 0, False)]  # one reminder on Thursday, no duplicate


async def test_stop_recurrence_cancels_the_already_generated_next_instance(
    session, make, occurrences_of, reminders_of, local, prefs
):
    [c] = await make(DAILY_9, now=local(2026, 10, 6, 8, 0))  # Tue 09:00
    await items.ensure_recurring(session, prefs, local(2026, 10, 6, 9, 1))  # Wed 09:00 exists now
    await items.stop_recurrence(session, c.item.id, local(2026, 10, 6, 12, 0))
    today, tomorrow = await occurrences_of(c.item.id)
    assert (today.status, tomorrow.status) == ("open", "cancelled")
    assert await reminders_of(tomorrow.id, "pending") == []
    assert await items.ensure_recurring(session, prefs, local(2026, 10, 9, 12, 0)) == 0
    assert today.status == "open"  # no later sibling: it waits in the overdue list until done


async def test_stop_recurrence_patch_cancels_the_next_instance_too(session, make, occurrences_of, local, prefs):
    [c] = await make(DAILY_9, now=local(2026, 10, 6, 8, 0))
    await items.ensure_recurring(session, prefs, local(2026, 10, 6, 9, 1))
    await items.apply_patch(session, c.item.id, EditPatch(action="stop_recurrence"), prefs, local(2026, 10, 6, 12, 0))
    # iteration 4, §2: a stop asked for by an edit also closes the instance already due
    assert [o.status for o in await occurrences_of(c.item.id)] == ["done", "cancelled"]


async def test_move_to_tomorrow_after_midnight_is_the_day_that_has_started(session, make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Позвонить", "when": {"time": "15:00"}}, now=now)
    [moved] = await items.move_to_tomorrow(session, [c.occurrence.id], prefs, local(2026, 10, 7, 0, 30))
    assert moved.due_at == local(2026, 10, 7, 15, 0)  # Wednesday, not Thursday


async def test_move_to_tomorrow_never_lands_in_the_past(session, make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Поздно", "when": {"day": "+1", "time": "00:15"}}, now=now)
    [moved] = await items.move_to_tomorrow(session, [c.occurrence.id], prefs, local(2026, 10, 7, 1, 0))
    assert moved.due_at == local(2026, 10, 8, 0, 15)


async def test_move_to_tomorrow_to_a_given_day(session, make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Вторник", "when": {"time": "15:00"}}, now=now)
    wed_8 = local(2026, 10, 7, 8, 0)
    [moved] = await items.move_to_tomorrow(session, [c.occurrence.id], prefs, wed_8, day=date(2026, 10, 7))
    assert moved.due_at == local(2026, 10, 7, 15, 0)
    [again] = await items.move_to_tomorrow(session, [c.occurrence.id], prefs, wed_8, day=date(2026, 10, 1))
    assert again.due_at == local(2026, 10, 8, 15, 0)  # still ahead: just one day later; ``day`` is for past dues


async def test_concurrent_snoozes_leave_one_pending_reminder(sessionmaker, make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Купить хлеб", "when": {"time": "11:00"}}, now=now)
    at = local(2026, 10, 6, 11, 0)
    async with sessionmaker() as first, sessionmaker() as second:
        await items.snooze(first, c.occurrence.id, minutes=15, prefs=prefs, now=at)  # holds the occurrence lock
        racing = asyncio.create_task(items.snooze(second, c.occurrence.id, minutes=15, prefs=prefs, now=at))
        await asyncio.sleep(0.3)  # the second tap reaches the lock
        await first.commit()
        await racing
        await second.commit()
    async with sessionmaker() as check:
        assert len(await items.pending_reminders(check, c.occurrence.id)) == 1
