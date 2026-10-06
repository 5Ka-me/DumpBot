from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from dumpbot import clock
from dumpbot.db.models import AiUsage
from dumpbot.domain.prefs import Prefs
from dumpbot.services.usage import (
    BudgetState,
    budget_state,
    month_spend,
    month_start,
    next_month_start,
    record_usage,
)

WARSAW = Prefs(tz="Europe/Warsaw")


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


async def add_spend(session, *entries: tuple[datetime, str]) -> None:
    session.add_all(
        AiUsage(created_at=at, purpose="extract", model="m", cost_usd=Decimal(cost)) for at, cost in entries
    )
    await session.commit()


async def test_record_usage_writes_a_row(sessionmaker, monkeypatch):
    monkeypatch.setattr(clock, "utcnow", lambda: utc(2026, 10, 6, 12, 0))
    await record_usage(
        sessionmaker,
        purpose="extract",
        model="google/gemma-4-31B-turbo-TEE",
        input_tokens=1200,
        output_tokens=150,
        cost_usd=0.00019961,
        latency_ms=850,
    )
    await record_usage(
        sessionmaker, purpose="stt", model="x" * 300, audio_seconds=2, ok=False, error="HTTP 503: " + "e" * 5000
    )

    async with sessionmaker() as s:
        ok, err = (await s.scalars(select(AiUsage).order_by(AiUsage.id))).all()
    assert ok.created_at == utc(2026, 10, 6, 12, 0)
    assert (ok.purpose, ok.input_tokens, ok.output_tokens, ok.latency_ms, ok.ok) == ("extract", 1200, 150, 850, True)
    assert ok.cost_usd == Decimal("0.000200")
    assert (err.ok, err.audio_seconds, len(err.model), len(err.error)) == (False, Decimal("2.00"), 128, 1000)


async def test_record_usage_never_raises(caplog):
    def broken_sessionmaker():
        raise RuntimeError("database is down")

    with caplog.at_level(logging.WARNING, logger="dumpbot.services.usage"):
        await record_usage(broken_sessionmaker, purpose="extract", model="m", cost_usd=0.01)
    assert "could not record AI usage" in caplog.text


async def test_record_usage_survives_a_failing_commit(caplog):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def add(self, row):
            pass

        async def commit(self):
            raise ConnectionError("lost connection")

    with caplog.at_level(logging.WARNING, logger="dumpbot.services.usage"):
        await record_usage(lambda: Session(), purpose="answer", model="m")
    assert "ConnectionError" in caplog.text


async def test_month_spend_uses_the_local_month_in_warsaw(session):
    await add_spend(
        session,
        (utc(2026, 9, 30, 21, 59), "1.0"),  # Sep 30 23:59 CEST -> September
        (utc(2026, 9, 30, 22, 0), "0.5"),  # Oct 1 00:00 CEST -> October
        (utc(2026, 10, 31, 22, 59), "0.25"),  # Oct 31 23:59 CET (after the DST change) -> October
        (utc(2026, 10, 31, 23, 0), "2.0"),  # Nov 1 00:00 CET -> November
    )
    assert await month_spend(session, WARSAW, utc(2026, 10, 6, 12, 0)) == pytest.approx(0.75)
    assert await month_spend(session, WARSAW, utc(2026, 9, 30, 21, 30)) == pytest.approx(1.0)
    assert await month_spend(session, WARSAW, utc(2026, 10, 31, 23, 30)) == pytest.approx(2.0)
    assert await month_spend(session, WARSAW, utc(2027, 1, 15, 0, 0)) == 0.0


def test_month_boundaries_in_warsaw():
    assert month_start(utc(2026, 10, 6, 12, 0), WARSAW) == utc(2026, 9, 30, 22, 0)
    assert next_month_start(utc(2026, 10, 6, 12, 0), WARSAW) == utc(2026, 10, 31, 23, 0)
    # 00:30 local on Nov 1 already belongs to November
    assert next_month_start(utc(2026, 10, 31, 23, 30), WARSAW) == utc(2026, 11, 30, 23, 0)
    assert next_month_start(utc(2026, 12, 15, 9, 0), WARSAW) == utc(2026, 12, 31, 23, 0)
    assert next_month_start(utc(2027, 3, 10, 9, 0), WARSAW) == utc(2027, 3, 31, 22, 0)  # April is CEST again
    assert next_month_start(datetime(2026, 10, 6, 12, 0), WARSAW) == utc(2026, 10, 31, 23, 0)  # naive = UTC


async def test_budget_state(session):
    now = utc(2026, 10, 6, 12, 0)
    prefs = Prefs(tz="Europe/Warsaw", budget_usd=5.0)
    assert await budget_state(session, prefs, now) == BudgetState(0.0, 5.0, 0.0, False)

    await add_spend(session, (utc(2026, 10, 2, 8, 0), "4.0"))
    state = await budget_state(session, prefs, now)
    assert (state.spent_usd, state.limit_usd, state.exhausted) == (pytest.approx(4.0), 5.0, False)
    assert state.ratio == pytest.approx(0.8)

    await add_spend(session, (utc(2026, 10, 3, 8, 0), "1.0"))
    state = await budget_state(session, prefs, now)
    assert state.exhausted is True
    assert state.ratio == pytest.approx(1.0)


async def test_budget_state_zero_limit(session):
    now = utc(2026, 10, 6, 12, 0)
    off = Prefs(tz="Europe/Warsaw", budget_usd=0)
    assert await budget_state(session, off, now) == BudgetState(0.0, 0.0, 0.0, True)

    await add_spend(session, (utc(2026, 10, 2, 8, 0), "0.01"))
    state = await budget_state(session, off, now)
    assert state.exhausted is True
    assert math.isinf(state.ratio)
