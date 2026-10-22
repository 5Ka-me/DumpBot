"""events: the journal of commands, button presses, Mini App calls, reminders sent and digest lines

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-22 12:00:00+00:00

Iteration 4, section 5 (docs/features/iteration-4.md, "Журнал действий"). Written by hand, checked with
``alembic check`` against the models. Only CREATE TABLE / CREATE INDEX (Supabase-safe, nothing existing changes):
- events (id, created_at, type, source, item_id, occurrence_id, data JSONB): what the owner did and what the bot
  showed, written best effort; no foreign keys (the journal outlives what it mentions);
- ix_events_type_created (type, created_at), ix_events_item_created (item_id, created_at).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=True),
        sa.Column("item_id", sa.BigInteger(), nullable=True),
        sa.Column("occurrence_id", sa.BigInteger(), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
    )
    op.create_index("ix_events_type_created", "events", ["type", "created_at"], unique=False)
    op.create_index("ix_events_item_created", "events", ["item_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_events_item_created", table_name="events")
    op.drop_index("ix_events_type_created", table_name="events")
    op.drop_table("events")
