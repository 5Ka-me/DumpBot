"""FSM states for short text-input dialogs (MemoryStorage: lost on restart, which is fine).

State data keys:
- edit:     ``item_id``, ``prompt_id`` (the card message turned into the prompt), ``since`` (ISO UTC); a prompt
            opened from a reminder ([Другая дата…] of its day picker) also ``origin`` = ``ORIGIN_REMINDER`` and
            ``occ_id`` (that instance): it turns back into the reminder, and the answer moves that instance;
- settings: ``field`` (a ``views.ASK_FIELDS`` key), ``prompt_id`` (the menu message), ``since``.

Only an answer to the prompt is taken (``bot.routers.common.is_answer``): a reply to the prompt within ``PROMPT_TTL``,
or a plain typed message within ``ANSWER_TTL``. Anything else (a forward, a reply to another message, a "?"
question, a later note) closes the dialog and is saved as usual: an abandoned "✏️ Название" must not swallow the
next thing the owner dumps.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Final

from aiogram.fsm.state import State, StatesGroup

from dumpbot import clock

PROMPT_TTL: Final = timedelta(minutes=15)  # a reply to the prompt message
ORIGIN_REMINDER: Final = "reminder"  # edit prompt data "origin": opened from a reminder message
ANSWER_TTL: Final = timedelta(minutes=5)  # a message that is not a reply


class EditStates(StatesGroup):
    time = State()  # new time as text -> job "edit_time"
    title = State()  # new title -> items.set_title
    tags = State()  # new tags -> items.set_tags


class SettingsStates(StatesGroup):
    value = State()  # a typed value for views.ASK_FIELDS[field]


def prompt_data(prompt_id: int | None, **values: Any) -> dict[str, Any]:
    """FSM data for a new prompt (stamped with the current time)."""
    return {"prompt_id": prompt_id, "since": clock.utcnow().isoformat(), **values}


def expired(data: dict[str, Any], ttl: timedelta = PROMPT_TTL) -> bool:
    """True when the prompt is older than ``ttl`` (or carries no usable timestamp)."""
    try:
        since = datetime.fromisoformat(str(data.get("since")))
    except ValueError:
        return True
    return clock.utcnow() - since > ttl
