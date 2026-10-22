"""Inline keyboard building blocks shared by ``bot.views`` (pure, no I/O)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import Final, Literal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from dumpbot.bot.cbdata import (
    DIGEST_DAY_FMT,
    OCC_BACK,
    OCC_PICK,
    OCC_SOMEDAY,
    OCC_STOP,
    STALE_DELETE,
    STALE_DONE,
    STALE_KEEP,
    STALE_SOMEDAY,
    DayCb,
    DigestCb,
    ItemCb,
    ListCb,
    OccCb,
    StaleCb,
    SubCb,
    UndoCb,
)
from dumpbot.domain.fmt import WEEKDAYS_SHORT

# Button colours (Bot API 9.4); clients that do not support them show a normal button.
Style = Literal["success", "danger", "primary"]
# Reminder keyboards by the item's type (docs/features/iteration-4.md §3, ``views.reminder_kind``).
ReminderKind = Literal["timed", "span", "series"]

BUTTON_TEXT_MAX: Final = 64  # longer labels get cut by clients anyway

BACK = "← Назад"
UNDO_LABEL: Final = "↩️ Отменить"
STEP_BUTTONS_PER_ROW: Final = 4
LIST_BUTTONS_PER_ROW: Final = 5
DAY_BUTTONS_PER_ROW: Final = 4
STOP_SERIES_LABEL: Final = "⏹ Хватит повторять"
NO_MORE_LABEL: Final = "⏹ Больше не повторять"
OTHER_DAY_LABEL: Final = "Другая дата…"


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
    steps: Sequence[tuple[int, int, bool]] = (),
    split: bool = False,
    related: bool = False,
) -> InlineKeyboardMarkup:
    """[☐ 1] [☑ 2] … / [↩️ Не сделано] / [✏️ Изменить] [🔕 Без напоминания] [🗑 Удалить] / [🪄 Разбить на шаги]
    [🔗 Похожее] / [⏹ Стоп повтор].

    ``steps``: (step item id, number, done) of the checklist; each button asks for the opposite state (SubCb).
    No [✅ Готово] here: the card is the "saved" confirmation, and a big done button right under it gets in the way.
    Done lives on reminder messages and in the edit menu.
    """
    toggles = [
        button(f"{'☑' if is_done else '☐'} {n}", SubCb(a="undo" if is_done else "done", id=step_id))
        for step_id, n, is_done in steps
    ]
    first: list[InlineKeyboardButton] = []
    if actionable and done and occurrence_id is not None:
        first.append(button("↩️ Не сделано", OccCb(a="undone", id=occurrence_id)))
    second = [button("✏️ Изменить", ItemCb(a="edit", id=item_id))]
    if has_reminders:
        second.append(button("🔕 Без напоминания", ItemCb(a="norem", id=item_id)))
    second.append(button("🗑 Удалить", ItemCb(a="del", id=item_id), style="danger"))
    extra: list[InlineKeyboardButton] = []
    if split:
        extra.append(button("🪄 Разбить на шаги", ItemCb(a="split", id=item_id)))
    if related:
        extra.append(button("🔗 Похожее", ItemCb(a="rel", id=item_id)))
    last = [button("⏹ Стоп повтор", ItemCb(a="stoprec", id=item_id))] if recurring else []
    return markup(*chunked(toggles, STEP_BUTTONS_PER_ROW), first, second, extra, last)


def reminder_keyboard(occurrence_id: int, *, kind: ReminderKind = "timed", month: bool = False) -> InlineKeyboardMarkup:
    """[✅ Готово] and a row by the item's type (docs/features/iteration-4.md §3):
    - ``timed`` (a one-off at a time): [⏰ 1 час] [⏰ 3 часа] [📅 Завтра];
    - ``span`` (a week / month / season, "на днях", a deadline): [➡️ След. неделя] ([➡️ След. месяц] with ``month``)
      [📅 Выбрать день] [💤 Когда-нибудь];
    - ``series``: [⏰ 1 час] [⏹ Хватит повторять].
    Reminders sent before had [⏰ 15 мин] / [🌙 Вечером]: those buttons keep working (``OccCb`` ``s15`` / ``eve``)."""
    if kind == "span":
        nxt = "nm" if month else "nw"
        row = [
            button("➡️ След. месяц" if month else "➡️ След. неделя", OccCb(a=nxt, id=occurrence_id)),
            button("📅 Выбрать день", OccCb(a=OCC_PICK, id=occurrence_id)),
            button("💤 Когда-нибудь", OccCb(a=OCC_SOMEDAY, id=occurrence_id)),
        ]
    elif kind == "series":
        row = [
            button("⏰ 1 час", OccCb(a="s60", id=occurrence_id)),
            button(STOP_SERIES_LABEL, OccCb(a=OCC_STOP, id=occurrence_id)),
        ]
    else:
        row = [
            button("⏰ 1 час", OccCb(a="s60", id=occurrence_id)),
            button("⏰ 3 часа", OccCb(a="s180", id=occurrence_id)),
            button("📅 Завтра", OccCb(a="tmr", id=occurrence_id)),
        ]
    return markup([button("✅ Готово", OccCb(a="done", id=occurrence_id), style="success")], row)


def undone_keyboard(occurrence_id: int, *, stop: bool = False) -> InlineKeyboardMarkup:
    """[↩️ Не сделано]; ``stop`` (a done instance of a series that still repeats): [⏹ Больше не повторять] too."""
    row = [button("↩️ Не сделано", OccCb(a="undone", id=occurrence_id))]
    if stop:
        row.append(button(NO_MORE_LABEL, OccCb(a=OCC_STOP, id=occurrence_id)))
    return markup(row)


def day_picker_keyboard(occurrence_id: int, item_id: int, days: Sequence[date]) -> InlineKeyboardMarkup:
    """[📅 Выбрать день]: [пн 26] [вт 27] … (DayCb), [Другая дата…] (the "🕒 Время" text prompt, ItemCb ``time``),
    [← Назад] (the reminder again)."""
    buttons = [
        button(f"{WEEKDAYS_SHORT[d.weekday()]} {d.day}", DayCb(id=occurrence_id, d=d.strftime(DIGEST_DAY_FMT)))
        for d in days
    ]
    return markup(
        *chunked(buttons, DAY_BUTTONS_PER_ROW),
        [button(OTHER_DAY_LABEL, ItemCb(a="time", id=item_id))],
        [button(BACK, OccCb(a=OCC_BACK, id=occurrence_id))],
    )


def stale_keyboard(item_id: int) -> InlineKeyboardMarkup:
    """The stale question (docs/features/iteration-4.md §4): [✅ Сделано] [💤 Когда-нибудь] / [🗑 Удалить] [Оставить]
    (two rows: four labels in one get cut on a phone)."""
    return markup(
        [
            button("✅ Сделано", StaleCb(a=STALE_DONE, id=item_id), style="success"),
            button("💤 Когда-нибудь", StaleCb(a=STALE_SOMEDAY, id=item_id)),
        ],
        [
            button("🗑 Удалить", StaleCb(a=STALE_DELETE, id=item_id), style="danger"),
            button("Оставить", StaleCb(a=STALE_KEEP, id=item_id)),
        ],
    )


def undo_keyboard(raw_id: int, item_id: int) -> InlineKeyboardMarkup:
    """[↩️ Отменить] under an edit applied by description (docs/features/iteration-3.md §3)."""
    return markup([button(UNDO_LABEL, UndoCb(raw=raw_id, item=item_id))])


def back_to_card_row(item_id: int) -> list[InlineKeyboardButton]:
    return [button(BACK, ItemCb(a="back", id=item_id))]


def digest_move_row(day: date) -> list[InlineKeyboardButton]:
    """[📅 Перенести невыполненное на завтра] for the evening digest of local ``day``."""
    cb = DigestCb(a="tmr", d=day.strftime(DIGEST_DAY_FMT))
    return [button("📅 Перенести невыполненное на завтра", cb, style="primary")]


def list_buttons(
    kind: str, day: date | None, lines: Iterable[tuple[str, int, int | None, int, bool]]
) -> list[list[InlineKeyboardButton]]:
    """[✅ 1] [✅ 2] … [↩️ 5] under a list, ``LIST_BUTTONS_PER_ROW`` a row, by number. ``lines``: (section, number,
    occurrence id, item id, done) of its numbered lines; a done one offers the undo (ListCb)."""
    d = day.strftime(DIGEST_DAY_FMT) if day is not None else ""
    buttons = [
        button(
            f"{'↩️' if done else '✅'} {n}",
            ListCb(a="undo" if done else "done", k=kind, d=d, s=section, n=n, o=occ_id or 0, i=item_id),
        )
        for section, n, occ_id, item_id, done in sorted(lines, key=lambda line: line[1])
    ]
    return chunked(buttons, LIST_BUTTONS_PER_ROW)
