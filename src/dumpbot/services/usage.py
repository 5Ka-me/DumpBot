"""AI usage log (``ai_usage``) and the monthly budget.

Month boundaries follow the owner's local calendar (``prefs.tz``), so "октябрь" starts at local midnight
on Oct 1, not at UTC midnight.

A job of the queue runs inside ``deferred()`` (docs/features/iteration-4.md §7: the card comes first, every database
round trip before it counts):

- ``record_usage`` keeps its rows in memory instead of a transaction each right after the call; ``write_pending``
  writes them in one statement (the processor before its final reaction, the job worker after the job in any case);
- ``job_spend`` reads the month's spend from the database once per job (``prefetch_spend`` can start that early) and
  adds what the job's own calls cost since: the budget check before the paid STT call and the one before
  ``extract()`` cost one round trip together, not one each;
- a call whose result is stored before the reply (a transcript, a checklist) has its rows stored with it
  (``kept_rows`` + ``with_rows`` in the same statement, or ``insert_rows`` in the same transaction): a process
  killed before the reply (the watchdog's exit, Render out of memory) loses nothing that was paid for — the re-run
  reuses the stored result and would never record the call again.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Update, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dumpbot import clock
from dumpbot.db.models import AiUsage
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import BudgetState

__all__ = [
    "BudgetState",
    "JobUsage",
    "budget_state",
    "deferred",
    "insert_rows",
    "job_budget",
    "kept_rows",
    "month_spend",
    "month_start",
    "next_month_start",
    "prefetch_spend",
    "record_usage",
    "with_rows",
    "write_pending",
]

log = logging.getLogger(__name__)


@dataclass
class JobUsage:
    """What ``deferred()`` keeps for one job: the rows not written yet, what all its recorded calls cost, and the
    month's spend as read once (``job_spend``)."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    cost: float = 0.0  # all calls recorded in this job, written or not
    _read: asyncio.Future[tuple[float, float]] | None = None  # (spend then incl. unwritten rows, ``cost`` then)
    _month: datetime | None = None


_job: ContextVar[JobUsage | None] = ContextVar("dumpbot_job_usage", default=None)


_unfinished: set[asyncio.Future[Any]] = set()  # reads a job left running: kept until they end (never cut half-way)


@contextmanager
def deferred() -> Iterator[JobUsage]:
    """``record_usage`` inside keeps the rows until ``write_pending``; what is left at the end is the caller's to
    write (``write_pending(sessionmaker, job)``)."""
    job = JobUsage()
    token = _job.set(job)
    try:
        yield job
    finally:
        _job.reset(token)
        if job._read is not None and not job._read.done():
            _unfinished.add(job._read)
            job._read.add_done_callback(_forget)


def _forget(read: asyncio.Future[Any]) -> None:
    _unfinished.discard(read)
    if not read.cancelled():
        read.exception()  # retrieved: a failed read nobody waits for is no error


async def write_pending(sessionmaker: async_sessionmaker[AsyncSession], job: JobUsage | None = None) -> None:
    """Write the rows ``record_usage`` kept (in ``job``, else in the current ``deferred()``) in one statement and
    forget them. Never raises: failures are only logged (the rows are dropped, like a failed ``record_usage``)."""
    job = _job.get() if job is None else job
    if job is None or not job.rows:
        return
    batch = list(job.rows)
    job.rows.clear()
    try:
        async with sessionmaker() as session:
            # The table, not the entity: one executemany for all rows (the ORM's bulk insert splits rows by which
            # values are None).
            await session.execute(insert(AiUsage.__table__), batch)
            await session.commit()
    except Exception as e:
        log.warning("could not record AI usage (%d row(s)): %s: %s", len(batch), type(e).__name__, e)


@asynccontextmanager
async def kept_rows(*, enabled: bool = True) -> AsyncIterator[list[dict[str, Any]]]:
    """The rows ``record_usage`` kept so far in this job, taken out to be stored with what the block writes
    (``with_rows`` / ``insert_rows``); when the block fails they go back, for ``write_pending``. Empty outside a job
    (or without ``enabled``)."""
    job = _job.get()
    rows: list[dict[str, Any]] = []
    if enabled and job is not None:
        rows = list(job.rows)
        job.rows.clear()
    try:
        yield rows
    except BaseException:
        if job is not None and rows:
            job.rows[:0] = rows
        raise


def with_rows(stmt: Update, rows: list[dict[str, Any]]) -> Update:
    """``stmt`` that also inserts ``rows`` into ai_usage (a data-modifying WITH: one statement, one round trip, and
    both or neither stored)."""
    if not rows:
        return stmt
    return stmt.add_cte(insert(AiUsage.__table__).values(rows).cte("ai_usage_rows"))


async def insert_rows(session: AsyncSession, rows: list[dict[str, Any]]) -> None:
    """``rows`` into ai_usage within ``session``'s transaction (no commit)."""
    if rows:
        await session.execute(insert(AiUsage.__table__), rows)


def prefetch_spend(sessionmaker: async_sessionmaker[AsyncSession], prefs: Prefs, now: datetime) -> None:
    """Start reading the month's spend for ``job_spend`` (e.g. while a voice note downloads); no-op outside a job or
    when it was read already."""
    job = _job.get()
    if job is not None:
        _start_read(job, sessionmaker, prefs, now)


async def job_spend(sessionmaker: async_sessionmaker[AsyncSession], prefs: Prefs, now: datetime) -> float:
    """USD spent on AI this month (``month_spend``), including the calls of this job not written yet. In a job the
    database is read once (per month); later calls add what the job's calls cost since."""
    job = _job.get()
    if job is None:
        async with sessionmaker() as session:
            return await month_spend(session, prefs, now)
    read = _start_read(job, sessionmaker, prefs, now)
    try:
        spent, cost_then = await asyncio.shield(read)
    except asyncio.CancelledError:
        raise
    except Exception:
        if job._read is read:  # a failed read is not kept: the next check reads again
            job._read = None
        raise
    return spent + (job.cost - cost_then)


async def job_budget(sessionmaker: async_sessionmaker[AsyncSession], prefs: Prefs, now: datetime) -> BudgetState:
    """``budget_state`` from ``job_spend``."""
    return _state(await job_spend(sessionmaker, prefs, now), prefs)


def _start_read(
    job: JobUsage, sessionmaker: async_sessionmaker[AsyncSession], prefs: Prefs, now: datetime
) -> asyncio.Future[tuple[float, float]]:
    month = month_start(now, prefs)
    if job._read is None or job._month != month:
        job._month = month
        job._read = asyncio.ensure_future(_read_spend(job, sessionmaker, prefs, now))
    return job._read


async def _read_spend(
    job: JobUsage, sessionmaker: async_sessionmaker[AsyncSession], prefs: Prefs, now: datetime
) -> tuple[float, float]:
    cost_then = job.cost
    unwritten = sum(float(row["cost_usd"]) for row in job.rows)
    async with sessionmaker() as session:
        spent = await month_spend(session, prefs, now)
    return spent + unwritten, cost_then


async def record_usage(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    purpose: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    audio_seconds: float = 0.0,
    cost_usd: float = 0.0,
    ok: bool = True,
    latency_ms: int = 0,
    error: str | None = None,
) -> None:
    """Insert one ``ai_usage`` row in its own session and commit — or, inside ``deferred()``, keep it for
    ``write_pending``. Never raises: failures are only logged."""
    try:
        row = {
            "created_at": clock.utcnow(),
            "purpose": purpose[:16],
            "model": model[:128],
            "input_tokens": max(0, int(input_tokens)),
            "output_tokens": max(0, int(output_tokens)),
            "audio_seconds": Decimal(f"{max(0.0, audio_seconds):.2f}"),
            "cost_usd": Decimal(f"{max(0.0, cost_usd):.6f}"),
            "ok": ok,
            "latency_ms": max(0, int(latency_ms)),
            "error": error[:1000] if error else None,
        }
        if (job := _job.get()) is not None:
            job.rows.append(row)
            job.cost += float(row["cost_usd"])
            return
        async with sessionmaker() as session:
            session.add(AiUsage(**row))
            await session.commit()
    except Exception as e:
        # Losing one accounting row is better than failing the AI call that already happened (and was paid for).
        log.warning("could not record AI usage (purpose=%s): %s: %s", purpose, type(e).__name__, e)


def _local(now: datetime, prefs: Prefs) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(prefs.zone)


def month_start(now: datetime, prefs: Prefs) -> datetime:
    """Aware UTC start of the current calendar month in ``prefs.tz``."""
    local = _local(now, prefs)
    return datetime(local.year, local.month, 1, tzinfo=prefs.zone).astimezone(UTC)


def next_month_start(now: datetime, prefs: Prefs) -> datetime:
    """Aware UTC start of the next calendar month in ``prefs.tz``."""
    local = _local(now, prefs)
    year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
    return datetime(year, month, 1, tzinfo=prefs.zone).astimezone(UTC)


async def month_spend(session: AsyncSession, prefs: Prefs, now: datetime) -> float:
    """USD spent on AI in the current calendar month (``prefs.tz``)."""
    total = await session.scalar(
        select(func.coalesce(func.sum(AiUsage.cost_usd), 0)).where(
            AiUsage.created_at >= month_start(now, prefs),
            AiUsage.created_at < next_month_start(now, prefs),
        )
    )
    return float(total or 0)


async def budget_state(session: AsyncSession, prefs: Prefs, now: datetime) -> BudgetState:
    """Spend vs ``prefs.budget_usd``. A zero limit means AI is switched off (always exhausted)."""
    return _state(await month_spend(session, prefs, now), prefs)


def _state(spent: float, prefs: Prefs) -> BudgetState:
    limit = float(prefs.budget_usd)
    if limit > 0:
        ratio = spent / limit
    else:
        ratio = math.inf if spent > 0 else 0.0
    return BudgetState(spent_usd=spent, limit_usd=limit, ratio=ratio, exhausted=limit <= 0 or spent >= limit)
