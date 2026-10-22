"""db.meter: what a job's database calls are counted as, the database time when calls overlap, the AI time, and the
checkout ping of idle connections that replaces ``pool_pre_ping`` (docs/features/iteration-4.md §7)."""

from __future__ import annotations

import asyncio
import re

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from dumpbot.db import meter
from dumpbot.db.engine import autocommit_sessionmaker, make_engine, make_sessionmaker


@pytest.fixture
async def app_engine(db_url: str):
    eng = make_engine(db_url)
    async with eng.connect() as conn:  # connected: the dialect's one-time set-up (and its ROLLBACK) is behind
        await conn.execute(text("SELECT 1"))
    yield eng
    await eng.dispose()


def kinds(m: meter.Meter) -> list[str]:
    return [c.what for c in m.trace or []]


async def test_a_session_counts_its_begin_statements_and_commit(app_engine: AsyncEngine) -> None:
    sessions = make_sessionmaker(app_engine)
    with meter.measure(trace=True) as m:
        async with sessions() as s:
            await s.execute(text("SELECT 1"))
            await s.execute(text("SELECT 2"))
            await s.commit()
    assert kinds(m) == ["BEGIN", "SELECT", "SELECT", "COMMIT"]
    assert m.queries == 4 and m.db_s > 0
    # a session closed without a commit rolls back (one call); nothing is counted outside a meter
    with meter.measure(trace=True) as m:
        async with sessions() as s:
            await s.execute(text("SELECT 1"))
    assert kinds(m) == ["BEGIN", "SELECT", "ROLLBACK"]
    async with sessions() as s:
        await s.execute(text("SELECT 1"))
    assert meter.current() is None


async def test_autocommit_sessions_send_no_begin_and_no_commit(app_engine: AsyncEngine) -> None:
    sessions = autocommit_sessionmaker(make_sessionmaker(app_engine))
    with meter.measure(trace=True) as m:
        async with sessions() as s:
            await s.execute(text("CREATE TEMP TABLE IF NOT EXISTS t_meter (x int)"))
            await s.execute(text("INSERT INTO t_meter VALUES (1)"))
            await s.commit()
    assert kinds(m) == ["CREATE", "INSERT t_meter"]
    async with sessions() as s:  # committed by the statement itself (same pooled connection: temp tables live on)
        assert (await s.execute(text("SELECT count(*) FROM t_meter"))).scalar_one() == 1


async def test_calls_at_once_count_once_in_the_database_time(app_engine: AsyncEngine) -> None:
    sessions = autocommit_sessionmaker(make_sessionmaker(app_engine))

    async def sleep() -> None:
        async with sessions() as s:
            await s.execute(text("SELECT pg_sleep(0.3)"))

    with meter.measure() as m:
        await asyncio.gather(sleep(), sleep())
    assert m.queries == 2
    assert 0.3 <= m.db_s < 0.5  # not 0.6


async def test_ai_time_leaves_out_the_database_calls_inside(app_engine: AsyncEngine) -> None:
    sessions = autocommit_sessionmaker(make_sessionmaker(app_engine))
    with meter.measure() as m:
        with meter.ai():
            await asyncio.sleep(0.2)
            async with sessions() as s:
                await s.execute(text("SELECT pg_sleep(0.2)"))
    assert 0.18 <= m.ai_s < 0.3
    assert 0.18 <= m.db_s < 0.3
    with meter.ai():  # no meter: nothing to count, nothing breaks
        pass


async def test_a_failed_statement_is_counted_and_the_meter_goes_on(app_engine: AsyncEngine) -> None:
    sessions = autocommit_sessionmaker(make_sessionmaker(app_engine))
    with meter.measure(trace=True) as m:
        async with sessions() as s:
            with pytest.raises(Exception, match="no_such_table"):
                await s.execute(text("SELECT * FROM no_such_table"))
        async with sessions() as s:
            await s.execute(text("SELECT 1"))
    assert kinds(m) == ["SELECT", "SELECT"]
    assert m._running == 0


def test_the_summary_is_numbers_only() -> None:
    m = meter.Meter(queries=7, db_s=0.314, ai_s=1.92)
    assert re.fullmatch(r"ai 1\.92 s, db 7 queries / 0\.31 s, total \d+\.\d\d s", m.summary())


# --- the checkout ping ---------------------------------------------------------------------------------------------


@pytest.fixture
async def pinged(db_url: str):
    """An engine like ``make_engine``'s, pinging connections idle for more than 0.2 s."""
    eng = create_async_engine(db_url, pool_size=1, max_overflow=0)
    meter.idle_ping(eng, idle_s=0.2)
    meter.install(eng)
    async with eng.connect() as conn:
        await conn.execute(text("SELECT 1"))
    await asyncio.sleep(0)
    yield eng
    await eng.dispose()


async def test_make_engine_pings_idle_connections_instead_of_every_checkout(app_engine: AsyncEngine) -> None:
    assert app_engine.sync_engine.pool._pre_ping is False
    sessions = make_sessionmaker(app_engine)
    with meter.measure(trace=True) as m:
        for _ in range(3):
            async with sessions() as s:
                await s.execute(text("SELECT 1"))
    assert "PING" not in kinds(m)


async def test_a_connection_idle_for_a_while_is_pinged_once(pinged: AsyncEngine) -> None:
    sessions = autocommit_sessionmaker(make_sessionmaker(pinged))
    async with sessions() as s:  # a new connection: no ping
        await s.execute(text("SELECT 1"))
    with meter.measure(trace=True) as m:
        async with sessions() as s:  # used a moment ago
            await s.execute(text("SELECT 1"))
        await asyncio.sleep(0.3)
        async with sessions() as s:
            await s.execute(text("SELECT 1"))
    assert kinds(m) == ["SELECT", "PING", "SELECT"]


async def test_a_dropped_idle_connection_is_replaced_on_checkout(pinged: AsyncEngine, db_url: str) -> None:
    sessions = autocommit_sessionmaker(make_sessionmaker(pinged))
    async with sessions() as s:
        pid = (await s.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    killer = create_async_engine(db_url)
    async with killer.connect() as conn:  # the pooler / the network drops it while it sits in the pool
        await conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        await conn.commit()
    await killer.dispose()
    await asyncio.sleep(0.3)
    async with sessions() as s:  # the ping fails, the pool connects again: the caller sees nothing
        new_pid = (await s.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    assert new_pid != pid
