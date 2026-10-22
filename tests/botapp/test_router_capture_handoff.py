"""The capture router hands a saved message over to its job (docs/features/iteration-4.md §7): the job of a message
or of a reply to a card starts with it instead of loading it again; album parts are loaded by their album job."""

from __future__ import annotations

from dumpbot.bot.sender import REACT_SEEN


async def test_a_saved_message_is_handed_to_its_job(feed, updates, seed, tg, ctx) -> None:
    update = updates.message("купить молоко")
    await feed(update)
    [raw] = await seed.raws()
    fresh = ctx.take_fresh(raw.id)
    assert fresh is not None
    assert (fresh.id, fresh.text, fresh.status, fresh.tg_date) == (raw.id, "купить молоко", "new", raw.tg_date)
    assert ctx.jobs_wakeup.is_set() and tg.reactions == [(update.message.message_id, REACT_SEEN)]


async def test_a_reply_to_a_card_is_handed_to_its_edit_job(feed, updates, seed, ctx) -> None:
    created = await seed.item()
    await seed.bot_message(700, purpose="card", item_id=created.item.id, occurrence_id=created.occurrence.id)
    await feed(updates.message("перенеси на пятницу", reply_to=700))
    [raw] = await seed.raws()
    fresh = ctx.take_fresh(raw.id)
    assert fresh is not None and fresh.reply_item_id == created.item.id


async def test_album_parts_and_redelivered_messages_are_not_handed_over(feed, updates, seed, ctx) -> None:
    await feed(updates.message(photo=True, caption="чек", media_group_id="g1"))
    [part] = await seed.raws()
    assert ctx.take_fresh(part.id) is None
    update = updates.message("купить хлеб")
    await feed(update)
    raw = next(r for r in await seed.raws() if r.text == "купить хлеб")
    assert ctx.take_fresh(raw.id) is not None
    await feed(update)  # Telegram sends it again after a crash: saved already, its job has run or will load it
    assert ctx.take_fresh(raw.id) is None
