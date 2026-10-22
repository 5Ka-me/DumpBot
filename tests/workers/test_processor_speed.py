"""services.processor and the database waits before the card (docs/features/iteration-4.md §7): a job starts with
what the capture router handed over, the raws are marked done after the card (a re-run after a crash in between
only marks them), the month's spend is read once per job, a new item's card needs no query of its own."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from dumpbot.ai.schemas import ExtractedItem, Extraction
from dumpbot.db import meter
from dumpbot.db.models import OnceKey, RawMessage
from dumpbot.domain.prefs import Prefs
from dumpbot.services import items, processor, usage

OWNER = 4242


def kinds(m: meter.Meter) -> list[str]:
    return [c.what for c in m.trace or []]


def before_save(m: meter.Meter) -> list[str]:
    calls = kinds(m)
    return calls[: calls.index("BEGIN")]


async def save_items_of(sessionmaker, raw: RawMessage) -> None:
    """What a run of the job saved before it stopped (between the save and marking the raw done)."""
    async with sessionmaker() as s:
        await items.create_items(
            s,
            raw=raw,
            extraction=Extraction(items=[ExtractedItem(kind="purchase", title="Купить молоко")]),
            source="text",
            source_text=raw.text,
            source_meta=None,
            urls=[],
            prefs=Prefs(),
            now=raw.tg_date,
            model="m",
            prompt_version="v1",
        )
        await s.commit()


async def test_a_message_handed_over_goes_straight_to_the_budget_and_the_candidates(
    ctx, add_raw, fake_ai, bot, sessionmaker, items_of_raw, get
) -> None:
    meter.install(sessionmaker.kw["bind"])
    raw = await add_raw("купить молоко в 18")
    ctx.keep_fresh(raw)  # the capture router saved it a moment ago
    await processor._prefs(ctx)  # and the prefs were loaded within the minute (the scheduler does every tick)
    fake_ai.push({"items": [{"kind": "purchase", "title": "Купить молоко", "when": {"text": "в 18", "time": "18:00"}}]})
    with meter.measure(trace=True) as m:
        await processor.handle_message(ctx, {"raw_id": raw.id})
    assert ctx.take_fresh(raw.id) is None
    assert len(await items_of_raw(raw.id)) == 1
    [card] = bot.sent
    assert "Купить молоко" in card["text"] and card["disable_notification"] is False  # quiet hours checked here
    # nothing loaded before the budget check and the related candidates, side by side; then one transaction
    assert sorted(before_save(m)) == ["SELECT", "WITH"]
    calls = kinds(m)
    save = calls[calls.index("BEGIN") : calls.index("COMMIT") + 1]
    assert save == ["BEGIN", "INSERT items", "INSERT occurrences", "INSERT reminders", "COMMIT"]  # no card queries
    assert calls[calls.index("COMMIT") + 1 :] == ["INSERT bot_messages", "UPDATE raw_messages"]
    assert (await get(RawMessage, raw.id)).status == "done"


async def test_the_raw_is_marked_done_after_its_card(ctx, add_raw, fake_ai, bot, get) -> None:
    raw = await add_raw("купить молоко")
    fake_ai.push({"items": [{"kind": "purchase", "title": "Купить молоко"}]})
    status_at_card: list[str] = []
    send = bot.send_message

    async def send_message(**kwargs: Any) -> Any:
        status_at_card.append((await get(RawMessage, raw.id)).status)
        return await send(**kwargs)

    bot.send_message = send_message
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert status_at_card == ["new"]
    done = await get(RawMessage, raw.id)
    assert (done.status, done.error) == ("done", None) and done.processed_at is not None
    assert bot.reactions == [(raw.message_id, "👌")]


async def test_a_message_saved_before_a_crash_is_only_marked_done(
    ctx, add_raw, fake_ai, bot, sessionmaker, items_of_raw, get
) -> None:
    raw = await add_raw("купить молоко")
    await save_items_of(sessionmaker, raw)
    await processor.handle_message(ctx, {"raw_id": raw.id})
    assert (await get(RawMessage, raw.id)).status == "done"
    assert fake_ai.inputs == [] and bot.sent == []  # no second AI call, no second card
    assert len(await items_of_raw(raw.id)) == 1


async def test_an_album_saved_before_a_crash_is_only_marked_done(ctx, add_raw, fake_ai, bot, sessionmaker, get) -> None:
    first = await add_raw("чек", content_type="photo", media_group_id="g1", file_id="f1")
    second = await add_raw(None, content_type="photo", media_group_id="g1", file_id="f2")
    await save_items_of(sessionmaker, first)
    await processor.handle_album(ctx, {"media_group_id": "g1", "chat_id": OWNER})
    assert [(await get(RawMessage, r.id)).status for r in (first, second)] == ["done", "done"]
    assert fake_ai.inputs == [] and bot.sent == []


async def test_a_reply_applied_before_a_crash_is_only_marked_done(
    ctx, add_raw, fake_ai, bot, sessionmaker, make_items, t0, get
) -> None:
    [task] = await make_items({"kind": "task", "title": "Позвонить маме"}, now=t0)
    reply = await add_raw("лучше в 19", reply_item_id=task.item.id)
    async with sessionmaker() as s:  # the edit's once-key is committed with the edit
        s.add(OnceKey(key=f"edit:{reply.id}:{task.item.id}", created_at=t0))
        await s.commit()
    await processor.handle_edit(ctx, {"raw_id": reply.id, "item_id": task.item.id})
    assert (await get(RawMessage, reply.id)).status == "done"
    assert fake_ai.inputs == [] and bot.sent == []


async def test_a_reply_is_marked_done_after_its_card(ctx, add_raw, fake_ai, bot, make_items, t0, get, db) -> None:
    [task] = await make_items({"kind": "task", "title": "Позвонить маме"}, now=t0)
    reply = await add_raw("лучше в 19", reply_item_id=task.item.id)
    ctx.keep_fresh(reply)
    fake_ai.push({"intent": "edit", "edit": {"action": "update", "when": {"text": "в 19", "time": "19:00"}}})
    await processor.handle_edit(ctx, {"raw_id": reply.id, "item_id": task.item.id})
    [card] = bot.sent
    assert "19:00" in card["text"]
    assert (await get(RawMessage, reply.id)).status == "done"
    assert await db(select(OnceKey.key)) == [f"edit:{reply.id}:{task.item.id}"]


async def test_a_voice_note_reads_the_spend_once(ctx, add_raw, fake_ai, bot, stt, sessionmaker, db) -> None:
    meter.install(sessionmaker.kw["bind"])
    raw = await add_raw(None, content_type="voice", file_id="v1", duration=3, mime_type="audio/ogg")
    stt.results.append("купить молоко в 18")
    fake_ai.push({"items": [{"kind": "purchase", "title": "Купить молоко", "when": {"text": "в 18", "time": "18:00"}}]})
    await processor._prefs(ctx)
    with usage.deferred() as job, meter.measure(trace=True) as m:
        await processor.handle_message(ctx, {"raw_id": raw.id})
    # the raw's load, then: the spend (read while the file downloads: the paid STT call's check, reused before
    # extract()), the transcript stored side by side with the related candidates
    assert before_save(m).count("SELECT") == 2
    assert "UPDATE raw_messages" in before_save(m) and "WITH" in before_save(m)
    assert job.rows == []
    assert [r.transcript for r in await db(select(RawMessage))] == ["купить молоко в 18"]
    assert bot.reactions == [(raw.message_id, "👌")]
