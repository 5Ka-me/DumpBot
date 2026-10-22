"""services.stale and the weekly review's queries (docs/features/iteration-4.md §4, §6): stale items from the events
journal (days with a digest in a row without an action, moves, a question left unanswered), the reminders nobody
reacted to, and the spans / "когда-нибудь" / undated tasks of the review."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dumpbot.ai.schemas import StepSpec
from dumpbot.db.models import Event, Item, Occurrence
from dumpbot.services import events, items, query, stale


@pytest.fixture
def journal(session: AsyncSession):
    """``await journal(type, item, at, **data)`` -> one committed event (its time given)."""

    async def _add(
        type_: str, item_id: int | None, at: datetime, *, occurrence_id: int | None = None, **data: Any
    ) -> Event:
        event = Event(type=type_, item_id=item_id, occurrence_id=occurrence_id, created_at=at, data=data or None)
        session.add(event)
        await session.commit()
        return event

    return _add


async def _digests(
    journal, item_id: int, start: datetime, count: int, *, step: timedelta = timedelta(days=1), **data: str
):
    for n in range(count):
        await journal(events.DIGEST_ITEM, item_id, start + n * step, **({"digest": "morning", "sec": "o"} | data))


async def test_three_digests_in_a_row_make_an_item_stale(session, make, now, journal) -> None:
    [card], [other] = (
        await make({"kind": "task", "title": "Сделать карту"}, now=now),
        await make({"kind": "task", "title": "Купить хлеб"}, now=now),
    )
    await _digests(journal, card.item.id, now + timedelta(hours=1), 3)
    await _digests(journal, other.item.id, now + timedelta(hours=1), 2)

    found = await stale.candidates(session)

    assert [(s.item_id, s.digests, s.moves) for s in found] == [(card.item.id, 3, 0)]
    assert (await stale.pick(session)).item_id == card.item.id


async def test_lines_of_one_digest_count_once(session, make, now, journal) -> None:
    [created] = await make({"kind": "task", "title": "Сделать карту"}, now=now)
    at = now + timedelta(hours=1)
    for sec in ("o", "v", "p"):  # one digest that listed it three times
        await journal(events.DIGEST_ITEM, created.item.id, at, digest="morning", sec=sec)
    await _digests(journal, created.item.id, at + timedelta(hours=12), 1)
    assert await stale.candidates(session) == []


async def test_an_action_starts_the_count_afresh(session, make, now, journal) -> None:
    [created] = await make({"kind": "task", "title": "Сделать карту"}, now=now)
    item_id = created.item.id
    await _digests(journal, item_id, now + timedelta(hours=1), 2)
    await journal(events.BUTTON, item_id, now + timedelta(days=1), action="i:edit")
    await _digests(journal, item_id, now + timedelta(days=1, hours=1), 2)
    assert await stale.candidates(session) == []

    await _digests(journal, item_id, now + timedelta(days=3), 1)
    assert [s.digests for s in await stale.candidates(session)] == [3]


async def test_a_mini_app_read_is_no_action_a_change_is(session, make, now, journal) -> None:
    [created] = await make({"kind": "task", "title": "Сделать карту"}, now=now)
    item_id = created.item.id
    await _digests(journal, item_id, now + timedelta(hours=1), 2)
    await journal(events.MINIAPP, item_id, now + timedelta(days=1), route="GET /api/items/{id}", status=200)
    await _digests(journal, item_id, now + timedelta(days=2, hours=1), 1)
    assert [s.item_id for s in await stale.candidates(session)] == [item_id]

    await journal(events.MINIAPP, item_id, now + timedelta(days=3), route="PATCH /api/items/{id}", status=200)
    assert await stale.candidates(session) == []


async def test_two_moves_make_an_item_stale(session, make, now, journal) -> None:
    [card], [snoozed] = (
        await make({"kind": "task", "title": "Сделать карту", "when": {"day": "+1"}}, now=now),
        await make({"kind": "task", "title": "Позвонить", "when": {"day": "+1"}}, now=now),
    )
    await journal(events.BUTTON, card.item.id, now + timedelta(hours=1), action="o:tmr", move=True)
    await journal(events.EDIT, card.item.id, now + timedelta(hours=2), move=True)  # "перенеси на пятницу"
    for hour in (1, 2, 3):  # snoozed by an hour, edited without a new time: no moves
        await journal(events.BUTTON, snoozed.item.id, now + timedelta(hours=hour), action="o:s60")
    await journal(events.EDIT, snoozed.item.id, now + timedelta(hours=4), move=False)

    found = await stale.candidates(session)

    assert [(s.item_id, s.digests, s.moves) for s in found] == [(card.item.id, 0, 2)]


async def test_a_question_is_asked_once_and_keep_starts_afresh(session, make, now, journal) -> None:
    [created] = await make({"kind": "task", "title": "Сделать карту"}, now=now)
    item_id = created.item.id
    await _digests(journal, item_id, now + timedelta(hours=1), 3)
    await journal(events.BUTTON, item_id, now + timedelta(days=2), action="o:tmr", move=True)
    await journal(events.STALE_ASK, item_id, now + timedelta(days=3), digests=3, moves=1)
    await _digests(journal, item_id, now + timedelta(days=3, hours=1), 5)
    assert await stale.candidates(session) == []  # asked and left unanswered: never again

    await journal(events.BUTTON, item_id, now + timedelta(days=6), action="st:keep")  # [Оставить]
    await _digests(journal, item_id, now + timedelta(days=6, hours=1), 2)
    assert await stale.candidates(session) == []
    await journal(events.BUTTON, item_id, now + timedelta(days=8), action="o:nw", move=True)
    assert await stale.candidates(session) == []  # one move since the question (the one before it does not count)
    await journal(events.BUTTON, item_id, now + timedelta(days=9), action="dy", move=True)
    assert [(s.digests, s.moves) for s in await stale.candidates(session)] == [(0, 2)]


async def test_done_someday_and_series_are_never_stale(session, make, now, journal, prefs) -> None:
    done, someday, series, card = (
        (await make({"kind": "task", "title": "Сделано"}, now=now))[0],
        (await make({"kind": "task", "title": "Когда-нибудь", "when": {"period": "someday"}}, now=now))[0],
        (await make({"kind": "task", "title": "Зарядка", "when": {"time": "09:00"}, "rrule": "FREQ=DAILY"}, now=now))[
            0
        ],
        (await make({"kind": "task", "title": "Сделать карту"}, now=now))[0],
    )
    await items.mark_done(session, done.occurrence.id, now, prefs)
    await session.commit()
    for created in (done, someday, series, card):
        await _digests(journal, created.item.id, now + timedelta(hours=1), 3)
    assert [s.item_id for s in await stale.candidates(session)] == [card.item.id]


async def test_the_item_saved_first_goes_first(session, make, now, journal) -> None:
    [newer] = await make({"kind": "task", "title": "Новое"}, now=now)
    [older] = await make({"kind": "task", "title": "Старое"}, now=now - timedelta(days=10))
    for created in (newer, older):
        await _digests(journal, created.item.id, now + timedelta(hours=1), 3)
    assert [s.item_id for s in await stale.candidates(session)] == [older.item.id, newer.item.id]
    assert [s.item_id for s in await stale.candidates(session, limit=1)] == [older.item.id]


async def test_digests_before_the_item_was_saved_do_not_count(session, make, now, journal) -> None:
    [created] = await make({"kind": "task", "title": "Сделать карту"}, now=now)
    await _digests(journal, created.item.id, now - timedelta(days=3), 3)  # (ids reused after a reset, say)
    assert await stale.candidates(session) == []


async def test_the_digests_of_one_day_count_once(session, make, now, journal, prefs, local) -> None:
    """A task due Tue 15:00 whose reminder got no reaction is in the morning, the evening ("Не сделано") and the
    next morning digest ("Просрочено"): two days, not "висит" 18 h after its time."""
    [created] = await make({"kind": "task", "title": "Позвонить в банк", "when": {"time": "15:00"}}, now=now)
    item_id = created.item.id
    await journal(events.DIGEST_ITEM, item_id, local(2026, 10, 6, 21), digest="evening", sec="n")
    await journal(events.DIGEST_ITEM, item_id, local(2026, 10, 7, 9), digest="morning", sec="v")
    await journal(events.DIGEST_ITEM, item_id, local(2026, 10, 7, 21), digest="evening", sec="n")
    assert await stale.candidates(session, prefs, now=local(2026, 10, 7, 21, 30)) == []

    await journal(events.DIGEST_ITEM, item_id, local(2026, 10, 8, 9), digest="morning", sec="v")
    found = await stale.candidates(session, prefs, now=local(2026, 10, 8, 9, 5))
    assert [(s.item_id, s.digests) for s in found] == [(item_id, 3)]


async def test_a_span_not_begun_is_not_stale(session, make, journal, prefs, local) -> None:
    """«Записаться к врачу» «в следующем месяце», saved Oct 1: three weekly reviews list it in October — it is not due
    until November, nothing hangs."""
    [created] = await make(
        {"kind": "task", "title": "Записаться к врачу", "when": {"period": "next_month"}}, now=local(2026, 10, 1, 9)
    )
    for day in (4, 11, 18):
        await journal(events.DIGEST_ITEM, created.item.id, local(2026, 10, day, 19), digest="weekly", sec="p")
    assert await stale.candidates(session, prefs, now=local(2026, 10, 18, 19, 30)) == []


async def test_the_reviews_span_lines_of_a_running_span_do_not_count(session, make, journal, prefs, local) -> None:
    """«в этом месяце»: the weekly review lists the span on purpose while it runs; only the other digests count."""
    [created] = await make(
        {"kind": "task", "title": "Сделать карту", "when": {"period": "this_month"}}, now=local(2026, 10, 1, 9)
    )
    item_id = created.item.id
    for day in (4, 11, 18):
        await journal(events.DIGEST_ITEM, item_id, local(2026, 10, day, 19), digest="weekly", sec="p")
    assert await stale.candidates(session, prefs, now=local(2026, 10, 19, 9)) == []

    for day in (19, 20):  # the morning digests: the span's last working days, say
        await journal(events.DIGEST_ITEM, item_id, local(2026, 10, day, 9), digest="morning", sec="p")
    await journal(events.DIGEST_ITEM, item_id, local(2026, 10, 21, 19), digest="weekly", sec="u")  # another section
    found = await stale.candidates(session, prefs, now=local(2026, 10, 21, 19, 30))
    assert [(s.item_id, s.digests) for s in found] == [(item_id, 3)]


async def test_a_ticked_step_is_an_action_on_its_parent(session, make, now, journal, prefs) -> None:
    """«Переезд» listed by three digests, a step ticked in its card after each: it is being worked on."""
    [parent] = await make({"kind": "task", "title": "Переезд", "when": {"day": "+1"}}, now=now)
    [step] = await items.add_steps(session, parent.item.id, [StepSpec(title="Упаковать вещи")], prefs, now)
    await session.commit()
    for n in range(3):
        for listed in (parent, step):
            await _digests(journal, listed.item.id, now + timedelta(days=n, hours=1), 1)
        await journal(events.BUTTON, step.item.id, now + timedelta(days=n, hours=2), action="c:done")
    assert await stale.candidates(session, prefs, now=now + timedelta(days=3)) == []  # neither the parent nor the step


async def test_asked_on_a_local_day(session, make, now, journal, prefs, local) -> None:
    [a] = await make({"kind": "task", "title": "A"}, now=now)
    [b] = await make({"kind": "task", "title": "B"}, now=now)
    await journal(events.STALE_ASK, a.item.id, local(2026, 10, 6, 0, 30))
    await journal(events.STALE_ASK, b.item.id, local(2026, 10, 7, 0, 30))
    assert await stale.asked_on(session, date(2026, 10, 6), prefs) == {a.item.id}
    assert await stale.asked_on(session, date(2026, 10, 8), prefs) == set()


# --- reminders nobody reacted to (§6) --------------------------------------------------------------------------


async def test_unanswered_reminders(session, make, now, journal, prefs, local) -> None:
    ignored, answered, done, before = [
        (await make({"kind": "task", "title": t, "when": {"time": "12:00"}}, now=now))[0]
        for t in ("Позвонить", "Отчёт", "Сделано", "Давнее")
    ]
    start, end = local(2026, 10, 5), local(2026, 10, 12)
    for created in (ignored, answered, done):
        await journal(
            events.REMINDER_SENT, created.item.id, local(2026, 10, 6, 12), occurrence_id=created.occurrence.id
        )
    await journal(events.REMINDER_SENT, before.item.id, local(2026, 10, 4, 12), occurrence_id=before.occurrence.id)
    await journal(events.BUTTON, answered.item.id, local(2026, 10, 6, 13), action="o:s60")
    await items.mark_done(session, done.occurrence.id, now, prefs)
    await session.commit()

    assert await stale.unanswered_reminders(session, start, end) == [(ignored.occurrence.id, ignored.item.id)]


async def test_unanswered_lists_an_item_once(session, make, now, journal, local) -> None:
    [created] = await make({"kind": "task", "title": "Позвонить", "when": {"time": "12:00"}}, now=now)
    occ_id = created.occurrence.id
    for hour in (12, 15):  # reminded twice (snoozed from the Mini App's list, say), never answered
        await journal(events.REMINDER_SENT, created.item.id, local(2026, 10, 6, hour), occurrence_id=occ_id)
    found = await stale.unanswered_reminders(session, local(2026, 10, 5), local(2026, 10, 12))
    assert found == [(occ_id, created.item.id)]


async def test_a_series_instance_left_missed_counts_as_unanswered(session, make, now, journal, local) -> None:
    [created] = await make(
        {"kind": "task", "title": "Таблетки", "when": {"time": "09:00"}, "rrule": "FREQ=DAILY"},
        now=now - timedelta(days=1),
    )
    occ = await session.get(Occurrence, created.occurrence.id)
    occ.status = "missed"  # superseded by the next instance
    await session.commit()
    await journal(events.REMINDER_SENT, created.item.id, local(2026, 10, 5, 9), occurrence_id=occ.id)
    found = await stale.unanswered_reminders(session, local(2026, 10, 5), local(2026, 10, 12))
    assert found == [(occ.id, created.item.id)]


# --- the weekly review's other sections (§6) -------------------------------------------------------------------


async def test_review_period_rows_this_and_next_week_or_month(session, make, local) -> None:
    sunday = local(2026, 10, 11, 19)  # the review of the week Oct 5–11
    specs = {
        "this_week": "Эта неделя",
        "next_week": "Следующая неделя",
        "this_month": "Этот месяц",
        "next_month": "Следующий месяц",
        "2026-12": "Декабрь",  # the month after next: not yet
    }
    made = {
        p: (await make({"kind": "task", "title": t, "when": {"period": p}}, now=sunday))[0] for p, t in specs.items()
    }
    [later_week] = await make({"kind": "task", "title": "Через неделю", "when": {"period": "next_week"}}, now=sunday)
    item = await session.get(Item, later_week.item.id)
    item.period_start, item.period_end = date(2026, 10, 19), date(2026, 10, 25)  # the week after next
    await session.commit()

    rows = await query.review_period_rows(
        session, weeks=(date(2026, 10, 5), date(2026, 10, 18)), months=(date(2026, 10, 1), date(2026, 11, 30))
    )

    expected = [made[p].item.id for p in ("this_week", "next_week", "this_month", "next_month")]
    assert [r.item_id for r in rows] == expected


async def test_someday_for_review_rotates_and_counts(session, make, now, journal) -> None:
    ids = [
        (
            await make(
                {"kind": "idea", "title": f"Идея {n}", "when": {"period": "someday"}}, now=now - timedelta(days=n)
            )
        )[0].item.id
        for n in range(4)
    ]  # ids[3] is the oldest
    await journal(events.DIGEST_ITEM, ids[3], now - timedelta(days=7), digest="weekly", sec="s")
    await journal(events.DIGEST_ITEM, ids[2], now - timedelta(days=14), digest="weekly", sec="s")
    await journal(events.DIGEST_ITEM, ids[1], now - timedelta(days=1), digest="morning", sec="p")  # not a review

    rows, total = await query.someday_for_review(session, 3)

    assert total == 4
    assert [r.item_id for r in rows] == [ids[1], ids[0], ids[2]]  # never shown (oldest first), then shown longest ago


async def test_undated_tasks(session, make, now, prefs) -> None:
    [plain] = await make({"kind": "task", "title": "Починить кран"}, now=now)  # the bot's 19:00: no date of its own
    [dated] = await make({"kind": "task", "title": "Созвон", "when": {"day": "+1", "time": "15:00"}}, now=now)
    [idea] = await make({"kind": "idea", "title": "Идея"}, now=now)
    [someday] = await make({"kind": "task", "title": "Потом", "when": {"period": "someday"}}, now=now)
    [week] = await make({"kind": "task", "title": "На неделе", "when": {"period": "this_week"}}, now=now)
    [cleared] = await make({"kind": "purchase", "title": "Лампочка", "when": {"day": "+1"}}, now=now)
    occ = await session.get(Occurrence, cleared.occurrence.id)
    occ.status = "cancelled"  # its date taken away
    await session.commit()
    del dated, idea, someday, week

    rows = await query.undated_tasks(session)

    assert [r.item_id for r in rows] == [plain.item.id, cleared.item.id]
    assert rows[1].due_at is None
