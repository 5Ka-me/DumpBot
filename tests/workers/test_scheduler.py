"""workers.scheduler: reminders, recovery, recurrence, digests, heartbeat, run loop."""

from __future__ import annotations

import asyncio
from datetime import time, timedelta
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import update

from dumpbot.bot.cbdata import DigestCb, OccCb
from dumpbot.db.models import Item, Occurrence, OnceKey, Reminder
from dumpbot.domain.prefs import Prefs
from dumpbot.services import items
from dumpbot.workers.scheduler import Scheduler

OWNER = 4242


def buttons(call: dict) -> list[str]:
    markup = call.get("reply_markup")
    return [] if markup is None else [b.callback_data for row in markup.inline_keyboard for b in row]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def network_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=SendMessage(chat_id=OWNER, text="x"), message="timeout")


@pytest.fixture
async def no_digests(set_prefs) -> Prefs:
    return await set_prefs(digest_morning_enabled=False, digest_evening_enabled=False)


@pytest.fixture
async def meeting(make_items, t0):
    """Event today 11:00 (created at 10:00): one reminder at 10:55."""
    [created] = await make_items({"kind": "event", "title": "Созвон", "when": {"time": "11:00"}}, now=t0)
    return created


async def set_reminder(sessionmaker, reminder_id: int, **values) -> None:
    async with sessionmaker() as session:
        await session.execute(update(Reminder).where(Reminder.id == reminder_id).values(**values))
        await session.commit()


# --- reminders ------------------------------------------------------------------------------------------------------


async def test_due_reminder_is_sent_with_buttons_and_marked_sent(
    ctx, no_digests, meeting, bot, frozen, local, get, bot_messages
) -> None:
    [reminder] = meeting.reminders
    assert reminder.fire_at == local(2026, 10, 6, 10, 55)
    scheduler = Scheduler(ctx)

    await scheduler.tick(local(2026, 10, 6, 10, 54))
    assert bot.sent == []

    now = frozen.set(local(2026, 10, 6, 10, 55))
    await scheduler.tick(now)
    [msg] = bot.sent
    assert "Созвон" in msg["text"] and "через 5 мин" in msg["text"]
    occ_id = meeting.occurrence.id
    assert OccCb(a="done", id=occ_id).pack() in buttons(msg)
    assert OccCb(a="s15", id=occ_id).pack() in buttons(msg)
    stored = await get(Reminder, reminder.id)
    assert (stored.status, stored.sent_at, stored.tg_message_id, stored.claimed_at) == ("sent", now, 9001, None)
    [bm] = await bot_messages()
    assert (bm.purpose, bm.item_id, bm.occurrence_id) == ("reminder", meeting.item.id, occ_id)

    await scheduler.tick(now + timedelta(seconds=30))
    assert len(bot.sent) == 1


async def test_stale_reminder_beyond_grace_is_missed(ctx, no_digests, meeting, bot, local, get) -> None:
    [reminder] = meeting.reminders
    await Scheduler(ctx).tick(reminder.fire_at + timedelta(minutes=361))
    assert bot.sent == []
    assert (await get(Reminder, reminder.id)).status == "missed"


async def test_stuck_sending_is_recovered(ctx, no_digests, meeting, bot, sessionmaker, local, get) -> None:
    [reminder] = meeting.reminders
    now = local(2026, 10, 6, 11, 0)
    await set_reminder(sessionmaker, reminder.id, status="sending", claimed_at=now - timedelta(minutes=3))
    await Scheduler(ctx).tick(now)
    assert (await get(Reminder, reminder.id)).status == "sent"
    assert len(bot.sent) == 1


async def test_recently_claimed_sending_is_left_alone(ctx, no_digests, meeting, bot, sessionmaker, local, get) -> None:
    [reminder] = meeting.reminders
    now = local(2026, 10, 6, 11, 0)
    await set_reminder(sessionmaker, reminder.id, status="sending", claimed_at=now - timedelta(minutes=1))
    await Scheduler(ctx).tick(now)
    assert (await get(Reminder, reminder.id)).status == "sending"
    assert bot.sent == []


async def test_deleted_item_or_done_occurrence_cancels(
    ctx, no_digests, make_items, t0, bot, sessionmaker, local, get
) -> None:
    deleted, done = await make_items(
        {"kind": "event", "title": "Удалю", "when": {"time": "11:00"}},
        {"kind": "event", "title": "Сделаю", "when": {"time": "11:00"}},
        now=t0,
    )
    async with sessionmaker() as session:
        await items.delete_item(session, deleted.item.id, t0)
        await session.commit()
        # Done without going through items (a race with the scheduler): the reminder is still pending.
        await session.execute(update(Occurrence).where(Occurrence.id == done.occurrence.id).values(status="done"))
        await session.commit()
    await Scheduler(ctx).tick(local(2026, 10, 6, 10, 56))
    assert bot.sent == []
    assert (await get(Reminder, deleted.reminders[0].id)).status == "cancelled"
    assert (await get(Reminder, done.reminders[0].id)).status == "cancelled"


async def test_send_failures_count_attempts_then_fail(ctx, no_digests, meeting, bot, local, get) -> None:
    [reminder] = meeting.reminders
    bot.fail("send_message", *(network_error() for _ in range(5)))
    scheduler = Scheduler(ctx)
    now = local(2026, 10, 6, 10, 55)
    await scheduler.tick(now)
    stored = await get(Reminder, reminder.id)
    assert (stored.status, stored.attempts, stored.claimed_at) == ("pending", 1, None)
    for i in range(4):
        await scheduler.tick(now + timedelta(seconds=30 * (i + 1)))
    stored = await get(Reminder, reminder.id)
    assert (stored.status, stored.attempts) == ("failed", 5)
    assert len(bot.sent) == 5


async def test_recurring_series_gets_its_next_instance(ctx, no_digests, make_items, local, occurrences_of) -> None:
    [created] = await make_items(
        {"kind": "task", "title": "Зарядка", "when": {"time": "09:00"}, "rrule": "FREQ=DAILY"},
        now=local(2026, 10, 6, 10, 0),
    )
    [first] = await occurrences_of(created.item.id)
    assert first.due_at == local(2026, 10, 7, 9, 0)
    await Scheduler(ctx).tick(local(2026, 10, 7, 9, 30))
    assert [o.due_at for o in await occurrences_of(created.item.id)] == [
        local(2026, 10, 7, 9, 0),
        local(2026, 10, 8, 9, 0),
    ]


# --- digests --------------------------------------------------------------------------------------------------------


async def test_morning_digest_once_per_day_inside_its_window(ctx, make_items, t0, bot, local, get) -> None:
    await make_items({"kind": "task", "title": "Спорт", "when": {"day": "+1", "time": "18:00"}}, now=t0)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 7, 8, 59))
    assert bot.sent == []
    await scheduler.tick(local(2026, 10, 7, 9, 0))
    [digest] = bot.sent
    assert "Доброе утро" in digest["text"] and "Спорт" in digest["text"]
    await scheduler.tick(local(2026, 10, 7, 9, 30))
    assert len(bot.sent) == 1
    assert await get(OnceKey, "digest:morning:2026-10-07") is not None


async def test_morning_digest_waits_for_the_end_of_quiet_hours(ctx, set_prefs, make_items, t0, bot, local) -> None:
    await set_prefs(digest_morning_time=time(7, 0))  # quiet hours 23:00-08:00
    await make_items({"kind": "task", "title": "Спорт", "when": {"day": "+1", "time": "18:00"}}, now=t0)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 7, 7, 30))
    assert bot.sent == []
    await scheduler.tick(local(2026, 10, 7, 8, 0))
    assert len(bot.sent) == 1


async def test_digest_window_closes_after_three_hours(ctx, make_items, t0, bot, local) -> None:
    await make_items({"kind": "task", "title": "Спорт", "when": {"day": "+1", "time": "18:00"}}, now=t0)
    await Scheduler(ctx).tick(local(2026, 10, 7, 12, 0))
    assert bot.sent == []


async def test_empty_digest_is_skipped(ctx, bot, local, get) -> None:
    await Scheduler(ctx).tick(local(2026, 10, 7, 9, 0))
    assert bot.sent == []
    assert await get(OnceKey, "digest:morning:2026-10-07") is not None  # not rebuilt every tick


async def test_disabled_digest_is_not_sent(ctx, set_prefs, make_items, t0, bot, local) -> None:
    await set_prefs(digest_morning_enabled=False)
    await make_items({"kind": "task", "title": "Спорт", "when": {"day": "+1", "time": "18:00"}}, now=t0)
    await Scheduler(ctx).tick(local(2026, 10, 7, 9, 0))
    assert bot.sent == []


async def test_evening_digest_with_done_and_open(ctx, make_items, t0, bot, sessionmaker, local, get) -> None:
    done, _ = await make_items(
        {"kind": "task", "title": "Отчёт", "when": {"time": "12:00"}},
        {"kind": "task", "title": "Звонок", "when": {"time": "15:00"}},
        now=t0,
    )
    async with sessionmaker() as session:
        await items.mark_done(session, done.occurrence.id, local(2026, 10, 6, 12, 5), Prefs())
        await session.commit()
    await Scheduler(ctx).tick(local(2026, 10, 6, 21, 0))
    [digest] = bot.sent
    assert "Итоги дня" in digest["text"] and "Сделано 1 из 2" in digest["text"]
    assert buttons(digest) == [DigestCb(a="tmr").pack()]
    assert await get(OnceKey, "digest:evening:2026-10-06") is not None


async def test_digest_send_failure_is_retried_next_tick(ctx, make_items, t0, bot, local, get) -> None:
    await make_items({"kind": "task", "title": "Спорт", "when": {"day": "+1", "time": "18:00"}}, now=t0)
    bot.fail("send_message", network_error())
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 7, 9, 0))
    assert await get(OnceKey, "digest:morning:2026-10-07") is None
    await scheduler.tick(local(2026, 10, 7, 9, 1))
    assert len(bot.sent) == 2  # the failed attempt and the successful one
    assert await get(OnceKey, "digest:morning:2026-10-07") is not None


# --- heartbeat / loop -----------------------------------------------------------------------------------------------


async def test_heartbeat_is_written_and_errors_are_ignored(ctx, settings, local, tmp_path: Path) -> None:
    now = local(2026, 10, 6, 10, 0)
    await Scheduler(ctx).tick(now)
    assert _read(Path(settings.heartbeat_file)) == now.isoformat()

    settings.heartbeat_file = str(tmp_path)  # a directory: writing fails, the tick does not
    await Scheduler(ctx).tick(now)


async def test_sleep_until_next_reminder(ctx, no_digests, meeting, local) -> None:
    scheduler = Scheduler(ctx)
    assert await scheduler._sleep_for(local(2026, 10, 6, 10, 54, 50)) == pytest.approx(10.0)
    assert await scheduler._sleep_for(local(2026, 10, 6, 10, 0)) == 30.0  # capped by the interval
    assert await scheduler._sleep_for(local(2026, 10, 6, 10, 56)) == 30.0  # overdue ones wait for the interval


async def test_run_loop_sends_and_cancels(ctx, no_digests, meeting, bot, frozen, local, get) -> None:
    frozen.set(local(2026, 10, 6, 10, 55))
    task = asyncio.create_task(Scheduler(ctx).run())
    for _ in range(200):
        if bot.sent and (await get(Reminder, meeting.reminders[0].id)).status == "sent":
            break
        await asyncio.sleep(0.02)
    assert len(bot.sent) == 1
    ctx.scheduler_wakeup.set()  # wakes up, ticks again: nothing new
    await asyncio.sleep(0.2)
    assert len(bot.sent) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_tick_survives_a_failing_step(ctx, no_digests, meeting, bot, monkeypatch, local, get) -> None:
    async def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(items, "ensure_recurring", broken)
    await Scheduler(ctx).tick(local(2026, 10, 6, 10, 55))
    assert (await get(Reminder, meeting.reminders[0].id)).status == "sent"
    assert (await get(Item, meeting.item.id)).status == "open"
