"""Callbacks of checklists and related items (docs/features/iteration-2.md §C): [☐ N] / [☑ N] toggles redraw the
parent's card and the last one sends 🎉, a step done from its reminder redraws the parent's card, [🪄 Разбить на шаги]
queues job "split" (once at a time), [🔗 Похожее] sends the related cards."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import select

from dumpbot import clock
from dumpbot.ai.schemas import Extraction
from dumpbot.bot.cbdata import ItemCb, OccCb, SubCb
from dumpbot.db.models import AiUsage, BotMessage, Item, ItemLink, Job, Occurrence
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import CreatedItem
from dumpbot.services import items
from dumpbot.workers import jobs

CLOSED = "🎉 Все шаги выполнены — «Переезд» закрыт"


async def checklist(seed, *steps: dict, title: str = "Переезд") -> tuple[CreatedItem, list[Item]]:
    spec = {"kind": "task", "title": title, "when": {"text": "в субботу", "day": "SA"}, "steps": list(steps)}
    async with seed.sessionmaker() as s:
        [created] = await items.create_items(
            s,
            raw=None,
            extraction=Extraction.model_validate({"items": [spec]}),
            source="text",
            source_text=None,
            source_meta=None,
            urls=[],
            prefs=Prefs(),
            now=clock.utcnow(),
            model=None,
            prompt_version=None,
        )
        await s.commit()
        stmt = select(Item).where(Item.parent_id == created.item.id).order_by(Item.position)
        return created, list((await s.scalars(stmt)).all())


async def status(seed, item_id: int) -> str:
    async with seed.sessionmaker() as s:
        return (await s.get(Item, item_id)).status


async def test_toggles_redraw_the_card_and_the_last_one_celebrates(feed, updates, seed, tg, ctx, get_buttons) -> None:
    parent, steps = await checklist(seed, {"title": "Упаковать вещи"}, {"title": "Отдать ключи"})
    await seed.bot_message(900, purpose="card", item_id=parent.item.id, occurrence_id=parent.occurrence.id)

    await feed(updates.callback(SubCb(a="done", id=steps[0].id), message_id=900))
    [edit] = tg.edits
    assert edit.message_id == 900 and "☑ 1. Упаковать вещи" in edit.text and "📋 Шаги: 1/2" in edit.text
    assert get_buttons(edit.reply_markup)[:2] == [
        ("☑ 1", SubCb(a="undo", id=steps[0].id).pack()),
        ("☐ 2", SubCb(a="done", id=steps[1].id).pack()),
    ]
    assert tg.toasts == ["☑ Упаковать вещи"] and tg.texts == []
    assert ctx.scheduler_wakeup.is_set()

    tg.clear()
    await feed(updates.callback(SubCb(a="done", id=steps[1].id), message_id=900))
    [edit] = tg.edits  # the pressed card only: it is the newest card of the task as well
    assert "✅ сделано" in edit.text and "📋 Шаги: 2/2" in edit.text
    assert tg.texts == [CLOSED]
    assert await status(seed, parent.item.id) == "done"

    tg.clear()
    await feed(updates.callback(SubCb(a="undo", id=steps[1].id), message_id=900))
    assert "✅ сделано" not in tg.edits[0].text and tg.texts == []
    assert await status(seed, parent.item.id) == "open"


async def test_stale_and_unknown_toggles(feed, updates, seed, tg) -> None:
    parent, [step] = await checklist(seed, {"title": "Упаковать вещи"})
    await feed(updates.callback(SubCb(a="flip", id=step.id), message_id=900))
    await feed(updates.callback(SubCb(a="done", id=999999), message_id=900))
    await feed(updates.callback(SubCb(a="done", id=parent.item.id), message_id=900))  # not a step
    assert tg.toasts == ["Кнопка устарела", "Запись не найдена", "Запись не найдена"]
    async with seed.sessionmaker() as s:
        await items.delete_item(s, parent.item.id, clock.utcnow())
        await s.commit()
    await feed(updates.callback(SubCb(a="done", id=step.id), message_id=900))
    assert tg.toasts[-1] == "🗑 Запись удалена" and tg.edits[-1].text.startswith("🗑 Удалено")


async def test_a_step_done_from_its_reminder_redraws_the_task_card(feed, updates, seed, tg) -> None:
    timed = {"title": "Заказать машину", "when": {"text": "завтра в 15", "day": "+1", "time": "15:00"}}
    parent, [step] = await checklist(seed, timed)
    async with seed.sessionmaker() as s:
        [occ] = (await s.scalars(select(Occurrence).where(Occurrence.item_id == step.id))).all()
    await seed.bot_message(900, purpose="card", item_id=parent.item.id, occurrence_id=parent.occurrence.id)
    await seed.bot_message(901, purpose="reminder", item_id=step.id, occurrence_id=occ.id)

    await feed(updates.callback(OccCb(a="done", id=occ.id), message_id=901))
    reminder, card = tg.edits
    assert reminder.message_id == 901 and reminder.text.startswith("✅ <s>Заказать машину</s>")
    assert card.message_id == 900 and "☑ 1. Заказать машину" in card.text and "✅ сделано" in card.text
    assert tg.texts == [CLOSED]

    tg.clear()
    await feed(updates.callback(OccCb(a="undone", id=occ.id), message_id=901))
    reminder, card = tg.edits
    assert reminder.text.split("\n")[1] == "↳ шаг из «Переезд»"
    assert "☐ 1. Заказать машину" in card.text and await status(seed, parent.item.id) == "open"


async def test_split_button_queues_one_job_at_a_time(feed, updates, seed, tg, ctx) -> None:
    created = await seed.item("Ремонт", timed=False)
    await feed(updates.callback(ItemCb(a="split", id=created.item.id), message_id=900))
    await feed(updates.callback(ItemCb(a="split", id=created.item.id), message_id=900))
    assert tg.toasts == ["🪄 Разбиваю на шаги…", "Уже разбиваю на шаги…"]
    [job] = await seed.jobs()
    assert (job.kind, job.payload["item_id"], job.payload["message_id"]) == ("split", created.item.id, 900)
    assert ctx.jobs_wakeup.is_set() and tg.edits == []

    async with seed.sessionmaker() as s:  # a finished (failed) split can be tried again
        job = await s.get(Job, job.id)
        job.status = "dead"
        await s.commit()
    await feed(updates.callback(ItemCb(a="split", id=created.item.id), message_id=900))
    assert tg.toasts[-1] == "🪄 Разбиваю на шаги…" and len(await seed.jobs()) == 2


async def test_a_split_press_waits_for_the_press_before_it(feed, updates, seed, tg) -> None:
    created = await seed.item("Ремонт", timed=False)
    async with seed.sessionmaker() as s:  # the first of two quick presses holds the item row and queues its job
        await items.lock_item(s, created.item.id)
        second = asyncio.create_task(feed(updates.callback(ItemCb(a="split", id=created.item.id), message_id=900)))
        await asyncio.sleep(0.3)
        assert not second.done()  # aiogram runs callbacks concurrently: this one waits for the lock
        await jobs.enqueue(s, "split", {"item_id": created.item.id, "message_id": 900}, dedupe_key="split:first")
        await s.commit()
    await second
    assert tg.toasts == ["Уже разбиваю на шаги…"] and len(await seed.jobs()) == 1


async def test_split_with_the_budget_spent_queues_nothing(feed, updates, seed, tg, frozen) -> None:
    created = await seed.item("Ремонт", timed=False)
    async with seed.sessionmaker() as s:
        s.add(AiUsage(created_at=frozen.now, purpose="extract", model="m", cost_usd=Decimal("99")))
        await s.commit()
    await feed(updates.callback(ItemCb(a="split", id=created.item.id), message_id=900))
    [toast] = tg.toasts
    assert toast.startswith("⛔ Бюджет на ИИ исчерпан до вс 1 ноя —")  # not "Разбиваю на шаги…" for weeks
    assert await seed.jobs() == [] and tg.edits == []


async def test_deleting_the_open_step_closes_the_parent_and_redraws_its_card(feed, updates, seed, tg) -> None:
    parent, steps = await checklist(seed, {"title": "Упаковать вещи"}, {"title": "Отдать ключи"})
    await seed.bot_message(900, purpose="card", item_id=parent.item.id, occurrence_id=parent.occurrence.id)
    await seed.bot_message(901, purpose="card", item_id=steps[1].id)
    await feed(updates.callback(SubCb(a="done", id=steps[0].id), message_id=900))
    tg.clear()

    await feed(updates.callback(ItemCb(a="del", id=steps[1].id), message_id=901))  # 🗑 on the step's own card
    step_card, parent_card = tg.edits
    assert step_card.message_id == 901 and step_card.text.startswith("🗑 Удалено")
    assert parent_card.message_id == 900 and "Отдать ключи" not in parent_card.text
    assert "✅ сделано" in parent_card.text and tg.texts == [CLOSED]

    tg.clear()
    await feed(updates.callback(ItemCb(a="undel", id=steps[1].id), message_id=901))
    assert tg.toasts == ["↩️ Восстановлено"] and await status(seed, parent.item.id) == "open"
    assert "☐ 2. Отдать ключи" in tg.edits[-1].text and tg.edits[-1].message_id == 900


async def test_restoring_a_step_of_a_deleted_task_brings_the_task_back(feed, updates, seed, tg) -> None:
    parent, [step] = await checklist(seed, {"title": "Упаковать вещи"})
    async with seed.sessionmaker() as s:
        await items.delete_item(s, parent.item.id, clock.utcnow())
        await s.commit()
    await feed(updates.callback(ItemCb(a="undel", id=step.id), message_id=901))
    assert tg.toasts == ["↩️ Восстановлено"]
    assert (await status(seed, step.id), await status(seed, parent.item.id)) == ("open", "open")


async def test_split_button_of_an_item_with_steps_or_a_series(feed, updates, seed, tg) -> None:
    parent, _ = await checklist(seed, {"title": "Упаковать вещи"})
    await feed(updates.callback(ItemCb(a="split", id=parent.item.id), message_id=900))
    series = await seed.item("Зарядка", rrule="FREQ=DAILY", timed=False)
    await feed(updates.callback(ItemCb(a="split", id=series.item.id), message_id=901))
    assert tg.toasts == ["У задачи уже есть шаги", "Эту запись не разбить на шаги"]
    assert len(tg.edits) == 2 and await seed.jobs() == []


async def test_related_button_sends_the_related_cards(feed, updates, seed, tg) -> None:
    old = await seed.item("Купить наушники Sony", kind="purchase", timed=False)
    new = await seed.item("Купить чехол", kind="purchase", timed=False)
    await feed(updates.callback(ItemCb(a="rel", id=new.item.id), message_id=900))
    assert tg.toasts == ["Похожих записей нет"] and tg.texts == []

    async with seed.sessionmaker() as s:
        s.add(ItemLink(item_id=new.item.id, related_id=old.item.id))
        await s.commit()
    tg.clear()
    await feed(updates.callback(ItemCb(a="rel", id=new.item.id), message_id=900))
    assert tg.toasts == ["🔗 Похожих записей: 1"]
    [sent] = tg.sent
    assert sent.text.startswith("🛒 <b>Купить наушники Sony</b>") and "🔗 похоже на: «Купить чехол»" in sent.text
    assert sent.reply_parameters.message_id == 900
    async with seed.sessionmaker() as s:
        records = (await s.scalars(select(BotMessage).where(BotMessage.item_id == old.item.id))).all()
    assert [r.purpose for r in records] == ["card"]  # a reply to it edits that item
