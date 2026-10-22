"""apply_patch: every EditPatch action and the usual combinations. An item's time is its notification time
(2026-10-07): a new time notifies at itself, "за N" moves the notification before the stated time."""

from __future__ import annotations

from datetime import datetime, time

import pytest

from dumpbot.ai.schemas import EditPatch, When
from dumpbot.services import items, query

DAILY_9 = {"kind": "task", "title": "Зарядка", "rrule": "FREQ=DAILY", "when": {"time": "09:00"}}
WEEKLY_MO_9 = {"kind": "task", "title": "Вынести мусор", "rrule": "FREQ=WEEKLY;BYDAY=MO", "when": {"time": "09:00"}}
MEETING = {"kind": "event", "title": "Встреча с Олегом", "when": {"time": "15:00"}}


def patch(**kwargs) -> EditPatch:
    return EditPatch.model_validate(kwargs)


def fires(result) -> list[tuple[datetime, int, bool]]:
    return [(r.fire_at, r.lead_minutes, r.auto) for r in result.reminders]


# --- when / lead -----------------------------------------------------------------------------------------------


async def test_reschedule_moves_current_occurrence(session, make, reminders_of, local, now, prefs):
    [c] = await make(MEETING, now=now)
    result = await items.apply_patch(
        session, c.item.id, patch(when={"text": "завтра в 16", "day": "+1", "time": "16:00"}), prefs, now
    )
    assert result.deleted is False
    assert result.warnings == []
    assert result.occurrence.id == c.occurrence.id
    assert (result.occurrence.due_at, result.occurrence.precision) == (local(2026, 10, 7, 16, 0), "exact")
    assert fires(result) == [(local(2026, 10, 7, 16, 0), 0, False)]
    assert await reminders_of(c.occurrence.id) == [
        (local(2026, 10, 6, 15, 0), "cancelled", 0, False),
        (local(2026, 10, 7, 16, 0), "pending", 0, False),
    ]
    assert c.item.when_spec["time"] == "16:00"


async def test_time_only_keeps_a_later_date(session, make, local, now, prefs):
    # Friday's meeting, "перенеси на 15:00" without the date: stays on Friday, not today.
    [c] = await make({**MEETING, "when": {"day": "FR", "time": "13:00"}}, now=now)
    assert c.occurrence.due_at == local(2026, 10, 9, 13, 0)
    result = await items.apply_patch(session, c.item.id, patch(when={"text": "на 15:00", "time": "15:00"}), prefs, now)
    assert result.occurrence.due_at == local(2026, 10, 9, 15, 0)
    assert result.warnings == []
    assert fires(result) == [(local(2026, 10, 9, 15, 0), 0, False)]


async def test_time_only_on_todays_item_uses_the_usual_rule(session, make, local, now, prefs):
    [c] = await make(MEETING, now=now)  # today 15:00; 09:00 has passed at 10:00 -> tomorrow
    result = await items.apply_patch(session, c.item.id, patch(when={"time": "09:00"}), prefs, now)
    assert result.occurrence.due_at == local(2026, 10, 7, 9, 0)


async def test_reschedule_creates_occurrence_for_item_without_due(session, make, local, now, prefs):
    [c] = await make({"kind": "note", "title": "Идея для поста"}, now=now)
    result = await items.apply_patch(session, c.item.id, patch(when={"day": "+2"}), prefs, now)
    assert (result.occurrence.due_at, result.occurrence.precision) == (local(2026, 10, 8, 10, 0), "date")
    assert fires(result) == [(local(2026, 10, 8, 10, 0), 0, True)]


async def test_reschedule_with_lead(session, make, local, now, prefs):
    # "перенеси на 18, напомни за полчаса": the item's time is 17:30.
    [c] = await make(MEETING, now=now)
    result = await items.apply_patch(session, c.item.id, patch(when={"time": "18:00"}, lead=[30]), prefs, now)
    assert c.item.lead_minutes == [30]
    assert result.occurrence.due_at == local(2026, 10, 6, 17, 30)
    assert fires(result) == [(local(2026, 10, 6, 17, 30), 0, False)]
    assert c.item.title == "Встреча с Олегом (18:00)"  # the time it is at, now that the card shows 17:30


async def test_lead_without_when_moves_the_notification(session, make, local, now, prefs):
    # "напомни за 15 минут" as a reply: the notification (the item's time) moves 15 minutes before 15:00.
    [c] = await make(MEETING, now=now)
    result = await items.apply_patch(session, c.item.id, patch(lead=[15]), prefs, now)
    assert (result.occurrence.id, result.occurrence.due_at) == (c.occurrence.id, local(2026, 10, 6, 14, 45))
    assert c.item.lead_minutes == [15]
    assert fires(result) == [(local(2026, 10, 6, 14, 45), 0, False)]
    # The card and /today show 14:45 now: the title keeps the meeting's own time.
    assert c.item.title == "Встреча с Олегом (15:00)"
    # Another lead counts from the stated 15:00 again, not from 14:45.
    result = await items.apply_patch(session, c.item.id, patch(lead=[60]), prefs, now)
    assert result.occurrence.due_at == local(2026, 10, 6, 14, 0)
    assert c.item.lead_minutes == [60]
    assert c.item.title == "Встреча с Олегом (15:00)"


async def test_several_leads_from_a_reply(session, make, reminders_of, local, now, prefs):
    [c] = await make({**MEETING, "when": {"day": "FR", "time": "15:00"}}, now=now)
    result = await items.apply_patch(session, c.item.id, patch(lead=[60, 1440]), prefs, now)
    assert result.occurrence.due_at == local(2026, 10, 9, 14, 0)  # the last notification, on the meeting's day
    assert [r.fire_at for r in result.reminders] == [local(2026, 10, 8, 15, 0), local(2026, 10, 9, 14, 0)]
    assert c.item.title == "Встреча с Олегом (15:00)"


async def test_a_new_time_without_a_lead_is_the_notification_time(session, make, local, now, prefs):
    # "Встреча (15:00)" notified at 14:30; "перенеси на 17": 17:00 is when it notifies now, the lead goes.
    [c] = await make({**MEETING, "title": "Встреча (15:00)", "lead": [30]}, now=now)
    assert c.occurrence.due_at == local(2026, 10, 6, 14, 30)
    result = await items.apply_patch(session, c.item.id, patch(when={"time": "17:00"}), prefs, now)
    assert result.occurrence.due_at == local(2026, 10, 6, 17, 0)
    assert c.item.lead_minutes is None
    assert fires(result) == [(local(2026, 10, 6, 17, 0), 0, False)]
    assert c.item.title == "Встреча"  # "(15:00)" named the old time


async def test_lead_on_a_series_moves_every_instance_ahead(session, make, occurrences_of, local, now, prefs):
    [c] = await make(DAILY_9, now=now)  # Wed 09:00
    result = await items.apply_patch(session, c.item.id, patch(lead=[15]), prefs, now)
    assert result.occurrence.due_at == local(2026, 10, 7, 8, 45)
    assert c.item.lead_minutes == [15]
    await items.mark_done(session, c.occurrence.id, local(2026, 10, 7, 8, 50), prefs)
    assert [(o.due_at, o.status) for o in await occurrences_of(c.item.id)] == [
        (local(2026, 10, 7, 8, 45), "done"),
        (local(2026, 10, 8, 8, 45), "open"),  # the rule still says 09:00
    ]
    assert c.item.rec_time_local == time(9, 0)


async def test_relative_reschedule(session, make, local, now, prefs):
    [c] = await make(MEETING, now=now)
    result = await items.apply_patch(session, c.item.id, patch(when={"in_min": 120}), prefs, now)
    assert (result.occurrence.due_at, result.occurrence.precision) == (local(2026, 10, 6, 12, 0), "relative")
    assert fires(result) == [(local(2026, 10, 6, 12, 0), 0, False)]


async def test_bad_when_only_warns(session, make, reminders_of, local, now, prefs):
    [c] = await make(MEETING, now=now)
    result = await items.apply_patch(session, c.item.id, patch(when={"day": "02-30"}), prefs, now)
    assert result.warnings == ["не понял дату или время"]
    assert result.occurrence.due_at == local(2026, 10, 6, 15, 0)
    assert await reminders_of(c.occurrence.id) == [(local(2026, 10, 6, 15, 0), "pending", 0, False)]


async def test_past_date_keeps_date_without_reminders(session, make, local, now, prefs):
    [c] = await make(MEETING, now=now)
    result = await items.apply_patch(session, c.item.id, patch(when={"day": "2026-09-01", "time": "10:00"}), prefs, now)
    assert result.occurrence.due_at == local(2026, 9, 1, 10, 0)
    assert result.warnings == ["дата в прошлом"]
    assert result.reminders == []
    assert c.item.needs_confirm is True


async def test_reschedule_reopens_done_item(session, make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Отчёт", "when": {"time": "12:00"}}, now=now)
    await items.mark_done(session, c.occurrence.id, now, prefs)
    result = await items.apply_patch(session, c.item.id, patch(when={"day": "+1", "time": "12:00"}), prefs, now)
    assert c.item.status == "open"
    assert result.occurrence.status == "open"
    assert result.occurrence.due_at == local(2026, 10, 7, 12, 0)


# --- actions ---------------------------------------------------------------------------------------------------


async def test_done_and_undone(session, make, local, now, prefs):
    [c] = await make(MEETING, now=now)
    result = await items.apply_patch(session, c.item.id, patch(action="done"), prefs, now)
    assert c.item.status == "done"
    assert result.occurrence.status == "done"
    assert result.reminders == []

    result = await items.apply_patch(session, c.item.id, patch(action="undone"), prefs, now)
    assert c.item.status == "open"
    assert result.occurrence.status == "open"
    assert fires(result) == [(local(2026, 10, 6, 15, 0), 0, False)]


async def test_done_and_undone_for_item_without_occurrence(session, make, now, prefs):
    [c] = await make({"kind": "note", "title": "Заметка"}, now=now)
    await items.apply_patch(session, c.item.id, patch(action="done"), prefs, now)
    assert c.item.status == "done"
    await items.apply_patch(session, c.item.id, patch(action="undone"), prefs, now)
    assert c.item.status == "open"


async def test_done_on_series_creates_next(session, make, occurrences_of, local, now, prefs):
    [c] = await make(WEEKLY_MO_9, now=now)
    result = await items.apply_patch(session, c.item.id, patch(action="done"), prefs, local(2026, 10, 12, 9, 30))
    assert c.item.status == "open"
    assert (result.occurrence.due_at, result.occurrence.status) == (local(2026, 10, 19, 9, 0), "open")
    assert fires(result) == [(local(2026, 10, 19, 9, 0), 0, False)]
    assert len(await occurrences_of(c.item.id)) == 2


async def test_done_with_new_time_moves_next_instance(session, make, local, now, prefs):
    [c] = await make(WEEKLY_MO_9, now=now)
    result = await items.apply_patch(
        session, c.item.id, patch(action="done", when={"day": "FR", "time": "09:00"}), prefs, local(2026, 10, 12, 9, 30)
    )
    assert c.occurrence.status == "done"
    assert result.occurrence.due_at == local(2026, 10, 16, 9, 0)
    assert c.item.rrule == "FREQ=WEEKLY;BYDAY=MO"


async def test_delete(session, make, reminders_of, now, prefs):
    [c] = await make(MEETING, now=now)
    result = await items.apply_patch(session, c.item.id, patch(action="delete", title="игнор"), prefs, now)
    assert result.deleted is True
    assert result.item.status == "deleted"
    assert result.item.title == "Встреча с Олегом"
    assert result.reminders == []
    assert [s for _, s, _, _ in await reminders_of(c.occurrence.id)] == ["cancelled"]


async def test_no_reminder_then_new_time_turns_reminders_back_on(session, make, local, now, prefs):
    [c] = await make(MEETING, now=now)
    result = await items.apply_patch(session, c.item.id, patch(action="no_reminder"), prefs, now)
    assert result.reminders == []
    assert c.item.lead_minutes == []

    result = await items.apply_patch(session, c.item.id, patch(when={"time": "17:00"}), prefs, now)
    assert c.item.lead_minutes is None
    assert fires(result) == [(local(2026, 10, 6, 17, 0), 0, False)]


async def test_reschedule_and_no_reminder_together(session, make, local, now, prefs):
    [c] = await make(MEETING, now=now)
    result = await items.apply_patch(
        session, c.item.id, patch(action="no_reminder", when={"time": "17:00"}), prefs, now
    )
    assert result.occurrence.due_at == local(2026, 10, 6, 17, 0)
    assert result.reminders == []


async def test_stop_recurrence(session, make, now, prefs):
    [c] = await make(WEEKLY_MO_9, now=now)
    result = await items.apply_patch(
        session, c.item.id, patch(action="stop_recurrence", rrule="FREQ=DAILY"), prefs, now
    )
    assert c.item.rec_active is False
    assert c.item.rrule == "FREQ=WEEKLY;BYDAY=MO"
    assert result.occurrence.id == c.occurrence.id
    # iteration 4, §2: next Monday's instance (ahead) is cancelled too; nothing is left, the item is done
    assert (result.occurrence.status, result.item.status, result.reminders) == ("cancelled", "done", [])


# --- rrule -----------------------------------------------------------------------------------------------------


async def test_rrule_turns_one_off_into_series(session, make, occurrences_of, reminders_of, local, now, prefs):
    [c] = await make({"kind": "event", "title": "Планёрка", "when": {"day": "FR", "time": "15:00"}}, now=now)
    assert c.occurrence.due_at == local(2026, 10, 9, 15, 0)
    result = await items.apply_patch(session, c.item.id, patch(rrule="FREQ=WEEKLY"), prefs, now)
    item = c.item
    assert (item.rrule, item.rec_active, item.rec_tz) == ("FREQ=WEEKLY", True, "Europe/Warsaw")
    assert item.rec_dtstart_local == datetime(2026, 10, 9, 15, 0)  # the weekday of a bare WEEKLY
    assert item.rec_time_local == time(15, 0)
    assert result.occurrence.id == c.occurrence.id
    assert result.occurrence.due_at == local(2026, 10, 9, 15, 0)
    assert fires(result) == [(local(2026, 10, 9, 15, 0), 0, False)]

    await items.mark_done(session, c.occurrence.id, local(2026, 10, 9, 15, 30), prefs)
    nxt = (await occurrences_of(item.id))[-1]
    assert (nxt.due_at, nxt.precision) == (local(2026, 10, 16, 15, 0), "exact")
    assert await reminders_of(nxt.id) == [(local(2026, 10, 16, 15, 0), "pending", 0, False)]


async def test_rrule_of_an_item_with_a_lead_repeats_the_stated_time(session, make, occurrences_of, local, now, prefs):
    # "Планёрка (15:00)" notified at 14:45, then "повторяй каждую неделю": the series is at 15:00, each due 14:45.
    spec = {"kind": "event", "title": "Планёрка (15:00)", "when": {"day": "FR", "time": "15:00"}, "lead": [15]}
    [c] = await make(spec, now=now)
    assert c.occurrence.due_at == local(2026, 10, 9, 14, 45)
    result = await items.apply_patch(session, c.item.id, patch(rrule="FREQ=WEEKLY"), prefs, now)
    assert (c.item.rec_time_local, c.item.lead_minutes) == (time(15, 0), [15])
    assert result.occurrence.due_at == local(2026, 10, 9, 14, 45)
    await items.mark_done(session, c.occurrence.id, local(2026, 10, 9, 14, 50), prefs)
    assert (await occurrences_of(c.item.id))[-1].due_at == local(2026, 10, 16, 14, 45)


async def test_rrule_with_time(session, make, reminders_of, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Полить цветы"}, now=now)  # default 19:00 today
    result = await items.apply_patch(
        session,
        c.item.id,
        patch(rrule="FREQ=WEEKLY;BYDAY=TU", when={"text": "по вторникам в 10", "time": "10:00"}),
        prefs,
        now,
    )
    assert c.item.rrule == "FREQ=WEEKLY;BYDAY=TU"
    assert c.item.when_spec["time"] == "10:00"
    assert (result.occurrence.id, result.occurrence.due_at) == (c.occurrence.id, local(2026, 10, 13, 10, 0))
    assert result.occurrence.precision == "exact"
    assert await reminders_of(c.occurrence.id) == [
        (local(2026, 10, 6, 19, 0), "cancelled", 0, True),
        (local(2026, 10, 13, 10, 0), "pending", 0, False),
    ]


async def test_rrule_without_time_on_default_item_stays_default(session, make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Полить цветы"}, now=now)
    result = await items.apply_patch(session, c.item.id, patch(rrule="FREQ=DAILY"), prefs, now)
    assert (result.occurrence.due_at, result.occurrence.precision) == (local(2026, 10, 6, 19, 0), "default")
    assert fires(result) == [(local(2026, 10, 6, 19, 0), 0, True)]


async def test_rrule_replacement_drops_old_instances(session, make, occurrences_of, local, now, prefs):
    [c] = await make(DAILY_9, now=now)
    later = local(2026, 10, 7, 10, 0)
    await items.ensure_recurring(session, prefs, later)  # Oct 7 (overdue) + Oct 8
    result = await items.apply_patch(session, c.item.id, patch(rrule="FREQ=WEEKLY;BYDAY=FR"), prefs, later)
    assert c.item.rrule == "FREQ=WEEKLY;BYDAY=FR"
    assert result.occurrence.due_at == local(2026, 10, 9, 9, 0)
    assert [(o.due_at, o.status) for o in await occurrences_of(c.item.id)] == [
        (local(2026, 10, 8, 9, 0), "cancelled"),
        (local(2026, 10, 9, 9, 0), "open"),
    ]


async def test_done_and_rrule_on_overdue_one_off(session, make, occurrences_of, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Полить цветы", "when": {"day": "MO", "time": "15:00"}}, now=now)
    wednesday = local(2026, 10, 14, 10, 0)  # the Monday 12 Oct instance is overdue
    result = await items.apply_patch(session, c.item.id, patch(action="done", rrule="FREQ=WEEKLY"), prefs, wednesday)
    assert c.item.status == "open"
    assert c.item.rec_active is True
    assert [(o.due_at, o.status) for o in await occurrences_of(c.item.id)] == [
        (local(2026, 10, 12, 15, 0), "done"),
        (local(2026, 10, 19, 15, 0), "open"),
    ]
    assert result.occurrence.due_at == local(2026, 10, 19, 15, 0)


async def test_done_ahead_of_time_and_rrule_starts_after_it(session, make, occurrences_of, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Полить цветы", "when": {"day": "FR", "time": "15:00"}}, now=now)
    result = await items.apply_patch(session, c.item.id, patch(action="done", rrule="FREQ=WEEKLY"), prefs, now)
    assert [(o.due_at, o.status) for o in await occurrences_of(c.item.id)] == [
        (local(2026, 10, 9, 15, 0), "done"),
        (local(2026, 10, 16, 15, 0), "open"),
    ]
    assert fires(result) == [(local(2026, 10, 16, 15, 0), 0, False)]


async def test_invalid_rrule_only_warns(session, make, local, now, prefs):
    [c] = await make(MEETING, now=now)
    result = await items.apply_patch(session, c.item.id, patch(rrule="FREQ=DAILY;BYSECOND=5"), prefs, now)
    assert result.warnings == ["не понял правило повтора"]
    assert c.item.rrule is None
    assert result.occurrence.due_at == local(2026, 10, 6, 15, 0)


# --- fields ----------------------------------------------------------------------------------------------------


async def test_kind_to_non_actionable_keeps_reminders(session, make, now, prefs):
    [c] = await make(MEETING, now=now)
    before = [r.id for r in await items.pending_reminders(session, c.occurrence.id)]
    result = await items.apply_patch(session, c.item.id, patch(kind="note"), prefs, now)
    assert c.item.kind == "note"
    assert [r.id for r in result.reminders] == before


async def test_task_without_time_to_idea_drops_the_default_reminder(
    session, make, reminders_of, occurrences_of, local, now, prefs
):
    # «Открыть кофейню» filed as a task without a time (today 19:00), then «это идея»: no 19:00 ping, not overdue.
    [c] = await make({"kind": "task", "title": "Открыть кофейню"}, now=now)
    assert (c.occurrence.precision, len(c.reminders)) == ("default", 1)

    result = await items.apply_patch(session, c.item.id, patch(kind="idea"), prefs, now)

    assert (c.item.kind, c.item.status) == ("idea", "open")
    [occ] = await occurrences_of(c.item.id)
    assert occ.status == "cancelled"
    assert await reminders_of(occ.id) == [(local(2026, 10, 6, 19, 0), "cancelled", 0, True)]
    assert result.reminders == []
    agenda = await query.agenda(session, prefs, local(2026, 10, 8, 10, 0))
    assert (agenda.open, agenda.overdue) == ([], [])

    # Back to a task: a fresh default reminder.
    again = await items.apply_patch(session, c.item.id, patch(kind="task"), prefs, now)
    assert (again.occurrence.status, again.occurrence.precision) == ("open", "default")
    assert fires(again) == [(local(2026, 10, 6, 19, 0), 0, True)]


async def test_kind_change_with_a_new_time_keeps_it(session, make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Открыть кофейню"}, now=now)
    result = await items.apply_patch(
        session, c.item.id, patch(kind="idea", when={"day": "+1", "time": "12:00"}), prefs, now
    )
    assert (result.occurrence.status, result.occurrence.due_at) == ("open", local(2026, 10, 7, 12, 0))


async def test_kind_to_actionable_gets_default_reminder(session, make, local, now, prefs):
    [c] = await make({"kind": "note", "title": "Купить молоко"}, now=now)
    result = await items.apply_patch(session, c.item.id, patch(kind="purchase"), prefs, now)
    assert c.item.kind == "purchase"
    assert (result.occurrence.due_at, result.occurrence.precision) == (local(2026, 10, 6, 19, 0), "default")
    assert fires(result) == [(local(2026, 10, 6, 19, 0), 0, True)]


async def test_title_and_tags_do_not_touch_reminders(session, make, now, prefs):
    [c] = await make({**MEETING, "tags": ["работа", "олег"]}, now=now)
    before = [r.id for r in await items.pending_reminders(session, c.occurrence.id)]
    result = await items.apply_patch(
        session,
        c.item.id,
        patch(title="Встреча с Олегом и Ирой", tags_add=["#Ира", "работа"], tags_remove=["олег"]),
        prefs,
        now,
    )
    assert c.item.title == "Встреча с Олегом и Ирой"
    assert c.item.tags == ["работа", "ира"]
    assert [r.id for r in result.reminders] == before


# --- series instances ------------------------------------------------------------------------------------------


async def test_when_on_series_moves_only_this_instance(session, make, occurrences_of, local, now, prefs):
    [c] = await make(DAILY_9, now=now)
    result = await items.apply_patch(session, c.item.id, patch(when={"day": "+1", "time": "11:00"}), prefs, now)
    assert result.occurrence.due_at == local(2026, 10, 7, 11, 0)
    assert c.item.when_spec["time"] == "09:00"
    assert c.item.rec_time_local == time(9, 0)
    await items.ensure_recurring(session, prefs, local(2026, 10, 7, 11, 30))
    assert [o.due_at for o in await occurrences_of(c.item.id)] == [local(2026, 10, 7, 11, 0), local(2026, 10, 8, 9, 0)]


async def test_when_onto_existing_instance_merges(session, make, occurrences_of, local, now, prefs):
    [c] = await make(DAILY_9, now=now)
    later = local(2026, 10, 7, 10, 0)
    await items.ensure_recurring(session, prefs, later)
    today, tomorrow = await occurrences_of(c.item.id)
    result = await items.apply_patch(session, c.item.id, patch(when={"day": "+1", "time": "09:00"}), prefs, later)
    assert result.occurrence.id == tomorrow.id
    assert today.status == "cancelled"
    assert fires(result) == [(local(2026, 10, 8, 9, 0), 0, False)]


async def test_attributes_stay_loaded_after_edits(session, make, now, prefs):
    """Views read results right after the call; an expired column would need a lazy load (fails under asyncio)."""
    [c] = await make(MEETING, now=now)
    item_id = c.item.id
    session.expunge_all()
    result = await items.apply_patch(
        session, item_id, patch(title="Новая встреча", when={"time": "17:00"}, tags_add=["x"]), prefs, now
    )
    await items.set_kind(session, item_id, "task")
    await items.mark_done(session, result.occurrence.id, now, prefs)
    await session.flush()
    for obj in (result.item, result.occurrence, *result.reminders):
        for column in type(obj).__table__.columns:
            if column.key != "search_tsv":
                getattr(obj, column.key)


async def test_unknown_item(session, prefs, now):
    with pytest.raises(items.NotFoundError):
        await items.apply_patch(session, 424242, patch(action="done"), prefs, now)


# --- iteration 4, §4: a move is another day -------------------------------------------------------------------------


async def test_moved_is_another_day_not_another_hour(session, make, now, prefs):
    """The stale-items check counts moves to another day: «не в 10, а в 11» is no move, «на пятницу» is; a first
    date for an item that had none (the bot's 19:00) is no move either."""
    [c] = await make({"kind": "event", "title": "Врач", "when": {"day": "+1", "time": "10:00"}}, now=now)
    hour = await items.apply_patch(session, c.item.id, EditPatch(when=When(text="в 11", time="11:00")), prefs, now)
    assert hour.moved is False
    day = await items.apply_patch(session, c.item.id, EditPatch(when=When(text="в пятницу", day="FR")), prefs, now)
    assert day.moved is True
    title = await items.apply_patch(session, c.item.id, EditPatch(title="Врач-терапевт"), prefs, now)
    assert title.moved is False

    [u] = await make({"kind": "task", "title": "Отчёт"}, now=now)
    first = await items.apply_patch(session, u.item.id, EditPatch(when=When(text="завтра", day="+1")), prefs, now)
    assert first.moved is False
