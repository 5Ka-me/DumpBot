"""Read side of related items and checklists (docs/features/iteration-2.md §C): RELATED CANDIDATES (OR of the
message's words, not the same message, 365 days, best rank first), card extras, and the list rules — a step shows in
top-level lists only with a time of its own, "(из «Переезд»)", a parent counts "2/5".
Reference time: Tue 2026-10-06 10:00 Europe/Warsaw (services conftest)."""

from __future__ import annotations

from sqlalchemy import select, update

from dumpbot.ai.schemas import EditPatch, QuerySpec, When
from dumpbot.db.models import Item, ItemLink
from dumpbot.services import items, query

MOVE = {
    "kind": "task",
    "title": "Переезд",
    "when": {"text": "в субботу", "day": "SA"},
    "steps": [
        {"title": "Упаковать вещи"},
        {"title": "Заказать машину", "when": {"text": "сегодня в 15", "day": "+0", "time": "15:00"}},
        {"title": "Отдать ключи"},
    ],
}


async def _steps(session, parent_id: int) -> list[Item]:
    stmt = select(Item).where(Item.parent_id == parent_id).order_by(Item.position)
    return list((await session.scalars(stmt)).all())


async def _link(session, item_id: int, related_id: int) -> None:
    session.add(ItemLink(item_id=item_id, related_id=related_id))
    await session.commit()


# --- related candidates --------------------------------------------------------------------------------------------


async def test_candidates_match_any_word_best_rank_first(session, make, now, local):
    [sony, cover, milk, _] = await make(
        {"kind": "purchase", "title": "Купить наушники Sony"},
        {"kind": "purchase", "title": "Чехол для наушников"},
        {"kind": "purchase", "title": "Купить молоко"},
        {"kind": "note", "title": "Погода хорошая"},
        now=local(2026, 9, 12, 10, 0),
    )
    rows = await query.related_candidates(session, "купить чехол для наушников Sony", now)
    # every item sharing a word comes up (OR), the one with most words first; "для" is a stop word
    assert [r.item_id for r in rows] == [sony.item.id, cover.item.id, milk.item.id]


async def test_candidates_skip_the_message_itself_old_deleted_and_steps(session, make, now, local, new_raw):
    raw = await new_raw("наушники")
    [own] = await make({"kind": "purchase", "title": "Наушники"}, now=now, raw=raw)
    [old] = await make({"kind": "purchase", "title": "Наушники старые"}, now=local(2025, 10, 5, 10, 0))
    [year] = await make({"kind": "purchase", "title": "Наушники годовалые"}, now=local(2025, 10, 7, 10, 0))
    [gone] = await make({"kind": "purchase", "title": "Наушники удалённые"}, now=now)
    [parent] = await make({"kind": "task", "title": "Сборы", "steps": [{"title": "Взять наушники"}]}, now=now)
    await items.delete_item(session, gone.item.id, now)
    await session.commit()

    rows = await query.related_candidates(session, "наушники", now, exclude_raw_ids=[raw.id])
    assert [r.item_id for r in rows] == [year.item.id]  # not: own, old (366 days), gone, the step of «Сборы»
    assert {r.item_id for r in await query.related_candidates(session, "наушники", now)} == {own.item.id, year.item.id}
    assert parent.item.id not in [r.item_id for r in rows] and old.item.id not in [r.item_id for r in rows]


async def test_candidates_ignore_numbers_and_operators_and_keep_five(session, make, now):
    await make(*[{"kind": "note", "title": f"Заметка {n} про отпуск"} for n in range(7)], now=now)
    await make({"kind": "note", "title": "Встреча в 15"}, now=now)
    assert await query.related_candidates(session, "в 15:00 & | ! ( ) :*", now) == []
    rows = await query.related_candidates(session, "отпуск в 15:00 & !", now)
    assert len(rows) == 5 and all("отпуск" in r.title for r in rows)
    assert await query.related_candidates(session, "   ", now) == []


# --- card extras ---------------------------------------------------------------------------------------------------


async def test_card_extras_of_a_parent_a_step_and_related_items(session, make, now, local, prefs):
    [old] = await make({"kind": "task", "title": "Купить коробки"}, now=local(2026, 9, 12, 10, 0))
    [parent] = await make(MOVE, now=now)
    steps = await _steps(session, parent.item.id)
    await _link(session, parent.item.id, old.item.id)
    await items.toggle_step(session, steps[0].id, done=True, prefs=prefs, now=now)
    await items.delete_item(session, steps[2].id, now)
    await session.commit()

    extras = await query.card_extras(session, parent.item)
    assert [(s.position, s.title, s.done, s.due_at, s.precision) for s in extras.steps] == [
        (1, "Упаковать вещи", True, None, None),
        (2, "Заказать машину", False, local(2026, 10, 6, 15, 0), "exact"),
    ]
    assert [(r.item_id, r.title) for r in extras.related] == [(old.item.id, "Купить коробки")]
    assert extras.parent_title is None

    # the link works both ways; a step names its parent and has no checklist of its own
    assert [r.item_id for r in (await query.card_extras(session, old.item)).related] == [parent.item.id]
    step_extras = await query.card_extras(session, steps[1])
    assert (step_extras.steps, step_extras.parent_title) == ((), "Переезд")

    # a deleted related item is not shown
    await items.delete_item(session, old.item.id, now)
    await session.commit()
    assert (await query.card_extras(session, parent.item)).related == ()


# --- lists ---------------------------------------------------------------------------------------------------------


async def test_tasks_show_timed_steps_with_the_parent_and_the_parent_with_progress(session, make, now, prefs):
    [parent] = await make(MOVE, now=now)
    steps = await _steps(session, parent.item.id)
    await items.toggle_step(session, steps[0].id, done=True, prefs=prefs, now=now)
    await session.commit()

    rows = await query.open_tasks(session, prefs, now)
    assert [(r.title, r.parent_title, r.steps_done, r.steps_total) for r in rows] == [
        ("Заказать машину", "Переезд", 0, 0),  # today 15:00
        ("Переезд", None, 1, 3),  # Saturday
    ]
    # untimed steps are not top-level rows anywhere but in their parent's card
    recent = await query.recent(session)
    assert [r.title for r in recent] == ["Заказать машину", "Переезд"]
    found = await query.run_query(session, QuerySpec(kinds=["task"]), prefs, now)
    assert sorted(r.title for r in found) == ["Заказать машину", "Переезд"]
    llm = query.rows_for_llm(rows, prefs)
    assert llm[0]["step_of"] == "Переезд" and llm[1]["steps_done"] == "1/3"


async def test_agenda_lists_a_timed_step_with_its_parent(session, make, now, prefs):
    await make(MOVE, now=now)
    agenda = await query.agenda(session, prefs, now)
    assert [(r.title, r.parent_title) for r in agenda.open] == [("Заказать машину", "Переезд")]
    saturday = await query.agenda(session, prefs, now, day_offset=4)
    assert [(r.title, r.steps_total) for r in saturday.open] == [("Переезд", 3)]


async def test_search_still_finds_untimed_steps(session, make, now):
    await make(MOVE, now=now)
    rows = await query.search(session, "упаковать")
    assert [(r.title, r.parent_title) for r in rows] == [("Упаковать вещи", "Переезд")]


async def test_items_of_a_message_are_its_top_level_items(session, make, now, new_raw):
    raw = await new_raw("переезд, распиши по шагам")
    [parent] = await make(MOVE, now=now, raw=raw)
    assert [r.item_id for r in await query.items_of_raw(session, [raw.id])] == [parent.item.id]


async def test_untimed_step_moved_to_a_time_shows_up(session, make, now, prefs):
    [parent] = await make(MOVE, now=now)
    first = (await _steps(session, parent.item.id))[0]
    await items.apply_patch(session, first.id, EditPatch(when=When(text="завтра", day="+1")), prefs, now)
    await session.commit()
    titles = [r.title for r in await query.open_tasks(session, prefs, now)]
    assert titles == ["Заказать машину", "Упаковать вещи", "Переезд"]
    # deleted steps are not counted
    await session.execute(update(Item).where(Item.parent_id == parent.item.id).values(status="deleted"))
    await session.commit()
    rows = await query.open_tasks(session, prefs, now)
    assert [(r.title, r.steps_total) for r in rows] == [("Переезд", 0)]
