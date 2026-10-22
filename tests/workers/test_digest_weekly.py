"""services.digest, iteration 4: a span's item in the morning digest only on the span's first and last working day
(and the day the stale question asks about it, §4); the weekly review's data (§6)."""

from __future__ import annotations

from datetime import date, timedelta

from dumpbot.db.models import Event
from dumpbot.domain.prefs import Prefs
from dumpbot.services import digest, events, query


async def _morning_titles(session, at) -> list[str]:
    data = await digest.digest_data(session, "morning", Prefs(), at)
    return [r.title for r in data.agenda.period_items]


async def test_a_week_item_is_in_the_morning_digest_on_monday_and_friday(session, make_items, local) -> None:
    await make_items(
        {"kind": "task", "title": "Raspberry Pi", "when": {"period": "this_week"}}, now=local(2026, 10, 5, 8)
    )
    shown = [day for day in range(5, 12) if await _morning_titles(session, local(2026, 10, day, 9)) == ["Raspberry Pi"]]
    assert shown == [5, 9]  # Mon (the first day), Fri (the last working day); not Tue–Thu, not the weekend
    agenda = await query.agenda(session, Prefs(), local(2026, 10, 7, 9))
    assert [r.title for r in agenda.period_items] == ["Raspberry Pi"]  # /today still lists it every day


async def test_an_item_saved_mid_span_is_shown_the_day_it_was_saved(session, make_items, local) -> None:
    await make_items({"kind": "task", "title": "Шины", "when": {"period": "this_week"}}, now=local(2026, 10, 7, 7))
    shown = [day for day in range(5, 12) if await _morning_titles(session, local(2026, 10, day, 9))]
    assert shown == [7, 9]


async def test_a_month_ends_on_its_last_working_day(session, make_items, local) -> None:
    await make_items({"kind": "task", "title": "Шины", "when": {"period": "this_month"}}, now=local(2026, 10, 1, 8))
    shown = [day for day in range(1, 32) if await _morning_titles(session, local(2026, 10, day, 9))]
    assert shown == [1, 30]  # Oct 31 is a Saturday


async def test_the_item_of_the_stale_question_is_shown_that_day(session, make_items, local) -> None:
    [created] = await make_items(
        {"kind": "task", "title": "Карта", "when": {"period": "this_week"}}, now=local(2026, 10, 5, 8)
    )
    wed = local(2026, 10, 7, 9)
    assert await _morning_titles(session, wed) == []
    data = await digest.build_digest(session, "morning", Prefs(), wed, ask=created.item.id)  # asked right after it
    assert data is not None and [r.title for r in data.agenda.period_items] == ["Карта"]
    session.add(Event(type=events.STALE_ASK, item_id=created.item.id, created_at=local(2026, 10, 7, 9, 1)))
    await session.commit()
    assert await _morning_titles(session, local(2026, 10, 7, 12)) == ["Карта"]  # a re-render after [✅ N]
    assert await _morning_titles(session, local(2026, 10, 8, 9)) == []


async def test_a_morning_with_only_a_span_mid_week_is_empty(session, make_items, local) -> None:
    await make_items(
        {"kind": "task", "title": "Raspberry Pi", "when": {"period": "this_week"}}, now=local(2026, 10, 5, 8)
    )
    assert await digest.build_digest(session, "morning", Prefs(), local(2026, 10, 6, 9)) is None
    assert await digest.build_digest(session, "morning", Prefs(), local(2026, 10, 9, 9)) is not None


async def test_the_weekly_review_sections(session, make_items, local) -> None:
    sunday = local(2026, 10, 11, 19)
    week, nxt, month, far = [
        (await make_items({"kind": "task", "title": t, "when": {"period": p}}, now=local(2026, 10, 5, 8)))[0]
        for t, p in (
            ("Эта неделя", "this_week"),
            ("След. неделя", "next_week"),
            ("Месяц", "this_month"),
            ("Дек", "2026-12"),
        )
    ]
    ideas = [
        (await make_items({"kind": "idea", "title": f"Идея {n}", "when": {"period": "someday"}}, now=sunday))[0]
        for n in range(7)
    ]
    [call] = await make_items(
        {"kind": "task", "title": "Позвонить", "when": {"time": "12:00"}}, now=local(2026, 10, 8, 8)
    )
    [plain] = await make_items({"kind": "task", "title": "Починить кран"}, now=local(2026, 10, 1, 8))
    for item_id, occ_id in ((call.item.id, call.occurrence.id), (week.item.id, week.occurrence.id)):
        session.add(
            Event(
                type=events.REMINDER_SENT, item_id=item_id, occurrence_id=occ_id, created_at=sunday - timedelta(days=2)
            )
        )
    await session.commit()

    data = await digest.weekly_data(session, Prefs(), sunday)

    assert data.day == date(2026, 10, 11)
    assert [r.item_id for r in data.period] == [week.item.id, nxt.item.id, month.item.id]
    assert (
        len(data.someday) == 5
        and data.someday_total == 7
        and {r.item_id for r in data.someday} <= {i.item.id for i in ideas}
    )
    assert [r.item_id for r in data.unanswered] == [call.item.id]  # the week item is in its own section already
    assert [r.item_id for r in data.undated] == [plain.item.id]
    assert far.item.id not in {r.item_id for r in (*data.period, *data.undated)}


async def test_an_empty_review_is_not_built(session, make_items, local) -> None:
    sunday = local(2026, 10, 11, 19)
    assert await digest.build_weekly(session, Prefs(), sunday) is None
    await make_items({"kind": "event", "title": "Созвон", "when": {"day": "+1", "time": "10:00"}}, now=sunday)
    assert await digest.build_weekly(session, Prefs(), sunday) is None  # a dated plan is no business of the review


async def test_a_re_render_of_the_review_lists_the_same_someday_items(session, make_items, local) -> None:
    sunday = local(2026, 10, 11, 19)
    for n in range(7):
        await make_items(
            {"kind": "idea", "title": f"Идея {n}", "when": {"period": "someday"}}, now=local(2026, 10, 1 + n)
        )
    sent = await digest.weekly_data(session, Prefs(), sunday)
    for row in sent.someday:  # the review went out: its lines are journaled
        session.add(Event(type=events.DIGEST_ITEM, item_id=row.item_id, created_at=sunday, data={"digest": "weekly"}))
    await session.commit()

    again = await digest.weekly_data(session, Prefs(), local(2026, 10, 11, 12))  # a press: the day of the button
    nxt = await digest.weekly_data(session, Prefs(), local(2026, 10, 18, 19))  # next Sunday: the others first

    assert [r.item_id for r in again.someday] == [r.item_id for r in sent.someday]
    assert {r.item_id for r in nxt.someday[:2]}.isdisjoint({r.item_id for r in sent.someday})
