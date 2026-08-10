"""Project & subtopic management -- the web UI's create/rename/delete write path, and (for
project creation only) the one MCP tool that touches structure rather than just entries. Same
discipline as entries.py: DB change first (flushed, not committed), then the matching git
change, roll back the DB on git failure, commit only once both sides agree.
"""

import asyncio

from slugify import slugify
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SENSITIVITY_LEVELS, Entry, Project, Subtopic
from app.db.repository import (
    NotFoundError,
    get_all_subtopics_for_project,
    get_project_by_slug,
    get_subtopic_path_parts,
)
from app.services.git_store import get_git_store, git_write_lock


def _normalize_description(description: str | None) -> str | None:
    if description is None:
        return None
    stripped = description.strip()
    return stripped or None


async def create_project(
    session: AsyncSession, *, name: str, sensitivity_level: str, description: str | None = None
) -> Project:
    if sensitivity_level not in SENSITIVITY_LEVELS:
        raise ValueError(f"invalid sensitivity_level: {sensitivity_level!r}")
    slug = slugify(name)[:64]
    if not slug:
        raise ValueError(f"name produces an empty slug: {name!r}")

    existing = (await session.execute(select(Project).where(Project.slug == slug))).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"a project with slug {slug!r} already exists")

    project = Project(
        slug=slug, name=name, sensitivity_level=sensitivity_level, description=_normalize_description(description)
    )
    session.add(project)
    await session.commit()
    return (await session.execute(select(Project).where(Project.id == project.id))).scalar_one()


async def rename_project(
    session: AsyncSession, *, project_slug: str, name: str, description: str | None = None
) -> Project:
    project = await get_project_by_slug(session, project_slug)
    project.name = name
    project.description = _normalize_description(description)
    await session.commit()
    return (await session.execute(select(Project).where(Project.id == project.id))).scalar_one()


async def delete_project(session: AsyncSession, *, project_slug: str, actor: str) -> None:
    project = await get_project_by_slug(session, project_slug)
    slug = project.slug  # capture before delete -- it's also the git directory name

    await session.delete(project)
    await session.flush()

    message = f"delete project: {slug}\n\nby {actor}"
    try:
        async with git_write_lock:
            await asyncio.to_thread(get_git_store().remove_path_and_commit, slug, message, actor)
    except Exception:
        await session.rollback()
        raise

    await session.commit()


async def rename_subtopic(session: AsyncSession, *, subtopic_id, name: str) -> Subtopic:
    subtopic = await session.get(Subtopic, subtopic_id)
    if subtopic is None:
        raise NotFoundError(f"unknown subtopic: {subtopic_id}")
    subtopic.name = name
    await session.commit()
    return (await session.execute(select(Subtopic).where(Subtopic.id == subtopic.id))).scalar_one()


async def delete_subtopic(session: AsyncSession, *, subtopic_id, actor: str) -> None:
    subtopic = await session.get(Subtopic, subtopic_id)
    if subtopic is None:
        raise NotFoundError(f"unknown subtopic: {subtopic_id}")
    # capture the full path before delete -- get_subtopic_path_parts walks the parent chain,
    # which needs the row (and its ancestors) to still exist
    relative_dir = "/".join(await get_subtopic_path_parts(session, subtopic))

    await session.delete(subtopic)
    await session.flush()

    message = f"delete subtopic: {relative_dir}\n\nby {actor}"
    try:
        async with git_write_lock:
            await asyncio.to_thread(get_git_store().remove_path_and_commit, relative_dir, message, actor)
    except Exception:
        await session.rollback()
        raise

    await session.commit()


async def get_project_stats(session: AsyncSession, project: Project) -> dict:
    entry_count, char_count = (
        await session.execute(
            select(func.count(Entry.id), func.coalesce(func.sum(func.length(Entry.body_markdown)), 0))
            .join(Subtopic, Entry.subtopic_id == Subtopic.id)
            .where(Subtopic.project_id == project.id, Entry.status == "aktuell")
        )
    ).one()
    subtopic_count = (
        await session.execute(select(func.count(Subtopic.id)).where(Subtopic.project_id == project.id))
    ).scalar_one()
    return {"entry_count": entry_count, "char_count": char_count, "subtopic_count": subtopic_count}


async def get_subtopic_stats_map(session: AsyncSession, project: Project) -> dict:
    """subtopic_id -> {entry_count, char_count}, INCLUDING descendants -- one grouped query for
    direct per-subtopic totals, then a memoized bottom-up rollup in Python (arbitrary depth,
    no assumption about traversal order)."""
    all_subtopics = await get_all_subtopics_for_project(session, project)

    direct_rows = (
        await session.execute(
            select(Entry.subtopic_id, func.count(Entry.id), func.coalesce(func.sum(func.length(Entry.body_markdown)), 0))
            .join(Subtopic, Entry.subtopic_id == Subtopic.id)
            .where(Subtopic.project_id == project.id, Entry.status == "aktuell")
            .group_by(Entry.subtopic_id)
        )
    ).all()
    direct = {row[0]: {"entry_count": row[1], "char_count": row[2]} for row in direct_rows}

    children_by_parent: dict = {}
    for s in all_subtopics:
        children_by_parent.setdefault(s.parent_subtopic_id, []).append(s.id)

    rolled_up: dict = {}

    def rollup(subtopic_id) -> dict:
        if subtopic_id in rolled_up:
            return rolled_up[subtopic_id]
        own = direct.get(subtopic_id, {"entry_count": 0, "char_count": 0})
        totals = dict(own)
        for child_id in children_by_parent.get(subtopic_id, []):
            child_totals = rollup(child_id)
            totals["entry_count"] += child_totals["entry_count"]
            totals["char_count"] += child_totals["char_count"]
        rolled_up[subtopic_id] = totals
        return totals

    for s in all_subtopics:
        rollup(s.id)
    return rolled_up
