"""Fuzzy time on the read side (docs/features/iteration-2.md §A): the agenda's period section (instead of open /
overdue while the span runs), the morning digest, /tasks without "когда-нибудь", /someday, range queries that
overlap a span, the rows for the answer model."""

from __future__ import annotations

from datetime import date

import pytest

from dumpbot.ai.schemas import EditPatch, QuerySpec
from dumpbot.services import digest, items, query

WEEK = {"text": "на этой неделе", "period": "this_week"}
SOMEDAY = {"text": "когда-нибудь", "period": "someday"}


def titles(rows) -> list[str]:
    return [r.title for r in rows]


@pytest.fixture
async def week_item(make, local):
    """ "На этой неделе посмотреть расбери пай", Tue 2026-10-06 13:36: due Fri 9 Oct 19:00."""
    [c] = await make({"kind": "task", "title": "Посмотреть расбери пай", "when": WEEK}, now=local(2026, 10, 6, 13, 36))
    return c


# --- agenda: the period section --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "day",
    [
        (2026, 10, 6, 13, 40),  # Tuesday, right after saving
        (2026, 10, 7, 9, 0),
        (2026, 10, 9, 9, 0),  # Friday: due tonight, still in the section, not in today's list
        (2026, 10, 9, 21, 0),  # Friday after the 19:00 reminder
        (2026, 10, 10, 9, 0),  # Saturday: Friday 19:00 has passed, but the week has not
        (2026, 10, 11, 9, 0),  # Sunday
    ],
)
async def test_running_period_item_is_in_the_section_every_day_of_the_week(session, prefs, local, week_item, day):
    agenda = await query.agenda(session, prefs, local(*day))
    assert titles(agenda.period_items) == ["Посмотреть расбери пай"]
    [row] = agenda.period_items
    assert (row.period_kind, row.period_start, row.period_end) == ("week", date(2026, 10, 5), date(2026, 10, 11))
    assert row.occurrence_id == week_item.occurrence.id and row.due_at == local(2026, 10, 9, 19, 0)
    assert agenda.open == [] and agenda.overdue == []


async def test_after_the_week_the_item_is_overdue(session, prefs, local, week_item):
    agenda = await query.agenda(session, prefs, local(2026, 10, 12, 9, 0))
    assert agenda.period_items == []
    assert titles(agenda.overdue) == ["Посмотреть расбери пай"]


async def test_done_and_deleted_period_items_leave_the_section(session, prefs, local, now, make, week_item):
    [gone] = await make({"kind": "task", "title": "Удалённое", "when": WEEK}, now=now)
    await items.delete_item(session, gone.item.id, now)
    await items.mark_done(session, week_item.occurrence.id, local(2026, 10, 7, 12, 0), prefs)
    await session.commit()

    agenda = await query.agenda(session, prefs, local(2026, 10, 7, 20, 0))
    assert agenda.period_items == []
    assert titles(agenda.done) == ["Посмотреть расбери пай"]  # done today counts in the evening digest


async def test_next_week_item_joins_the_section_on_its_monday(session, prefs, local, now, make):
    await make({"kind": "task", "title": "Шины", "when": {"text": "на след. неделе", "period": "next_week"}}, now=now)
    this_week = await query.agenda(session, prefs, local(2026, 10, 7, 9, 0))
    assert this_week.period_items == [] and this_week.open == []
    monday = await query.agenda(session, prefs, local(2026, 10, 12, 9, 0))
    assert titles(monday.period_items) == ["Шины"]
    assert monday.open == []  # its (1) reminder fires at 10:00, but the item is not "due" on Monday


async def test_section_lists_the_shortest_span_first(session, prefs, local, now, make):
    await make({"kind": "task", "title": "Осенью", "when": {"text": "осенью", "period": "autumn"}}, now=now)
    await make({"kind": "task", "title": "В октябре", "when": {"text": "в октябре", "period": "2026-10"}}, now=now)
    await make({"kind": "task", "title": "На неделе", "when": WEEK}, now=now)
    await make({"kind": "task", "title": "Летом", "when": {"text": "летом", "period": "summer"}}, now=now)
    await make({"kind": "task", "title": "На днях", "when": {"text": "на днях", "period": "soon"}}, now=now)

    agenda = await query.agenda(session, prefs, local(2026, 10, 8, 9, 0))
    assert titles(agenda.period_items) == ["На неделе", "В октябре", "Осенью"]  # not next summer, not "на днях"
    assert titles(agenda.open) == ["На днях"]  # due Thu 19:00: a plain dated item


async def test_morning_digest_with_only_period_items_is_sent(session, prefs, local, week_item):
    data = await digest.build_digest(session, "morning", prefs, local(2026, 10, 10, 9, 0))
    assert data is not None and titles(data.agenda.period_items) == ["Посмотреть расбери пай"]
    assert await digest.build_digest(session, "evening", prefs, local(2026, 10, 10, 21, 0)) is None


# --- /tasks and /someday --------------------------------------------------------------------------------------


async def test_open_tasks_leave_someday_items_out(session, prefs, now, make, week_item):
    await make({"kind": "task", "title": "Выучить испанский", "when": SOMEDAY}, now=now)
    rows = await query.open_tasks(session, prefs, now)
    assert titles(rows) == ["Посмотреть расбери пай"]
    assert rows[0].period_kind == "week" and not rows[0].someday


async def test_someday_items_lists_open_someday_items_of_any_kind(session, prefs, now, local, make):
    [first] = await make({"kind": "task", "title": "Выучить испанский", "when": SOMEDAY}, now=now)
    await make({"kind": "idea", "title": "Написать книгу", "when": SOMEDAY}, now=local(2026, 10, 6, 11, 0))
    [done] = await make({"kind": "task", "title": "Сделанное", "when": SOMEDAY}, now=now)
    [deleted] = await make({"kind": "task", "title": "Удалённое", "when": SOMEDAY}, now=now)
    await make({"kind": "task", "title": "Обычная задача"}, now=now)
    await items.apply_patch(session, done.item.id, EditPatch(action="done"), prefs, now)
    await items.delete_item(session, deleted.item.id, now)
    await session.commit()

    rows = await query.someday_items(session)
    assert titles(rows) == ["Написать книгу", "Выучить испанский"]  # newest first
    assert all(r.someday and r.due_at is None and r.occurrence_id is None for r in rows)
    assert first.item.id in [r.item_id for r in rows]


# --- QuerySpec ranges overlap spans -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("range_", "found"),
    [
        ("tomorrow", True),  # Wednesday is inside "на этой неделе" although the item is due on Friday
        ("today", True),
        ("this_week", True),
        ("next_week", False),
        ("last_week", False),
        ("2026-10-11", True),
        ("2026-10-12..2026-10-20", False),
        ("this_month", True),
    ],
)
async def test_due_range_query_finds_items_whose_span_overlaps(session, prefs, local, week_item, range_, found):
    rows = await query.run_query(session, QuerySpec(range=range_), prefs, local(2026, 10, 6, 14, 0))
    assert (titles(rows) == ["Посмотреть расбери пай"]) is found


async def test_due_range_query_keeps_status_filters_for_spans(session, prefs, local, week_item):
    now = local(2026, 10, 6, 14, 0)
    assert titles(await query.run_query(session, QuerySpec(range="tomorrow", status="open"), prefs, now)) == [
        "Посмотреть расбери пай"
    ]
    assert await query.run_query(session, QuerySpec(range="tomorrow", status="done"), prefs, now) == []


async def test_due_range_query_still_finds_plain_occurrences_and_skips_someday(session, prefs, now, local, make):
    await make({"kind": "task", "title": "Завтра", "when": {"text": "завтра", "day": "+1"}}, now=now)
    await make({"kind": "task", "title": "Испанский", "when": SOMEDAY}, now=now)
    await make({"kind": "task", "title": "Летом", "when": {"text": "летом", "period": "summer"}}, now=now)
    rows = await query.run_query(session, QuerySpec(range="tomorrow"), prefs, now)
    assert titles(rows) == ["Завтра"]
    rows = await query.run_query(session, QuerySpec(range="2027-07-01..2027-07-31"), prefs, now)
    assert titles(rows) == ["Летом"]


async def test_rows_for_llm_mark_spans_and_someday(session, prefs, now, make, week_item):
    await make({"kind": "task", "title": "Испанский", "when": SOMEDAY}, now=now)
    rows = await query.recent(session)
    by_title = {r["title"]: r for r in query.rows_for_llm(rows, prefs)}
    assert by_title["Посмотреть расбери пай"]["period"] == "2026-10-05..2026-10-11"
    assert by_title["Испанский"]["someday"] is True and "due" not in by_title["Испанский"]
