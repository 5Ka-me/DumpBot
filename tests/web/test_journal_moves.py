"""The Mini App's moves in the events journal (docs/features/iteration-4.md §4): a new time, a snooze to tomorrow and a
new span are marked ``"move": true``; «📅 Перенести невыполненное на завтра» journals each moved item; the weekly
review's settings (§6) are part of ``/api/settings``."""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import select

from dumpbot.db.models import Event

TODAY_12 = {"text": "сегодня в 12", "time": "12:00"}
TOMORROW_12 = {"text": "завтра в 12", "day": "+1", "time": "12:00"}


async def _wait_events(db, count: int, limit_s: float = 3.0) -> list[Event]:
    deadline = time.monotonic() + limit_s
    while True:
        rows = await db(select(Event).order_by(Event.id))
        if len(rows) >= count or time.monotonic() > deadline:
            return rows
        await asyncio.sleep(0.02)


async def test_moves_are_marked(api, make_items, db) -> None:
    [created] = await make_items({"kind": "task", "title": "Отчёт", "when": TOMORROW_12})
    item_id = created.item.id
    calls = [
        ("post", f"/api/items/{item_id}/when", {"due_local": "2026-10-09T15:00"}, True),
        ("post", f"/api/items/{item_id}/snooze", {"minutes": 60}, False),
        ("post", f"/api/items/{item_id}/snooze", {"tomorrow": True}, True),
        ("patch", f"/api/items/{item_id}", {"title": "Квартальный отчёт"}, False),
        ("patch", f"/api/items/{item_id}", {"period": "next_week"}, True),
        ("patch", f"/api/items/{item_id}", {"period": "someday"}, False),
    ]
    for method, path, body, _ in calls:
        reply = await getattr(api, method)(path, body)
        assert reply.status == 200, (path, body, reply.data)

    rows = await _wait_events(db, len(calls))
    assert [(e.item_id, e.data.get("move", False)) for e in rows] == [(item_id, move) for *_, move in calls]


async def test_a_refused_move_is_no_move(api, make_items, db) -> None:
    [created] = await make_items(
        {"kind": "idea", "title": "Испанский", "when": {"text": "когда-нибудь", "period": "someday"}}
    )
    reply = await api.post(f"/api/items/{created.item.id}/snooze", {"tomorrow": True})
    assert reply.status == 409
    [row] = await _wait_events(db, 1)
    assert row.data == {"route": "POST /api/items/{id}/snooze", "status": 409}


async def test_the_bulk_move_journals_each_item(api, make_items, db, frozen) -> None:
    first, second = await make_items(
        {"kind": "task", "title": "Отчёт", "when": TODAY_12},
        {"kind": "task", "title": "Звонок", "when": {"text": "сегодня в 13", "time": "13:00"}},
    )
    frozen.advance(hours=5)

    assert (await api.post("/api/today/move", {})).data["moved"] == 2

    rows = await _wait_events(db, 3)
    assert rows[0].type == "miniapp" and rows[0].data == {"route": "POST /api/today/move", "status": 200}
    assert sorted((e.type, e.source, e.item_id, e.occurrence_id, e.data["move"]) for e in rows[1:]) == sorted(
        [
            ("edit", "miniapp", first.item.id, first.occurrence.id, True),
            ("edit", "miniapp", second.item.id, second.occurrence.id, True),
        ]
    )


async def test_settings_have_the_weekly_review(api) -> None:
    prefs = (await api.get("/api/settings")).data["prefs"]
    assert (prefs["digest_weekly_enabled"], prefs["digest_weekly_time"]) == (True, "19:00")

    reply = await api.patch("/api/settings", {"digest_weekly_enabled": False, "digest_weekly_time": "18:30"})
    assert reply.status == 200
    assert (reply.data["prefs"]["digest_weekly_enabled"], reply.data["prefs"]["digest_weekly_time"]) == (False, "18:30")
    bad = await api.patch("/api/settings", {"digest_weekly_time": "25:00"})
    assert (bad.status, bad.data["error"]["field"]) == (400, "digest_weekly_time")
