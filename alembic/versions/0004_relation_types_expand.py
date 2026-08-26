"""expand relation_type vocabulary (supersedes, causes, fixes, contradicts)

Revision ID: 0004_relation_types_expand
Revises: 0003_entry_relations
Create Date: 2026-08-26

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0004_relation_types_expand"
down_revision: Union[str, None] = "0003_entry_relations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_TYPES = ("related_to", "same_as", "follow_up_of", "mentions")
NEW_TYPES = OLD_TYPES + ("supersedes", "causes", "fixes", "contradicts")


def upgrade() -> None:
    op.drop_constraint("ck_relations_type", "entry_relations", type_="check")
    op.create_check_constraint("ck_relations_type", "entry_relations", f"relation_type IN {NEW_TYPES}")


def downgrade() -> None:
    # rows using one of the four new types can't survive the narrower constraint
    op.execute(f"DELETE FROM entry_relations WHERE relation_type NOT IN {OLD_TYPES}")
    op.drop_constraint("ck_relations_type", "entry_relations", type_="check")
    op.create_check_constraint("ck_relations_type", "entry_relations", f"relation_type IN {OLD_TYPES}")
