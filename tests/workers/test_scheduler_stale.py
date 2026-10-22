"""workers.scheduler, iteration 4: the stale question (§4: once a day, after the morning digest, never in quiet
hours, its item shown in that digest, journaled) and the Sunday weekly review (§6: time, quiet hours, window, empty,
journaled)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import select

from dumpbot.db.models import Event, OnceKey
from dumpbot.services import events
from dumpbot.workers.scheduler import STALE_CHECK_EVERY, Scheduler, stale_key

OWNER = 4242


async def _shown(sessionmaker, item_id: int, at: datetime, count: int = 3) -> None:
    """Digests listed the item on ``count`` days (a day apart, before ``at``) and nothing was done about it."""
    async with sessionmaker() as session:
        for n in range(count):
            at_n = at - timedelta(days=count - n)
            session.add(Event(type=events.DIGEST_ITEM, item_id=item_id, created_at=at_n, data={"digest": "morning"}))
        await session.commit()


async def _events(db, type_: str) -> list[Event]:
    return [e for e in await db(select(Event).order_by(Event.id)) if e.type == type_]


def _buttons(message: dict) -> list[str]:
    markup = message.get("reply_markup")
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup is not None else []


async def test_the_stale_question_follows_the_morning_digest(
    ctx, make_items, local, bot, sessionmaker, db, bot_messages
) -> None:
    [card] = await make_items({"kind": "task", "title": "Сделать карту"}, now=local(2026, 10, 1, 8))
    await _shown(sessionmaker, card.item.id, local(2026, 10, 6, 9))

    await Scheduler(ctx).tick(local(2026, 10, 6, 9, 0))

    digest_msg, question = bot.sent
    assert "Доброе утро" in digest_msg["text"] and "Сделать карту" in digest_msg["text"]
    assert question["text"].startswith("🕸 📌 «Сделать карту» висит 5 дней. Что делаем?")
    assert _buttons(question) == [f"st:{a}:{card.item.id}" for a in ("done", "smd", "del", "keep")]
    [asked] = await _events(db, events.STALE_ASK)
    assert (asked.item_id, asked.source, asked.data) == (card.item.id, "stale", {"digests": 3, "moves": 0})
    stored = [m for m in await bot_messages() if m.purpose == "stale"]
    assert [(m.item_id, m.occurrence_id) for m in stored] == [(card.item.id, card.occurrence.id)]


async def test_at_most_one_question_a_day(ctx, make_items, local, bot, sessionmaker, get) -> None:
    first, second = [
        (await make_items({"kind": "task", "title": t}, now=local(2026, 10, 1, 8)))[0] for t in ("Первое", "Второе")
    ]
    for created in (first, second):
        await _shown(sessionmaker, created.item.id, local(2026, 10, 6, 9))
    scheduler = Scheduler(ctx)

    await scheduler.tick(local(2026, 10, 6, 9, 0))
    await scheduler.tick(local(2026, 10, 6, 15, 0))
    await scheduler.tick(local(2026, 10, 7, 7, 30))  # in quiet hours: no
    questions = [m["text"] for m in bot.sent if m["text"].startswith("🕸")]
    assert len(questions) == 1 and "Первое" in questions[0]
    assert await get(OnceKey, stale_key(date(2026, 10, 6))) is not None

    await scheduler.tick(local(2026, 10, 7, 9, 0))
    questions = [m["text"] for m in bot.sent if m["text"].startswith("🕸")]
    assert len(questions) == 2 and "Второе" in questions[1]  # the first one was asked: it is not asked again


async def test_no_question_before_the_morning_digest_nor_in_quiet_hours(
    ctx, set_prefs, make_items, local, bot, sessionmaker
) -> None:
    [card] = await make_items({"kind": "task", "title": "Сделать карту"}, now=local(2026, 10, 1, 8))
    await _shown(sessionmaker, card.item.id, local(2026, 10, 6, 8))
    await Scheduler(ctx).tick(local(2026, 10, 6, 8, 30))  # out of quiet hours, but before the 09:00 digest
    assert bot.sent == []

    await set_prefs(digest_morning_enabled=False, quiet_start=datetime(2026, 1, 1, 7).time())
    await Scheduler(ctx).tick(local(2026, 10, 6, 7, 30))  # quiet hours 07:00–08:00
    assert bot.sent == []
    await Scheduler(ctx).tick(local(2026, 10, 6, 9, 30))  # the digest is off: its time still opens the day's window
    assert [m["text"].startswith("🕸") for m in bot.sent] == [True]


async def test_the_question_of_a_span_item_puts_it_into_that_morning_digest(
    ctx, make_items, local, bot, sessionmaker
) -> None:
    [created] = await make_items(
        {"kind": "task", "title": "Raspberry Pi", "when": {"period": "next_week"}}, now=local(2026, 10, 4, 8)
    )
    await _shown(sessionmaker, created.item.id, local(2026, 10, 7, 9))

    await Scheduler(ctx).tick(local(2026, 10, 7, 9, 0))  # Wednesday: mid-span

    digest_msg, question = bot.sent
    assert "🗓 <b>На этой неделе</b> (до вс 11 окт)\n1. 📌 Raspberry Pi" in digest_msg["text"]
    assert "«Raspberry Pi» висит 3 дня" in question["text"]


async def test_without_a_candidate_the_next_look_waits(ctx, make_items, local, bot, sessionmaker, monkeypatch) -> None:
    from dumpbot.services import stale

    looks: list[datetime] = []
    real_pick = stale.pick

    async def counting_pick(session, *args, **kwargs):
        looks.append(datetime.now())
        return await real_pick(session, *args, **kwargs)

    monkeypatch.setattr(stale, "pick", counting_pick)
    scheduler = Scheduler(ctx)
    at = local(2026, 10, 6, 12, 30)  # the morning digest's window is over: only the stale step looks
    await scheduler.tick(at)
    await scheduler.tick(at + timedelta(minutes=1))
    assert len(looks) == 1
    [card] = await make_items({"kind": "task", "title": "Сделать карту"}, now=local(2026, 10, 1, 8))
    await _shown(sessionmaker, card.item.id, at)
    await scheduler.tick(at + STALE_CHECK_EVERY)
    assert len(looks) == 2 and bot.sent[-1]["text"].startswith("🕸")


async def test_a_failed_question_is_asked_again(ctx, make_items, local, bot, sessionmaker, db, get) -> None:
    [card] = await make_items({"kind": "task", "title": "Сделать карту"}, now=local(2026, 10, 1, 8))
    await _shown(sessionmaker, card.item.id, local(2026, 10, 6, 10))
    bot.fail("send_message", TelegramNetworkError(method=SendMessage(chat_id=OWNER, text="x"), message="timeout"))

    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 6, 12, 0))
    assert await get(OnceKey, stale_key(date(2026, 10, 6))) is None
    assert await _events(db, events.STALE_ASK) == []

    await scheduler.tick(local(2026, 10, 6, 12, 1))
    assert bot.sent[-1]["text"].startswith("🕸") and len(await _events(db, events.STALE_ASK)) == 1


# --- the weekly review (§6) ------------------------------------------------------------------------------------


async def test_the_weekly_review_on_sunday_evening(ctx, set_prefs, make_items, local, bot, db, bot_messages) -> None:
    await set_prefs(digest_morning_enabled=False, digest_evening_enabled=False)
    [week] = await make_items(
        {"kind": "task", "title": "След. неделя", "when": {"period": "next_week"}}, now=local(2026, 10, 6, 8)
    )
    [idea] = await make_items(
        {"kind": "idea", "title": "Испанский", "when": {"period": "someday"}}, now=local(2026, 10, 6, 8)
    )
    scheduler = Scheduler(ctx)

    await scheduler.tick(local(2026, 10, 10, 19, 0))  # Saturday
    await scheduler.tick(local(2026, 10, 11, 18, 59))
    assert bot.sent == []
    await scheduler.tick(local(2026, 10, 11, 19, 0))
    await scheduler.tick(local(2026, 10, 11, 20, 0))

    [review] = bot.sent
    assert review["text"].startswith("🗓 <b>Обзор недели</b>, вс 11 окт")
    assert "1. 📌 След. неделя" in review["text"] and "2. 💡 Испанский" in review["text"]
    assert _buttons(review) == [
        f"ld:done:w:20261011:p:1:{week.occurrence.id}:{week.item.id}",
        f"ld:done:w:20261011:s:2:0:{idea.item.id}",
    ]
    assert [m.purpose for m in await bot_messages()] == ["digest"]
    lines = await _events(db, events.DIGEST_ITEM)
    assert [(e.item_id, e.data) for e in lines] == [
        (week.item.id, {"digest": "weekly", "sec": "p"}),
        (idea.item.id, {"digest": "weekly", "sec": "s"}),
    ]


async def test_the_weekly_review_is_off_empty_or_waits_for_quiet_hours(ctx, set_prefs, make_items, local, bot) -> None:
    await set_prefs(digest_morning_enabled=False, digest_evening_enabled=False, digest_weekly_enabled=False)
    await make_items({"kind": "idea", "title": "Испанский", "when": {"period": "someday"}}, now=local(2026, 10, 6, 8))
    await Scheduler(ctx).tick(local(2026, 10, 11, 19, 0))
    assert bot.sent == []  # off

    await set_prefs(digest_weekly_enabled=True, digest_weekly_time=datetime(2026, 1, 1, 23, 30).time())
    await Scheduler(ctx).tick(local(2026, 10, 11, 23, 30))
    assert bot.sent == []  # 23:30 is in quiet hours: the end of them
    await Scheduler(ctx).tick(local(2026, 10, 12, 8, 0))
    assert [m["text"].startswith("🗓 <b>Обзор недели</b>") for m in bot.sent] == [True]


async def test_an_empty_weekly_review_is_not_sent(ctx, set_prefs, local, bot) -> None:
    await set_prefs(digest_morning_enabled=False, digest_evening_enabled=False)
    await Scheduler(ctx).tick(local(2026, 10, 11, 19, 0))
    assert bot.sent == []


async def test_a_late_weekly_review_after_a_downtime(ctx, set_prefs, make_items, local, bot) -> None:
    await set_prefs(digest_morning_enabled=False, digest_evening_enabled=False)
    await make_items({"kind": "idea", "title": "Испанский", "when": {"period": "someday"}}, now=local(2026, 10, 6, 8))
    await Scheduler(ctx).tick(local(2026, 10, 11, 21, 59))  # the bot was down since 18:00: late, within the window
    assert len(bot.sent) == 1 and "Обзор недели" in bot.sent[0]["text"]


async def test_a_weekly_review_after_its_window_is_not_sent_without_a_downtime(
    ctx, set_prefs, make_items, local, bot
) -> None:
    """The first tick ever, after the window: nothing says the bot was down at its time (a fresh start)."""
    await set_prefs(digest_morning_enabled=False, digest_evening_enabled=False, quiet_enabled=False)
    await make_items({"kind": "idea", "title": "Испанский", "when": {"period": "someday"}}, now=local(2026, 10, 6, 8))
    await Scheduler(ctx).tick(local(2026, 10, 11, 22, 1))  # the window is three hours
    assert bot.sent == []


async def test_a_weekly_review_the_bot_was_down_for_goes_out_late(ctx, set_prefs, make_items, local, bot) -> None:
    """Down from Sun 18:30 to 22:30 (a Render restart): the review is not dropped — it goes out at 22:30."""
    await set_prefs(digest_morning_enabled=False, digest_evening_enabled=False)
    await make_items({"kind": "idea", "title": "Испанский", "when": {"period": "someday"}}, now=local(2026, 10, 6, 8))
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 11, 18, 30))
    assert bot.sent == []
    await scheduler.tick(local(2026, 10, 11, 22, 30))  # past the window
    assert [m["text"].startswith("🗓 <b>Обзор недели</b>, вс 11 окт") for m in bot.sent] == [True]
    await scheduler.tick(local(2026, 10, 11, 22, 31))
    assert len(bot.sent) == 1


async def test_a_weekly_review_owed_over_quiet_hours_goes_out_in_the_morning(
    ctx, set_prefs, make_items, local, bot
) -> None:
    await set_prefs(digest_morning_enabled=False, digest_evening_enabled=False)
    await make_items({"kind": "idea", "title": "Испанский", "when": {"period": "someday"}}, now=local(2026, 10, 6, 8))
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 11, 18, 30))
    await scheduler.tick(local(2026, 10, 12, 7, 30))  # back in quiet hours
    await scheduler.tick(local(2026, 10, 12, 7, 31))
    assert [m["text"] for m in bot.sent if "Обзор недели" in m["text"]] == []
    await scheduler.tick(local(2026, 10, 12, 8, 0))  # quiet hours over: Monday, still within a day
    assert [m["text"].startswith("🗓 <b>Обзор недели</b>, вс 11 окт") for m in bot.sent] == [True]


async def test_a_weekly_review_owed_for_more_than_a_day_is_dropped(ctx, set_prefs, make_items, local, bot) -> None:
    await set_prefs(digest_morning_enabled=False, digest_evening_enabled=False)
    await make_items({"kind": "idea", "title": "Испанский", "when": {"period": "someday"}}, now=local(2026, 10, 6, 8))
    scheduler = Scheduler(ctx)
    await scheduler.tick(local(2026, 10, 11, 18, 30))
    await scheduler.tick(local(2026, 10, 12, 19, 30))  # back on Monday evening: last week's review is old news
    assert [m["text"] for m in bot.sent if "Обзор недели" in m["text"]] == []
