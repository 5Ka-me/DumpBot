"""Callback data: round trips and the 64-byte limit."""

from __future__ import annotations

import pytest
from aiogram import F
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, User

from dumpbot.bot.cbdata import (
    ALL_CALLBACKS,
    DIGEST_ACTIONS,
    ITEM_ACTIONS,
    OCC_ACTIONS,
    SNOOZE_MINUTES,
    AskCb,
    DigestCb,
    ItemCb,
    KindCb,
    LeadCb,
    OccCb,
    PickCb,
    SetCb,
)
from dumpbot.bot.views import ASK_FIELDS, SETTINGS_MENUS
from dumpbot.domain.kinds import KINDS

BIG = 10**12


def _roundtrip(cb: CallbackData) -> str:
    packed = cb.pack()
    assert len(packed.encode()) <= 64, packed
    assert type(cb).unpack(packed) == cb
    return packed


def test_prefixes_are_unique_and_short():
    prefixes = [cls.__prefix__ for cls in ALL_CALLBACKS]
    assert prefixes == ["i", "o", "k", "l", "a", "p", "d", "s"]
    assert len(set(prefixes)) == len(prefixes)


@pytest.mark.parametrize("action", sorted(ITEM_ACTIONS))
def test_item_actions(action: str):
    assert _roundtrip(ItemCb(a=action, id=BIG)) == f"i:{action}:{BIG}"


def test_item_actions_match_contract():
    assert ITEM_ACTIONS == {
        "edit",
        "del",
        "undel",
        "norem",
        "stoprec",
        "kind",
        "lead",
        "back",
        "time",
        "title",
        "tags",
        "done",
    }


@pytest.mark.parametrize("action", sorted(OCC_ACTIONS))
def test_occurrence_actions(action: str):
    assert _roundtrip(OccCb(a=action, id=BIG)) == f"o:{action}:{BIG}"


def test_snooze_minutes_cover_snooze_actions():
    assert set(SNOOZE_MINUTES) == OCC_ACTIONS - {"done", "undone"}
    assert SNOOZE_MINUTES == {"s15": 15, "s60": 60, "tmr": None}


@pytest.mark.parametrize("kind", KINDS)
def test_kind(kind: str):
    assert _roundtrip(KindCb(id=BIG, k=kind)) == f"k:{BIG}:{kind}"


@pytest.mark.parametrize("minutes", [0, 5, 1440, 43200])
def test_lead(minutes: int):
    assert _roundtrip(LeadCb(id=BIG, m=minutes)) == f"l:{BIG}:{minutes}"


def test_ask_pick_digest():
    assert _roundtrip(AskCb(raw=BIG, n=3)) == f"a:{BIG}:3"
    assert _roundtrip(PickCb(raw=BIG, item=BIG)) == f"p:{BIG}:{BIG}"
    for action in DIGEST_ACTIONS:
        assert _roundtrip(DigestCb(a=action)) == f"d:{action}"


def test_unpacked_ids_are_ints():
    cb = ItemCb.unpack(f"i:del:{BIG}")
    assert cb.id == BIG and isinstance(cb.id, int)
    assert PickCb.unpack("p:5:7") == PickCb(raw=5, item=7)


def test_set_with_empty_value_roundtrips():
    assert _roundtrip(SetCb(key="close")) == "s:close:"
    assert SetCb.unpack("s:quiet_toggle:") == SetCb(key="quiet_toggle", v="")


@pytest.mark.parametrize("field", sorted(ASK_FIELDS))
def test_set_ask_fields_fit(field: str):
    _roundtrip(SetCb(key="ask", v=field))


@pytest.mark.parametrize("menu", sorted(SETTINGS_MENUS))
def test_set_menus_fit(menu: str):
    _roundtrip(SetCb(key="menu", v=menu))


def test_set_long_timezone_fits():
    _roundtrip(SetCb(key="tz", v="America/Argentina/ComodRivadavia"))


def test_separator_in_value_is_rejected():
    with pytest.raises(ValueError, match="Separator"):
        SetCb(key="slot", v="09:00").pack()


def test_too_long_data_is_rejected():
    with pytest.raises(ValueError, match="too long"):
        SetCb(key="tz", v="x" * 70).pack()


def test_wrong_prefix_is_rejected():
    with pytest.raises(ValueError, match="prefix"):
        ItemCb.unpack("o:done:1")


def _query(data: str) -> CallbackQuery:
    return CallbackQuery(id="1", from_user=User(id=1, is_bot=False, first_name="x"), chat_instance="c", data=data)


async def test_router_filters_match_actions():
    snooze = OccCb.filter(F.a.in_(set(SNOOZE_MINUTES)))
    assert await snooze(_query(f"o:s15:{BIG}")) == {"callback_data": OccCb(a="s15", id=BIG)}
    assert await snooze(_query(f"o:done:{BIG}")) is False
    assert await snooze(_query(f"i:s15:{BIG}")) is False  # other prefix
    assert await SetCb.filter(F.key == "close")(_query("s:close:")) == {"callback_data": SetCb(key="close")}
