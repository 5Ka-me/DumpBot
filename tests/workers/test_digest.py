"""services.digest.build_digest."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from dumpbot.db.models import OnceKey
from dumpbot.domain.prefs import Prefs
from dumpbot.services import digest, items


async def test_empty_day_gives_no_digest(session, t0) -> None:
    assert await digest.build_digest(session, "morning", Prefs(), t0) is None
    assert await digest.build_digest(session, "evening", Prefs(), t0) is None


async def test_morning_has_today_and_overdue(session, make_items, t0, local) -> None:
    await make_items({"kind": "task", "title": "Сегодня", "when": {"time": "18:00"}}, now=t0)
    await make_items(
        {"kind": "task", "title": "Вчерашнее", "when": {"day": "+1", "time": "12:00"}}, now=t0 - timedelta(days=2)
    )
    await make_items(
        {"kind": "event", "title": "Планёрка", "when": {"time": "09:30"}, "rrule": "FREQ=DAILY"},
        now=local(2026, 10, 6, 8, 0),
    )
    data = await digest.build_digest(session, "morning", Prefs(), t0)
    assert data is not None and data.kind == "morning"
    assert data.agenda.day.isoformat() == "2026-10-06"
    assert [r.title for r in data.agenda.open] == ["Планёрка", "Сегодня"]
    assert [r.title for r in data.agenda.overdue] == ["Вчерашнее"]
    assert data.recurring_today == 1


async def test_done_only_day_is_an_evening_digest_but_no_morning_one(session, make_items, t0) -> None:
    [created] = await make_items({"kind": "task", "title": "Сделал", "when": {"time": "12:00"}}, now=t0)
    await items.mark_done(session, created.occurrence.id, t0, Prefs())
    await session.commit()
    assert await digest.build_digest(session, "morning", Prefs(), t0) is None
    evening = await digest.build_digest(session, "evening", Prefs(), t0)
    assert evening is not None and [r.title for r in evening.agenda.done] == ["Сделал"]


async def test_unknown_kind(session, t0) -> None:
    with pytest.raises(ValueError):
        await digest.build_digest(session, "weekly", Prefs(), t0)


async def test_morning_carries_yesterday_when_its_evening_digest_was_folded(session, make_items, t0, local) -> None:
    """Iteration 3, §5: the evening digest of Tue was missed (the bot slept): Wed's morning digest has Tue's agenda."""
    done, _ = await make_items(
        {"kind": "task", "title": "Отчёт", "when": {"time": "12:00"}},
        {"kind": "task", "title": "Звонок", "when": {"time": "20:00"}},
        now=t0,
    )
    await items.mark_done(session, done.occurrence.id, local(2026, 10, 6, 12, 5), Prefs())
    await session.commit()
    wed = local(2026, 10, 7, 9, 0)
    assert (await digest.build_digest(session, "morning", Prefs(), wed)).yesterday is None  # nothing folded

    session.add(OnceKey(key=digest.folded_key(date(2026, 10, 6))))
    await session.commit()
    data = await digest.build_digest(session, "morning", Prefs(), wed)
    assert data is not None and data.yesterday is not None
    assert data.yesterday.day == date(2026, 10, 6)
    assert [r.title for r in data.yesterday.open] == ["Звонок"]
    assert [r.title for r in data.yesterday.done] == ["Отчёт"]
    assert (await digest.build_digest(session, "evening", Prefs(), wed)) is not None
    assert (await digest.digest_data(session, "evening", Prefs(), wed)).yesterday is None  # the morning's only


async def test_folded_yesterday_alone_makes_a_morning_digest(session, make_items, t0, local) -> None:
    [done] = await make_items({"kind": "task", "title": "Отчёт", "when": {"time": "12:00"}}, now=t0)
    await items.mark_done(session, done.occurrence.id, local(2026, 10, 6, 12, 5), Prefs())
    session.add(OnceKey(key=digest.folded_key(date(2026, 10, 6))))
    await session.commit()
    data = await digest.build_digest(session, "morning", Prefs(), local(2026, 10, 7, 9, 0))
    assert data is not None and data.agenda.open == [] and [r.title for r in data.yesterday.done] == ["Отчёт"]
