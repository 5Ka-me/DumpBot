"""Helpers shared by the routers: card rendering, prompt restore, small text utilities."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from dumpbot.bot import sender, views
from dumpbot.bot.cbdata import ItemCb
from dumpbot.bot.keyboards import button, markup
from dumpbot.bot.states import EditStates, SettingsStates
from dumpbot.domain.recurrence import describe_rrule_ru
from dumpbot.services import items

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import StatesGroup
    from sqlalchemy.ext.asyncio import AsyncSession

    from dumpbot.context import AppContext
    from dumpbot.db.models import Item
    from dumpbot.domain.prefs import Prefs
    from dumpbot.dto import Row

log = logging.getLogger(__name__)

NOT_FOUND: Final = "Запись не найдена"
STALE: Final = "Кнопка устарела"
DELETED: Final = "🗑 Запись удалена"
FAILED: Final = "😢 Не получилось, попробуй ещё раз"
TOAST_MAX: Final = 200  # answerCallbackQuery text limit

RECURRING_TITLE: Final = "🔁 Повторяющиеся"
RECURRING_MAX: Final = 30  # rows (and stop buttons) shown by /recurring
# bot_messages.purpose of the /recurring list: its stop buttons re-render the list instead of a card.
PURPOSE_RECURRING: Final = "recurring"

View = views.View


async def get_item(session: AsyncSession, item_id: int) -> Item:
    """The item or ``items.NotFoundError``."""
    item = await items.get_item(session, item_id)
    if item is None:
        raise items.NotFoundError(f"item {item_id} not found")
    return item


async def card_view(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> View:
    """Fresh card of an item: current occurrence and its pending reminders (deleted -> ``deleted_card``)."""
    if item.status == "deleted":
        return views.deleted_card(item)
    occ = await items.current_occurrence(session, item.id)
    reminders = await items.pending_reminders(session, occ.id) if occ is not None else []
    return views.item_card(item, occ, reminders, prefs, now)


def recurring_view(rows: Sequence[Row], prefs: Prefs, now: datetime) -> View:
    """``/recurring``: numbered active series ("каждый пн") with a [⏹ N. title] stop button each."""
    if not rows:
        return views.rows_list(RECURRING_TITLE, [], prefs, now, empty="Повторяющихся задач нет.")
    rows = list(rows)[:RECURRING_MAX]
    lines = [f"{i}. {views.row_line(r, prefs, now)} — {views.esc(_rule_text(r.rrule))}" for i, r in enumerate(rows, 1)]
    header = f"<b>{views.esc(RECURRING_TITLE)}</b>"
    text = views.fit("\n".join([header, *views.fit_lines(lines, views.MAX_TEXT - 200)]))
    buttons = [[button(f"⏹ {i}. {r.title}", ItemCb(a="stoprec", id=r.item_id))] for i, r in enumerate(rows, 1)]
    return text, markup(*buttons)


def _rule_text(rule: str | None) -> str:
    if not rule:
        return "повтор"
    try:
        return describe_rrule_ru(rule)
    except ValueError:
        return rule


def message_id_of(callback: CallbackQuery) -> int | None:
    """Id of the message the button belongs to (None for inline-mode messages)."""
    return callback.message.message_id if callback.message is not None else None


async def purpose_of(session: AsyncSession, ctx: AppContext, message_id: int | None) -> str | None:
    """``bot_messages.purpose`` of a bot message (card | reminder | digest | recurring …), if recorded."""
    if message_id is None:
        return None
    row = await sender.lookup(session, ctx.chat_id, message_id)
    return row.purpose if row is not None else None


async def answer(callback: CallbackQuery, text: str | None = None) -> None:
    """Best effort: stop the button's spinner (an old query can no longer be answered; that must not stop the
    message from being updated)."""
    try:
        await callback.answer(text[:TOAST_MAX] if text else None)
    except TelegramAPIError as e:
        log.info("callback answer failed: %s", type(e).__name__)


async def show(ctx: AppContext, message_id: int | None, view: View) -> None:
    """Edit a bot message in place (no-op without a message)."""
    if message_id is not None:
        await sender.edit(ctx, message_id, view[0], view[1])


def command_args(message: Message) -> str:
    """Text after the command (``/search  foo bar`` -> ``foo bar``)."""
    parts = (message.text or message.caption or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def is_prompt(state: FSMContext, message_id: int | None, group: type[StatesGroup]) -> bool:
    """True when ``message_id`` is the prompt of the current dialog and that dialog belongs to ``group``."""
    if message_id is None or await state.get_state() not in group:
        return False
    return (await state.get_data()).get("prompt_id") == message_id


async def restore_prompt(
    ctx: AppContext, state: FSMContext, prefs: Prefs, now: datetime, *, keep: int | None = None
) -> bool:
    """Leave the current input dialog: clear the state and turn its prompt message back into the card / menu
    (unless the prompt is message ``keep``, which the caller is about to redraw anyway).

    Returns False when there was no dialog.
    """
    current = await state.get_state()
    if current is None:
        return False
    data = await state.get_data()
    await state.clear()
    prompt_id = data.get("prompt_id")
    if not isinstance(prompt_id, int) or prompt_id == keep:
        return True
    view = await _prompt_origin(ctx, current, data, prefs, now)
    if view is not None:
        await show(ctx, prompt_id, view)
    return True


async def _prompt_origin(
    ctx: AppContext, current: str, data: dict[str, Any], prefs: Prefs, now: datetime
) -> View | None:
    if current in EditStates and isinstance(data.get("item_id"), int):
        async with ctx.sessionmaker() as session:
            item = await items.get_item(session, data["item_id"])
            return await card_view(session, item, prefs, now) if item is not None else None
    if current == SettingsStates.value.state:
        field = views.ASK_FIELDS.get(str(data.get("field")))
        return views.settings_view(field.menu if field else "main", prefs)
    return None
