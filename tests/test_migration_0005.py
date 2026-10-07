"""Migration 0005 (an item's time is its notification time, 2026-10-07) on a scratch database seeded at 0004.

Runs ``alembic upgrade`` for real against a database of its own (``<test db>_mig0005``, created and dropped here) on
the test Postgres of ``TEST_DATABASE_URL``; skipped without it.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]


def T(day: int, hour: int, minute: int = 0) -> datetime:
    """October 2026, UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


@pytest.fixture
def scratch_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    parsed = make_url(url)
    if parsed.host == "localhost":
        parsed = parsed.set(host="127.0.0.1")
    name = f"{parsed.database}_mig0005"
    admin = parsed.render_as_string(hide_password=False)
    asyncio.run(_admin(admin, f'DROP DATABASE IF EXISTS "{name}"', f'CREATE DATABASE "{name}"'))
    try:
        yield parsed.set(database=name).render_as_string(hide_password=False)
    finally:
        asyncio.run(_admin(admin, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


async def _admin(url: str, *statements: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


def _alembic(url: str) -> Config:
    """No alembic.ini: its logging setup would reconfigure the test run's loggers."""
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


async def _run(url: str, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params or {})
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _seed(url: str) -> dict[str, int]:
    """Rows as the old code left them (default lead 5 minutes, explicit leads before the time)."""
    ids: dict[str, int] = {}
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:

            async def item(
                key: str, *, leads: list[int] | None = None, role: str = "event", series: bool = False
            ) -> int:
                row = await conn.execute(
                    text(
                        "INSERT INTO items (kind, title, when_spec, lead_minutes, rrule, rec_active) "
                        "VALUES ('event', :title, CAST(:spec AS jsonb), :leads, :rrule, :series) RETURNING id"
                    ),
                    {
                        "title": key,
                        "spec": json.dumps({"text": key, "role": role}),
                        "leads": leads,
                        "rrule": "FREQ=DAILY" if series else None,
                        "series": series,
                    },
                )
                ids[key] = row.scalar_one()
                return ids[key]

            async def occ(key: str, item_id: int, due: datetime, precision: str = "exact", status: str = "open") -> int:
                row = await conn.execute(
                    text(
                        "INSERT INTO occurrences (item_id, due_at, precision, status) "
                        "VALUES (:item, :due, :precision, :status) RETURNING id"
                    ),
                    {"item": item_id, "due": due, "precision": precision, "status": status},
                )
                ids[key] = row.scalar_one()
                return ids[key]

            async def rem(
                occ_id: int, fire: datetime, lead: int, *, auto: bool = False, status: str = "pending"
            ) -> None:
                await conn.execute(
                    text(
                        "INSERT INTO reminders (occurrence_id, fire_at, lead_minutes, auto, status) "
                        "VALUES (:occ, :fire, :lead, :auto, :status)"
                    ),
                    {"occ": occ_id, "fire": fire, "lead": lead, "auto": auto, "status": status},
                )

            # A: "в обед" -> 13:00, the default lead planned 12:55
            a = await occ("occ_a", await item("lunch"), T(7, 11))
            await rem(a, T(7, 10, 55), 5)
            # B: the same, and a snooze already waiting at the time itself: one pending reminder is left
            b = await occ("occ_b", await item("snoozed"), T(7, 11))
            await rem(b, T(7, 10, 55), 5)
            await rem(b, T(7, 11), 0)
            # C: a deadline with a time: the evening before stays, 14:55 -> 15:00
            c = await occ("occ_c", await item("deadline", role="deadline"), T(9, 13))
            await rem(c, T(8, 17), 1200, auto=True)
            await rem(c, T(9, 12, 55), 5)
            # D: "за день и за час до встречи в пт 15:00": due at the first notification, both kept
            d = await occ("occ_d", await item("two leads", leads=[1440, 60]), T(9, 13))
            await rem(d, T(8, 13), 1440)
            await rem(d, T(9, 12), 60)
            # E: the same, "за день" already sent: due at "за час", only that lead is left
            e = await occ("occ_e", await item("one left", leads=[1440, 60]), T(9, 13))
            await rem(e, T(8, 13), 1440, status="sent")
            await rem(e, T(9, 12), 60)
            # F: a series "в 10, за 15 минут": its next instance is due 09:45, the series keeps the lead
            f = await item("series", leads=[15], series=True)
            await occ("occ_f1", f, T(7, 8), status="done")
            f2 = await occ("occ_f2", f, T(8, 8))
            await rem(f2, T(8, 7, 45), 15)
            # G: a series "за день" whose earlier instance sits where the next one would move: due kept
            g = await item("clash", leads=[1440], series=True)
            await occ("occ_g1", g, T(8, 8))
            g2 = await occ("occ_g2", g, T(9, 8))
            await rem(g2, T(8, 8), 1440)
            # H: a span's first-morning reminder (automatic, before the time) and a done occurrence: untouched
            h = await occ("occ_h", await item("span"), T(16, 17), precision="period")
            await rem(h, T(12, 8), 5820, auto=True)
            await rem(h, T(16, 17), 0, auto=True)
            done = await occ("occ_done", await item("done"), T(6, 11), status="done")
            await rem(done, T(6, 10, 55), 5, status="cancelled")

            prefs = {"tz": "Europe/Warsaw", "lead_minutes": 5, "slot_lunch": "12:30:00"}
            await conn.execute(
                text("INSERT INTO user_settings (owner_id, prefs) VALUES (42, CAST(:p AS jsonb))"),
                {"p": json.dumps(prefs)},
            )
    finally:
        await engine.dispose()
    return ids


async def _state(url: str) -> dict[str, Any]:
    occs = await _run(url, "SELECT id, due_at FROM occurrences")
    rems = await _run(
        url,
        "SELECT occurrence_id, fire_at, lead_minutes, auto, status FROM reminders ORDER BY occurrence_id, fire_at, id",
    )
    items = await _run(url, "SELECT title, lead_minutes FROM items")
    [prefs] = await _run(url, "SELECT prefs FROM user_settings")
    by_occ: dict[int, list[tuple[datetime, int, bool, str]]] = {}
    for occ_id, fire, lead, auto, status in rems:
        by_occ.setdefault(occ_id, []).append((fire, lead, auto, status))
    return {
        "due": dict(occs),
        "reminders": by_occ,
        "leads": {title: leads for title, leads in items},
        "prefs": prefs[0],
    }


def test_upgrade_moves_default_leads_and_makes_explicit_leads_the_items_time(scratch_url: str) -> None:
    cfg = _alembic(scratch_url)
    command.upgrade(cfg, "0004")
    ids = asyncio.run(_seed(scratch_url))

    command.upgrade(cfg, "head")
    state = asyncio.run(_state(scratch_url))
    due, rems, leads = state["due"], state["reminders"], state["leads"]

    # A: 12:55 -> 13:00 (11:00 UTC), lead 0
    assert due[ids["occ_a"]] == T(7, 11)
    assert rems[ids["occ_a"]] == [(T(7, 11), 0, False, "pending")]
    # B: deduplicated: one pending reminder at the time
    assert [r for r in rems[ids["occ_b"]] if r[3] == "pending"] == [(T(7, 11), 0, False, "pending")]
    assert sorted(r[3] for r in rems[ids["occ_b"]]) == ["cancelled", "pending"]
    # C: the evening before stays automatic and early; the deadline's own reminder at its time
    assert due[ids["occ_c"]] == T(9, 13)
    assert rems[ids["occ_c"]] == [(T(8, 17), 1200, True, "pending"), (T(9, 13), 0, False, "pending")]
    # D: due at the first notification, both notifications kept, the leads stay on the item
    assert due[ids["occ_d"]] == T(8, 13)
    assert rems[ids["occ_d"]] == [(T(8, 13), 0, False, "pending"), (T(9, 12), 0, False, "pending")]
    assert leads["two leads"] == [1440, 60]
    # E: only "за час" is left: it is the item's time and its only lead
    assert due[ids["occ_e"]] == T(9, 12)
    assert [r[3] for r in rems[ids["occ_e"]]] == ["sent", "pending"]
    assert leads["one left"] == [60]
    # F: the next instance of the series is due at its notification; the series keeps its lead
    assert (due[ids["occ_f1"]], due[ids["occ_f2"]]) == (T(7, 8), T(8, 7, 45))
    assert rems[ids["occ_f2"]] == [(T(8, 7, 45), 0, False, "pending")]
    assert leads["series"] == [15]
    # G: another instance is due right there: this one keeps its due, its reminder stays as it was planned
    assert (due[ids["occ_g1"]], due[ids["occ_g2"]]) == (T(8, 8), T(9, 8))
    assert rems[ids["occ_g2"]] == [(T(8, 8), 0, False, "pending")]
    # H: a span and a done occurrence are untouched
    assert due[ids["occ_h"]] == T(16, 17)
    assert rems[ids["occ_h"]] == [(T(12, 8), 5820, True, "pending"), (T(16, 17), 0, True, "pending")]
    assert rems[ids["occ_done"]] == [(T(6, 10, 55), 5, False, "cancelled")]
    # the retired preference is gone, the others stay
    assert state["prefs"] == {"tz": "Europe/Warsaw", "slot_lunch": "12:30:00"}

    # the schema is still the models' (a data-only revision)
    command.check(cfg)

    # downgrade is a no-op, and upgrading again changes nothing
    command.downgrade(cfg, "0004")
    assert asyncio.run(_state(scratch_url)) == state
    command.upgrade(cfg, "head")
    assert asyncio.run(_state(scratch_url)) == state
