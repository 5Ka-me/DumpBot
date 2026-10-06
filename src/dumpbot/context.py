"""Shared runtime dependencies passed to job handlers, the scheduler and bot handlers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from dumpbot.config import Settings
from dumpbot.domain.prefs import Prefs
from dumpbot.services.settings_repo import load_prefs
from dumpbot.watchdog import Watchdog

if TYPE_CHECKING:
    from aiogram import Bot

    from dumpbot.ai.client import ChutesClient
    from dumpbot.ai.stt import RoutedSTT


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

    @property
    def owner_id(self) -> int:
        return self.settings.owner_id

    @property
    def chat_id(self) -> int:
        """Private chat with the owner (same as the owner's user id)."""
        return self.settings.owner_id

    async def prefs(self, session: AsyncSession) -> Prefs:
        return await load_prefs(
            session,
            self.settings.owner_id,
            default_tz=self.settings.default_tz,
            default_budget=self.settings.default_budget_usd,
        )
