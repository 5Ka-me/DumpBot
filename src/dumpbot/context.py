"""Shared runtime dependencies passed to job handlers, the scheduler and bot handlers."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Final

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dumpbot.config import Settings
from dumpbot.db.engine import autocommit_sessionmaker
from dumpbot.domain.prefs import Prefs
from dumpbot.services.settings_repo import load_prefs
from dumpbot.watchdog import Watchdog

if TYPE_CHECKING:
    from aiogram import Bot

    from dumpbot.ai.client import ChutesClient
    from dumpbot.ai.stt import RoutedSTT
    from dumpbot.db.models import RawMessage

PREFS_FRESH_S: Final = 60.0  # how long ``recent_prefs`` trusts the prefs loaded last
FRESH_RAWS_MAX: Final = 32


@dataclass
class AppContext:
    settings: Settings
    sessionmaker: async_sessionmaker[AsyncSession]
    bot: Bot
    ai: ChutesClient
    stt: RoutedSTT  # chutes and the local Whisper, ordered by Prefs.stt_mode
    # Set to make the job worker / scheduler run immediately instead of waiting for their poll interval.
    jobs_wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    scheduler_wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    # The job worker and the scheduler report progress here; ``bot.app.Lifecycle`` exits the process on a stall.
    watchdog: Watchdog = field(default_factory=Watchdog)
    # docs/features/iteration-4.md §7: what the job of a message needs first, kept so that it does not wait for the
    # database to hand it back — the prefs loaded last (``recent_prefs``) and the messages the capture router has
    # just saved (``keep_fresh``).
    _prefs_memo: tuple[Prefs, float] | None = field(default=None, init=False, repr=False, compare=False)
    _prefs_generation: int = field(default=0, init=False, repr=False, compare=False)
    _fresh_raws: dict[int, RawMessage] = field(default_factory=dict, init=False, repr=False, compare=False)

    @cached_property
    def autocommit(self) -> async_sessionmaker[AsyncSession]:
        """Sessions without a transaction (``db.engine.autocommit_sessionmaker``): a read or a single write costs one
        round trip to the database instead of three."""
        return autocommit_sessionmaker(self.sessionmaker)

    @property
    def owner_id(self) -> int:
        return self.settings.owner_id

    @property
    def chat_id(self) -> int:
        """Private chat with the owner (same as the owner's user id)."""
        return self.settings.owner_id

    async def prefs(self, session: AsyncSession) -> Prefs:
        """The owner's prefs from the database (remembered for ``recent_prefs``)."""
        generation = self._prefs_generation
        prefs = await load_prefs(
            session,
            self.settings.owner_id,
            default_tz=self.settings.default_tz,
            default_budget=self.settings.default_budget_usd,
        )
        if generation == self._prefs_generation:  # not loaded before a change that ``forget_prefs`` announced
            self._prefs_memo = (prefs.model_copy(deep=True), time.monotonic())
        return prefs

    def recent_prefs(self, max_age_s: float = PREFS_FRESH_S) -> Prefs | None:
        """A copy of the prefs ``prefs`` loaded last, when that was at most ``max_age_s`` ago (the scheduler loads them
        every tick), else None. For readers that may be a minute behind a change made outside the bot: whoever saves
        prefs calls ``forget_prefs``."""
        memo = self._prefs_memo
        if memo is None or time.monotonic() - memo[1] > max_age_s:
            return None
        return memo[0].model_copy(deep=True)

    def forget_prefs(self) -> None:
        """The prefs were changed: ``recent_prefs`` has nothing until they are loaded again."""
        self._prefs_generation += 1
        self._prefs_memo = None

    def keep_fresh(self, raw: RawMessage) -> None:
        """A message just saved (and committed) with its job queued: the job takes it with ``take_fresh`` instead of
        loading it. At most ``FRESH_RAWS_MAX`` are kept (the oldest go): a job that finds nothing loads its raw."""
        self._fresh_raws[raw.id] = raw
        while len(self._fresh_raws) > FRESH_RAWS_MAX:
            self._fresh_raws.pop(next(iter(self._fresh_raws)))

    def take_fresh(self, raw_id: int) -> RawMessage | None:
        """The message ``keep_fresh`` kept (once: a later run of the job loads it)."""
        return self._fresh_raws.pop(raw_id, None)
