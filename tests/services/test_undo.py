"""services.undo: an edit applied by description and its [↩️ Отменить] (docs/features/iteration-3.md §3).

Every action of an EditPatch is applied with ``undo.apply`` and taken back with ``undo.undo_edit``: the item, its
checklist, every occurrence and every reminder must be exactly as before (``updated_at`` aside), whatever the edit
did — closed an instance of a series and made the next one, moved several instances, cancelled reminders, switched a
span or "когда-нибудь" on. A stale undo (changed since) and a second press change nothing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import select

from dumpbot.ai.schemas import EditPatch, StepSpec
from dumpbot.db.models import Item, Occurrence, RawMessage, Reminder
from dumpbot.services import ingest, items, undo

DAILY_9 = {"kind": "task", "title": "Зарядка", "rrule": "FREQ=DAILY", "when": {"time": "09:00"}}
MEETING = {"kind": "event", "title": "Встреча с Олегом", "when": {"time": "15:00"}}
TASK_17 = {"kind": "task", "title": "Написать документацию", "when": {"time": "17:00"}}


async def state(session, item_id: int) -> dict[str, Any]:
    """Everything an edit may change: the family's items (but ``updated_at``), all their occurrences and all their
    reminders, any status."""
    root = (await session.get(Item, item_id)).parent_id or item_id
    family = [root, *(await session.scalars(select(Item.id).where(Item.parent_id == root))).all()]
    rows = (
        await session.scalars(select(Item).where(Item.id.in_(family)).execution_options(populate_existing=True))
    ).all()
    occs = (
        await session.scalars(
            select(Occurrence).where(Occurrence.item_id.in_(family)).execution_options(populate_existing=True)
        )
    ).all()
    rems = (
        await session.scalars(
            select(Reminder)
            .join(Occurrence, Occurrence.id == Reminder.occurrence_id)
            .where(Occurrence.item_id.in_(family))
            .execution_options(populate_existing=True)
        )
    ).all()
    return {
        "items": {i.id: {f: getattr(i, f) for f in undo.ITEM_FIELDS} for i in rows},
        "occs": {o.id: (o.item_id, o.due_at, o.precision, o.status, o.done_at) for o in occs},
        "rems": {r.id: (r.occurrence_id, r.fire_at, r.lead_minutes, r.auto, r.status) for r in rems},
    }


@pytest.fixture
def edit(session, new_raw, prefs):
    """``raw, applied = await edit(item_id, now, action="done", …)``: the edit applied and its undo kept with a new raw
    message, committed."""

    async def _edit(item_id: int, now: datetime, **fields: Any) -> tuple[RawMessage, undo.Applied]:
        raw = await new_raw("правка")
        applied = await undo.apply(session, item_id, EditPatch.model_validate(fields), prefs, now)
        assert applied.snapshot is not None
        undo.keep(raw, item_id, applied.snapshot)
        await session.commit()
        return raw, applied

    return _edit


async def take_back(session, raw: RawMessage, item_id: int, now: datetime) -> str:
    outcome = await undo.undo_edit(session, raw.id, item_id, now)
    await session.commit()
    return outcome


async def check_round_trip(session, edit, item_id: int, now: datetime, **fields: Any) -> undo.Applied:
    """Apply, check that something changed, undo, check that everything is as before."""
    before = await state(session, item_id)
    raw, applied = await edit(item_id, now, **fields)
    assert applied.changed
    assert await state(session, item_id) != before
    assert await take_back(session, raw, item_id, now) == undo.UNDONE
    assert await state(session, item_id) == before
    return applied


# --- every action ------------------------------------------------------------------------------------------------


async def test_done_of_a_one_off_and_back(session, make, edit, now):
    [c] = await make(TASK_17, now=now)
    before = await state(session, c.item.id)
    raw, applied = await edit(c.item.id, now, action="done", target="документация")
    assert (c.item.status, applied.result.occurrence.status) == ("done", "done")
    assert [r[4] for r in (await state(session, c.item.id))["rems"].values()] == ["cancelled"]
    assert applied.title_before == "Написать документацию" and applied.steps_closed == 0

    assert await take_back(session, raw, c.item.id, now) == undo.UNDONE
    assert await state(session, c.item.id) == before  # open, done_at None, the 17:00 reminder pending again
    stored = undo.stored(await session.get(RawMessage, raw.id), c.item.id)
    assert stored is not None and stored["undone"] == now.isoformat()
    assert await take_back(session, raw, c.item.id, now) == undo.ALREADY  # a double tap


async def test_done_of_a_series_instance_drops_the_next_one(session, make, edit, local, now):
    [c] = await make(DAILY_9, now=now)
    before = await state(session, c.item.id)
    raw, applied = await edit(c.item.id, now, action="done")
    after = await state(session, c.item.id)
    assert len(after["occs"]) == 2 and applied.result.occurrence.due_at == local(2026, 10, 8, 9, 0)
    assert await take_back(session, raw, c.item.id, now) == undo.UNDONE
    assert await state(session, c.item.id) == before  # the next instance and its reminder are gone


async def test_delete_and_back(session, make, edit, now):
    [c] = await make(MEETING, now=now)
    applied = await check_round_trip(session, edit, c.item.id, now, action="delete")
    assert applied.result.deleted
    assert c.item.status == "open" and c.item.deleted_at is None


async def test_delete_of_a_parent_brings_back_its_steps(session, make, edit, now, prefs):
    [c] = await make({**TASK_17, "title": "Переезд"}, now=now)
    steps = await items.add_steps(
        session, c.item.id, [StepSpec(title="Упаковать"), StepSpec(title="Отвезти")], prefs, now
    )
    await items.toggle_step(session, steps[0].item.id, done=True, prefs=prefs, now=now)
    await session.commit()
    await check_round_trip(session, edit, c.item.id, now, action="delete")
    assert [s.item.status for s in steps] == ["done", "open"]


async def test_done_of_a_parent_reopens_only_the_steps_it_closed(session, make, edit, now, prefs):
    [c] = await make({**TASK_17, "title": "Переезд"}, now=now)
    specs = [StepSpec(title="Упаковать"), StepSpec(title="Отвезти"), StepSpec(title="Отдать ключи")]
    steps = await items.add_steps(session, c.item.id, specs, prefs, now)
    await items.toggle_step(session, steps[0].item.id, done=True, prefs=prefs, now=now)
    await session.commit()
    before = await state(session, c.item.id)
    raw, applied = await edit(c.item.id, now, action="done")
    assert applied.steps_closed == 2
    assert await take_back(session, raw, c.item.id, now) == undo.UNDONE
    assert await state(session, c.item.id) == before
    assert items.take_parent_events(session).changed == {c.item.id}  # the parent's cards are redrawn


async def test_done_of_the_last_step_closes_and_reopens_the_parent(session, make, edit, now, prefs):
    [c] = await make({**TASK_17, "title": "Переезд"}, now=now)
    steps = await items.add_steps(
        session, c.item.id, [StepSpec(title="Упаковать"), StepSpec(title="Отвезти")], prefs, now
    )
    await items.toggle_step(session, steps[0].item.id, done=True, prefs=prefs, now=now)
    await session.commit()
    items.take_parent_events(session)
    before = await state(session, steps[1].item.id)
    raw, _ = await edit(steps[1].item.id, now, action="done")
    assert c.item.status == "done"  # the last step closed the parent, its 17:00 reminder cancelled
    assert await take_back(session, raw, steps[1].item.id, now) == undo.UNDONE
    assert await state(session, steps[1].item.id) == before


async def test_undone_and_back(session, make, edit, now, prefs):
    [c] = await make(TASK_17, now=now)
    await items.mark_done(session, c.occurrence.id, now, prefs)
    await session.commit()
    await check_round_trip(session, edit, c.item.id, now, action="undone")
    assert c.item.status == "done"


async def test_new_time_and_back(session, make, edit, local, now):
    [c] = await make(MEETING, now=now)
    applied = await check_round_trip(
        session, edit, c.item.id, now, when={"text": "на завтра 16", "day": "+1", "time": "16:00"}
    )
    moved = applied.snapshot["occs"][str(c.occurrence.id)]
    assert applied.moved and moved["a"]["due_at"] == local(2026, 10, 7, 16, 0).isoformat()
    assert c.occurrence.due_at == local(2026, 10, 6, 15, 0) and c.item.when_spec["time"] == "15:00"


async def test_new_time_of_an_item_with_a_lead_and_back(session, make, edit, now):
    [c] = await make({**MEETING, "title": "Встреча с Олегом (15:00)", "lead": [30]}, now=now)
    await check_round_trip(session, edit, c.item.id, now, when={"text": "на 17", "time": "17:00"})
    assert (c.item.title, c.item.lead_minutes) == ("Встреча с Олегом (15:00)", [30])


async def test_lead_alone_and_back(session, make, edit, now):
    [c] = await make(MEETING, now=now)
    await check_round_trip(session, edit, c.item.id, now, lead=[60])
    assert (c.item.title, c.item.lead_minutes) == ("Встреча с Олегом", None)


async def test_new_lead_of_a_series_moves_instances_back_into_each_others_place(session, make, edit, local, now):
    """Two open instances moved a day earlier: the second takes the first one's old time. The undo puts the first
    back where the second sits now (UNIQUE item_id + due_at: the rows are parked first)."""
    [c] = await make(DAILY_9, now=now)
    second = Occurrence(item_id=c.item.id, due_at=local(2026, 10, 8, 9, 0), precision="exact", status="open")
    session.add(second)
    await session.flush()
    session.add(Reminder(occurrence_id=second.id, fire_at=second.due_at, status="pending"))
    await session.commit()
    before = await state(session, c.item.id)
    raw, _ = await edit(c.item.id, now, lead=[1440])
    dues = sorted(o[1] for o in (await state(session, c.item.id))["occs"].values())
    assert dues == [local(2026, 10, 6, 9, 0), local(2026, 10, 7, 9, 0)]
    assert await take_back(session, raw, c.item.id, now) == undo.UNDONE
    assert await state(session, c.item.id) == before


async def test_recurrence_on_and_back(session, make, edit, now):
    [c] = await make(TASK_17, now=now)
    await check_round_trip(session, edit, c.item.id, now, rrule="FREQ=WEEKLY;BYDAY=MO", when={"time": "09:00"})
    assert (c.item.rrule, c.item.rec_active, c.item.rec_dtstart_local) == (None, False, None)


async def test_stop_recurrence_and_back(session, make, edit, local, now, prefs):
    [c] = await make(DAILY_9, now=now)
    await items.ensure_recurring(session, prefs, local(2026, 10, 7, 9, 30))  # the next instance exists too
    await session.commit()
    await check_round_trip(session, edit, c.item.id, local(2026, 10, 7, 9, 30), action="stop_recurrence")
    assert c.item.rec_active is True


async def test_no_reminder_and_back(session, make, edit, now):
    meeting = {**MEETING, "title": "Встреча с Олегом (ср 15:00)", "when": {"day": "+1", "time": "15:00"}}
    [c] = await make({**meeting, "lead": [1440, 60]}, now=now)
    await check_round_trip(session, edit, c.item.id, now, action="no_reminder")
    assert c.item.lead_minutes == [1440, 60]


async def test_period_and_back(session, make, edit, now):
    [c] = await make(TASK_17, now=now)
    applied = await check_round_trip(
        session, edit, c.item.id, now, when={"text": "на следующей неделе", "period": "next_week"}
    )
    assert applied.moved
    assert (c.item.period_kind, c.item.period_start, c.item.period_end) == (None, None, None)


async def test_from_a_period_to_a_date_and_back(session, make, edit, now):
    [c] = await make({**TASK_17, "when": {"text": "на этой неделе", "period": "this_week"}}, now=now)
    await check_round_trip(session, edit, c.item.id, now, when={"text": "завтра", "day": "+1"})
    assert c.item.period_kind == "week"


async def test_someday_and_back(session, make, edit, now):
    [c] = await make(DAILY_9, now=now)
    await check_round_trip(session, edit, c.item.id, now, when={"text": "когда-нибудь", "period": "someday"})
    assert (c.item.someday, c.item.rec_active) == (False, True)


async def test_title_kind_tags_and_back(session, make, edit, now):
    [c] = await make({**TASK_17, "tags": ["работа"]}, now=now)
    await check_round_trip(session, edit, c.item.id, now, title="Дописать README", kind="idea", tags_add=["docs"])
    assert (c.item.title, c.item.kind, c.item.tags) == ("Написать документацию", "task", ["работа"])


async def test_kind_without_a_time_drops_the_default_reminder_and_back(session, make, edit, now):
    [c] = await make({"kind": "task", "title": "Подумать о докладе"}, now=now)  # 19:00 by default
    await check_round_trip(session, edit, c.item.id, now, kind="idea")


async def test_done_of_a_note_and_back(session, make, edit, now):
    [c] = await make({"kind": "note", "title": "Заметка"}, now=now)
    await check_round_trip(session, edit, c.item.id, now, action="done")


# --- stale, missing, nothing to undo -------------------------------------------------------------------------------


async def test_a_later_edit_makes_the_undo_stale(session, make, edit, now, prefs):
    [c] = await make(TASK_17, now=now)
    raw, _ = await edit(c.item.id, now, when={"day": "+1", "time": "10:00"})
    await items.set_title(session, c.item.id, "Написать документацию к API")
    await session.commit()
    changed = await state(session, c.item.id)
    assert await take_back(session, raw, c.item.id, now) == undo.STALE
    assert await state(session, c.item.id) == changed  # nothing touched
    assert undo.stored(await session.get(RawMessage, raw.id), c.item.id).get("undone") is None


async def test_a_reminder_that_went_off_makes_the_undo_stale(session, make, edit, now):
    [c] = await make(TASK_17, now=now)
    raw, applied = await edit(c.item.id, now, when={"in_min": 30, "text": "через полчаса"})
    [reminder] = applied.result.reminders
    reminder.status = "sent"
    await session.commit()
    assert await take_back(session, raw, c.item.id, now) == undo.STALE


async def test_a_new_instance_of_the_series_makes_the_undo_stale(session, make, edit, local, now, prefs):
    [c] = await make(DAILY_9, now=now)
    raw, _ = await edit(c.item.id, now, title="Зарядка утром")
    await items.ensure_recurring(session, prefs, local(2026, 10, 7, 9, 30))
    await session.commit()
    assert await take_back(session, raw, c.item.id, now) == undo.STALE


async def test_missing_or_garbage_undo(session, make, new_raw, now):
    [c] = await make(TASK_17, now=now)
    raw = await new_raw("правка")
    assert await take_back(session, raw, c.item.id, now) == undo.MISSING
    raw.raw_json = {ingest.UNDO_KEY: {str(c.item.id): {"v": 1, "item": c.item.id, "family": "x"}}}
    await session.commit()
    assert undo.stored(raw, c.item.id) is None
    assert await take_back(session, raw, c.item.id, now) == undo.MISSING
    assert await take_back(session, raw, 10**9, now) == undo.MISSING


async def test_an_edit_that_changes_nothing(session, make, now, prefs):
    [c] = await make(TASK_17, now=now)
    await items.mark_done(session, c.occurrence.id, now, prefs)
    applied = await undo.apply(session, c.item.id, EditPatch(action="done"), prefs, now)
    assert not applied.changed and not applied.moved


async def test_keep_holds_one_undo_per_item(session, make, edit, new_raw, now, prefs):
    a, b = await make(TASK_17, MEETING, now=now)
    raw = await new_raw("правка")
    raw.raw_json = {"_candidates": [a.item.id, b.item.id]}
    for item_id in (a.item.id, b.item.id):
        applied = await undo.apply(session, item_id, EditPatch(action="done"), prefs, now)
        undo.keep(raw, item_id, applied.snapshot)
    await session.commit()
    assert set(raw.raw_json[ingest.UNDO_KEY]) == {str(a.item.id), str(b.item.id)}
    assert raw.raw_json["_candidates"] == [a.item.id, b.item.id]
    assert await take_back(session, raw, b.item.id, now) == undo.UNDONE
    assert (a.item.status, b.item.status) == ("done", "open")
