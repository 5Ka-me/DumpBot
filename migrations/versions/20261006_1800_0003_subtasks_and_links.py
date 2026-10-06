"""subtasks and related items

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06 18:00:00+00:00

Iteration 2, section C (docs/features/iteration-2.md): "🔗 похоже на …" and checklists. Written by hand, checked with
``alembic check`` against the models:
- items.parent_id (FK items.id, ON DELETE CASCADE) and items.position: a step of a checklist is an item with the id
  of its parent and its place in the list; NULL for top-level items. ix_items_parent (parent_id, position);
- item_links (item_id, related_id) -> items.id, both ON DELETE CASCADE, kind 'similar', created_at; the reverse
  lookup (cards of the earlier item) uses ix_item_links_related.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("items", sa.Column("parent_id", sa.BigInteger(), nullable=True))
    op.add_column("items", sa.Column("position", sa.Integer(), nullable=True))
    op.create_foreign_key(op.f("fk_items_parent_id_items"), "items", "items", ["parent_id"], ["id"], ondelete="CASCADE")
    op.create_index("ix_items_parent", "items", ["parent_id", "position"], unique=False)
    op.create_table(
        "item_links",
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("related_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), server_default="similar", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_item_links_item_id_items"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["related_id"], ["items.id"], name=op.f("fk_item_links_related_id_items"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("item_id", "related_id", name=op.f("pk_item_links")),
    )
    op.create_index("ix_item_links_related", "item_links", ["related_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_item_links_related", table_name="item_links")
    op.drop_table("item_links")
    op.drop_index("ix_items_parent", table_name="items")
    op.drop_constraint(op.f("fk_items_parent_id_items"), "items", type_="foreignkey")
    op.drop_column("items", "position")
    op.drop_column("items", "parent_id")
