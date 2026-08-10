from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Project, Subtopic


async def get_all_subtopics_for_project(session: AsyncSession, project: Project) -> list[Subtopic]:
    return list(
        (await session.execute(select(Subtopic).where(Subtopic.project_id == project.id))).scalars()
    )


def collect_descendant_ids(subtopics: list[Subtopic], root_id) -> set:
    """root_id + every subtopic reachable from it, given ALL of the project's subtopics."""
    children_by_parent: dict = {}
    for s in subtopics:
        children_by_parent.setdefault(s.parent_subtopic_id, []).append(s.id)

    result = {root_id}
    frontier = [root_id]
    while frontier:
        current = frontier.pop()
        for child_id in children_by_parent.get(current, []):
            if child_id not in result:
                result.add(child_id)
                frontier.append(child_id)
    return result


class NotFoundError(Exception):
    pass


async def get_project_by_slug(session: AsyncSession, slug: str) -> Project:
    project = (await session.execute(select(Project).where(Project.slug == slug))).scalar_one_or_none()
    if project is None:
        raise NotFoundError(f"unknown project: {slug}")
    return project


async def resolve_or_create_subtopic_path(session: AsyncSession, project: Project, path: str) -> Subtopic:
    """Walk/create a '/'-separated subtopic path (e.g. 'kunde-mueller/vorgang-2026-08'),
    auto-creating any missing level. Segments are slugified; the display name of a newly
    created level is derived from the segment (title-cased) since callers only pass slugs."""
    parent: Subtopic | None = None
    subtopic: Subtopic | None = None
    for raw_segment in path.strip("/").split("/"):
        segment = slugify(raw_segment)
        if not segment:
            continue
        stmt = select(Subtopic).where(
            Subtopic.project_id == project.id,
            Subtopic.slug == segment,
            Subtopic.parent_subtopic_id == parent.id if parent is not None else Subtopic.parent_subtopic_id.is_(None),
        )
        subtopic = (await session.execute(stmt)).scalar_one_or_none()
        if subtopic is None:
            subtopic = Subtopic(
                project_id=project.id,
                parent_subtopic_id=parent.id if parent is not None else None,
                slug=segment,
                name=raw_segment.replace("-", " ").title(),
            )
            session.add(subtopic)
            await session.flush()
        parent = subtopic
    if subtopic is None:
        raise ValueError(f"empty subtopic path: {path!r}")
    return subtopic


async def find_subtopic_by_path(session: AsyncSession, project: Project, path: str) -> Subtopic:
    parent_id = None
    subtopic: Subtopic | None = None
    for raw_segment in path.strip("/").split("/"):
        segment = slugify(raw_segment)
        if not segment:
            continue
        stmt = select(Subtopic).where(
            Subtopic.project_id == project.id,
            Subtopic.slug == segment,
            Subtopic.parent_subtopic_id == parent_id if parent_id is not None else Subtopic.parent_subtopic_id.is_(None),
        )
        subtopic = (await session.execute(stmt)).scalar_one_or_none()
        if subtopic is None:
            raise NotFoundError(f"unknown subtopic path: {path!r}")
        parent_id = subtopic.id
    if subtopic is None:
        raise ValueError(f"empty subtopic path: {path!r}")
    return subtopic


async def get_subtopic_path_parts(session: AsyncSession, subtopic: Subtopic) -> list[str]:
    """Full filesystem-style path parts: [project_slug, *subtopic_slug_chain]."""
    parts: list[str] = [subtopic.slug]
    current = subtopic
    while current.parent_subtopic_id is not None:
        current = await session.get(Subtopic, current.parent_subtopic_id)
        parts.insert(0, current.slug)
    project = await session.get(Project, subtopic.project_id)
    parts.insert(0, project.slug)
    return parts
