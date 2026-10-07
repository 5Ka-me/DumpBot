"""Fixtures for the items/query service tests (Postgres from tests/conftest.py; needs TEST_DATABASE_URL).

Test files cannot import each other (``--import-mode=importlib``), so helpers are exposed as fixtures.
Reference time: Tue 2026-10-06 10:00 Europe/Warsaw (CEST, UTC+2).
"""

from __future__ import annotations

import itertools
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dumpbot.ai.schemas import ExtractedItem, Extraction
from dumpbot.db.models import Occurrence, RawMessage, Reminder
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import CreatedItem
from dumpbot.services import items

WARSAW = ZoneInfo("Europe/Warsaw")


def _local(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Warsaw wall time -> aware UTC (what the services store and return)."""
    return datetime(year, month, day, hour, minute, tzinfo=WARSAW).astimezone(UTC)


@pytest.fixture
def local() -> Callable[..., datetime]:
    return _local


@pytest.fixture
def now() -> datetime:
    return _local(2026, 10, 6, 10, 0)


@pytest.fixture
def new_raw(session: AsyncSession) -> Callable[..., Awaitable[RawMessage]]:
    message_ids = itertools.count(100)

    async def _new_raw(text: str = "", tg_date: datetime | None = None) -> RawMessage:
        raw = RawMessage(
            chat_id=42,
            message_id=next(message_ids),
            content_type="text",
            text=text,
            tg_date=tg_date or _local(2026, 10, 6, 10, 0),
        )
        session.add(raw)
        await session.flush()
        return raw

    return _new_raw


@pytest.fixture
def make(session: AsyncSession, prefs: Prefs) -> Callable[..., Awaitable[list[CreatedItem]]]:
    """``await make({"kind": "task", "title": "…", "when": {...}}, now=…)`` -> committed CreatedItems."""

    async def _make(
        *specs: dict[str, Any],
        now: datetime,
        source: str = "text",
        urls: tuple[str, ...] = (),
        raw: RawMessage | None = None,
        source_text: str | None = None,
        prefs_: Prefs | None = None,
        related_allowed: Collection[int] = (),
    ) -> list[CreatedItem]:
        created = await items.create_items(
            session,
            raw=raw,
            extraction=Extraction(items=[ExtractedItem.model_validate(s) for s in specs]),
            source=source,
            source_text=source_text,
            source_meta=None,
            urls=list(urls),
            prefs=prefs_ or prefs,
            now=now,
            model="test-model",
            prompt_version="v1",
            related_allowed=related_allowed,
        )
        await session.commit()
        return created

    return _make


@pytest.fixture
def reminders_of(session: AsyncSession) -> Callable[..., Awaitable[list[tuple[datetime, str, int, bool]]]]:
    """All reminders of an occurrence as (fire_at, status, lead_minutes, auto), by fire time."""

    async def _reminders_of(occurrence_id: int, status: str | None = None) -> list[tuple[datetime, str, int, bool]]:
        stmt = select(Reminder).where(Reminder.occurrence_id == occurrence_id)
        if status is not None:
            stmt = stmt.where(Reminder.status == status)
        rows = (await session.scalars(stmt.order_by(Reminder.fire_at, Reminder.id))).all()
        return [(r.fire_at, r.status, r.lead_minutes, r.auto) for r in rows]

    return _reminders_of


@pytest.fixture
def occurrences_of(session: AsyncSession) -> Callable[..., Awaitable[list[Occurrence]]]:
    """All occurrences of an item, by due time."""

    async def _occurrences_of(item_id: int) -> list[Occurrence]:
        stmt = select(Occurrence).where(Occurrence.item_id == item_id).order_by(Occurrence.due_at, Occurrence.id)
        return list((await session.scalars(stmt)).all())

    return _occurrences_of
