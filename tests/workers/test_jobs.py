"""workers.jobs: enqueue / claim / JobWorker outcomes (retry, backoff, dead), run loop, requeue_deferred."""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from typing import Any

import asyncpg
import pytest
from sqlalchemy import exc as sa_exc
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi

from dumpbot.ai.client import AIConfigError, AIUnavailable
from dumpbot.db.models import Job, RawMessage
from dumpbot.watchdog import Watchdog
from dumpbot.workers import jobs
from dumpbot.workers.jobs import JobWorker, RetryLater, claim, enqueue


class Recorder:
    """Handler that records payloads and raises the queued exceptions in order."""

    def __init__(self, *errors: BaseException) -> None:
        self.errors = list(errors)
        self.payloads: list[dict[str, Any]] = []

    async def __call__(self, ctx: Any, payload: dict[str, Any]) -> None:
        self.payloads.append(payload)
        if self.errors:
            raise self.errors.pop(0)


class DeadLog:
    def __init__(self) -> None:
        self.jobs: list[Job] = []

    async def __call__(self, ctx: Any, job: Job) -> None:
        self.jobs.append(job)


async def _enqueue(sessionmaker, kind: str = "k", payload: dict | None = None, **kw: Any) -> int:
    async with sessionmaker() as session:
        job_id = await enqueue(session, kind, payload or {}, **kw)
        await session.commit()
    assert job_id is not None
    return job_id


async def test_enqueue_dedupes_by_key(session, frozen) -> None:
    first = await enqueue(session, "message", {"raw_id": 1}, dedupe_key="msg:1")
    second = await enqueue(session, "message", {"raw_id": 1}, dedupe_key="msg:1")
    plain = [await enqueue(session, "x", {}), await enqueue(session, "x", {})]
    await session.commit()
    assert first is not None and second is None
    assert None not in plain and plain[0] != plain[1]
    job = await session.get(Job, first)
    assert (job.status, job.attempts, job.run_after) == ("queued", 0, frozen.now)


async def test_concurrent_claims_never_share_a_job(sessionmaker, frozen) -> None:
    ids = [await _enqueue(sessionmaker, payload={"n": i}) for i in range(6)]
    async with sessionmaker() as s1, sessionmaker() as s2:
        a = await claim(s1, frozen.now, limit=3)
        b = await claim(s2, frozen.now, limit=10)  # s1 still holds its row locks: they are skipped
        await s1.commit()
        await s2.commit()
    got_a, got_b = {j.id for j in a}, {j.id for j in b}
    assert len(got_a) == 3 and len(got_b) == 3
    assert not got_a & got_b
    assert got_a | got_b == set(ids)
    assert all(j.status == "running" and j.attempts == 1 for j in [*a, *b])
    assert all(j.locked_until == frozen.now + jobs.DEFAULT_LOCK for j in a)


async def test_claim_respects_run_after_and_reclaims_expired_locks(sessionmaker, frozen) -> None:
    future = await _enqueue(sessionmaker, run_after=frozen.now + timedelta(minutes=1))
    stale = await _enqueue(sessionmaker)
    fresh = await _enqueue(sessionmaker)
    async with sessionmaker() as session:
        await session.execute(
            update(Job).where(Job.id == stale).values(status="running", locked_until=frozen.now - timedelta(seconds=1))
        )
        await session.execute(
            update(Job).where(Job.id == fresh).values(status="running", locked_until=frozen.now + timedelta(minutes=1))
        )
        await session.commit()
        claimed = await claim(session, frozen.now)
        await session.commit()
    assert [j.id for j in claimed] == [stale]
    assert future not in {j.id for j in claimed}


async def test_success_marks_done(ctx, sessionmaker, get) -> None:
    job_id = await _enqueue(sessionmaker, "ok", {"a": 1})
    handler = Recorder()
    assert await JobWorker(ctx, {"ok": handler}, on_dead=None).run_once() == 1
    assert handler.payloads == [{"a": 1}]
    job = await get(Job, job_id)
    assert (job.status, job.attempts, job.locked_until, job.last_error) == ("done", 1, None, None)


async def test_failure_backoff_then_dead_with_on_dead(ctx, sessionmaker, get, frozen) -> None:
    job_id = await _enqueue(sessionmaker, "boom")
    async with sessionmaker() as session:
        await session.execute(update(Job).values(max_attempts=3))
        await session.commit()
    dead = DeadLog()
    worker = JobWorker(
        ctx, {"boom": Recorder(ValueError("secret text"), KeyError("x"), RuntimeError("y"))}, on_dead=dead
    )

    await worker.run_once()
    job = await get(Job, job_id)
    assert (job.status, job.attempts) == ("queued", 1)
    assert job.run_after == frozen.now + timedelta(minutes=2)
    assert job.last_error.startswith("ValueError at ") and "secret" not in job.last_error

    assert await worker.run_once() == 0  # not due yet
    frozen.advance(minutes=2)
    await worker.run_once()
    job = await get(Job, job_id)
    assert (job.status, job.attempts, job.run_after) == ("queued", 2, frozen.now + timedelta(minutes=4))

    frozen.advance(minutes=4)
    await worker.run_once()
    job = await get(Job, job_id)
    assert (job.status, job.attempts) == ("dead", 3)
    assert [j.id for j in dead.jobs] == [job_id]
    assert dead.jobs[0].status == "dead" and dead.jobs[0].last_error.startswith("RuntimeError")


async def test_retry_later_with_and_without_counting(ctx, sessionmaker, get, frozen) -> None:
    counted = await _enqueue(sessionmaker, "a")
    free = await _enqueue(sessionmaker, "b")
    at = frozen.now + timedelta(days=20)
    worker = JobWorker(
        ctx,
        {
            "a": Recorder(RetryLater(timedelta(minutes=5), "download failed")),
            "b": Recorder(RetryLater(at=at, reason="budget exhausted", count_attempt=False)),
        },
        on_dead=None,
    )
    await worker.run_once()
    a, b = await get(Job, counted), await get(Job, free)
    assert (a.status, a.attempts, a.run_after, a.last_error) == (
        "queued",
        1,
        frozen.now + timedelta(minutes=5),
        "download failed",
    )
    assert (b.status, b.attempts, b.run_after, b.last_error) == ("queued", 0, at, "budget exhausted")


async def test_counted_retry_later_goes_dead_at_max_attempts(ctx, sessionmaker, get) -> None:
    job_id = await _enqueue(sessionmaker, "a")
    async with sessionmaker() as session:
        await session.execute(update(Job).values(max_attempts=1))
        await session.commit()
    dead = DeadLog()
    await JobWorker(ctx, {"a": Recorder(RetryLater(timedelta(minutes=5), "nope"))}, on_dead=dead).run_once()
    assert (await get(Job, job_id)).status == "dead"
    assert len(dead.jobs) == 1


async def test_ai_unavailable_backs_off_respects_retry_after_and_never_dies(ctx, sessionmaker, get, frozen) -> None:
    job_id = await _enqueue(sessionmaker, "ai")
    async with sessionmaker() as session:
        await session.execute(update(Job).values(max_attempts=2))
        await session.commit()
    dead = DeadLog()
    handler = Recorder(
        AIUnavailable("503 no instances", retry_after=600),
        AIUnavailable("timeout"),
        AIUnavailable("timeout"),
    )
    worker = JobWorker(ctx, {"ai": handler}, on_dead=dead)

    await worker.run_once()
    job = await get(Job, job_id)
    assert job.status == "queued"
    assert job.run_after == frozen.now + timedelta(minutes=10)  # Retry-After beats the 2 min backoff
    assert job.last_error == "AIUnavailable: 503 no instances"

    for _ in range(2):
        frozen.set(job.run_after)
        await worker.run_once()
        job = await get(Job, job_id)
    assert job.status == "queued" and not dead.jobs
    assert job.attempts == 1  # capped below max_attempts; each claim makes it 2 again -> 4 min backoff
    assert job.run_after == frozen.now + timedelta(minutes=4)


async def test_ai_unavailable_for_a_week_gives_up(ctx, sessionmaker, get, frozen) -> None:
    job_id = await _enqueue(sessionmaker, "ai")
    frozen.advance(days=8)
    dead = DeadLog()
    await JobWorker(ctx, {"ai": Recorder(AIUnavailable("down"))}, on_dead=dead).run_once()
    assert (await get(Job, job_id)).status == "dead"
    assert len(dead.jobs) == 1


async def test_other_ai_errors_use_generic_backoff(ctx, sessionmaker, get, frozen) -> None:
    job_id = await _enqueue(sessionmaker, "ai")
    await JobWorker(ctx, {"ai": Recorder(AIConfigError("401 invalid token"))}, on_dead=None).run_once()
    job = await get(Job, job_id)
    assert job.status == "queued" and job.last_error == "AIConfigError: 401 invalid token"


async def test_unknown_kind_is_dead(ctx, sessionmaker, get) -> None:
    job_id = await _enqueue(sessionmaker, "mystery")
    dead = DeadLog()
    await JobWorker(ctx, {}, on_dead=dead).run_once()
    job = await get(Job, job_id)
    assert job.status == "dead" and "no handler" in job.last_error
    assert len(dead.jobs) == 1


async def test_crash_looping_job_is_dead_without_running(ctx, sessionmaker, get, frozen) -> None:
    job_id = await _enqueue(sessionmaker, "k")
    async with sessionmaker() as session:  # it was claimed max_attempts times and the process died each time
        await session.execute(
            update(Job).values(status="running", attempts=10, locked_until=frozen.now - timedelta(seconds=1))
        )
        await session.commit()
    handler = Recorder()
    dead = DeadLog()
    await JobWorker(ctx, {"k": handler}, on_dead=dead).run_once()
    assert handler.payloads == []
    assert (await get(Job, job_id)).status == "dead"
    assert len(dead.jobs) == 1


async def test_default_on_dead_notifies_owner_and_reacts(ctx, sessionmaker, get, add_raw, bot) -> None:
    raw = await add_raw("что-то", message_id=321)
    job_id = await _enqueue(sessionmaker, "message", {"raw_id": raw.id})
    async with sessionmaker() as session:
        await session.execute(update(Job).values(max_attempts=1))
        await session.commit()
    await JobWorker(ctx, {"message": Recorder(ValueError("x"))}).run_once()  # default on_dead = processor's

    assert (await get(Job, job_id)).status == "dead"
    assert len(bot.sent) == 1
    assert "Не смог обработать сообщение" in bot.texts[0]
    assert bot.sent[0]["reply_parameters"].message_id == 321
    assert bot.reactions == [(321, "😢")]
    stored = await get(RawMessage, raw.id)
    assert stored.status == "failed" and stored.error.startswith("ValueError")


async def test_run_loop_wakes_survives_errors_and_cancels(ctx, sessionmaker, get) -> None:
    done = asyncio.Event()
    calls: list[str] = []

    async def bad(ctx_: Any, payload: dict) -> None:
        calls.append("bad")
        raise RuntimeError("handler bug")

    async def good(ctx_: Any, payload: dict) -> None:
        calls.append("good")
        done.set()

    worker = JobWorker(ctx, {"bad": bad, "good": good}, poll_interval_s=60, on_dead=None)
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(0.05)
    await _enqueue(sessionmaker, "bad")
    good_id = await _enqueue(sessionmaker, "good")
    ctx.jobs_wakeup.set()
    await asyncio.wait_for(done.wait(), 5)
    assert calls == ["bad", "good"]
    for _ in range(100):  # the outcome is recorded right after the handler returns
        if (await get(Job, good_id)).status == "done":
            break
        await asyncio.sleep(0.02)
    assert (await get(Job, good_id)).status == "done"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_loop_reports_progress_to_the_watchdog(ctx, sessionmaker) -> None:
    clock = [0.0]
    ctx.watchdog = Watchdog(monotonic=lambda: clock[0])
    release = asyncio.Event()
    started = asyncio.Event()

    async def hangs(ctx_: Any, payload: dict) -> None:
        started.set()
        await release.wait()

    task = asyncio.create_task(JobWorker(ctx, {"hangs": hangs}, poll_interval_s=60, on_dead=None).run())
    await asyncio.sleep(0.05)
    await _enqueue(sessionmaker, "hangs")
    ctx.jobs_wakeup.set()
    await asyncio.wait_for(started.wait(), 5)
    clock[0] += jobs.STALL_AFTER.total_seconds()
    assert ctx.watchdog.stalled() == {}
    clock[0] += 1
    assert ctx.watchdog.stalled() == {"jobs": jobs.STALL_AFTER.total_seconds() + 1}  # a job hung for 30 min

    release.set()  # the job ends: the worker beats again
    for _ in range(100):
        if not ctx.watchdog.stalled():
            break
        await asyncio.sleep(0.02)
    assert ctx.watchdog.stalled() == {}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancel_mid_job_releases_it(ctx, sessionmaker, get) -> None:
    started = asyncio.Event()

    async def slow(ctx_: Any, payload: dict) -> None:
        started.set()
        await asyncio.sleep(60)

    job_id = await _enqueue(sessionmaker, "slow")
    task = asyncio.create_task(JobWorker(ctx, {"slow": slow}, on_dead=None).run_once())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    job = await get(Job, job_id)
    assert (job.status, job.attempts, job.locked_until) == ("queued", 0, None)


async def test_requeue_deferred(session, add_raw, frozen) -> None:
    deferred = await add_raw("a", status="deferred")
    deferred_album = await add_raw("b", status="deferred", media_group_id="G")
    normal = await add_raw("c")
    later = frozen.now + timedelta(days=25)
    await enqueue(session, "message", {"raw_id": deferred.id}, run_after=later)
    await enqueue(session, "album", {"media_group_id": "G", "chat_id": deferred_album.chat_id}, run_after=later)
    await enqueue(session, "message", {"raw_id": normal.id}, run_after=later)
    budget_job = await enqueue(session, "edit_time", {"item_id": 1, "text": "x"}, run_after=later)
    await session.execute(update(Job).where(Job.id == budget_job).values(last_error=jobs.BUDGET_REASON))
    await session.commit()

    moved = await jobs.requeue_deferred(session, frozen.now)
    await session.commit()
    assert moved == 3
    rows = (await session.scalars(select(Job).order_by(Job.id))).all()
    assert [j.run_after == frozen.now for j in rows] == [True, True, False, True]


def test_describe_error_is_content_free() -> None:
    try:
        raise ValueError("Купить молоко у Оли")
    except ValueError as e:
        text = jobs.describe_error(e)
    assert text.startswith("ValueError at test_jobs.py:")
    assert "молоко" not in text
    assert jobs.describe_error(AIUnavailable("x bot123:ABC-def y")) == "AIUnavailable: x bot<token> y"
    assert jobs.describe_error(RetryLater(reason="budget exhausted")) == "RetryLater: budget exhausted"


def _db_error(statement: str, params: Any, orig: BaseException) -> sa_exc.DBAPIError:
    """The error SQLAlchemy raises around a driver error, raised here so it has a traceback."""
    try:
        raise sa_exc.DBAPIError.instance(statement, params, orig, AsyncAdapt_asyncpg_dbapi.Error) from orig
    except sa_exc.DBAPIError as e:
        return e


def test_describe_error_names_the_database_error_without_its_text() -> None:
    # As SQLAlchemy's asyncpg adapter does it: its own DB-API error with the SQLSTATE, raised from asyncpg's error.
    driver = asyncpg.exceptions.UndefinedTableError('relation "Купить молоко" does not exist')
    try:
        raise AsyncAdapt_asyncpg_dbapi.ProgrammingError(f"{type(driver)}: {driver}") from driver
    except AsyncAdapt_asyncpg_dbapi.ProgrammingError as adapted:
        adapted.sqlstate = driver.sqlstate
        orig = adapted
    e = _db_error('SELECT * FROM "Купить молоко" WHERE title = $1', ("у Оли",), orig)
    assert isinstance(e, sa_exc.ProgrammingError)

    described = jobs.describe_error(e)
    assert re.fullmatch(r"ProgrammingError \(UndefinedTableError 42P01\) at test_jobs\.py:\d+", described), described
    assert "молоко" not in described and "Оли" not in described and "relation" not in described


def _adapted(cls: type[Exception], message: str, *, sqlstate: Any, cause: BaseException | None) -> BaseException:
    try:
        raise cls(message) from cause
    except cls as e:
        e.sqlstate = sqlstate  # type: ignore[attr-defined]
        return e


def test_describe_error_of_other_database_errors() -> None:
    dbapi = AsyncAdapt_asyncpg_dbapi
    # The server went away: the code is on the adapter's error.
    shutdown = _adapted(dbapi.InternalServerError, "terminating connection", sqlstate="57P01", cause=None)
    assert jobs.describe_error(_db_error("SELECT 1", None, shutdown)).startswith(
        "InternalError (InternalServerError 57P01) at test_jobs.py:"
    )
    # A client-side asyncpg error has no SQLSTATE.
    busy = asyncpg.exceptions.InterfaceError("cannot perform operation: another operation is in progress")
    adapted = _adapted(dbapi.InterfaceError, str(busy), sqlstate=None, cause=busy)
    assert jobs.describe_error(_db_error("SELECT 1", None, adapted)).startswith(
        "InterfaceError (InterfaceError) at test_jobs.py:"
    )
    # Only a real five-character code is shown.
    odd = _adapted(dbapi.Error, "x", sqlstate="42P01; DROP TABLE items", cause=None)
    assert jobs.describe_error(_db_error("SELECT 1", None, odd)).startswith("DBAPIError (Error) at test_jobs.py:")


async def test_describe_error_of_a_real_missing_table(session) -> None:
    with pytest.raises(sa_exc.DBAPIError) as caught:
        await session.execute(text("SELECT * FROM no_such_table_молоко"))
    described = jobs.describe_error(caught.value)
    assert described.startswith("ProgrammingError (UndefinedTableError 42P01) at "), described
    assert "no_such_table" not in described


def test_backoff_is_capped() -> None:
    assert jobs.backoff(1) == timedelta(minutes=2)
    assert jobs.backoff(5) == timedelta(minutes=32)
    assert jobs.backoff(9) == jobs.backoff(500) == timedelta(hours=6)


async def _a_cancel_turned_into_a_database_error() -> int:
    """What SQLAlchemy may do when the task is cancelled mid-query: its own error instead of ``CancelledError``."""
    asyncio.current_task().cancel()  # type: ignore[union-attr]
    try:
        await asyncio.sleep(1)
    except asyncio.CancelledError:
        raise sa_exc.ResourceClosedError("This result object is closed.") from None
    return 0


async def test_a_cancel_the_database_swallowed_still_stops_the_worker(ctx, monkeypatch) -> None:
    """The shutdown cancels the worker while it waits for a query: the cancel must not get lost in the loop's
    ``except Exception`` (the shutdown would wait for ever)."""
    worker = JobWorker(ctx, {}, poll_interval_s=60)
    monkeypatch.setattr(worker, "run_once", _a_cancel_turned_into_a_database_error)
    running = asyncio.create_task(worker.run())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 2)
