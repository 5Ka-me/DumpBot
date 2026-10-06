"""Read endpoints of the Mini App API against the DB: me, feed (filters and cursor), today, tasks, someday,
recurring, week, usage, jobs."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from dumpbot.ai.schemas import EditPatch
from dumpbot.db.models import AiUsage, Job
from dumpbot.domain.prefs import Prefs
from dumpbot.services import items
from dumpbot.web.api_lists import encode_cursor

TOMORROW_13 = {"text": "завтра в 13", "day": "+1", "time": "13:00"}
TODAY_15 = {"text": "сегодня в 15", "day": "+0", "time": "15:00"}


async def test_me(api) -> None:
    reply = await api.get("/api/me")
    assert reply.status == 200
    data = reply.data
    assert data["tz"] == "Europe/Warsaw"
    assert data["now"] == "2026-10-06T08:00:00Z" and data["today"] == "2026-10-06"
    assert {"kind": "task", "label": "Задача", "emoji": "📌"} in data["kinds"]
    assert len(data["kinds"]) == 9
    assert data["local_stt"] is False
    assert reply.headers["Cache-Control"] == "no-store"
    assert reply.headers["X-Content-Type-Options"] == "nosniff"


async def test_today(api, make_items) -> None:
    [today] = await make_items({"kind": "task", "title": "Созвон", "when": TODAY_15})
    [week] = await make_items({"kind": "task", "title": "Посмотреть Raspberry Pi", "when": {"period": "this_week"}})
    await make_items({"kind": "task", "title": "Завтрашнее", "when": TOMORROW_13})

    data = (await api.get("/api/today")).data

    assert data["day"] == "2026-10-06"
    [row] = data["open"]
    assert row["id"] == today.item.id and row["title"] == "Созвон"
    assert row["due_at"] == "2026-10-06T13:00:00Z" and row["when_text"] == "сегодня 15:00"
    assert row["kind"] == "task" and row["status"] == "open" and row["occ_status"] == "open"
    assert row["images"] == 0 and row["period"] is None and row["someday"] is False
    [period_row] = data["period_items"]
    assert period_row["id"] == week.item.id
    assert period_row["period"] == {
        "kind": "week",
        "start": "2026-10-05",
        "end": "2026-10-11",
        "label": "на этой неделе",
    }
    assert data["overdue"] == [] and data["done"] == []


async def test_tasks_someday_recurring(api, make_items) -> None:
    [task] = await make_items({"kind": "task", "title": "Купить молоко"})
    [idea] = await make_items({"kind": "idea", "title": "Выучить испанский", "when": {"period": "someday"}})
    [series] = await make_items(
        {"kind": "task", "title": "Планёрка", "when": {"text": "в 9", "time": "09:00"}, "rrule": "FREQ=WEEKLY;BYDAY=MO"}
    )

    tasks = (await api.get("/api/tasks")).data["rows"]
    assert {r["id"] for r in tasks} == {task.item.id, series.item.id}
    someday = (await api.get("/api/someday")).data["rows"]
    assert [r["id"] for r in someday] == [idea.item.id]
    assert someday[0]["someday"] is True and someday[0]["when_text"] == "когда-нибудь"
    recurring = (await api.get("/api/recurring")).data["rows"]
    assert [r["id"] for r in recurring] == [series.item.id]
    assert recurring[0]["rrule"] == "FREQ=WEEKLY;BYDAY=MO" and recurring[0]["repeat_text"] == "каждый понедельник"
    assert (await api.get("/api/tasks?limit=0")).status == 400


async def test_week_has_seven_days(api, make_items) -> None:
    await make_items({"kind": "event", "title": "Стрижка", "when": {"text": "в пятницу", "day": "FR", "time": "18:00"}})
    days = (await api.get("/api/week")).data["days"]
    assert [d["day"] for d in days] == [f"2026-10-{d:02d}" for d in range(6, 13)]
    friday = days[3]
    assert [r["title"] for r in friday["open"]] == ["Стрижка"]
    assert all(d["overdue"] == [] for d in days)


async def test_feed_pages_with_a_cursor(api, make_items, frozen) -> None:
    created = []
    for n in range(5):
        created += await make_items({"kind": "note", "title": f"Заметка {n}"}, now=frozen.now - timedelta(hours=5 - n))

    first = (await api.get("/api/feed?limit=2")).data
    assert [r["title"] for r in first["rows"]] == ["Заметка 4", "Заметка 3"]
    assert first["next_cursor"]
    second = (await api.get(f"/api/feed?limit=2&cursor={first['next_cursor']}")).data
    assert [r["title"] for r in second["rows"]] == ["Заметка 2", "Заметка 1"]
    third = (await api.get(f"/api/feed?limit=2&cursor={second['next_cursor']}")).data
    assert [r["title"] for r in third["rows"]] == ["Заметка 0"]
    assert third["next_cursor"] is None


async def test_feed_cursor_keeps_items_saved_at_the_same_moment(api, make_items) -> None:
    created = await make_items(*({"kind": "note", "title": f"Одновременно {n}"} for n in range(3)))
    ids = sorted((c.item.id for c in created), reverse=True)
    first = (await api.get("/api/feed?limit=2")).data
    second = (await api.get(f"/api/feed?limit=2&cursor={first['next_cursor']}")).data
    assert [r["id"] for r in first["rows"] + second["rows"]] == ids


async def test_feed_filters(api, make_items, frozen, sessionmaker) -> None:
    [headphones] = await make_items(
        {"kind": "purchase", "title": "Купить наушники Sony", "tags": ["электроника"]},
        now=frozen.now - timedelta(days=3),
    )
    [idea] = await make_items({"kind": "idea", "title": "Подкаст про наушники", "tags": ["идеи"]})
    [gone] = await make_items({"kind": "note", "title": "Удалённая заметка"})
    async with sessionmaker() as session:
        await items.delete_item(session, gone.item.id, frozen.now)
        await items.apply_patch(session, idea.item.id, EditPatch(action="done"), Prefs(), frozen.now)
        await session.commit()

    async def ids(query: str) -> list[int]:
        reply = await api.get(f"/api/feed?{query}")
        assert reply.status == 200, reply.data
        return [r["id"] for r in reply.data["rows"]]

    assert await ids("") == [idea.item.id, headphones.item.id]
    assert await ids("q=наушники") == [idea.item.id, headphones.item.id]
    assert await ids("q=sony") == [headphones.item.id]
    assert await ids("kinds=purchase,task") == [headphones.item.id]
    assert await ids("tags=%23Электроника") == [headphones.item.id]
    assert await ids("status=done") == [idea.item.id]
    assert await ids("status=open") == [headphones.item.id]
    assert await ids("status=deleted") == [gone.item.id]
    assert await ids("from=2026-10-04&to=2026-10-05") == []
    assert await ids("from=2026-10-03&to=2026-10-03") == [headphones.item.id]
    assert await ids("from=2026-10-06") == [idea.item.id]


async def test_feed_rejects_bad_filters(api) -> None:
    for query, field in [
        ("kinds=car", "kinds"),
        ("status=lost", "status"),
        ("from=06.10.2026", "from"),
        ("cursor=@@@", "cursor"),
        ("limit=500", "limit"),
        ("q=" + "x" * 201, "q"),
    ]:
        reply = await api.get(f"/api/feed?{query}")
        assert reply.status == 400, query
        assert reply.data["error"] == {"code": "bad_request", "message": reply.data["error"]["message"], "field": field}


def test_cursor_is_opaque_text() -> None:
    from datetime import UTC, datetime

    cursor = encode_cursor(datetime(2026, 10, 6, 8, 0, 0, 123456, tzinfo=UTC), 42)
    assert "|" not in cursor and "=" not in cursor


async def test_usage(api, sessionmaker, frozen) -> None:
    async with sessionmaker() as session:
        session.add_all(
            [
                AiUsage(created_at=frozen.now, purpose="extract", model="m", cost_usd=Decimal("0.5")),
                AiUsage(created_at=frozen.now, purpose="extract", model="m", cost_usd=Decimal("0.25")),
                AiUsage(created_at=frozen.now, purpose="stt", model="local:small", cost_usd=Decimal("0")),
            ]
        )
        await session.commit()
    data = (await api.get("/api/usage")).data
    assert data["month"] == "октябрь 2026"
    assert data["spent_usd"] == 0.75 and data["limit_usd"] == 5.0 and data["ratio"] == 0.15
    assert data["exhausted"] is False and data["calls"] == 3
    assert data["by_purpose"] == [
        {"purpose": "extract", "label": "разбор сообщений", "calls": 2, "usd": 0.75},
        {"purpose": "stt", "label": "голосовые", "calls": 1, "usd": 0.0},
    ]


async def test_job_status(api, sessionmaker) -> None:
    async with sessionmaker() as session:
        job = Job(kind="split", payload={"item_id": 1, "app": True}, status="queued", last_error="budget exhausted")
        done = Job(kind="edit_time", payload={"item_id": 1}, status="done", result={"ok": True, "item_id": 1})
        session.add_all([job, done])
        await session.commit()
    queued = (await api.get(f"/api/jobs/{job.id}")).data
    assert queued["status"] == "queued" and queued["reason"] == "budget" and queued["retry_at"]
    finished = (await api.get(f"/api/jobs/{done.id}")).data
    assert finished == {
        "id": done.id,
        "kind": "edit_time",
        "status": "done",
        "retry_at": None,
        "reason": None,
        "result": {"ok": True, "item_id": 1},
    }
    assert (await api.get("/api/jobs/999")).status == 404
    assert (await api.get("/api/jobs/abc")).status == 404
