"""Processor and checklists / related items (docs/features/iteration-2.md §C): RELATED CANDIDATES before the extraction
and only their ids linked, "распиши по шагам" in a message, "разбей на шаги" in reply to a card or by target, job
"split" of the [🪄 Разбить на шаги] button (redraw, nothing to split, failures), 🎉 after an edit closes a parent.
FakeAI stands in for extract / split_steps (conftest)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from dumpbot.ai.client import AIBadResponse, AIUnavailable
from dumpbot.db.models import BotMessage, Item, ItemLink, Job, OnceKey, RawMessage
from dumpbot.domain.prefs import Prefs
from dumpbot.services import items, processor

MOVE = {
    "intent": "save",
    "items": [
        {
            "kind": "task",
            "title": "Переезд",
            "steps": [{"title": "Упаковать вещи"}, {"title": "Заказать машину"}],
        }
    ],
}


def reply_to(call: dict) -> int | None:
    params = call.get("reply_parameters")
    return params.message_id if params is not None else None


def labels(call: dict) -> list[str]:
    markup = call.get("reply_markup")
    return [b.text for row in markup.inline_keyboard for b in row] if markup is not None else []


async def steps_of(db, parent_id: int) -> list[Item]:
    return await db(select(Item).where(Item.parent_id == parent_id).order_by(Item.position))


# --- related candidates --------------------------------------------------------------------------------------------


async def test_candidates_go_to_the_model_and_only_their_ids_are_linked(
    ctx, add_raw, fake_ai, bot, make_items, t0, db
) -> None:
    [old, _] = await make_items(
        {"kind": "purchase", "title": "Купить наушники Sony"},
        {"kind": "note", "title": "Погода"},
        now=t0 - timedelta(days=24),
    )
    raw = await add_raw("купить чехол для наушников Sony")
    fake_ai.push(
        {"items": [{"kind": "purchase", "title": "Купить чехол для наушников Sony", "related": [old.item.id, 4242]}]}
    )
    await processor.handle_message(ctx, {"raw_id": raw.id})

    [inp] = fake_ai.inputs
    assert inp.related == [
        {"id": old.item.id, "kind": "purchase", "title": "Купить наушники Sony", "date": "2026-09-12"}
    ]
    [new] = await db(select(Item).where(Item.raw_message_id == raw.id))
    links = await db(select(ItemLink))
    assert [(link.item_id, link.related_id) for link in links] == [(new.id, old.item.id)]
    [card] = bot.sent
    assert "🔗 похоже на: «Купить наушники Sony» (12 сен)" in card["text"]
    assert "🔗 Похожее" in labels(card)


async def test_a_question_gets_no_candidates(ctx, add_raw, fake_ai, make_items, t0) -> None:
    await make_items({"kind": "purchase", "title": "Купить наушники"}, now=t0)
    raw = await add_raw("? где наушники", force_query=True)
    fake_ai.push({"intent": "query", "query": {"text": "наушники"}})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert fake_ai.inputs[0].related == []


async def test_a_note_linked_to_an_earlier_item_gets_a_card(ctx, add_raw, fake_ai, bot, make_items, t0) -> None:
    [old] = await make_items({"kind": "idea", "title": "Подкаст про путешествия"}, now=t0)
    raw = await add_raw("идея для подкаста: выпуск про Грузию")
    fake_ai.push({"items": [{"kind": "idea", "title": "Выпуск подкаста про Грузию", "related": [old.item.id]}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [card] = bot.sent  # an idea alone would only get 👌; the hint is worth a card
    assert "🔗 похоже на: «Подкаст про путешествия»" in card["text"]


# --- steps in a message --------------------------------------------------------------------------------------------


async def test_steps_in_a_message_make_one_card_with_the_checklist(ctx, add_raw, fake_ai, bot, db) -> None:
    raw = await add_raw("переезд, распиши по шагам")
    fake_ai.push(MOVE)
    await processor.handle_message(ctx, {"raw_id": raw.id})
    [card] = bot.sent
    assert "📋 Шаги: 0/2\n☐ 1. Упаковать вещи\n☐ 2. Заказать машину" in card["text"]
    assert labels(card)[:2] == ["☐ 1", "☐ 2"] and "🪄 Разбить на шаги" not in labels(card)
    [parent] = await db(select(Item).where(Item.raw_message_id == raw.id, Item.parent_id.is_(None)))
    assert [s.title for s in await steps_of(db, parent.id)] == ["Упаковать вещи", "Заказать машину"]


# --- "разбей на шаги" ------------------------------------------------------------------------------------------------


async def test_split_by_a_reply_to_the_card(ctx, add_raw, fake_ai, bot, make_items, t0, db, get) -> None:
    [created] = await make_items({"kind": "task", "title": "Отпуск"}, now=t0)
    raw = await add_raw("разбей на шаги", reply_item_id=created.item.id, message_id=700)
    fake_ai.push({"intent": "edit", "edit": {"action": "split"}})
    fake_ai.splits.append(["Купить билеты", "Забронировать отель"])
    payload = {"raw_id": raw.id, "item_id": created.item.id}
    await processor.handle_edit(ctx, payload)

    [inp] = fake_ai.split_inputs
    assert inp.note == "разбей на шаги"
    assert inp.task == {"kind": "task", "title": "Отпуск", "due_local": "2026-10-06 19:00"}
    assert [s.title for s in await steps_of(db, created.item.id)] == ["Купить билеты", "Забронировать отель"]
    [card] = bot.sent
    assert reply_to(card) == 700 and "☐ 1. Купить билеты" in card["text"]
    assert bot.reactions == [(700, "👌")]
    assert (await get(RawMessage, raw.id)).status == "done"
    assert await get(OnceKey, f"edit:{raw.id}:{created.item.id}") is not None

    await processor.handle_edit(ctx, payload)  # redelivered: nothing again
    assert len(fake_ai.split_inputs) == 1 and len(bot.sent) == 1


async def test_split_of_an_item_with_steps_only_shows_the_card(ctx, add_raw, fake_ai, bot, make_items, t0) -> None:
    [created] = await make_items(MOVE["items"][0], now=t0)
    raw = await add_raw("разбей на шаги", reply_item_id=created.item.id, message_id=701)
    fake_ai.push({"intent": "edit", "edit": {"action": "split"}})
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})
    assert fake_ai.split_inputs == []
    [card] = bot.sent
    assert card["text"].startswith(processor.HAS_STEPS_TEXT) and "☐ 1. Упаковать вещи" in card["text"]
    assert bot.reactions == [(701, "👌")]


async def test_split_by_target(ctx, add_raw, fake_ai, bot, make_items, t0, db) -> None:
    [created] = await make_items({"kind": "task", "title": "Ремонт на кухне"}, now=t0)
    raw = await add_raw("разбей ремонт на кухне на шаги")
    fake_ai.push({"intent": "edit", "edit": {"target": "ремонт на кухне", "action": "split"}})
    fake_ai.splits.append(["Выбрать плитку", "Найти мастера"])
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert len(await steps_of(db, created.item.id)) == 2
    [card] = bot.sent
    assert reply_to(card) == raw.message_id and "☐ 2. Найти мастера" in card["text"]


# --- job "split" (the button) --------------------------------------------------------------------------------------


async def test_split_job_redraws_the_card(ctx, fake_ai, bot, make_items, t0, db) -> None:
    [created] = await make_items({"kind": "task", "title": "Ремонт", "details": "кухня и ванная"}, now=t0)
    fake_ai.splits.append(["Выбрать плитку", "Найти мастера", "Купить материалы"])
    await processor.handle_split(ctx, {"item_id": created.item.id, "message_id": 555, "now": t0.isoformat()})

    [inp] = fake_ai.split_inputs
    assert inp.note is None and inp.task["details"] == "кухня и ванная" and inp.now == t0
    [edit] = bot.calls_of("edit_message_text")
    assert edit["message_id"] == 555 and "📋 Шаги: 0/3" in edit["text"]
    assert bot.sent == [] and bot.reactions == []
    assert [s.position for s in await steps_of(db, created.item.id)] == [1, 2, 3]


async def test_split_job_with_nothing_to_split(ctx, fake_ai, bot, make_items, t0, db) -> None:
    [created] = await make_items({"kind": "task", "title": "Позвонить маме"}, now=t0)
    fake_ai.splits.append([])
    await processor.handle_split(ctx, {"item_id": created.item.id, "message_id": 555})
    [note] = bot.sent
    assert note["text"].startswith(processor.NO_STEPS_TEXT) and reply_to(note) == 555
    assert await steps_of(db, created.item.id) == []


async def test_split_job_of_a_closed_item_asks_nothing(ctx, fake_ai, bot, make_items, t0) -> None:
    [created] = await make_items({"kind": "task", "title": "Сделано", "when": {"day": "+0", "time": "12:00"}}, now=t0)
    await processor.handle_split(ctx, {"item_id": created.item.id, "message_id": 555})  # still open: it splits
    assert len(fake_ai.split_inputs) == 1

    [series] = await make_items({"kind": "task", "title": "Зарядка", "rrule": "FREQ=DAILY"}, now=t0)
    await processor.handle_split(ctx, {"item_id": series.item.id, "message_id": 556})
    assert len(fake_ai.split_inputs) == 1
    assert bot.sent[-1]["text"].startswith(processor.CANT_SPLIT_TEXT)

    await processor.handle_split(ctx, {"item_id": 999999, "message_id": 557})
    assert bot.sent[-1]["text"] == processor.ITEM_GONE_TEXT


async def test_split_job_failures(ctx, fake_ai, bot, make_items, t0, db) -> None:
    [created] = await make_items({"kind": "task", "title": "Ремонт"}, now=t0)
    payload = {"item_id": created.item.id, "message_id": 555}

    fake_ai.splits.append(AIBadResponse("no valid steps"))
    await processor.handle_split(ctx, payload)
    assert bot.texts[-1] == processor.FAILED_SPLIT_TEXT and reply_to(bot.sent[-1]) == 555

    fake_ai.splits.extend([AIUnavailable("down"), AIUnavailable("down")])
    for _ in range(2):  # told once, the queue retries
        try:
            await processor.handle_split(ctx, payload)
        except AIUnavailable:
            pass
    assert bot.texts.count(processor.SPLIT_WAIT_TEXT) == 1
    assert await steps_of(db, created.item.id) == []

    await processor.on_dead(ctx, Job(id=7, kind="split", payload=payload))
    assert bot.texts[-1] == processor.FAILED_SPLIT_TEXT
    assert bot.reactions == []  # the message is the bot's own card


async def test_a_split_job_that_waited_plans_from_its_run_time(ctx, fake_ai, make_items, t0) -> None:
    [created] = await make_items({"kind": "task", "title": "Ремонт"}, now=t0)
    fake_ai.splits.extend([["Выбрать плитку"], ["Найти мастера"]])
    pressed = t0 - timedelta(days=12)  # queued on Sep 24, deferred by the budget until today
    await processor.handle_split(ctx, {"item_id": created.item.id, "message_id": 555, "now": pressed.isoformat()})
    assert fake_ai.split_inputs[0].now == t0  # "завтра" of a step is tomorrow, not Sep 25

    [other] = await make_items({"kind": "task", "title": "Переезд"}, now=t0)
    fresh = t0 - timedelta(minutes=2)  # a job that ran right away keeps the press time
    await processor.handle_split(ctx, {"item_id": other.item.id, "message_id": 556, "now": fresh.isoformat()})
    assert fake_ai.split_inputs[1].now == fresh


# --- 🎉 after an edit ------------------------------------------------------------------------------------------------


async def test_a_new_time_for_a_ticked_step_redraws_the_reopened_parent(
    ctx, fake_ai, bot, make_items, t0, db, sessionmaker
) -> None:
    [created] = await make_items({"kind": "task", "title": "Переезд", "steps": [{"title": "Упаковать вещи"}]}, now=t0)
    [step] = await steps_of(db, created.item.id)
    async with sessionmaker() as s:
        await items.toggle_step(s, step.id, done=True, prefs=Prefs(tz="Europe/Warsaw"), now=t0)
        s.add(BotMessage(chat_id=ctx.chat_id, message_id=700, purpose="card", item_id=created.item.id))
        await s.commit()
    [parent] = await db(select(Item).where(Item.id == created.item.id))
    assert parent.status == "done"

    # "🕒 Время" of the ticked step: «завтра 12:00»
    fake_ai.push({"intent": "edit", "edit": {"when": {"text": "завтра 12:00", "day": "+1", "time": "12:00"}}})
    await processor.handle_edit_time(ctx, {"item_id": step.id, "text": "завтра 12:00", "now": t0.isoformat()})
    [parent] = await db(select(Item).where(Item.id == created.item.id))
    assert parent.status == "open"
    [edit] = bot.calls_of("edit_message_text")  # the parent's chat card is not left at «✅ сделано»
    assert edit["message_id"] == 700 and "☐ 1. Упаковать вещи" in edit["text"] and "✅ сделано" not in edit["text"]


async def test_done_by_a_reply_to_the_last_step_celebrates(ctx, add_raw, fake_ai, bot, make_items, t0, db) -> None:
    [created] = await make_items(
        {"kind": "task", "title": "Переезд", "steps": [{"title": "Отдать ключи"}]},
        now=t0,
    )
    [step] = await steps_of(db, created.item.id)
    raw = await add_raw("готово", reply_item_id=step.id, message_id=800)
    fake_ai.push({"intent": "edit", "edit": {"action": "done"}})
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": step.id})
    card, closed = bot.sent
    assert "Отдать ключи" in card["text"] and "↳ шаг из «Переезд»" in card["text"]
    assert closed["text"] == "🎉 Все шаги выполнены — «Переезд» закрыт"
    [parent] = await db(select(Item).where(Item.id == created.item.id))
    assert parent.status == "done"
