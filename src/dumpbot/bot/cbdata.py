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
                                        ``back``    back to the card (views.item_card)
                                        ``time``    ask for a new time as text (FSM -> job "edit_time")
                                        ``title``   ask for a new title as text (FSM -> items.set_title)
                                        ``tags``    ask for new tags as text (FSM -> items.set_tags)
                                        ``done``    mark the item's current occurrence done; used on cards only
                                                    when the card has no occurrence (otherwise OccCb is used)
                                        ``split``   🪄 break the task into steps (job "split" -> the AI)
                                        ``rel``     🔗 send the cards of the related items (up to 3)
OccCb     ``o:<a>:<occurrence id>``     Occurrence actions (cards and reminders): ``done``, ``undone``,
                                        ``s15`` / ``s60`` (snooze 15 / 60 min), ``tmr`` (snooze to tomorrow),
                                        ``eve`` (🌙 snooze to today's ``Prefs.slot_evening``; offered while it
                                        is more than an hour away, instead of ``s15``)
KindCb    ``k:<item id>:<kind>``        Set the item's category; ``k`` is a ``domain.kinds.Kind`` code
AskCb     ``a:<raw id>:<n>``            Answer option ``n`` (index in ``Ask.options``) of a clarifying question
                                        about raw message ``raw`` (-> job "clarify")
PickCb    ``p:<raw id>:<item id>``      The user picked ``item`` among edit candidates for raw message ``raw``
                                        (-> job "edit")
DigestCb  ``d:<a>:<YYYYMMDD>``          Digest actions: ``tmr`` = move the not-done occurrences of the digest's
                                        day ``d`` (already due) to the day after. A digest delayed by quiet hours
                                        arrives the next morning, so the day is in the data; buttons sent before
                                        it was (``d:tmr``) use the local date of the digest message.
SetCb     ``s:<key>:<v>``               Settings menu; every key/value is documented in ``bot.views``.
SubCb     ``c:<a>:<step item id>``      A checklist line of a parent's card: ``done`` (☐ pressed) / ``undo``
                                        (☑ pressed). The wanted state is in the data, so a stale card never flips
                                        a step the wrong way.
ListCb    ``ld:<a>:<k>:<d>:<s>:<n>:``   [✅ N] / [↩️ N] under a list (docs/features/iteration-3.md §2): ``a`` =
          ``<o>:<i>``                   ``done`` / ``undo`` (the wanted state, as in SubCb); ``k`` = the list
                                        (``LIST_KINDS``: ``t`` /today, ``k`` /tasks, ``m`` / ``e`` the morning /
                                        evening digest); ``d`` = its local day ``YYYYMMDD`` ("" for /tasks);
                                        ``s`` = the section of the line (``views.SEC_*``), ``n`` = its
                                        number; ``o`` = the occurrence of the line (0: none, the item itself),
                                        ``i`` = its item. The buttons of a list message are its snapshot: a press
                                        re-renders the same numbered lines (``bot.lists``).
UndoCb    ``u:<raw id>:<item id>``      [↩️ Отменить] under "✅ Закрыл «…»": undo the edit of ``item`` that raw
                                        message ``raw`` asked for by description (docs/features/iteration-3.md §3;
                                        what it changed is stored with the raw message, ``services.undo``)
AgainCb   ``vr:<raw id>``               [🔁 Ещё раз] under "🎤 Не разобрал голосовое…": transcribe the voice note of
                                        raw message ``raw`` again (docs/features/iteration-4.md §1;
                                        ``processor.retry_unheard``)
========  ============================  =============================================================================

Removed 2026-10-07 (an item's time is its notification time): ``ItemCb`` ``lead`` (the "⏱ Напомнить за…" menu) and
``LeadCb`` ``l:<item id>:<minutes>``. Buttons still in old messages answer "Кнопка устарела" (``on_stale``).
"""

from __future__ import annotations

from typing import Final

from aiogram.filters.callback_data import CallbackData

MAX_CALLBACK_BYTES: Final = 64

ITEM_ACTIONS: Final[frozenset[str]] = frozenset(
    {
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
)
STEP_ACTIONS: Final[frozenset[str]] = frozenset({"done", "undo"})
OCC_ACTIONS: Final[frozenset[str]] = frozenset({"done", "undone", "s15", "s60", "tmr", "eve"})
SNOOZE_EVENING: Final = "eve"
# Snooze actions -> minutes (None = tomorrow, same local time); "eve" is today's evening slot.
SNOOZE_MINUTES: Final[dict[str, int | None]] = {"s15": 15, "s60": 60, "tmr": None}
SNOOZE_ACTIONS: Final[frozenset[str]] = frozenset({*SNOOZE_MINUTES, SNOOZE_EVENING})
DIGEST_ACTIONS: Final[frozenset[str]] = frozenset({"tmr"})
DIGEST_DAY_FMT: Final = "%Y%m%d"  # DigestCb.d, ListCb.d
LIST_ACTIONS: Final[frozenset[str]] = frozenset({"done", "undo"})
LIST_TODAY: Final = "t"
LIST_TASKS: Final = "k"
LIST_MORNING: Final = "m"
LIST_EVENING: Final = "e"
LIST_KINDS: Final[frozenset[str]] = frozenset({LIST_TODAY, LIST_TASKS, LIST_MORNING, LIST_EVENING})


class ItemCb(CallbackData, prefix="i"):
    a: str
    id: int


class OccCb(CallbackData, prefix="o"):
    a: str
    id: int


class KindCb(CallbackData, prefix="k"):
    id: int
    k: str


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


class SubCb(CallbackData, prefix="c"):
    a: str
    id: int


class ListCb(CallbackData, prefix="ld"):
    a: str  # done | undo
    k: str  # LIST_KINDS
    d: str  # the list's local day, DIGEST_DAY_FMT ("" for /tasks)
    s: str  # section of the line
    n: int  # its number
    o: int  # occurrence id, 0 = none
    i: int  # item id


class UndoCb(CallbackData, prefix="u"):
    raw: int
    item: int


class AgainCb(CallbackData, prefix="vr"):
    raw: int


ALL_CALLBACKS: Final[tuple[type[CallbackData], ...]] = (
    ItemCb,
    OccCb,
    KindCb,
    AskCb,
    PickCb,
    DigestCb,
    SetCb,
    SubCb,
    ListCb,
    UndoCb,
    AgainCb,
)
