"""Read side of related items and checklists (docs/features/iteration-2.md §C): RELATED CANDIDATES (OR of the
message's subject words — no reminder / time words, common verbs or numbers —, open items only, not the same message,
365 days, best rank first), card extras, and the list rules — a step shows in top-level lists only with a time of its
own, "(из «Переезд»)", a parent counts "2/5".
Reference time: Tue 2026-10-06 10:00 Europe/Warsaw (services conftest)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import select, update

from dumpbot.ai.schemas import EditPatch, QuerySpec, When
from dumpbot.bot import views
from dumpbot.db.models import Item, ItemLink, Occurrence
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


async def test_candidates_match_any_subject_word_best_rank_first(session, make, now, local):
    [sony, cover, _, _] = await make(
        {"kind": "purchase", "title": "Купить наушники Sony"},
        {"kind": "purchase", "title": "Чехол для наушников"},
        {"kind": "purchase", "title": "Купить молоко"},
        {"kind": "note", "title": "Погода хорошая"},
        now=local(2026, 9, 12, 10, 0),
    )
    rows = await query.related_candidates(session, "купить ещё одни наушники Sony", now)
    # every item sharing a subject word comes up (OR), the one with most words first; "для" is a stop word and
    # "купить" a generic verb: «Купить молоко» is not about headphones
    assert [r.item_id for r in rows] == [sony.item.id, cover.item.id]


async def test_generic_words_bring_no_candidates(session, make, now, local):
    # the live case: «Покушать» was saved from "напомни через час покушать", «Позвонить маме завтра» shares "завтра"
    await make(
        {"kind": "task", "title": "Покушать"}, now=local(2026, 10, 6, 8, 0), source_text="напомни через час покушать"
    )
    await make({"kind": "task", "title": "Позвонить маме завтра"}, now=local(2026, 10, 5, 8, 0))
    [visa] = await make({"kind": "note", "title": "Виза в Черногорию: нужен паспорт"}, now=local(2026, 9, 1, 8, 0))

    text = "Напомни часов в 17 прогуглить про визу в черногорию"
    assert [r.item_id for r in await query.related_candidates(session, text, now)] == [visa.item.id]
    # nothing but reminder / time words, common verbs, infinitives and numbers -> no candidates at all: a bare verb
    # («покушать») names no subject
    for text in (
        "напомни завтра в 10 позвонить",
        "Напомни через час",
        "в пятницу вечером купить",
        "5-го в 17:00",
        "покушать бы",
        "напомни через час покушать",
    ):
        assert await query.related_candidates(session, text, now) == [], text


async def test_candidates_are_open_items_only(session, make, now, local, prefs):
    day = local(2026, 10, 5, 9, 0)
    today = {"text": "сегодня в 12", "day": "+0", "time": "12:00"}
    [open_, done, gone, done_occ, series, idea, overdue] = await make(
        {"kind": "task", "title": "Таблетки"},
        {"kind": "task", "title": "Таблетки", "when": today},
        {"kind": "task", "title": "Таблетки удалённые"},
        {"kind": "task", "title": "Таблетки по старому", "when": today},
        {"kind": "task", "title": "Таблетки по расписанию", "when": today, "rrule": "FREQ=DAILY"},
        {"kind": "idea", "title": "Таблетки в новой упаковке"},
        {"kind": "task", "title": "Купить таблетки", "when": today},
        now=day,
    )
    await items.mark_done(session, done.occurrence.id, day, prefs)
    await items.delete_item(session, gone.item.id, day)
    # an actionable item whose current occurrence is done counts as done, even if its status says otherwise
    await items.mark_done(session, done_occ.occurrence.id, day, prefs)
    await session.execute(update(Item).where(Item.id == done_occ.item.id).values(status="open", done_at=None))
    # a series stays a candidate whatever its instances
    await items.mark_done(session, series.occurrence.id, day, prefs)
    await session.execute(update(Occurrence).where(Occurrence.item_id == series.item.id).values(status="done"))
    await session.commit()
    assert (await session.get(Item, done.item.id)).status == "done"

    # a past-due one the owner has not ticked (yesterday 12:00) is still open: "не сделано" (requirements)
    rows = await query.related_candidates(session, "таблетки", now)
    assert {r.item_id for r in rows} == {open_.item.id, series.item.id, idea.item.id, overdue.item.id}


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


async def test_candidates_rank_title_words_and_more_words_first(session, make, now, local):
    # a word in the title beats one only in the message a multi-item save came from; more shared words beat one
    [dentist] = await make({"kind": "task", "title": "Записаться к стоматологу"}, now=local(2026, 9, 1, 10, 0))
    for day in range(10, 15):
        await make(
            {"kind": "purchase", "title": f"Купить молоко {day}"},
            now=local(2026, 9, day, 10, 0),
            source_text="купить молоко и записаться к стоматологу",
        )
    rows = await query.related_candidates(session, "стоматолог", now)
    assert len(rows) == 5 and rows[0].item_id == dentist.item.id
    [sony] = await make({"kind": "purchase", "title": "Наушники Sony"}, now=local(2026, 8, 1, 10, 0))
    rows = await query.related_candidates(session, "наушники Sony стоматолог", now)
    assert [r.item_id for r in rows[:2]] == [sony.item.id, dentist.item.id]


async def test_a_leaked_verb_does_not_crowd_out_the_subject(session, make, now, local):
    # five newer «Принять …» and the tablets saved on 1 Sep: "принять" is no subject, the tablets come up
    [tablets] = await make({"kind": "purchase", "title": "Купить таблетки от давления"}, now=local(2026, 9, 1, 10, 0))
    others = ("Принять посылку", "Принять решение по ипотеке", "Принять душ перед выходом", "Принять работу у мастера")
    for day, title in enumerate([*others, "Принять звонок от HR"], start=10):
        await make({"kind": "task", "title": title}, now=local(2026, 9, day, 10, 0), source_text=title.lower())
    rows = await query.related_candidates(session, "напомни в 21 принять таблетки", now)
    assert [r.item_id for r in rows] == [tablets.item.id]


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


async def test_card_extras_carry_the_span_of_a_step(session, make, now, prefs):
    """A step «на следующей неделе»: the parent's checklist names the span, not the date of its reminder (2)."""
    spec = {
        "kind": "task",
        "title": "Переезд",
        "steps": [{"title": "Упаковать вещи", "when": {"text": "на следующей неделе", "period": "next_week"}}],
    }
    [parent] = await make(spec, now=now)
    [step] = (await query.card_extras(session, parent.item)).steps
    assert (step.precision, step.period_kind, step.period_start, step.period_end) == (
        "period",
        "week",
        date(2026, 10, 12),
        date(2026, 10, 18),
    )
    assert views.checklist_lines([step], prefs, now)[1] == "☐ 1. Упаковать вещи · на следующей неделе"
