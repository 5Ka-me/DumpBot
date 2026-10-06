"""Shared fixtures.

DB tests need a disposable Postgres and the env var TEST_DATABASE_URL, e.g.

    docker run -d --name dump-test-db -e POSTGRES_USER=dump -e POSTGRES_PASSWORD=dump \
        -e POSTGRES_DB=dump_test -p 127.0.0.1:55432:5432 postgres:18
    TEST_DATABASE_URL=postgresql+asyncpg://dump:dump@127.0.0.1:55432/dump_test uv run pytest

Tests that use the ``session`` / ``sessionmaker`` fixtures are skipped when it is not set.
The schema is recreated from the SQLAlchemy models once per test session; tables are truncated after each test.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from dumpbot.db import models  # noqa: F401  (register tables)
from dumpbot.db.base import Base
from dumpbot.domain.prefs import Prefs


@pytest.fixture(scope="session")
def db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    url = _ipv4_localhost(url)

    async def _init() -> None:
        eng = create_async_engine(url, poolclass=NullPool)
        async with eng.begin() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            await conn.run_sync(Base.metadata.create_all)
        await eng.dispose()

    asyncio.run(_init())
    return url


def _ipv4_localhost(url: str) -> str:
    """``localhost`` -> ``127.0.0.1``. The test container publishes on 127.0.0.1 only; asyncpg tries ``::1`` first,
    and on Windows a refused connection takes ~2 s, which every new (NullPool) connection would pay."""
    parsed = make_url(url)
    if parsed.host != "localhost":
        return url
    return parsed.set(host="127.0.0.1").render_as_string(hide_password=False)


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(db_url, poolclass=NullPool)
    yield eng
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with eng.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as s:
        yield s


@pytest.fixture
def prefs() -> Prefs:
    return Prefs(tz="Europe/Warsaw")
