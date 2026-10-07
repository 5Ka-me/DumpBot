"""notification time: no default lead

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07 17:00:00+00:00

Data only (docs/requirements.md, "Изменение 2026-10-07: только время уведомления"): an item's time is its
notification time — no "за 5 минут" by default, and an explicit lead ("напомни за 30 минут до встречи в 15") makes
the occurrence due at the notification itself. Open occurrences with pending reminders planned before their time:
- an item without explicit leads (``items.lead_minutes`` NULL): its default-lead reminders (pending, not automatic,
  ``lead_minutes`` > 0 — 12:55 for 13:00) move to the occurrence's time with ``lead_minutes`` 0; one pending
  reminder per (occurrence, fire_at) is kept, the duplicates are cancelled. The automatic ones before the time (a
  deadline's evening before, a span's first morning) stay;
- an item with explicit leads (not a deadline, a precision that takes them: exact / slot / relative, or date /
  fallback with "за день"): the pending reminders keep their ``fire_at`` (they are the notification times now), the
  occurrence becomes due at the earliest of them (unless another occurrence of the item is due right then) and they
  get ``lead_minutes`` 0; a one-off item keeps as ``lead_minutes`` the leads of those pending reminders (the first is
  its due, the others notify later). A deadline keeps its own time, so its rows stay as they are.
The removed preference ``lead_minutes`` is dropped from ``user_settings.prefs`` too (``Prefs`` ignores it anyway).

Downgrade is a no-op: the old fire times and leads are not kept, and the old code reads the moved rows correctly
(a reminder at the occurrence's time; it re-plans the default lead only for new or re-planned occurrences).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Precisions whose occurrences took explicit leads in the old plan (date / fallback: whole days only).
LEAD_PRECISIONS = ("exact", "slot", "relative", "date", "fallback")

OCCURRENCES = sa.text(
    """
    SELECT o.id, o.item_id, o.due_at, o.precision, i.lead_minutes,
           (i.rrule IS NOT NULL AND i.rec_active) AS series,
           coalesce(i.when_spec ->> 'role', 'event') AS role
    FROM occurrences o
    JOIN items i ON i.id = o.item_id
    WHERE o.status = 'open'
      AND EXISTS (
          SELECT 1 FROM reminders r
          WHERE r.occurrence_id = o.id AND r.status = 'pending' AND r.lead_minutes > 0
      )
    ORDER BY o.item_id, o.due_at, o.id
    """
)
PENDING = sa.text(
    "SELECT id, fire_at, lead_minutes, auto FROM reminders "
    "WHERE occurrence_id = :occ AND status = 'pending' ORDER BY fire_at, id"
)
MOVE_REMINDER = sa.text("UPDATE reminders SET fire_at = :fire, lead_minutes = 0 WHERE id = :id")
CANCEL_REMINDER = sa.text("UPDATE reminders SET status = 'cancelled' WHERE id = :id")
ZERO_LEADS = sa.text("UPDATE reminders SET lead_minutes = 0 WHERE occurrence_id = :occ AND status = 'pending'")
DUE_TAKEN = sa.text("SELECT 1 FROM occurrences WHERE item_id = :item AND due_at = :due AND id <> :occ")
MOVE_OCCURRENCE = sa.text("UPDATE occurrences SET due_at = :due WHERE id = :occ")
ITEM_LEADS = sa.text("UPDATE items SET lead_minutes = :leads WHERE id = :item")


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE user_settings SET prefs = prefs - 'lead_minutes' WHERE prefs ? 'lead_minutes'"))
    # Earliest first per item: an occurrence moving earlier frees its time for the next one of a series.
    for occ in bind.execute(OCCURRENCES).all():
        pending = bind.execute(PENDING, {"occ": occ.id}).all()
        if not occ.lead_minutes:
            _move_default_leads(bind, occ, pending)
        elif occ.role != "deadline" and occ.precision in LEAD_PRECISIONS:
            _due_at_first_notification(bind, occ, pending)


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


def _due_at_first_notification(bind: sa.Connection, occ: sa.Row, pending: Sequence[sa.Row]) -> None:
    """ "За день и за час до встречи в 15": due at the first pending notification, the pending ones' leads kept."""
    first = pending[0].fire_at
    if (
        first != occ.due_at
        and bind.execute(DUE_TAKEN, {"item": occ.item_id, "due": first, "occ": occ.id}).first() is None
    ):
        bind.execute(MOVE_OCCURRENCE, {"due": first, "occ": occ.id})
    if not occ.series:  # a series keeps its leads for the next instances
        leads = sorted({r.lead_minutes for r in pending if r.lead_minutes > 0}, reverse=True)
        bind.execute(ITEM_LEADS, {"leads": leads or None, "item": occ.item_id})
    bind.execute(ZERO_LEADS, {"occ": occ.id})


def downgrade() -> None:
    """No-op: the old fire times are not kept (see the module docstring)."""
