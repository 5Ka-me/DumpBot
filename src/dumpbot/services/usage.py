"""AI usage log (``ai_usage``) and the monthly budget.

Month boundaries follow the owner's local calendar (``prefs.tz``), so "октябрь" starts at local midnight
on Oct 1, not at UTC midnight.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dumpbot import clock
from dumpbot.db.models import AiUsage
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import BudgetState

__all__ = ["BudgetState", "budget_state", "month_spend", "month_start", "next_month_start", "record_usage"]

log = logging.getLogger(__name__)


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
    """Insert one ``ai_usage`` row in its own session and commit. Never raises: failures are only logged."""
    try:
        row = AiUsage(
            created_at=clock.utcnow(),
            purpose=purpose[:16],
            model=model[:128],
            input_tokens=max(0, int(input_tokens)),
            output_tokens=max(0, int(output_tokens)),
            audio_seconds=Decimal(f"{max(0.0, audio_seconds):.2f}"),
            cost_usd=Decimal(f"{max(0.0, cost_usd):.6f}"),
            ok=ok,
            latency_ms=max(0, int(latency_ms)),
            error=error[:1000] if error else None,
        )
        async with sessionmaker() as session:
            session.add(row)
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
    spent = await month_spend(session, prefs, now)
    limit = float(prefs.budget_usd)
    if limit > 0:
        ratio = spent / limit
    else:
        ratio = math.inf if spent > 0 else 0.0
    return BudgetState(spent_usd=spent, limit_usd=limit, ratio=ratio, exhausted=limit <= 0 or spent >= limit)
