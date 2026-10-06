"""Inline keyboard building blocks shared by ``bot.views`` (pure, no I/O)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Final, Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from dumpbot.bot.cbdata import DIGEST_DAY_FMT, DigestCb, ItemCb, OccCb

# Button colours (Bot API 9.4); clients that do not support them show a normal button.
Style = Literal["success", "danger", "primary"]

BUTTON_TEXT_MAX: Final = 64  # longer labels get cut by clients anyway

BACK = "← Назад"


def clip_label(text: str, limit: int = BUTTON_TEXT_MAX) -> str:
    """Button labels are plain text (no HTML); keep them short and single-line."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def button(text: str, cb: CallbackData | str, *, style: Style | None = None) -> InlineKeyboardButton:
    data = cb if isinstance(cb, str) else cb.pack()  # pack() raises ValueError when data > 64 bytes
    return InlineKeyboardButton(text=clip_label(text), callback_data=data, style=style)


def markup(*rows: Sequence[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    """Keyboard from rows; empty rows are dropped."""
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def chunked(buttons: Iterable[InlineKeyboardButton], per_row: int) -> list[list[InlineKeyboardButton]]:
    items = list(buttons)
    return [items[i : i + per_row] for i in range(0, len(items), per_row)]


def item_card_keyboard(
    item_id: int,
    *,
    occurrence_id: int | None,
    actionable: bool,
    done: bool,
    has_reminders: bool,
    recurring: bool,
) -> InlineKeyboardMarkup:
    """[↩️ Не сделано] / [✏️ Изменить] [🔕 Без напоминания] [🗑 Удалить] / [⏹ Стоп повтор].

    No [✅ Готово] here: the card is the "saved" confirmation, and a big done button right under it gets in the way.
    Done lives on reminder messages and in the edit menu.
    """
    first: list[InlineKeyboardButton] = []
    if actionable and done and occurrence_id is not None:
        first.append(button("↩️ Не сделано", OccCb(a="undone", id=occurrence_id)))
    second = [button("✏️ Изменить", ItemCb(a="edit", id=item_id))]
    if has_reminders:
        second.append(button("🔕 Без напоминания", ItemCb(a="norem", id=item_id)))
    second.append(button("🗑 Удалить", ItemCb(a="del", id=item_id), style="danger"))
    third = [button("⏹ Стоп повтор", ItemCb(a="stoprec", id=item_id))] if recurring else []
    return markup(first, second, third)


def reminder_keyboard(occurrence_id: int) -> InlineKeyboardMarkup:
    """[✅ Готово] / [⏰ 15 мин] [⏰ 1 час] [📅 Завтра]."""
    return markup(
        [button("✅ Готово", OccCb(a="done", id=occurrence_id), style="success")],
        [
            button("⏰ 15 мин", OccCb(a="s15", id=occurrence_id)),
            button("⏰ 1 час", OccCb(a="s60", id=occurrence_id)),
            button("📅 Завтра", OccCb(a="tmr", id=occurrence_id)),
        ],
    )


def done_only_keyboard(occurrence_id: int) -> InlineKeyboardMarkup:
    return markup([button("✅ Готово", OccCb(a="done", id=occurrence_id), style="success")])


def undone_keyboard(occurrence_id: int) -> InlineKeyboardMarkup:
    return markup([button("↩️ Не сделано", OccCb(a="undone", id=occurrence_id))])


def back_to_card_row(item_id: int) -> list[InlineKeyboardButton]:
    return [button(BACK, ItemCb(a="back", id=item_id))]


def digest_move_keyboard(day: date) -> InlineKeyboardMarkup:
    """[📅 Перенести невыполненное на завтра] for the evening digest of local ``day``."""
    cb = DigestCb(a="tmr", d=day.strftime(DIGEST_DAY_FMT))
    return markup([button("📅 Перенести невыполненное на завтра", cb, style="primary")])
