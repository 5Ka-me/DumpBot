"""services.processor and the events journal (docs/features/iteration-4.md §4): an edit applied from a message — a
reply to a card, an edit by description, the "🕒 Время" answer — is an ``edit`` event of the item, ``"move": true``
when it moved the item; the stale-items check counts both."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from dumpbot.ai.schemas import EditPatch, When
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
    raw = await add_raw("перенеси встречу с Олегом на 16")
    fake_ai.push({"intent": "edit", "edit": {"target": "встреча Олег", "when": {"day": "+1", "time": "16:00"}}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert await _edits(db) == [(created.item.id, "text", {"move": True})]


async def test_the_time_prompts_answer(ctx, fake_ai, make_items, t0, db) -> None:
    [created] = await make_items({"kind": "task", "title": "Отчёт"}, now=t0)
    fake_ai.push({"intent": "edit", "edit": {"when": {"text": "в пятницу в 18", "day": "FR", "time": "18:00"}}})
    await processor.handle_edit_time(ctx, {"item_id": created.item.id, "text": "в пятницу в 18", "message_id": 300})
    assert await _edits(db) == [(created.item.id, "text", {"move": True})]


@pytest.mark.parametrize(
    ("patch", "move"),
    [
        (EditPatch(when=When(text="завтра", day="+1")), True),
        (EditPatch(when=When(text="когда-нибудь", period="someday")), False),
        (EditPatch(action="done", when=When(text="сегодня", day="+0")), False),
        (EditPatch(title="Новое название"), False),
        (EditPatch(rrule="FREQ=WEEKLY"), False),
    ],
)
def test_what_counts_as_a_move(patch: EditPatch, move: bool) -> None:
    assert processor.moves(patch) is move
