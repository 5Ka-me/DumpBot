"""AppContext's memory for the job of a message (docs/features/iteration-4.md §7): the prefs loaded last
(``recent_prefs``) and the messages the capture router has just saved (``keep_fresh`` / ``take_fresh``)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from dumpbot import context as context_module
from dumpbot.config import Settings
from dumpbot.context import FRESH_RAWS_MAX, AppContext
from dumpbot.db.models import RawMessage
from dumpbot.domain.prefs import Prefs


@pytest.fixture
def ctx() -> AppContext:
    settings = Settings(_env_file=None, bot_token="123:TEST", chutes_api_key="k", owner_id=1)  # type: ignore[call-arg]
    return AppContext(settings=settings, sessionmaker=None, bot=None, ai=None, stt=None)  # type: ignore[arg-type]


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What ``load_prefs`` finds in the database (``prefs``), with an optional pause (``gate``)."""
    db: dict[str, Any] = {"prefs": Prefs(tz="Europe/Warsaw", budget_usd=5.0), "gate": None}

    async def load_prefs(session: Any, owner_id: int, **kw: Any) -> Prefs:
        if db["gate"] is not None:
            await db["gate"].wait()
        return db["prefs"].model_copy(deep=True)

    monkeypatch.setattr(context_module, "load_prefs", load_prefs)
    return db


async def test_recent_prefs_are_the_ones_loaded_last(ctx: AppContext, stored: dict[str, Any]) -> None:
    assert ctx.recent_prefs() is None
    loaded = await ctx.prefs(None)  # type: ignore[arg-type]
    recent = ctx.recent_prefs()
    assert recent == loaded and recent is not loaded  # a copy: whoever changes it changes only their own
    recent.budget_usd = 99.0
    assert ctx.recent_prefs().budget_usd == 5.0  # type: ignore[union-attr]
    assert ctx.recent_prefs(max_age_s=-1) is None  # too old


async def test_saved_prefs_are_forgotten(ctx: AppContext, stored: dict[str, Any]) -> None:
    await ctx.prefs(None)  # type: ignore[arg-type]
    ctx.forget_prefs()
    assert ctx.recent_prefs() is None


async def test_a_load_that_began_before_a_change_is_not_remembered(ctx: AppContext, stored: dict[str, Any]) -> None:
    stored["gate"] = asyncio.Event()
    loading = asyncio.ensure_future(ctx.prefs(None))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    stored["prefs"] = Prefs(tz="Europe/Warsaw", budget_usd=1.0)
    ctx.forget_prefs()  # the change was saved while the old prefs were on their way
    stored["gate"].set()
    await loading
    assert ctx.recent_prefs() is None
    assert (await ctx.prefs(None)).budget_usd == 1.0  # type: ignore[arg-type]
    assert ctx.recent_prefs().budget_usd == 1.0  # type: ignore[union-attr]


def raw(raw_id: int) -> RawMessage:
    return RawMessage(id=raw_id, chat_id=1, message_id=raw_id, content_type="text", text="x", status="new")


def test_a_fresh_message_is_taken_once(ctx: AppContext) -> None:
    message = raw(7)
    ctx.keep_fresh(message)
    assert ctx.take_fresh(7) is message
    assert ctx.take_fresh(7) is None  # a later run of the job loads it
    assert ctx.take_fresh(8) is None


def test_only_the_newest_fresh_messages_are_kept(ctx: AppContext) -> None:
    for raw_id in range(1, FRESH_RAWS_MAX + 6):
        ctx.keep_fresh(raw(raw_id))
    assert ctx.take_fresh(5) is None
    assert ctx.take_fresh(6) is not None and ctx.take_fresh(FRESH_RAWS_MAX + 5) is not None
