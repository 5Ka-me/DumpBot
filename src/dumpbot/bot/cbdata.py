"""Inline-button callback data (aiogram ``CallbackData``), packed as ``prefix:field:field`` (<= 64 bytes).

Only ids and short codes go into callback data, never user text. Values must not contain ":" (the separator),
so times are encoded as ``HHMM`` and free text is asked for with a separate message instead.

========  ============================  =============================================================================
Class     Fields                        Meaning
========  ============================  =============================================================================
ItemCb    ``i:<a>:<item id>``           Item card actions (``a``):
                                        ``edit``    open the edit menu (views.edit_menu)
                                        ``del``     soft-delete -> views.deleted_card
                                        ``undel``   restore a deleted item -> views.item_card
                                        ``norem``   cancel all pending reminders (items.remove_reminders)
                                        ``stoprec`` stop the recurrence (items.stop_recurrence)
                                        ``kind``    open the category menu (views.kind_menu)
                                        ``lead``    open the "remind before" menu (views.lead_menu)
                                        ``back``    back to the card (views.item_card)
                                        ``time``    ask for a new time as text (FSM -> job "edit_time")
                                        ``title``   ask for a new title as text (FSM -> items.set_title)
                                        ``tags``    ask for new tags as text (FSM -> items.set_tags)
                                        ``done``    mark the item's current occurrence done; used on cards only
                                                    when the card has no occurrence (otherwise OccCb is used)
OccCb     ``o:<a>:<occurrence id>``     Occurrence actions (cards and reminders): ``done``, ``undone``,
                                        ``s15`` / ``s60`` (snooze 15 / 60 min), ``tmr`` (snooze to tomorrow)
KindCb    ``k:<item id>:<kind>``        Set the item's category; ``k`` is a ``domain.kinds.Kind`` code
LeadCb    ``l:<item id>:<minutes>``     Set the item's "remind N minutes before" (items.set_lead(..., [m]))
AskCb     ``a:<raw id>:<n>``            Answer option ``n`` (index in ``Ask.options``) of a clarifying question
                                        about raw message ``raw`` (-> job "clarify")
PickCb    ``p:<raw id>:<item id>``      The user picked ``item`` among edit candidates for raw message ``raw``
                                        (-> job "edit")
DigestCb  ``d:<a>:<YYYYMMDD>``          Digest actions: ``tmr`` = move the not-done occurrences of the digest's
                                        day ``d`` (already due) to the day after. A digest delayed by quiet hours
                                        arrives the next morning, so the day is in the data; buttons sent before
                                        it was (``d:tmr``) use the local date of the digest message.
SetCb     ``s:<key>:<v>``               Settings menu; every key/value is documented in ``bot.views``.
========  ============================  =============================================================================
"""

from __future__ import annotations

from typing import Final

from aiogram.filters.callback_data import CallbackData

MAX_CALLBACK_BYTES: Final = 64

ITEM_ACTIONS: Final[frozenset[str]] = frozenset(
    {"edit", "del", "undel", "norem", "stoprec", "kind", "lead", "back", "time", "title", "tags", "done"}
)
OCC_ACTIONS: Final[frozenset[str]] = frozenset({"done", "undone", "s15", "s60", "tmr"})
# Snooze actions -> minutes (None = tomorrow, same local time).
SNOOZE_MINUTES: Final[dict[str, int | None]] = {"s15": 15, "s60": 60, "tmr": None}
DIGEST_ACTIONS: Final[frozenset[str]] = frozenset({"tmr"})
DIGEST_DAY_FMT: Final = "%Y%m%d"  # DigestCb.d


class ItemCb(CallbackData, prefix="i"):
    a: str
    id: int


class OccCb(CallbackData, prefix="o"):
    a: str
    id: int


class KindCb(CallbackData, prefix="k"):
    id: int
    k: str


class LeadCb(CallbackData, prefix="l"):
    id: int
    m: int


class AskCb(CallbackData, prefix="a"):
    raw: int
    n: int


class PickCb(CallbackData, prefix="p"):
    raw: int
    item: int


class DigestCb(CallbackData, prefix="d"):
    a: str
    d: str = ""  # the digest's local day, DIGEST_DAY_FMT


class SetCb(CallbackData, prefix="s"):
    key: str
    v: str = ""


ALL_CALLBACKS: Final[tuple[type[CallbackData], ...]] = (ItemCb, OccCb, KindCb, LeadCb, AskCb, PickCb, DigestCb, SetCb)
