"""Load / save the owner's Prefs (single row in user_settings)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from dumpbot.db.models import UserSettings
from dumpbot.domain.prefs import Prefs


def default_prefs(*, tz: str = "Europe/Warsaw", budget_usd: float = 5.0) -> Prefs:
    return Prefs(tz=tz, budget_usd=budget_usd)


async def load_prefs(
    session: AsyncSession, owner_id: int, *, default_tz: str = "Europe/Warsaw", default_budget: float = 5.0
) -> Prefs:
    """Return the owner's prefs, creating the row with defaults on first use (does not commit)."""
    row = await session.scalar(select(UserSettings).where(UserSettings.owner_id == owner_id))
    if row is None:
        prefs = default_prefs(tz=default_tz, budget_usd=default_budget)
        await session.execute(
            insert(UserSettings)
            .values(owner_id=owner_id, prefs=prefs.model_dump(mode="json"))
            .on_conflict_do_nothing(index_elements=[UserSettings.owner_id])
        )
        return prefs
    return Prefs.model_validate(row.prefs or {})


async def save_prefs(session: AsyncSession, owner_id: int, prefs: Prefs) -> None:
    """Upsert the owner's prefs (does not commit)."""
    data = prefs.model_dump(mode="json")
    await session.execute(
        insert(UserSettings)
        .values(owner_id=owner_id, prefs=data)
        .on_conflict_do_update(index_elements=[UserSettings.owner_id], set_={"prefs": data})
    )
