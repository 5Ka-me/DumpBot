"""Item lifecycle: creation from AI extractions, occurrences, reminders, recurrence and edits (architecture.md §4.10).

Every function takes an ``AsyncSession``, flushes but never commits (the caller commits). ORM relationships are
``lazy="raise"``, so related rows are always loaded with explicit queries.

Conventions kept by this module:
- an item's time is its notification time (docs/requirements.md, 2026-10-07): ``Occurrence.due_at`` is when it
  notifies — "встреча в 15" at 15:00, no default lead. ``Item.lead_minutes`` keeps the explicit leads still in effect
  ("напомни за 30 минут до встречи в 15": [30], the occurrence is due at 14:30 and the title keeps "(15:00)"); with
  several ("за день и за час") the occurrence is due at the first moment and notifies again at the others
  (``timeresolve.plan_reminders``). A series keeps its stated times in its rule, each instance is due its lead
  earlier. A deadline keeps its own time ("до пятницы": the evening before and the day itself). A new time from an
  edit or the Mini App's picker is the notification time itself (the item's leads go); "напомни за N" alone moves
  the notification N minutes before the time the owner stated (``_shift_by_lead``); a snooze moves the time;
- the *current* occurrence of an item is its earliest open one, else the latest by ``due_at`` (cancelled last);
- re-planning cancels the occurrence's **pending** reminders (sent ones are history) and inserts a fresh plan
  from ``timeresolve.plan_reminders``;
- ``Item.lead_minutes == []`` means "reminders switched off" (🔕): set by ``remove_reminders``, so the next
  occurrences of a series stay silent too; an explicit new time or lead switches reminders back on;
- precision and role of a recurring series come from ``Item.when_spec`` (moving one instance does not change it);
- fuzzy time (docs/features/iteration-2.md §A): ``Item.period_kind/period_start/period_end`` keep the span of
  "на этой неделе" & co. and its occurrence has precision ``period`` (reminders (1)/(2) are planned from the span);
  ``Item.someday`` items have no open occurrence. A new explicit date, a move to tomorrow or a recurrence turns
  such an item into a plain dated one;
- checklists (docs/features/iteration-2.md §C2): a step is an item with ``parent_id``/``position``, one level deep.
  A step gets an occurrence only when it has a time of its own (no default 19:00 nudge for every step). Whatever
  changes a step (☑ / ☐, done / undone, delete / restore, a new time) re-checks its parent on fresh, locked rows
  (``_sync_parent``): no open step left and one done closes it, an open step reopens a closed one. A parent done by
  hand closes its open steps at its own ``done_at``; its "↩️ Не сделано" reopens exactly the steps done at that
  moment. Deleting / restoring a parent takes its steps along, restoring a step of a deleted parent brings the
  parent back. A checklist never repeats (no rrule for a parent with steps, nor for a step). What happened to
  parents is collected per session (``take_parent_events``) for the bot's "🎉 Все шаги выполнены" message;
- related items (§C1): ``item_links`` rows for the earlier items the AI named among the candidates it was shown and
  that share a subject word with the new item (``services.related``: not a reminder / time word or a common verb);
- ``updated_at`` is always set explicitly (server-side ``onupdate`` would expire it, and async sessions cannot
  lazy-load).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Select, case, exists, false, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from dumpbot import clock
from dumpbot.ai.schemas import MAX_RELATED, MAX_STEPS, EditPatch, ExtractedItem, Extraction, StepSpec, When
from dumpbot.db.models import Item, ItemLink, Occurrence, RawMessage, Reminder, UserSettings
from dumpbot.domain import recurrence, timeresolve
from dumpbot.domain.fmt import fmt_date, fmt_time
from dumpbot.domain.kinds import ACTIONABLE, KINDS, Precision, SourceKind
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import CreatedItem, PatchResult, ReminderPlan, Resolution
from dumpbot.services.related import sharing_subject

__all__ = [
    "MAX_CHECKLIST",
    "STEP_KIND",
    "SUPERSEDED_AFTER",
    "CreatedItem",
    "NotFoundError",
    "ParentEvents",
    "PatchResult",
    "add_step",
    "add_steps",
    "apply_patch",
    "can_split",
    "clear_someday",
    "clear_time",
    "create_items",
    "current_occurrence",
    "current_occurrence_order",
    "delete_item",
    "ensure_recurring",
    "get_item",
    "item_snapshot",
    "lock_item",
    "mark_done",
    "mark_undone",
    "move_to_tomorrow",
    "pending_reminders",
    "remove_reminders",
    "replace_items_for_raw",
    "restore_item",
    "set_details",
    "set_kind",
    "set_tags",
    "set_title",
    "snooze",
    "stop_recurrence",
    "take_parent_events",
    "toggle_step",
]

log = logging.getLogger(__name__)

# An open occurrence of a series that is older than this and has a later sibling is marked missed.
SUPERSEDED_AFTER: Final = timedelta(hours=12)
MAX_LEAD_MINUTES: Final = 43200  # 30 days, same cap as the AI schema
_HHMM_RE: Final = re.compile(r"^\d{2}:\d{2}$")
_LOCAL_FMT: Final = "%Y-%m-%d %H:%M"
STEP_KIND: Final = "task"  # every step of a checklist is a task
MAX_CHECKLIST: Final = 20  # steps of one checklist added by hand (the AI gives at most MAX_STEPS)
_EVENTS_KEY: Final = "dumpbot.parent_events"  # Session.info key of ParentEvents
WARN_CHECKLIST_RRULE: Final = "задача с шагами не повторяется: отметки шагов были бы общими для всех раз"
WARN_STEP_RRULE: Final = "шаг чек-листа не повторяется"
_FUZZY: Final = ("period", "soon")  # precisions of a span's occurrence: due at its reminder (2), not a named day
# Precisions a snoozed occurrence keeps; any other (a time the bot chose, a span) becomes the exact new time.
_SNOOZE_KEEPS: Final = frozenset({"exact", "slot", "relative", "date", "fallback"})


class NotFoundError(LookupError):
    """The item or occurrence id does not exist."""


@dataclass
class ParentEvents:
    """Checklist changes made in one session, for the bot after commit: parents closed because their last open step
    was done ("🎉 Все шаги выполнены"), and every parent whose checklist changed (its cards are stale)."""

    closed: list[int] = field(default_factory=list)
    changed: set[int] = field(default_factory=set)


def take_parent_events(session: AsyncSession) -> ParentEvents:
    """The checklist changes of this session so far (and forget them)."""
    return session.info.pop(_EVENTS_KEY, None) or ParentEvents()


def _events(session: AsyncSession) -> ParentEvents:
    return session.info.setdefault(_EVENTS_KEY, ParentEvents())


# --- creation --------------------------------------------------------------------------------------------------


async def create_items(
    session: AsyncSession,
    *,
    raw: RawMessage | None,
    extraction: Extraction,
    source: SourceKind,
    source_text: str | None,
    source_meta: dict[str, Any] | None,
    urls: list[str],
    prefs: Prefs,
    now: datetime,
    model: str | None,
    prompt_version: str | None,
    related_allowed: Collection[int] = (),
) -> list[CreatedItem]:
    """For each ExtractedItem: resolve_item -> Item (+ Occurrence when it has a due time, + its Reminders), its
    checklist (``steps``; not for a series: every instance would need its own) and links to the earlier items it
    names in ``related`` — only ids in ``related_allowed`` (the candidates the model was shown; others are dropped)
    whose title / details share a subject word with the new item's (``services.related``).

    A link item without URLs of its own gets all URLs of the message. Steps are not in the result (they show in
    their parent's card). Flushes, does not commit.
    """
    return await _create_all(
        session,
        raw.id if raw is not None else None,
        extraction=extraction,
        source=source,
        source_text=source_text,
        source_meta=source_meta,
        urls=urls,
        prefs=prefs,
        now=now,
        model=model,
        prompt_version=prompt_version,
        related_allowed=related_allowed,
    )


async def replace_items_for_raw(
    session: AsyncSession,
    raw_id: int,
    *,
    raw: RawMessage | None = None,
    extraction: Extraction,
    source: SourceKind,
    source_text: str | None,
    source_meta: dict[str, Any] | None,
    urls: list[str],
    prefs: Prefs,
    now: datetime,
    model: str | None,
    prompt_version: str | None,
    related_allowed: Collection[int] = (),
) -> list[CreatedItem]:
    """After a clarification (Ask): soft-delete the items of this raw message (with their steps) and create them
    again.

    ``raw`` is accepted for symmetry with ``create_items``; the new items are linked to ``raw_id`` either way.
    """
    now = _utc(now)
    stmt = select(Item).where(Item.raw_message_id == raw_id, Item.status != "deleted", Item.parent_id.is_(None))
    for item in (await session.scalars(stmt)).all():
        await _soft_delete(session, item, now)
    return await _create_all(
        session,
        raw_id,
        extraction=extraction,
        source=source,
        source_text=source_text,
        source_meta=source_meta,
        urls=urls,
        prefs=prefs,
        now=now,
        model=model,
        prompt_version=prompt_version,
        related_allowed=related_allowed,
    )


async def _create_all(
    session: AsyncSession,
    raw_id: int | None,
    *,
    extraction: Extraction,
    source: SourceKind,
    source_text: str | None,
    source_meta: dict[str, Any] | None,
    urls: list[str],
    prefs: Prefs,
    now: datetime,
    model: str | None,
    prompt_version: str | None,
    related_allowed: Collection[int] = (),
) -> list[CreatedItem]:
    now = _utc(now)
    single = len(extraction.items) == 1
    allowed = set(related_allowed)
    out: list[CreatedItem] = []
    for extracted in extraction.items:
        res = timeresolve.resolve_item(extracted, now, prefs, source=source)
        item = Item(
            raw_message_id=raw_id,
            kind=extracted.kind,
            title=_with_stated_time(extracted.title.strip() or "Без названия", res, prefs),
            details=extracted.details,
            tags=list(extracted.tags),
            source_text=source_text,
            source_kind=source,
            source=source_meta,
            urls=_item_urls(extracted, urls, single=single),
            status="open",
            when_spec=_when_dump(extracted.when),
            lead_minutes=list(res.leads) if res.leads else None,
            period_kind=res.period_kind,
            period_start=res.period_start,
            period_end=res.period_end,
            someday=res.precision == "someday",
            needs_confirm=res.needs_confirm,
            confidence=extracted.conf,
            ai_model=model,
            prompt_version=prompt_version,
            created_at=now,  # message time: "what did I save yesterday" follows the message, not the queue
            updated_at=clock.utcnow(),
        )
        if res.rrule:
            item.rrule = res.rrule
            item.rec_time_local = res.rec_time_local
            item.rec_dtstart_local = res.rec_dtstart_local
            item.rec_tz = prefs.tz
            item.rec_active = True
        created = await _insert(session, item, res)
        if extracted.steps and not item.rec_active:
            await _create_steps(session, item, extracted.steps, prefs, now, model=model, prompt_version=prompt_version)
        await _link(session, item, [i for i in extracted.related if i in allowed])
        out.append(created)
    log.info("created %d item(s) from raw %s", len(out), raw_id)
    return out


async def _insert(session: AsyncSession, item: Item, res: Resolution) -> CreatedItem:
    """Add the item, plus its occurrence and reminders when the resolution has a due time."""
    session.add(item)
    await session.flush()
    occ: Occurrence | None = None
    reminders: list[Reminder] = []
    if res.due_at is not None:
        occ = Occurrence(item_id=item.id, due_at=res.due_at, precision=res.precision, status="open")
        session.add(occ)
        await session.flush()
        reminders = await _add_reminders(session, occ.id, res.reminders)
    return CreatedItem(item=item, occurrence=occ, reminders=reminders, resolution=res)


async def _link(session: AsyncSession, item: Item, related_ids: Sequence[int]) -> None:
    """``item_links`` rows "item is similar to the earlier related item" (at most MAX_RELATED, never itself), only
    for related items whose title / details share a subject word with the item's (``related.sharing_subject``: the
    model's "related" alone is not trusted)."""
    ids = [i for i in dict.fromkeys(related_ids) if i != item.id]
    if not ids:
        return
    ids = (await sharing_subject(session, f"{item.title} {item.details or ''}", ids))[:MAX_RELATED]
    if not ids:
        return
    rows = [{"item_id": item.id, "related_id": i, "kind": "similar"} for i in ids]
    await session.execute(pg_insert(ItemLink).values(rows).on_conflict_do_nothing())


def _with_stated_time(title: str, res: Resolution, prefs: Prefs) -> str:
    """ "Встреча" -> "Встреча (15:00)" when a lead moved the notification before the time the message stated (the
    item's time is its notification time, so the stated one lives in the title). The prompt asks the model to keep it
    there; this covers a model that did not: nothing is added when the title already has the hour (or the day of a
    date) as a number. A stated time on another day than the notification reads "(пт 9 окт 15:00)"."""
    if res.stated_at is None or res.due_at is None:
        return title
    tz = prefs.zone
    stated = res.stated_at.astimezone(tz)
    timed = res.precision not in ("date", "fallback")
    if re.search(rf"(?<!\d)0?{stated.hour if timed else stated.day}(?!\d)", title):
        return title
    if not timed:
        label = fmt_date(stated.date())
    elif stated.date() == res.due_at.astimezone(tz).date():
        label = fmt_time(stated.time())
    else:
        label = f"{fmt_date(stated.date())} {fmt_time(stated.time())}"
    return f"{title} ({label})"


def _item_urls(extracted: ExtractedItem, urls: Sequence[str], *, single: bool) -> list[str]:
    """URLs of the message that belong to this item (all of them for a single-item message)."""
    if single:
        return list(urls)
    text = f"{extracted.title} {extracted.details or ''}"
    own = [u for u in urls if u in text]
    if not own and extracted.kind == "link":
        return list(urls)
    return own


# --- checklists ------------------------------------------------------------------------------------------------


def can_split(item: Item) -> bool:
    """Whether the item may get a checklist: open, top-level (steps are one level deep) and not a series (each
    instance would need a checklist of its own). Whether it has steps already is checked separately."""
    return item.status == "open" and item.parent_id is None and not item.rec_active


async def add_steps(
    session: AsyncSession,
    parent_id: int,
    steps: Sequence[StepSpec],
    prefs: Prefs,
    now: datetime,
    *,
    model: str | None = None,
    prompt_version: str | None = None,
) -> list[CreatedItem]:
    """Give an item its checklist (the "split" call). Nothing is added when it cannot be split (``can_split``) or has
    steps already: the parent row is locked first, so two splits of one item run one after the other."""
    now = _utc(now)
    parent = await session.scalar(_locked(parent_id))
    if parent is None:
        raise NotFoundError(f"item {parent_id} not found")
    if not can_split(parent) or await _has_steps(session, parent.id):
        return []
    created = await _create_steps(session, parent, steps, prefs, now, model=model, prompt_version=prompt_version)
    _touch(parent)
    return created


async def toggle_step(session: AsyncSession, step_id: int, *, done: bool, prefs: Prefs, now: datetime) -> Item:
    """☐ / ☑ of a checklist line: the step (its open occurrence, when it has a time) done, or open again. The parent
    follows: closed by its last step, reopened by a step of a closed parent. A stale button that asks for the state
    the step is already in changes nothing."""
    now = _utc(now)
    step = await _get_item(session, step_id)
    if step.parent_id is None:
        raise NotFoundError(f"item {step_id} is not a step")
    await _lock_parent(session, step)  # a second quick tap waits here and then sees the first one's result
    if done and step.status == "open":
        await _patch_done(session, step, prefs, now)
    elif not done and step.status == "done":
        await _patch_undone(session, step, prefs, now)
    _touch(step)
    return step


async def add_step(
    session: AsyncSession, parent_id: int, title: str, prefs: Prefs, now: datetime, *, when: When | None = None
) -> CreatedItem:
    """One more step at the end of the checklist (added by hand in the Mini App), with its own time if ``when`` gives
    one. A done parent opens again: it has an open step now. ValueError: an empty title, a full checklist
    (``MAX_CHECKLIST``), or an item that cannot have steps (a step itself, a series, deleted)."""
    now = _utc(now)
    title = " ".join((title or "").split())
    if not title:
        raise ValueError("empty title")
    parent = await session.scalar(_locked(parent_id))
    if parent is None:
        raise NotFoundError(f"item {parent_id} not found")
    if parent.parent_id is not None or parent.rec_active or parent.status == "deleted":
        raise ValueError("this item cannot have steps")
    listed = func.count().filter(Item.status != "deleted")
    count, last = (
        await session.execute(select(listed, func.max(Item.position)).where(Item.parent_id == parent.id))
    ).one()
    if count >= MAX_CHECKLIST:
        raise ValueError("the checklist is full")
    spec = StepSpec(title=title, when=when)
    [created] = await _create_steps(
        session, parent, [spec], prefs, now, model=None, prompt_version=None, first=(last or 0) + 1
    )
    if parent.status == "done":
        await _patch_undone(session, parent, prefs, now)
    _events(session).changed.add(parent.id)
    _touch(parent)
    return created


async def _create_steps(
    session: AsyncSession,
    parent: Item,
    specs: Sequence[StepSpec],
    prefs: Prefs,
    now: datetime,
    *,
    model: str | None,
    prompt_version: str | None,
    first: int = 1,
) -> list[CreatedItem]:
    """Steps ``first``.. (at most MAX_STEPS) of ``parent``; only those with a time of their own get an occurrence."""
    named = [(title, spec) for spec in specs if (title := " ".join(spec.title.split()))]
    out: list[CreatedItem] = []
    for position, (title, spec) in enumerate(named[:MAX_STEPS], start=first):
        res = _step_resolution(spec, now, prefs)
        item = Item(
            raw_message_id=parent.raw_message_id,
            parent_id=parent.id,
            position=position,
            kind=STEP_KIND,
            title=title,
            tags=[],
            source_kind=parent.source_kind,
            urls=[],
            status="open",
            when_spec=_when_dump(spec.when) if res.due_at is not None else None,
            period_kind=res.period_kind,
            period_start=res.period_start,
            period_end=res.period_end,
            someday=False,
            needs_confirm=res.needs_confirm,
            ai_model=model,
            prompt_version=prompt_version,
            created_at=now,
            updated_at=clock.utcnow(),
        )
        out.append(await _insert(session, item, res))
    return out


def _step_resolution(spec: StepSpec, now: datetime, prefs: Prefs) -> Resolution:
    """The step's own time; without one (or "когда-нибудь") no due and no reminder, unlike a task: a checklist
    must not ping at 19:00 for every step."""
    extracted = ExtractedItem(kind=STEP_KIND, title=spec.title, when=spec.when)
    res = timeresolve.resolve_item(extracted, now, prefs, source="text")
    if res.precision in ("default", "someday"):
        return Resolution(due_at=None, precision="none", warnings=res.warnings)
    return res


async def _has_steps(session: AsyncSession, item_id: int) -> bool:
    stmt = select(exists().where(Item.parent_id == item_id, Item.status != "deleted"))
    return bool(await session.scalar(stmt))


def _locked(item_id: int) -> Select[tuple[Item]]:
    """SELECT … FOR UPDATE of one item that also refreshes the object already in the session."""
    return select(Item).where(Item.id == item_id).with_for_update().execution_options(populate_existing=True)


async def lock_item(session: AsyncSession, item_id: int) -> Item:
    """The item's row locked until commit and re-read: for actions that must run one at a time per item (two quick
    presses of [🪄 Разбить на шаги] queue one job)."""
    item = await session.scalar(_locked(item_id))
    if item is None:
        raise NotFoundError(f"item {item_id} not found")
    return item


async def _lock_parent(session: AsyncSession, step: Item) -> Item | None:
    """Lock the parent row of a step until commit and re-read the parent and the step: changes to one checklist (two
    quick ☑ taps, a tap racing an edit) run one after the other, and the one that waited decides on what the first
    committed, not on objects it loaded before the lock. None for a top-level item."""
    if step.parent_id is None:
        return None
    parent = await session.scalar(_locked(step.parent_id))
    await session.scalar(select(Item).where(Item.id == step.id).execution_options(populate_existing=True))
    return parent


async def _after_done(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> None:
    """Checklist rules once an item is done: a parent closes its open steps, a step may close its parent."""
    if item.status != "done":
        return
    if item.parent_id is None:
        await _close_steps(session, item, now)
    else:
        await _sync_parent(session, item, prefs, now)


async def _after_undone(
    session: AsyncSession, item: Item, closed_at: datetime | None, prefs: Prefs, now: datetime, *, steps: bool
) -> None:
    """Checklist rules once an item is open again: a step reopens its closed parent; a parent taken back by hand
    ("↩️ Не сделано", ``steps``) reopens the steps that were closed with it — done at its ``closed_at``: those its
    «Готово» closed, or the last tick that closed it."""
    if item.parent_id is not None:
        await _sync_parent(session, item, prefs, now)
    elif steps and closed_at is not None:
        stmt = select(Item).where(Item.parent_id == item.id, Item.status == "done", Item.done_at == closed_at)
        for step in (await session.scalars(stmt)).all():
            await _patch_undone(session, step, prefs, now)


async def _sync_parent(session: AsyncSession, step: Item, prefs: Prefs | None, now: datetime) -> None:
    """After a step changed (done or open again, deleted, restored, given a new time) its parent follows: an open one
    closes when no step is open any more and at least one is done (pending reminders cancelled, recorded in
    ``ParentEvents.closed``; a series parent never), a done one opens again when a step is open. The parent's cards
    are stale either way (``ParentEvents.changed``). ``prefs`` (None: the owner's) re-plan a reopened parent."""
    parent = await _lock_parent(session, step)
    if parent is None or parent.status == "deleted":
        return
    events = _events(session)
    events.changed.add(parent.id)
    if step.status == "open":
        if parent.status == "done":
            prefs = prefs if prefs is not None else await _owner_prefs(session)
            await _patch_undone(session, parent, prefs, now, steps=False)
    elif parent.status == "open" and not parent.rec_active and await _all_steps_done(session, parent.id):
        await _finish(session, parent, now)
        events.closed.append(parent.id)


async def _all_steps_done(session: AsyncSession, parent_id: int) -> bool:
    """No step of the checklist is open and at least one is done (deleted steps do not count)."""
    await session.flush()  # a Core query does not autoflush the step's new status
    stmt = select(Item.status).where(Item.parent_id == parent_id, Item.status.in_(("open", "done"))).distinct()
    return set((await session.scalars(stmt)).all()) == {"done"}


async def _close_steps(session: AsyncSession, parent: Item, now: datetime) -> None:
    """A parent done by hand: the whole checklist is finished, its open steps are done too — at the parent's own
    ``done_at``, which tells them apart from steps ticked before (an undo of the parent reopens exactly these)."""
    stmt = select(Item).where(Item.parent_id == parent.id, Item.status == "open")
    for step in (await session.scalars(stmt)).all():
        await _finish(session, step, parent.done_at or now)


async def _finish(session: AsyncSession, item: Item, now: datetime) -> None:
    """Item done with every open occurrence (their pending reminders cancelled)."""
    stmt = select(Occurrence).where(Occurrence.item_id == item.id, Occurrence.status == "open")
    occs = list((await session.scalars(stmt)).all())
    for occ in occs:
        occ.status = "done"
        occ.done_at = now
    await _cancel_pending(session, [o.id for o in occs])
    item.status = "done"
    item.done_at = now
    _touch(item)


# --- reads used by the bot -------------------------------------------------------------------------------------


async def get_item(session: AsyncSession, item_id: int) -> Item | None:
    return await session.get(Item, item_id)


async def item_snapshot(session: AsyncSession, item_id: int, prefs: Prefs, now: datetime) -> dict[str, Any]:
    """Item as the edit prompt sees it: {id, kind, title, details, tags, due_local, rrule, lead, status}, plus
    ``period`` ("YYYY-MM-DD..YYYY-MM-DD") or ``someday: true`` for an item with a fuzzy time."""
    item = await _get_item(session, item_id)
    occ = await current_occurrence(session, item_id)
    snap: dict[str, Any] = {
        "id": item.id,
        "kind": item.kind,
        "title": item.title,
        "details": item.details,
        "tags": list(item.tags or []),
        "due_local": occ.due_at.astimezone(prefs.zone).strftime(_LOCAL_FMT) if occ is not None else None,
        "rrule": item.rrule if item.rec_active else None,
        "lead": item.lead_minutes,
        "status": item.status,
    }
    if item.someday:  # an old (cancelled) occurrence is not its time any more
        snap["someday"] = True
        snap["due_local"] = None
    elif item.period_start is not None and item.period_end is not None:
        snap["period"] = f"{item.period_start.isoformat()}..{item.period_end.isoformat()}"
    return snap


def current_occurrence_order() -> tuple[ColumnElement[Any], ...]:
    """ORDER BY that puts the current occurrence first: earliest open, else latest by due (cancelled last)."""
    is_open = Occurrence.status == "open"
    return (
        is_open.desc(),
        (Occurrence.status == "cancelled").asc(),
        case((is_open, Occurrence.due_at)).asc(),
        Occurrence.due_at.desc(),
        Occurrence.id.desc(),
    )


async def current_occurrence(session: AsyncSession, item_id: int) -> Occurrence | None:
    """Earliest open occurrence, else the latest one."""
    stmt = select(Occurrence).where(Occurrence.item_id == item_id).order_by(*current_occurrence_order()).limit(1)
    return await session.scalar(stmt)


async def pending_reminders(session: AsyncSession, occurrence_id: int) -> list[Reminder]:
    stmt = (
        select(Reminder)
        .where(Reminder.occurrence_id == occurrence_id, Reminder.status == "pending")
        .order_by(Reminder.fire_at, Reminder.id)
    )
    return list((await session.scalars(stmt)).all())


# --- done / undone / snooze ------------------------------------------------------------------------------------


async def mark_done(session: AsyncSession, occurrence_id: int, now: datetime, prefs: Prefs) -> Occurrence:
    """occurrence -> done, its pending reminders -> cancelled. A one-off item becomes done; an active series
    gets its next open occurrence (with reminders) right away and stays open."""
    now = _utc(now)
    occ = await _get_occ(session, occurrence_id)
    item = await _get_item(session, occ.item_id)
    if await _lock_parent(session, item) is not None:  # a step: decide on the rows as the lock holder left them
        occ = await _reload_occ(session, occ.id)
    if occ.status != "done":
        occ.status = "done"
        occ.done_at = now
        await _cancel_pending(session, [occ.id])
    if item.rec_active and item.rrule:
        await _ensure_next(session, item, prefs, now)
    await _close_if_finished(session, item, now)
    await _after_done(session, item, prefs, now)
    _touch(item)
    return occ


async def mark_undone(
    session: AsyncSession, occurrence_id: int, *, prefs: Prefs | None = None, now: datetime | None = None
) -> Occurrence:
    """Back to open (item too) and re-plan its reminders if it is still ahead. A parent of a checklist reopens the
    steps that were closed with it (its «Готово» by mistake, then "↩️ Не сделано"); a step reopens its closed parent.

    A "когда-нибудь" item has no live occurrence: its occurrence (an old reminder's "↩️ Не сделано") is no longer
    done but stays out of the plan (cancelled, as ``_make_someday`` leaves open ones), and only the item opens.

    ``prefs``/``now`` are optional extensions of the contract; without them the owner's stored prefs and the
    current time are used.
    """
    occ = await _get_occ(session, occurrence_id)
    prefs = prefs if prefs is not None else await _owner_prefs(session)
    now = _utc(now) if now is not None else clock.utcnow()
    return await _undone(session, occ, prefs, now, steps=True)


async def _undone(session: AsyncSession, occ: Occurrence, prefs: Prefs, now: datetime, *, steps: bool) -> Occurrence:
    """``mark_undone``; ``steps``: a parent reopens the steps closed with it (not when a step reopens the parent)."""
    item = await _get_item(session, occ.item_id)
    if await _lock_parent(session, item) is not None:
        occ = await _reload_occ(session, occ.id)
    if occ.status == "open":
        return occ
    closed_at = item.done_at
    occ.status = "cancelled" if item.someday else "open"
    occ.done_at = None
    _reopen(item)
    if not item.someday:
        await _replan(session, item, occ, prefs, now)
    await _after_undone(session, item, closed_at, prefs, now, steps=steps)
    _touch(item)
    return occ


async def snooze(
    session: AsyncSession,
    occurrence_id: int,
    *,
    minutes: int | None = None,
    tomorrow: bool = False,
    prefs: Prefs,
    now: datetime,
) -> Reminder:
    """[⏰ 15 мин] / [⏰ 1 час] / [📅 Завтра]: move the item's time to ``now + minutes``, or tomorrow (relative to now;
    before ``night_cutoff`` that is the day that has just started) at the local time of day of ``due_at`` — the
    occurrence is due then (``/today``, the digests and the Mini App show the new time) and has exactly one pending
    reminder, at that time (auto=False). A one-off item's explicit leads go with the old time; a span ("на этой
    неделе") or a time the bot chose becomes this exact time. An instance of a series moves alone (merging into an
    instance already there, e.g. tomorrow's of a daily series).

    A deadline or a span (they remind before their time too: the evening before, the first morning) snoozed to a
    moment still before its time keeps that time: the snooze replaces the reminders up to then, the deadline's own
    (later) ones stay. The returned reminder's ``occurrence_id`` is the occurrence it now belongs to.
    """
    if (minutes is None) == (not tomorrow):
        raise ValueError("snooze needs exactly one of minutes / tomorrow")
    now = _utc(now)
    occ = await _get_occ(session, occurrence_id)
    item = await _get_item(session, occ.item_id)
    if minutes is not None:
        if minutes <= 0:
            raise ValueError(f"snooze minutes must be positive: {minutes}")
        fire = now + timedelta(minutes=minutes)
    else:
        tz = prefs.zone
        day, clock_time = timeresolve.tomorrow(now, prefs), occ.due_at.astimezone(tz).time()
        fire = timeresolve.localize(day, clock_time, tz).astimezone(UTC)
        if fire <= now:  # "завтра" right after midnight is today, but that time of day has passed
            fire = timeresolve.localize(day + timedelta(days=1), clock_time, tz).astimezone(UTC)
    if fire < occ.due_at and _reminds_before(item, occ):  # a deadline / a span, snoozed to before its time
        await _cancel_pending(session, [occ.id], until=fire)
        lead = int((occ.due_at - fire).total_seconds() // 60)
        [reminder] = await _add_reminders(session, occ.id, [ReminderPlan(fire_at=fire, lead_minutes=lead, auto=False)])
        _touch(item)
        return reminder
    precision: Precision = occ.precision if occ.precision in _SNOOZE_KEEPS else "exact"  # type: ignore[assignment]
    if not (item.rec_active and item.rrule):
        if item.lead_minutes:  # "за день и за час" of the old time: the new time is the one notification
            item.lead_minutes = None
        if occ.precision in _FUZZY:  # "на этой неделе" snoozed to a moment is that moment now
            _set_period(item, None, None, None)
    target = await _move(session, item, occ, fire, precision)
    await _cancel_pending(session, [target.id])
    [reminder] = await _add_reminders(session, target.id, [ReminderPlan(fire_at=fire, lead_minutes=0, auto=False)])
    _touch(item)
    return reminder


def _reminds_before(item: Item, occ: Occurrence) -> bool:
    """Whether the occurrence also reminds before its time: a span ((1) on its first morning) or a deadline (the
    evening before) — its time is more than a notification moment."""
    return occ.precision == "period" or (
        _role(item) == "deadline" and occ.precision in ("exact", "slot", "date", "fallback")
    )


# --- delete / restore / reminders off --------------------------------------------------------------------------


async def delete_item(session: AsyncSession, item_id: int, now: datetime) -> None:
    """Soft delete: status=deleted, deleted_at, rec_active=False, pending reminders cancelled. A parent takes its steps
    along; a deleted step may leave a checklist whose steps are all done, which closes the parent (🎉)."""
    now = _utc(now)
    item = await _get_item(session, item_id)
    await _lock_parent(session, item)
    if item.status == "deleted":
        return
    await _soft_delete(session, item, now)
    if item.parent_id is not None:
        await _sync_parent(session, item, None, now)


async def restore_item(session: AsyncSession, item_id: int, now: datetime, prefs: Prefs) -> Item:
    """Undo a delete: previous status (done if it had been finished), the series resumes, reminders of open
    occurrences that are still ahead are planned again. A parent brings back the steps deleted together with it. A
    step of a deleted parent (an old reminder's [↩️ Вернуть]) brings the parent back first — a step never lives
    without it; an open step reopens a done parent."""
    now = _utc(now)
    item = await _get_item(session, item_id)
    parent = await _lock_parent(session, item)
    if item.status != "deleted":
        return item
    if parent is not None and parent.status == "deleted":
        await restore_item(session, parent.id, now, prefs)  # this step too, when it was deleted with the parent
        if item.status != "deleted":
            _events(session).changed.add(parent.id)
            return item
    deleted_at = item.deleted_at
    await _restore(session, item, prefs, now)
    if parent is not None:
        if item.status == "open":
            await _sync_parent(session, item, prefs, now)
        _events(session).changed.add(parent.id)
    elif deleted_at is not None:  # the steps deleted together with it come back too
        stmt = select(Item).where(Item.parent_id == item.id, Item.status == "deleted", Item.deleted_at == deleted_at)
        for step in (await session.scalars(stmt)).all():
            await _restore(session, step, prefs, now)
    return item


async def _restore(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> None:
    item.status = "done" if item.done_at is not None else "open"
    item.deleted_at = None
    # delete_item cleared rec_active; a stopped-then-deleted series cannot be told apart and resumes too. A series
    # moved to "когда-нибудь" (``_make_someday`` keeps the rule, stopped) can: it stays stopped.
    item.rec_active = item.rrule is not None and item.status == "open" and not item.someday
    for occ in await _open_occurrences(session, item.id, after=now):
        await _replan(session, item, occ, prefs, now)
    if item.rec_active:
        await _ensure_next(session, item, prefs, now)
    _touch(item)


async def remove_reminders(session: AsyncSession, item_id: int) -> int:
    """Cancel all pending reminders of the item and switch reminders off for it (also for the next occurrences
    of a series). Returns the number of cancelled reminders."""
    item = await _get_item(session, item_id)
    count = await _cancel_pending(session, await _occurrence_ids(session, item.id))
    item.lead_minutes = []
    _touch(item)
    return count


# --- simple field edits ----------------------------------------------------------------------------------------


async def set_kind(session: AsyncSession, item_id: int, kind: str) -> Item:
    """Change the category. Existing occurrences and reminders are kept as they are."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    item = await _get_item(session, item_id)
    item.kind = kind
    _touch(item)
    return item


async def set_title(session: AsyncSession, item_id: int, title: str) -> Item:
    title = " ".join((title or "").split())
    if not title:
        raise ValueError("empty title")
    item = await _get_item(session, item_id)
    item.title = title
    _touch(item)
    return item


async def set_details(session: AsyncSession, item_id: int, details: str | None) -> Item:
    """Replace the details (blank -> none)."""
    item = await _get_item(session, item_id)
    item.details = (details or "").strip() or None
    _touch(item)
    return item


async def set_tags(session: AsyncSession, item_id: int, tags: list[str]) -> Item:
    """Replace the tags (normalized like the AI's: lower case, no '#', spaces -> '_', unique)."""
    item = await _get_item(session, item_id)
    item.tags = _clean_tags(tags)
    _touch(item)
    return item


async def stop_recurrence(session: AsyncSession, item_id: int, now: datetime | None = None) -> Item:
    """rec_active=False; the current (earliest open) occurrence stays. Later instances that were already generated
    (the next one exists as soon as the current one is due) are cancelled with their reminders.

    ``now`` is an optional extension of the contract (default: the current time).
    """
    now = _utc(now) if now is not None else clock.utcnow()
    item = await _get_item(session, item_id)
    item.rec_active = False
    current = await _first_open(session, item.id)
    for occ in await _open_occurrences(session, item.id, after=now):
        if current is not None and occ.id != current.id:
            occ.status = "cancelled"
            await _cancel_pending(session, [occ.id])
    _touch(item)
    return item


async def clear_someday(session: AsyncSession, item_id: int, prefs: Prefs, now: datetime) -> PatchResult:
    """No longer "когда-нибудь": an item without a date; an actionable one gets the default reminder, like a task
    saved without a time."""
    now = _utc(now)
    item = await _get_item(session, item_id)
    if item.someday:
        item.when_spec = None
        _set_period(item, None, None, None)
        if item.kind in ACTIONABLE:
            await _schedule_default(session, item, prefs, now)
        _touch(item)
    return await _patch_result(session, item, [])


async def clear_time(session: AsyncSession, item_id: int) -> Item:
    """No time of its own any more (a step: it then lives in its parent's card only): open occurrences are cancelled
    with their pending reminders, the time spec and any fuzzy span are dropped. A step loses its done occurrence too
    (a ticked step showed that time in the checklist and the lists): its tick stays on the step itself."""
    item = await _get_item(session, item_id)
    statuses = ("open", "done") if item.parent_id is not None else ("open",)
    stmt = select(Occurrence).where(Occurrence.item_id == item.id, Occurrence.status.in_(statuses))
    occs = list((await session.scalars(stmt)).all())
    for occ in occs:
        occ.status = "cancelled"
    await _cancel_pending(session, [o.id for o in occs])
    item.when_spec = None
    _set_period(item, None, None, None)
    _touch(item)
    return item


# --- edits from the AI (EditPatch) -----------------------------------------------------------------------------


async def apply_patch(
    session: AsyncSession, item_id: int, patch: EditPatch, prefs: Prefs, now: datetime
) -> PatchResult:
    """Apply an EditPatch. Order: delete (exclusive) -> title/kind/tags -> done/undone -> rrule -> when -> lead
    -> no_reminder / stop_recurrence ("сделал, теперь каждую неделю" closes this instance, then starts the rule).

    ``when`` moves the current open occurrence (or creates one) and re-plans its reminders; for an active
    series it moves only this instance. The new time is the notification time, unless the patch has a ``lead`` too
    ("перенеси на 16, напомни за полчаса": due 15:30). A ``lead`` alone ("напомни за час") moves the notification
    that long before the time the owner stated (``_shift_by_lead``). ``rrule`` enables or replaces the recurrence
    (time of day from the new/current time). An item that becomes actionable without any time gets the default
    reminder; one that stops being actionable (task -> idea) loses it: the open ``default`` occurrences (the 19:00 a
    task without a time got) are cancelled with their reminders, times the owner set (exact, slot, date…) stay.
    """
    now = _utc(now)
    item = await _get_item(session, item_id)
    await _lock_parent(session, item)  # a step: its checklist's lock first, as every change of a step takes it
    if patch.action == "delete":
        await delete_item(session, item_id, now)
        return await _patch_result(session, item, [], deleted=True)

    warnings: list[str] = []
    was_actionable = item.kind in ACTIONABLE
    _apply_fields(item, patch)

    if patch.action == "done":
        await _patch_done(session, item, prefs, now)
    elif patch.action == "undone":
        await _patch_undone(session, item, prefs, now)

    scheduled = False
    if patch.rrule and patch.action != "stop_recurrence":
        scheduled = await _set_rrule(session, item, patch, prefs, now, warnings)
    if not scheduled and patch.when is not None:
        scheduled = await _reschedule(session, item, patch.when, patch.lead, prefs, now, warnings)
    if not scheduled and patch.lead:
        await _shift_by_lead(session, item, patch.lead, prefs, now)
    if not scheduled and not was_actionable and item.kind in ACTIONABLE:
        await _schedule_default(session, item, prefs, now)
    if not scheduled and was_actionable and item.kind not in ACTIONABLE and not item.rec_active:
        await _drop_default(session, item)

    if patch.action == "no_reminder":
        await remove_reminders(session, item.id)
    elif patch.action == "stop_recurrence":
        await stop_recurrence(session, item.id, now)

    _touch(item)
    return await _patch_result(session, item, warnings)


def _apply_fields(item: Item, patch: EditPatch) -> None:
    if patch.title and patch.title.strip():
        item.title = " ".join(patch.title.split())
    if patch.kind:
        item.kind = patch.kind
    if patch.tags_add or patch.tags_remove:
        drop = set(patch.tags_remove)
        item.tags = [t for t in _clean_tags([*(item.tags or []), *patch.tags_add]) if t not in drop]


async def _patch_done(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> None:
    await _lock_parent(session, item)  # a step: decided on fresh rows
    occ = await _first_open(session, item.id)
    if occ is not None:
        await mark_done(session, occ.id, now, prefs)
    elif item.status == "open" and not item.rec_active:
        item.status = "done"
        item.done_at = now
        await _after_done(session, item, prefs, now)


async def _patch_undone(session: AsyncSession, item: Item, prefs: Prefs, now: datetime, *, steps: bool = True) -> None:
    """The latest done occurrence open again (with the item). A "когда-нибудь" item only takes back its own done mark:
    an instance done before it became someday stays history, and none of its occurrences is planned again.
    ``steps``: a parent reopens the steps closed with it (``_after_undone``)."""
    await _lock_parent(session, item)
    conds = [Occurrence.item_id == item.id, Occurrence.status == "done"]
    if item.someday:  # only the occurrence whose "done" closed the item (a stale reminder's ✅), if any
        conds.append(Occurrence.done_at >= item.done_at if item.done_at is not None else false())
    stmt = (
        select(Occurrence)
        .where(*conds)
        .order_by(Occurrence.done_at.desc().nulls_last(), Occurrence.due_at.desc())
        .limit(1)
    )
    occ = await session.scalar(stmt)
    if occ is not None:
        await _undone(session, occ, prefs, now, steps=steps)
    elif item.status == "done":
        closed_at = item.done_at
        _reopen(item)
        await _after_undone(session, item, closed_at, prefs, now, steps=steps)
    else:
        _reopen(item)


async def _reschedule(
    session: AsyncSession,
    item: Item,
    when: When,
    lead: list[int] | None,
    prefs: Prefs,
    now: datetime,
    warnings: list[str],
) -> bool:
    """Move the current open occurrence (or create one) to ``when``. False if ``when`` gives no time.

    "Когда-нибудь" drops the dates instead (``_make_someday``); a fuzzy span ("на следующей неделе") is kept on
    the item, any other time clears it. An instance of a series moved to a span becomes a plain date. The new time is
    when the item notifies: the item's leads go (a series keeps them for its next instances), only ``lead`` of the
    same edit moves the notification before it.
    """
    first = await _first_open(session, item.id)
    when = _keep_future_date(when, first, prefs, now)
    stated, precision, when_warnings, confirm = timeresolve.resolve_when(when, now, prefs)
    warnings.extend(when_warnings)
    if precision == "someday":
        await _make_someday(session, item, when, confirm, prefs, now)
        return True
    if stated is None:
        return False
    series = bool(item.rec_active and item.rrule)
    if not series:  # a series keeps its own spec; only this instance moves
        item.when_spec = _when_dump(when)
        _set_period(item, *timeresolve.when_period(when, now, prefs))
    elif precision in ("period", "soon"):
        precision = "date"
    due, kept = timeresolve.notify_due(stated, precision, when.role, _clean_lead(lead), prefs, now)
    if series:
        if lead:
            item.lead_minutes = _clean_lead(lead)
        elif item.lead_minutes == []:  # a new explicit time switches reminders back on
            item.lead_minutes = None
    else:
        item.lead_minutes = list(kept) if kept else None  # also switches reminders back on
    item.needs_confirm = confirm
    occ = await _move(session, item, first, due, precision)
    await _reopen_for_time(session, item, prefs, now)
    await _replan(session, item, occ, prefs, now, role=when.role)
    return True


def _keep_future_date(when: When, occ: Occurrence | None, prefs: Prefs, now: datetime) -> When:
    """A new time of day alone ("перенеси на 15:00") keeps the date of an occurrence planned for a later day.

    The prompt asks the model to repeat the date (``day: YYYY-MM-DD``); this guards against a model that does not:
    without it the item for Friday would jump to today. Today's / past occurrences keep the usual rule.
    """
    if occ is None or not when.time or when.day or when.at or when.in_min or when.period:
        return when
    tz = prefs.zone
    occ_day = occ.due_at.astimezone(tz).date()
    if occ_day <= now.astimezone(tz).date():
        return when
    return when.model_copy(update={"day": occ_day.isoformat()})


def _stated_at(item: Item, occ: Occurrence, prefs: Prefs) -> datetime:
    """The time the owner stated for an occurrence: its due (the notification time) plus the item's lead shift
    ("Встреча (15:00)" due at 14:30 with ``lead_minutes`` [30] -> 15:00)."""
    shift = timeresolve.lead_shift(occ.precision, _role(item), item.lead_minutes or None)
    return timeresolve.shift_forward(occ.due_at, shift, _rec_zone(item, prefs))


async def _shift_by_lead(session: AsyncSession, item: Item, lead: Sequence[int], prefs: Prefs, now: datetime) -> None:
    """ "Напомни за час" without a new time: the notification moves that long before the time the owner stated
    (``_stated_at``) — of the current open occurrence, or of every instance still ahead for a series (which keeps the
    lead for its next ones). A lead moment already past leaves the stated time itself."""
    leads = _clean_lead(lead)
    role = _role(item)
    if item.rec_active and item.rrule:
        occs = await _open_occurrences(session, item.id, after=now)
    else:
        first = await _first_open(session, item.id)
        occs = [first] if first is not None else []
    stated = [(occ, _stated_at(item, occ, prefs)) for occ in occs]  # with the old leads
    kept: tuple[int, ...] | None = None
    moved: list[Occurrence] = []
    for occ, at in stated:  # earliest first: an instance moving earlier frees its place for the next one
        due, kept = timeresolve.notify_due(at, occ.precision, role, leads, prefs, now)
        moved.append(await _move(session, item, occ, due, occ.precision))
    if item.rec_active and item.rrule:
        item.lead_minutes = leads
    elif stated:
        item.lead_minutes = list(kept) if kept else None
    for occ in moved:
        await _replan(session, item, occ, prefs, now)


async def _set_rrule(
    session: AsyncSession, item: Item, patch: EditPatch, prefs: Prefs, now: datetime, warnings: list[str]
) -> bool:
    """Enable or replace the recurrence and move the first open occurrence to the rule's next date.

    Returns False (with a warning) when the rule is invalid or never occurs again, and for a checklist: a parent's
    ticks would be shared by every instance, a repeating step would never let its parent close.
    """
    if item.parent_id is not None or await _has_steps(session, item.id):
        warnings.append(WARN_STEP_RRULE if item.parent_id is not None else WARN_CHECKLIST_RRULE)
        return False
    try:
        rule = recurrence.normalize_rrule(patch.rrule or "")
    except ValueError:
        warnings.append(timeresolve.WARN_BAD_RRULE)
        return False
    tz = prefs.zone
    first = await _first_open(session, item.id)
    # A cancelled occurrence is no time of the item any more ("когда-нибудь", a task turned into an idea).
    reference = first or await _latest_kept(session, item.id)
    # The series repeats the time the owner stated: the reference occurrence's due plus its lead.
    ref_at = _stated_at(item, reference, prefs) if reference is not None else None
    ref_precision = reference.precision if reference is not None else None
    start, time_spec = _series_start(patch.when, ref_at, ref_precision, prefs, now, span_start=item.period_start)
    dtstart = start.replace(tzinfo=None)
    after, inclusive = now, True
    if first is None and reference is not None and reference.status == "done" and reference.due_at >= now:
        # Nothing open, just marked done ahead of time: the series continues after that instance.
        after, inclusive = ref_at or reference.due_at, False
    if recurrence.next_occurrence(rule, dtstart, tz, after, inclusive=inclusive) is None:
        warnings.append(timeresolve.WARN_BAD_RRULE)
        return False

    role = patch.when.role if patch.when is not None else _role(item)
    spec: dict[str, Any] = {"text": patch.when.text if patch.when is not None else "", "role": role}
    if time_spec:
        spec["time"] = time_spec
    item.when_spec = spec
    _set_period(item, None, None, None)
    item.rrule = rule
    item.rec_time_local = dtstart.time()
    item.rec_dtstart_local = dtstart
    item.rec_tz = prefs.tz
    item.rec_active = True
    if patch.lead:
        item.lead_minutes = _clean_lead(patch.lead)
    elif patch.when is not None:  # a new stated time is the notification time (and switches reminders back on)
        item.lead_minutes = None
    shift = timeresolve.lead_shift(_rec_precision(item), role, item.lead_minutes or None)
    due = timeresolve.next_series_due(rule, dtstart, tz, after, inclusive=inclusive, shift=shift, now=now)
    if due is None:
        warnings.append(timeresolve.WARN_BAD_RRULE)
        return False

    occ = await _move(session, item, first, due, _rec_precision(item))
    # Instances generated by the old rule are dropped; overdue ones are left to be marked missed.
    for other in await _open_occurrences(session, item.id, after=now):
        if other.id != occ.id:
            other.status = "cancelled"
            await _cancel_pending(session, [other.id])
    await _reopen_for_time(session, item, prefs, now)
    await _replan(session, item, occ, prefs, now)
    return True


def _series_start(
    when: When | None,
    ref_at: datetime | None,
    ref_precision: str | None,
    prefs: Prefs,
    now: datetime,
    *,
    span_start: date | None = None,
) -> tuple[datetime, str | None]:
    """Local aware start of a series edited in place, and the time spec it keeps ("HH:MM", slot name or None).

    The time of day comes from ``when`` if it names one, else from the item's current occurrence (``ref_at``: the
    time stated for it, ``ref_precision`` its precision), else from ``task_default_time``; the date from ``when``,
    else the current occurrence (its weekday matters for a bare FREQ=WEEKLY), else today. A fuzzy span ("на этой
    неделе", "на днях": in ``when``, or the item's own with ``span_start``) names no day: its occurrence sits at the
    reminder (2) moment, so the series starts on the span's first day (today once it has begun) at
    ``task_default_time``.
    """
    tz = prefs.zone
    today = now.astimezone(tz).date()
    due, precision, first_day = None, "none", None
    if when is not None:
        due, precision, _, _ = timeresolve.resolve_when(when, now, prefs)
        if precision in _FUZZY:  # "каждый день со следующей недели"
            due, first_day = None, timeresolve.when_period(when, now, prefs)[1] or today
    if ref_at is not None and ref_precision in _FUZZY:  # the item's own span
        first_day, ref_at = first_day or span_start or today, None
    ref_local = ref_at.astimezone(tz) if ref_at is not None else None
    ref_timed = ref_at is not None and ref_precision in ("exact", "slot")

    if due is not None and precision != "date":
        start = due.astimezone(tz)
        time_spec = when.time if precision == "slot" and when is not None else start.strftime("%H:%M")
        return start, time_spec
    if due is not None:
        day = due.astimezone(tz).date()
    elif first_day is not None:
        day = max(first_day, today)
    elif ref_local is not None:
        day = ref_local.date()
    else:
        day = today
    clock_time = ref_local.time() if ref_local is not None else prefs.task_default_time
    start = timeresolve.localize(day, clock_time, tz)
    return start, start.strftime("%H:%M") if ref_timed else None


async def _schedule_default(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> None:
    """An item that just became actionable and has no open occurrence gets the default-time reminder (not a
    "когда-нибудь" one: it has no date on purpose, nor a step of a checklist)."""
    if item.status != "open" or item.someday or item.parent_id is not None or await _has_open(session, item.id):
        return
    occ = await _move(session, item, None, _default_due(now, prefs), "default")
    await _replan(session, item, occ, prefs, now)


async def _make_someday(
    session: AsyncSession, item: Item, when: When, confirm: bool, prefs: Prefs, now: datetime
) -> None:
    """ "Перенеси на когда-нибудь": no date any more — open occurrences are cancelled with their pending reminders,
    a series stops, a finished item is open again."""
    item.when_spec = _when_dump(when)
    _set_period(item, "someday", None, None)
    item.rec_active = False
    item.needs_confirm = confirm
    stmt = select(Occurrence).where(Occurrence.item_id == item.id, Occurrence.status == "open")
    occs = list((await session.scalars(stmt)).all())
    for occ in occs:
        occ.status = "cancelled"
    await _cancel_pending(session, [o.id for o in occs])
    await _reopen_for_time(session, item, prefs, now)


async def _reopen_for_time(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> None:
    """A new time (or "когда-нибудь") makes a finished item open again; a done step takes its closed parent along."""
    was_done = item.status == "done"
    _reopen(item)
    if was_done and item.parent_id is not None:
        await _sync_parent(session, item, prefs, now)


def _set_period(item: Item, kind: str | None, start: date | None, end: date | None) -> None:
    """Fuzzy time of the item (``timeresolve.when_period``); all None makes it a plain dated one."""
    item.period_kind = kind
    item.period_start = start
    item.period_end = end
    item.someday = kind == "someday"


async def _drop_default(session: AsyncSession, item: Item) -> None:
    """An item that is no longer actionable: its open ``default`` occurrences (a time the bot chose only because
    it was a task) -> cancelled with their pending reminders, so an idea neither pings at 19:00 nor sits in
    «Просрочено». Times the owner gave (exact, slot, date, relative, fallback) are kept."""
    stmt = select(Occurrence).where(
        Occurrence.item_id == item.id, Occurrence.status == "open", Occurrence.precision == "default"
    )
    occs = list((await session.scalars(stmt)).all())
    for occ in occs:
        occ.status = "cancelled"
    await _cancel_pending(session, [o.id for o in occs])


# --- recurrence ------------------------------------------------------------------------------------------------


async def ensure_recurring(session: AsyncSession, prefs: Prefs, now: datetime) -> int:
    """For every active series without an open occurrence after ``now``: create the next one (after the latest
    existing due) with reminders. Superseded open occurrences older than 12 h -> missed. Returns the number
    created. Idempotent: two queries when there is nothing to do."""
    now = _utc(now)
    future_open = exists().where(Occurrence.item_id == Item.id, Occurrence.status == "open", Occurrence.due_at > now)
    stmt = select(Item).where(
        Item.rec_active.is_(True), Item.status != "deleted", Item.rrule.is_not(None), ~future_open
    )
    created = 0
    for item in (await session.scalars(stmt)).all():
        if await _create_next(session, item, prefs, now) is not None:
            created += 1
    await _mark_superseded(session, now)
    return created


async def _ensure_next(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> Occurrence | None:
    stmt = select(exists().where(Occurrence.item_id == item.id, Occurrence.status == "open", Occurrence.due_at > now))
    if await session.scalar(stmt):
        return None
    return await _create_next(session, item, prefs, now)


async def _create_next(session: AsyncSession, item: Item, prefs: Prefs, now: datetime) -> Occurrence | None:
    """Next occurrence after max(latest existing due, now); an exhausted or broken rule deactivates the series."""
    max_due = await session.scalar(select(func.max(Occurrence.due_at)).where(Occurrence.item_id == item.id))
    after = max(max_due, now) if max_due is not None else now
    tz = _rec_zone(item, prefs)
    dtstart = item.rec_dtstart_local or datetime.combine(
        now.astimezone(tz).date(), item.rec_time_local or prefs.task_default_time
    )
    # The rule gives the stated times; each instance is due its lead earlier ("за 15 минут" before 10:00).
    shift = timeresolve.lead_shift(_rec_precision(item), _role(item), item.lead_minutes or None)
    try:
        due = timeresolve.next_series_due(
            item.rrule or "", dtstart, tz, timeresolve.shift_forward(after, shift, tz), shift=shift, now=now
        )
    except ValueError:
        log.warning("item %s: invalid stored rrule, recurrence stopped", item.id)
        due = None
    if due is None:
        item.rec_active = False
        _touch(item)
        return None
    occ = await _insert_occurrence(session, item.id, due, _rec_precision(item))
    if occ is None:  # created concurrently (e.g. scheduler tick vs. "Готово")
        return None
    await _plan(session, item, occ, prefs, now)
    return occ


async def _mark_superseded(session: AsyncSession, now: datetime) -> None:
    """Open series instances older than 12 h with a later sibling -> missed, unless a reminder of theirs is still
    to come (a snooze): they stay open until it has been sent (the scheduler cancels reminders of instances that
    are no longer open)."""
    later = aliased(Occurrence)
    superseded = exists().where(
        later.item_id == Occurrence.item_id, later.due_at > Occurrence.due_at, later.status != "cancelled"
    )
    awaited = exists().where(
        Reminder.occurrence_id == Occurrence.id, Reminder.status == "pending", Reminder.fire_at > now
    )
    stmt = (
        select(Occurrence)
        .join(Item, Item.id == Occurrence.item_id)
        .where(
            Occurrence.status == "open",
            Occurrence.due_at < now - SUPERSEDED_AFTER,
            Item.rrule.is_not(None),
            superseded,
            ~awaited,
        )
    )
    stale = (await session.scalars(stmt)).all()
    for occ in stale:
        occ.status = "missed"
    if stale:
        await _cancel_pending(session, [occ.id for occ in stale])


def _rec_zone(item: Item, prefs: Prefs) -> ZoneInfo:
    """The series' own timezone (wall-clock times stay put if prefs.tz changes later)."""
    if item.rec_tz:
        try:
            return ZoneInfo(item.rec_tz)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("item %s: unknown rec_tz, using prefs.tz", item.id)
    return prefs.zone


def _rec_precision(item: Item) -> Precision:
    """Precision of the series' instances, from its When spec: HH:MM -> exact, slot -> slot, none -> default."""
    spec_time = (item.when_spec or {}).get("time")
    if not spec_time:
        return "default"
    return "exact" if _HHMM_RE.match(str(spec_time)) else "slot"


# --- move to tomorrow ------------------------------------------------------------------------------------------


async def move_to_tomorrow(
    session: AsyncSession, occurrence_ids: list[int], prefs: Prefs, now: datetime, *, day: date | None = None
) -> list[Occurrence]:
    """+1 day to the local date at the same time of day, re-plan. A due in the past goes to ``day`` (default:
    tomorrow, which before ``night_cutoff`` is the day that has just started; never a day in the past) at that
    time of day, or to the day after when that moment has passed as well.

    Done/cancelled occurrences, deleted items and "когда-нибудь" ones (no date on purpose) are skipped. Moving onto
    an existing occurrence of the same item (a daily series) merges into it. ``day`` is an optional extension of the
    contract (the evening digest passes its own day + 1).
    """
    now = _utc(now)
    tz = prefs.zone
    past_due_day = max(day or timeresolve.tomorrow(now, prefs), now.astimezone(tz).date())
    moved: dict[int, Occurrence] = {}
    for occurrence_id in dict.fromkeys(occurrence_ids):
        occ = await session.get(Occurrence, occurrence_id)
        if occ is None or occ.status in ("done", "cancelled"):
            continue
        item = await _get_item(session, occ.item_id)
        if item.status == "deleted" or item.someday:
            continue
        local = occ.due_at.astimezone(tz)
        new_day = local.date() + timedelta(days=1) if occ.due_at > now else past_due_day
        due = timeresolve.localize(new_day, local.time(), tz).astimezone(UTC)
        if due <= now:
            due = timeresolve.localize(new_day + timedelta(days=1), local.time(), tz).astimezone(UTC)
        precision = occ.precision
        if precision in ("period", "soon"):  # "на этой неделе" moved to a day is that day now
            precision = "date"
            _set_period(item, None, None, None)
        target = await _move(session, item, occ, due, precision)
        _reopen(item)
        await _replan(session, item, target, prefs, now)
        _touch(item)
        moved[target.id] = target
    return list(moved.values())


# --- internals: occurrences ------------------------------------------------------------------------------------


async def _move(
    session: AsyncSession, item: Item, occ: Occurrence | None, due: datetime, precision: Precision
) -> Occurrence:
    """Put ``occ`` (or a new occurrence) at ``due``, respecting UNIQUE(item_id, due_at): when another occurrence
    already sits there, ``occ`` is cancelled and that one is reopened instead."""
    clash = await session.scalar(select(Occurrence).where(Occurrence.item_id == item.id, Occurrence.due_at == due))
    if clash is not None and (occ is None or clash.id != occ.id):
        if occ is not None:
            occ.status = "cancelled"
            await _cancel_pending(session, [occ.id])
        target = clash
    elif occ is not None:
        target = occ
        target.due_at = due
    else:
        new = await _insert_occurrence(session, item.id, due, precision)
        target = new or await session.scalar(
            select(Occurrence).where(Occurrence.item_id == item.id, Occurrence.due_at == due)
        )
        if target is None:  # pragma: no cover - insert conflicted and the row vanished
            raise RuntimeError(f"could not place occurrence for item {item.id}")
    target.precision = precision
    target.status = "open"
    target.done_at = None
    await session.flush()
    return target


async def _insert_occurrence(
    session: AsyncSession, item_id: int, due: datetime, precision: Precision
) -> Occurrence | None:
    """INSERT … ON CONFLICT (item_id, due_at) DO NOTHING; None when the occurrence already exists."""
    stmt = (
        pg_insert(Occurrence)
        .values(item_id=item_id, due_at=due, precision=precision, status="open")
        .on_conflict_do_nothing(constraint="uq_occurrences_item_due")
        .returning(Occurrence)
    )
    return (await session.scalars(stmt)).first()


async def _first_open(session: AsyncSession, item_id: int) -> Occurrence | None:
    stmt = (
        select(Occurrence)
        .where(Occurrence.item_id == item_id, Occurrence.status == "open")
        .order_by(Occurrence.due_at, Occurrence.id)
        .limit(1)
    )
    return await session.scalar(stmt)


async def _latest_kept(session: AsyncSession, item_id: int) -> Occurrence | None:
    """The latest occurrence by due that was not cancelled (done, missed, or open)."""
    stmt = (
        select(Occurrence)
        .where(Occurrence.item_id == item_id, Occurrence.status != "cancelled")
        .order_by(Occurrence.due_at.desc(), Occurrence.id.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def _open_occurrences(session: AsyncSession, item_id: int, *, after: datetime) -> list[Occurrence]:
    stmt = (
        select(Occurrence)
        .where(Occurrence.item_id == item_id, Occurrence.status == "open", Occurrence.due_at > after)
        .order_by(Occurrence.due_at, Occurrence.id)
    )
    return list((await session.scalars(stmt)).all())


async def _has_open(session: AsyncSession, item_id: int) -> bool:
    stmt = select(exists().where(Occurrence.item_id == item_id, Occurrence.status == "open"))
    return bool(await session.scalar(stmt))


async def _occurrence_ids(session: AsyncSession, item_id: int) -> list[int]:
    return list((await session.scalars(select(Occurrence.id).where(Occurrence.item_id == item_id))).all())


# --- internals: reminders --------------------------------------------------------------------------------------


async def _replan(
    session: AsyncSession, item: Item, occ: Occurrence, prefs: Prefs, now: datetime, *, role: str | None = None
) -> list[Reminder]:
    await _cancel_pending(session, [occ.id])
    return await _plan(session, item, occ, prefs, now, role=role)


async def _plan(
    session: AsyncSession, item: Item, occ: Occurrence, prefs: Prefs, now: datetime, *, role: str | None = None
) -> list[Reminder]:
    """Insert the reminder plan for an open occurrence (nothing for deleted / silenced items)."""
    if item.status == "deleted" or occ.status != "open" or item.lead_minutes == []:
        return []
    plans = timeresolve.plan_reminders(
        occ.due_at,
        occ.precision,
        role or _role(item),
        item.lead_minutes or None,
        prefs,
        now,
        period=_period_span(item, occ),
    )
    return await _add_reminders(session, occ.id, plans)


def _period_span(item: Item, occ: Occurrence) -> tuple[date, date] | None:
    """The span reminder (1) of a ``period`` occurrence is planned from."""
    if occ.precision != "period" or item.period_start is None or item.period_end is None:
        return None
    return item.period_start, item.period_end


async def _add_reminders(session: AsyncSession, occurrence_id: int, plans: Iterable[ReminderPlan]) -> list[Reminder]:
    reminders = [
        Reminder(
            occurrence_id=occurrence_id, fire_at=p.fire_at, lead_minutes=p.lead_minutes, auto=p.auto, status="pending"
        )
        for p in plans
    ]
    if reminders:
        session.add_all(reminders)
        await session.flush()
    return reminders


async def _cancel_pending(
    session: AsyncSession, occurrence_ids: Sequence[int], *, until: datetime | None = None
) -> int:
    """pending -> cancelled (with ``until``: only those firing at or before it); sent/sending/missed reminders are
    left alone.

    The occurrence rows are locked first (until commit), so two re-plans of one occurrence (a double tap on
    "+15 мин", a button racing an edit job) run one after the other: the second one sees the first one's new
    reminders as pending and cancels them instead of adding a duplicate next to them.
    """
    if not occurrence_ids:
        return 0
    lock = select(Occurrence.id).where(Occurrence.id.in_(occurrence_ids)).order_by(Occurrence.id).with_for_update()
    await session.execute(lock)
    stmt = select(Reminder).where(Reminder.occurrence_id.in_(occurrence_ids), Reminder.status == "pending")
    if until is not None:
        stmt = stmt.where(Reminder.fire_at <= until)
    reminders = (await session.scalars(stmt)).all()
    for reminder in reminders:
        reminder.status = "cancelled"
    return len(reminders)


# --- internals: items ------------------------------------------------------------------------------------------


async def _get_item(session: AsyncSession, item_id: int) -> Item:
    item = await session.get(Item, item_id)
    if item is None:
        raise NotFoundError(f"item {item_id} not found")
    return item


async def _get_occ(session: AsyncSession, occurrence_id: int) -> Occurrence:
    occ = await session.get(Occurrence, occurrence_id)
    if occ is None:
        raise NotFoundError(f"occurrence {occurrence_id} not found")
    return occ


async def _reload_occ(session: AsyncSession, occurrence_id: int) -> Occurrence:
    """The occurrence as committed now (the object already in the session is refreshed)."""
    stmt = select(Occurrence).where(Occurrence.id == occurrence_id).execution_options(populate_existing=True)
    occ = await session.scalar(stmt)
    if occ is None:
        raise NotFoundError(f"occurrence {occurrence_id} not found")
    return occ


async def _soft_delete(session: AsyncSession, item: Item, now: datetime) -> None:
    """status deleted, pending reminders cancelled; a parent takes its steps along (same ``deleted_at``, so that
    restoring it brings back exactly these)."""
    item.status = "deleted"
    item.deleted_at = now
    item.rec_active = False
    await _cancel_pending(session, await _occurrence_ids(session, item.id))
    _touch(item)
    if item.parent_id is None:
        stmt = select(Item).where(Item.parent_id == item.id, Item.status != "deleted")
        for step in (await session.scalars(stmt)).all():
            await _soft_delete(session, step, now)


async def _close_if_finished(session: AsyncSession, item: Item, now: datetime) -> None:
    """A one-off (or finished) item with nothing open left becomes done."""
    if item.status == "open" and not item.rec_active and not await _has_open(session, item.id):
        item.status = "done"
        item.done_at = now


def _reopen(item: Item) -> None:
    if item.status != "deleted":
        item.status = "open"
        item.done_at = None


def _touch(item: Item) -> None:
    item.updated_at = clock.utcnow()


async def _patch_result(
    session: AsyncSession, item: Item, warnings: list[str], *, deleted: bool = False
) -> PatchResult:
    occ = await current_occurrence(session, item.id)
    reminders = await pending_reminders(session, occ.id) if occ is not None else []
    return PatchResult(item=item, occurrence=occ, reminders=reminders, warnings=warnings, deleted=deleted)


async def _owner_prefs(session: AsyncSession) -> Prefs:
    """Prefs of the (single) owner, for callers that do not pass them."""
    row = await session.scalar(select(UserSettings).order_by(UserSettings.owner_id).limit(1))
    return Prefs.from_stored(row.prefs) if row is not None else Prefs()


def _role(item: Item) -> str:
    return str((item.when_spec or {}).get("role") or "event")


def _when_dump(when: When | None) -> dict[str, Any] | None:
    return when.model_dump(mode="json", exclude_none=True) if when is not None else None


def _default_due(now: datetime, prefs: Prefs) -> datetime:
    """Next ``task_default_time``: today if still ahead, otherwise tomorrow."""
    tz = prefs.zone
    today = now.astimezone(tz).date()
    due = timeresolve.localize(today, prefs.task_default_time, tz)
    if due <= now:
        due = timeresolve.localize(today + timedelta(days=1), prefs.task_default_time, tz)
    return due.astimezone(UTC)


def _clean_tags(tags: Iterable[str]) -> list[str]:
    out: list[str] = []
    for tag in tags:
        tag = re.sub(r"\s+", "_", str(tag).strip().lstrip("#").lower())[:30]
        if tag and tag not in out:
            out.append(tag)
    return out


def _clean_lead(lead: Iterable[int] | None) -> list[int] | None:
    """Unique leads within [0, 30 days], largest first; empty -> None (no lead: the stated time itself)."""
    values = sorted({int(m) for m in lead or () if 0 <= int(m) <= MAX_LEAD_MINUTES}, reverse=True)
    return values or None


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
