"""bot.journal: what the journal keeps of a command and of a button press (docs/features/iteration-4.md §5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from dumpbot.bot.cbdata import AgainCb, AskCb, DigestCb, ItemCb, KindCb, ListCb, OccCb, PickCb, SetCb, SubCb, UndoCb
from dumpbot.bot.journal import button_action, button_entry, command_name
from dumpbot.services import events

OWNER = 4242


def _callback(data: str, *, message_id: int | None = 77) -> CallbackQuery:
    message = None
    if message_id is not None:
        message = Message(
            message_id=message_id,
            date=datetime(2026, 10, 22, tzinfo=UTC),
            chat=Chat(id=OWNER, type="private"),
            text="🔔 Купить хлеб",
        )
    return CallbackQuery(
        id="1", from_user=User(id=OWNER, is_bot=False, first_name="Дима"), chat_instance="c", data=data, message=message
    )


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("/today", "today"),
        ("/search наушники sony", "search"),  # the arguments are message text: never kept
        ("/Today@dump_bot", "today"),
        ("  /tasks  ", "tasks"),
        ("купить хлеб", None),
        ("/", None),
        ("", None),
    ],
)
def test_command_name(text: str, name: str | None) -> None:
    assert command_name(text) == name


@pytest.mark.parametrize(
    ("cb", "action"),
    [
        (OccCb(a="done", id=5), "o:done"),
        (ItemCb(a="edit", id=5), "i:edit"),
        (ListCb(a="undo", k="m", d="20261022", s="o", n=2, o=9, i=5), "ld:undo"),
        (SetCb(key="stt_mode", v="local"), "s:stt_mode"),
        (KindCb(id=5, k="idea"), "k"),
        (AgainCb(raw=3), "vr"),
        (DigestCb(a="tmr", d="20261022"), "d:tmr"),
    ],
)
def test_button_action(cb, action: str) -> None:
    assert button_action(cb) == action


@pytest.mark.parametrize(
    ("cb", "item_id", "occurrence_id"),
    [
        (OccCb(a="s60", id=9), None, 9),
        (ItemCb(a="del", id=5), 5, None),
        (KindCb(id=5, k="idea"), 5, None),
        (SubCb(a="done", id=6), 6, None),
        (PickCb(raw=3, item=5), 5, None),
        (UndoCb(raw=3, item=5), 5, None),
        (ListCb(a="done", k="t", d="20261022", s="o", n=1, o=9, i=5), 5, 9),
        (ListCb(a="done", k="k", d="", s="o", n=1, o=0, i=5), 5, None),  # 0: no occurrence
        (AskCb(raw=3, n=1), None, None),
        (AgainCb(raw=3), None, None),
    ],
)
def test_button_entry_ids(cb, item_id: int | None, occurrence_id: int | None) -> None:
    entry = button_entry(_callback(cb.pack()), cb, chat_id=OWNER)
    assert (entry.type, entry.item_id, entry.occurrence_id) == (events.BUTTON, item_id, occurrence_id)
    assert entry.data == {"action": button_action(cb), "cb": cb.pack()}
    assert entry.message == (OWNER, 77) and entry.source is None  # the source: that message's purpose


def test_button_entry_of_an_old_button_without_callback_data() -> None:
    entry = button_entry(_callback("l:12:30"), None, chat_id=OWNER, error="ValueError")
    assert entry.data == {"action": "l:12", "cb": "l:12:30", "error": "ValueError"}
    assert (entry.item_id, entry.occurrence_id) == (None, None)


def test_button_entry_without_a_message() -> None:
    assert button_entry(_callback("o:done:1", message_id=None), OccCb(a="done", id=1), chat_id=OWNER).message is None
