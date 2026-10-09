"""services.query: agenda, lists, search, QuerySpec, edit candidates, usage summary, rows_for_llm."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from dumpbot.ai.schemas import EditPatch, QuerySpec, When
from dumpbot.db.models import AiUsage
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import Row
from dumpbot.services import items, query

DAILY_9 = {"kind": "task", "title": "Зарядка", "rrule": "FREQ=DAILY", "when": {"time": "09:00"}}


def titles(rows: list[Row]) -> list[str]:
    return [r.title for r in rows]


# --- agenda ----------------------------------------------------------------------------------------------------


async def test_agenda_today_overdue_done_and_tomorrow(session, make, local, now, prefs):
    [lunch, morning_call] = await make(
        {"kind": "purchase", "title": "Купить наушники", "when": {"time": "lunch"}},
        {"kind": "event", "title": "Созвон", "when": {"time": "11:00"}},
        now=now,
    )
    [done] = await make({"kind": "task", "title": "Позвонить", "when": {"time": "08:30"}}, now=local(2026, 10, 6, 7, 0))
    await items.mark_done(session, done.occurrence.id, local(2026, 10, 6, 9, 0), prefs)
    await make({"kind": "task", "title": "Отчёт", "when": {"time": "09:00"}}, now=local(2026, 10, 5, 8, 0))
    await make({"kind": "task", "title": "Древнее"}, now=local(2026, 8, 20, 8, 0))
    await make({"kind": "event", "title": "Завтрашнее", "when": {"day": "+1", "time": "12:00"}}, now=now)
    [deleted] = await make({"kind": "task", "title": "Удалённое", "when": {"time": "16:00"}}, now=now)
    await items.delete_item(session, deleted.item.id, now)
    await make({"kind": "note", "title": "Заметка без времени"}, now=now)
    await session.commit()

    today = await query.agenda(session, prefs, now)
    assert today.day == date(2026, 10, 6)
    assert titles(today.open) == ["Созвон", "Купить наушники"]
    assert today.open[1].due_at == local(2026, 10, 6, 13, 0)
    assert today.open[1].occurrence_id == lunch.occurrence.id
    assert today.open[1].occ_status == "open"
    assert titles(today.overdue) == ["Отчёт"]
    assert titles(today.done) == ["Позвонить"]
    assert today.done[0].occ_status == "done"
    assert morning_call.item.id == today.open[0].item_id

    tomorrow = await query.agenda(session, prefs, now, day_offset=1)
    assert tomorrow.day == date(2026, 10, 7)
    assert titles(tomorrow.open) == ["Завтрашнее"]
    assert tomorrow.overdue == []
    assert tomorrow.done == []


async def test_agenda_keeps_missed_series_instance_as_not_done(session, make, local, prefs):
    [c] = await make(DAILY_9, now=local(2026, 10, 6, 7, 0))
    await items.ensure_recurring(session, prefs, local(2026, 10, 6, 9, 30))
    await items.ensure_recurring(session, prefs, local(2026, 10, 6, 21, 1))
    assert c.occurrence.status == "missed"

    evening = await query.agenda(session, prefs, local(2026, 10, 6, 21, 5))
    assert [(r.title, r.occ_status) for r in evening.open] == [("Зарядка", "missed")]
    assert evening.open[0].rrule == "FREQ=DAILY"
    morning = await query.agenda(session, prefs, local(2026, 10, 7, 8, 0))
    assert morning.overdue == []
    assert [r.due_at for r in morning.open] == [local(2026, 10, 7, 9, 0)]


# --- lists -----------------------------------------------------------------------------------------------------


async def test_open_tasks_ordering(session, make, local, now, prefs):
    await make({"kind": "task", "title": "Завтра", "when": {"day": "+1", "time": "12:00"}}, now=now)
    await make({"kind": "purchase", "title": "Сегодня", "when": {"time": "lunch"}}, now=now)
    [no_due_old] = await make({"kind": "note", "title": "Без срока старая"}, now=local(2026, 10, 1, 9, 0))
    [no_due_new] = await make({"kind": "note", "title": "Без срока новая"}, now=local(2026, 10, 2, 9, 0))
    await items.set_kind(session, no_due_old.item.id, "task")
    await items.set_kind(session, no_due_new.item.id, "task")
    [done] = await make({"kind": "task", "title": "Сделанная"}, now=now)
    await items.mark_done(session, done.occurrence.id, now, prefs)
    [gone] = await make({"kind": "task", "title": "Удалённая"}, now=now)
    await items.delete_item(session, gone.item.id, now)
    await make({"kind": "idea", "title": "Идея", "when": {"time": "11:00"}}, now=now)
    await session.commit()

    rows = await query.open_tasks(session, prefs, now)
    assert titles(rows) == ["Сегодня", "Завтра", "Без срока новая", "Без срока старая"]
    assert rows[2].due_at is None
    assert rows[2].occurrence_id is None
    assert titles(await query.open_tasks(session, prefs, now, limit=1)) == ["Сегодня"]


async def test_recent(session, make, local, now):
    await make({"kind": "note", "title": "Первая"}, now=local(2026, 10, 5, 9, 0))
    [second] = await make({"kind": "task", "title": "Вторая", "when": {"time": "18:00"}}, now=now)
    [third] = await make({"kind": "note", "title": "Третья"}, now=local(2026, 10, 6, 11, 0))
    await items.delete_item(session, third.item.id, now)
    await session.commit()
    rows = await query.recent(session)
    assert titles(rows) == ["Вторая", "Первая"]
    assert rows[0].due_at == local(2026, 10, 6, 18, 0)
    assert rows[0].occurrence_id == second.occurrence.id
    assert titles(await query.recent(session, limit=1)) == ["Вторая"]


async def test_recurring_lists_active_series_with_next_occurrence(session, make, local, now, prefs):
    [daily] = await make(DAILY_9, now=now)
    [weekly] = await make(
        {"kind": "task", "title": "Мусор", "rrule": "FREQ=WEEKLY;BYDAY=MO", "when": {"time": "08:00"}}, now=now
    )
    [stopped] = await make({**DAILY_9, "title": "Остановленная"}, now=now)
    await items.stop_recurrence(session, stopped.item.id)
    [deleted] = await make({**DAILY_9, "title": "Удалённая"}, now=now)
    await items.delete_item(session, deleted.item.id, now)
    await session.commit()

    rows = await query.recurring(session)
    assert titles(rows) == ["Зарядка", "Мусор"]
    assert [r.due_at for r in rows] == [local(2026, 10, 7, 9, 0), local(2026, 10, 12, 8, 0)]
    assert [r.rrule for r in rows] == ["FREQ=DAILY", "FREQ=WEEKLY;BYDAY=MO"]
    assert rows[0].occurrence_id == daily.occurrence.id
    assert rows[1].item_id == weekly.item.id


# --- search ----------------------------------------------------------------------------------------------------


@pytest.fixture
async def library(session, make, local, now):
    await make(
        {
            "kind": "purchase",
            "title": "Купить наушники",
            "details": "buy headphones for running",
            "tags": ["электроника"],
        },
        now=local(2026, 10, 1, 9, 0),
    )
    await make({"kind": "link", "title": "Наушники для бега — обзор"}, now=local(2026, 10, 2, 9, 0))
    await make({"kind": "task", "title": "Позвонить маме"}, now=local(2026, 10, 3, 9, 0))
    [old] = await make({"kind": "note", "title": "Старые наушники"}, now=local(2026, 10, 4, 9, 0))
    await items.delete_item(session, old.item.id, now)
    await session.commit()


async def test_search_russian_stemming(session, library):
    assert titles(await query.search(session, "наушников")) == ["Наушники для бега — обзор", "Купить наушники"]


async def test_search_english_words(session, library):
    rows = await query.search(session, "headphones")
    assert titles(rows) == ["Купить наушники"]
    assert rows[0].snippet == "buy headphones for running"


async def test_search_partial_word_via_ilike(session, library):
    assert set(titles(await query.search(session, "науш"))) == {"Купить наушники", "Наушники для бега — обзор"}


async def test_search_tag(session, library):
    assert titles(await query.search(session, "#электроника")) == ["Купить наушники"]


async def test_search_all_words_must_match(session, library):
    assert titles(await query.search(session, "наушники бег")) == ["Наушники для бега — обзор"]


async def test_search_empty_and_special_chars(session, library):
    assert await query.search(session, "   ") == []
    assert await query.search(session, "100%_") == []


async def test_search_ranks_better_match_first(session, make, local):
    await make(
        {"kind": "note", "title": "Пицца", "details": "пицца пицца — рецепт пиццы"},
        now=local(2026, 10, 1, 9, 0),
        source_text="пицца",
    )
    await make({"kind": "note", "title": "Ужин", "details": "может, пицца"}, now=local(2026, 10, 5, 9, 0))
    assert titles(await query.search(session, "пицца")) == ["Пицца", "Ужин"]
    assert titles(await query.search(session, "пицца", limit=1)) == ["Пицца"]


# --- run_query -------------------------------------------------------------------------------------------------


@pytest.fixture
async def dataset(session, make, local, now, prefs):
    await make({"kind": "purchase", "title": "Купить молоко", "tags": ["еда"], "when": {"time": "lunch"}}, now=now)
    await make({"kind": "task", "title": "Отчёт", "tags": ["работа"], "when": {"day": "+1", "time": "15:00"}}, now=now)
    [done] = await make({"kind": "task", "title": "Сдать отчёт", "when": {"time": "12:00"}}, now=now)
    await items.mark_done(session, done.occurrence.id, local(2026, 10, 6, 12, 5), prefs)
    await make({"kind": "note", "title": "Идея про отпуск"}, now=now)
    await make({"kind": "note", "title": "Старая заметка"}, now=local(2026, 10, 1, 9, 0))
    await make(DAILY_9, now=now)
    await session.commit()


async def test_run_query_due_today(session, dataset, now, prefs):
    rows = await query.run_query(session, QuerySpec(range="today"), prefs, now)
    assert titles(rows) == ["Сдать отчёт", "Купить молоко"]
    assert [r.occ_status for r in rows] == ["done", "open"]
    assert titles(await query.run_query(session, QuerySpec(range="today", status="open"), prefs, now)) == [
        "Купить молоко"
    ]
    assert titles(await query.run_query(session, QuerySpec(range="today", status="done"), prefs, now)) == [
        "Сдать отчёт"
    ]


async def test_run_query_due_tomorrow_tasks(session, dataset, local, now, prefs):
    rows = await query.run_query(session, QuerySpec(range="tomorrow", kinds=["task"]), prefs, now)
    assert titles(rows) == ["Зарядка", "Отчёт"]
    assert [r.due_at for r in rows] == [local(2026, 10, 7, 9, 0), local(2026, 10, 7, 15, 0)]


async def test_run_query_due_range_lists_every_series_instance(session, dataset, local, now, prefs):
    await items.ensure_recurring(session, prefs, local(2026, 10, 7, 9, 30))
    rows = await query.run_query(session, QuerySpec(range="this_week", text="зарядка"), prefs, now)
    assert [r.due_at for r in rows] == [local(2026, 10, 7, 9, 0), local(2026, 10, 8, 9, 0)]


async def test_run_query_created_range(session, dataset, now, prefs):
    spec = QuerySpec(date_field="created", range="today", kinds=["note"])
    assert titles(await query.run_query(session, spec, prefs, now)) == ["Идея про отпуск"]
    spec = QuerySpec(date_field="created", range="this_month", kinds=["note"])
    assert titles(await query.run_query(session, spec, prefs, now)) == ["Идея про отпуск", "Старая заметка"]
    spec = QuerySpec(date_field="created", range="yesterday")
    assert await query.run_query(session, spec, prefs, now) == []


async def test_run_query_text_status_tags_limit(session, dataset, now, prefs):
    assert set(titles(await query.run_query(session, QuerySpec(text="отчёты"), prefs, now))) == {
        "Отчёт",
        "Сдать отчёт",
    }
    assert titles(await query.run_query(session, QuerySpec(text="отчёт", status="open"), prefs, now)) == ["Отчёт"]
    assert titles(await query.run_query(session, QuerySpec(text="отчёт", status="done"), prefs, now)) == ["Сдать отчёт"]
    assert titles(await query.run_query(session, QuerySpec(tags=["еда"]), prefs, now)) == ["Купить молоко"]
    assert len(await query.run_query(session, QuerySpec(limit=2), prefs, now)) == 2


async def test_run_query_due_without_range_orders_by_due(session, dataset, now, prefs):
    rows = await query.run_query(session, QuerySpec(kinds=["task", "purchase"], status="open"), prefs, now)
    assert titles(rows) == ["Купить молоко", "Зарядка", "Отчёт"]


async def test_run_query_invalid_range_is_ignored(session, dataset, now, prefs):
    rows = await query.run_query(session, QuerySpec(range="2026-02-30", kinds=["note"]), prefs, now)
    assert set(titles(rows)) == {"Идея про отпуск", "Старая заметка"}


async def test_run_query_open_includes_missed_instances(session, make, local, prefs):
    await make(DAILY_9, now=local(2026, 10, 6, 7, 0))
    await items.ensure_recurring(session, prefs, local(2026, 10, 6, 9, 30))
    later = local(2026, 10, 7, 12, 0)
    await items.ensure_recurring(session, prefs, later)
    rows = await query.run_query(session, QuerySpec(range="past:2", status="open"), prefs, later)
    assert [(r.due_at, r.occ_status) for r in rows] == [
        (local(2026, 10, 6, 9, 0), "missed"),
        (local(2026, 10, 7, 9, 0), "open"),
    ]


# --- edit candidates -------------------------------------------------------------------------------------------


@pytest.fixture
async def meetings(session, make, local, now):
    await make({"kind": "event", "title": "Встреча с Олегом", "when": {"day": "+5", "time": "10:00"}}, now=now)
    await make({"kind": "event", "title": "Встреча с Олегом", "when": {"day": "+1", "time": "16:00"}}, now=now)
    await make({"kind": "task", "title": "Позвонить Олегу"}, now=now)  # default: today 19:00
    await make({"kind": "purchase", "title": "Купить подарок"}, now=now)
    [deleted] = await make({"kind": "event", "title": "Встреча с Олегом удалённая"}, now=now)
    await items.delete_item(session, deleted.item.id, now)
    [old] = await make({"kind": "event", "title": "Встреча с Олегом в кафе"}, now=local(2026, 7, 1, 10, 0))
    await items.mark_done(session, old.occurrence.id, local(2026, 7, 1, 20, 0), Prefs())
    old.item.updated_at = local(2026, 7, 1, 20, 0)
    await session.commit()


async def test_find_edit_candidates_nearest_due_first(session, meetings, local, now, prefs):
    rows = await query.find_edit_candidates(session, "встречу с Олегом", prefs, now)
    assert titles(rows) == ["Встреча с Олегом", "Встреча с Олегом"]
    assert [r.due_at for r in rows] == [local(2026, 10, 7, 16, 0), local(2026, 10, 11, 10, 0)]
    assert len(await query.find_edit_candidates(session, "встречу с Олегом", prefs, now, limit=1)) == 1


async def test_find_edit_candidates_any_word_fallback(session, meetings, now, prefs):
    rows = await query.find_edit_candidates(session, "созвон с Олегом", prefs, now)
    assert titles(rows) == ["Позвонить Олегу", "Встреча с Олегом", "Встреча с Олегом"]


async def test_find_edit_candidates_recent_done_items_count(session, make, local, now, prefs):
    [c] = await make({"kind": "task", "title": "Сдать отчёт"}, now=local(2026, 9, 30, 9, 0))
    await items.mark_done(session, c.occurrence.id, local(2026, 9, 30, 18, 0), prefs)
    await session.commit()
    assert titles(await query.find_edit_candidates(session, "отчёт", prefs, now)) == ["Сдать отчёт"]


async def test_find_edit_candidates_empty_or_unknown(session, meetings, now, prefs):
    assert await query.find_edit_candidates(session, "", prefs, now) == []
    assert await query.find_edit_candidates(session, "космос", prefs, now) == []


# --- usage -----------------------------------------------------------------------------------------------------


async def test_usage_summary_local_month(session, local, now, prefs):
    def usage(at: datetime, purpose: str, cost: str, ok: bool = True) -> AiUsage:
        return AiUsage(created_at=at, purpose=purpose, model="m", cost_usd=Decimal(cost), ok=ok)

    session.add_all(
        [
            usage(local(2026, 9, 30, 23, 30), "extract", "1.00"),  # Sep 30 21:30 UTC: September
            usage(local(2026, 10, 1, 0, 30), "extract", "0.010"),  # Sep 30 22:30 UTC: October locally
            usage(local(2026, 10, 6, 9, 0), "stt", "0.002"),
            usage(local(2026, 10, 6, 9, 30), "extract", "0.003", ok=False),
            usage(local(2026, 11, 1, 0, 10), "extract", "2.00"),
        ]
    )
    await session.commit()

    summary = await query.usage_summary(session, Prefs(budget_usd=3.5), now)
    assert summary.month == "октябрь 2026"
    assert summary.calls == 3
    assert summary.spent_usd == pytest.approx(0.015)
    assert summary.limit_usd == 3.5
    assert summary.by_purpose == {"extract": (2, pytest.approx(0.013)), "stt": (1, pytest.approx(0.002))}


async def test_usage_summary_empty_month(session, local, prefs):
    summary = await query.usage_summary(session, prefs, local(2027, 1, 15, 12, 0))
    assert (summary.month, summary.calls, summary.spent_usd, summary.by_purpose) == ("январь 2027", 0, 0.0, {})


# --- rows_for_llm ----------------------------------------------------------------------------------------------


def test_rows_for_llm_uses_local_time_and_drops_empty_fields(prefs):
    rows = [
        Row(
            item_id=7,
            kind="purchase",
            title="Купить наушники",
            tags=["электроника"],
            status="open",
            due_at=datetime(2026, 10, 7, 11, 0, tzinfo=UTC),
            occurrence_id=3,
            occ_status="open",
            rrule="FREQ=WEEKLY;BYDAY=MO",
            created_at=datetime(2026, 10, 6, 12, 5, tzinfo=UTC),
            snippet="в обед",
            urls=["https://a.example"],
        ),
        Row(
            item_id=8,
            kind="note",
            title="Мысль",
            tags=[],
            status="open",
            due_at=None,
            occurrence_id=None,
            occ_status=None,
            rrule=None,
            created_at=datetime(2026, 1, 6, 12, 5, tzinfo=UTC),
            snippet=None,
        ),
    ]
    assert query.rows_for_llm(rows, prefs) == [
        {
            "item_id": 7,
            "kind": "purchase",
            "title": "Купить наушники",
            "status": "open",
            "tags": ["электроника"],
            "due": "2026-10-07 13:00",
            "occ_status": "open",
            "repeat": "каждый понедельник",
            "created": "2026-10-06 14:05",
            "snippet": "в обед",
            "urls": ["https://a.example"],
        },
        {"item_id": 8, "kind": "note", "title": "Мысль", "status": "open", "created": "2026-01-06 13:05"},
    ]


async def test_rows_carry_snippet_and_urls(session, make, now):
    await make(
        {"kind": "link", "title": "Статья", "details": "x" * 300},
        now=now,
        urls=("https://a.example/1",),
    )
    [same] = await make({"kind": "note", "title": "Короткая"}, now=now, source_text="Короткая")
    await session.commit()
    rows = await query.recent(session)
    by_title = {r.title: r for r in rows}
    assert by_title["Статья"].urls == ["https://a.example/1"]
    assert len(by_title["Статья"].snippet) == 200
    assert by_title["Статья"].snippet.endswith("…")
    assert by_title["Короткая"].snippet is None
    assert same.item.source_text == "Короткая"


# --- list_rows: the numbered lines of a list message (iteration 3, §2) ----------------------------------------


async def test_list_rows_show_each_line_as_it_is_now(session, make, local, now, prefs):
    [done, deleted, someday, plain] = await make(
        {"kind": "task", "title": "Сделано", "when": {"time": "12:00"}},
        {"kind": "task", "title": "Удалено", "when": {"time": "13:00"}},
        {"kind": "task", "title": "Потом", "when": {"time": "14:00"}},
        {"kind": "task", "title": "Обычное", "when": {"time": "15:00"}},
        now=now,
    )
    await items.mark_done(session, done.occurrence.id, now, prefs)
    await items.delete_item(session, deleted.item.id, now)
    await items.apply_patch(
        session, someday.item.id, EditPatch(when=When(text="когда-нибудь", period="someday")), prefs, now
    )
    await session.flush()

    refs = [
        (done.occurrence.id, done.item.id),
        (deleted.occurrence.id, deleted.item.id),
        (someday.occurrence.id, someday.item.id),  # its occurrence was cancelled: the line is the item itself
        (None, plain.item.id),  # a line without an occurrence: the item's current one
        (plain.occurrence.id, done.item.id),  # an occurrence of another item is not trusted
        (None, 10**9),  # gone
    ]
    rows = await query.list_rows(session, refs)
    assert rows[1] is None and rows[5] is None
    assert (rows[0].title, rows[0].occurrence_id, rows[0].occ_status) == ("Сделано", done.occurrence.id, "done")
    assert (rows[2].title, rows[2].occurrence_id, rows[2].due_at, rows[2].someday) == ("Потом", None, None, True)
    assert (rows[3].occurrence_id, rows[3].due_at) == (plain.occurrence.id, local(2026, 10, 6, 15, 0))
    assert (rows[4].item_id, rows[4].occurrence_id) == (done.item.id, done.occurrence.id)
    assert await query.list_rows(session, []) == []


async def test_list_rows_of_a_series_keep_the_listed_instance(session, make, local, prefs):
    [series] = await make({**DAILY_9, "when": {"time": "12:00"}}, now=local(2026, 10, 6, 10, 0))
    await items.mark_done(session, series.occurrence.id, local(2026, 10, 6, 12, 5), prefs)  # the next one is created
    await session.flush()
    [row] = await query.list_rows(session, [(series.occurrence.id, series.item.id)])
    assert (row.occurrence_id, row.occ_status, row.rrule) == (series.occurrence.id, "done", "FREQ=DAILY")
    [current] = await query.list_rows(session, [(None, series.item.id)])
    assert (current.occ_status, current.due_at) == ("open", local(2026, 10, 7, 12, 0))


async def test_list_rows_count_the_steps_of_a_parent(session, make, now):
    [parent] = await make(
        {
            "kind": "task",
            "title": "Переезд",
            "when": {"day": "SA"},
            "steps": [{"title": "Упаковать"}, {"title": "Ключи"}],
        },
        now=now,
    )
    await session.flush()
    [row] = await query.list_rows(session, [(parent.occurrence.id, parent.item.id)])
    assert (row.steps_done, row.steps_total) == (0, 2)
