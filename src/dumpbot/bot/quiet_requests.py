"""Quiet hours for every outgoing message, not only the ones the scheduler plans.

A sleeping PC freezes the bot; when it wakes at night it catches up on updates queued by Telegram and answers them
(a list for a command sent at 22:46 arrived at 00:50 with a sound). This request middleware makes any message sent
inside the owner's quiet hours silent unless the caller decided explicitly (``disable_notification`` already set):
reminders for a time the user named themselves pass ``False`` and still ring, and so does the job processor, which
looked at the quiet hours itself (``sender.send(quiet_checked=True)``). The prefs are the ones loaded within the last
minute (``AppContext.recent_prefs``) when there are such: a message costs no round trip to the database for this.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.methods.base import Response, TelegramMethod, TelegramType

from dumpbot import clock
from dumpbot.domain import quiet

if TYPE_CHECKING:
    from aiogram import Bot

    from dumpbot.context import AppContext

log = logging.getLogger(__name__)


class QuietHoursRequests(BaseRequestMiddleware):
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        if getattr(method, "disable_notification", False) is None and await self._in_quiet_hours():
            method.disable_notification = True  # type: ignore[attr-defined]
        return await make_request(bot, method)

    async def _in_quiet_hours(self) -> bool:
        prefs = self._ctx.recent_prefs()  # loaded within the last minute: no round trip to the database
        if prefs is not None:
            return quiet.in_quiet_hours(clock.utcnow(), prefs)
        try:
            async with self._ctx.autocommit() as session:
                prefs = await self._ctx.prefs(session)
        except Exception as e:  # never block a message because the DB hiccupped
            log.warning("quiet hours check skipped: %s", type(e).__name__)
            return False
        return quiet.in_quiet_hours(clock.utcnow(), prefs)


def install(ctx: AppContext) -> None:
    """Register on the bot's session (all methods with ``disable_notification`` go through it)."""
    ctx.bot.session.middleware(QuietHoursRequests(ctx))


__all__ = ["QuietHoursRequests", "install"]
