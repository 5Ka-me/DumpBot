"""Settings (GET / PATCH with the bot's validation) and a question to the base (POST /api/ask)."""

from __future__ import annotations

from sqlalchemy import select

from dumpbot.ai.client import AIBadResponse, AIConfigError, AIUnavailable
from dumpbot.db.models import Job, RawMessage
from dumpbot.domain.prefs import Prefs


async def test_get_settings_lists_every_preference(api) -> None:
    data = (await api.get("/api/settings")).data
    assert set(data["prefs"]) == set(Prefs.model_fields)
    prefs = data["prefs"]
    assert prefs["tz"] == "Europe/Warsaw" and prefs["slot_lunch"] == "13:00" and prefs["quiet_start"] == "23:00"
    assert prefs["lead_minutes"] == 5 and prefs["budget_usd"] == 5.0 and prefs["stt_language"] == "ru"
    assert prefs["stt_mode"] == "auto" and prefs["quiet_enabled"] is True
    assert data["local_stt"] is False


async def test_patch_settings(api, ctx) -> None:
    ctx.scheduler_wakeup.clear()
    body = {
        "tz": "Europe/Berlin",
        "slot_lunch": "12.30",
        "task_default_time": "8:05",
        "lead_minutes": 15,
        "quiet_enabled": False,
        "budget_usd": 7.567,
        "stt_language": "en",
        "stt_mode": "local",
        "soon_days": 3,
    }
    reply = await api.patch("/api/settings", body)
    assert reply.status == 200, reply.data
    prefs = reply.data["prefs"]
    assert prefs["tz"] == "Europe/Berlin" and prefs["slot_lunch"] == "12:30" and prefs["task_default_time"] == "08:05"
    assert prefs["lead_minutes"] == 15 and prefs["quiet_enabled"] is False and prefs["budget_usd"] == 7.57
    assert prefs["stt_language"] == "en" and prefs["stt_mode"] == "local" and prefs["soon_days"] == 3
    assert ctx.scheduler_wakeup.is_set()
    assert (await api.get("/api/settings")).data["prefs"] == prefs
    assert (await api.patch("/api/settings", {"stt_language": None})).data["prefs"]["stt_language"] is None


async def test_patch_settings_validation(api) -> None:
    for body, field in [
        ({"tz": "Mars/Base"}, "tz"),
        ({"slot_lunch": "25:00"}, "slot_lunch"),
        ({"slot_lunch": 1300}, "slot_lunch"),
        ({"lead_minutes": 10081}, "lead_minutes"),
        ({"lead_minutes": "5"}, "lead_minutes"),
        ({"lead_minutes": True}, "lead_minutes"),
        ({"budget_usd": -1}, "budget_usd"),
        ({"budget_usd": "5"}, "budget_usd"),
        ({"quiet_enabled": "yes"}, "quiet_enabled"),
        ({"stt_mode": "cloud"}, "stt_mode"),
        ({"soon_days": 0}, "soon_days"),
        ({"colour": "red"}, "colour"),
    ]:
        reply = await api.patch("/api/settings", body)
        assert reply.status == 400, body
        assert reply.data["error"]["field"] == field, body
    assert (await api.patch("/api/settings", {})).status == 400
    # nothing half-saved
    reply = await api.patch("/api/settings", {"lead_minutes": 30, "tz": "Mars/Base"})
    assert reply.status == 400
    assert (await api.get("/api/settings")).data["prefs"]["lead_minutes"] == 5


async def test_raising_the_budget_releases_deferred_jobs(api, sessionmaker, frozen, ctx) -> None:
    await api.patch("/api/settings", {"budget_usd": 0})
    async with sessionmaker() as session:
        session.add(
            Job(kind="edit_time", payload={"item_id": 1}, status="queued", last_error="budget exhausted",
                run_after=frozen.now.replace(year=2027))
        )  # fmt: skip
        await session.commit()
    ctx.jobs_wakeup.clear()
    await api.patch("/api/settings", {"budget_usd": 3})
    async with sessionmaker() as session:
        job = await session.scalar(select(Job))
    assert job.run_after == frozen.now
    assert ctx.jobs_wakeup.is_set()


# --- ask -----------------------------------------------------------------------------------------------------------


async def test_ask_returns_rows_and_a_prose_answer(api, fake_ai, make_items, db) -> None:
    [milk] = await make_items({"kind": "purchase", "title": "Купить молоко"})
    fake_ai.push({"intent": "query", "query": {"text": "купить", "answer": "summary"}})
    fake_ai.answers.append("Надо купить молоко.")
    reply = await api.post("/api/ask", {"question": "?что купить"})
    assert reply.status == 200, reply.data
    assert reply.data["answer"] == "Надо купить молоко." and reply.data["title"] == "🔎 По запросу «купить»"
    assert [r["id"] for r in reply.data["rows"]] == [milk.item.id]
    assert reply.data["query"]["text"] == "купить" and reply.data["query"]["answer"] == "summary"
    [inp] = fake_ai.inputs
    assert inp.force_query is True and inp.text == "что купить"
    assert await db(select(RawMessage)) == []  # a question is not saved


async def test_ask_list_answer(api, fake_ai, make_items) -> None:
    await make_items({"kind": "idea", "title": "Отпуск в Грузии"})
    fake_ai.push({"intent": "query", "query": {"kinds": ["idea"]}})
    data = (await api.post("/api/ask", {"question": "какие идеи?"})).data
    assert data["answer"] is None and data["title"] == "🔎 Вот что нашёл" and len(data["rows"]) == 1


async def test_ask_when_the_prose_answer_fails_still_gives_rows(api, fake_ai, make_items) -> None:
    await make_items({"kind": "idea", "title": "Отпуск"})
    fake_ai.push({"intent": "query", "query": {"answer": "summary"}})
    fake_ai.answers.append(AIUnavailable("down"))
    data = (await api.post("/api/ask", {"question": "что есть?"})).data
    assert data["answer"] is None and len(data["rows"]) == 1


async def test_ask_errors(api, fake_ai) -> None:
    for body in [{}, {"question": " "}, {"question": "x" * 1001}]:
        assert (await api.post("/api/ask", body)).status == 400
    for error, status, code in [
        (AIUnavailable("down"), 503, "ai_unavailable"),
        (AIConfigError("401"), 502, "ai_config"),
        (AIBadResponse("bad"), 502, "ai_failed"),
    ]:
        fake_ai.push(error)
        reply = await api.post("/api/ask", {"question": "что завтра?"})
        assert (reply.status, reply.data["error"]["code"]) == (status, code)


async def test_ask_with_an_exhausted_budget(api, fake_ai, bot) -> None:
    await api.patch("/api/settings", {"budget_usd": 0})
    reply = await api.post("/api/ask", {"question": "что завтра?"})
    assert (reply.status, reply.data["error"]["code"]) == (402, "budget_exhausted")
    assert fake_ai.inputs == []
