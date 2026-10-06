"""item periods: fuzzy time spans and "someday"

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06 14:00:00+00:00

Iteration 2, section A (docs/features/iteration-2.md): "на этой неделе", "в октябре", "летом", "на днях",
"когда-нибудь". Written by hand, checked with ``alembic check`` against the models:
- items.period_kind (week | month | season | soon | someday), items.period_start / period_end (local dates,
  inclusive) — NULL for items without a fuzzy time;
- items.someday (NOT NULL, default false): someday items have no occurrence and stay out of /tasks;
- ix_items_period (period_start, period_end) for the digest's period section and query range overlaps.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("items", sa.Column("period_kind", sa.String(length=16), nullable=True))
    op.add_column("items", sa.Column("period_start", sa.Date(), nullable=True))
    op.add_column("items", sa.Column("period_end", sa.Date(), nullable=True))
    op.add_column("items", sa.Column("someday", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.create_index("ix_items_period", "items", ["period_start", "period_end"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_items_period", table_name="items")
    op.drop_column("items", "someday")
    op.drop_column("items", "period_end")
    op.drop_column("items", "period_start")
    op.drop_column("items", "period_kind")
