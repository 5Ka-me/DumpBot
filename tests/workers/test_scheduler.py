"""workers.scheduler: reminders, recovery, recurrence, digests, heartbeat, run loop."""

from __future__ import annotations

import asyncio
from datetime import time, timedelta
from pathlib import Path

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import SendMessage
from sqlalchemy import update

from dumpbot.bot.cbdata import DigestCb, ListCb, OccCb
from dumpbot.db.models import Item, Occurrence, OnceKey, Reminder
from dumpbot.domain.prefs import Prefs
from dumpbot.services import items
from dumpbot.watchdog import Watchdog
from dumpbot.workers import scheduler as scheduler_module
from dumpbot.workers.scheduler import Scheduler

OWNER = 4242


def buttons(call: dict) -> list[str]:
    markup = call.get("reply_markup")
    return [] if markup is None else [b.callback_data for row in markup.inline_keyboard for b in row]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def network_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=SendMessage(chat_id=OWNER, text="x"), message="timeout")


def server_error() -> TelegramServerError:
    return TelegramServerError(method=SendMessage(chat_id=OWNER, text="x"), message="Bad Gateway")


def long_flood_wait() -> TelegramRetryAfter:
    """Longer than the sender sleeps through itself."""
    return TelegramRetryAfter(method=SendMessage(chat_id=OWNER, text="x"), message="Too Many Requests", retry_after=120)


def bad_request() -> TelegramBadRequest:
    return TelegramBadRequest(method=SendMessage(chat_id=OWNER, text="x"), message="can't parse entities")


@pytest.fixture
async def no_digests(set_prefs) -> Prefs:
    return await set_prefs(digest_morning_enabled=False, digest_evening_enabled=False)


@pytest.fixture
async def meeting(make_items, t0):
    """Event today 10:55 (created at 10:00): one reminder at 10:55 — the item's time is its notification time."""
    [created] = await make_items({"kind": "event", "title": "Созвон", "when": {"time": "10:55"}}, now=t0)
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
    assert msg["text"] == "🔔 <b>Созвон</b>"  # no "через 5 мин": it comes at the item's time
    occ_id = meeting.occurrence.id
    assert OccCb(a="done", id=occ_id).pack() in buttons(msg)
    assert OccCb(a="s180", id=occ_id).pack() in buttons(msg)  # iteration 4, §3: [⏰ 3 часа], no 🌙 any more
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
        {"kind": "event", "title": "Удалю", "when": {"time": "10:55"}},
        {"kind": "event", "title": "Сделаю", "when": {"time": "10:55"}},
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


@pytest.mark.parametrize("error", [network_error, server_error, long_flood_wait])
async def test_telegram_outage_is_waited_out(ctx, no_digests, meeting, bot, local, get, error) -> None:
    # Telegram is unreachable for 10.5 minutes after fire_at (a router reboot): the reminder still arrives.
    [reminder] = meeting.reminders
    bot.fail("send_message", *(error() for _ in range(21)))
    scheduler = Scheduler(ctx)
    now = local(2026, 10, 6, 10, 55)
    for i in range(21):
        await scheduler.tick(now + timedelta(seconds=30 * i))
        stored = await get(Reminder, reminder.id)
        assert (stored.status, stored.attempts, stored.claimed_at) == ("pending", 0, None)
    await scheduler.tick(now + timedelta(minutes=10, seconds=30))
    stored = await get(Reminder, reminder.id)
    assert (stored.status, stored.attempts) == ("sent", 0)
    assert len(bot.sent) == 22 and "Созвон" in bot.sent[-1]["text"]


async def test_reminder_is_missed_if_the_outage_outlasts_the_grace(ctx, no_digests, meeting, bot, local, get) -> None:
    [reminder] = meeting.reminders
    bot.fail("send_message", network_error())
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 10, 55))
    await scheduler.tick(reminder.fire_at + timedelta(minutes=361))
    assert (await get(Reminder, reminder.id)).status == "missed"
    assert len(bot.sent) == 1


async def test_permanent_send_failures_count_attempts_then_fail(ctx, no_digests, meeting, bot, local, get) -> None:
    [reminder] = meeting.reminders
    bot.fail("send_message", *(bad_request() for _ in range(5)))
    scheduler = Scheduler(ctx)
    now = local(2026, 10, 6, 10, 55)
    await scheduler.tick(now)
    stored = await get(Reminder, reminder.id)
    assert (stored.status, stored.attempts, stored.claimed_at) == ("pending", 1, None)
    for i in range(5):
        await scheduler.tick(now + timedelta(seconds=30 * (i + 1)))
    stored = await get(Reminder, reminder.id)
    assert (stored.status, stored.attempts) == ("failed", 5)
    assert len(bot.sent) == 5  # not retried any more


async def test_transient_failure_leaves_the_rest_of_the_batch_for_the_next_tick(
    ctx, no_digests, make_items, t0, bot, local, get, monkeypatch
) -> None:
    first, second = await make_items(
        {"kind": "event", "title": "Созвон", "when": {"time": "10:55"}},
        {"kind": "event", "title": "Обед", "when": {"time": "10:55"}},
        now=t0,
    )
    monkeypatch.setattr(scheduler_module, "CLAIM_LIMIT", 2)  # a full claim: normally ticks again right away
    bot.fail("send_message", network_error())
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 10, 55))
    assert len(bot.sent) == 1  # the second one is not tried against a dead network
    assert scheduler._backlog is False  # no immediate re-tick: it would spin while Telegram is down
    for created in (first, second):
        stored = await get(Reminder, created.reminders[0].id)
        assert (stored.status, stored.attempts, stored.claimed_at) == ("pending", 0, None)
    await scheduler.tick(local(2026, 10, 6, 10, 55, 30))
    assert len(bot.sent) == 3
    for created in (first, second):
        assert (await get(Reminder, created.reminders[0].id)).status == "sent"


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
    done, call = await make_items(
        {"kind": "task", "title": "Отчёт", "when": {"time": "12:00"}},
        {"kind": "task", "title": "Звонок", "when": {"time": "14:00"}},  # its reminder is over the 6 h grace at 21:00
        now=t0,
    )
    async with sessionmaker() as session:
        await items.mark_done(session, done.occurrence.id, local(2026, 10, 6, 12, 5), Prefs())
        await session.commit()
    await Scheduler(ctx).tick(local(2026, 10, 6, 21, 0))
    [digest] = bot.sent
    assert "Итоги дня" in digest["text"] and "Сделано 1 из 2" in digest["text"]
    assert buttons(digest) == [  # [✅ 1] for the not-done call, then the move button
        ListCb(a="done", k="e", d="20261006", s="m", n=1, o=call.occurrence.id, i=call.item.id).pack(),
        DigestCb(a="tmr", d="20261006").pack(),
    ]
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


async def test_run_loop_reports_progress_to_the_watchdog(ctx, no_digests, monkeypatch) -> None:
    clock = [0.0]
    ctx.watchdog = Watchdog(monotonic=lambda: clock[0])
    scheduler = Scheduler(ctx)
    assert scheduler.stall_limit_s == 15 * 60
    ticks: list[object] = []
    real_tick = scheduler.tick

    async def counted_tick(now) -> None:
        await real_tick(now)
        ticks.append(now)

    async def wait_ticks(n: int) -> None:
        for _ in range(200):
            if len(ticks) >= n:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"{len(ticks)} tick(s)")

    monkeypatch.setattr(scheduler, "tick", counted_tick)
    task = asyncio.create_task(scheduler.run())
    await wait_ticks(1)  # now sleeping for the interval
    clock[0] += 15 * 60 + 1
    assert ctx.watchdog.stalled() == {"scheduler": 15 * 60 + 1}

    ctx.scheduler_wakeup.set()  # the next iteration beats
    await wait_ticks(2)
    assert ctx.watchdog.stalled() == {}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_stall_limit_covers_long_intervals(ctx, settings) -> None:
    settings.scheduler_interval_s = 600
    assert Scheduler(ctx).stall_limit_s == 1800


async def test_tick_survives_a_failing_step(ctx, no_digests, meeting, bot, monkeypatch, local, get) -> None:
    async def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(items, "ensure_recurring", broken)
    await Scheduler(ctx).tick(local(2026, 10, 6, 10, 55))
    assert (await get(Reminder, meeting.reminders[0].id)).status == "sent"
    assert (await get(Item, meeting.item.id)).status == "open"


# --- snoozed series instances / quiet hours for late messages -------------------------------------------------------


async def _snooze_tomorrow(sessionmaker, occurrence_id: int, now) -> Reminder:
    """What the "📅 Завтра" button does."""
    async with sessionmaker() as session:
        reminder = await items.snooze(session, occurrence_id, tomorrow=True, prefs=Prefs(), now=now)
        await session.commit()
    return reminder


@pytest.mark.parametrize(
    ("rule", "due", "snoozed_at", "fires", "ticks"),
    [
        # weekly MO 09:00: snoozed Mon 09:01 -> Tue 09:00 (the instance would be missed at Mon 21:00)
        (
            "FREQ=WEEKLY;BYDAY=MO",
            (2026, 10, 12, 9, 0),
            (2026, 10, 12, 9, 1),
            (2026, 10, 13, 9, 0),
            [(2026, 10, 12, 21, 30), (2026, 10, 13, 7, 0)],
        ),
        # "платить за квартиру 5-го": snoozed 5 Nov 19:02 -> 6 Nov 19:00 (missed at 6 Nov 07:00)
        (
            "FREQ=MONTHLY;BYMONTHDAY=5",
            (2026, 11, 5, 19, 0),
            (2026, 11, 5, 19, 2),
            (2026, 11, 6, 19, 0),
            [(2026, 11, 6, 7, 30), (2026, 11, 6, 12, 0)],
        ),
    ],
)
async def test_tomorrow_snooze_of_a_series_instance_is_delivered(
    ctx, no_digests, make_items, sessionmaker, bot, local, rule, due, snoozed_at, fires, ticks
) -> None:
    hhmm = f"{due[3]:02d}:{due[4]:02d}"
    spec = {"kind": "task", "title": "Платёж", "when": {"time": hhmm}, "rrule": rule}
    [created] = await make_items(spec, now=local(2026, 10, 6, 10, 0))
    assert created.occurrence.due_at == local(*due)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(*due))  # the reminder, at the item's time
    await scheduler.tick(local(*due) + timedelta(seconds=30))  # the next instance is generated
    assert len(bot.sent) == 1

    reminder = await _snooze_tomorrow(sessionmaker, created.occurrence.id, local(*snoozed_at))
    assert reminder.fire_at == local(*fires)
    for tick in ticks:
        await scheduler.tick(local(*tick))
    assert len(bot.sent) == 1
    await scheduler.tick(local(*fires))
    assert len(bot.sent) == 2 and "Платёж" in bot.sent[1]["text"]
    async with sessionmaker() as session:
        assert (await session.get(Reminder, reminder.id)).status == "sent"


async def test_tomorrow_snooze_of_a_daily_instance_reminds_once(
    ctx, no_digests, make_items, sessionmaker, bot, local
) -> None:
    spec = {"kind": "task", "title": "Таблетки", "when": {"time": "11:00"}, "rrule": "FREQ=DAILY"}
    [created] = await make_items(spec, now=local(2026, 10, 6, 10, 0))
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 10, 55))
    await scheduler.tick(local(2026, 10, 6, 11, 0, 30))
    await _snooze_tomorrow(sessionmaker, created.occurrence.id, local(2026, 10, 6, 11, 1))
    for tick in [(2026, 10, 6, 23, 30), (2026, 10, 7, 10, 55), (2026, 10, 7, 11, 0), (2026, 10, 7, 11, 1)]:
        await scheduler.tick(local(*tick))
    assert len(bot.sent) == 2  # Tuesday's reminder and exactly one on Wednesday


async def test_late_evening_digest_is_not_sent_in_quiet_hours(ctx, make_items, t0, bot, local, get) -> None:
    # The PC slept from 18:50 to 23:40: the 21:00 digest's window is still open, but it is 23:40 now.
    await make_items({"kind": "task", "title": "Звонок", "when": {"time": "15:00"}}, now=t0)
    await Scheduler(ctx).tick(local(2026, 10, 6, 23, 40))
    assert bot.sent == []
    assert await get(OnceKey, "digest:evening:2026-10-06") is None


async def test_late_automatic_reminder_is_not_sent_in_quiet_hours(
    ctx, no_digests, make_items, t0, bot, local, get
) -> None:
    [created] = await make_items({"kind": "purchase", "title": "Купить молоко"}, now=t0)  # 19:00, auto
    [reminder] = created.reminders
    assert reminder.auto is True
    scheduler = Scheduler(ctx)
    await scheduler.tick(reminder.fire_at + timedelta(hours=4, minutes=40))  # 23:40
    assert bot.sent == []
    # Iteration 3, §5: nothing is lost — it waits for 08:00, even though that is beyond the 6 h grace by then
    stored = await get(Reminder, reminder.id)
    assert (stored.status, stored.fire_at, stored.held_from) == ("pending", local(2026, 10, 7, 8, 0), reminder.fire_at)
    await scheduler.tick(local(2026, 10, 7, 8, 0))
    [msg] = bot.sent
    assert msg["disable_notification"] is True
    assert msg["text"].splitlines()[1] == "⏰ вчера 19:00 · опоздал на 13 ч"
    assert (await get(Reminder, reminder.id)).status == "sent"


async def test_late_automatic_reminder_waits_for_the_end_of_short_quiet_hours(
    ctx, no_digests, set_prefs, make_items, t0, bot, local, get
) -> None:
    prefs = await set_prefs(task_default_time=time(22, 30), quiet_end=time(1, 0))
    [created] = await make_items({"kind": "task", "title": "Полить цветы"}, now=t0, prefs=prefs)
    [reminder] = created.reminders
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 23, 10))  # late: the bot was down at 22:30
    assert bot.sent == []
    stored = await get(Reminder, reminder.id)
    assert (stored.status, stored.fire_at, stored.attempts) == ("pending", local(2026, 10, 7, 1, 0), 0)
    await scheduler.tick(local(2026, 10, 7, 1, 0))
    assert len(bot.sent) == 1


async def test_explicit_time_reminder_is_sent_in_quiet_hours(ctx, no_digests, make_items, t0, bot, local) -> None:
    await make_items({"kind": "event", "title": "Поезд", "when": {"time": "23:30"}}, now=t0)
    await Scheduler(ctx).tick(local(2026, 10, 6, 23, 31))  # the 23:30 reminder, a minute late
    assert len(bot.sent) == 1
