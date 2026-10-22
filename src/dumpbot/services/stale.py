"""Stale items and reminders nobody reacted to, read from the events journal (docs/features/iteration-4.md §4, §6).

An open item is *stale* when it sat in digests on ``DIGESTS_IN_A_ROW`` (3) days with nothing done about it, or was
moved ``MOVES`` (2) times: the scheduler then asks about it once — "«Сделать карту» висит 15 дней. Что делаем?"
[✅ Сделано] [💤 Когда-нибудь] [🗑 Удалить] [Оставить] — at most one such question a day, never in quiet hours
(``workers.scheduler``).

What counts, from ``services.events``:

- an *action* on an item (``is_action``): a button press naming it (its reminder, a list's [✅ N], a card…), a Mini
  App call on it other than a read (``GET``), a change by a text message or the bulk move (``edit``); an action on a
  step of its checklist ([☑ N], the Mini App's step toggle) is an action on the parent too;
- a *move*: an action marked ``"move": true`` ([📅 Завтра], [➡️ След. неделя / месяц], a day of the picker, a new
  time on another day from the Mini App or a text edit, «📅 Перенести невыполненное на завтра»);
- the item's digests in a row: the local days with a digest listing it (``digest_item`` events) since its last action,
  or since it was saved — the morning, the evening digest and the weekly review of one day are one day, so "3" means
  about three days, not one missed reminder and a day. An item whose span has not begun is not counted at all, and
  the weekly review's "сроки" lines of an item still in its span do not count (§4: a span is left alone while it runs,
  the review lists it on purpose);
- its moves since the last question about it (``stale_ask``), or since it was saved;
- a question left unanswered (no action after it) is not asked again; [Оставить] is an action (and so is any other
  touch of the item), so after it the digests are counted afresh and so are the moves.

Active series are never stale (a new instance every time: ignoring one is not the item hanging), nor "когда-нибудь"
items (they are out of the plan on purpose). Of several stale items the one saved first is asked about.

``unanswered_reminders`` (the weekly review, §6): reminders sent in a span whose item had no action since and whose
instance is still not done.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

from sqlalchemy import ColumnElement, Date, and_, cast, distinct, exists, func, not_, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from dumpbot import clock
from dumpbot.db.models import Event, Item, Occurrence
from dumpbot.domain import timeresolve
from dumpbot.domain.prefs import Prefs
from dumpbot.services import events

__all__ = [
    "DIGESTS_IN_A_ROW",
    "MOVES",
    "Stale",
    "asked_on",
    "candidates",
    "is_action",
    "is_move",
    "pick",
    "unanswered_reminders",
]

DIGESTS_IN_A_ROW: Final = 3
MOVES: Final = 2
ACTION_TYPES: Final = (events.BUTTON, events.MINIAPP, events.EDIT)
_NOT_DONE: Final = ("open", "missed")
WEEKLY: Final = "weekly"  # data["digest"] of the weekly review's lines
SPAN_SECTION: Final = "p"  # data["sec"] of its "сроки" section (``views.SEC_PERIOD``)


@dataclass(frozen=True)
class Stale:
    """A stale item: when it was saved, its digest days since the last action and its moves since the last
    question."""

    item_id: int
    created_at: datetime
    digests: int
    moves: int


def is_action(ev: Any = Event) -> ColumnElement[bool]:
    """An owner's action on the event's item: a button press, a Mini App call other than a read, an ``edit``.
    ``ev``: ``Event`` or an alias of it."""
    route = ev.data["route"].astext
    return and_(
        ev.type.in_(ACTION_TYPES),
        or_(ev.type != events.MINIAPP, route.is_(None), not_(route.startswith("GET "))),
    )


def is_move(ev: Any = Event) -> ColumnElement[bool]:
    """An action that moved the item to another day (``"move": true``)."""
    return and_(is_action(ev), ev.data[events.MOVE].as_boolean().is_(True))


async def candidates(
    session: AsyncSession, prefs: Prefs | None = None, *, now: datetime | None = None, limit: int = 10
) -> list[Stale]:
    """The stale items, saved first first (see the module docstring); days are local (``prefs.tz``)."""
    tz = (prefs or Prefs()).tz
    today = (now or clock.utcnow()).astimezone((prefs or Prefs()).zone).date()
    step = aliased(Item)  # an action on a step of a checklist ([☑ N]) is one on its parent too
    acts = union_all(
        select(Event.item_id.label("item_id"), Event.created_at.label("at")).where(
            Event.item_id.is_not(None), is_action()
        ),
        select(step.parent_id.label("item_id"), Event.created_at.label("at"))
        .join(step, step.id == Event.item_id)
        .where(step.parent_id.is_not(None), is_action()),
    ).subquery("acts")
    last_act = (
        select(acts.c.item_id.label("item_id"), func.max(acts.c.at).label("at"))
        .group_by(acts.c.item_id)
        .subquery("last_act")
    )
    last_ask = (
        select(Event.item_id.label("item_id"), func.max(Event.created_at).label("at"))
        .where(Event.item_id.is_not(None), Event.type == events.STALE_ASK)
        .group_by(Event.item_id)
        .subquery("last_ask")
    )
    shown = aliased(Event)
    in_span = and_(Item.period_end.is_not(None), Item.period_end >= today)
    review_span_line = and_(
        func.coalesce(shown.data["digest"].astext, "") == WEEKLY,
        func.coalesce(shown.data["sec"].astext, "") == SPAN_SECTION,
    )
    digests = (
        select(func.count(distinct(cast(func.timezone(tz, shown.created_at), Date))))
        .where(
            shown.item_id == Item.id,
            shown.type == events.DIGEST_ITEM,
            shown.created_at > func.greatest(Item.created_at, last_act.c.at),  # GREATEST skips NULL
            not_(and_(in_span, review_span_line)),
            or_(Item.period_start.is_(None), Item.period_start <= today),  # a span not begun is not hanging
        )
        .scalar_subquery()
    )
    moved = aliased(Event)
    moves = (
        select(func.count(moved.id))
        .where(
            moved.item_id == Item.id, is_move(moved), moved.created_at > func.greatest(Item.created_at, last_ask.c.at)
        )
        .scalar_subquery()
    )
    counted = (
        select(
            Item.id.label("item_id"),
            Item.created_at.label("created_at"),
            digests.label("digests"),
            moves.label("moves"),
        )
        .outerjoin(last_act, last_act.c.item_id == Item.id)
        .outerjoin(last_ask, last_ask.c.item_id == Item.id)
        .where(
            Item.status == "open",
            Item.someday.is_(False),
            Item.rec_active.is_(False),
            or_(last_ask.c.at.is_(None), last_act.c.at > last_ask.c.at),  # asked and ignored: not asked again
        )
        .subquery("counted")
    )
    stmt = (
        select(counted)
        .where(or_(counted.c.digests >= DIGESTS_IN_A_ROW, counted.c.moves >= MOVES))
        .order_by(counted.c.created_at, counted.c.item_id)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [Stale(row.item_id, row.created_at, int(row.digests), int(row.moves)) for row in rows]


async def pick(session: AsyncSession, prefs: Prefs | None = None, *, now: datetime | None = None) -> Stale | None:
    """The item to ask about now, if any."""
    found = await candidates(session, prefs, now=now, limit=1)
    return found[0] if found else None


async def asked_on(session: AsyncSession, day: date, prefs: Prefs) -> set[int]:
    """The items the stale question asked about on local ``day``."""
    start, end = _day_bounds(day, prefs)
    stmt = select(distinct(Event.item_id)).where(
        Event.type == events.STALE_ASK, Event.created_at >= start, Event.created_at < end, Event.item_id.is_not(None)
    )
    return {int(item_id) for item_id in (await session.scalars(stmt)).all()}


async def unanswered_reminders(session: AsyncSession, start: datetime, end: datetime) -> list[tuple[int, int]]:
    """(occurrence id, item id) of the reminders sent in [``start``, ``end``) that got no reaction: no action on the
    item since, its instance still not done (open, or left missed by a series), the item open and not "когда-нибудь".
    The latest such reminder per item."""
    sent = aliased(Event)
    later = aliased(Event)
    reacted = exists().where(later.item_id == sent.item_id, is_action(later), later.created_at > sent.created_at)
    stmt = (
        select(sent.occurrence_id, sent.item_id)
        .distinct(sent.item_id)
        .join(Occurrence, Occurrence.id == sent.occurrence_id)
        .join(Item, Item.id == Occurrence.item_id)
        .where(
            sent.type == events.REMINDER_SENT,
            sent.created_at >= start,
            sent.created_at < end,
            ~reacted,
            Occurrence.status.in_(_NOT_DONE),
            Item.status == "open",
            Item.someday.is_(False),
        )
        .order_by(sent.item_id, sent.created_at.desc())
    )
    return [(int(occ_id), int(item_id)) for occ_id, item_id in (await session.execute(stmt)).tuples()]


def _day_bounds(day: date, prefs: Prefs) -> tuple[datetime, datetime]:
    tz = prefs.zone
    start = timeresolve.localize(day, time(0), tz).astimezone(UTC)
    return start, timeresolve.localize(day + timedelta(days=1), time(0), tz).astimezone(UTC)
