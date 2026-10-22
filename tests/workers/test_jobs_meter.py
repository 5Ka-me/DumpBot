"""The job worker and the speed of a job (docs/features/iteration-4.md §7): an INFO line per job with numbers only,
the AI calls' ai_usage rows written after the job whatever its outcome, one statement to claim a job and one to
record its outcome."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select

from dumpbot.db import meter
from dumpbot.db.models import AiUsage, Job
from dumpbot.services import usage
from dumpbot.workers.jobs import JobWorker, RetryLater, enqueue

SECRET = "позвонить Оле в 18"


async def _enqueue(sessionmaker, kind: str, payload: dict[str, Any]) -> int:
    async with sessionmaker() as session:
        job_id = await enqueue(session, kind, payload)
        await session.commit()
    assert job_id is not None
    return job_id


async def test_every_job_logs_where_its_time_went_without_content(ctx, sessionmaker, caplog) -> None:
    async def handler(ctx: Any, payload: dict[str, Any]) -> None:
        with meter.ai():
            pass
        async with ctx.autocommit() as session:
            await session.execute(select(Job.id))

    await _enqueue(sessionmaker, "message", {"text": SECRET})
    with caplog.at_level(logging.INFO, logger="dumpbot.workers.jobs"):
        await JobWorker(ctx, {"message": handler}, on_dead=None).run_once()
    [line] = [r.getMessage() for r in caplog.records if "queries" in r.getMessage()]
    assert re.fullmatch(r"job \d+ \(message\): ai \d+\.\d\d s, db \d+ queries / \d+\.\d\d s, total \d+\.\d\d s", line)
    assert SECRET not in caplog.text


async def test_the_jobs_usage_rows_are_written_after_it_whatever_the_outcome(ctx, sessionmaker) -> None:
    async def paid(ctx: Any, payload: dict[str, Any]) -> None:
        await usage.record_usage(ctx.sessionmaker, purpose="extract", model="m", cost_usd=0.01)
        async with ctx.sessionmaker() as session:  # nothing written yet: it waits for the reply
            assert (await session.scalars(select(AiUsage))).all() == []
        if payload.get("fail"):
            raise RetryLater(reason="later")

    await _enqueue(sessionmaker, "paid", {})
    await _enqueue(sessionmaker, "paid", {"fail": True})
    await JobWorker(ctx, {"paid": paid}, on_dead=None).run_once()
    async with sessionmaker() as session:
        assert [u.purpose for u in (await session.scalars(select(AiUsage))).all()] == ["extract", "extract"]
        assert sorted(j.status for j in (await session.scalars(select(Job))).all()) == ["done", "queued"]


async def test_claiming_and_recording_a_job_take_one_statement_each(ctx, sessionmaker) -> None:
    meter.install(sessionmaker.kw["bind"])
    await _enqueue(sessionmaker, "ok", {})

    async def ok(ctx: Any, payload: dict[str, Any]) -> None:
        return None

    worker = JobWorker(ctx, {"ok": ok}, on_dead=None)
    with meter.measure(trace=True) as m:
        job = await worker._claim_one()
        assert job is not None
        await worker._record(job.id, status="done")
    assert [c.what for c in m.trace or []] == ["UPDATE jobs", "UPDATE jobs"]  # no BEGIN / COMMIT around them
