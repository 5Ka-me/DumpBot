"""Small values the bot keeps across restarts (``app_state``, a JSON value per key).

The scheduler keeps the time of its last tick (and of its progress during a long one) here: a tick that finds it far
in the past knows the bot was down (the PC slept, Docker stood still) — docs/features/iteration-3.md §5. A downtime
whose consequences (the folded evening digest, the notice) are not handled yet is kept too, so that a restart in the
middle does not lose it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from dumpbot import clock
from dumpbot.db.models import AppState

__all__ = ["delete", "get", "put", "swap"]


async def get(session: AsyncSession, key: str) -> Any | None:
    """The value stored under ``key``, None when there is none."""
    return await session.scalar(select(AppState.value).where(AppState.key == key))


async def put(session: AsyncSession, key: str, value: Any) -> None:
    """Store ``value`` (JSON) under ``key``. Does not commit."""
    now = clock.utcnow()
    stmt = (
        insert(AppState)
        .values(key=key, value=value, updated_at=now)
        .on_conflict_do_update(index_elements=[AppState.key], set_={"value": value, "updated_at": now})
    )
    await session.execute(stmt)


async def swap(session: AsyncSession, key: str, value: Any) -> Any | None:
    """Store ``value`` under ``key`` and return the value it replaced (None: there was none). The row is locked first,
    so two callers get one value each. Does not commit."""
    old = await session.scalar(select(AppState.value).where(AppState.key == key).with_for_update())
    await put(session, key, value)
    return old


async def delete(session: AsyncSession, key: str) -> None:
    """Remove the value stored under ``key`` (if any). Does not commit."""
    await session.execute(sql_delete(AppState).where(AppState.key == key))
