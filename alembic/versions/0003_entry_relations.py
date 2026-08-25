"""add entry_relations table

Revision ID: 0003_entry_relations
Revises: 0002_project_description
Create Date: 2026-08-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0003_entry_relations"
down_revision: Union[str, None] = "0002_project_description"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "entry_relations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "from_entry_id", UUID(as_uuid=True), sa.ForeignKey("entries.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "to_entry_id", UUID(as_uuid=True), sa.ForeignKey("entries.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("relation_type", sa.String(16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_by", sa.String(255), nullable=False),
        sa.UniqueConstraint("from_entry_id", "to_entry_id", "relation_type", name="uq_relations_from_to_type"),
        sa.CheckConstraint("from_entry_id != to_entry_id", name="ck_relations_no_self_link"),
        sa.CheckConstraint(
            "relation_type IN ('related_to', 'same_as', 'follow_up_of', 'mentions')", name="ck_relations_type"
        ),
    )
    op.create_index("ix_relations_from_entry_id", "entry_relations", ["from_entry_id"])
    op.create_index("ix_relations_to_entry_id", "entry_relations", ["to_entry_id"])


def downgrade() -> None:
    op.drop_table("entry_relations")
