"""add 'document' to source_type vocabulary

Revision ID: 0005_source_type_document
Revises: 0004_relation_types_expand
Create Date: 2026-08-26

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005_source_type_document"
down_revision: Union[str, None] = "0004_relation_types_expand"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_TYPES = ("mail", "whatsapp", "signal", "paperless", "nextcloud", "hero")
NEW_TYPES = OLD_TYPES + ("document",)


def upgrade() -> None:
    op.drop_constraint("ck_sources_type", "sources", type_="check")
    op.create_check_constraint("ck_sources_type", "sources", f"source_type IN {NEW_TYPES}")


def downgrade() -> None:
    op.execute(f"DELETE FROM sources WHERE source_type NOT IN {OLD_TYPES}")
    op.drop_constraint("ck_sources_type", "sources", type_="check")
    op.create_check_constraint("ck_sources_type", "sources", f"source_type IN {OLD_TYPES}")
