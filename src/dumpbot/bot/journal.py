"""Journal middlewares of the bot (docs/features/iteration-4.md §5, ``services.events``).

- ``CommandJournal`` (inner middleware of the commands router): a ``command`` event after a slash command was
  handled — the command's name only, never its arguments (``/search …`` carries message text).
- ``ButtonJournal`` (inner middleware of every callback query handler, registered on the dispatcher): a ``button``
  event after a press was handled — the action of its callback data (``o:done``, ``ld:undo``, ``s:stt_mode``), the
  item / occurrence it names, and as the source the purpose of the bot message it is on (reminder, digest, list,
  card, …). A press whose handler failed is journaled with the error type. A press that moves the item to another
  day (``MOVE_ACTIONS``: [📅 Завтра], [➡️ След. неделя / месяц], a day of the picker) is marked ``"move": true`` —
  the stale-items check counts those (docs/features/iteration-4.md §4).

Both write after the handler (the reply / toast is out already) and never fail the update: ``events.record``
swallows its errors.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final

from aiogram import BaseMiddleware
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Message, TelegramObject

from dumpbot.bot.cbdata import DayCb, ItemCb, KindCb, ListCb, OccCb, PickCb, StaleCb, SubCb, UndoCb
from dumpbot.services import events

if TYPE_CHECKING:
    from dumpbot.context import AppContext

log = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]

NAME_MAX: Final = 32
ACTION_MAX: Final = 32
# Callback data field naming the item / occurrence of a press (classes not listed name neither).
ITEM_FIELD: Final[dict[type[CallbackData], str]] = {
    ItemCb: "id",
    KindCb: "id",
    SubCb: "id",
    PickCb: "item",
    UndoCb: "item",
    ListCb: "i",
    StaleCb: "id",
}
OCC_FIELD: Final[dict[type[CallbackData], str]] = {OccCb: "id", ListCb: "o", DayCb: "id"}
# Presses that move the item to another day (``button_action`` codes): to tomorrow, to the next week / month, to a
# day of the picker. Snoozing by hours is not a move.
MOVE_ACTIONS: Final = frozenset({"o:tmr", "o:nw", "o:nm", "dy"})


class CommandJournal(BaseMiddleware):
    """``command`` event after a command handler of the router it is registered on."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        result = await handler(event, data)
        ctx: AppContext | None = data.get("ctx")
        if ctx is not None and isinstance(event, Message):
            name = command_name(event.text or event.caption or "")
            if name:
                await events.record(ctx.sessionmaker, events.Entry(events.COMMAND, data={"name": name}))
        return result


class ButtonJournal(BaseMiddleware):
    """``button`` event after a callback query handler (whichever router it is in)."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        try:
            result = await handler(event, data)
        except Exception as e:
            await self._record(event, data, error=type(e).__name__)
            raise
        await self._record(event, data)
        return result

    @staticmethod
    async def _record(event: TelegramObject, data: dict[str, Any], *, error: str | None = None) -> None:
        ctx: AppContext | None = data.get("ctx")
        if ctx is None or not isinstance(event, CallbackQuery):
            return
        try:
            entry = button_entry(event, data.get("callback_data"), chat_id=ctx.chat_id, error=error)
        except Exception as e:  # never let the journal break a button
            log.warning("could not describe a button press for the journal: %s", type(e).__name__)
            return
        await events.record(ctx.sessionmaker, entry)


def command_name(text: str) -> str | None:
    """``/today@dump_bot args`` -> ``today``; None when the text is not a command."""
    head = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    if not head.startswith("/") or len(head) < 2:
        return None
    return head[1:].split("@", 1)[0].lower()[:NAME_MAX] or None


def button_entry(
    callback: CallbackQuery, callback_data: Any, *, chat_id: int, error: str | None = None
) -> events.Entry:
    """The ``button`` event of a press: action, ids, the raw callback data (ids and short codes only, by design of
    ``bot.cbdata``) and the bot message it was on (its purpose becomes the source)."""
    raw = callback.data or ""
    item_id = occurrence_id = None
    if isinstance(callback_data, CallbackData):
        action = button_action(callback_data)
        item_id = _int_field(callback_data, ITEM_FIELD.get(type(callback_data)))
        occurrence_id = _int_field(callback_data, OCC_FIELD.get(type(callback_data)))
    else:  # a button of an older version or unknown data: its first two parts
        action = ":".join(raw.split(":")[:2])
    info: dict[str, Any] = {"action": action[:ACTION_MAX], "cb": raw}
    if action in MOVE_ACTIONS and error is None:
        info[events.MOVE] = True
    if error is not None:
        info["error"] = error
    message = callback.message
    return events.Entry(
        events.BUTTON,
        item_id=item_id,
        occurrence_id=occurrence_id,
        data=info,
        message=(chat_id, message.message_id) if message is not None else None,
    )


def button_action(callback_data: CallbackData) -> str:
    """``o:done``, ``i:edit``, ``ld:undo``, ``s:stt_mode``, ``k`` — the prefix with the action code (``a``) or the
    settings key, without ids."""
    parts = [str(callback_data.__prefix__)]
    for name in ("a", "key"):
        value = getattr(callback_data, name, None)
        if isinstance(value, str) and value:
            parts.append(value)
    return ":".join(parts)


def _int_field(callback_data: CallbackData, name: str | None) -> int | None:
    value = getattr(callback_data, name, None) if name else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
