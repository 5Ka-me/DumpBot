"""downtime: the scheduler's last tick, reminders held over quiet hours

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09 12:00:00+00:00

Iteration 3, section 5 (docs/features/iteration-3.md, "Ничего не теряется после простоя"). Written by hand, checked
with ``alembic check`` against the models:
- app_state (key TEXT primary key, value JSONB, updated_at): small values that survive a restart; the scheduler keeps
  the time of its last tick there ("scheduler:last_tick") to notice that the bot was down;
- reminders.held_from (timestamptz NULL): a reminder that came due late inside quiet hours waits for their end
  (``fire_at`` moves there) and keeps its own time here; it is then sent silently. NULL for every existing row.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_state",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_state")),
    )
    op.add_column("reminders", sa.Column("held_from", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("reminders", "held_from")
    op.drop_table("app_state")
