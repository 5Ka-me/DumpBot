"""workers.scheduler after downtime (docs/features/iteration-3.md §5): the gap between ticks, reminders held over quiet
hours, the missed evening digest in the next morning digest, the one silent notice — and a restart after a gap.

Quiet hours 23:00-08:00, digests 09:00 / 21:00, grace 6 h (the defaults). Tue 2026-10-06.
"""

from __future__ import annotations

from datetime import time

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage

from dumpbot.db.models import AppState, OnceKey, Reminder
from dumpbot.domain.prefs import Prefs
from dumpbot.services import digest
from dumpbot.workers.scheduler import LAST_TICK_KEY, Scheduler

OWNER = 4242


@pytest.fixture
async def no_digests(set_prefs) -> Prefs:
    return await set_prefs(digest_morning_enabled=False, digest_evening_enabled=False)


def network_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=SendMessage(chat_id=OWNER, text="x"), message="timeout")


def texts(bot) -> list[str]:
    return [kw["text"] for kw in bot.sent]


async def mom_at_20(make_items, t0):
    """An explicit 20:00 call (not automatic)."""
    [created] = await make_items({"kind": "task", "title": "Позвонить маме", "when": {"time": "20:00"}}, now=t0)
    return created


# --- the gap -------------------------------------------------------------------------------------------------------


async def test_the_spec_example_sleep_from_19_to_23_19(ctx, make_items, t0, bot, local, get) -> None:
    mom = await mom_at_20(make_items, t0)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 19, 0))
    assert bot.sent == []

    # the laptop sleeps 19:00 -> 23:19: the 20:00 reminder and the 21:00 digest are missed while it is down
    await scheduler.tick(local(2026, 10, 6, 23, 19))
    [notice] = bot.sent
    assert notice["text"] == (
        "😴 Меня не было с 19:00 до 23:19 (компьютер спал?). Опоздало напоминаний: 1 — пришлю в 08:00. "
        "Итог дня — в утреннем дайджесте."
    )
    assert notice["disable_notification"] is True
    # the reminder waits for the end of quiet hours (3 h 19 min late is more than 30 min, and it is 23:19)
    stored = await get(Reminder, mom.reminders[0].id)
    assert (stored.status, stored.fire_at, stored.held_from) == (
        "pending",
        local(2026, 10, 7, 8, 0),
        local(2026, 10, 6, 20, 0),
    )
    # the evening digest is not sent late, it goes into tomorrow's morning digest
    assert await get(OnceKey, "digest:evening:2026-10-06") is not None
    assert await get(OnceKey, digest.folded_key(local(2026, 10, 6, 12, 0).date())) is not None
    assert scheduler.downtime is None

    await scheduler.tick(local(2026, 10, 6, 23, 19, 30))  # one notice only
    await scheduler.tick(local(2026, 10, 7, 7, 59))  # nothing more before 08:00
    assert len(bot.sent) == 1

    # 08:00: the held reminder, silently, saying when it was for
    await scheduler.tick(local(2026, 10, 7, 8, 0))
    reminder = bot.sent[1]
    assert reminder["text"].splitlines() == ["🔔 <b>Позвонить маме</b>", "⏰ вчера 20:00 · опоздал на 12 ч"]
    assert reminder["disable_notification"] is True
    assert (await get(Reminder, mom.reminders[0].id)).status == "sent"

    # 09:00: the morning digest carries yesterday's summary, the call under it (not under "Просрочено")
    await scheduler.tick(local(2026, 10, 7, 9, 0))
    morning = bot.sent[2]["text"]
    assert "🌙 <b>Вчерашний итог</b> (не успел прислать)\nСделано 0 из 1\n1. 20:00 · 📌 Позвонить маме" in morning
    assert "Просрочено" not in morning
    assert len(bot.sent) == 3


async def test_no_notice_when_the_gap_affected_nothing(ctx, no_digests, bot, local) -> None:
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 10, 0))
    await scheduler.tick(local(2026, 10, 6, 14, 0))
    assert scheduler.downtime is None
    assert bot.sent == []


async def test_a_short_gap_is_no_downtime(ctx, no_digests, make_items, t0, bot, local) -> None:
    await make_items({"kind": "event", "title": "Созвон", "when": {"time": "10:05"}}, now=t0)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 10, 0))
    await scheduler.tick(local(2026, 10, 6, 10, 14))  # 14 min: under GAP_AFTER
    assert scheduler.downtime is None
    [reminder] = bot.sent
    assert "опоздал на 9 мин" in reminder["text"]  # late, as before — but no notice


async def test_late_reminder_in_the_daytime_comes_at_once_with_sound(
    ctx, no_digests, make_items, t0, bot, local
) -> None:
    await make_items({"kind": "event", "title": "Созвон", "when": {"time": "12:00"}}, now=t0)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 10, 0))
    await scheduler.tick(local(2026, 10, 6, 14, 0))
    reminder, notice = bot.sent
    assert reminder["text"].splitlines()[1] == "⏰ сегодня 12:00 · опоздал на 2 ч"
    assert reminder["disable_notification"] is False  # outside quiet hours: as it always was
    assert notice["text"] == "😴 Меня не было с 10:00 до 14:00 (компьютер спал?). Опоздало напоминаний: 1."
    assert notice["disable_notification"] is True


async def test_little_late_explicit_reminder_in_quiet_hours_is_not_held(
    ctx, no_digests, make_items, t0, bot, local, get
) -> None:
    [train] = await make_items({"kind": "event", "title": "Поезд", "when": {"time": "23:10"}}, now=t0)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 22, 50))
    await scheduler.tick(local(2026, 10, 6, 23, 30))  # 20 min late: not more than 30
    reminder, notice = bot.sent
    assert reminder["text"].startswith("🔔 <b>Поезд</b>\n⏰ сегодня 23:10 · опоздал на 20 мин")
    assert (await get(Reminder, train.reminders[0].id)).held_from is None
    assert notice["text"].endswith("Опоздало напоминаний: 1.")


async def test_too_old_reminders_are_counted_as_not_sent(ctx, no_digests, make_items, t0, bot, local, get) -> None:
    [call] = await make_items({"kind": "event", "title": "Созвон", "when": {"time": "10:30"}}, now=t0)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 10, 0))
    await scheduler.tick(local(2026, 10, 6, 17, 0))  # 6 h 30 min late: beyond the grace
    [notice] = bot.sent
    assert notice["text"] == (
        "😴 Меня не было с 10:00 до 17:00 (компьютер спал?). Не прислал устаревших напоминаний: 1 — эти дела в /today."
    )
    assert (await get(Reminder, call.reminders[0].id)).status == "missed"


async def test_reminders_due_before_the_gap_do_not_count(ctx, no_digests, make_items, t0, bot, local, sessionmaker):
    """A reminder that was overdue already before the bot went down (it kept failing) is not the gap's doing."""
    await make_items({"kind": "event", "title": "Созвон", "when": {"time": "10:30"}}, now=t0)
    bot.fail("send_message", network_error())
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 10, 30))  # Telegram is down at 10:30
    await scheduler.tick(local(2026, 10, 6, 11, 0))  # sent 30 min late, but not because of a downtime
    assert len(bot.sent) == 2 and scheduler.downtime is None


async def test_the_notice_waits_until_telegram_takes_the_late_reminders(ctx, no_digests, make_items, t0, bot, local):
    await make_items({"kind": "event", "title": "Созвон", "when": {"time": "12:00"}}, now=t0)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 10, 0))
    bot.fail("send_message", network_error())  # the network is not up yet after the wake-up
    await scheduler.tick(local(2026, 10, 6, 14, 0))
    assert len(bot.sent) == 1 and scheduler.downtime is not None  # the failed reminder only
    await scheduler.tick(local(2026, 10, 6, 14, 0, 30))
    assert [t.splitlines()[0] for t in texts(bot)[1:]] == [
        "🔔 <b>Созвон</b>",
        "😴 Меня не было с 10:00 до 14:00 (компьютер спал?). Опоздало напоминаний: 1.",
    ]


async def test_a_failed_notice_is_sent_on_the_next_tick(ctx, no_digests, make_items, t0, bot, local):
    await make_items({"kind": "event", "title": "Созвон", "when": {"time": "12:00"}}, now=t0)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 10, 0))
    original = bot.send_message
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # the reminder goes through, the notice does not
            bot.calls.append(("send_message", kwargs))
            raise network_error()
        return await original(**kwargs)

    bot.send_message = flaky
    await scheduler.tick(local(2026, 10, 6, 14, 0))
    assert scheduler.downtime is not None
    await scheduler.tick(local(2026, 10, 6, 14, 0, 30))
    assert scheduler.downtime is None
    assert texts(bot)[-1].startswith("😴 Меня не было с 10:00 до 14:00")
    assert sum("😴" in t for t in texts(bot)) == 2  # the failed attempt and the one that went out


async def test_two_gaps_before_the_notice_make_one_notice(ctx, no_digests, make_items, t0, bot, local):
    await make_items({"kind": "event", "title": "Созвон", "when": {"time": "12:00"}}, now=t0)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 10, 0))
    bot.fail("send_message", network_error(), network_error())  # the reminder fails, then the next try
    await scheduler.tick(local(2026, 10, 6, 14, 0))
    await scheduler.tick(local(2026, 10, 6, 16, 0))  # asleep again before the notice went out
    await scheduler.tick(local(2026, 10, 6, 16, 0, 30))
    assert texts(bot)[-1] == "😴 Меня не было с 10:00 до 16:00 (компьютер спал?). Опоздало напоминаний: 1."
    assert sum("😴" in t for t in texts(bot)) == 1


# --- a restart after a gap -------------------------------------------------------------------------------------------


async def test_restart_after_a_gap_is_noticed_from_the_stored_tick(ctx, make_items, t0, bot, local, get) -> None:
    await mom_at_20(make_items, t0)
    await Scheduler(ctx).tick(local(2026, 10, 6, 19, 0))
    stored = await get(AppState, LAST_TICK_KEY)
    assert stored.value == local(2026, 10, 6, 19, 0).isoformat()

    # Docker stood still; a new process (a new Scheduler) starts at 23:19
    await Scheduler(ctx).tick(local(2026, 10, 6, 23, 19))
    [notice] = bot.sent
    assert notice["text"].startswith("😴 Меня не было с 19:00 до 23:19 (компьютер спал?). Опоздало напоминаний: 1")
    assert (await get(AppState, LAST_TICK_KEY)).value == local(2026, 10, 6, 23, 19).isoformat()


async def test_first_tick_ever_and_a_garbled_stored_tick_are_no_downtime(ctx, no_digests, bot, local, sessionmaker):
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 10, 0))
    assert scheduler.downtime is None
    async with sessionmaker() as session:
        row = await session.get(AppState, LAST_TICK_KEY)
        row.value = {"not": "a time"}
        await session.commit()
    await scheduler.tick(local(2026, 10, 6, 18, 0))
    assert scheduler.downtime is None
    await scheduler.tick(local(2026, 10, 6, 17, 0))  # the clock went back: no gap either
    assert scheduler.downtime is None and bot.sent == []


# --- the missed evening digest ---------------------------------------------------------------------------------------


async def test_evening_digest_missed_overnight_goes_into_the_morning_digest(ctx, make_items, t0, bot, local, get):
    await make_items({"kind": "task", "title": "Купить хлеб", "when": {"time": "18:00"}}, now=t0)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 18, 0))  # the 18:00 reminder, on time
    assert len(bot.sent) == 1

    # asleep from 18:00 to 08:30: the digest window (21:00-24:00) closed meanwhile
    await scheduler.tick(local(2026, 10, 7, 8, 30))
    notice = bot.sent[1]
    assert notice["text"] == (
        "😴 Меня не было с вчера 18:00 до 08:30 (компьютер спал?). Вчерашний итог — в утреннем дайджесте."
    )
    await scheduler.tick(local(2026, 10, 7, 9, 0))
    morning = bot.sent[2]["text"]
    assert morning.splitlines()[2:7] == [
        "На сегодня ничего не запланировано.",
        "",
        "🌙 <b>Вчерашний итог</b> (не успел прислать)",
        "Сделано 0 из 1",
        "1. 18:00 · 📌 Купить хлеб",
    ]
    assert await get(OnceKey, "digest:morning:2026-10-07") is not None


async def test_evening_digest_is_sent_late_when_the_bot_wakes_before_quiet_hours(ctx, make_items, t0, bot, local):
    await make_items({"kind": "task", "title": "Купить хлеб", "when": {"time": "18:00"}}, now=t0)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 18, 0))
    await scheduler.tick(local(2026, 10, 6, 22, 0))  # 21:00 passed while down, but the window is still open
    digest_text = texts(bot)[1]
    assert digest_text.startswith("🌙 <b>Итоги дня</b>")
    assert len(bot.sent) == 2  # no notice: nothing was lost
    await scheduler.tick(local(2026, 10, 7, 9, 0))
    assert "Вчерашний итог" not in texts(bot)[-1]


async def test_an_empty_missed_evening_digest_is_not_folded(ctx, bot, local, get) -> None:
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 19, 0))
    await scheduler.tick(local(2026, 10, 6, 23, 19))
    assert bot.sent == []
    assert await get(OnceKey, "digest:evening:2026-10-06") is not None
    assert await get(OnceKey, digest.folded_key(local(2026, 10, 6, 12, 0).date())) is None


async def test_without_a_morning_digest_the_notice_points_to_today(ctx, set_prefs, make_items, t0, bot, local, get):
    await set_prefs(digest_morning_enabled=False)
    await make_items({"kind": "task", "title": "Купить хлеб", "when": {"time": "18:00"}}, now=t0)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 18, 0))
    await scheduler.tick(local(2026, 10, 6, 23, 30))
    assert texts(bot)[-1] == (
        "😴 Меня не было с 18:00 до 23:30 (компьютер спал?). Итог дня не прислал — несделанное в /today."
    )


async def test_digest_window_in_quiet_hours_after_a_late_evening_time(ctx, set_prefs, make_items, t0, bot, local, get):
    """Evening digest at 22:30: the bot comes back at 23:10, inside its window but in quiet hours."""
    await set_prefs(digest_evening_time=time(22, 30))
    await make_items({"kind": "task", "title": "Купить хлеб", "when": {"time": "18:00"}}, now=t0)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 18, 0))
    await scheduler.tick(local(2026, 10, 6, 22, 0))  # 4 h gap: the 18:00 reminder was on time, nothing affected
    await scheduler.tick(local(2026, 10, 6, 23, 10))
    assert texts(bot)[-1].endswith("Итог дня — в утреннем дайджесте.")
    assert await get(OnceKey, digest.folded_key(local(2026, 10, 6, 12, 0).date())) is not None


async def test_held_reminder_not_lost_to_the_grace_but_lost_to_a_second_long_gap(
    ctx, no_digests, make_items, t0, bot, local, get
) -> None:
    mom = await mom_at_20(make_items, t0)
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 19, 0))
    await scheduler.tick(local(2026, 10, 6, 23, 19))  # held until 08:00
    await scheduler.tick(local(2026, 10, 7, 14, 30))  # down again over 08:00: 6 h 30 min after its new time
    assert (await get(Reminder, mom.reminders[0].id)).status == "missed"
    assert texts(bot)[-1].endswith("Не прислал устаревших напоминаний: 1 — эти дела в /today.")
