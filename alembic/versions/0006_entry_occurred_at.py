"""add entries.occurred_at (event date, separate from updated_at)

Revision ID: 0006_entry_occurred_at
Revises: 0005_source_type_document
Create Date: 2026-10-26

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_entry_occurred_at"
down_revision: Union[str, None] = "0005_source_type_document"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable and deliberately not backfilled: NULL means "event date unknown", and search
    # falls back to updated_at for those rows (see app/services/search.py). Copying
    # updated_at in here would turn a guess into something that looks like a recorded fact.
    op.add_column("entries", sa.Column("occurred_at", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("entries", "occurred_at")
