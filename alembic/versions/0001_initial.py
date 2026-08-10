"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Keep in sync with app.config.Settings.embedding_dim. A future embedding-model swap needs a
# follow-up migration (new column + reindex) plus scripts/reembed_all.py, not an in-place edit here.
EMBEDDING_DIM = 384


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "projects",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("sensitivity_level", sa.String(16), nullable=False),
        sa.Column("cowork_project_ref", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("sensitivity_level IN ('niedrig','mittel','hoch')", name="ck_projects_sensitivity"),
    )

    op.create_table(
        "subtopics",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "parent_subtopic_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subtopics.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "parent_subtopic_id", "slug", name="uq_subtopics_project_parent_slug"),
    )
    op.create_index("ix_subtopics_project_id", "subtopics", ["project_id"])
    op.create_index("ix_subtopics_parent_subtopic_id", "subtopics", ["parent_subtopic_id"])

    op.create_table(
        "entries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subtopic_id", UUID(as_uuid=True), sa.ForeignKey("subtopics.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("slug", sa.String(128), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("body_embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="aktuell"),
        sa.Column("follow_up_status", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_by", sa.String(255), nullable=False),
        sa.UniqueConstraint("subtopic_id", "slug", name="uq_entries_subtopic_slug"),
        sa.CheckConstraint("status IN ('aktuell','veraltet')", name="ck_entries_status"),
        sa.CheckConstraint(
            "follow_up_status IS NULL OR follow_up_status IN ('offen','wartet')",
            name="ck_entries_follow_up_status",
        ),
    )
    op.create_index("ix_entries_subtopic_id", "entries", ["subtopic_id"])
    op.create_index("ix_entries_follow_up_status", "entries", ["follow_up_status"])
    op.create_index(
        "ix_entries_body_embedding_hnsw",
        "entries",
        ["body_embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"body_embedding": "vector_cosine_ops"},
    )

    # body_tsvector: generated column, added via raw SQL because the text-search config is
    # decided at migration time against the live DB (falls back to 'simple' if 'german' is
    # unavailable) rather than hardcoded.
    conn = op.get_bind()
    has_german = conn.execute(sa.text("SELECT 1 FROM pg_ts_config WHERE cfgname = 'german'")).first()
    ts_config = "german" if has_german else "simple"
    op.execute(
        f"""
        ALTER TABLE entries ADD COLUMN body_tsvector tsvector
        GENERATED ALWAYS AS (
            to_tsvector('{ts_config}', coalesce(title, '') || ' ' || coalesce(body_markdown, ''))
        ) STORED
        """
    )
    op.create_index("ix_entries_body_tsvector", "entries", ["body_tsvector"], postgresql_using="gin")

    op.create_table(
        "entry_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("entry_id", UUID(as_uuid=True), sa.ForeignKey("entries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("body_markdown", sa.Text(), nullable=False),
        sa.Column("git_commit_hash", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("created_by", sa.String(255), nullable=False),
    )
    op.create_index("ix_entry_versions_entry_id", "entry_versions", ["entry_id"])

    op.create_table(
        "sources",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("entry_id", UUID(as_uuid=True), sa.ForeignKey("entries.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_ref", sa.String(512), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("source_type", "source_ref", name="uq_sources_type_ref"),
        sa.CheckConstraint(
            "source_type IN ('mail','whatsapp','signal','paperless','nextcloud','hero')",
            name="ck_sources_type",
        ),
    )
    op.create_index("ix_sources_entry_id", "sources", ["entry_id"])

    op.create_table(
        "tags",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
    )

    op.create_table(
        "entry_tags",
        sa.Column(
            "entry_id", UUID(as_uuid=True), sa.ForeignKey("entries.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("tag_id", UUID(as_uuid=True), sa.ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("entry_tags")
    op.drop_table("tags")
    op.drop_table("sources")
    op.drop_table("entry_versions")
    op.drop_table("entries")
    op.drop_table("subtopics")
    op.drop_table("projects")
