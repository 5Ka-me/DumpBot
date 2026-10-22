"""services.usage inside a job (docs/features/iteration-4.md §7): ai_usage rows wait for ``write_pending``, the
month's spend is read once per job (``job_spend``) and can be read while something else goes on
(``prefetch_spend``)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from dumpbot import clock
from dumpbot.db import meter
from dumpbot.db.models import AiUsage
from dumpbot.domain.prefs import Prefs
from dumpbot.services import usage

PREFS = Prefs(tz="Europe/Warsaw", budget_usd=1.0)
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


async def rows(sessionmaker) -> list[AiUsage]:
    async with sessionmaker() as s:
        return list((await s.scalars(select(AiUsage).order_by(AiUsage.id))).all())


async def test_rows_wait_for_write_pending_and_go_in_one_statement(sessionmaker, engine, monkeypatch):
    monkeypatch.setattr(clock, "utcnow", lambda: NOW)
    meter.install(engine)
    with usage.deferred() as job:
        await usage.record_usage(sessionmaker, purpose="stt", model="whisper", cost_usd=0.001, audio_seconds=3)
        await usage.record_usage(sessionmaker, purpose="extract", model="m", cost_usd=0.002, ok=False, error="x")
        assert await rows(sessionmaker) == []
        assert abs(job.cost - 0.003) < 1e-12 and len(job.rows) == 2
        with meter.measure(trace=True) as m:
            await usage.write_pending(sessionmaker)
        # (a new connection's set-up rolls back once)
        assert [c.what for c in m.trace or [] if c.what != "ROLLBACK"] == ["BEGIN", "INSERT ai_usage", "COMMIT"]
        assert job.rows == [] and abs(job.cost - 0.003) < 1e-12  # written, still this job's cost
        await usage.write_pending(sessionmaker)  # nothing left: no statement
    stt, extract = await rows(sessionmaker)
    assert (stt.purpose, stt.cost_usd, stt.audio_seconds, stt.created_at) == ("stt", Decimal("0.001000"), 3, NOW)
    assert (extract.ok, extract.error) == (False, "x")
    # outside a job a row is written at once
    await usage.record_usage(sessionmaker, purpose="answer", model="m", cost_usd=0.5)
    assert [r.purpose for r in await rows(sessionmaker)] == ["stt", "extract", "answer"]


async def test_rows_left_over_belong_to_the_caller(sessionmaker):
    with usage.deferred() as job:
        await usage.record_usage(sessionmaker, purpose="stt", model="whisper", cost_usd=0.001)
    assert len(job.rows) == 1 and await rows(sessionmaker) == []
    await usage.write_pending(sessionmaker, job)
    assert [r.purpose for r in await rows(sessionmaker)] == ["stt"]


async def test_a_failed_write_is_logged_and_dropped(caplog):
    def broken():
        raise ConnectionError("db down")

    with usage.deferred() as job:
        await usage.record_usage(broken, purpose="stt", model="whisper", cost_usd=0.001)  # type: ignore[arg-type]
        await usage.write_pending(broken)  # type: ignore[arg-type]
    assert job.rows == []
    assert "could not record AI usage" in caplog.text


async def test_the_spend_is_read_once_per_job_with_the_jobs_own_calls(sessionmaker, engine):
    meter.install(engine)
    async with sessionmaker() as s:
        s.add(AiUsage(created_at=NOW, purpose="extract", model="m", cost_usd=Decimal("0.4")))
        await s.commit()
    with usage.deferred(), meter.measure(trace=True) as m:
        await usage.record_usage(sessionmaker, purpose="stt", model="whisper", cost_usd=0.1)  # not written yet
        assert await usage.job_spend(sessionmaker, PREFS, NOW) == 0.5
        await usage.record_usage(sessionmaker, purpose="extract", model="m", cost_usd=0.2)
        assert abs(await usage.job_spend(sessionmaker, PREFS, NOW) - 0.7) < 1e-9
        await usage.write_pending(sessionmaker)  # written now: counted once, not twice
        state = await usage.job_budget(sessionmaker, PREFS, NOW)
    assert abs(state.spent_usd - 0.7) < 1e-9 and not state.exhausted
    assert [c.what for c in m.trace or []].count("SELECT") == 1
    # outside a job: read every time, from the database
    assert abs(await usage.job_spend(sessionmaker, PREFS, NOW) - 0.7) < 1e-9


async def test_the_spend_can_be_read_while_something_else_goes_on(sessionmaker, engine):
    meter.install(engine)
    with usage.deferred(), meter.measure(trace=True) as m:
        usage.prefetch_spend(sessionmaker, PREFS, NOW)
        await asyncio.sleep(0.05)  # e.g. a voice note downloading
        assert await usage.job_spend(sessionmaker, PREFS, NOW) == 0.0
        usage.prefetch_spend(sessionmaker, PREFS, NOW)  # read already: nothing more
        assert await usage.job_spend(sessionmaker, PREFS, NOW) == 0.0
    assert [c.what for c in m.trace or []].count("SELECT") == 1
    usage.prefetch_spend(sessionmaker, PREFS, NOW)  # outside a job: no-op


async def test_a_failed_read_is_read_again(sessionmaker):
    calls = 0

    def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("db down")
        return sessionmaker()

    with usage.deferred():
        try:
            await usage.job_spend(flaky, PREFS, NOW)  # type: ignore[arg-type]
        except ConnectionError:
            pass
        else:
            raise AssertionError("the failed read was swallowed")
        assert await usage.job_spend(flaky, PREFS, NOW) == 0.0  # type: ignore[arg-type]
    assert calls == 2
