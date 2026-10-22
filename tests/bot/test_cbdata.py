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
    LIST_ACTIONS,
    LIST_KINDS,
    OCC_ACTIONS,
    SNOOZE_ACTIONS,
    SNOOZE_MINUTES,
    STEP_ACTIONS,
    AgainCb,
    AskCb,
    DigestCb,
    ItemCb,
    KindCb,
    ListCb,
    OccCb,
    PickCb,
    SetCb,
    SubCb,
    UndoCb,
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
    # "l" (LeadCb) is retired: never reuse it
    assert prefixes == ["i", "o", "k", "a", "p", "d", "s", "c", "ld", "u", "vr"]
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
        "back",
        "time",
        "title",
        "tags",
        "done",
        "split",
        "rel",
    }


@pytest.mark.parametrize("action", sorted(STEP_ACTIONS))
def test_step_toggles(action: str):
    assert STEP_ACTIONS == {"done", "undo"}
    assert _roundtrip(SubCb(a=action, id=BIG)) == f"c:{action}:{BIG}"


@pytest.mark.parametrize("action", sorted(OCC_ACTIONS))
def test_occurrence_actions(action: str):
    assert _roundtrip(OccCb(a=action, id=BIG)) == f"o:{action}:{BIG}"


def test_snooze_minutes_cover_snooze_actions():
    assert SNOOZE_ACTIONS == OCC_ACTIONS - {"done", "undone"}
    assert SNOOZE_MINUTES == {"s15": 15, "s60": 60, "tmr": None}
    assert SNOOZE_ACTIONS - set(SNOOZE_MINUTES) == {"eve"}  # 🌙 today's evening slot


@pytest.mark.parametrize("action", sorted(LIST_ACTIONS))
@pytest.mark.parametrize("kind", sorted(LIST_KINDS))
def test_list_lines_fit_with_huge_ids(action: str, kind: str):
    assert LIST_ACTIONS == {"done", "undo"} and LIST_KINDS == {"t", "k", "m", "e"}
    packed = _roundtrip(ListCb(a=action, k=kind, d="20261009", s="v", n=20, o=BIG * 1000, i=BIG * 1000))
    assert packed == f"ld:{action}:{kind}:20261009:v:20:{BIG * 1000}:{BIG * 1000}"
    assert _roundtrip(ListCb(a=action, k=kind, d="", s="o", n=1, o=0, i=5)) == f"ld:{action}:{kind}::o:1:0:5"


async def test_list_buttons_match_only_the_list_filter():
    # "ld:…" is neither "l:…" (the retired LeadCb, answered as stale) nor "d:…" (DigestCb)
    data = "ld:done:t:20261009:o:1:0:5"
    assert await ListCb.filter()(_query(data)) == {"callback_data": ListCb.unpack(data)}
    assert await DigestCb.filter()(_query(data)) is False
    assert await ListCb.filter()(_query("d:tmr:20261009")) is False


async def test_undo_button_fits_and_matches_only_its_filter():
    """[↩️ Отменить] of an edit applied by description (iteration 3, §3): the raw message and the item."""
    assert _roundtrip(UndoCb(raw=BIG * 1000, item=BIG * 1000)) == f"u:{BIG * 1000}:{BIG * 1000}"
    data = UndoCb(raw=7, item=12).pack()
    assert await UndoCb.filter()(_query(data)) == {"callback_data": UndoCb(raw=7, item=12)}
    assert await PickCb.filter()(_query(data)) is False


@pytest.mark.parametrize("kind", KINDS)
def test_kind(kind: str):
    assert _roundtrip(KindCb(id=BIG, k=kind)) == f"k:{BIG}:{kind}"


def test_lead_buttons_are_retired():
    # An item's time is its notification time (2026-10-07): no "⏱ Напомнить за…" menu, no lead setting.
    assert "lead" not in ITEM_ACTIONS
    assert all(cls.__prefix__ != "l" for cls in ALL_CALLBACKS)
    assert "lead" not in SETTINGS_MENUS and "lead_minutes" not in ASK_FIELDS


def test_ask_pick_digest():
    assert _roundtrip(AskCb(raw=BIG, n=3)) == f"a:{BIG}:3"
    assert _roundtrip(PickCb(raw=BIG, item=BIG)) == f"p:{BIG}:{BIG}"
    for action in DIGEST_ACTIONS:
        assert _roundtrip(DigestCb(a=action, d="20261006")) == f"d:{action}:20261006"
        assert _roundtrip(DigestCb(a=action)) == f"d:{action}:"


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


def test_again_carries_the_voice_note():
    """[🔁 Ещё раз] under "🎤 Не разобрал голосовое…" (iteration 4, §1)."""
    assert _roundtrip(AgainCb(raw=BIG)) == f"vr:{BIG}"
