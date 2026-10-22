"""workers.scheduler and the events journal (docs/features/iteration-4.md §5): a sent reminder and every line of a
sent digest become events; a failed send or an empty digest does not; a failed journal write breaks nothing."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import select

from dumpbot.db.models import Event, Reminder
from dumpbot.domain.prefs import Prefs
from dumpbot.services import events, items
from dumpbot.workers.scheduler import Scheduler

OWNER = 4242


@pytest.fixture
async def no_digests(set_prefs) -> Prefs:
    return await set_prefs(digest_morning_enabled=False, digest_evening_enabled=False)


async def _events(db) -> list[Event]:
    return await db(select(Event).order_by(Event.id))


async def test_a_sent_reminder_is_journaled(ctx, no_digests, make_items, t0, bot, frozen, local, db) -> None:
    [created] = await make_items({"kind": "event", "title": "Созвон", "when": {"time": "10:55"}}, now=t0)
    [reminder] = created.reminders
    now = frozen.set(local(2026, 10, 6, 10, 58))

    await Scheduler(ctx).tick(now)

    assert len(bot.sent) == 1
    [row] = await _events(db)
    assert (row.type, row.source, row.item_id, row.occurrence_id) == (
        "reminder_sent",
        "reminder",
        created.item.id,
        created.occurrence.id,
    )
    assert row.data == {"reminder": reminder.id, "late_min": 3}
    assert row.created_at == now


async def test_a_reminder_held_over_quiet_hours_says_so(ctx, no_digests, make_items, t0, bot, sessionmaker, local, db):
    [created] = await make_items({"kind": "event", "title": "Созвон", "when": {"time": "10:55"}}, now=t0)
    [reminder] = created.reminders
    async with sessionmaker() as session:  # due at 22:40, held until the end of quiet hours (08:00)
        stored = await session.get(Reminder, reminder.id)
        stored.held_from = local(2026, 10, 6, 22, 40)
        stored.fire_at = local(2026, 10, 7, 8, 0)
        await session.commit()

    await Scheduler(ctx).tick(local(2026, 10, 7, 8, 0))

    [row] = await _events(db)
    assert row.data == {"reminder": reminder.id, "late_min": 560, "held": True}


async def test_a_failed_send_is_not_journaled(ctx, no_digests, make_items, t0, bot, local, db) -> None:
    await make_items({"kind": "event", "title": "Созвон", "when": {"time": "10:55"}}, now=t0)
    bot.fail("send_message", TelegramNetworkError(method=SendMessage(chat_id=OWNER, text="x"), message="timeout"))
    await Scheduler(ctx).tick(local(2026, 10, 6, 10, 55))
    assert await _events(db) == []


async def test_a_failed_journal_write_breaks_nothing(
    ctx, no_digests, make_items, t0, bot, local, get, monkeypatch, caplog
) -> None:
    [created] = await make_items({"kind": "event", "title": "Созвон", "when": {"time": "10:55"}}, now=t0)

    async def broken(*args, **kwargs):
        raise RuntimeError("no events table")

    monkeypatch.setattr(events, "_insert", broken)
    await Scheduler(ctx).tick(local(2026, 10, 6, 10, 55))

    assert len(bot.sent) == 1
    assert (await get(Reminder, created.reminders[0].id)).status == "sent"
    assert "could not write 1 journal event(s) (reminder_sent): RuntimeError" in caplog.text


async def test_every_line_of_the_morning_digest_is_journaled(ctx, set_prefs, make_items, t0, bot, local, db) -> None:
    await set_prefs(digest_evening_enabled=False)
    [sport] = await make_items({"kind": "task", "title": "Спорт", "when": {"day": "+1", "time": "18:00"}}, now=t0)
    overdue_at = t0 - timedelta(days=2)
    [late] = await make_items({"kind": "task", "title": "Отчёт", "when": {"time": "12:00"}}, now=overdue_at)

    await Scheduler(ctx).tick(local(2026, 10, 7, 9, 0))

    [digest] = bot.sent
    assert "Спорт" in digest["text"]
    rows = [e for e in await _events(db) if e.type == "digest_item"]
    assert {(e.item_id, e.data["sec"]) for e in rows} == {(sport.item.id, "o"), (late.item.id, "v")}
    assert all((e.source, e.data["digest"]) == ("digest", "morning") for e in rows)
    assert {e.occurrence_id for e in rows if e.item_id == sport.item.id} == {sport.occurrence.id}


async def test_the_evening_digest_journals_done_and_not_done(
    ctx, set_prefs, make_items, t0, bot, sessionmaker, local, db
) -> None:
    await set_prefs(digest_morning_enabled=False)
    done, call = await make_items(
        {"kind": "task", "title": "Отчёт", "when": {"time": "12:00"}},
        {"kind": "task", "title": "Звонок", "when": {"time": "14:00"}},
        now=t0,
    )
    async with sessionmaker() as session:
        await items.mark_done(session, done.occurrence.id, local(2026, 10, 6, 12, 5), Prefs())
        await session.commit()

    await Scheduler(ctx).tick(local(2026, 10, 6, 21, 0))

    rows = [e for e in await _events(db) if e.type == "digest_item"]
    assert sorted((e.item_id, e.data["digest"], e.data["sec"]) for e in rows) == sorted(
        [(done.item.id, "evening", "d"), (call.item.id, "evening", "m")]
    )


async def test_an_empty_digest_journals_nothing(ctx, bot, local, db) -> None:
    await Scheduler(ctx).tick(local(2026, 10, 7, 9, 0))
    assert bot.sent == [] and await _events(db) == []
