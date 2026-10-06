"""Fuzzy time in the item services (docs/features/iteration-2.md §A): period columns and reminders on create, edits
("перенеси на следующую неделю", "когда-нибудь", an explicit date), done/undone/restore/lead/move re-planning, the
edit prompt snapshot. Reference time: Tue 2026-10-06 10:00 Europe/Warsaw (services conftest)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select

from dumpbot.ai.schemas import EditPatch, When
from dumpbot.db.models import Item, Occurrence
from dumpbot.services import items

WEEK = {"text": "на этой неделе", "period": "this_week"}
NEXT_WEEK = {"text": "на следующей неделе", "period": "next_week"}
SOMEDAY = {"text": "когда-нибудь", "period": "someday"}


async def _period_columns(session, item_id: int) -> tuple:
    stmt = select(Item.period_kind, Item.period_start, Item.period_end, Item.someday).where(Item.id == item_id)
    return tuple((await session.execute(stmt)).one())


async def _pending(reminders_of, occ_id: int) -> list[tuple]:
    return [(fire, auto) for fire, _, _, auto in await reminders_of(occ_id, "pending")]


# --- create --------------------------------------------------------------------------------------------------------


async def test_this_week_item_keeps_its_span_and_the_friday_reminder(session, make, local, reminders_of):
    [c] = await make({"kind": "task", "title": "Посмотреть Raspberry Pi", "when": WEEK}, now=local(2026, 10, 6, 13, 36))

    assert await _period_columns(session, c.item.id) == ("week", date(2026, 10, 5), date(2026, 10, 11), False)
    assert (c.occurrence.precision, c.occurrence.due_at) == ("period", local(2026, 10, 9, 19, 0))
    assert await reminders_of(c.occurrence.id) == [(local(2026, 10, 9, 19, 0), "pending", 0, True)]
    assert c.item.when_spec == {"text": "на этой неделе", "period": "this_week", "role": "event"}


async def test_next_week_item_gets_both_reminders(session, make, local, now, reminders_of):
    [c] = await make({"kind": "task", "title": "Поменять шины", "when": NEXT_WEEK}, now=now)
    assert await _period_columns(session, c.item.id) == ("week", date(2026, 10, 12), date(2026, 10, 18), False)
    assert await _pending(reminders_of, c.occurrence.id) == [
        (local(2026, 10, 12, 10, 0), True),
        (local(2026, 10, 16, 19, 0), True),
    ]


async def test_season_item(session, make, now, local, reminders_of):
    [c] = await make(
        {"kind": "idea", "title": "Сплавиться по реке", "when": {"text": "летом", "period": "summer"}}, now=now
    )
    assert await _period_columns(session, c.item.id) == ("season", date(2027, 6, 1), date(2027, 8, 31), False)
    assert await _pending(reminders_of, c.occurrence.id) == [
        (local(2027, 6, 1, 10, 0), True),
        (local(2027, 8, 31, 19, 0), True),
    ]


async def test_soon_item(session, make, now, local, reminders_of):
    [c] = await make(
        {"kind": "task", "title": "Позвонить Пете", "when": {"text": "на днях", "period": "soon"}}, now=now
    )
    assert await _period_columns(session, c.item.id) == ("soon", date(2026, 10, 6), date(2026, 10, 8), False)
    assert (c.occurrence.precision, c.occurrence.due_at) == ("soon", local(2026, 10, 8, 19, 0))
    assert await _pending(reminders_of, c.occurrence.id) == [(local(2026, 10, 8, 19, 0), True)]


async def test_someday_task_has_no_occurrence_and_no_default_reminder(session, make, now, occurrences_of):
    [c] = await make({"kind": "task", "title": "Выучить испанский", "when": SOMEDAY}, now=now)
    assert (c.occurrence, c.reminders, c.resolution.precision) == (None, [], "someday")
    assert await _period_columns(session, c.item.id) == ("someday", None, None, True)
    assert await occurrences_of(c.item.id) == []


async def test_deadline_end_of_week_is_a_plain_dated_item(session, make, now, local, reminders_of):
    when = {"text": "до конца недели", "period": "this_week", "role": "deadline"}
    [c] = await make({"kind": "task", "title": "Сдать отчёт", "when": when}, now=now)
    assert await _period_columns(session, c.item.id) == (None, None, None, False)
    assert (c.occurrence.precision, c.occurrence.due_at) == ("date", local(2026, 10, 9, 10, 0))
    assert await _pending(reminders_of, c.occurrence.id) == [
        (local(2026, 10, 8, 19, 0), True),
        (local(2026, 10, 9, 10, 0), True),
    ]


# --- edits ---------------------------------------------------------------------------------------------------------


async def test_reply_moves_a_dated_task_to_next_week(session, prefs, make, now, local, reminders_of):
    """ "перенеси на следующую неделю" on a card: the span is stored and both reminders planned from it."""
    [c] = await make({"kind": "task", "title": "Поменять шины", "when": {"text": "завтра", "day": "+1"}}, now=now)
    result = await items.apply_patch(session, c.item.id, EditPatch(when=When(**NEXT_WEEK)), prefs, now)
    await session.commit()

    assert result.occurrence.id == c.occurrence.id
    assert (result.occurrence.precision, result.occurrence.due_at) == ("period", local(2026, 10, 16, 19, 0))
    assert [(r.fire_at, r.auto) for r in result.reminders] == [
        (local(2026, 10, 12, 10, 0), True),
        (local(2026, 10, 16, 19, 0), True),
    ]
    assert await _period_columns(session, c.item.id) == ("week", date(2026, 10, 12), date(2026, 10, 18), False)
    old = [r for r in await reminders_of(c.occurrence.id) if r[1] == "cancelled"]
    assert [fire for fire, *_ in old] == [local(2026, 10, 7, 10, 0)]


async def test_explicit_date_turns_a_period_item_into_a_dated_one(session, prefs, make, now, local, reminders_of):
    [c] = await make({"kind": "task", "title": "Шины", "when": WEEK}, now=now)
    when = When(text="на завтра 15:00", day="+1", time="15:00")
    result = await items.apply_patch(session, c.item.id, EditPatch(when=when), prefs, now)
    await session.commit()

    assert (result.occurrence.precision, result.occurrence.due_at) == ("exact", local(2026, 10, 7, 15, 0))
    assert await _period_columns(session, c.item.id) == (None, None, None, False)
    assert await _pending(reminders_of, c.occurrence.id) == [(local(2026, 10, 7, 14, 55), False)]


async def test_someday_edit_drops_dates_and_reminders_then_a_period_brings_them_back(
    session, prefs, make, now, local, reminders_of, occurrences_of
):
    [c] = await make({"kind": "task", "title": "Выучить испанский", "when": {"text": "завтра", "day": "+1"}}, now=now)
    result = await items.apply_patch(session, c.item.id, EditPatch(when=When(**SOMEDAY)), prefs, now)
    await session.commit()

    assert result.reminders == [] and result.item.status == "open"
    assert await _period_columns(session, c.item.id) == ("someday", None, None, True)
    assert [o.status for o in await occurrences_of(c.item.id)] == ["cancelled"]
    assert [status for _, status, _, _ in await reminders_of(c.occurrence.id)] == ["cancelled"]
    assert result.item.when_spec["period"] == "someday"
    snap = await items.item_snapshot(session, c.item.id, prefs, now)
    assert (snap["someday"], snap["due_local"]) == (True, None)  # not the cancelled occurrence's date

    result = await items.apply_patch(session, c.item.id, EditPatch(when=When(**NEXT_WEEK)), prefs, now)
    await session.commit()
    assert await _period_columns(session, c.item.id) == ("week", date(2026, 10, 12), date(2026, 10, 18), False)
    assert (result.occurrence.status, result.occurrence.precision) == ("open", "period")
    assert [r.fire_at for r in result.reminders] == [local(2026, 10, 12, 10, 0), local(2026, 10, 16, 19, 0)]


async def test_someday_edit_stops_a_series(session, prefs, make, now, occurrences_of):
    spec = {"kind": "task", "title": "Зарядка", "when": {"text": "каждый день в 9", "time": "09:00"}}
    [c] = await make({**spec, "rrule": "FREQ=DAILY"}, now=now)
    await items.apply_patch(session, c.item.id, EditPatch(when=When(**SOMEDAY)), prefs, now)
    await session.commit()

    item = await session.get(Item, c.item.id)
    assert (item.rec_active, item.someday) == (False, True)
    assert all(o.status == "cancelled" for o in await occurrences_of(c.item.id))
    assert await items.ensure_recurring(session, prefs, now) == 0


async def test_someday_idea_turned_into_a_task_gets_no_default_reminder(session, prefs, make, now, occurrences_of):
    [c] = await make({"kind": "idea", "title": "Выучить испанский", "when": SOMEDAY}, now=now)
    result = await items.apply_patch(session, c.item.id, EditPatch(kind="task"), prefs, now)
    await session.commit()
    assert result.item.kind == "task" and result.occurrence is None
    assert await occurrences_of(c.item.id) == []


async def test_recurrence_makes_a_someday_item_a_dated_series(session, prefs, make, now, local):
    [c] = await make({"kind": "task", "title": "Испанский", "when": SOMEDAY}, now=now)
    patch = EditPatch(rrule="FREQ=WEEKLY;BYDAY=MO", when=When(text="по понедельникам в 19", time="19:00"))
    result = await items.apply_patch(session, c.item.id, patch, prefs, now)
    await session.commit()
    assert await _period_columns(session, c.item.id) == (None, None, None, False)
    assert result.item.rec_active and result.occurrence.due_at == local(2026, 10, 12, 19, 0)


async def test_series_instance_moved_to_next_week_is_a_plain_date(session, prefs, make, now, local):
    spec = {"kind": "task", "title": "Зарядка", "when": {"text": "каждый день в 9", "time": "09:00"}}
    [c] = await make({**spec, "rrule": "FREQ=DAILY"}, now=now)
    result = await items.apply_patch(session, c.item.id, EditPatch(when=When(**NEXT_WEEK)), prefs, now)
    await session.commit()
    assert await _period_columns(session, c.item.id) == (None, None, None, False)
    moved = await session.scalar(
        select(Occurrence).where(Occurrence.item_id == c.item.id, Occurrence.due_at == local(2026, 10, 16, 19, 0))
    )
    assert moved is not None and moved.precision == "date"
    assert result.item.rec_active


# --- lifecycle re-plans from the span ------------------------------------------------------------------------------


async def test_done_then_undone_replans_both_reminders(session, prefs, make, now, local, reminders_of):
    [c] = await make({"kind": "task", "title": "Шины", "when": NEXT_WEEK}, now=now)
    await items.mark_done(session, c.occurrence.id, now, prefs)
    await session.commit()
    assert await _pending(reminders_of, c.occurrence.id) == []
    assert (await session.get(Item, c.item.id)).status == "done"

    await items.mark_undone(session, c.occurrence.id, prefs=prefs, now=local(2026, 10, 7, 12, 0))
    await session.commit()
    assert await _pending(reminders_of, c.occurrence.id) == [
        (local(2026, 10, 12, 10, 0), True),
        (local(2026, 10, 16, 19, 0), True),
    ]


async def test_undone_inside_the_span_skips_the_first_day_reminder(session, prefs, make, now, local, reminders_of):
    [c] = await make({"kind": "task", "title": "Шины", "when": NEXT_WEEK}, now=now)
    await items.mark_done(session, c.occurrence.id, now, prefs)
    await items.mark_undone(session, c.occurrence.id, prefs=prefs, now=local(2026, 10, 13, 12, 0))
    await session.commit()
    assert await _pending(reminders_of, c.occurrence.id) == [(local(2026, 10, 16, 19, 0), True)]


async def test_lead_change_keeps_the_span_reminders(session, prefs, make, now, local):
    [c] = await make({"kind": "task", "title": "Шины", "when": NEXT_WEEK}, now=now)
    result = await items.set_lead(session, c.item.id, [30], prefs, now)
    await session.commit()
    assert [r.fire_at for r in result.reminders] == [local(2026, 10, 12, 10, 0), local(2026, 10, 16, 19, 0)]


async def test_restore_brings_back_both_reminders(session, prefs, make, now, local, reminders_of):
    [c] = await make({"kind": "task", "title": "Шины", "when": NEXT_WEEK}, now=now)
    await items.delete_item(session, c.item.id, now)
    await items.restore_item(session, c.item.id, now, prefs)
    await session.commit()
    assert await _pending(reminders_of, c.occurrence.id) == [
        (local(2026, 10, 12, 10, 0), True),
        (local(2026, 10, 16, 19, 0), True),
    ]


async def test_move_to_tomorrow_makes_an_overdue_period_item_a_plain_date(
    session, prefs, make, now, local, reminders_of
):
    [c] = await make({"kind": "task", "title": "Шины", "when": WEEK}, now=now)
    monday_evening = local(2026, 10, 12, 21, 0)  # the week is over, Friday 19:00 was not done
    [moved] = await items.move_to_tomorrow(session, [c.occurrence.id], prefs, monday_evening)
    await session.commit()
    assert (moved.precision, moved.due_at) == ("date", local(2026, 10, 13, 19, 0))
    assert await _period_columns(session, c.item.id) == (None, None, None, False)
    assert await _pending(reminders_of, moved.id) == [(local(2026, 10, 13, 19, 0), True)]


async def test_snapshot_tells_the_model_about_the_span(session, prefs, make, now):
    [week] = await make({"kind": "task", "title": "Шины", "when": WEEK}, now=now)
    [someday] = await make({"kind": "task", "title": "Испанский", "when": SOMEDAY}, now=now)
    snap = await items.item_snapshot(session, week.item.id, prefs, now)
    assert snap["period"] == "2026-10-05..2026-10-11" and "someday" not in snap
    assert snap["due_local"] == "2026-10-09 19:00"
    snap = await items.item_snapshot(session, someday.item.id, prefs, now)
    assert snap["someday"] is True and snap["due_local"] is None and "period" not in snap
