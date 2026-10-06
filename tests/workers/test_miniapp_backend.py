"""Queue and pipeline pieces the Mini App relies on (docs/features/iteration-2.md §D): a handler's dict result kept in
``Job.result``, ``pending_job``, the raw message of "закинуть в свалку" (synthetic id, URLs from plain text), and the
"edit_time" / "split" jobs started from the app (outcome returned, nothing in the chat)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from dumpbot.ai.client import AIBadResponse
from dumpbot.db.models import Job, RawMessage
from dumpbot.services import ingest, processor
from dumpbot.workers import jobs
from dumpbot.workers.jobs import JobWorker

WHEN_FR = {"intent": "edit", "edit": {"when": {"text": "в пятницу", "day": "FR", "time": "18:00"}}}


async def test_a_dict_result_is_kept_with_the_job(ctx, sessionmaker, jobs_all) -> None:
    async def reports(ctx_: Any, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "n": payload["n"]}

    async def silent(ctx_: Any, payload: dict[str, Any]) -> None:
        return None

    async with sessionmaker() as session:
        await jobs.enqueue(session, "reports", {"n": 3})
        await jobs.enqueue(session, "silent", {})
        await session.commit()
    worker = JobWorker(ctx, {"reports": reports, "silent": silent}, on_dead=None)
    assert await worker.run_once() == 2
    first, second = await jobs_all()
    assert (first.status, first.result) == ("done", {"ok": True, "n": 3})
    assert (second.status, second.result) == ("done", None)


async def test_pending_job(sessionmaker) -> None:
    async with sessionmaker() as session:
        assert await jobs.pending_job(session, "split", 7) is None
        job_id = await jobs.enqueue(session, "split", {"item_id": 7})
        await jobs.enqueue(session, "edit_time", {"item_id": 8})
        await session.commit()
        assert await jobs.pending_job(session, "split", 7) == job_id
        assert await jobs.pending_job(session, "split", 8) is None
        job = await session.get(Job, job_id)
        job.status = "done"
        await session.commit()
        assert await jobs.pending_job(session, "split", 7) is None


def test_urls_in_plain_text() -> None:
    text = "глянь https://habr.com/ru/articles/1/, и www.example.com/x) и (https://a.b/c?d=1#e). ещёhttps://x.y"
    assert ingest.text_urls(text) == [
        "https://habr.com/ru/articles/1/",
        "https://www.example.com/x",
        "https://a.b/c?d=1#e",
    ]
    assert ingest.text_urls("https://a.b https://a.b") == ["https://a.b"]
    assert ingest.text_urls("без ссылок") == []


async def test_app_text_gets_a_synthetic_negative_message_id(sessionmaker, t0) -> None:
    async with sessionmaker() as session:
        first = await ingest.save_app_text(session, 4242, "купить молоко", t0)
        second = await ingest.save_app_text(session, 4242, "?что купить", t0)
        await session.commit()
    assert first.message_id == -first.id and second.message_id == -second.id
    assert first.source == ingest.APP_SOURCE and first.status == "new" and first.content_type == "text"
    assert first.force_query is False and second.force_query is True
    assert first.tg_date == t0


async def test_telegram_messages_keep_the_telegram_source(add_raw, db) -> None:
    raw = await add_raw("из чата")
    [stored] = await db(select(RawMessage).where(RawMessage.id == raw.id))
    assert stored.source == "telegram"


# --- jobs started from the app ---------------------------------------------------------------------------------------


async def test_edit_time_from_the_app_returns_its_outcome(ctx, make_items, fake_ai, bot, t0) -> None:
    [created] = await make_items({"kind": "task", "title": "Созвон"}, now=t0)
    fake_ai.push(WHEN_FR)
    result = await processor.handle_edit_time(
        ctx, {"item_id": created.item.id, "text": "в пятницу в 18", "now": t0.isoformat(), "app": True}
    )
    assert result == {"ok": True, "reason": None, "message": None, "item_id": created.item.id, "warnings": []}
    assert bot.calls == []


async def test_edit_time_from_the_app_failures(ctx, make_items, fake_ai, bot, t0) -> None:
    [created] = await make_items({"kind": "task", "title": "Созвон"}, now=t0)
    payload = {"item_id": created.item.id, "text": "как-нибудь", "app": True}
    fake_ai.push(AIBadResponse("bad"))
    assert (await processor.handle_edit_time(ctx, payload))["reason"] == "failed"
    assert (await processor.handle_edit_time(ctx, {**payload, "item_id": 999}))["reason"] == "gone"
    assert (await processor.handle_edit_time(ctx, {**payload, "text": " "}))["reason"] == "empty"
    assert bot.calls == []


async def test_edit_time_from_the_chat_still_returns_nothing(ctx, make_items, fake_ai, bot, t0) -> None:
    [created] = await make_items({"kind": "task", "title": "Созвон"}, now=t0)
    fake_ai.push(WHEN_FR)
    assert await processor.handle_edit_time(ctx, {"item_id": created.item.id, "text": "в пятницу в 18"}) is None
    assert len(bot.sent) == 1  # the new card


async def test_split_from_the_app_returns_its_outcome(ctx, make_items, fake_ai, bot, t0) -> None:
    [created] = await make_items({"kind": "task", "title": "Переезд"}, now=t0)
    fake_ai.splits.append(["Коробки", "Машина"])
    result = await processor.handle_split(ctx, {"item_id": created.item.id, "app": True})
    assert result == {"ok": True, "reason": None, "message": None, "steps": 2}
    again = await processor.handle_split(ctx, {"item_id": created.item.id, "app": True})
    assert again["ok"] is False and again["reason"] == "has_steps"
    fake_ai.splits.append(AIBadResponse("bad"))
    [other] = await make_items({"kind": "task", "title": "Ремонт"}, now=t0)
    failed = await processor.handle_split(ctx, {"item_id": other.item.id, "app": True})
    assert failed["reason"] == "failed" and failed["message"] == processor.FAILED_SPLIT_TEXT
    assert (await processor.handle_split(ctx, {"item_id": 999, "app": True}))["reason"] == "gone"
    assert bot.calls == []
