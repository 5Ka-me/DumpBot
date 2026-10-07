"""notification time: no default lead

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07 17:00:00+00:00

Data only (docs/requirements.md, "Изменение 2026-10-07: только время уведомления"): an item's time is its
notification time — no "за 5 минут" by default, and an explicit lead ("напомни за 30 минут до встречи в 15") makes
the occurrence due at the notification itself; with several ("за день и за час") at the last one (the smallest
lead), the earlier ones notify before it. The new code reads the time the owner stated as due + the smallest lead
(``items._stated_at``), so every open occurrence still planned the old way (due = the stated time) is converted:
- an item without explicit leads (``items.lead_minutes`` NULL or empty): its default-lead reminders (pending, not
  automatic, ``lead_minutes`` > 0 — 12:55 for 13:00) move to the occurrence's time with ``lead_minutes`` 0; one
  pending reminder per (occurrence, fire_at) is kept, the duplicates are cancelled. The automatic ones before the
  time (a deadline's evening before, a span's first morning) stay;
- a one-off item with explicit leads (not a deadline; a precision that takes them: exact / slot / relative, or date
  / fallback with whole days): with lead moments still pending, the occurrence becomes due at the last of them (a
  snooze, ``lead_minutes`` 0, is not one) unless another occurrence of the item is due right then, and the item keeps
  the leads of those moments; with none pending ("за час до встречи в 18", 17:00 already sent) it stays at the stated
  time and loses its leads — what ``timeresolve.notify_due`` gives when no lead moment is ahead. A one-off with
  nothing open (done, missed) loses its leads too;
- a series with explicit leads keeps them: every open instance planned the old way is due its stated time minus the
  smallest lead (``timeresolve.lead_shift``), whatever was sent already;
- a moved occurrence keeps the stated time in the title, as new items do: "Встреча (15:00)", "Созвон (пт 9 окт
  15:00)" on another day, "(пт 9 окт)" for a date, a series the time of day only.
Planned the old way = a pending reminder with ``lead_minutes`` > 0, or a sent / missed one whose lead puts it right
before the due; a converted one-off is due at one of its reminders. So a second run changes nothing. A deadline keeps
its own time, so its rows stay as they are (only a default lead moves to the time). The removed preference
``lead_minutes`` is dropped from ``user_settings.prefs`` too (``Prefs`` ignores it anyway).

Downgrade is a no-op: the old fire times and leads are not kept, and the old code reads the moved rows correctly
(a reminder at the occurrence's time; it re-plans the default lead only for new or re-planned occurrences).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Precisions that take any explicit lead, and those that take whole days only (timeresolve.effective_leads).
TIMED_PRECISIONS = ("exact", "slot", "relative")
DATE_PRECISIONS = ("date", "fallback")
DEFAULT_TZ = "Europe/Warsaw"
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
WEEKDAY_STEMS = ("понедельник", "вторник", r"сред[аеуы]\b", "четверг", "пятниц", "суббот", "воскресен")
MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
LABEL_RE = re.compile(
    rf"\s*\(((?:(?:{'|'.join(WEEKDAYS)})\s+)?(?:\d{{1,2}}\s+(?:{'|'.join(MONTHS)})(?:\s+\d{{4}})?)?"
    r"\s*(?:\d{1,2}:\d{2})?)\)\s*$"
)

OCCURRENCES = sa.text(
    """
    SELECT o.id, o.item_id, o.due_at, o.precision, i.lead_minutes, i.title, i.rec_tz,
           (i.rrule IS NOT NULL AND i.rec_active) AS series,
           coalesce(i.when_spec ->> 'role', 'event') AS role
    FROM occurrences o
    JOIN items i ON i.id = o.item_id
    WHERE o.status = 'open'
      AND (
          coalesce(cardinality(i.lead_minutes), 0) > 0
          OR EXISTS (
              SELECT 1 FROM reminders r
              WHERE r.occurrence_id = o.id AND r.status = 'pending' AND r.lead_minutes > 0
          )
      )
    ORDER BY o.item_id, o.due_at, o.id
    """
)
REMINDERS = sa.text(
    "SELECT id, fire_at, lead_minutes, auto, status FROM reminders "
    "WHERE occurrence_id = :occ AND status <> 'cancelled' ORDER BY fire_at, id"
)
TIMEZONE = sa.text("SELECT prefs ->> 'tz' FROM user_settings ORDER BY owner_id LIMIT 1")
MOVE_REMINDER = sa.text("UPDATE reminders SET fire_at = :fire, lead_minutes = 0 WHERE id = :id")
CANCEL_REMINDER = sa.text("UPDATE reminders SET status = 'cancelled' WHERE id = :id")
ZERO_LEADS = sa.text("UPDATE reminders SET lead_minutes = 0 WHERE occurrence_id = :occ AND status = 'pending'")
DUE_TAKEN = sa.text("SELECT 1 FROM occurrences WHERE item_id = :item AND due_at = :due AND id <> :occ")
MOVE_OCCURRENCE = sa.text("UPDATE occurrences SET due_at = :due WHERE id = :occ")
ITEM_LEADS = sa.text("UPDATE items SET lead_minutes = :leads WHERE id = :item")
# A one-off with nothing open (done, missed): its leads would shift the time an "↩️ Не сделано" plans from.
CLOSED_ONE_OFF_LEADS = sa.text(
    """
    UPDATE items i SET lead_minutes = NULL
    WHERE coalesce(cardinality(i.lead_minutes), 0) > 0
      AND NOT (i.rrule IS NOT NULL AND i.rec_active)
      AND coalesce(i.when_spec ->> 'role', 'event') <> 'deadline'
      AND NOT EXISTS (SELECT 1 FROM occurrences o WHERE o.item_id = i.id AND o.status = 'open')
    """
)
ITEM_TITLE = sa.text("UPDATE items SET title = :title WHERE id = :item")


def upgrade() -> None:
    bind = op.get_bind()
    zone = _zone(bind.execute(TIMEZONE).scalar())
    bind.execute(sa.text("UPDATE user_settings SET prefs = prefs - 'lead_minutes' WHERE prefs ? 'lead_minutes'"))
    bind.execute(CLOSED_ONE_OFF_LEADS)
    titled: set[int] = set()
    # Earliest first per item: an occurrence moving earlier frees its time for the next one of a series.
    for occ in bind.execute(OCCURRENCES).all():
        reminders = bind.execute(REMINDERS, {"occ": occ.id}).all()
        pending = [r for r in reminders if r.status == "pending"]
        if not occ.lead_minutes:
            _move_default_leads(bind, occ, pending)
            continue
        leads = _effective(occ.precision, occ.lead_minutes)
        if occ.role == "deadline" or not leads:
            continue
        tz = _zone(occ.rec_tz) if occ.series and occ.rec_tz else zone
        if occ.series:
            moved = _series_instance(bind, occ, reminders, leads, tz)
        else:
            moved = _one_off(bind, occ, reminders, leads, tz)
        if moved is not None and occ.item_id not in titled:
            titled.add(occ.item_id)
            title = _stated_label(occ.title, occ.due_at, moved, occ.precision, tz, series=occ.series)
            if title != occ.title:
                bind.execute(ITEM_TITLE, {"title": title, "item": occ.item_id})


def _move_default_leads(bind: sa.Connection, occ: sa.Row, pending: Sequence[sa.Row]) -> None:
    """12:55 for 13:00 -> 13:00; at most one pending reminder at that time."""
    taken = any(r.fire_at == occ.due_at for r in pending if r.auto or r.lead_minutes <= 0)
    for r in pending:
        if r.auto or r.lead_minutes <= 0:
            continue
        if taken:
            bind.execute(CANCEL_REMINDER, {"id": r.id})
        else:
            bind.execute(MOVE_REMINDER, {"id": r.id, "fire": occ.due_at})
            taken = True


def _one_off(
    bind: sa.Connection, occ: sa.Row, reminders: Sequence[sa.Row], leads: list[int], tz: ZoneInfo
) -> datetime | None:
    """ "За день и за час до встречи в 15": due at the last lead moment still pending, the pending ones' leads kept.
    Nothing pending (or the moment taken): the stated time without leads. Returns the new due when it moved."""
    moments = [r for r in reminders if r.status == "pending" and r.lead_minutes in leads]
    if moments:
        last = max(moments, key=lambda r: (r.fire_at, r.id)).fire_at
        bind.execute(ZERO_LEADS, {"occ": occ.id})
        if last == occ.due_at or not _free(bind, occ, last):
            bind.execute(ITEM_LEADS, {"leads": None, "item": occ.item_id})
            return None
        bind.execute(MOVE_OCCURRENCE, {"due": last, "occ": occ.id})
        kept = sorted({r.lead_minutes for r in moments}, reverse=True)
        bind.execute(ITEM_LEADS, {"leads": kept, "item": occ.item_id})
        return last
    at_due = any(r.fire_at == occ.due_at for r in reminders)
    if not at_due or _planned_before(occ, reminders, tz):  # every lead moment has passed
        bind.execute(ITEM_LEADS, {"leads": None, "item": occ.item_id})
    return None


def _series_instance(
    bind: sa.Connection, occ: sa.Row, reminders: Sequence[sa.Row], leads: list[int], tz: ZoneInfo
) -> datetime | None:
    """An instance of a series planned the old way: due its stated time minus the smallest lead (unless another
    instance is due right then). Returns the new due when it moved."""
    if not any(r.status == "pending" and r.lead_minutes > 0 for r in reminders) and not _planned_before(
        occ, reminders, tz
    ):
        return None  # converted already, or nothing tells
    bind.execute(ZERO_LEADS, {"occ": occ.id})
    due = _back(occ.due_at, leads[-1], tz)
    if not _free(bind, occ, due):
        return None
    bind.execute(MOVE_OCCURRENCE, {"due": due, "occ": occ.id})
    return due


def _planned_before(occ: sa.Row, reminders: Sequence[sa.Row], tz: ZoneInfo) -> bool:
    """A reminder already sent (or missed) that the old plan put its lead before the due."""
    return any(
        r.status != "pending" and r.lead_minutes > 0 and _back(occ.due_at, r.lead_minutes, tz) == r.fire_at
        for r in reminders
    )


def _free(bind: sa.Connection, occ: sa.Row, due: datetime) -> bool:
    return bind.execute(DUE_TAKEN, {"item": occ.item_id, "due": due, "occ": occ.id}).first() is None


def _effective(precision: str, lead: Sequence[int]) -> list[int]:
    """``timeresolve.effective_leads``: positive, unique, largest first; whole days only for a date."""
    values = {int(m) for m in lead or () if int(m) > 0}
    if precision in DATE_PRECISIONS:
        values = {m for m in values if m % 1440 == 0}
    elif precision not in TIMED_PRECISIONS:
        values = set()
    return sorted(values, reverse=True)


def _back(t: datetime, minutes: int, tz: ZoneInfo) -> datetime:
    """``timeresolve.shift_back``: whole days on the wall clock, other leads in UTC minutes."""
    if minutes % 1440 == 0:
        local = t.astimezone(tz)
        wall = datetime.combine(local.date() - timedelta(days=minutes // 1440), local.time())
        return wall.replace(tzinfo=tz).astimezone(UTC)
    return t - timedelta(minutes=minutes)


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def _stated_label(title: str, stated: datetime, due: datetime, precision: str, tz: ZoneInfo, *, series: bool) -> str:
    """``items._stated_label``: the title names the stated time unless it has it — the hour, and on another day than
    the due the day too; a trailing "(10:30)" becomes "(пт 9 окт 10:30)"."""
    local = stated.astimezone(tz)
    timed = precision not in DATE_PRECISIONS
    need_day = not timed or (not series and local.date() != due.astimezone(tz).date())
    has_time = not timed or re.search(rf"(?<![\d:])0?{local.hour}(?!\d)", title) is not None
    has_day = not need_day or _names_day(title, local)
    if has_time and has_day:
        return title
    day = f"{WEEKDAYS[local.weekday()]} {local.day} {MONTHS[local.month - 1]}"
    clock = f"{local.hour:02d}:{local.minute:02d}"
    label = f"{day} {clock}" if timed and need_day else (clock if timed else day)
    trailing = LABEL_RE.search(title)
    if trailing is not None and trailing.group(1).strip():
        return f"{title[: trailing.start()]} ({label})"
    return f"{title} ({day if has_time else label})"


def _names_day(title: str, local: datetime) -> bool:
    low = title.lower()
    weekday = local.weekday()
    return (
        re.search(rf"(?<![\d:]){local.day}(?![\d:])", low) is not None
        or re.search(rf"(?<![а-яё]){WEEKDAYS[weekday]}(?![а-яё])", low) is not None
        or re.search(WEEKDAY_STEMS[weekday], low) is not None
    )


def downgrade() -> None:
    """No-op: the old fire times are not kept (see the module docstring)."""
