"""Speed (docs/features/iteration-4.md §7): the database waits between "job claimed" and "card sent", with the
database 150 ms away (``SlowDatabase`` in conftest.py: the bot on Render, the data on Supabase).

Before this iteration a text message with a time cost ~50 round trips there (7–10 s here): a pre-ping on every
session, ``BEGIN`` / ``COMMIT`` around every read, the prefs loaded again for every message sent, the related
candidates in two queries (three with a match), the card's extras in three, the ai_usage row in a transaction of its
own. Now, with the connections warm (their prepared statements cached):

- the job starts with the raw the capture router handed over and the prefs loaded within the last minute;
- one wait for the budget check and the related candidates (one statement), side by side;
- the save: ``BEGIN``, one ``INSERT`` per table for all items, ``COMMIT``;
- ``bot_messages``, the raw's "done", the ai_usage rows and the job's outcome come after the card.

The waits are counted by the proxy (round trips started within half a round trip of each other are one wait), the
database time by the job's meter (``db.meter``: wall time with a database call under way).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from dumpbot.db import meter
from dumpbot.db.models import AiUsage, BotMessage, Job, RawMessage

RTT = 0.15  # Render -> Supabase
TARGET_S = 1.0  # database time before the card (the spec's goal)
WAITS = 6  # 6 x 150 ms = 0.9 s
OGG = b"OggS\x00\x02" + bytes(range(64)) * 3
TELEGRAM_DOWNLOAD_S = 0.2  # a voice note from Telegram's file server


@pytest.fixture
def db_rtt_s() -> float:
    return RTT


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch) -> list[meter.Meter]:
    """The meters of the jobs run, traced (every database call with its times)."""
    out: list[meter.Meter] = []
    real = meter.measure

    @contextmanager
    def traced(*, trace: bool = False) -> Iterator[meter.Meter]:
        with real(trace=True) as m:
            out.append(m)
            yield m

    monkeypatch.setattr(meter, "measure", traced)
    return out


@dataclass
class Run:
    card: Any
    job: meter.Meter
    db: Any  # the proxy (``SlowDatabase``)
    since: float  # where the waits are counted from: the job's start, unless the test says otherwise (``after``)

    @property
    def waits(self) -> int:
        """Database waits one after another between ``since`` and the card."""
        return int(self.db.waits(self.since, self.card.at))

    @property
    def db_s(self) -> float:
        """Wall time with a database call of the job under way between ``since`` and the card."""
        return db_time(self.job, self.since, self.card.at)

    @property
    def wall_s(self) -> float:
        return float(self.card.at - self.job.started)

    def after(self, since: float) -> Run:
        return Run(card=self.card, job=self.job, db=self.db, since=since)

    def __str__(self) -> str:
        t0 = self.job.started
        calls = ", ".join(f"{c.what} {c.start - t0:.2f}-{c.end - t0:.2f}" for c in self.job.trace or [])
        return (
            f"card after {self.wall_s:.2f} s; from {self.since - t0:.2f} s: {self.waits} waits, db {self.db_s:.2f} s"
            f" [{calls}]"
        )


async def run(e2e: Any, engine: AsyncEngine, jobs: list[meter.Meter], push: Callable[[], int]) -> Run:
    """Push a message, wait for its card and for the job to end (asking the database directly, not through the app's
    pool and the proxy: the app's connections stay its own)."""
    before = len(jobs)
    message_id = push()
    card = await e2e.card_for(message_id)
    await e2e.reaction(message_id, "👌")
    await settled(engine)
    job = jobs[before]
    return Run(card=card, job=job, db=e2e.db, since=job.started)


async def settled(engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(engine)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        async with sessions() as s:
            busy = (await s.scalars(select(Job.id).where(Job.status.in_(("running", "queued"))))).all()
        if not busy:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the job did not end")


def db_time(job: meter.Meter, since: float, until: float) -> float:
    """Wall time with a database call of the job under way in [since, until]."""
    spans = sorted(
        (max(c.start, since), min(c.end, until)) for c in job.trace or [] if c.start < until and c.end > since
    )
    total, end = 0.0, float("-inf")
    for a, b in spans:
        if b > end:
            total += b - max(a, end)
            end = b
    return total


def task(*titles: str, at: str = "18:00") -> dict[str, Any]:
    when = {"text": f"в {at}", "time": at}
    return {"intent": "save", "items": [{"kind": "task", "title": t, "when": when} for t in titles]}


def text(e2e: Any, words: str, extraction: dict[str, Any]) -> Callable[[], int]:
    e2e.ai.script(words, extraction)
    return lambda: e2e.tg.push_message(words)


async def warm_up(e2e: Any, engine: AsyncEngine, jobs: list[meter.Meter]) -> None:
    """The first messages connect and prepare the statements (a statement new to a connection costs one more round
    trip); the bot in production runs warm."""
    for n in range(2):
        await run(e2e, engine, jobs, text(e2e, f"Разминка {n}", task(f"Разминка {n}")))


async def test_a_task_card_after_six_database_waits(e2e: Any, engine: AsyncEngine, jobs: list[meter.Meter]) -> None:
    await warm_up(e2e, engine, jobs)
    for title in ("Позвонить маме", "Записаться к стоматологу", "Полить цветы"):
        result = await run(e2e, engine, jobs, text(e2e, f"{title} в 18", task(title)))
        print(f"\n{title}: {result}")
        assert f"<b>{title}</b>" in result.card.text
        assert result.waits <= WAITS, result
        assert result.waits * RTT <= TARGET_S
        assert result.db_s <= TARGET_S, result

    # what waited for the card is all there now
    async with async_sessionmaker(engine)() as s:
        raws = (await s.scalars(select(RawMessage))).all()
        assert {r.status for r in raws} == {"done"}
        assert len((await s.scalars(select(AiUsage))).all()) == len(raws)
        assert len((await s.scalars(select(BotMessage))).all()) == len(raws)
        assert {j.status for j in (await s.scalars(select(Job))).all()} == {"done"}


async def test_several_items_cost_no_more_waits(e2e: Any, engine: AsyncEngine, jobs: list[meter.Meter]) -> None:
    await warm_up(e2e, engine, jobs)
    # the first ones prepare the 3-row INSERTs (on each of the two connections a job uses)
    for foods in (("молоко", "хлеб", "сыр"), ("чай", "мёд", "лимон"), ("рис", "соль", "масло")):
        words = f"Купить {', '.join(foods)} в 18"
        result = await run(e2e, engine, jobs, text(e2e, words, task(*(f"Купить {f}" for f in foods))))
        print(f"\n{words}: {result}")
    assert len(e2e.tg.replies_to(result.card.reply_to)) == 3  # a card per item
    assert result.waits <= WAITS, result  # one INSERT per table, not per item
    assert result.db_s <= TARGET_S, result


async def test_a_voice_note_reads_the_budget_while_it_downloads(
    e2e: Any, engine: AsyncEngine, jobs: list[meter.Meter]
) -> None:
    await warm_up(e2e, engine, jobs)
    e2e.tg.download_delay_s = TELEGRAM_DOWNLOAD_S
    titles = ("Купить наушники", "Купить батарейки", "Купить зарядку")  # the first one prepares the statements
    for n, title in enumerate(titles, start=1):
        audio, words = OGG + bytes([n]), f"{title} в 18"
        e2e.tg.add_file(f"voice-{n}", audio, f"voice/file_{n}.oga")
        e2e.ai.transcripts[audio] = words
        e2e.ai.script(words, task(title))
        voice = {"file_id": f"voice-{n}", "file_unique_id": f"uv{n}", "duration": 3, "mime_type": "audio/ogg"}
        result = await run(e2e, engine, jobs, lambda voice=voice: e2e.tg.push_message(voice=voice))
        print(f"\n🎤 {words}: {result}")
    # The paid STT call's budget check was read while the file downloaded (Telegram's file server)...
    [budget, *_] = result.job.trace or []
    stt = e2e.ai.stt_at[-1]
    assert budget.what == "SELECT" and budget.end <= stt
    # ... and after the transcript: the related candidates side by side with storing it, then the save; extract()'s
    # budget check reuses the job's read.
    after_stt = result.after(stt)
    assert after_stt.waits <= WAITS, after_stt
    assert after_stt.db_s <= TARGET_S, after_stt
    async with async_sessionmaker(engine)() as s:
        usage = (await s.scalars(select(AiUsage).order_by(AiUsage.id))).all()
        assert [u.purpose for u in usage[-6:]] == ["stt", "extract"] * 3
        stmt = select(RawMessage.transcript).where(RawMessage.content_type == "voice").order_by(RawMessage.id)
        assert (await s.scalars(stmt)).all() == [f"{t} в 18" for t in titles]


async def test_a_reply_to_a_card(e2e: Any, engine: AsyncEngine, jobs: list[meter.Meter]) -> None:
    """An edit (a reply to the card) changes an item with a history: ``items.apply_patch`` locks the item, moves its
    occurrence, plans its reminders again, statement by statement, and the card shows its checklist and related
    items. Not reworked in this iteration: 19 waits warm (~3 s at 150 ms), the start of the job and the reply's
    bookkeeping are the message path's."""
    await warm_up(e2e, engine, jobs)
    first = await run(e2e, engine, jobs, text(e2e, "Позвонить маме в 18", task("Позвонить маме")))
    results: list[Run] = []
    for at in ("19:00", "19:30", "20:00", "20:30"):  # the first ones prepare the statements
        words = f"лучше в {at}"
        when = {"text": f"в {at}", "time": at}
        e2e.ai.script(words, {"intent": "edit", "edit": {"action": "update", "when": when}})
        result = await run(e2e, engine, jobs, lambda words=words: e2e.tg.push_message(words, reply_to=first.card))
        print(f"\n↩️ {words}: {result}")
        assert at in result.card.text
        results.append(result)
    warm = min(results[-2:], key=lambda r: r.waits)
    assert warm.waits <= 19, warm
