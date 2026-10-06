"""Processor and fuzzy time (docs/features/iteration-2.md §A): cards for spans and "когда-нибудь", a reply that moves
an item to next week, "🕒 Время" answered with "когда-нибудь". Fake AI and bot; reference Tue 2026-10-06."""

from __future__ import annotations

from datetime import date

from dumpbot.db.models import Item
from dumpbot.services import processor

WEEK = {"text": "на этой неделе", "period": "this_week"}
SOMEDAY = {"text": "когда-нибудь", "period": "someday"}


async def test_live_example_gets_a_period_card(
    ctx, add_raw, fake_ai, bot, frozen, local, items_of_raw, occurrences_of, reminders_of
) -> None:
    frozen.set(local(2026, 10, 6, 13, 36))
    raw = await add_raw("На этой неделе посмотреть расбери пай")
    fake_ai.push({"items": [{"kind": "task", "title": "Посмотреть Raspberry Pi", "when": WEEK}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})

    [item] = await items_of_raw(raw.id)
    assert (item.period_kind, item.period_start, item.period_end) == ("week", date(2026, 10, 5), date(2026, 10, 11))
    [occ] = await occurrences_of(item.id)
    assert (occ.precision, occ.due_at) == ("period", local(2026, 10, 9, 19, 0))
    assert [(r.fire_at, r.auto) for r in await reminders_of(occ.id)] == [(local(2026, 10, 9, 19, 0), True)]
    [card] = bot.texts
    assert card.split("\n")[:2] == [
        "📌 <b>Посмотреть Raspberry Pi</b>",
        "🗓 на этой неделе (до вс 11 окт) · напомню пт в 19:00",
    ]
    assert bot.reactions[-1] == (raw.message_id, "👌")


async def test_someday_task_gets_a_card_without_a_date(ctx, add_raw, fake_ai, bot, items_of_raw, occurrences_of):
    raw = await add_raw("когда-нибудь выучить испанский")
    fake_ai.push({"items": [{"kind": "task", "title": "Выучить испанский", "when": SOMEDAY}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})

    [item] = await items_of_raw(raw.id)
    assert (item.someday, item.period_kind) == (True, "someday")
    assert await occurrences_of(item.id) == []
    [card] = bot.texts
    assert card.split("\n")[1] == "🌙 когда-нибудь"


async def test_someday_idea_only_gets_a_reaction(ctx, add_raw, fake_ai, bot, items_of_raw) -> None:
    raw = await add_raw("когда-нибудь написать книгу")
    fake_ai.push({"items": [{"kind": "idea", "title": "Написать книгу", "when": SOMEDAY}]})
    await processor.handle_message(ctx, {"raw_id": raw.id})

    [item] = await items_of_raw(raw.id)
    assert item.someday
    assert bot.texts == [] and bot.reactions[-1] == (raw.message_id, "👌")


async def test_reply_moves_the_item_to_next_week(
    ctx, add_raw, fake_ai, bot, make_items, t0, local, get, occurrences_of
) -> None:
    [created] = await make_items({"kind": "task", "title": "Шины", "when": {"day": "+1"}}, now=t0)
    raw = await add_raw("перенеси на следующую неделю", status="done")
    fake_ai.push({"intent": "edit", "edit": {"when": {"text": "на следующую неделю", "period": "next_week"}}})
    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})

    item = await get(Item, created.item.id)
    assert (item.period_kind, item.period_start, item.period_end) == ("week", date(2026, 10, 12), date(2026, 10, 18))
    [occ] = [o for o in await occurrences_of(item.id) if o.status == "open"]
    assert (occ.precision, occ.due_at) == ("period", local(2026, 10, 16, 19, 0))
    assert "🗓 на следующей неделе (до вс 18 окт) · напомню пн в 10:00 и пт 16 окт в 19:00" in bot.texts[-1]


async def test_time_button_answered_with_someday(ctx, fake_ai, bot, make_items, t0, get, occurrences_of) -> None:
    [created] = await make_items({"kind": "task", "title": "Испанский", "when": {"day": "+1", "time": "15:00"}}, now=t0)
    fake_ai.push({"intent": "edit", "edit": {"when": SOMEDAY}})
    await processor.handle_edit_time(ctx, {"item_id": created.item.id, "text": "когда-нибудь", "message_id": 560})

    item = await get(Item, created.item.id)
    assert (item.someday, item.status) == (True, "open")
    assert [o.status for o in await occurrences_of(item.id)] == ["cancelled"]
    assert bot.texts[-1].split("\n")[1] == "🌙 когда-нибудь"
    assert bot.reactions == [(560, "👌")]
