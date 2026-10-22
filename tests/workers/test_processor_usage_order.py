"""services.processor, what is stored when (docs/features/iteration-4.md §7 and its review): an AI call whose result
is stored before the reply is stored with it — the STT row with its transcript, the split row with its steps — so
neither a process killed before the reply nor /usage right after the checklist misses it; a reply to an item that is
gone asks nothing about the budget; the cards of one message go out back to back."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select

from dumpbot.db.models import AiUsage, BotMessage, Item, RawMessage
from dumpbot.services import processor, usage


async def _paid(ctx: Any, purpose: str) -> None:
    """What a real backend does after a paid call (``usage.record_usage``)."""
    await usage.record_usage(ctx.sessionmaker, purpose=purpose, model="m", cost_usd=0.001, audio_seconds=1.0)


async def test_the_stt_call_is_stored_with_its_transcript(ctx, add_raw, bot, stt, fake_ai, db, monkeypatch) -> None:
    """The transcript and the paid STT call's ai_usage row are stored together, before extract(): a process killed
    after that (the watchdog's exit, Render out of memory) loses no cost — the re-run reuses the transcript and
    never calls STT again."""
    bot.files["v"] = b"OGG"
    raw = await add_raw(None, content_type="voice", file_id="v", mime_type="audio/ogg", duration=4)

    async def paid_transcribe(audio: bytes, **kwargs: Any) -> str:
        await _paid(ctx, "stt")
        return "Купить хлеб"

    monkeypatch.setattr(stt, "transcribe", paid_transcribe)
    stored_at_extract: list[tuple[str | None, list[str]]] = []
    real_extract = processor.extract

    async def looking_extract(client: Any, settings: Any, inp: Any) -> Any:
        [stored] = await db(select(RawMessage).where(RawMessage.id == raw.id))
        stored_at_extract.append((stored.transcript, list(await db(select(AiUsage.purpose)))))
        return await real_extract(client, settings, inp)

    monkeypatch.setattr(processor, "extract", looking_extract)
    fake_ai.push({"items": [{"kind": "purchase", "title": "Купить хлеб"}]})

    with usage.deferred() as job:
        await processor.handle_message(ctx, {"raw_id": raw.id})
        assert job.rows == []  # nothing left for the worker

    assert stored_at_extract == [("Купить хлеб", ["stt"])]
    assert list(await db(select(AiUsage.purpose).order_by(AiUsage.id))) == ["stt"]  # once


async def test_the_split_call_is_stored_with_its_steps(ctx, fake_ai, bot, make_items, t0, db, monkeypatch) -> None:
    """/usage pressed when the checklist shows counts the split already (it was written after the redraw)."""
    [created] = await make_items({"kind": "task", "title": "Ремонт"}, now=t0)
    real_split = fake_ai.split_steps

    async def paid_split(client: Any, settings: Any, inp: Any) -> Any:
        await _paid(ctx, "split")
        return await real_split(client, settings, inp)

    monkeypatch.setattr(processor, "split_steps", paid_split)
    fake_ai.splits.append(["Выбрать плитку", "Найти мастера"])
    at_redraw: list[list[str]] = []
    real_edit = bot.edit_message_text

    async def looking_edit(**kwargs: Any) -> Any:
        at_redraw.append(list(await db(select(AiUsage.purpose))))
        return await real_edit(**kwargs)

    monkeypatch.setattr(bot, "edit_message_text", looking_edit)

    with usage.deferred():
        await processor.handle_split(ctx, {"item_id": created.item.id, "message_id": 555, "now": t0.isoformat()})

    assert at_redraw == [["split"]]


async def test_a_reply_to_an_item_gone_says_nothing_about_the_budget(
    ctx, add_raw, set_prefs, make_items, t0, bot, get
) -> None:
    """The month's budget is spent and the item replied to is gone: «🤷 Не нашёл эту запись», and neither the budget
    notice nor ✍ ("will be processed later") for a reply that needs no AI."""
    await set_prefs(budget_usd=0)  # AI switched off: always "exhausted"
    [created] = await make_items({"kind": "task", "title": "Отчёт"}, now=t0)
    raw = await add_raw("перенеси на пятницу", reply_item_id=created.item.id, message_id=700)
    async with ctx.sessionmaker() as session:
        await session.execute(delete(Item).where(Item.id == created.item.id))
        await session.commit()

    await processor.handle_edit(ctx, {"raw_id": raw.id, "item_id": created.item.id})

    assert bot.texts == [processor.ITEM_GONE_TEXT]
    assert bot.reactions == []
    assert (await get(RawMessage, raw.id)).status == "done"


async def test_the_cards_of_one_message_go_out_back_to_back(ctx, add_raw, fake_ai, bot, db, monkeypatch) -> None:
    """Three items, three cards: their bot_messages rows are written together after the last card (one statement),
    not one database wait between two cards."""
    raw = await add_raw("Купить рис, соль, масло в 18")
    when = {"text": "в 18", "time": "18:00"}
    fake_ai.push(
        {"items": [{"kind": "purchase", "title": f"Купить {f}", "when": when} for f in ("рис", "соль", "масло")]}
    )
    recorded_when_sent: list[int] = []
    real_send = bot.send_message

    async def counting_send(**kwargs: Any) -> Any:
        recorded_when_sent.append(len(await db(select(BotMessage))))
        return await real_send(**kwargs)

    monkeypatch.setattr(bot, "send_message", counting_send)

    await processor.handle_message(ctx, {"raw_id": raw.id})

    assert recorded_when_sent == [0, 0, 0]
    rows = await db(select(BotMessage).order_by(BotMessage.message_id))
    assert [(m.purpose, m.raw_message_id) for m in rows] == [("card", raw.id)] * 3
    assert len({m.item_id for m in rows}) == 3
