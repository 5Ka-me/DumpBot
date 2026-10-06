"""mini app: raw message source, job results

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-06 20:00:00+00:00

Iteration 2, section D (docs/features/iteration-2.md): the Telegram Mini App. Written by hand, checked with
``alembic check`` against the models:
- raw_messages.source ('telegram' | 'app', NOT NULL, default 'telegram'): "закинуть в свалку" from the app is a raw
  message with a synthetic message id that gets no card in the chat;
- jobs.result (JSONB NULL): what a finished job reports to the app ("🕒 Время" by text, "🪄 Разбить на шаги").
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("raw_messages", sa.Column("source", sa.String(length=16), server_default="telegram", nullable=False))
    op.add_column("jobs", sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "result")
    op.drop_column("raw_messages", "source")
