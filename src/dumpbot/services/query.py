"""Read side: agenda, task lists, search, QuerySpec execution, AI spend summary (architecture.md §4.11).

Nothing here writes. Deleted items are always excluded. Search uses the persisted ``items.search_tsv``
(``to_tsvector('russian', …)``: Cyrillic is stemmed by the Russian stemmer, ASCII words by the English one),
``websearch_to_tsquery('russian', q)``, plus an ILIKE fallback on the title and a tag match.

Fuzzy time (docs/features/iteration-2.md §A): an open item whose calendar period (week / month / season) contains
the day is listed in ``Agenda.period_items`` instead of open / overdue; "когда-нибудь" items are only in
``someday_items``; a due-date query also finds items whose period overlaps its range.

Checklists and related items (§C): a step shows in the top-level lists (/tasks, /recent, digests, queries) only
when it has a time of its own, named after its parent ("(из «Переезд»)"); a parent row counts its steps ("2/5").
``related_candidates`` finds earlier open items sharing a subject word (``services.related``) with a new message for
the "RELATED CANDIDATES" prompt line;
``card_extras`` loads what an item card shows besides the item (checklist, related items, parent, and — iteration 3,
§6 — the transcript of the voice note it came from, ``heard``).

The Mini App (§D): ``feed`` pages through all items newest first with filters (keyset cursor), ``rows_by_ids`` loads
the rows an answer named, ``item_images`` / ``image_counts`` find the pictures of the message an item came from.

Iteration 3 (§2): ``list_rows`` reads the numbered lines of a list message (its [✅ N] buttons) as they are now.

Iteration 4 (§6), the weekly review: ``review_period_rows`` (the spans of this and next week / month),
``someday_for_review`` (a few "когда-нибудь" items, the ones the reviews showed least recently first),
``undated_tasks`` (open tasks without any date).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import ColumnElement, DateTime, Select, and_, exists, func, literal, literal_column, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlalchemy.sql.selectable import Subquery

from dumpbot.ai.schemas import QuerySpec
from dumpbot.db.models import AiUsage, Event, Item, ItemLink, Occurrence, RawMessage
from dumpbot.domain import recurrence, timeresolve
from dumpbot.domain.kinds import ACTIONABLE, CALENDAR_PERIODS
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import Agenda, CardExtras, RelatedRef, Row, Step, UsageSummary
from dumpbot.services.ingest import UNHEARD_KEY
from dumpbot.services.items import current_occurrence, current_occurrence_order
from dumpbot.services.related import message_words, subject_lexeme_array

__all__ = [
    "Agenda",
    "Row",
    "UsageSummary",
    "agenda",
    "card_extras",
    "feed",
    "find_edit_candidates",
    "heard",
    "image_counts",
    "item_images",
    "items_of_raw",
    "list_rows",
    "open_tasks",
    "recent",
    "recurring",
    "related_candidates",
    "related_refs",
    "review_period_rows",
    "rows_by_ids",
    "rows_for_llm",
    "run_query",
    "search",
    "someday_for_review",
    "someday_items",
    "undated_tasks",
    "usage_summary",
]

log = logging.getLogger(__name__)

OVERDUE_DAYS: Final = 30
EDIT_WINDOW_DAYS: Final = 30
SNIPPET_LEN: Final = 200
# Statuses that count as "not done" for a day: a superseded instance of a series is marked missed after 12 h,
# which would otherwise hide this morning's 09:00 instance from the 21:00 evening digest.
_NOT_DONE: Final = ("open", "missed")
_MONTHS_NOM: Final = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)
_LOCAL_FMT: Final = "%Y-%m-%d %H:%M"
_RU = literal_column("'russian'::regconfig")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_ACTIONABLE_KINDS: Final = tuple(sorted(ACTIONABLE))
_CALENDAR_PERIODS: Final = tuple(sorted(CALENDAR_PERIODS))
RELATED_DAYS: Final = 365  # related candidates are items saved within this many days
RELATED_LIMIT: Final = 5
FEED_STATUSES: Final = ("any", "open", "done", "deleted")  # any = not deleted; deleted = the trash
IMAGE_TYPES: Final = ("photo", "image_document")
_VOICE_TYPES: Final = ("voice", "audio")  # messages whose transcript a card shows ("🎤 «…»")
# Pictures the Mini App previews (raster formats a browser shows; never SVG from our own origin).
PREVIEW_MIME: Final = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})


# --- agenda / lists --------------------------------------------------------------------------------------------


async def agenda(session: AsyncSession, prefs: Prefs, now: datetime, *, day_offset: int = 0) -> Agenda:
    """Occurrences of one local day: not done (open, or missed instances of a series) by time; overdue open
    ones from the previous 30 days (only for today); done that day (by done_at); open items whose calendar
    period contains the day (they are not in open / overdue while the period runs: "на этой неделе" is due on
    Friday 19:00 but still fine on Saturday). "Когда-нибудь" items are never due, whatever occurrence they kept."""
    tz = prefs.zone
    day = _utc(now).astimezone(tz).date() + timedelta(days=day_offset)
    start = timeresolve.localize(day, time(0), tz).astimezone(UTC)
    end = timeresolve.localize(day + timedelta(days=1), time(0), tz).astimezone(UTC)
    dated = and_(_no_running_period(day), Item.someday.is_(False))  # not in period_items, not someday

    open_rows = await _occ_rows(
        session,
        Occurrence.status.in_(_NOT_DONE),
        Occurrence.due_at >= start,
        Occurrence.due_at < end,
        dated,
        order=(Occurrence.due_at, Occurrence.id),
    )
    overdue: list[Row] = []
    if day_offset == 0:
        overdue = await _occ_rows(
            session,
            Occurrence.status == "open",
            Occurrence.due_at < start,
            Occurrence.due_at >= start - timedelta(days=OVERDUE_DAYS),
            dated,
            order=(Occurrence.due_at, Occurrence.id),
        )
    done = await _occ_rows(
        session,
        Occurrence.status == "done",
        Occurrence.done_at >= start,
        Occurrence.done_at < end,
        order=(Occurrence.done_at, Occurrence.id),
    )
    period_rows = await _period_rows(session, day)
    return Agenda(day=day, open=open_rows, overdue=overdue, done=done, period_items=period_rows)


async def done_after_day(session: AsyncSession, prefs: Prefs, day: date) -> list[Row]:
    """Occurrences due on local ``day`` and done after it ended (an evening's reminder held until the morning and
    ticked then): ``Agenda.done`` of that day counts only those done on it."""
    tz = prefs.zone
    start = timeresolve.localize(day, time(0), tz).astimezone(UTC)
    end = timeresolve.localize(day + timedelta(days=1), time(0), tz).astimezone(UTC)
    return await _occ_rows(
        session,
        Occurrence.status == "done",
        Occurrence.due_at >= start,
        Occurrence.due_at < end,
        Occurrence.done_at >= end,
        and_(_no_running_period(day), Item.someday.is_(False)),
        order=(Occurrence.due_at, Occurrence.id),
    )


async def _period_rows(session: AsyncSession, day: date) -> list[Row]:
    """Open items whose week / month / season contains ``day``: the shortest (soonest ending) span first."""
    occ = _occ_subquery(open_only=True)
    stmt = (
        _item_select(occ)
        .where(
            Item.status == "open",
            Item.period_kind.in_(_CALENDAR_PERIODS),
            Item.period_start <= day,
            Item.period_end >= day,
        )
        .order_by(Item.period_end, Item.period_start.desc(), Item.created_at, Item.id)
    )
    return await _item_rows(session, stmt)


def _no_running_period(day: date) -> ColumnElement[bool]:
    """Not an item whose calendar period still runs on ``day`` (``Agenda.period_items`` lists those)."""
    return or_(
        Item.period_kind.is_(None),
        Item.period_kind.not_in(_CALENDAR_PERIODS),
        Item.period_end.is_(None),
        Item.period_end < day,
    )


async def open_tasks(session: AsyncSession, prefs: Prefs, now: datetime, limit: int = 30) -> list[Row]:
    """Actionable open items: those with a due time first (earliest open occurrence ascending), then the rest
    (newest first). "Когда-нибудь" items are left for ``someday_items``."""
    occ = _occ_subquery(open_only=True)
    stmt = (
        _item_select(occ)
        .where(Item.kind.in_(_ACTIONABLE_KINDS), Item.status == "open", Item.someday.is_(False), _listed())
        .order_by(occ.c.due_at.asc().nulls_last(), Item.created_at.desc(), Item.id.desc())
        .limit(limit)
    )
    return await _item_rows(session, stmt)


async def someday_items(session: AsyncSession, limit: int = 50) -> list[Row]:
    """Open "когда-нибудь" items of any kind (``/someday``), newest first."""
    occ = _occ_subquery(open_only=True)
    stmt = (
        _item_select(occ)
        .where(Item.someday.is_(True), Item.status == "open", _listed())
        .order_by(Item.created_at.desc(), Item.id.desc())
        .limit(limit)
    )
    return await _item_rows(session, stmt)


async def review_period_rows(
    session: AsyncSession, weeks: tuple[date, date], months: tuple[date, date], limit: int = 30
) -> list[Row]:
    """Open items of a calendar span that overlaps ``weeks`` (a week span) or ``months`` (a month or a season): the
    weekly review's "this and next week / month" (docs/features/iteration-4.md §6), the shortest span first."""
    occ = _occ_subquery(open_only=True)

    def overlaps(span: tuple[date, date]) -> ColumnElement[bool]:
        return and_(Item.period_start <= span[1], Item.period_end >= span[0])

    stmt = (
        _item_select(occ)
        .where(
            Item.status == "open",
            Item.someday.is_(False),
            or_(
                and_(Item.period_kind == "week", overlaps(weeks)),
                and_(Item.period_kind.in_(("month", "season")), overlaps(months)),
            ),
        )
        .order_by(Item.period_end, Item.period_start.desc(), Item.created_at, Item.id)
        .limit(limit)
    )
    return await _item_rows(session, stmt)


async def someday_for_review(
    session: AsyncSession, limit: int, *, before: datetime | None = None, digest: str = "weekly"
) -> tuple[list[Row], int]:
    """Up to ``limit`` open "когда-нибудь" items for the weekly review — never shown by a review (``digest_item``
    events of ``digest``) first, then the ones shown longest ago, the oldest saved first — and how many there are.
    ``before``: only reviews before it count (the review's own day: a re-render after [✅ N] lists the same items)."""
    shown = [Event.item_id == Item.id, Event.type == "digest_item", Event.data["digest"].astext == digest]
    if before is not None:
        shown.append(Event.created_at < before)
    last_shown = select(func.max(Event.created_at)).where(*shown).scalar_subquery()
    occ = _occ_subquery(open_only=True)
    where = (Item.someday.is_(True), Item.status == "open", _listed())
    stmt = (
        _item_select(occ).where(*where).order_by(last_shown.asc().nulls_first(), Item.created_at, Item.id).limit(limit)
    )
    total = await session.scalar(select(func.count()).select_from(Item).where(*where))
    return await _item_rows(session, stmt), int(total or 0)


async def undated_tasks(session: AsyncSession, limit: int = 30) -> list[Row]:
    """Open tasks / events / purchases the owner never gave a date (the weekly review's last section): no open
    occurrence, or only the time the bot chose for a task without one (``default``: "купить молоко" got today 19:00);
    not a span, not "когда-нибудь", not a series. Steps live in their parent's card; the oldest saved first."""
    has_date = exists().where(
        Occurrence.item_id == Item.id, Occurrence.status == "open", Occurrence.precision != "default"
    )
    stmt = (
        _item_select(_occ_subquery(open_only=True))
        .where(
            Item.kind.in_(_ACTIONABLE_KINDS),
            Item.status == "open",
            Item.someday.is_(False),
            Item.parent_id.is_(None),
            Item.rec_active.is_(False),
            ~has_date,
        )
        .order_by(Item.created_at, Item.id)
        .limit(limit)
    )
    return await _item_rows(session, stmt)


async def recent(session: AsyncSession, limit: int = 15) -> list[Row]:
    """Latest saved items (by created_at), not deleted (steps only with a time of their own)."""
    occ = _occ_subquery(open_only=False)
    stmt = (
        _item_select(occ)
        .where(Item.status != "deleted", _listed())
        .order_by(Item.created_at.desc(), Item.id.desc())
        .limit(limit)
    )
    return await _item_rows(session, stmt)


async def recurring(session: AsyncSession) -> list[Row]:
    """Active series with their nearest open occurrence."""
    occ = _occ_subquery(open_only=True)
    stmt = (
        _item_select(occ)
        .where(Item.rec_active.is_(True), Item.status != "deleted")
        .order_by(occ.c.due_at.asc().nulls_last(), Item.id)
    )
    return await _item_rows(session, stmt)


async def list_rows(
    session: AsyncSession, refs: Sequence[tuple[int | None, int]], *, current: bool = False
) -> list[Row | None]:
    """The numbered lines of a list message as they are now (docs/features/iteration-3.md §2), one per
    ``(occurrence id | None, item id)``, in order: the item with that occurrence — or, when it is gone (cancelled:
    moved onto another instance, the item became "когда-нибудь") or there was none, with the item's current one
    (none when that is cancelled too: the line is the item itself). None for a missing or deleted item.

    ``current`` (/tasks, a line per item): an instance of a series that was left ``missed`` (the next one is due
    now) stands for the item's current one too — the line is the task, not yesterday's instance."""
    if not refs:
        return []
    item_ids = {item_id for _, item_id in refs}
    stmt = select(Item).where(Item.id.in_(item_ids), Item.status != "deleted").options(defer(Item.search_tsv))
    found = {item.id: item for item in (await session.scalars(stmt)).all()}
    occ_ids = {occ_id for occ_id, _ in refs if occ_id}
    occs: dict[int, Occurrence] = {}
    if occ_ids:
        occs = {o.id: o for o in (await session.scalars(select(Occurrence).where(Occurrence.id.in_(occ_ids)))).all()}
    rows: list[Row | None] = []
    for occ_id, item_id in refs:
        item = found.get(item_id)
        if item is None:
            rows.append(None)
            continue
        occ = occs.get(occ_id) if occ_id else None
        gone = ("cancelled", "missed") if current else ("cancelled",)
        if occ is None or occ.item_id != item.id or occ.status in gone:
            occ = await current_occurrence(session, item.id)
            if occ is not None and occ.status == "cancelled":
                occ = None
        rows.append(
            _row(
                item,
                occurrence_id=occ.id if occ is not None else None,
                due_at=occ.due_at if occ is not None else None,
                occ_status=occ.status if occ is not None else None,
            )
        )
    await _with_family(session, [row for row in rows if row is not None])
    return rows


async def items_of_raw(session: AsyncSession, raw_ids: Sequence[int]) -> list[Row]:
    """Not deleted items saved from these raw messages (a message or the parts of an album), oldest first, with
    their current occurrence. Steps of a checklist are not counted: a reply to the message edits the task."""
    if not raw_ids:
        return []
    occ = _occ_subquery(open_only=False)
    stmt = (
        _item_select(occ)
        .where(Item.raw_message_id.in_(list(raw_ids)), Item.status != "deleted", Item.parent_id.is_(None))
        .order_by(Item.id)
    )
    return await _item_rows(session, stmt)


# --- related items and checklists ------------------------------------------------------------------------------


async def related_candidates(
    session: AsyncSession, text: str, now: datetime, *, exclude_raw_ids: Sequence[int] = (), limit: int = RELATED_LIMIT
) -> list[Row]:
    """Earlier items that share a subject word with ``text`` (§C1, for the "RELATED CANDIDATES" line): full text with
    an OR of the message's subject lexemes (``services.related``: no infinitives, reminder / time words, common verbs
    or numbers). Best first: most of those lexemes in the title / details, then in the whole item (with the message
    it came from), then ``ts_rank``, then newest — a word in the title beats one only in the source text of a
    multi-item message, and two shared words beat one. Only open items (not done, not deleted; an actionable item
    whose current occurrence is done counts as done, an active series stays; a past-due one the owner has not ticked
    is still open), top-level, saved in the last ``RELATED_DAYS`` days, not from ``exclude_raw_ids`` (the message
    itself). Nothing left of the message -> [].

    One statement: the message's subject lexemes are worked out inside it (``related.subject_lexeme_array``), and the
    rows come without the checklist counts of ``_with_family`` (the prompt does not show them)."""
    words = message_words(text)
    if not words:
        return []
    lx = select(subject_lexeme_array(words).label("lexemes")).cte("lx")
    lexemes = lx.c.lexemes
    tsq = func.to_tsquery(literal_column("'simple'::regconfig"), func.array_to_string(lexemes, " | "))
    cutoff = _utc(now) - timedelta(days=RELATED_DAYS)
    occ = _occ_subquery(open_only=False)
    stmt = (
        _item_select(occ)
        .join(lx, true())
        .where(
            func.cardinality(lexemes) > 0,
            Item.status == "open",
            or_(
                Item.kind.not_in(_ACTIONABLE_KINDS),
                Item.rec_active.is_(True),
                occ.c.occ_status.is_distinct_from("done"),
            ),
            Item.parent_id.is_(None),
            Item.created_at >= cutoff,
            Item.search_tsv.op("@@", is_comparison=True)(tsq),
        )
    )
    if exclude_raw_ids:
        stmt = stmt.where(or_(Item.raw_message_id.is_(None), Item.raw_message_id.not_in(list(exclude_raw_ids))))
    title = func.to_tsvector(_RU, func.concat_ws(" ", Item.title, Item.details))
    stmt = stmt.order_by(
        _lexeme_hits(title, lexemes).desc(),
        _lexeme_hits(Item.search_tsv, lexemes).desc(),
        func.ts_rank(Item.search_tsv, tsq).desc(),
        Item.created_at.desc(),
        Item.id.desc(),
    )
    result = await session.execute(stmt.limit(limit))
    return [
        _row(item, occurrence_id=occ_id, due_at=due_at, occ_status=occ_status)
        for item, occ_id, due_at, occ_status in result.all()
    ]


def _lexeme_hits(doc: ColumnElement[Any], lexemes: ColumnElement[Any]) -> ColumnElement[int]:
    """How many of ``lexemes`` (a ``text[]``) the tsvector ``doc`` has: its length minus the length without them."""
    return func.length(doc) - func.length(func.ts_delete(doc, lexemes))


async def related_refs(session: AsyncSession, item_id: int) -> list[RelatedRef]:
    """Items linked to this one in either direction (``item_links``), not deleted, newest first."""
    linked = or_(
        and_(ItemLink.item_id == item_id, ItemLink.related_id == Item.id),
        and_(ItemLink.related_id == item_id, ItemLink.item_id == Item.id),
    )
    stmt = (
        select(Item.id, Item.kind, Item.title, Item.created_at)
        .join(ItemLink, linked)
        .where(Item.status != "deleted", Item.id != item_id)
        .distinct()
        .order_by(Item.created_at.desc(), Item.id.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [RelatedRef(item_id=i, kind=kind, title=title, created_at=created) for i, kind, title, created in rows]


async def card_extras(session: AsyncSession, item: Item, *, with_heard: bool = True) -> CardExtras:
    """What the item's card shows besides the item: its checklist (a top-level item), the related items, the title
    of its parent (a step), what was heard in the voice note it came from (not looked up without ``with_heard``:
    the card shows another transcript)."""
    steps = await _steps(session, item.id) if item.parent_id is None else []
    parent_title = None
    if item.parent_id is not None:
        parent_title = await session.scalar(select(Item.title).where(Item.id == item.parent_id))
    related = await related_refs(session, item.id)
    return CardExtras(
        steps=tuple(steps),
        related=tuple(related),
        parent_title=parent_title,
        heard=await heard(session, item.raw_message_id) if with_heard else None,
    )


async def heard(session: AsyncSession, raw_id: int | None) -> str | None:
    """The transcript of raw message ``raw_id`` when it is a voice note or an audio file (of every such part of its
    album, in order); None for any other message or an empty transcript. A transcript rejected as not speech (an
    audio file of music saved by its caption: ``UNHEARD_KEY`` with its tries) is not shown."""
    if raw_id is None:
        return None
    unheard = RawMessage.raw_json[UNHEARD_KEY].label("unheard")
    cols = (RawMessage.content_type, RawMessage.transcript, RawMessage.media_group_id, RawMessage.chat_id, unheard)
    row = (await session.execute(select(*cols).where(RawMessage.id == raw_id))).one_or_none()
    if row is None:
        return None
    parts: Sequence[Any] = [row] if row.content_type in _VOICE_TYPES else []
    if row.media_group_id:
        stmt = (
            select(RawMessage.transcript, unheard)
            .where(
                RawMessage.media_group_id == row.media_group_id,
                RawMessage.chat_id == row.chat_id,
                RawMessage.content_type.in_(_VOICE_TYPES),
            )
            .order_by(RawMessage.message_id)
        )
        parts = (await session.execute(stmt)).all()
    texts = [p.transcript for p in parts if not (isinstance(p.unheard, dict) and "tries" in p.unheard)]
    return " ".join(t.strip() for t in texts if t and t.strip()) or None


async def _steps(session: AsyncSession, parent_id: int) -> list[Step]:
    """Not deleted steps in order, numbered 1..n, with the due of their current occurrence (not a cancelled one) and
    their fuzzy span."""
    occ = _occ_subquery(open_only=False)
    stmt = (
        select(
            Item.id,
            Item.title,
            Item.status,
            occ.c.due_at,
            occ.c.precision,
            occ.c.occ_status,
            Item.period_kind,
            Item.period_start,
            Item.period_end,
        )
        .outerjoin(occ, occ.c.item_id == Item.id)
        .where(Item.parent_id == parent_id, Item.status != "deleted")
        .order_by(Item.position.asc().nulls_last(), Item.id)
    )
    out: list[Step] = []
    for n, row in enumerate((await session.execute(stmt)).all(), 1):
        step_id, title, status, due_at, precision, occ_status, period_kind, period_start, period_end = row
        timed = due_at is not None and occ_status != "cancelled"
        out.append(
            Step(
                item_id=step_id,
                position=n,
                title=title,
                done=status == "done",
                due_at=due_at if timed else None,
                precision=precision if timed else None,
                period_kind=period_kind,
                period_start=period_start,
                period_end=period_end,
            )
        )
    return out


def _listed() -> ColumnElement[bool]:
    """Top-level lists show a step only when it has a time of its own; the others live in the parent's card."""
    timed = exists().where(Occurrence.item_id == Item.id, Occurrence.status != "cancelled")
    return or_(Item.parent_id.is_(None), timed)


async def _with_family(session: AsyncSession, rows: list[Row]) -> list[Row]:
    """Fill ``parent_title`` of steps and ``steps_done/steps_total`` of parents (two small queries)."""
    parent_ids = {r.parent_id for r in rows if r.parent_id is not None}
    if parent_ids:
        result = await session.execute(select(Item.id, Item.title).where(Item.id.in_(parent_ids)))
        titles = {item_id: title for item_id, title in result.tuples()}
        for row in rows:
            if row.parent_id is not None:
                row.parent_title = titles.get(row.parent_id)
    top_ids = {r.item_id for r in rows if r.parent_id is None}
    if top_ids:
        stmt = (
            select(Item.parent_id, func.count(), func.count().filter(Item.status == "done"))
            .where(Item.parent_id.in_(top_ids), Item.status != "deleted")
            .group_by(Item.parent_id)
        )
        progress = {pid: (int(done), int(total)) for pid, total, done in (await session.execute(stmt)).tuples()}
        for row in rows:
            if row.item_id in progress:
                row.steps_done, row.steps_total = progress[row.item_id]
    return rows


# --- Mini App --------------------------------------------------------------------------------------------------


async def feed(
    session: AsyncSession,
    prefs: Prefs,
    *,
    text: str | None = None,
    kinds: Sequence[str] = (),
    tags: Sequence[str] = (),
    status: str = "any",
    created_from: date | None = None,
    created_to: date | None = None,
    before: tuple[datetime, int] | None = None,
    limit: int = 30,
) -> list[Row]:
    """All items newest first (the Mini App's feed), one page of ``limit`` after ``before`` = (created_at, id) of the
    last row of the previous page. ``text`` matches like ``search`` (steps too); without it a step shows only with a
    time of its own, as in the other lists. ``kinds`` / ``tags``: any of them. ``status``: ``FEED_STATUSES``.
    ``created_from`` / ``created_to``: local dates of saving, inclusive."""
    if status not in FEED_STATUSES:
        raise ValueError(f"unknown status: {status}")
    conds: list[ColumnElement[bool]] = []
    if status == "any":
        conds.append(Item.status != "deleted")
    else:
        conds.append(Item.status == status)
    text = (text or "").strip()
    conds.append(_matches(text, _tsquery(text)) if text else _listed())
    if kinds:
        conds.append(Item.kind.in_(list(kinds)))
    if tags:
        conds.append(Item.tags.overlap(list(tags)))
    tz = prefs.zone
    if created_from is not None:
        conds.append(Item.created_at >= timeresolve.localize(created_from, time(0), tz).astimezone(UTC))
    if created_to is not None:
        conds.append(
            Item.created_at < timeresolve.localize(created_to + timedelta(days=1), time(0), tz).astimezone(UTC)
        )
    if before is not None:
        at, item_id = before
        conds.append(or_(Item.created_at < at, and_(Item.created_at == at, Item.id < item_id)))
    stmt = (
        _item_select(_occ_subquery(open_only=False))
        .where(*conds)
        .order_by(Item.created_at.desc(), Item.id.desc())
        .limit(limit)
    )
    return await _item_rows(session, stmt)


async def rows_by_ids(session: AsyncSession, item_ids: Sequence[int]) -> list[Row]:
    """Rows of these items (not deleted) in the given order, with their current occurrence."""
    ids = list(dict.fromkeys(item_ids))
    if not ids:
        return []
    stmt = _item_select(_occ_subquery(open_only=False)).where(Item.id.in_(ids), Item.status != "deleted")
    by_id = {row.item_id: row for row in await _item_rows(session, stmt)}
    return [by_id[i] for i in ids if i in by_id]


def _previewable() -> ColumnElement[bool]:
    """A picture the Mini App can preview: a Telegram photo, or a JPEG / PNG / WebP / GIF sent as a file."""
    return and_(
        RawMessage.file_id.is_not(None),
        or_(
            RawMessage.content_type == "photo",
            and_(
                RawMessage.content_type == "image_document",
                func.lower(func.coalesce(RawMessage.mime_type, "")).in_(sorted(PREVIEW_MIME)),
            ),
        ),
    )


async def item_images(session: AsyncSession, item: Item) -> list[RawMessage]:
    """Pictures of the message the item was saved from (every part of an album), in message order. A step has none
    of its own (it shares its parent's message)."""
    if item.raw_message_id is None or item.parent_id is not None:
        return []
    raw = await session.get(RawMessage, item.raw_message_id)
    if raw is None:
        return []
    stmt = select(RawMessage).where(_previewable()).options(defer(RawMessage.raw_json))
    if raw.media_group_id:
        stmt = stmt.where(RawMessage.chat_id == raw.chat_id, RawMessage.media_group_id == raw.media_group_id)
    else:
        stmt = stmt.where(RawMessage.id == raw.id)
    return list((await session.scalars(stmt.order_by(RawMessage.message_id, RawMessage.id))).all())


async def image_counts(session: AsyncSession, item_ids: Sequence[int]) -> dict[int, int]:
    """Number of previewable pictures (``item_images``) per item, for list rows; items without any are left out."""
    if not item_ids:
        return {}
    stmt = (
        select(Item.id, RawMessage.chat_id, RawMessage.media_group_id, _previewable())
        .join(RawMessage, RawMessage.id == Item.raw_message_id)
        .where(Item.id.in_(list(item_ids)), Item.parent_id.is_(None))
    )
    rows = (await session.execute(stmt)).tuples().all()
    groups = {(chat, group) for _, chat, group, _ in rows if group}
    per_group: dict[tuple[int, str], int] = {}
    if groups:
        count_stmt = (
            select(RawMessage.chat_id, RawMessage.media_group_id, func.count())
            .where(_previewable(), RawMessage.media_group_id.in_(sorted(g for _, g in groups)))
            .group_by(RawMessage.chat_id, RawMessage.media_group_id)
        )
        per_group = {(c, g): int(n) for c, g, n in (await session.execute(count_stmt)).tuples()}
    counts: dict[int, int] = {}
    for item_id, chat, group, previewable in rows:
        n = per_group.get((chat, group), 0) if group else int(bool(previewable))
        if n:
            counts[item_id] = n
    return counts


# --- search ----------------------------------------------------------------------------------------------------


async def search(session: AsyncSession, text: str, limit: int = 20) -> list[Row]:
    """Full-text search (Russian stemming, English words too) OR title ILIKE OR tag match; best rank first,
    then newest."""
    text = (text or "").strip()
    if not text:
        return []
    tsq = _tsquery(text)
    occ = _occ_subquery(open_only=False)
    stmt = (
        _item_select(occ)
        .where(Item.status != "deleted", _matches(text, tsq))
        .order_by(func.ts_rank(Item.search_tsv, tsq).desc(), Item.created_at.desc(), Item.id.desc())
        .limit(limit)
    )
    return await _item_rows(session, stmt)


async def find_edit_candidates(
    session: AsyncSession, hint: str, prefs: Prefs, now: datetime, limit: int = 5, *, exact: bool = False
) -> list[Row]:
    """Items an edit like "перенеси встречу с Олегом" may refer to: open or touched in the last 30 days, not
    deleted, matching ``hint``; the nearest open occurrence (by |due - now|) first, then rank.

    All words must match (or a tag equals one of them); if nothing does, any word may (stop words are ignored by
    the FTS config). ``exact=True`` (an optional extension of the contract): only items whose own words match —
    every word in the title / details (full text, not the message the item came from: the items of one message
    share it), or the hint as whole words of the title ("чай" is not in "случайно") — no tag-only match, no any-word
    fallback.
    """
    hint = (hint or "").strip()
    if not hint:
        return []
    now = _utc(now)
    tsq = _tsquery(hint)
    if exact:
        return await _edit_candidates(session, tsq, now, limit, match=_own_words_match(hint, tsq))
    rows = await _edit_candidates(session, tsq, now, limit, match=_matches(hint, tsq))
    words = [w for w in _WORD_RE.findall(hint) if w.lower() != "or"]
    if not rows and len(words) > 1:
        any_word = _tsquery(" or ".join(words))
        rows = await _edit_candidates(session, any_word, now, limit, match=_matches(hint, any_word))
    return rows


def _own_words_match(hint: str, tsq: ColumnElement[Any]) -> ColumnElement[bool]:
    """The item's title / details match every word of ``hint`` (full text), or its title has ``hint`` as whole
    words."""
    own = func.to_tsvector(_RU, func.concat_ws(" ", Item.title, Item.details)).op("@@", is_comparison=True)(tsq)
    words = re.findall(r"\w+", hint)  # letters and digits only: nothing to escape in the pattern
    if not words:
        return own
    return or_(own, Item.title.op("~*")(r"\m" + r"\W+".join(words) + r"\M"))


async def _edit_candidates(
    session: AsyncSession, tsq: ColumnElement[Any], now: datetime, limit: int, *, match: ColumnElement[bool]
) -> list[Row]:
    cutoff = now - timedelta(days=EDIT_WINDOW_DAYS)
    occ = _occ_subquery(open_only=True)
    distance = func.abs(func.extract("epoch", occ.c.due_at - literal(now, DateTime(timezone=True))))
    stmt = (
        _item_select(occ)
        .where(
            Item.status != "deleted",
            or_(Item.status == "open", Item.created_at >= cutoff, Item.updated_at >= cutoff),
            match,
        )
        .order_by(
            distance.asc().nulls_last(),
            func.ts_rank(Item.search_tsv, tsq).desc(),
            Item.created_at.desc(),
            Item.id.desc(),
        )
        .limit(limit)
    )
    return await _item_rows(session, stmt)


# --- QuerySpec -------------------------------------------------------------------------------------------------


async def run_query(session: AsyncSession, spec: QuerySpec, prefs: Prefs, now: datetime) -> list[Row]:
    """Execute the LLM's QuerySpec.

    With ``date_field="due"`` and a range, every occurrence due in the range is a row (and the occurrence of an
    item whose week / month / season overlaps the range: "на этой неделе" counts for "что у меня завтра?") and
    ``status`` filters occurrences (open = not done: open or missed). Otherwise rows are items with their current
    occurrence, ``range`` filters ``created_at`` (for ``date_field="created"``) and ``status`` filters the item
    status.
    """
    now = _utc(now)
    window = _range(spec.range, now, prefs)
    conds: list[ColumnElement[bool]] = [Item.status != "deleted"]
    if spec.kinds:
        conds.append(Item.kind.in_(list(spec.kinds)))
    if spec.tags:
        conds.append(Item.tags.overlap(list(spec.tags)))
    text = (spec.text or "").strip()
    tsq = _tsquery(text) if text else None
    if tsq is not None:
        conds.append(_matches(text, tsq))

    if spec.date_field == "due" and window is not None:
        return await _run_due_query(session, spec, conds, window, prefs)

    occ = _occ_subquery(open_only=False)
    stmt = _item_select(occ).where(*conds, _listed())
    if window is not None:  # date_field == "created"
        stmt = stmt.where(Item.created_at >= window[0], Item.created_at < window[1])
    if spec.status != "any":
        stmt = stmt.where(Item.status == spec.status)
    order: list[ColumnElement[Any]] = []
    if tsq is not None:
        order.append(func.ts_rank(Item.search_tsv, tsq).desc())
    if spec.date_field == "due":
        order.append(occ.c.due_at.asc().nulls_last())
    order += [Item.created_at.desc(), Item.id.desc()]
    return await _item_rows(session, stmt.order_by(*order).limit(spec.limit))


async def _run_due_query(
    session: AsyncSession,
    spec: QuerySpec,
    conds: list[ColumnElement[bool]],
    window: tuple[datetime, datetime],
    prefs: Prefs,
) -> list[Row]:
    tz = prefs.zone
    first_day = window[0].astimezone(tz).date()
    last_day = (window[1] - timedelta(microseconds=1)).astimezone(tz).date()
    # The item's span overlaps the range: that counts for the occurrence of the span only (precision "period", due
    # inside it), not for an earlier done one of the same item at some other time.
    due_day = func.date(func.timezone(prefs.tz, Occurrence.due_at))
    overlaps = and_(
        Item.period_kind.in_(_CALENDAR_PERIODS),
        Item.period_start <= last_day,
        Item.period_end >= first_day,
        Occurrence.precision == "period",
        due_day.between(Item.period_start, Item.period_end),
    )
    occ_conds: list[ColumnElement[bool]] = [
        or_(and_(Occurrence.due_at >= window[0], Occurrence.due_at < window[1]), overlaps),
        Occurrence.status != "cancelled",
    ]
    if spec.status == "open":
        occ_conds.append(Occurrence.status.in_(_NOT_DONE))
    elif spec.status == "done":
        occ_conds.append(Occurrence.status == "done")
    return await _occ_rows(session, *conds, *occ_conds, order=(Occurrence.due_at, Occurrence.id), limit=spec.limit)


def _range(spec_range: str | None, now: datetime, prefs: Prefs) -> tuple[datetime, datetime] | None:
    if not spec_range:
        return None
    try:
        return timeresolve.resolve_range(spec_range, now, prefs)
    except ValueError:
        log.debug("ignoring invalid query range %r", spec_range)
        return None


# --- AI spend --------------------------------------------------------------------------------------------------


async def usage_summary(session: AsyncSession, prefs: Prefs, now: datetime) -> UsageSummary:
    """AI calls and spend in the current local calendar month, total and per purpose."""
    now = _utc(now)
    start, end = timeresolve.resolve_range("this_month", now, prefs)
    stmt = (
        select(AiUsage.purpose, func.count(), func.coalesce(func.sum(AiUsage.cost_usd), 0))
        .where(AiUsage.created_at >= start, AiUsage.created_at < end)
        .group_by(AiUsage.purpose)
        .order_by(AiUsage.purpose)
    )
    by_purpose: dict[str, tuple[int, float]] = {}
    calls, spent = 0, Decimal(0)
    for purpose, count, usd in (await session.execute(stmt)).all():
        by_purpose[purpose] = (int(count), float(usd))
        calls += int(count)
        spent += Decimal(usd)
    local = now.astimezone(prefs.zone)
    return UsageSummary(
        month=f"{_MONTHS_NOM[local.month - 1]} {local.year}",
        spent_usd=float(spent),
        limit_usd=float(prefs.budget_usd),
        calls=calls,
        by_purpose=by_purpose,
    )


# --- LLM view --------------------------------------------------------------------------------------------------


def rows_for_llm(rows: Sequence[Row], prefs: Prefs) -> list[dict[str, Any]]:
    """Compact dicts for answer_query: local "YYYY-MM-DD HH:MM" times, empty fields dropped, item_id kept."""
    tz = prefs.zone
    out: list[dict[str, Any]] = []
    for row in rows:
        entry: dict[str, Any] = {"item_id": row.item_id, "kind": row.kind, "title": row.title, "status": row.status}
        if row.tags:
            entry["tags"] = list(row.tags)
        if row.due_at is not None:
            entry["due"] = row.due_at.astimezone(tz).strftime(_LOCAL_FMT)
        if row.occ_status:
            entry["occ_status"] = row.occ_status
        if row.rrule:
            entry["repeat"] = recurrence.describe_rrule_ru(row.rrule)
        if row.someday:
            entry["someday"] = True
        elif row.period_start is not None and row.period_end is not None:
            entry["period"] = f"{row.period_start.isoformat()}..{row.period_end.isoformat()}"
        if row.parent_title:
            entry["step_of"] = row.parent_title
        if row.steps_total:
            entry["steps_done"] = f"{row.steps_done}/{row.steps_total}"
        entry["created"] = row.created_at.astimezone(tz).strftime(_LOCAL_FMT)
        if row.snippet:
            entry["snippet"] = row.snippet
        if row.urls:
            entry["urls"] = list(row.urls)
        out.append(entry)
    return out


# --- internals -------------------------------------------------------------------------------------------------


def _occ_subquery(*, open_only: bool) -> Subquery:
    """One occurrence per item (DISTINCT ON): the earliest open one, or with ``open_only=False`` the current
    one (earliest open, else the latest)."""
    stmt = select(
        Occurrence.item_id.label("item_id"),
        Occurrence.id.label("occ_id"),
        Occurrence.due_at.label("due_at"),
        Occurrence.status.label("occ_status"),
        Occurrence.precision.label("precision"),
    ).distinct(Occurrence.item_id)
    if open_only:
        stmt = stmt.where(Occurrence.status == "open").order_by(Occurrence.item_id, Occurrence.due_at, Occurrence.id)
    else:
        stmt = stmt.order_by(Occurrence.item_id, *current_occurrence_order())
    return stmt.subquery("cur")


def _item_select(occ: Subquery) -> Select[Any]:
    return (
        select(Item, occ.c.occ_id, occ.c.due_at, occ.c.occ_status)
        .outerjoin(occ, occ.c.item_id == Item.id)
        .options(defer(Item.search_tsv))
    )


async def _item_rows(session: AsyncSession, stmt: Select[Any]) -> list[Row]:
    result = await session.execute(stmt)
    rows = [
        _row(item, occurrence_id=occ_id, due_at=due_at, occ_status=occ_status)
        for item, occ_id, due_at, occ_status in result.all()
    ]
    return await _with_family(session, rows)


async def _occ_rows(
    session: AsyncSession,
    *conds: ColumnElement[bool],
    order: Sequence[Any],
    limit: int | None = None,
) -> list[Row]:
    """One row per occurrence matching ``conds`` (items not deleted)."""
    stmt = (
        select(Item, Occurrence)
        .join(Occurrence, Occurrence.item_id == Item.id)
        .where(Item.status != "deleted", *conds)
        .options(defer(Item.search_tsv))
        .order_by(*order)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    rows = [_row(item, occurrence_id=occ.id, due_at=occ.due_at, occ_status=occ.status) for item, occ in result.all()]
    return await _with_family(session, rows)


def _row(item: Item, *, occurrence_id: int | None, due_at: datetime | None, occ_status: str | None) -> Row:
    return Row(
        item_id=item.id,
        kind=item.kind,
        title=item.title,
        tags=list(item.tags or []),
        status=item.status,
        due_at=due_at,
        occurrence_id=occurrence_id,
        occ_status=occ_status,
        rrule=item.rrule if item.rec_active else None,
        created_at=item.created_at,
        snippet=_snippet(item),
        urls=list(item.urls or []),
        period_kind=item.period_kind,
        period_start=item.period_start,
        period_end=item.period_end,
        someday=bool(item.someday),
        parent_id=item.parent_id,
    )


def _snippet(item: Item) -> str | None:
    """Details, else the source text, whitespace-collapsed and cut to 200 chars; None if it only repeats the title."""
    text = " ".join((item.details or item.source_text or "").split())
    if not text or text == " ".join(item.title.split()):
        return None
    return text if len(text) <= SNIPPET_LEN else text[: SNIPPET_LEN - 1].rstrip() + "…"


def _tsquery(text: str) -> ColumnElement[Any]:
    return func.websearch_to_tsquery(_RU, text)


def _matches(text: str, tsq: ColumnElement[Any], *, tags: bool = True) -> ColumnElement[bool]:
    """FTS match OR title contains the phrase OR (``tags``) a tag equals one of the words."""
    conds: list[ColumnElement[bool]] = [
        Item.search_tsv.op("@@", is_comparison=True)(tsq),
        Item.title.ilike(f"%{_escape_like(text)}%", escape="\\"),
    ]
    words = list(dict.fromkeys(w.lower() for w in re.findall(r"#?(\w+)", text))) if tags else []
    if words:
        conds.append(Item.tags.overlap(words))
    return or_(*conds)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
