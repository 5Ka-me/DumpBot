"""Checklists and related items in the item services (docs/features/iteration-2.md §C): steps from an extraction and
from the "split" call, their reminders (only steps with a time), toggling, the parent closing by its last step and
opening again, a parent done / deleted / restored by hand, links to related items (foreign ids dropped).
Reference time: Tue 2026-10-06 10:00 Europe/Warsaw (services conftest)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import select

from dumpbot.ai.schemas import EditPatch, Extraction, StepSpec, When
from dumpbot.db.models import Item, ItemLink, Occurrence
from dumpbot.domain.prefs import Prefs
from dumpbot.services import items, query

MOVE = {
    "kind": "task",
    "title": "Переезд",
    "when": {"text": "в субботу", "day": "SA"},
    "steps": [
        {"title": "Упаковать вещи"},
        {"title": "Заказать машину", "when": {"text": "в четверг в 15", "day": "TH", "time": "15:00"}},
        {"title": "Отдать ключи"},
    ],
}


async def _steps(session, parent_id: int) -> list[Item]:
    stmt = select(Item).where(Item.parent_id == parent_id).order_by(Item.position)
    return list((await session.scalars(stmt.execution_options(populate_existing=True))).all())


async def _fresh(session, model, id_: int):
    return await session.get(model, id_, populate_existing=True)


# --- creation ------------------------------------------------------------------------------------------------------


async def test_steps_of_an_extraction_become_child_items(session, make, now, local, occurrences_of, reminders_of):
    [parent] = await make(MOVE, now=now)  # the steps are not in the result: they show in the parent's card

    steps = await _steps(session, parent.item.id)
    assert [(s.position, s.title, s.kind, s.status) for s in steps] == [
        (1, "Упаковать вещи", "task", "open"),
        (2, "Заказать машину", "task", "open"),
        (3, "Отдать ключи", "task", "open"),
    ]
    assert {s.raw_message_id for s in steps} == {parent.item.raw_message_id}
    # without a time of its own a step has no occurrence and no 19:00 reminder; with one it is a normal reminder
    assert await occurrences_of(steps[0].id) == [] and await occurrences_of(steps[2].id) == []
    [occ] = await occurrences_of(steps[1].id)
    assert (occ.due_at, occ.precision) == (local(2026, 10, 8, 15, 0), "exact")
    assert await reminders_of(occ.id) == [(local(2026, 10, 8, 15, 0), "pending", 0, False)]
    # the parent keeps its own time
    assert (parent.occurrence.due_at, parent.occurrence.precision) == (local(2026, 10, 10, 10, 0), "date")


async def test_a_parent_without_time_keeps_its_default_reminder_but_its_steps_get_none(session, make, now, local):
    [parent] = await make({"kind": "task", "title": "Ремонт", "steps": [{"title": "Купить краску"}]}, now=now)
    assert (parent.occurrence.due_at, parent.occurrence.precision) == (local(2026, 10, 6, 19, 0), "default")
    [step] = await _steps(session, parent.item.id)
    assert await session.scalar(select(Occurrence.id).where(Occurrence.item_id == step.id)) is None


async def test_a_series_gets_no_checklist(session, make, now):
    spec = {
        "kind": "task",
        "title": "Уборка",
        "when": {"text": "каждую субботу", "day": "SA"},
        "rrule": "FREQ=WEEKLY;BYDAY=SA",
        "steps": [{"title": "Пропылесосить"}, {"title": "Помыть пол"}],
    }
    [created] = await make(spec, now=now)
    assert created.item.rec_active and await _steps(session, created.item.id) == []


async def test_steps_are_capped_and_blank_titles_skipped(session, make, now, prefs):
    steps = [{"title": f"Шаг {n}"} for n in range(1, 11)]  # the schema keeps the first 8
    [parent] = await make({"kind": "task", "title": "Проект", "steps": steps}, now=now)
    titles = [s.title for s in await _steps(session, parent.item.id)]
    assert titles == [f"Шаг {n}" for n in range(1, 9)]

    [task] = await make({"kind": "task", "title": "Ремонт"}, now=now)
    specs = [StepSpec(title="  "), StepSpec(title=" Купить   краску ")]
    created = await items.add_steps(session, task.item.id, specs, prefs, now)
    assert [(c.item.position, c.item.title) for c in created] == [(1, "Купить краску")]


async def test_add_steps_once_and_only_to_an_open_top_level_task(session, make, now, prefs):
    [task, series, done] = await make(
        {"kind": "task", "title": "Переезд"},
        {"kind": "task", "title": "Спорт", "when": {"text": "каждый день"}, "rrule": "FREQ=DAILY"},
        {"kind": "task", "title": "Сделано"},
        now=now,
    )
    await items.apply_patch(session, done.item.id, EditPatch(action="done"), prefs, now)
    specs = [StepSpec(title="Упаковать вещи"), StepSpec(title="Заказать машину", when=When(day="+1", time="15:00"))]

    created = await items.add_steps(session, task.item.id, specs, prefs, now, model="m", prompt_version="v3")
    await session.commit()
    assert [(c.item.position, c.item.title, c.item.ai_model) for c in created] == [
        (1, "Упаковать вещи", "m"),
        (2, "Заказать машину", "m"),
    ]
    assert created[0].occurrence is None and created[1].occurrence is not None
    # already split, a series, a done item, a step: nothing added
    assert await items.add_steps(session, task.item.id, specs, prefs, now) == []
    assert await items.add_steps(session, series.item.id, specs, prefs, now) == []
    assert await items.add_steps(session, done.item.id, specs, prefs, now) == []
    assert await items.add_steps(session, created[0].item.id, specs, prefs, now) == []
    assert len(await _steps(session, task.item.id)) == 2


# --- toggling and the parent ---------------------------------------------------------------------------------------


async def test_the_last_step_closes_the_parent_and_unticking_reopens_it(session, make, now, local, prefs, reminders_of):
    [parent] = await make(MOVE, now=now)
    steps = await _steps(session, parent.item.id)
    items.take_parent_events(session)

    for step in steps[:2]:
        await items.toggle_step(session, step.id, done=True, prefs=prefs, now=now)
    await session.commit()
    events = items.take_parent_events(session)
    assert (events.closed, events.changed) == ([], {parent.item.id})
    assert (await _fresh(session, Item, parent.item.id)).status == "open"
    # the timed step's reminder is gone with it
    [timed_occ] = (await session.scalars(select(Occurrence).where(Occurrence.item_id == steps[1].id))).all()
    assert (await _fresh(session, Occurrence, timed_occ.id)).status == "done"
    assert [r[1] for r in await reminders_of(timed_occ.id)] == ["cancelled"]

    await items.toggle_step(session, steps[2].id, done=True, prefs=prefs, now=now)
    await session.commit()
    assert items.take_parent_events(session).closed == [parent.item.id]
    closed = await _fresh(session, Item, parent.item.id)
    assert (closed.status, closed.done_at) == ("done", now)
    assert (await _fresh(session, Occurrence, parent.occurrence.id)).status == "done"
    assert [r[1] for r in await reminders_of(parent.occurrence.id)] == ["cancelled"]

    # unticking a step of the closed parent opens it again (its reminder too, the date is still ahead)
    await items.toggle_step(session, steps[0].id, done=False, prefs=prefs, now=now)
    await session.commit()
    events = items.take_parent_events(session)
    assert (events.closed, events.changed) == ([], {parent.item.id})
    assert (await _fresh(session, Item, steps[0].id)).status == "open"
    assert (await _fresh(session, Item, parent.item.id)).status == "open"
    assert (await _fresh(session, Occurrence, parent.occurrence.id)).status == "open"
    assert await reminders_of(parent.occurrence.id, "pending") == [(local(2026, 10, 10, 10, 0), "pending", 0, True)]


async def test_a_stale_toggle_changes_nothing(session, make, now, prefs):
    [parent] = await make(MOVE, now=now)
    first = (await _steps(session, parent.item.id))[0]
    await items.toggle_step(session, first.id, done=False, prefs=prefs, now=now)  # already open
    await items.toggle_step(session, first.id, done=True, prefs=prefs, now=now)
    await items.toggle_step(session, first.id, done=True, prefs=prefs, now=now)  # pressed twice
    await session.commit()
    assert (await _fresh(session, Item, first.id)).status == "done"
    assert (await _fresh(session, Item, parent.item.id)).status == "open"


async def test_a_timed_step_done_from_its_reminder_can_close_the_parent(session, make, now, prefs, occurrences_of):
    [parent] = await make(
        {
            "kind": "task",
            "title": "Переезд",
            "steps": [{"title": "Заказать машину", "when": {"text": "завтра в 15", "day": "+1", "time": "15:00"}}],
        },
        now=now,
    )
    [step] = await _steps(session, parent.item.id)
    [occ] = await occurrences_of(step.id)
    items.take_parent_events(session)
    await items.mark_done(session, occ.id, now, prefs)
    await session.commit()
    assert items.take_parent_events(session).closed == [parent.item.id]
    assert (await _fresh(session, Item, parent.item.id)).status == "done"

    await items.mark_undone(session, occ.id, prefs=prefs, now=now)  # "↩️ Не сделано" on the reminder
    await session.commit()
    assert (await _fresh(session, Item, parent.item.id)).status == "open"


async def test_a_step_done_by_an_edit_closes_the_parent(session, make, now, prefs):
    [parent] = await make({"kind": "task", "title": "Переезд", "steps": [{"title": "Упаковать вещи"}]}, now=now)
    [step] = await _steps(session, parent.item.id)
    items.take_parent_events(session)
    await items.apply_patch(session, step.id, EditPatch(action="done"), prefs, now)
    await session.commit()
    assert items.take_parent_events(session).closed == [parent.item.id]
    await items.apply_patch(session, step.id, EditPatch(action="undone"), prefs, now)
    await session.commit()
    assert (await _fresh(session, Item, parent.item.id)).status == "open"


async def test_a_parent_done_by_hand_closes_its_open_steps_and_its_undo_reopens_them(
    session, make, now, local, prefs, reminders_of, occurrences_of
):
    [parent] = await make(MOVE, now=now)
    steps = await _steps(session, parent.item.id)
    await items.toggle_step(session, steps[0].id, done=True, prefs=prefs, now=now + timedelta(minutes=1))
    await session.commit()
    items.take_parent_events(session)
    await items.mark_done(session, parent.occurrence.id, now + timedelta(minutes=2), prefs)  # «Готово» by mistake
    await session.commit()
    assert items.take_parent_events(session).closed == []  # closed by hand: no 🎉
    assert [(await _fresh(session, Item, s.id)).status for s in steps] == ["done", "done", "done"]
    [timed_occ] = await occurrences_of(steps[1].id)
    assert timed_occ.status == "done" and await reminders_of(timed_occ.id, "pending") == []

    # "↩️ Не сделано" of the parent takes back exactly the ticks its «Готово» made (the timed step's reminder too)
    await items.mark_undone(session, parent.occurrence.id, prefs=prefs, now=now + timedelta(minutes=3))
    await session.commit()
    assert [(await _fresh(session, Item, s.id)).status for s in steps] == ["done", "open", "open"]
    assert (await _fresh(session, Item, parent.item.id)).status == "open"
    assert (await _fresh(session, Occurrence, timed_occ.id)).status == "open"
    assert await reminders_of(timed_occ.id, "pending") == [(local(2026, 10, 8, 15, 0), "pending", 0, False)]
    events = items.take_parent_events(session)
    assert (events.closed, events.changed) == ([], {parent.item.id})


async def test_undoing_a_parent_closed_by_its_last_tick_takes_that_tick_back(session, make, now, prefs):
    [parent] = await make({"kind": "task", "title": "Переезд", "steps": [{"title": "A"}, {"title": "B"}]}, now=now)
    a, b = await _steps(session, parent.item.id)
    await items.toggle_step(session, a.id, done=True, prefs=prefs, now=now + timedelta(minutes=1))
    await items.toggle_step(session, b.id, done=True, prefs=prefs, now=now + timedelta(minutes=2))
    await session.commit()
    assert (await _fresh(session, Item, parent.item.id)).status == "done"

    await items.apply_patch(session, parent.item.id, EditPatch(action="undone"), prefs, now + timedelta(minutes=3))
    await session.commit()
    assert [(await _fresh(session, Item, s.id)).status for s in (a, b)] == ["done", "open"]
    assert (await _fresh(session, Item, parent.item.id)).status == "open"


# --- delete / restore / a new time of a step: the parent follows ---------------------------------------------------


async def test_deleting_the_last_open_step_closes_the_parent(session, make, now, prefs, reminders_of):
    [parent] = await make(MOVE, now=now)
    steps = await _steps(session, parent.item.id)
    for step in steps[:2]:
        await items.toggle_step(session, step.id, done=True, prefs=prefs, now=now)
    await session.commit()
    items.take_parent_events(session)

    await items.delete_item(session, steps[2].id, now)  # the Mini App's 🗑 of the only open step
    await session.commit()
    events = items.take_parent_events(session)
    assert (events.closed, events.changed) == ([parent.item.id], {parent.item.id})
    assert (await _fresh(session, Item, parent.item.id)).status == "done"
    assert await reminders_of(parent.occurrence.id, "pending") == []  # no Saturday reminder of a finished task


async def test_deleting_a_step_with_others_open_only_redraws_the_parent(session, make, now):
    [parent] = await make(MOVE, now=now)
    steps = await _steps(session, parent.item.id)
    items.take_parent_events(session)
    await items.apply_patch(session, steps[0].id, EditPatch(action="delete"), Prefs(tz="Europe/Warsaw"), now)
    await session.commit()
    events = items.take_parent_events(session)
    assert (events.closed, events.changed) == ([], {parent.item.id})
    assert (await _fresh(session, Item, parent.item.id)).status == "open"


async def test_restoring_an_open_step_reopens_the_closed_parent(session, make, now, local, prefs, reminders_of):
    [parent] = await make(
        {"kind": "task", "title": "Переезд", "when": {"day": "SA"}, "steps": [{"title": "A"}, {"title": "B"}]}, now=now
    )
    a, b = await _steps(session, parent.item.id)
    await items.delete_item(session, b.id, now)
    await items.toggle_step(session, a.id, done=True, prefs=prefs, now=now)
    await session.commit()
    assert (await _fresh(session, Item, parent.item.id)).status == "done"
    items.take_parent_events(session)

    restored = await items.restore_item(session, b.id, now, prefs)
    await session.commit()
    assert restored.status == "open"
    assert (await _fresh(session, Item, parent.item.id)).status == "open"
    assert await reminders_of(parent.occurrence.id, "pending") == [(local(2026, 10, 10, 10, 0), "pending", 0, True)]
    events = items.take_parent_events(session)
    assert (events.closed, events.changed) == ([], {parent.item.id})


async def test_restoring_a_step_of_a_deleted_parent_brings_the_parent_back(
    session, make, now, prefs, reminders_of, occurrences_of
):
    [parent] = await make(MOVE, now=now)
    steps = await _steps(session, parent.item.id)
    await items.delete_item(session, steps[2].id, now)  # deleted on its own first
    await session.commit()
    await items.delete_item(session, parent.item.id, now + timedelta(minutes=5))
    await session.commit()
    items.take_parent_events(session)

    # [↩️ Вернуть] on the deleted card of the timed step (an old reminder of it): the whole task comes back
    await items.restore_item(session, steps[1].id, now + timedelta(minutes=6), prefs)
    await session.commit()
    assert (await _fresh(session, Item, parent.item.id)).status == "open"
    assert [(await _fresh(session, Item, s.id)).status for s in steps] == ["open", "open", "deleted"]
    [timed_occ] = await occurrences_of(steps[1].id)
    assert len(await reminders_of(timed_occ.id, "pending")) == 1
    assert items.take_parent_events(session).changed == {parent.item.id}

    # a step deleted on its own before the parent: restoring it restores the parent and the step itself
    await items.delete_item(session, parent.item.id, now + timedelta(minutes=7))
    await session.commit()
    await items.restore_item(session, steps[2].id, now + timedelta(minutes=8), prefs)
    await session.commit()
    assert [(await _fresh(session, Item, s.id)).status for s in [parent.item, *steps]] == ["open"] * 4


async def test_a_new_time_for_a_ticked_step_reopens_the_closed_parent(session, make, now, local, prefs, reminders_of):
    [parent] = await make(MOVE, now=now)
    steps = await _steps(session, parent.item.id)
    for step in steps:
        await items.toggle_step(session, step.id, done=True, prefs=prefs, now=now)
    await session.commit()
    assert (await _fresh(session, Item, parent.item.id)).status == "done"
    items.take_parent_events(session)

    # «перенеси на завтра 12:00» in reply to the reminder of «Упаковать вещи»
    patch = EditPatch(when=When(text="завтра 12:00", day="+1", time="12:00"))
    result = await items.apply_patch(session, steps[0].id, patch, prefs, now)
    await session.commit()
    assert result.item.status == "open" and result.occurrence.due_at == local(2026, 10, 7, 12, 0)
    assert (await _fresh(session, Item, parent.item.id)).status == "open"
    assert await reminders_of(parent.occurrence.id, "pending") == [(local(2026, 10, 10, 10, 0), "pending", 0, True)]
    events = items.take_parent_events(session)
    assert (events.closed, events.changed) == ([], {parent.item.id})

    # "когда-нибудь" for a ticked step opens it (and the closed parent) as well
    await items.toggle_step(session, steps[0].id, done=True, prefs=prefs, now=now)
    await session.commit()
    assert (await _fresh(session, Item, parent.item.id)).status == "done"
    await items.apply_patch(
        session, steps[1].id, EditPatch(when=When(text="когда-нибудь", period="someday")), prefs, now
    )
    await session.commit()
    assert (await _fresh(session, Item, steps[1].id)).status == "open"
    assert (await _fresh(session, Item, parent.item.id)).status == "open"


async def test_a_checklist_never_becomes_a_series(session, make, now, prefs):
    [parent] = await make(MOVE, now=now)
    steps = await _steps(session, parent.item.id)
    weekly = EditPatch(rrule="FREQ=WEEKLY;BYDAY=SA", when=When(text="каждую субботу", day="SA"))

    result = await items.apply_patch(session, parent.item.id, weekly, prefs, now)
    await session.commit()
    assert result.warnings == [items.WARN_CHECKLIST_RRULE]
    assert not result.item.rec_active and result.item.rrule is None
    result = await items.apply_patch(session, steps[0].id, weekly, prefs, now)
    await session.commit()
    assert result.warnings == [items.WARN_STEP_RRULE] and not result.item.rec_active

    # every step ticked: the (one-off) parent closes as usual
    events = None
    for step in steps:
        await items.toggle_step(session, step.id, done=True, prefs=prefs, now=now)
    await session.commit()
    events = items.take_parent_events(session)
    assert events.closed == [parent.item.id]


async def test_removing_the_time_of_a_ticked_step_drops_it_everywhere(session, make, now, prefs):
    [parent] = await make(MOVE, now=now)
    steps = await _steps(session, parent.item.id)
    await items.toggle_step(session, steps[1].id, done=True, prefs=prefs, now=now)
    await session.commit()
    assert "Заказать машину" in [r.title for r in await query.recent(session)]

    await items.clear_time(session, steps[1].id)  # «Убрать время» in the Mini App
    await session.commit()
    [_, timed, _] = (await query.card_extras(session, await _fresh(session, Item, parent.item.id))).steps
    assert (timed.title, timed.done, timed.due_at) == ("Заказать машину", True, None)
    assert [r.title for r in await query.recent(session)] == ["Переезд"]
    # unticking it later just opens it, with no time
    await items.toggle_step(session, steps[1].id, done=False, prefs=prefs, now=now)
    await session.commit()
    assert (await _fresh(session, Item, steps[1].id)).status == "open"
    assert await items.current_occurrence(session, steps[1].id) is not None  # history only (cancelled)
    assert [r.title for r in await query.recent(session)] == ["Переезд"]


# --- concurrent taps -----------------------------------------------------------------------------------------------


async def _race(sessionmaker, change, parent_id: int, step_id: int, first_at, second_at):
    """Two sessions that loaded the step and its parent before either took the lock (as on_step does) make the same
    change; the second waits for the first's commit. Returns the parent events of each."""
    async with sessionmaker() as s1, sessionmaker() as s2:
        for s in (s1, s2):
            await s.get(Item, step_id)
            await s.get(Item, parent_id)
        await change(s1, first_at)
        second = asyncio.create_task(change(s2, second_at))
        await asyncio.sleep(0.2)
        assert not second.done()  # blocked by the first tap's lock on the parent row
        await s1.commit()
        await second
        await s2.commit()
        return items.take_parent_events(s1), items.take_parent_events(s2)


async def test_a_double_tap_on_the_last_step_closes_the_parent_once(session, sessionmaker, make, now, prefs):
    [parent] = await make({"kind": "task", "title": "Переезд", "steps": [{"title": "A"}, {"title": "B"}]}, now=now)
    a, b = await _steps(session, parent.item.id)
    await items.toggle_step(session, a.id, done=True, prefs=prefs, now=now)
    await session.commit()

    async def tap(s, at):
        await items.toggle_step(s, b.id, done=True, prefs=prefs, now=at)

    first_at, second_at = now + timedelta(seconds=1), now + timedelta(seconds=2)
    first, second = await _race(sessionmaker, tap, parent.item.id, b.id, first_at, second_at)
    assert (first.closed, second.closed) == ([parent.item.id], [])  # one 🎉
    assert (await _fresh(session, Item, parent.item.id)).done_at == first_at
    assert (await _fresh(session, Item, b.id)).done_at == first_at


async def test_a_double_tap_on_the_reminder_of_the_last_step_closes_the_parent_once(
    session, sessionmaker, make, now, prefs, occurrences_of
):
    timed = {"title": "Заказать машину", "when": {"text": "завтра в 15", "day": "+1", "time": "15:00"}}
    [parent] = await make({"kind": "task", "title": "Переезд", "steps": [timed]}, now=now)
    [step] = await _steps(session, parent.item.id)
    [occ] = await occurrences_of(step.id)

    async def tap(s, at):
        await s.get(Occurrence, occ.id)  # on_occ loads the occurrence first
        await items.mark_done(s, occ.id, at, prefs)

    first_at, second_at = now + timedelta(seconds=1), now + timedelta(seconds=2)
    first, second = await _race(sessionmaker, tap, parent.item.id, step.id, first_at, second_at)
    assert (first.closed, second.closed) == ([parent.item.id], [])
    assert (await _fresh(session, Occurrence, occ.id)).done_at == first_at


async def test_a_deleted_step_does_not_count_and_the_parent_takes_its_steps_along(
    session, make, now, prefs, reminders_of, occurrences_of
):
    [parent] = await make(MOVE, now=now)
    steps = await _steps(session, parent.item.id)
    await items.delete_item(session, steps[0].id, now)
    await session.commit()

    later = now.replace(minute=5)
    await items.delete_item(session, parent.item.id, later)
    await session.commit()
    assert [(await _fresh(session, Item, s.id)).status for s in steps] == ["deleted"] * 3
    [timed_occ] = await occurrences_of(steps[1].id)
    assert await reminders_of(timed_occ.id, "pending") == []

    await items.restore_item(session, parent.item.id, later, prefs)
    await session.commit()
    # the steps deleted with it come back (with their reminders), the one deleted before stays deleted
    assert [(await _fresh(session, Item, s.id)).status for s in steps] == ["deleted", "open", "open"]
    assert len(await reminders_of(timed_occ.id, "pending")) == 1

    for step in steps[1:]:
        await items.toggle_step(session, step.id, done=True, prefs=prefs, now=later)
    await session.commit()
    assert (await _fresh(session, Item, parent.item.id)).status == "done"


async def test_replacing_the_items_of_a_message_drops_their_steps(session, make, now, new_raw, prefs):
    raw = await new_raw("переезд, распиши по шагам")
    [parent] = await make(MOVE, now=now, raw=raw)
    steps = await _steps(session, parent.item.id)
    created = await items.replace_items_for_raw(
        session,
        raw.id,
        extraction=Extraction.model_validate({"items": [MOVE]}),
        source="text",
        source_text=None,
        source_meta=None,
        urls=[],
        prefs=prefs,
        now=now,
        model=None,
        prompt_version=None,
    )
    await session.commit()
    assert [(await _fresh(session, Item, s.id)).status for s in steps] == ["deleted"] * 3
    assert len(await _steps(session, created[0].item.id)) == 3


async def test_a_step_never_gets_the_default_reminder_on_a_kind_change(session, make, now, prefs, occurrences_of):
    [parent] = await make({"kind": "task", "title": "Переезд", "steps": [{"title": "Упаковать вещи"}]}, now=now)
    [step] = await _steps(session, parent.item.id)
    await items.apply_patch(session, step.id, EditPatch(kind="idea"), prefs, now)
    await items.apply_patch(session, step.id, EditPatch(kind="task"), prefs, now)
    await session.commit()
    assert await occurrences_of(step.id) == []


# --- related items -------------------------------------------------------------------------------------------------


async def test_related_links_keep_only_the_candidates_the_model_was_shown(session, make, now, local, prefs):
    [old, other] = await make(
        {"kind": "purchase", "title": "Купить наушники Sony"},
        {"kind": "note", "title": "Соседи шумят"},
        now=local(2026, 9, 12, 10, 0),
    )
    spec = {
        "kind": "purchase",
        "title": "Купить чехол для наушников Sony",
        "related": [old.item.id, other.item.id, 999999, old.item.id],
    }
    created = await items.create_items(
        session,
        raw=None,
        extraction=Extraction.model_validate({"items": [spec]}),
        source="text",
        source_text=None,
        source_meta=None,
        urls=[],
        prefs=prefs,
        now=now,
        model=None,
        prompt_version=None,
        related_allowed={old.item.id},
    )
    await session.commit()
    links = (await session.execute(select(ItemLink.item_id, ItemLink.related_id, ItemLink.kind))).all()
    assert [tuple(r) for r in links] == [(created[0].item.id, old.item.id, "similar")]


async def test_without_candidates_no_link_is_made(session, make, now):
    [old] = await make({"kind": "purchase", "title": "Купить наушники Sony"}, now=now)
    await make({"kind": "purchase", "title": "Купить чехол", "related": [old.item.id]}, now=now)
    assert (await session.execute(select(ItemLink))).all() == []


async def test_a_related_id_needs_a_common_subject_word(session, make, now, local, prefs):
    # the live case: the model linked «Прогуглить про визу в Черногорию» to «Покушать» (saved from "напомни через
    # час покушать"); only a subject word in the titles / details links two items, generic words never do
    [eat, headphones, plan] = await make(
        {"kind": "task", "title": "Покушать"},
        {"kind": "purchase", "title": "Купить наушники"},
        {"kind": "task", "title": "Купить билеты", "details": "Черногория, виза не нужна?"},
        now=local(2026, 10, 1, 10, 0),
        source_text="напомни через час покушать",
    )
    allowed = {eat.item.id, headphones.item.id, plan.item.id}
    specs = [
        {"kind": "task", "title": "Прогуглить про визу в Черногорию", "related": [eat.item.id, plan.item.id]},
        {"kind": "purchase", "title": "Купить наушники Sony", "related": [headphones.item.id, eat.item.id]},
        {"kind": "task", "title": "Купить хлеб", "details": "напомни через час", "related": [eat.item.id]},
    ]
    visa, sony, bread = await items.create_items(
        session,
        raw=None,
        extraction=Extraction.model_validate({"items": specs}),
        source="text",
        source_text="напомни через час",
        source_meta=None,
        urls=[],
        prefs=prefs,
        now=now,
        model=None,
        prompt_version=None,
        related_allowed=allowed,
    )
    await session.commit()
    links = (await session.execute(select(ItemLink.item_id, ItemLink.related_id).order_by(ItemLink.item_id))).all()
    assert [tuple(r) for r in links] == [(visa.item.id, plan.item.id), (sony.item.id, headphones.item.id)]
    assert bread.item.id not in {r.item_id for r in links}
