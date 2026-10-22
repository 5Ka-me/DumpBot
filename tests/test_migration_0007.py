"""Migration 0007 (iteration 4, §5: the events journal) on a scratch database.

Runs ``alembic upgrade`` for real against a database of its own (``<test db>_mig0007``, created and dropped here) on
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
    name = f"{parsed.database}_mig0007"
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


async def _indexes(url: str, table: str) -> list[str]:
    rows = await _run(url, f"SELECT indexname FROM pg_indexes WHERE tablename = '{table}' ORDER BY indexname")
    return [r[0] for r in rows]


def test_upgrade_adds_the_events_table_and_downgrades_cleanly(scratch_url: str) -> None:
    cfg = _alembic(scratch_url)
    command.upgrade(cfg, "0006")
    asyncio.run(_run(scratch_url, "INSERT INTO items (id, kind, title) VALUES (1, 'task', 'Позвонить маме')"))

    command.upgrade(cfg, "head")
    command.check(cfg)  # the schema is the models'
    assert asyncio.run(_columns(scratch_url, "events")) == [
        "created_at",
        "data",
        "id",
        "item_id",
        "occurrence_id",
        "source",
        "type",
    ]
    assert asyncio.run(_indexes(scratch_url, "events")) == [
        "ix_events_item_created",
        "ix_events_type_created",
        "pk_events",
    ]
    rows = asyncio.run(
        _run(
            scratch_url,
            "INSERT INTO events (type, source, item_id, data) "
            """VALUES ('button', 'reminder', 1, '{"action": "o:done"}')""",
            "INSERT INTO events (type, item_id) VALUES ('command', 404)",  # no foreign key: the journal outlives items
            "SELECT id, type, source, item_id, data, created_at IS NOT NULL FROM events ORDER BY id",
        )
    )
    assert rows == [
        (1, "button", "reminder", 1, {"action": "o:done"}, True),
        (2, "command", None, 404, None, True),
    ]

    command.downgrade(cfg, "0006")
    assert asyncio.run(_columns(scratch_url, "events")) == []
    assert asyncio.run(_run(scratch_url, "SELECT title FROM items")) == [("Позвонить маме",)]  # the data stays
    command.upgrade(cfg, "head")
    command.check(cfg)
