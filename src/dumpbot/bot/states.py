"""FSM states for short text-input dialogs (MemoryStorage: lost on restart, which is fine).

State data keys:
- edit:     ``item_id``, ``prompt_id`` (the card message turned into the prompt), ``since`` (ISO UTC);
- settings: ``field`` (a ``views.ASK_FIELDS`` key), ``prompt_id`` (the menu message), ``since``.

A prompt older than ``PROMPT_TTL`` no longer captures text: an abandoned "✏️ Название" must not swallow the next
note the owner dumps hours later.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Final

from aiogram.fsm.state import State, StatesGroup

from dumpbot import clock

PROMPT_TTL: Final = timedelta(minutes=15)


class EditStates(StatesGroup):
    time = State()  # new time as text -> job "edit_time"
    title = State()  # new title -> items.set_title
    tags = State()  # new tags -> items.set_tags


class SettingsStates(StatesGroup):
    value = State()  # a typed value for views.ASK_FIELDS[field]


def prompt_data(prompt_id: int | None, **values: Any) -> dict[str, Any]:
    """FSM data for a new prompt (stamped with the current time)."""
    return {"prompt_id": prompt_id, "since": clock.utcnow().isoformat(), **values}


def expired(data: dict[str, Any]) -> bool:
    """True when the prompt is older than PROMPT_TTL (or carries no usable timestamp)."""
    try:
        since = datetime.fromisoformat(str(data.get("since")))
    except ValueError:
        return True
    return clock.utcnow() - since > PROMPT_TTL
