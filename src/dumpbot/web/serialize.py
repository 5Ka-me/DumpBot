"""Service objects as the JSON of the Mini App API (field by field: docs/features/miniapp-api.md, TypeScript:
webapp/src/api/types.ts).

Times are ISO 8601 in UTC with "Z" (``iso``), local dates "YYYY-MM-DD", local wall times "YYYY-MM-DDTHH:MM"
(``*_local``). Human texts ("сегодня 13:00", "на этой неделе", "каждый пн") come from ``bot.views`` so the app reads
like the chat.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Any, Final

from dumpbot.bot import views
from dumpbot.db.models import RawMessage
from dumpbot.domain.kinds import CALENDAR_PERIODS
from dumpbot.services import items, query
from dumpbot.services.ingest import APP_SOURCE
from dumpbot.workers import jobs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from dumpbot.db.models import Item, Occurrence, Reminder
    from dumpbot.domain.prefs import Prefs
    from dumpbot.dto import Agenda, RelatedRef, Row, Step

LOCAL_FMT: Final = "%Y-%m-%dT%H:%M"
_PERIOD_KINDS: Final = (*sorted(CALENDAR_PERIODS), "soon")


def iso(dt: datetime | None) -> str | None:
    """Aware datetime -> "2026-10-06T08:00:00Z" (naive values are taken as UTC)."""
    if dt is None:
        return None
    dt = dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return dt.isoformat().replace("+00:00", "Z")


def day(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def hhmm(t: time) -> str:
    return t.strftime("%H:%M")


def local(dt: datetime, prefs: Prefs) -> str:
    """Wall time in the owner's timezone: "2026-10-06T13:00"."""
    return dt.astimezone(prefs.zone).strftime(LOCAL_FMT)


def period(kind: str | None, start: date | None, end: date | None, today: date) -> dict[str, Any] | None:
    """A fuzzy span (week / month / season / "на днях") with its words; None for none and for "когда-нибудь"."""
    if kind not in _PERIOD_KINDS or start is None or end is None:
        return None
    return {"kind": kind, "start": day(start), "end": day(end), "label": views.period_label(kind, start, end, today)}


def _today(prefs: Prefs, now: datetime) -> date:
    return now.astimezone(prefs.zone).date()


# --- list rows -----------------------------------------------------------------------------------------------------


def row(r: Row, prefs: Prefs, now: datetime, *, images: int = 0) -> dict[str, Any]:
    return {
        "id": r.item_id,
        "kind": r.kind,
        "title": r.title,
        "tags": list(r.tags),
        "status": r.status,
        "due_at": iso(r.due_at),
        "occurrence_id": r.occurrence_id,
        "occ_status": r.occ_status,
        "rrule": r.rrule,
        "repeat_text": views.repeat_text(r.rrule),
        "created_at": iso(r.created_at),
        "snippet": r.snippet,
        "urls": list(r.urls),
        "period": period(r.period_kind, r.period_start, r.period_end, _today(prefs, now)),
        "someday": r.someday,
        "parent_id": r.parent_id,
        "parent_title": r.parent_title,
        "steps_done": r.steps_done,
        "steps_total": r.steps_total,
        "when_text": views.row_when(r, prefs, now),
        "images": images,
    }


async def rows(session: AsyncSession, found: Sequence[Row], prefs: Prefs, now: datetime) -> list[dict[str, Any]]:
    """Rows with the number of their picture previews."""
    counts = await query.image_counts(session, [r.item_id for r in found])
    return [row(r, prefs, now, images=counts.get(r.item_id, 0)) for r in found]


async def agenda(session: AsyncSession, a: Agenda, prefs: Prefs, now: datetime) -> dict[str, Any]:
    return {
        "day": day(a.day),
        "open": await rows(session, a.open, prefs, now),
        "overdue": await rows(session, a.overdue, prefs, now),
        "done": await rows(session, a.done, prefs, now),
        "period_items": await rows(session, a.period_items, prefs, now),
    }


# --- an item -------------------------------------------------------------------------------------------------------


def occurrence(occ: Occurrence | None, prefs: Prefs) -> dict[str, Any] | None:
    if occ is None:
        return None
    return {
        "id": occ.id,
        "due_at": iso(occ.due_at),
        "due_local": local(occ.due_at, prefs),
        "precision": occ.precision,
        "status": occ.status,
        "done_at": iso(occ.done_at),
    }


def reminder(r: Reminder) -> dict[str, Any]:
    return {"id": r.id, "fire_at": iso(r.fire_at), "lead_minutes": r.lead_minutes, "auto": r.auto, "status": r.status}


def step(s: Step, prefs: Prefs, now: datetime) -> dict[str, Any]:
    return {
        "id": s.item_id,
        "position": s.position,
        "title": s.title,
        "done": s.done,
        "due_at": iso(s.due_at),
        "precision": s.precision,
        "when_text": views.step_when(s, prefs, now),
    }


def related(r: RelatedRef) -> dict[str, Any]:
    return {"id": r.item_id, "kind": r.kind, "title": r.title, "created_at": iso(r.created_at)}


def _forward(item: Item) -> dict[str, Any] | None:
    """The forwarded post's origin (``Item.source["forward_origin"]``): who, a link to it, when."""
    origin = item.source.get("forward_origin") if isinstance(item.source, dict) else None
    if not isinstance(origin, dict):
        return None
    username = origin.get("username")
    name = origin.get("title") or origin.get("sender_name") or (f"@{username}" if username else None)
    url = origin.get("url")
    return {
        "type": str(origin.get("type") or "unknown"),
        "name": name if isinstance(name, str) else None,
        "url": url if isinstance(url, str) and url.startswith(("http://", "https://")) else None,
        "date": origin.get("date") if isinstance(origin.get("date"), str) else None,
    }


async def _source(session: AsyncSession, item: Item) -> dict[str, Any]:
    raw = await session.get(RawMessage, item.raw_message_id) if item.raw_message_id is not None else None
    return {
        "kind": item.source_kind,
        "text": item.source_text,
        "forward": _forward(item),
        "raw_id": item.raw_message_id,
        "from_app": raw is not None and raw.source == APP_SOURCE,
        "message_date": iso(raw.tg_date) if raw is not None else None,
    }


def _link_titles(item: Item) -> dict[str, str]:
    titles = item.source.get("link_titles") if isinstance(item.source, dict) else None
    if not isinstance(titles, dict):
        return {}
    return {str(url): str(title) for url, title in titles.items() if isinstance(title, str)}


async def item_detail(
    session: AsyncSession, item: Item, prefs: Prefs, now: datetime, *, warnings: Sequence[str] = ()
) -> dict[str, Any]:
    """Everything the card shows and more: fields, current occurrence with its pending reminders, checklist, related
    items, parent, source, picture previews, whether it can be split now."""
    occ = await items.current_occurrence(session, item.id)
    reminders = await items.pending_reminders(session, occ.id) if occ is not None else []
    extras = await query.card_extras(session, item)
    pictures = await query.item_images(session, item)
    parent = await items.get_item(session, item.parent_id) if item.parent_id is not None else None
    steps = list(extras.steps)
    can_split = item.kind in views.SPLIT_KINDS and items.can_split(item) and not steps
    return {
        "id": item.id,
        "kind": item.kind,
        "title": item.title,
        "details": item.details,
        "tags": list(item.tags or []),
        "status": item.status,
        "created_at": iso(item.created_at),
        "updated_at": iso(item.updated_at),
        "done_at": iso(item.done_at),
        "deleted_at": iso(item.deleted_at),
        "urls": list(item.urls or []),
        "link_titles": _link_titles(item),
        "lead_minutes": list(item.lead_minutes) if item.lead_minutes is not None else None,
        "reminders_off": item.lead_minutes == [],
        "rrule": item.rrule,
        "rec_active": bool(item.rec_active),
        "repeat_text": views.repeat_text(item.rrule),
        "period": period(item.period_kind, item.period_start, item.period_end, _today(prefs, now)),
        "someday": bool(item.someday),
        "role": str((item.when_spec or {}).get("role") or "event"),
        "needs_confirm": bool(item.needs_confirm),
        "parent": {"id": parent.id, "title": parent.title} if parent is not None else None,
        "position": item.position,
        "occurrence": occurrence(None if item.someday else occ, prefs),
        "reminders": [reminder(r) for r in reminders] if not item.someday else [],
        "when_text": views.time_text(item, occ, reminders, prefs, now),
        "steps": [step(s, prefs, now) for s in steps],
        "steps_done": sum(s.done for s in steps),
        "steps_total": len(steps),
        "related": [related(r) for r in extras.related],
        "source": await _source(session, item),
        "images": [
            {
                "n": n,
                "url": f"/api/files/{item.id}/{n}",
                "mime_type": "image/jpeg" if p.content_type == "photo" else (p.mime_type or "").lower(),
                "width": p.width,
                "height": p.height,
            }
            for n, p in enumerate(pictures)
        ],
        "can_split": can_split,
        "split_pending": await jobs.pending_job(session, "split", item.id) is not None,
        "warnings": list(dict.fromkeys(w for w in warnings if w)),
    }
