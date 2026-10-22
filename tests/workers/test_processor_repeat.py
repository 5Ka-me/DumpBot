"""services.processor, iteration 4 §2 (docs/features/iteration-4.md): stop phrases stop a series whatever the model
made of them (and a reply that is nothing but one needs no AI at all), several times a day in one series, and
«⚠️ понял только 09:00» when a series kept fewer times than the message named."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from dumpbot.ai.schemas import EditPatch
from dumpbot.db.models import Item, Occurrence, OnceKey, Reminder
from dumpbot.services import items, processor

DAILY_9 = {"kind": "task", "title": "Таблетки", "rrule": "FREQ=DAILY", "when": {"time": "09:00"}}


def reply_to(call: dict) -> int | None:
    params = call.get("reply_parameters")
    return params.message_id if params is not None else None


async def _pending(db, item_id: int) -> list[datetime]:
    stmt = (
        select(Reminder.fire_at)
        .join(Occurrence, Occurrence.id == Reminder.occurrence_id)
        .where(Occurrence.item_id == item_id, Reminder.status == "pending")
    )
    return await db(stmt)


# --- stop phrases -----------------------------------------------------------------------------------------------------


async def test_a_reply_that_is_only_a_stop_phrase_needs_no_ai(
    ctx, add_raw, fake_ai, bot, make_items, local, frozen, get, occurrences_of, db
) -> None:
    """ "Закрывай вообще" in reply to the 09:00 reminder: the series stops, today's instance is closed, tomorrow's
    (made in advance) is cancelled with its reminder — no AI call, no budget (the bug of 17.10)."""
    [created] = await make_items(DAILY_9, now=local(2026, 10, 6, 8, 0))
    await processor_ensure(ctx, local(2026, 10, 6, 9, 1))
    frozen.set(local(2026, 10, 6, 9, 30))
    raw = await add_raw("Закрывай вообще!", reply_item_id=created.item.id, message_id=700)
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})

    assert fake_ai.inputs == []
    item = await get(Item, created.item.id)
    assert (item.rec_active, item.status) == (False, "done")
    today, tomorrow = await occurrences_of(created.item.id)
    assert (today.status, tomorrow.status) == ("done", "cancelled")
    assert await _pending(db, created.item.id) == []
    [card] = bot.sent
    assert "🔁 повтор остановлен" in card["text"] and reply_to(card) == 700
    assert bot.reactions == [(700, "👌")]
    assert await get(OnceKey, f"edit:{raw.id}:{created.item.id}") is not None


async def test_a_stop_phrase_reply_to_a_one_off_is_done(ctx, add_raw, fake_ai, bot, make_items, t0, get) -> None:
    [created] = await make_items({"kind": "task", "title": "Позвонить", "when": {"time": "15:00"}}, now=t0)
    raw = await add_raw("больше не напоминай", reply_item_id=created.item.id)
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})
    assert fake_ai.inputs == []
    assert (await get(Item, created.item.id)).status == "done"


async def test_the_model_reading_a_stop_phrase_as_done_still_stops_the_series(
    ctx, add_raw, fake_ai, bot, make_items, local, frozen, get, occurrences_of
) -> None:
    """ "Сделал, больше не напоминай про это" goes to the AI (it says more than the phrase); the model's "done" would
    leave the series running — the phrase makes it a stop."""
    [created] = await make_items(DAILY_9, now=local(2026, 10, 6, 8, 0))
    frozen.set(local(2026, 10, 6, 9, 30))
    raw = await add_raw("сделал, больше не напоминай про таблетки", reply_item_id=created.item.id)
    fake_ai.push({"intent": "edit", "edit": {"action": "done"}})
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})

    assert len(fake_ai.inputs) == 1
    item = await get(Item, created.item.id)
    assert (item.rec_active, item.status) == (False, "done")
    assert [o.status for o in await occurrences_of(created.item.id)] == ["done"]


async def test_a_stop_phrase_with_a_new_time_is_left_to_the_model(
    ctx, add_raw, fake_ai, bot, make_items, local, frozen, get
) -> None:
    [created] = await make_items(DAILY_9, now=local(2026, 10, 6, 8, 0))
    frozen.set(local(2026, 10, 6, 9, 30))
    raw = await add_raw("больше не напоминай утром, напоминай в 21", reply_item_id=created.item.id)
    fake_ai.push({"intent": "edit", "edit": {"when": {"text": "в 21", "time": "21:00"}}})
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})
    assert (await get(Item, created.item.id)).rec_active is True


async def test_a_stop_phrase_by_description(ctx, add_raw, fake_ai, bot, make_items, local, frozen, get) -> None:
    """ "Хватит напоминать про таблетки" (no reply): the model finds the item and says "no_reminder" — a stop."""
    [created] = await make_items(DAILY_9, now=local(2026, 10, 6, 8, 0))
    frozen.set(local(2026, 10, 6, 9, 30))
    raw = await add_raw("хватит напоминать про таблетки")
    fake_ai.push({"intent": "edit", "edit": {"target": "таблетки", "action": "no_reminder"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    item = await get(Item, created.item.id)
    assert (item.rec_active, item.status) == (False, "done")
    [msg] = bot.sent
    assert msg["text"].startswith("⏹ Остановил повтор «Таблетки» и закрыл этот раз")


def test_stop_phrase_patch_keeps_other_actions():
    stop = EditPatch(action="stop_recurrence")
    assert processor.stop_phrase_patch(EditPatch(action="done"), "закрывай вообще") == stop
    assert processor.stop_phrase_patch(EditPatch(action="no_reminder"), "больше не напоминай") == stop
    assert processor.stop_phrase_patch(EditPatch(), "хватит повторять") == stop
    for patch in (EditPatch(action="delete"), EditPatch(action="split"), EditPatch(rrule="FREQ=DAILY")):
        assert processor.stop_phrase_patch(patch, "хватит повторять") == patch
    assert processor.stop_phrase_patch(EditPatch(action="done"), "сделал") == EditPatch(action="done")


# --- several times a day ---------------------------------------------------------------------------------------------


async def test_two_times_a_day_card(ctx, add_raw, fake_ai, bot, items_of_raw, local, frozen) -> None:
    frozen.set(local(2026, 10, 6, 8, 0))
    raw = await add_raw("каждый день в 9:00 и 18:00 пить таблетки")
    fake_ai.push(
        {
            "items": [
                {
                    "kind": "task",
                    "title": "Выпить таблетки",
                    "when": {"text": "каждый день в 9:00 и 18:00", "time": "09:00"},
                    "rrule": "FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0",
                }
            ]
        }
    )
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [item] = await items_of_raw(raw.id)
    assert item.rrule == "FREQ=DAILY;BYHOUR=9,18;BYMINUTE=0"
    [card] = bot.texts
    assert "🔁 каждый день в 09:00 и 18:00" in card.splitlines()
    assert "понял только" not in card


async def test_a_lost_time_is_said_on_the_card(ctx, add_raw, fake_ai, bot, local, frozen) -> None:
    """The model kept only 09:00 of "в 9:00 и 18:00" (the bug of 2026-10): the card says so."""
    frozen.set(local(2026, 10, 6, 8, 0))
    raw = await add_raw("каждый день в 9:00 и 18:00 пить таблетки")
    fake_ai.push(
        {
            "items": [
                {
                    "kind": "task",
                    "title": "Выпить таблетки",
                    "when": {"text": "каждый день в 9:00", "time": "09:00"},
                    "rrule": "FREQ=DAILY",
                }
            ]
        }
    )
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [card] = bot.texts
    assert "⚠️ понял только 09:00" in card.splitlines()


async def test_times_kept_by_two_items_are_not_lost(ctx, add_raw, fake_ai, bot, local, frozen) -> None:
    """ "в 9 пить таблетки, в 18 гулять с собакой": two series, each with its time — nothing is lost."""
    frozen.set(local(2026, 10, 6, 8, 0))
    raw = await add_raw("каждый день в 9 пить таблетки, а в 18 гулять с собакой")
    fake_ai.push(
        {
            "items": [
                {"kind": "task", "title": "Таблетки", "when": {"time": "09:00"}, "rrule": "FREQ=DAILY"},
                {"kind": "task", "title": "Собака", "when": {"time": "18:00"}, "rrule": "FREQ=DAILY"},
            ]
        }
    )
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert len(bot.texts) == 2 and all("понял только" not in text for text in bot.texts)


async def test_a_reply_with_two_times_and_one_kept(ctx, add_raw, fake_ai, bot, make_items, t0, get) -> None:
    [created] = await make_items({"kind": "task", "title": "Таблетки", "when": {"time": "15:00"}}, now=t0)
    raw = await add_raw("каждый день в 9 и 18", reply_item_id=created.item.id)
    fake_ai.push({"intent": "edit", "edit": {"rrule": "FREQ=DAILY", "when": {"text": "в 9", "time": "09:00"}}})
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})
    [card] = bot.texts
    assert "⚠️ понял только 09:00" in card.splitlines()


async def processor_ensure(ctx, now: datetime) -> None:
    """The scheduler's step that makes the next instance of a series."""
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        await items.ensure_recurring(session, prefs, now)
        await session.commit()
