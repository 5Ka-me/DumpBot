"""Checklists and related items in the item services (docs/features/iteration-2.md §C): steps from an extraction and
from the "split" call, their reminders (only steps with a time), toggling, the parent closing by its last step and
opening again, a parent done / deleted / restored by hand, links to related items (foreign ids dropped).
Reference time: Tue 2026-10-06 10:00 Europe/Warsaw (services conftest)."""

from __future__ import annotations

from sqlalchemy import select

from dumpbot.ai.schemas import EditPatch, Extraction, StepSpec, When
from dumpbot.db.models import Item, ItemLink, Occurrence
from dumpbot.services import items

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
    assert await reminders_of(occ.id) == [(local(2026, 10, 8, 14, 55), "pending", 5, False)]
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


async def test_a_parent_done_by_hand_closes_its_open_steps(session, make, now, prefs, reminders_of, occurrences_of):
    [parent] = await make(MOVE, now=now)
    steps = await _steps(session, parent.item.id)
    items.take_parent_events(session)
    await items.mark_done(session, parent.occurrence.id, now, prefs)
    await session.commit()
    assert items.take_parent_events(session).closed == []  # closed by hand: no 🎉
    assert [(await _fresh(session, Item, s.id)).status for s in steps] == ["done", "done", "done"]
    [timed_occ] = await occurrences_of(steps[1].id)
    assert timed_occ.status == "done" and await reminders_of(timed_occ.id, "pending") == []
    # undone of the parent leaves the steps as they are
    await items.mark_undone(session, parent.occurrence.id, prefs=prefs, now=now)
    await session.commit()
    assert [(await _fresh(session, Item, s.id)).status for s in steps] == ["done", "done", "done"]


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
