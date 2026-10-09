"""Migration 0006 (iteration 3, §5: the scheduler's last tick, reminders held over quiet hours) on a scratch database.

Runs ``alembic upgrade`` for real against a database of its own (``<test db>_mig0006``, created and dropped here) on
the test Postgres of ``TEST_DATABASE_URL``; skipped without it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
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


@pytest.fixture
def scratch_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    parsed = make_url(url)
    if parsed.host == "localhost":
        parsed = parsed.set(host="127.0.0.1")
    name = f"{parsed.database}_mig0006"
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


async def _run(url: str, *statements: str) -> list[Any]:
    """Run the statements in one transaction; the rows of the last one."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            rows: list[Any] = []
            for statement in statements:
                result = await conn.execute(text(statement))
                rows = list(result.all()) if result.returns_rows else []
            return rows
    finally:
        await engine.dispose()


async def _columns(url: str, table: str) -> list[str]:
    rows = await _run(
        url, f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table}' ORDER BY column_name"
    )
    return [r[0] for r in rows]


def test_upgrade_adds_app_state_and_held_from_and_downgrades_cleanly(scratch_url: str) -> None:
    cfg = _alembic(scratch_url)
    command.upgrade(cfg, "0005")
    asyncio.run(
        _run(
            scratch_url,
            "INSERT INTO items (id, kind, title) VALUES (1, 'task', 'Позвонить маме')",
            "INSERT INTO occurrences (id, item_id, due_at, precision) VALUES (1, 1, '2026-10-06 18:00+00', 'exact')",
            "INSERT INTO reminders (occurrence_id, fire_at) VALUES (1, '2026-10-06 18:00+00')",
        )
    )

    command.upgrade(cfg, "head")
    command.check(cfg)  # the schema is the models'
    assert asyncio.run(_columns(scratch_url, "app_state")) == ["key", "updated_at", "value"]
    assert asyncio.run(_run(scratch_url, "SELECT held_from, status FROM reminders")) == [(None, "pending")]
    rows = asyncio.run(
        _run(
            scratch_url,
            """INSERT INTO app_state (key, value) VALUES ('scheduler:last_tick', '"2026-10-06T17:00:00+00:00"')""",
            "SELECT key, value, updated_at IS NOT NULL FROM app_state",
        )
    )
    assert rows == [("scheduler:last_tick", "2026-10-06T17:00:00+00:00", True)]

    command.downgrade(cfg, "0005")
    assert asyncio.run(_columns(scratch_url, "app_state")) == []
    assert "held_from" not in asyncio.run(_columns(scratch_url, "reminders"))
    assert asyncio.run(_run(scratch_url, "SELECT status FROM reminders")) == [("pending",)]  # the data stays
    command.upgrade(cfg, "head")
    command.check(cfg)
