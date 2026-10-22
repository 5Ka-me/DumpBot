"""services.processor and the events journal (docs/features/iteration-4.md §4): an edit applied from a message — a
reply to a card, an edit by description, the "🕒 Время" answer — is an ``edit`` event of the item, ``"move": true``
when it moved the item to another day (not a new hour of the same day, not the first date of an item without one);
the stale-items check counts both."""

from __future__ import annotations

from sqlalchemy import select

from dumpbot.db.models import Event
from dumpbot.services import processor


async def _edits(db) -> list[tuple[int | None, str | None, dict | None]]:
    return [(e.item_id, e.source, e.data) for e in await db(select(Event).order_by(Event.id)) if e.type == "edit"]


async def test_a_reply_that_moves_the_item(ctx, add_raw, fake_ai, make_items, t0, db) -> None:
    [created] = await make_items({"kind": "task", "title": "Отчёт", "when": {"day": "+1", "time": "15:00"}}, now=t0)
    raw = await add_raw("перенеси на пятницу", reply_item_id=created.item.id)
    fake_ai.push({"intent": "edit", "edit": {"when": {"text": "в пятницу", "day": "FR"}}})
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})
    assert await _edits(db) == [(created.item.id, "text", {"move": True})]


async def test_a_reply_that_renames_the_item(ctx, add_raw, fake_ai, make_items, t0, db) -> None:
    [created] = await make_items({"kind": "task", "title": "Отчёт"}, now=t0)
    raw = await add_raw("назови это «Квартальный отчёт»", reply_item_id=created.item.id)
    fake_ai.push({"intent": "edit", "edit": {"title": "Квартальный отчёт"}})
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})
    assert await _edits(db) == [(created.item.id, "text", {"move": False})]


async def test_an_edit_by_description(ctx, add_raw, fake_ai, make_items, t0, db) -> None:
    [created] = await make_items(
        {"kind": "event", "title": "Встреча с Олегом", "when": {"day": "+1", "time": "15:00"}}, now=t0
    )
    raw = await add_raw("перенеси встречу с Олегом на четверг")
    fake_ai.push({"intent": "edit", "edit": {"target": "встреча Олег", "when": {"day": "+2", "time": "15:00"}}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert await _edits(db) == [(created.item.id, "text", {"move": True})]


async def test_a_new_hour_of_the_same_day_is_no_move(ctx, add_raw, fake_ai, make_items, t0, db) -> None:
    """«не в 10, а в 11», «в 11:30»: twice an edit of the time, never "переносили уже 2 раза" a minute later."""
    [created] = await make_items({"kind": "event", "title": "Врач", "when": {"day": "+1", "time": "10:00"}}, now=t0)
    for words, at in (("не в 10, а в 11", "11:00"), ("в 11:30", "11:30")):
        raw = await add_raw(words, reply_item_id=created.item.id)
        fake_ai.push({"intent": "edit", "edit": {"when": {"text": f"в {at}", "time": at}}})
        await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})
    assert await _edits(db) == [(created.item.id, "text", {"move": False})] * 2

    raw = await add_raw("перенеси встречу с врачом на 16", reply_item_id=created.item.id)
    fake_ai.push({"intent": "edit", "edit": {"target": "Врач", "when": {"day": "+1", "time": "16:00"}}})
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})
    assert (await _edits(db))[-1] == (created.item.id, "text", {"move": False})


async def test_the_time_prompts_answer(ctx, fake_ai, make_items, t0, db) -> None:
    [created] = await make_items({"kind": "task", "title": "Отчёт", "when": {"day": "+1", "time": "15:00"}}, now=t0)
    fake_ai.push({"intent": "edit", "edit": {"when": {"text": "в пятницу в 18", "day": "FR", "time": "18:00"}}})
    await processor.handle_edit_time(ctx, {"item_id": created.item.id, "text": "в пятницу в 18", "message_id": 300})
    assert await _edits(db) == [(created.item.id, "text", {"move": True})]


async def test_a_first_date_is_no_move(ctx, fake_ai, make_items, t0, db) -> None:
    [created] = await make_items({"kind": "task", "title": "Отчёт"}, now=t0)  # the bot's 19:00: no date of its own
    fake_ai.push({"intent": "edit", "edit": {"when": {"text": "в пятницу в 18", "day": "FR", "time": "18:00"}}})
    await processor.handle_edit_time(ctx, {"item_id": created.item.id, "text": "в пятницу в 18", "message_id": 300})
    assert await _edits(db) == [(created.item.id, "text", {"move": False})]
