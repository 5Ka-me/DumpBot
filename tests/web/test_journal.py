"""The Mini App's API and the events journal (docs/features/iteration-4.md §5): every authorized ``/api`` call is a
``miniapp`` event, written after the answer went out; polls, pictures, the frontend and refused calls are not."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sqlalchemy import select

from dumpbot.db.models import Event
from dumpbot.services import events


async def _wait_events(db, count: int, limit_s: float = 3.0) -> list[Event]:
    """The journal is written right after the answer: wait for it."""
    deadline = time.monotonic() + limit_s
    while True:
        rows = await db(select(Event).order_by(Event.id))
        if len(rows) >= count or time.monotonic() > deadline:
            return rows
        await asyncio.sleep(0.02)


def _summary(rows: list[Event]) -> list[tuple[Any, ...]]:
    return [(e.type, e.source, e.item_id, e.data["route"], e.data["status"]) for e in rows]


async def test_calls_are_journaled_with_route_and_item(api, make_items, db) -> None:
    [created] = await make_items({"kind": "task", "title": "Купить хлеб"})
    item_id = created.item.id

    assert (await api.get("/api/today")).status == 200
    assert (await api.get(f"/api/items/{item_id}")).status == 200
    assert (await api.get("/api/items/999999")).status == 404

    rows = await _wait_events(db, 3)
    assert _summary(rows) == [
        ("miniapp", "miniapp", None, "GET /api/today", 200),
        ("miniapp", "miniapp", item_id, "GET /api/items/{id}", 200),
        ("miniapp", "miniapp", 999999, "GET /api/items/{id}", 404),
    ]


async def test_polls_pictures_the_frontend_and_refused_calls_are_not_journaled(api, db) -> None:
    assert (await api.get("/api/jobs/1")).status == 404  # the app polls a job it started
    assert (await api.get("/api/inbox/1")).status == 404  # … and a message it sent
    assert (await api.get("/api/files/1/0", raw=True)).status == 404
    assert (await api.get("/", raw=True)).status == 200  # the frontend
    assert (await api.get("/api/today", auth_header="tma nonsense")).status == 401
    assert (await api.get("/api/nowhere")).status == 404  # no such route
    await asyncio.sleep(0.2)
    assert await db(select(Event)) == []


async def test_a_failed_journal_write_breaks_no_call(api, db, monkeypatch, caplog) -> None:
    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("no events table")

    monkeypatch.setattr(events, "_insert", broken)
    reply = await api.get("/api/today")
    assert reply.status == 200 and "open" in reply.data
    await asyncio.sleep(0.2)
    assert "could not write 1 journal event(s) (miniapp): RuntimeError" in caplog.text
