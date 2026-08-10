"""The single write path for entries. Both MCP tools and web routes call ONLY these
functions — neither talks to the DB or the git repo directly. upsert_entry() is where the
DB-write + git-commit + entry_versions-row sequence from the design doc actually happens.
"""

import asyncio

from slugify import slugify
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Entry, EntryVersion, Source, Subtopic, Tag, entry_tags
from app.db.repository import (
    NotFoundError,
    collect_descendant_ids,
    find_subtopic_by_path,
    get_all_subtopics_for_project,
    get_project_by_slug,
    get_subtopic_path_parts,
    resolve_or_create_subtopic_path,
)
from app.services import sources as sources_service
from app.services.embeddings import embed_passage_async
from app.services.git_store import get_git_store, git_write_lock


async def _set_tags(session: AsyncSession, entry: Entry, tag_names: list[str]) -> None:
    """Replace entry's tag set. Goes through the entry_tags association table directly
    (not `entry.tags = [...]`) because assigning an ORM collection on a persistent object
    forces a synchronous lazy-load of the current collection to diff against, which raises
    MissingGreenlet under AsyncSession -- there's no awaited ORM call for it to piggyback on."""
    tag_ids = []
    for raw_name in tag_names:
        name = raw_name.strip().lower()
        if not name:
            continue
        tag = (await session.execute(select(Tag).where(Tag.name == name))).scalar_one_or_none()
        if tag is None:
            tag = Tag(name=name)
            session.add(tag)
            await session.flush()
        tag_ids.append(tag.id)

    await session.execute(delete(entry_tags).where(entry_tags.c.entry_id == entry.id))
    if tag_ids:
        await session.execute(insert(entry_tags), [{"entry_id": entry.id, "tag_id": tid} for tid in tag_ids])


async def _finalize_write(
    session: AsyncSession,
    entry: Entry,
    subtopic: Subtopic,
    *,
    actor: str,
    tags: list[str] | None,
    old_slug: str | None,
    action: str,
) -> Entry:
    """Shared tail of every entry write: embed, (optionally) replace tags, render+commit to
    git, record the entry_versions row, commit the DB transaction, and return the fully
    reloaded entry. `old_slug` is set only when an existing entry's title (and therefore its
    slug/filename) changed, so the git file is renamed in the same commit rather than leaving
    an orphaned copy under the old path -- see git_store.write_and_commit."""
    entry.body_embedding = await embed_passage_async(f"{entry.title}\n\n{entry.body_markdown}")

    if tags is not None:
        await _set_tags(session, entry, tags)

    source_rows = (
        await session.execute(select(Source.source_type, Source.source_ref).where(Source.entry_id == entry.id))
    ).all()
    # explicit query, not `entry.tags`: relationship attributes on a persistent object aren't
    # reliably still-loaded by this point (a flush() that touches server-computed columns can
    # leave other attributes expired), and a bare sync access outside an awaited ORM call
    # raises MissingGreenlet under AsyncSession if it needs to lazy-load.
    if tags is not None:
        tag_names = tags
    else:
        tag_names = [
            row[0]
            for row in (
                await session.execute(
                    select(Tag.name).join(entry_tags, entry_tags.c.tag_id == Tag.id).where(
                        entry_tags.c.entry_id == entry.id
                    )
                )
            ).all()
        ]

    path_parts = await get_subtopic_path_parts(session, subtopic)
    subtopic_path_str = "/".join(path_parts[1:])
    new_relative_path = "/".join([*path_parts, entry.slug]) + ".md"
    old_relative_path = "/".join([*path_parts, old_slug]) + ".md" if old_slug else None

    rendered = get_git_store().render(
        title=entry.title,
        subtopic_path=subtopic_path_str,
        tags=tag_names,
        sources=[f"{t}:{r}" for t, r in source_rows],
        body_markdown=entry.body_markdown,
    )
    message = f"{action}: {path_parts[0]}/{subtopic_path_str}/{entry.slug}\n\nby {actor}"

    try:
        async with git_write_lock:
            commit_hash = await asyncio.to_thread(
                get_git_store().write_and_commit,
                new_relative_path,
                rendered,
                message,
                actor,
                old_relative_path=old_relative_path,
            )
    except Exception:
        await session.rollback()
        raise

    session.add(
        EntryVersion(
            entry_id=entry.id,
            body_markdown=entry.body_markdown,
            git_commit_hash=commit_hash,
            created_by=actor,
        )
    )
    await session.commit()
    # Re-fetch (rather than session.refresh()) so both columns and the tags relationship come
    # back fully loaded -- refresh(attribute_names=[...]) only guarantees the named attributes
    # and can leave others expired, which then raises MissingGreenlet on next sync access
    # (e.g. from a caller serializing the response, or later from Jinja2 template rendering).
    return await get_entry_by_id(session, entry.id)


async def upsert_entry(
    session: AsyncSession,
    *,
    project_slug: str,
    subtopic_path: str,
    title: str,
    body_markdown: str,
    actor: str,
    sources: list[tuple[str, str]] | None = None,
    tags: list[str] | None = None,
    follow_up_status: str | None = None,
) -> Entry:
    """Create or update the entry identified by (subtopic, slugify(title)) within a project --
    the MCP tool's write path, where the caller only ever supplies a title, not an entry id.
    The subtopic path is auto-created if any level is missing. `follow_up_status` of "none"
    clears the field; None (the default) leaves it untouched.

    NOTE: this is keyed by (subtopic, slugify(title)) -- calling it again with a DIFFERENT
    title creates a new entry rather than renaming the existing one. For "I have an entry_id,
    save my edits" (the web UI's edit form, where a human plausibly tweaks the title), use
    update_entry() instead."""
    project = await get_project_by_slug(session, project_slug)
    subtopic = await resolve_or_create_subtopic_path(session, project, subtopic_path)

    slug = slugify(title)[:128]
    entry = (
        await session.execute(
            select(Entry)
            .options(selectinload(Entry.tags))
            .where(Entry.subtopic_id == subtopic.id, Entry.slug == slug)
        )
    ).scalar_one_or_none()

    is_new = entry is None
    if entry is None:
        entry = Entry(
            subtopic_id=subtopic.id,
            slug=slug,
            title=title,
            body_markdown=body_markdown,
            status="aktuell",
            updated_by=actor,
        )
        session.add(entry)
    else:
        entry.title = title
        entry.body_markdown = body_markdown
        entry.updated_by = actor
        entry.status = "aktuell"

    if follow_up_status is not None:
        entry.follow_up_status = None if follow_up_status == "none" else follow_up_status

    await session.flush()

    if sources:
        await sources_service.register_sources(session, entry, sources)

    return await _finalize_write(
        session, entry, subtopic, actor=actor, tags=tags, old_slug=None, action="create" if is_new else "update"
    )


async def update_entry(
    session: AsyncSession,
    *,
    entry_id,
    title: str,
    body_markdown: str,
    actor: str,
    tags: list[str] | None = None,
    follow_up_status: str | None = None,
) -> Entry:
    """Update an entry identified by its own id -- the web UI's edit-form write path. Unlike
    upsert_entry, a title change here renames the existing entry (and its git file) instead of
    creating a new one. Moving an entry to a different subtopic isn't supported (out of scope
    for v1); only title/body/tags/follow_up_status can change."""
    entry = await get_entry_by_id(session, entry_id)
    subtopic = await session.get(Subtopic, entry.subtopic_id)

    new_slug = slugify(title)[:128]
    old_slug = entry.slug if new_slug != entry.slug else None
    if old_slug:
        collision = (
            await session.execute(
                select(Entry).where(
                    Entry.subtopic_id == subtopic.id, Entry.slug == new_slug, Entry.id != entry.id
                )
            )
        ).scalar_one_or_none()
        if collision is not None:
            raise ValueError(f"another entry with title-slug {new_slug!r} already exists in this subtopic")

    entry.title = title
    entry.slug = new_slug
    entry.body_markdown = body_markdown
    entry.updated_by = actor
    entry.status = "aktuell"
    if follow_up_status is not None:
        entry.follow_up_status = None if follow_up_status == "none" else follow_up_status

    await session.flush()

    return await _finalize_write(session, entry, subtopic, actor=actor, tags=tags, old_slug=old_slug, action="update")


async def get_entries(
    session: AsyncSession,
    *,
    project_slug: str,
    subtopic_path: str | None = None,
    include_descendants: bool = True,
    status: str | None = "aktuell",
) -> list[Entry]:
    project = await get_project_by_slug(session, project_slug)

    stmt = (
        select(Entry)
        .options(selectinload(Entry.tags))
        .join(Subtopic, Entry.subtopic_id == Subtopic.id)
        .where(Subtopic.project_id == project.id)
    )
    if status:
        stmt = stmt.where(Entry.status == status)

    if subtopic_path:
        subtopic = await find_subtopic_by_path(session, project, subtopic_path)
        if include_descendants:
            all_subtopics = await get_all_subtopics_for_project(session, project)
            subtopic_ids = collect_descendant_ids(all_subtopics, subtopic.id)
            stmt = stmt.where(Entry.subtopic_id.in_(subtopic_ids))
        else:
            stmt = stmt.where(Entry.subtopic_id == subtopic.id)

    stmt = stmt.order_by(Entry.updated_at.desc())
    return list((await session.execute(stmt)).scalars())


async def list_open(session: AsyncSession, *, project_slug: str | None = None) -> list[Entry]:
    stmt = select(Entry).options(selectinload(Entry.tags)).where(Entry.follow_up_status.is_not(None))
    if project_slug:
        project = await get_project_by_slug(session, project_slug)
        stmt = stmt.join(Subtopic, Entry.subtopic_id == Subtopic.id).where(Subtopic.project_id == project.id)
    stmt = stmt.order_by(Entry.updated_at.desc())
    return list((await session.execute(stmt)).scalars())


async def get_entry_by_id(session: AsyncSession, entry_id) -> Entry:
    # a plain select(), not session.get(): get() short-circuits to the identity map when the
    # PK is already cached in this session (e.g. right after upsert_entry's own flush) and
    # skips issuing SQL entirely, silently ignoring the selectinload option and leaving `tags`
    # unloaded -- a select()+execute() always runs and applies eager-load options properly.
    entry = (
        await session.execute(select(Entry).options(selectinload(Entry.tags)).where(Entry.id == entry_id))
    ).scalar_one_or_none()
    if entry is None:
        raise NotFoundError(f"unknown entry: {entry_id}")
    return entry


async def get_history(session: AsyncSession, entry_id, limit: int = 20) -> list[EntryVersion]:
    await get_entry_by_id(session, entry_id)  # raises NotFoundError if missing
    stmt = (
        select(EntryVersion)
        .where(EntryVersion.entry_id == entry_id)
        .order_by(EntryVersion.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())
