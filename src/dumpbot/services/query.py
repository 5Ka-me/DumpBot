"""Read side: agenda, task lists, search, QuerySpec execution, AI spend summary (architecture.md §4.11).

Nothing here writes. Deleted items are always excluded. Search uses the persisted ``items.search_tsv``
(``to_tsvector('russian', …)``: Cyrillic is stemmed by the Russian stemmer, ASCII words by the English one),
``websearch_to_tsquery('russian', q)``, plus an ILIKE fallback on the title and a tag match.

Fuzzy time (docs/features/iteration-2.md §A): an open item whose calendar period (week / month / season) contains
the day is listed in ``Agenda.period_items`` instead of open / overdue; "когда-нибудь" items are only in
``someday_items``; a due-date query also finds items whose period overlaps its range.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import ColumnElement, DateTime, Select, and_, func, literal, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlalchemy.sql.selectable import Subquery

from dumpbot.ai.schemas import QuerySpec
from dumpbot.db.models import AiUsage, Item, Occurrence
from dumpbot.domain import recurrence, timeresolve
from dumpbot.domain.kinds import ACTIONABLE, CALENDAR_PERIODS
from dumpbot.domain.prefs import Prefs
from dumpbot.dto import Agenda, Row, UsageSummary
from dumpbot.services.items import current_occurrence_order

__all__ = [
    "Agenda",
    "Row",
    "UsageSummary",
    "agenda",
    "find_edit_candidates",
    "items_of_raw",
    "open_tasks",
    "recent",
    "recurring",
    "rows_for_llm",
    "run_query",
    "search",
    "someday_items",
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


# --- agenda / lists --------------------------------------------------------------------------------------------


async def agenda(session: AsyncSession, prefs: Prefs, now: datetime, *, day_offset: int = 0) -> Agenda:
    """Occurrences of one local day: not done (open, or missed instances of a series) by time; overdue open
    ones from the previous 30 days (only for today); done that day (by done_at); open items whose calendar
    period contains the day (they are not in open / overdue while the period runs: "на этой неделе" is due on
    Friday 19:00 but still fine on Saturday)."""
    tz = prefs.zone
    day = _utc(now).astimezone(tz).date() + timedelta(days=day_offset)
    start = timeresolve.localize(day, time(0), tz).astimezone(UTC)
    end = timeresolve.localize(day + timedelta(days=1), time(0), tz).astimezone(UTC)
    not_running = _no_running_period(day)

    open_rows = await _occ_rows(
        session,
        Occurrence.status.in_(_NOT_DONE),
        Occurrence.due_at >= start,
        Occurrence.due_at < end,
        not_running,
        order=(Occurrence.due_at, Occurrence.id),
    )
    overdue: list[Row] = []
    if day_offset == 0:
        overdue = await _occ_rows(
            session,
            Occurrence.status == "open",
            Occurrence.due_at < start,
            Occurrence.due_at >= start - timedelta(days=OVERDUE_DAYS),
            not_running,
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
        .where(Item.kind.in_(_ACTIONABLE_KINDS), Item.status == "open", Item.someday.is_(False))
        .order_by(occ.c.due_at.asc().nulls_last(), Item.created_at.desc(), Item.id.desc())
        .limit(limit)
    )
    return await _item_rows(session, stmt)


async def someday_items(session: AsyncSession, limit: int = 50) -> list[Row]:
    """Open "когда-нибудь" items of any kind (``/someday``), newest first."""
    occ = _occ_subquery(open_only=True)
    stmt = (
        _item_select(occ)
        .where(Item.someday.is_(True), Item.status == "open")
        .order_by(Item.created_at.desc(), Item.id.desc())
        .limit(limit)
    )
    return await _item_rows(session, stmt)


async def recent(session: AsyncSession, limit: int = 15) -> list[Row]:
    """Latest saved items (by created_at), not deleted."""
    occ = _occ_subquery(open_only=False)
    stmt = (
        _item_select(occ).where(Item.status != "deleted").order_by(Item.created_at.desc(), Item.id.desc()).limit(limit)
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


async def items_of_raw(session: AsyncSession, raw_ids: Sequence[int]) -> list[Row]:
    """Not deleted items saved from these raw messages (a message or the parts of an album), oldest first, with
    their current occurrence."""
    if not raw_ids:
        return []
    occ = _occ_subquery(open_only=False)
    stmt = _item_select(occ).where(Item.raw_message_id.in_(list(raw_ids)), Item.status != "deleted").order_by(Item.id)
    return await _item_rows(session, stmt)


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
    the FTS config). ``exact=True`` (an optional extension of the contract): only items that match every word
    (full text) or whose title contains the hint — no tag-only match, no any-word fallback.
    """
    hint = (hint or "").strip()
    if not hint:
        return []
    now = _utc(now)
    rows = await _edit_candidates(session, hint, _tsquery(hint), now, limit, tags=not exact)
    words = [w for w in _WORD_RE.findall(hint) if w.lower() != "or"]
    if not rows and len(words) > 1 and not exact:
        rows = await _edit_candidates(session, hint, _tsquery(" or ".join(words)), now, limit)
    return rows


async def _edit_candidates(
    session: AsyncSession, hint: str, tsq: ColumnElement[Any], now: datetime, limit: int, *, tags: bool = True
) -> list[Row]:
    cutoff = now - timedelta(days=EDIT_WINDOW_DAYS)
    occ = _occ_subquery(open_only=True)
    distance = func.abs(func.extract("epoch", occ.c.due_at - literal(now, DateTime(timezone=True))))
    stmt = (
        _item_select(occ)
        .where(
            Item.status != "deleted",
            or_(Item.status == "open", Item.created_at >= cutoff, Item.updated_at >= cutoff),
            _matches(hint, tsq, tags=tags),
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
    stmt = _item_select(occ).where(*conds)
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
    overlaps = and_(
        Item.period_kind.in_(_CALENDAR_PERIODS), Item.period_start <= last_day, Item.period_end >= first_day
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
    return [
        _row(item, occurrence_id=occ_id, due_at=due_at, occ_status=occ_status)
        for item, occ_id, due_at, occ_status in result.all()
    ]


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
    return [_row(item, occurrence_id=occ.id, due_at=occ.due_at, occ_status=occ.status) for item, occ in result.all()]


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
