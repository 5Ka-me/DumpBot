"""«Закинуть в свалку» from the Mini App through the real job handlers (FakeAI for the model): the raw message, the
items, a "?" question, a clarifying question and its answer, an edit with candidates and the pick, failures. Nothing
reaches the chat (no cards, reactions or answers there)."""

from __future__ import annotations

from sqlalchemy import select

from dumpbot.ai.client import AIBadResponse, AIUnavailable
from dumpbot.db.models import Item, Job, RawMessage
from dumpbot.services import processor
from dumpbot.services.ingest import APP_SOURCE

MILK = {"intent": "save", "items": [{"kind": "purchase", "title": "Купить молоко"}]}


async def test_inbox_saves_through_the_pipeline(api, fake_ai, run_jobs, db, jobs_all, bot, ctx) -> None:
    ctx.jobs_wakeup.clear()
    reply = await api.post("/api/inbox", {"text": "  купить молоко https://shop.example/milk  "})
    assert reply.status == 202
    status = reply.data
    assert status["pending"] is True and status["status"] == "new" and status["items"] == []
    assert status["text"] == "купить молоко https://shop.example/milk"
    assert status["job"]["kind"] == "message" and status["job"]["status"] == "queued"
    assert ctx.jobs_wakeup.is_set()
    [raw] = await db(select(RawMessage))
    assert raw.source == APP_SOURCE and raw.message_id == -raw.id and raw.chat_id == 4242
    assert raw.urls == ["https://shop.example/milk"] and raw.force_query is False
    [job] = await jobs_all()
    assert job.payload == {"raw_id": raw.id}

    fake_ai.push(MILK)
    await run_jobs()

    status = (await api.get(f"/api/inbox/{raw.id}")).data
    assert status["status"] == "done" and status["pending"] is False
    [row] = status["items"]
    assert row["title"] == "Купить молоко" and row["kind"] == "purchase"
    assert row["urls"] == ["https://shop.example/milk"]
    assert status["note"] is None and status["ask"] is None and status["candidates"] is None
    assert status["answer"] is None and status["edited"] == []
    assert fake_ai.inputs[0].text == "купить молоко https://shop.example/milk"
    assert bot.calls == []  # no card, no reaction: the message is not in the chat
    detail = (await api.get(f"/api/items/{row['id']}")).data
    assert detail["source"]["from_app"] is True and detail["source"]["raw_id"] == raw.id


async def test_question_gets_an_answer_with_rows(api, fake_ai, run_jobs, make_items, bot) -> None:
    [milk] = await make_items({"kind": "purchase", "title": "Купить молоко"})
    raw_id = (await api.post("/api/inbox", {"text": "? что купить"})).data["raw_id"]
    fake_ai.push({"intent": "query", "query": {"text": "купить", "answer": "summary"}})
    fake_ai.answers.append("Молоко")
    await run_jobs()

    assert fake_ai.inputs[0].force_query is True and fake_ai.inputs[0].text == "что купить"
    status = (await api.get(f"/api/inbox/{raw_id}")).data
    assert status["status"] == "done" and status["items"] == []
    assert status["answer"]["text"] == "Молоко" and status["answer"]["title"] == "🔎 По запросу «купить»"
    assert [r["id"] for r in status["answer"]["rows"]] == [milk.item.id]
    assert bot.calls == []


async def test_clarifying_question_and_its_answer(api, fake_ai, run_jobs, jobs_all, bot) -> None:
    raw_id = (await api.post("/api/inbox", {"text": "встреча в 8"})).data["raw_id"]
    fake_ai.push(
        {
            "items": [{"kind": "event", "title": "Встреча", "when": {"text": "в 8", "time": "08:00"}}],
            "ask": {"q": "В 8 утра или вечера?", "options": ["утра", "вечера"]},
        }
    )
    await run_jobs()
    status = (await api.get(f"/api/inbox/{raw_id}")).data
    assert status["ask"] == {"question": "В 8 утра или вечера?", "options": ["утра", "вечера"]}
    assert [r["title"] for r in status["items"]] == ["Встреча"]
    assert bot.calls == []

    assert (await api.post(f"/api/inbox/{raw_id}/answer", {"option": 5})).status == 400
    reply = await api.post(f"/api/inbox/{raw_id}/answer", {"option": 1})
    assert reply.status == 202 and reply.data["pending"] is True and reply.data["ask"] is None
    again = await api.post(f"/api/inbox/{raw_id}/answer", {"option": 0})
    assert (again.status, again.data["error"]["code"]) == (409, "already_answered")

    fake_ai.push({"items": [{"kind": "event", "title": "Встреча", "when": {"text": "в 20", "time": "20:00"}}]})
    await run_jobs()
    assert fake_ai.inputs[-1].clarification == "вечера"
    status = (await api.get(f"/api/inbox/{raw_id}")).data
    assert status["pending"] is False and status["ask"] is None
    [row] = status["items"]
    assert row["when_text"] == "сегодня 20:00"
    assert [j.kind for j in await jobs_all()] == ["message", "clarify"]
    assert bot.calls == []


async def test_edit_with_candidates_and_the_pick(api, fake_ai, run_jobs, make_items, bot) -> None:
    first, second = await make_items(
        {"kind": "event", "title": "Встреча с Олегом", "when": {"text": "завтра в 13", "day": "+1", "time": "13:00"}},
        {"kind": "event", "title": "Встреча с Олегом по проекту"},
    )
    raw_id = (await api.post("/api/inbox", {"text": "перенеси встречу с Олегом на пятницу"})).data["raw_id"]
    edit = {"target": "встреча с Олегом", "when": {"text": "на пятницу", "day": "FR"}}
    fake_ai.push({"intent": "edit", "edit": edit})
    await run_jobs()

    status = (await api.get(f"/api/inbox/{raw_id}")).data
    assert {r["id"] for r in status["candidates"]} == {first.item.id, second.item.id}
    assert status["items"] == [] and status["status"] == "done"

    assert (await api.post(f"/api/inbox/{raw_id}/pick", {"item_id": 999})).data["error"]["code"] == "not_listed"
    reply = await api.post(f"/api/inbox/{raw_id}/pick", {"item_id": first.item.id})
    assert reply.status == 202 and reply.data["candidates"] is None and reply.data["pending"] is True

    fake_ai.push({"intent": "edit", "edit": {"when": {"text": "на пятницу", "day": "FR"}}})
    await run_jobs()
    status = (await api.get(f"/api/inbox/{raw_id}")).data
    assert status["pending"] is False
    [edited] = status["edited"]
    assert edited["id"] == first.item.id and edited["when_text"] == "пт 9 окт 10:00"
    assert bot.calls == []


async def test_edit_of_a_single_exact_match_is_applied(api, fake_ai, run_jobs, make_items, bot) -> None:
    [created] = await make_items({"kind": "task", "title": "Стрижка"})
    raw_id = (await api.post("/api/inbox", {"text": "перенеси стрижку на завтра 18:00"})).data["raw_id"]
    edit = {"target": "Стрижка", "when": {"text": "завтра 18:00", "day": "+1", "time": "18:00"}}
    fake_ai.push({"intent": "edit", "edit": edit})
    await run_jobs()
    status = (await api.get(f"/api/inbox/{raw_id}")).data
    assert [r["id"] for r in status["edited"]] == [created.item.id]
    assert status["edited"][0]["when_text"] == "завтра 18:00"
    assert bot.calls == []


async def test_edit_that_finds_nothing_is_kept_as_a_note(api, fake_ai, run_jobs, bot) -> None:
    raw_id = (await api.post("/api/inbox", {"text": "удали приложение банка"})).data["raw_id"]
    fake_ai.push({"intent": "edit", "edit": {"target": "приложение банка", "action": "delete"}})
    await run_jobs()
    status = (await api.get(f"/api/inbox/{raw_id}")).data
    assert status["note"] == processor.NOT_FOUND_TEXT
    assert [r["title"] for r in status["items"]] == ["удали приложение банка"]
    assert bot.calls == []


async def test_ai_unavailable_defers_quietly(api, fake_ai, run_jobs, bot) -> None:
    raw_id = (await api.post("/api/inbox", {"text": "купить молоко"})).data["raw_id"]
    fake_ai.push(AIUnavailable("down"))
    await run_jobs()
    status = (await api.get(f"/api/inbox/{raw_id}")).data
    assert status["status"] == "deferred" and status["pending"] is True
    assert status["job"]["reason"] == "ai_unavailable" and status["job"]["retry_at"]
    assert bot.calls == []  # no ✍ reaction: the message is not in the chat


async def test_exhausted_budget_defers(api, run_jobs, bot) -> None:
    assert (await api.patch("/api/settings", {"budget_usd": 0})).status == 200
    raw_id = (await api.post("/api/inbox", {"text": "купить молоко"})).data["raw_id"]
    await run_jobs()
    status = (await api.get(f"/api/inbox/{raw_id}")).data
    assert status["status"] == "deferred" and status["job"]["reason"] == "budget"
    # the owner is told about the budget once, in the chat (as for any message), without a reaction
    assert len(bot.texts) == 1 and bot.texts[0].startswith("⛔ ИИ выключен")
    assert bot.calls_of("set_message_reaction") == []


async def test_unusable_ai_answer_keeps_the_text_as_a_note(api, fake_ai, run_jobs, bot) -> None:
    raw_id = (await api.post("/api/inbox", {"text": "что-то непонятное"})).data["raw_id"]
    fake_ai.push(AIBadResponse("nonsense"))
    await run_jobs()
    status = (await api.get(f"/api/inbox/{raw_id}")).data
    assert status["status"] == "done" and status["note"] == processor.FAILED_MESSAGE_TEXT
    assert [r["title"] for r in status["items"]] == ["что-то непонятное"]
    assert bot.calls == []


async def test_dead_job_of_an_app_message(api, ctx, db, bot) -> None:
    raw_id = (await api.post("/api/inbox", {"text": "купить молоко"})).data["raw_id"]
    [job] = await db(select(Job))
    job.last_error = "boom"
    await processor.on_dead(ctx, job)
    status = (await api.get(f"/api/inbox/{raw_id}")).data
    assert status["status"] == "failed"
    [sent] = bot.calls_of("send_message")
    assert sent["reply_parameters"] is None  # nothing in the chat to reply to
    assert bot.calls_of("set_message_reaction") == []


async def test_dead_app_job_without_a_message_is_not_announced(api, ctx, make_items, db, bot) -> None:
    [created] = await make_items({"kind": "task", "title": "Переезд"})
    await api.post(f"/api/items/{created.item.id}/split")
    [job] = await db(select(Job))
    await processor.on_dead(ctx, job)
    assert bot.calls == []


async def test_inbox_validation(api, add_raw) -> None:
    for body in [{}, {"text": "  "}, {"text": 5}, {"text": "x" * 4097}, {"text": "a\u0000b"}]:
        reply = await api.post("/api/inbox", body)
        assert reply.status == 400 and reply.data["error"]["field"] == "text"
    telegram = await add_raw(message_id=55, text="из чата")
    assert (await api.get(f"/api/inbox/{telegram.id}")).status == 404
    assert (await api.get("/api/inbox/99999")).status == 404
    assert (await api.post(f"/api/inbox/{telegram.id}/answer", {"option": 0})).status == 404


async def test_inbox_items_belong_to_the_app_message(api, fake_ai, run_jobs, db) -> None:
    raw_id = (await api.post("/api/inbox", {"text": "переезд, распиши по шагам"})).data["raw_id"]
    fake_ai.push(
        {"items": [{"kind": "task", "title": "Переезд", "steps": [{"title": "Коробки"}, {"title": "Машина"}]}]}
    )
    await run_jobs()
    status = (await api.get(f"/api/inbox/{raw_id}")).data
    [row] = status["items"]  # the steps are in the parent's card, not separate rows
    assert row["steps_total"] == 2
    steps = await db(select(Item).where(Item.parent_id == row["id"]))
    assert {s.raw_message_id for s in steps} == {raw_id}
