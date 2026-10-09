"""[↩️ Отменить] of an edit applied by description (docs/features/iteration-3.md §3).

"Закрой задачу про документацию" with exactly one exact match is applied at once and answered "✅ Закрыл «…»" with
[↩️ Отменить]. The undo puts back exactly what that edit changed: statuses and ``done_at``, the occurrences' times and
statuses with their pending reminders, the series (rule, start, active), title, kind, tags, span and leads — of the
item and of its checklist (a parent closes its open steps, the last step closes its parent).

``apply`` runs ``items.apply_patch`` between two reads of the item's family (the top-level item and all its steps:
whatever an edit can touch) and keeps only the difference, as JSON the caller stores with the raw message
(``keep``: ``raw_json[ingest.UNDO_KEY][item id]``) — the changed rows before and after, the occurrences and reminders
the edit created. ``undo_edit`` locks the family and checks that each of those rows is still as the edit left it and
that nothing was added since (the item's ``updated_at`` catches any later change of it — another edit, a button —
and a reminder that went off or the next instance of a series show in the rows): a stale undo changes nothing. Then
the created rows are deleted and the changed ones get their old values back; occurrences that move go through a
parking time first, because UNIQUE(item_id, due_at) is checked row by row. The state is read with column selects,
fresh from the database (never from objects the session may hold).

Why the raw message and not a table of its own: the undo belongs to the message that asked for the edit, that row
already carries the pipeline's bookkeeping (``_ask``, ``_candidates``, ``_app``) and lives as long as the items, the
difference is a few rows, and the running bot needs no migration for it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Final

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from dumpbot.ai.schemas import EditPatch
from dumpbot.db.models import Item, Occurrence, RawMessage, Reminder
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import PatchResult
from dumpbot.services import items
from dumpbot.services.ingest import UNDO_KEY

__all__ = [
    "ALREADY",
    "ITEM_FIELDS",
    "MISSING",
    "STALE",
    "UNDONE",
    "Applied",
    "apply",
    "keep",
    "stored",
    "undo_edit",
]

VERSION: Final = 1
# What an undo puts back on an item (``updated_at`` becomes the time of the undo).
ITEM_FIELDS: Final[tuple[str, ...]] = (
    "status",
    "done_at",
    "deleted_at",
    "kind",
    "title",
    "tags",
    "when_spec",
    "lead_minutes",
    "rrule",
    "rec_time_local",
    "rec_dtstart_local",
    "rec_tz",
    "rec_active",
    "period_kind",
    "period_start",
    "period_end",
    "someday",
    "needs_confirm",
)
_ITEM_READ: Final = (*ITEM_FIELDS, "updated_at")
# Item fields that are its time besides its occurrences ("когда-нибудь" may have none to change).
_TIME_FIELDS: Final = ("someday", "period_kind", "period_start", "period_end", "rrule", "rec_active", "lead_minutes")
_OCC_FIELDS: Final = ("due_at", "precision", "status", "done_at")
_OCC_READ: Final = ("item_id", *_OCC_FIELDS)
_REM_FIELDS: Final = ("fire_at", "lead_minutes", "auto", "status")
_REM_READ: Final = ("occurrence_id", *_REM_FIELDS)
_LIVE_REMINDERS: Final = ("pending", "sending")
_DATETIMES: Final = frozenset({"done_at", "deleted_at", "updated_at", "rec_dtstart_local", "due_at", "fire_at"})
_DATES: Final = frozenset({"period_start", "period_end"})
_TIMES: Final = frozenset({"rec_time_local"})
# Occurrences that move back wait here first, so that none takes a time another one still holds.
_PARK: Final = timedelta(days=365_000)

# undo_edit outcomes
UNDONE: Final = "undone"
ALREADY: Final = "already"  # pressed before (a double tap)
STALE: Final = "stale"  # the item changed since the edit: nothing was touched
MISSING: Final = "missing"  # nothing to undo is stored (unknown message or item, garbage)

Fields = dict[str, Any]


@dataclass
class _State:
    items: dict[int, Fields]
    occs: dict[int, Fields]
    rems: dict[int, Fields]  # live (pending / sending) ones and the ids asked for


@dataclass
class Applied:
    """``items.apply_patch``'s result, the undo of it (None when the edit cannot be undone) and what the reply
    says: the title before the edit, how many open steps were closed with the item, whether its time moved (its
    occurrences, span or series changed) and whether anything changed at all."""

    result: PatchResult
    snapshot: dict[str, Any] | None
    title_before: str
    steps_closed: int
    moved: bool
    changed: bool


async def apply(session: AsyncSession, item_id: int, patch: EditPatch, prefs: Prefs, now: datetime) -> Applied:
    """``items.apply_patch`` plus the undo of it (flushes, does not commit; ``items.NotFoundError`` for an unknown
    item)."""
    family = await _family(session, item_id)
    before = await _read(session, family)
    result = await items.apply_patch(session, item_id, patch, prefs, now)
    family = list(dict.fromkeys([*family, *await _family(session, item_id)]))
    after = await _read(session, family, known=before.rems)
    closed = 0
    if family[0] == item_id:  # a parent: its steps closed with it
        closed = sum(
            1
            for i in family[1:]
            if before.items.get(i, {}).get("status") == "open" and after.items.get(i, {}).get("status") == "done"
        )
    old, new = before.items[item_id], after.items.get(item_id, {})
    own_occs = {k: v for k, v in before.occs.items() if v["item_id"] == item_id}
    moved = own_occs != {k: v for k, v in after.occs.items() if v["item_id"] == item_id} or any(
        old.get(f) != new.get(f) for f in _TIME_FIELDS
    )
    changed = (before.occs, before.rems) != (after.occs, after.rems) or any(
        {k: v for k, v in fields.items() if k != "updated_at"}
        != {k: v for k, v in after.items.get(i, {}).items() if k != "updated_at"}
        for i, fields in before.items.items()
    )
    snapshot = _diff(item_id, family, before, after)
    return Applied(result, snapshot, str(old["title"]), closed, moved=moved, changed=changed)


def keep(raw: RawMessage, item_id: int, snapshot: Mapping[str, Any]) -> None:
    """Store the undo of the edit of ``item_id`` with the raw message that asked for it."""
    data = dict(raw.raw_json or {})
    undos = data.get(UNDO_KEY)
    data[UNDO_KEY] = {**(undos if isinstance(undos, dict) else {}), str(item_id): dict(snapshot)}
    raw.raw_json = data


def stored(raw: RawMessage | None, item_id: int) -> dict[str, Any] | None:
    """The stored undo of the edit of ``item_id`` by this raw message (None: none, or not one this code wrote)."""
    undos = (raw.raw_json or {}).get(UNDO_KEY) if raw is not None else None
    snap = undos.get(str(item_id)) if isinstance(undos, dict) else None
    if not isinstance(snap, dict) or snap.get("v") != VERSION or snap.get("item") != item_id:
        return None
    return snap if _well_formed(snap) else None


def _well_formed(snap: Mapping[str, Any]) -> bool:
    try:
        ids = [int(i) for i in snap["family"]]
        for part in ("items", "occs", "occs_new", "rems", "rems_new"):
            ids += [int(k) for k in snap[part]]
        ids += [int(snap["occ_max"]), int(snap["rem_max"])]
        entries = [*snap["items"].values(), *snap["occs"].values(), *snap["rems"].values()]
        return all(isinstance(e, dict) and isinstance(e["a"], dict) and isinstance(e["b"], dict) for e in entries)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


async def undo_edit(session: AsyncSession, raw_id: int, item_id: int, now: datetime) -> str:
    """[↩️ Отменить]: put back what the edit of ``item_id`` by raw message ``raw_id`` changed (flushes, does not
    commit). Returns ``UNDONE``, ``ALREADY`` (undone before), ``STALE`` (changed since: nothing touched) or
    ``MISSING``.

    Locks the family's rows (items, occurrences, reminders) and only then the raw message: two presses run one after
    the other, and the second one finds the undo done (the lock order is the edit's: its rows before the raw)."""
    snap = stored(await session.get(RawMessage, raw_id), item_id)
    if snap is None:
        return MISSING
    if snap.get("undone"):
        return ALREADY
    family = [int(i) for i in snap["family"]]
    known = [int(k) for part in ("rems", "rems_new") for k in snap[part]]
    await _lock(session, family, known)
    raw = await session.scalar(
        select(RawMessage).where(RawMessage.id == raw_id).with_for_update().execution_options(populate_existing=True)
    )
    snap = stored(raw, item_id)
    if raw is None or snap is None:
        return MISSING
    if snap.get("undone"):
        return ALREADY
    if not _as_left(snap, await _read(session, family, known=known)):
        return STALE
    await _put_back(session, snap, now)
    keep(raw, item_id, {**snap, "undone": now.isoformat()})
    if len(family) > 1:
        items.checklist_changed(session, family[0])
    return UNDONE


# --- state -----------------------------------------------------------------------------------------------------------


async def _family(session: AsyncSession, item_id: int) -> list[int]:
    """The top-level item first, then all its steps (an edit of one may change the others)."""
    row = (await session.execute(select(Item.id, Item.parent_id).where(Item.id == item_id))).first()
    if row is None:
        raise items.NotFoundError(f"item {item_id} not found")
    root = row.parent_id if row.parent_id is not None else item_id
    steps = (await session.scalars(select(Item.id).where(Item.parent_id == root).order_by(Item.id))).all()
    return [root, *steps]


async def _read(session: AsyncSession, family: Sequence[int], *, known: Iterable[int] = ()) -> _State:
    """The family's items, all their occurrences, and their live reminders plus the ``known`` ones — as JSON
    values, read from the database."""
    await session.flush()
    item_rows = await session.execute(select(Item.id, *_cols(Item, _ITEM_READ)).where(Item.id.in_(family)))
    occ_rows = await session.execute(
        select(Occurrence.id, *_cols(Occurrence, _OCC_READ)).where(Occurrence.item_id.in_(family))
    )
    rem_stmt = (
        select(Reminder.id, *_cols(Reminder, _REM_READ))
        .join(Occurrence, Occurrence.id == Reminder.occurrence_id)
        .where(
            Occurrence.item_id.in_(family),
            or_(Reminder.status.in_(_LIVE_REMINDERS), Reminder.id.in_(list(known))),
        )
    )
    rem_rows = await session.execute(rem_stmt)
    return _State(
        items={row[0]: _dump_row(_ITEM_READ, row[1:]) for row in item_rows},
        occs={row[0]: _dump_row(_OCC_READ, row[1:]) for row in occ_rows},
        rems={row[0]: _dump_row(_REM_READ, row[1:]) for row in rem_rows},
    )


def _diff(item_id: int, family: Sequence[int], before: _State, after: _State) -> dict[str, Any] | None:
    """What the edit changed (rows before / after, rows it created) and the highest ids after it (rows created later
    make the undo stale). None when it cannot be undone (it removed rows or changed the family)."""
    if before.items.keys() != after.items.keys() or not before.occs.keys() <= after.occs.keys():
        return None
    if not before.rems.keys() <= after.rems.keys():
        return None

    def core(fields: Fields) -> Fields:
        return {k: v for k, v in fields.items() if k != "updated_at"}

    changed_items = {
        str(i): {"b": core(before.items[i]), "a": after.items[i]}
        for i in family
        if i in before.items and (i == item_id or core(before.items[i]) != core(after.items[i]))
    }
    return {
        "v": VERSION,
        "item": item_id,
        "family": list(family),
        "items": changed_items,
        "occs": _changed(before.occs, after.occs),
        "occs_new": {str(k): v for k, v in sorted(after.occs.items()) if k not in before.occs},
        "rems": _changed(before.rems, after.rems),
        "rems_new": {str(k): v for k, v in sorted(after.rems.items()) if k not in before.rems},
        "occ_max": max(after.occs, default=0),
        "rem_max": max(after.rems, default=0),
    }


def _changed(before: Mapping[int, Fields], after: Mapping[int, Fields]) -> dict[str, dict[str, Fields]]:
    return {str(k): {"b": b, "a": after[k]} for k, b in sorted(before.items()) if after[k] != b}


def _as_left(snap: Mapping[str, Any], now: _State) -> bool:
    """Every row the edit changed or created is still as it left it, and no occurrence or live reminder was added
    to the family since."""
    for part, rows in (("items", now.items), ("occs", now.occs), ("rems", now.rems)):
        if any(rows.get(int(k)) != entry["a"] for k, entry in snap[part].items()):
            return False
    for part, rows in (("occs_new", now.occs), ("rems_new", now.rems)):
        if any(rows.get(int(k)) != fields for k, fields in snap[part].items()):
            return False
    return all(k <= int(snap["occ_max"]) for k in now.occs) and all(k <= int(snap["rem_max"]) for k in now.rems)


# --- restore ---------------------------------------------------------------------------------------------------------


async def _lock(session: AsyncSession, family: Sequence[int], reminder_ids: Sequence[int]) -> None:
    """Row locks until commit, in the order the item services take them: items, occurrences, reminders."""
    await session.execute(select(Item.id).where(Item.id.in_(family)).order_by(Item.id).with_for_update())
    occs = select(Occurrence.id).where(Occurrence.item_id.in_(family)).order_by(Occurrence.id)
    await session.execute(occs.with_for_update())
    if reminder_ids:
        rems = select(Reminder.id).where(Reminder.id.in_(reminder_ids)).order_by(Reminder.id)
        await session.execute(rems.with_for_update())


async def _put_back(session: AsyncSession, snap: Mapping[str, Any], now: datetime) -> None:
    created_rems = [int(k) for k in snap["rems_new"]]
    created_occs = [int(k) for k in snap["occs_new"]]
    if created_rems:
        await session.execute(delete(Reminder).where(Reminder.id.in_(created_rems)))
    if created_occs:  # their reminders go with them (ON DELETE CASCADE)
        await session.execute(delete(Occurrence).where(Occurrence.id.in_(created_occs)))

    occ_before = {int(k): entry["b"] for k, entry in snap["occs"].items()}
    occs = await _objects(session, Occurrence, occ_before)
    moving = [o for o in occs if o.due_at != _load("due_at", occ_before[o.id]["due_at"])]
    for occ in moving:  # park first: another one may still sit at the time this one goes back to
        occ.due_at = occ.due_at + _PARK
    if moving:
        await session.flush()
    for occ in occs:
        _assign(occ, _OCC_FIELDS, occ_before[occ.id])

    rem_before = {int(k): entry["b"] for k, entry in snap["rems"].items()}
    for reminder in await _objects(session, Reminder, rem_before):
        _assign(reminder, _REM_FIELDS, rem_before[reminder.id])

    item_before = {int(k): entry["b"] for k, entry in snap["items"].items()}
    for item in await _objects(session, Item, item_before):
        _assign(item, ITEM_FIELDS, item_before[item.id])
        item.updated_at = now
    await session.flush()


async def _objects[T: (Item, Occurrence, Reminder)](
    session: AsyncSession, model: type[T], ids: Iterable[int]
) -> list[T]:
    ids = list(ids)
    if not ids:
        return []
    stmt = select(model).where(model.id.in_(ids)).order_by(model.id).execution_options(populate_existing=True)
    return list((await session.scalars(stmt)).all())


# --- JSON values -----------------------------------------------------------------------------------------------------


def _cols(model: type, names: Sequence[str]) -> list[Any]:
    return [getattr(model, name) for name in names]


def _dump_row(names: Sequence[str], values: Sequence[Any]) -> Fields:
    return {name: _dump(value) for name, value in zip(names, values, strict=True)}


def _dump(value: Any) -> Any:
    if isinstance(value, date | time):  # datetime is a date too
        return value.isoformat()
    if isinstance(value, list | tuple):
        return list(value)
    return value


def _load(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name in _DATETIMES:
        return datetime.fromisoformat(value)
    if name in _DATES:
        return date.fromisoformat(value)
    if name in _TIMES:
        return time.fromisoformat(value)
    return list(value) if isinstance(value, list) else value


def _assign(obj: object, names: Sequence[str], fields: Mapping[str, Any]) -> None:
    for name in names:
        setattr(obj, name, _load(name, fields.get(name)))
