import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    String,
    Table,
    Text,
    UniqueConstraint,
    Column,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import get_settings
from app.db.base import Base

SENSITIVITY_LEVELS = ("niedrig", "mittel", "hoch")
ENTRY_STATUSES = ("aktuell", "veraltet")
FOLLOW_UP_STATUSES = ("offen", "wartet")
SOURCE_TYPES = ("mail", "whatsapp", "signal", "paperless", "nextcloud", "hero")
RELATION_TYPES = ("related_to", "same_as", "follow_up_of", "mentions")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


entry_tags = Table(
    "entry_tags",
    Base.metadata,
    Column("entry_id", UUID(as_uuid=True), ForeignKey("entries.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", UUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (CheckConstraint(f"sensitivity_level IN {SENSITIVITY_LEVELS}", name="ck_projects_sensitivity"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sensitivity_level: Mapped[str] = mapped_column(String(16), nullable=False)
    cowork_project_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    subtopics: Mapped[list["Subtopic"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Subtopic(Base):
    __tablename__ = "subtopics"
    __table_args__ = (
        UniqueConstraint("project_id", "parent_subtopic_id", "slug", name="uq_subtopics_project_parent_slug"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    parent_subtopic_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subtopics.id", ondelete="CASCADE"), nullable=True
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped["Project"] = relationship(back_populates="subtopics")
    parent: Mapped["Subtopic | None"] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list["Subtopic"]] = relationship(back_populates="parent", cascade="all, delete-orphan")
    entries: Mapped[list["Entry"]] = relationship(back_populates="subtopic", cascade="all, delete-orphan")


class Entry(Base):
    __tablename__ = "entries"
    __table_args__ = (
        UniqueConstraint("subtopic_id", "slug", name="uq_entries_subtopic_slug"),
        CheckConstraint(f"status IN {ENTRY_STATUSES}", name="ck_entries_status"),
        CheckConstraint(
            f"follow_up_status IS NULL OR follow_up_status IN {FOLLOW_UP_STATUSES}",
            name="ck_entries_follow_up_status",
        ),
        Index("ix_entries_body_tsvector", "body_tsvector", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    subtopic_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subtopics.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    # Server-generated column (real DDL text picked at migration time -- 'german' vs. 'simple'
    # fallback, see alembic/versions/0001_initial.py). The Computed() clause here isn't what
    # creates the column (Alembic does, via raw SQL); it exists so the ORM knows to exclude
    # this column from INSERT/UPDATE statements instead of sending NULL, which Postgres
    # rejects for GENERATED ALWAYS AS columns.
    body_tsvector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(body_markdown, ''))"),
        nullable=True,
    )
    body_embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding_dim), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="aktuell")
    follow_up_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)

    subtopic: Mapped["Subtopic"] = relationship(back_populates="entries")
    versions: Mapped[list["EntryVersion"]] = relationship(back_populates="entry", cascade="all, delete-orphan")
    sources: Mapped[list["Source"]] = relationship(back_populates="entry", cascade="all, delete-orphan")
    tags: Mapped[list["Tag"]] = relationship(secondary=entry_tags, back_populates="entries")


class EntryVersion(Base):
    __tablename__ = "entry_versions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entries.id", ondelete="CASCADE"), nullable=False
    )
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    git_commit_hash: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    entry: Mapped["Entry"] = relationship(back_populates="versions")


class Source(Base):
    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("source_type", "source_ref", name="uq_sources_type_ref"),
        CheckConstraint(f"source_type IN {SOURCE_TYPES}", name="ck_sources_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entries.id", ondelete="CASCADE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    entry: Mapped["Entry"] = relationship(back_populates="sources")


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    entries: Mapped[list["Entry"]] = relationship(secondary=entry_tags, back_populates="tags")


class Relation(Base):
    """A direct, typed link from one entry to another (e.g. "these are the same client",
    filed under two different titles). Postgres-only -- deliberately not mirrored into either
    entry's git frontmatter, since a link to another entry can go stale if that entry is later
    renamed or deleted, and this table's own created_at/created_by is audit trail enough
    without that sync problem. See app/services/relations.py, the only code that touches this
    table."""

    __tablename__ = "entry_relations"
    __table_args__ = (
        UniqueConstraint("from_entry_id", "to_entry_id", "relation_type", name="uq_relations_from_to_type"),
        CheckConstraint("from_entry_id != to_entry_id", name="ck_relations_no_self_link"),
        CheckConstraint(f"relation_type IN {RELATION_TYPES}", name="ck_relations_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    from_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entries.id", ondelete="CASCADE"), nullable=False
    )
    to_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("entries.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
