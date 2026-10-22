"""Postgres-backed work queue (architecture.md §4.12): ``enqueue`` / ``claim`` / ``JobWorker``.

A job is claimed with ``UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED) RETURNING`` (status ``running``,
``attempts + 1``, ``locked_until``). A ``running`` job whose lock expired (the process died mid-job) is claimed
again. Handlers open their own sessions from ``ctx.sessionmaker``; claiming a job and recording its outcome are one
statement each, without a transaction around them (``ctx.autocommit``: docs/features/iteration-4.md §7):

- success -> ``done`` (a handler that returns a dict leaves it in ``Job.result`` for the Mini App);
- ``RetryLater`` -> ``queued`` at ``at`` or ``now + delay`` (``count_attempt=False`` gives the attempt back,
  e.g. budget deferral); a counted RetryLater at ``max_attempts`` -> ``dead``;
- ``AIUnavailable`` -> ``queued`` at ``now + max(min(2**attempts min, 6 h), retry_after)``; never ``dead`` while
  the job is younger than ``AI_RETRY_WINDOW`` (chutes outages are expected to end);
- any other exception -> the same backoff; at ``max_attempts`` -> ``dead`` and ``on_dead(ctx, job)``.

``JobWorker.run`` registers with ``ctx.watchdog`` and beats on every iteration and before every job: silence for
``STALL_AFTER`` (far beyond the slowest job: the AI fallback ladder, speech-to-text, Telegram) exits the process.
A job with many slow steps beats ``WATCH_NAME`` itself: the processor does before each part of an album it
transcribes.

Every job runs inside ``db.meter.measure()`` and ``usage.deferred()``: the ai_usage rows its AI calls left unwritten
are written after it whatever its outcome, and an INFO line says where its time went, numbers only
(``job 12 (message): ai 1.92 s, db 7 queries / 0.31 s, total 2.40 s``).

Logs and ``last_error`` never contain exception messages of unknown origin: they may carry message text (SQL
parameters, validation input) or Telegram file URLs with the bot token. Only the exception type and the code
location are kept; messages of our own error types are content-free by design.
"""

from __future__ import annotations

import asyncio
import logging
import re
import traceback
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Final

from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from sqlalchemy import BigInteger, and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from dumpbot import clock
from dumpbot.ai.client import AIError, AIUnavailable
from dumpbot.context import AppContext
from dumpbot.db import meter
from dumpbot.db.models import Job, OnceKey, RawMessage
from dumpbot.services import usage

__all__ = [
    "AI_RETRY_WINDOW",
    "BUDGET_REASON",
    "MAX_BACKOFF",
    "STALL_AFTER",
    "WATCH_NAME",
    "DeadHook",
    "Handler",
    "JobWorker",
    "RetryLater",
    "backoff",
    "claim",
    "describe_error",
    "enqueue",
    "forget_once",
    "pending_job",
    "requeue_deferred",
    "safe_traceback",
    "try_once",
]

log = logging.getLogger(__name__)

Handler = Callable[[AppContext, dict[str, Any]], Awaitable[Any]]  # a dict result is kept in Job.result
DeadHook = Callable[[AppContext, Job], Awaitable[None]]

MAX_BACKOFF: Final = timedelta(hours=6)
# AIUnavailable keeps a job alive this long (at most one attempt per MAX_BACKOFF once the backoff is capped).
AI_RETRY_WINDOW: Final = timedelta(days=7)
DEFAULT_LOCK: Final = timedelta(minutes=5)
MIN_RETRY_DELAY: Final = timedelta(seconds=1)  # a RetryLater "now" must not spin the worker
MIN_WAIT_S: Final = 0.5
STALL_AFTER: Final = timedelta(minutes=30)
WATCH_NAME: Final = "jobs"
# ``last_error`` of jobs deferred until the AI budget allows them (see ``requeue_deferred``).
BUDGET_REASON: Final = "budget exhausted"
_ERROR_MAX: Final = 500
_TOKEN_RE: Final = re.compile(r"bot\d+:[\w-]+")  # defence in depth: never store/log a bot token


class RetryLater(Exception):
    """Raised by a handler: put the job back into the queue at ``at`` (or ``now + delay``).

    ``count_attempt=False`` does not use up an attempt (waiting for the budget, a config fix by the owner…).
    """

    def __init__(
        self,
        delay: timedelta | None = None,
        reason: str = "",
        *,
        at: datetime | None = None,
        count_attempt: bool = True,
    ) -> None:
        super().__init__(reason or "retry later")
        self.delay = delay
        self.reason = reason
        self.at = at
        self.count_attempt = count_attempt


def backoff(attempts: int) -> timedelta:
    """``min(2**attempts minutes, 6 h)``."""
    if attempts >= 9:  # 2**9 min > 6 h; avoids huge powers for long-lived jobs
        return MAX_BACKOFF
    return min(timedelta(minutes=2 ** max(attempts, 0)), MAX_BACKOFF)


# --- queue primitives ------------------------------------------------------------------------------------------


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str | None = None,
    run_after: datetime | None = None,
) -> int | None:
    """Insert a queued job; ``None`` when a job with the same ``dedupe_key`` already exists. Does not commit."""
    now = clock.utcnow()
    stmt = (
        pg_insert(Job)
        .values(
            kind=kind,
            payload=payload,
            dedupe_key=dedupe_key,
            status="queued",
            attempts=0,
            run_after=run_after or now,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=[Job.dedupe_key])
        .returning(Job.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def claim(session: AsyncSession, now: datetime, limit: int = 5, lock_for: timedelta = DEFAULT_LOCK) -> list[Job]:
    """Lock up to ``limit`` runnable jobs (queued and due, or running with an expired lock) for this worker.

    Concurrent claimers never get the same job (SKIP LOCKED). Does not commit.
    """
    runnable = or_(
        and_(Job.status == "queued", Job.run_after <= now),
        and_(Job.status == "running", Job.locked_until < now),
    )
    ids = (
        select(Job.id)
        .where(runnable)
        .order_by(Job.run_after, Job.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    stmt = (
        update(Job)
        .where(Job.id.in_(ids))
        .values(status="running", attempts=Job.attempts + 1, locked_until=now + lock_for, updated_at=now)
        .returning(Job)
        .execution_options(synchronize_session=False)
    )
    jobs = list((await session.scalars(stmt)).all())
    jobs.sort(key=lambda j: (j.run_after, j.id))
    return jobs


async def requeue_deferred(session: AsyncSession, now: datetime) -> int:
    """Make jobs waiting for the AI budget runnable now (after the owner raised the budget). Does not commit.

    Matches queued jobs whose raw message is ``deferred`` (by ``raw_id`` or album ``media_group_id``) or that were
    deferred for the budget without a raw message (``last_error == BUDGET_REASON``). Returns the number of jobs.
    """
    deferred_ids = select(RawMessage.id).where(RawMessage.status == "deferred")
    deferred_groups = select(RawMessage.media_group_id).where(
        RawMessage.status == "deferred", RawMessage.media_group_id.is_not(None)
    )
    stmt = (
        update(Job)
        .where(
            Job.status == "queued",
            Job.run_after > now,
            or_(
                Job.last_error == BUDGET_REASON,
                Job.payload["raw_id"].astext.cast(BigInteger).in_(deferred_ids),
                Job.payload["media_group_id"].astext.in_(deferred_groups),
            ),
        )
        .values(run_after=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def pending_job(session: AsyncSession, kind: str, item_id: int) -> int | None:
    """Id of a queued or running job of ``kind`` about the item (payload ``item_id``), if there is one."""
    stmt = (
        select(Job.id)
        .where(Job.kind == kind, Job.status.in_(("queued", "running")), Job.payload["item_id"].astext == str(item_id))
        .order_by(Job.id)
        .limit(1)
    )
    return await session.scalar(stmt)


async def try_once(session: AsyncSession, key: str) -> bool:
    """Insert a ``once_keys`` marker; True only for the caller that inserted it. Does not commit."""
    stmt = (
        pg_insert(OnceKey)
        .values(key=key, created_at=clock.utcnow())
        .on_conflict_do_nothing(index_elements=[OnceKey.key])
        .returning(OnceKey.key)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def forget_once(session: AsyncSession, key: str) -> None:
    """Remove a marker so the guarded action can run again (e.g. the digest could not be sent)."""
    await session.execute(delete(OnceKey).where(OnceKey.key == key))


# --- worker ----------------------------------------------------------------------------------------------------


async def _default_on_dead(ctx: AppContext, job: Job) -> None:
    # Imported lazily: the processor imports this module.
    from dumpbot.services.processor import on_dead

    await on_dead(ctx, job)


class JobWorker:
    """Runs queued jobs one at a time (each claimed right before it runs, so its lock is fresh)."""

    def __init__(
        self,
        ctx: AppContext,
        handlers: dict[str, Handler],
        *,
        poll_interval_s: float = 5.0,
        on_dead: DeadHook | None = _default_on_dead,
        batch: int = 5,
        lock_for: timedelta = DEFAULT_LOCK,
    ) -> None:
        self._ctx = ctx
        self._handlers = dict(handlers)
        self._poll = poll_interval_s
        self._on_dead = on_dead
        self._batch = max(1, batch)
        self._lock_for = lock_for

    async def run(self) -> None:
        """Forever: run due jobs, then wait for ``ctx.jobs_wakeup``, the next ``run_after`` or the poll interval."""
        log.info("job worker started (%d handlers)", len(self._handlers))
        self._ctx.watchdog.watch(WATCH_NAME, STALL_AFTER.total_seconds())
        while True:
            self._ctx.watchdog.beat(WATCH_NAME)
            self._ctx.jobs_wakeup.clear()  # before claiming: a wakeup during the run is not lost
            handled = 0
            try:
                handled = await self.run_once()
            except Exception as e:  # e.g. the DB is down: keep the loop alive, try again later
                log.error("job worker iteration failed: %s", describe_error(e))
            if handled >= self._batch:
                await asyncio.sleep(0)  # more jobs may be due: go again, but let other tasks run
                continue
            timeout = await self._wait_time()
            try:
                await asyncio.wait_for(self._ctx.jobs_wakeup.wait(), timeout)
            except TimeoutError:
                pass

    async def run_once(self) -> int:
        """Claim and run up to ``batch`` due jobs; returns how many ran."""
        count = 0
        while count < self._batch:
            self._ctx.watchdog.beat(WATCH_NAME)
            job = await self._claim_one()
            if job is None:
                break
            await self._execute(job)
            count += 1
        return count

    async def _claim_one(self) -> Job | None:
        # One statement: its own transaction is enough (no BEGIN / COMMIT round trips).
        async with self._ctx.autocommit() as session:
            jobs = await claim(session, clock.utcnow(), limit=1, lock_for=self._lock_for)
            await session.commit()
        return jobs[0] if jobs else None

    async def _execute(self, job: Job) -> None:
        handler = self._handlers.get(job.kind)
        if handler is None:
            log.error("job %s: no handler for kind %r", job.id, job.kind)
            await self._dead(job, f"no handler for kind {job.kind!r}")
            return
        if job.attempts > job.max_attempts:  # it kept dying without recording an outcome (process crashes)
            log.error("job %s (%s): attempts exhausted without an outcome", job.id, job.kind)
            await self._dead(job, "attempts exhausted (crashed repeatedly)")
            return
        log.debug("job %s (%s) attempt %d", job.id, job.kind, job.attempts)
        # The AI calls' ai_usage rows wait until the reply is out (services.usage.deferred); what the handler did
        # not write itself is written here, whatever the outcome.
        with meter.measure() as spent, usage.deferred() as spending:
            try:
                result = await handler(self._ctx, dict(job.payload or {}))
            except asyncio.CancelledError:
                await self._release(job, spending)
                raise
            except Exception as e:
                await usage.write_pending(self._ctx.autocommit, spending)
                if isinstance(e, RetryLater):
                    await self._retry_later(job, e)
                elif isinstance(e, AIUnavailable):
                    await self._ai_unavailable(job, e)
                else:
                    await self._failed(job, e)
            else:
                await usage.write_pending(self._ctx.autocommit, spending)
                outcome = result if isinstance(result, dict) else None
                await self._record(job.id, status="done", last_error=None, result=outcome)
                log.debug("job %s (%s) done", job.id, job.kind)
        # docs/features/iteration-4.md §7: where the time went, numbers only.
        log.info("job %s (%s): %s", job.id, job.kind, spent.summary())

    async def _retry_later(self, job: Job, e: RetryLater) -> None:
        now = clock.utcnow()
        attempts = job.attempts if e.count_attempt else max(job.attempts - 1, 0)
        reason = (e.reason or "retry later")[:_ERROR_MAX]
        if e.count_attempt and attempts >= job.max_attempts:
            log.warning("job %s (%s): giving up after %d attempts: %s", job.id, job.kind, attempts, reason)
            await self._dead(job, reason)
            return
        when = e.at or now + (e.delay if e.delay is not None else backoff(attempts))
        when = max(when, now + MIN_RETRY_DELAY)
        log.info("job %s (%s) retry at %s: %s", job.id, job.kind, when.isoformat(timespec="seconds"), reason)
        await self._record(job.id, status="queued", run_after=when, attempts=attempts, last_error=reason)

    async def _ai_unavailable(self, job: Job, e: AIUnavailable) -> None:
        now = clock.utcnow()
        reason = describe_error(e)
        if job.created_at is not None and now - job.created_at > AI_RETRY_WINDOW:
            log.warning("job %s (%s): AI unavailable for too long, giving up", job.id, job.kind)
            await self._dead(job, reason)
            return
        delay = backoff(job.attempts)
        if e.retry_after:
            delay = min(max(delay, timedelta(seconds=e.retry_after)), MAX_BACKOFF)
        # Capped below max_attempts: AI outages alone never make a job dead (see AI_RETRY_WINDOW).
        attempts = min(job.attempts, job.max_attempts - 1)
        log.warning("job %s (%s): AI unavailable, retry in %s", job.id, job.kind, delay)
        await self._record(job.id, status="queued", run_after=now + delay, attempts=attempts, last_error=reason)

    async def _failed(self, job: Job, e: Exception) -> None:
        reason = describe_error(e)
        log.error(
            "job %s (%s) failed (attempt %d/%d): %s\n%s",
            job.id,
            job.kind,
            job.attempts,
            job.max_attempts,
            reason,
            safe_traceback(e),
        )
        if job.attempts >= job.max_attempts:
            await self._dead(job, reason)
            return
        run_after = clock.utcnow() + backoff(job.attempts)
        await self._record(job.id, status="queued", run_after=run_after, last_error=reason)

    async def _dead(self, job: Job, reason: str) -> None:
        reason = reason[:_ERROR_MAX]
        await self._record(job.id, status="dead", last_error=reason)
        job.status = "dead"
        job.last_error = reason
        if self._on_dead is None:
            return
        try:
            await self._on_dead(self._ctx, job)
        except Exception as e:
            log.error("on_dead for job %s failed: %s", job.id, describe_error(e))

    async def _release(self, job: Job, spending: usage.JobUsage) -> None:
        """Cancelled mid-job (shutdown): keep the ai_usage rows of the calls already made, give the attempt back and
        make the job runnable right away."""

        async def release() -> None:
            await usage.write_pending(self._ctx.autocommit, spending)
            await self._record(job.id, status="queued", run_after=clock.utcnow(), attempts=max(job.attempts - 1, 0))

        try:
            await asyncio.wait_for(release(), timeout=2.0)
        except (Exception, asyncio.CancelledError):
            log.warning("job %s: could not release on cancel; its lock expires on its own", job.id)

    async def _record(self, job_id: int, **values: Any) -> None:
        values.setdefault("locked_until", None)
        values["updated_at"] = clock.utcnow()
        try:
            async with self._ctx.autocommit() as session:  # one statement: no transaction around it
                await session.execute(update(Job).where(Job.id == job_id).values(**values))
                await session.commit()
        except Exception as e:  # the job stays "running" and is claimed again when its lock expires
            log.error("job %s: could not record outcome %s: %s", job_id, values.get("status"), describe_error(e))

    async def _wait_time(self) -> float:
        """Seconds until the next queued job is due (album debounce), capped by the poll interval."""
        try:
            async with self._ctx.autocommit() as session:
                nxt = await session.scalar(select(func.min(Job.run_after)).where(Job.status == "queued"))
        except Exception as e:
            log.debug("next run_after lookup failed: %s", describe_error(e))
            return self._poll
        if nxt is None:
            return self._poll
        seconds = (nxt - clock.utcnow()).total_seconds()
        return min(self._poll, max(seconds, MIN_WAIT_S))


# --- privacy-safe error descriptions ----------------------------------------------------------------------------


def describe_error(e: BaseException) -> str:
    """``Type: message`` for our own content-free errors, ``Type at file:line`` for everything else; a database
    error also names the driver's error and its SQLSTATE, still without the message:
    ``ProgrammingError (UndefinedTableError 42P01) at asyncpg.py:797``."""
    name = type(e).__name__
    # Network errors may quote the request URL, which contains the bot token for Telegram calls.
    if isinstance(e, AIError | RetryLater | TelegramAPIError) and not isinstance(e, TelegramNetworkError):
        text = (e.message if isinstance(e, TelegramAPIError) else str(e)) or ""
        text = _TOKEN_RE.sub("bot<token>", text.strip())
        return f"{name}: {text}"[:_ERROR_MAX] if text else name
    if isinstance(e, DBAPIError):
        name += _db_detail(e)
    tb = traceback.extract_tb(e.__traceback__)
    if tb:
        last = tb[-1]
        return f"{name} at {last.filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{last.lineno}"
    return name


def _db_detail(e: DBAPIError) -> str:
    """`` (UndefinedTableError 42P01)``: the driver's exception type and SQLSTATE tell a missing table from a lost
    connection or a pooler conflict without the message, which may quote stored values. SQLAlchemy's asyncpg adapter
    raises its own DB-API error from the asyncpg one, so the type is taken from ``e.orig.__cause__`` when there is
    one."""
    orig = e.orig
    if orig is None:
        return ""
    driver = orig.__cause__ if isinstance(orig.__cause__, Exception) else orig
    code = getattr(driver, "sqlstate", None) or getattr(orig, "sqlstate", None)
    sqlstate = f" {code}" if isinstance(code, str) and code.isalnum() and len(code) <= 5 else ""
    return f" ({type(driver).__name__}{sqlstate})"


def safe_traceback(e: BaseException) -> str:
    """Stack frames (code locations and source lines) without the exception message or local values."""
    return "".join(traceback.format_tb(e.__traceback__)).rstrip()
