"""[↩️ Отменить] (``UndoCb``) under an edit applied by description (docs/features/iteration-3.md §3): the edit is
taken back exactly, the message turns into the item's card; a double tap, a stale undo (changed since) and a button
without a stored undo only get a toast."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from dumpbot.ai.schemas import EditPatch, StepSpec
from dumpbot.bot.cbdata import ItemCb, OccCb, UndoCb
from dumpbot.bot.keyboards import undo_keyboard
from dumpbot.bot.routers.callbacks import UNDO_ALREADY, UNDO_STALE
from dumpbot.db.models import Item, Occurrence, RawMessage
from dumpbot.domain.prefs import Prefs
from dumpbot.services import items, undo

REPLY = "✅ Закрыл «Купить наушники»"


async def apply(seed, frozen, item_id: int, message_id: int = 61, **fields: Any) -> RawMessage:
    """The edit applied by description, its undo kept with a new raw message (as the processor does)."""
    raw = await seed.raw(message_id, text="правка")
    async with seed.sessionmaker() as s:
        applied = await undo.apply(s, item_id, EditPatch.model_validate(fields), Prefs(), frozen.now)
        fresh = await s.get(RawMessage, raw.id)
        undo.keep(fresh, item_id, applied.snapshot)
        await s.commit()
    return raw


async def _item(seed, item_id: int) -> Item:
    async with seed.sessionmaker() as s:
        return await s.get(Item, item_id)


async def _occurrences(seed, item_id: int) -> list[tuple[str, int]]:
    async with seed.sessionmaker() as s:
        occs = (await s.scalars(select(Occurrence).where(Occurrence.item_id == item_id))).all()
        return [(o.status, len(await items.pending_reminders(s, o.id))) for o in occs]


def press(updates, raw: RawMessage, item_id: int, text: str = REPLY):
    return updates.callback(
        UndoCb(raw=raw.id, item=item_id), message_id=950, text=text, reply_markup=undo_keyboard(raw.id, item_id)
    )


async def test_undo_of_done_reopens_the_item_and_shows_its_card(feed, updates, seed, tg, frozen, ctx, get_buttons):
    created = await seed.item()
    before = await _occurrences(seed, created.item.id)
    raw = await apply(seed, frozen, created.item.id, action="done", target="наушники")
    assert (await _item(seed, created.item.id)).status == "done"

    await feed(press(updates, raw, created.item.id))

    assert (await _item(seed, created.item.id)).status == "open"
    assert await _occurrences(seed, created.item.id) == before == [("open", 1)]
    assert tg.toasts == ["↩️ Отменено"]
    [edit] = tg.edits
    assert edit.message_id == 950 and edit.text.startswith("📌 <b>Купить наушники</b>")
    assert ("✏️ Изменить", ItemCb(a="edit", id=created.item.id).pack()) in get_buttons(edit.reply_markup)
    assert ctx.scheduler_wakeup.is_set()


async def test_double_tap_undoes_once(feed, updates, seed, tg, frozen):
    created = await seed.item()
    raw = await apply(seed, frozen, created.item.id, action="delete")

    await feed(press(updates, raw, created.item.id, text="🗑 Удалил «Купить наушники»"))
    await feed(press(updates, raw, created.item.id, text="🗑 Удалил «Купить наушники»"))

    assert (await _item(seed, created.item.id)).status == "open"
    assert tg.toasts == ["↩️ Отменено", UNDO_ALREADY]
    assert len(tg.edits) == 1  # the second press leaves the message alone


async def test_stale_undo_changes_nothing_and_drops_the_button(feed, updates, seed, tg, frozen):
    created = await seed.item()
    raw = await apply(seed, frozen, created.item.id, action="done")
    async with seed.sessionmaker() as s:  # the owner renamed it since
        await items.set_title(s, created.item.id, "Купить наушники Sony")
        await s.commit()

    await feed(press(updates, raw, created.item.id))

    item = await _item(seed, created.item.id)
    assert (item.status, item.title) == ("done", "Купить наушники Sony")
    assert tg.toasts == [UNDO_STALE]
    [edit] = tg.edits
    assert edit.text == REPLY and edit.reply_markup is None


async def test_undo_after_the_item_was_reopened_by_a_button_is_stale(feed, updates, seed, tg, frozen):
    created = await seed.item()
    raw = await apply(seed, frozen, created.item.id, action="done")
    await feed(updates.callback(OccCb(a="undone", id=created.occurrence.id), message_id=951))
    tg.clear()

    await feed(press(updates, raw, created.item.id))
    assert tg.toasts == [UNDO_STALE]
    assert (await _item(seed, created.item.id)).status == "open"


async def test_undo_without_a_stored_undo(feed, updates, seed, tg):
    created = await seed.item()
    raw = await seed.raw(62)
    await feed(press(updates, raw, created.item.id))
    await feed(updates.callback(UndoCb(raw=10**9, item=created.item.id)))
    assert tg.toasts == ["Кнопка устарела", "Кнопка устарела"]
    assert tg.edits == []


async def test_undo_of_the_last_step_redraws_the_parent_card(feed, updates, seed, tg, frozen, get_buttons):
    parent = await seed.item("Переезд")
    async with seed.sessionmaker() as s:
        [step] = await items.add_steps(s, parent.item.id, [StepSpec(title="Упаковать вещи")], Prefs(), frozen.now)
        await s.commit()
    await seed.bot_message(940, purpose="card", item_id=parent.item.id, occurrence_id=parent.occurrence.id)
    raw = await apply(seed, frozen, step.item.id, action="done")
    assert (await _item(seed, parent.item.id)).status == "done"  # its last step closed it

    await feed(press(updates, raw, step.item.id, text="✅ Закрыл «Упаковать вещи»"))

    assert (await _item(seed, parent.item.id)).status == "open"
    assert (await _item(seed, step.item.id)).status == "open"
    redrawn = [e for e in tg.edits if e.message_id == 940]
    assert redrawn and "☐ 1. Упаковать вещи" in redrawn[-1].text
