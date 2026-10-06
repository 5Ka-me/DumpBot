"""Dispatcher middlewares (outer, on ``dp.update``).

aiogram registers its own update middlewares (errors, user context, FSM) in ``Dispatcher.__init__``, so by the
time these run ``data["event_from_user"]`` is set.

- ``InFlight`` counts updates being handled, so shutdown can let them finish: aiogram does not wait for its
  handler tasks, and a capture cut off before its commit would lose a message Telegram already considers delivered.
- ``OwnerOnly`` drops everything that is not from the owner (setup mode when the owner is not configured).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Final

from aiogram import BaseMiddleware
from aiogram.enums import ChatType, ParseMode
from aiogram.types import Message, TelegramObject, Update, User

log = logging.getLogger(__name__)

SETUP_TEXT: Final = (
    "👋 Твой Telegram ID: <code>{user_id}</code>\n"
    "Впиши его в .env как <code>OWNER_ID={user_id}</code> и перезапусти бота "
    "(<code>docker compose up -d</code>). После этого бот будет отвечать только тебе."
)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class InFlight(BaseMiddleware):
    """Tracks how many updates are being handled; ``wait_idle`` returns when none are."""

    def __init__(self) -> None:
        self.active = 0
        self._idle = asyncio.Event()
        self._idle.set()

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        self.active += 1
        self._idle.clear()
        try:
            return await handler(event, data)
        finally:
            self.active -= 1
            if self.active == 0:
                self._idle.set()

    async def wait_idle(self, limit_s: float) -> bool:
        """Wait up to ``limit_s`` seconds for the running handlers; False if some are still running."""
        try:
            await asyncio.wait_for(self._idle.wait(), limit_s)
        except TimeoutError:
            log.warning("%d update(s) still being handled at shutdown", self.active)
            return False
        return True


class OwnerOnly(BaseMiddleware):
    """Drop every update that is not from the owner.

    With ``owner_id == 0`` (not configured yet) the bot is in setup mode: a ``/start`` in a private chat is
    answered with the sender's id and instructions, everything else is dropped.
    """

    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("event_from_user")
        if self.owner_id == 0:
            await self._setup_mode(event, user)
            return None
        if user is None or user.id != self.owner_id:
            update_id = event.update_id if isinstance(event, Update) else None
            log.info("ignored update %s from user %s", update_id, user.id if user else None)
            return None
        return await handler(event, data)

    async def _setup_mode(self, event: TelegramObject, user: User | None) -> None:
        message = event.message if isinstance(event, Update) else None
        if user is None or message is None or not _is_start(message):
            return
        log.warning("setup mode: /start from user %s; set OWNER_ID and restart", user.id)
        await message.answer(SETUP_TEXT.format(user_id=user.id), parse_mode=ParseMode.HTML)


def _is_start(message: Message) -> bool:
    """``/start`` (optionally ``/start@bot`` or with a deep-link payload) in a private chat."""
    if message.chat.type != ChatType.PRIVATE or not message.text:
        return False
    command = message.text.split(maxsplit=1)[0].split("@", 1)[0]
    return command.lower() == "/start"
