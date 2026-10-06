"""Shared fixtures.

DB tests need a disposable Postgres and the env var TEST_DATABASE_URL, e.g.

    docker run -d --name dump-test-db -e POSTGRES_USER=dump -e POSTGRES_PASSWORD=dump \
        -e POSTGRES_DB=dump_test -p 55432:5432 postgres:18
    TEST_DATABASE_URL=postgresql+asyncpg://dump:dump@localhost:55432/dump_test uv run pytest

Tests that use the ``session`` / ``sessionmaker`` fixtures are skipped when it is not set.
The schema is recreated from the SQLAlchemy models once per test session; tables are truncated after each test.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
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

    async def _init() -> None:
        eng = create_async_engine(url, poolclass=NullPool)
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await eng.dispose()

    asyncio.run(_init())
    return url


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
