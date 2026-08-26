"""Full-text (Postgres tsvector/GIN) + semantic (pgvector/HNSW) search, merged with
Reciprocal Rank Fusion so neither signal has to "win" outright -- an exact term hit and a
semantic near-miss both surface, which plain vector-only or fulltext-only search would miss."""

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Entry, Relation, Subtopic, Tag, entry_tags
from app.db.repository import (
    collect_descendant_ids,
    find_subtopic_by_path,
    get_all_subtopics_for_project,
    get_project_by_slug,
)
from app.services.embeddings import embed_query_async

_RRF_K = 60


def _rrf_merge(rankings: list[list]) -> list[tuple]:
    scores: dict = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)


def _tag_filter_subquery(tag_names: list[str]):
    return select(entry_tags.c.entry_id).join(Tag, Tag.id == entry_tags.c.tag_id).where(Tag.name.in_(tag_names))


async def _fulltext_candidates(
    session: AsyncSession, query: str, project_id, subtopic_ids, tag_names, limit: int
) -> list:
    ts_query = func.websearch_to_tsquery("german", query)
    stmt = select(Entry.id).where(Entry.body_tsvector.op("@@")(ts_query))
    if project_id is not None:
        stmt = stmt.join(Subtopic, Entry.subtopic_id == Subtopic.id).where(Subtopic.project_id == project_id)
    if subtopic_ids is not None:
        stmt = stmt.where(Entry.subtopic_id.in_(subtopic_ids))
    if tag_names:
        stmt = stmt.where(Entry.id.in_(_tag_filter_subquery(tag_names)))
    stmt = stmt.order_by(func.ts_rank(Entry.body_tsvector, ts_query).desc()).limit(limit)
    return [row[0] for row in (await session.execute(stmt)).all()]


async def _vector_candidates(
    session: AsyncSession, query: str, project_id, subtopic_ids, tag_names, limit: int
) -> list:
    query_vec = await embed_query_async(query)
    stmt = select(Entry.id).where(Entry.body_embedding.is_not(None))
    if project_id is not None:
        stmt = stmt.join(Subtopic, Entry.subtopic_id == Subtopic.id).where(Subtopic.project_id == project_id)
    if subtopic_ids is not None:
        stmt = stmt.where(Entry.subtopic_id.in_(subtopic_ids))
    if tag_names:
        stmt = stmt.where(Entry.id.in_(_tag_filter_subquery(tag_names)))
    stmt = stmt.order_by(Entry.body_embedding.cosine_distance(query_vec)).limit(limit)
    return [row[0] for row in (await session.execute(stmt)).all()]


async def _relation_candidates(
    session: AsyncSession, seed_ids: list, project_id, subtopic_ids, tag_names, limit: int
) -> list:
    """Direct (single-hop only) neighbors of whatever fulltext/vector search already
    surfaced -- lets an entry with no textual/semantic match of its own still surface because
    it's linked to something that did match. Neighbors are ranked by how many seed entries
    point at them. Scoped to the same project/subtopic (and, if given, tags) as the rest of
    the search, so a relation can't leak an entry across project boundaries or past a tag
    filter the caller explicitly asked for."""
    if not seed_ids:
        return []
    seed_set = set(seed_ids)
    rows = (
        await session.execute(
            select(Relation.from_entry_id, Relation.to_entry_id).where(
                or_(Relation.from_entry_id.in_(seed_ids), Relation.to_entry_id.in_(seed_ids))
            )
        )
    ).all()

    counts: dict = {}
    for from_id, to_id in rows:
        for endpoint, other in ((from_id, to_id), (to_id, from_id)):
            if endpoint in seed_set and other not in seed_set:
                counts[other] = counts.get(other, 0) + 1
    if not counts:
        return []

    scope_stmt = select(Entry.id).where(Entry.id.in_(counts.keys()))
    if project_id is not None:
        scope_stmt = scope_stmt.join(Subtopic, Entry.subtopic_id == Subtopic.id).where(Subtopic.project_id == project_id)
    if subtopic_ids is not None:
        scope_stmt = scope_stmt.where(Entry.subtopic_id.in_(subtopic_ids))
    if tag_names:
        scope_stmt = scope_stmt.where(Entry.id.in_(_tag_filter_subquery(tag_names)))
    allowed = {row[0] for row in (await session.execute(scope_stmt)).all()}

    ranked = sorted((eid for eid in counts if eid in allowed), key=lambda eid: counts[eid], reverse=True)
    return ranked[:limit]


async def _tag_filtered_ids(session: AsyncSession, tag_names: list[str], project_id, subtopic_ids, limit: int) -> list:
    """Pure tag browse -- no query text at all, so there's no text/vector rank to sort by;
    most-recently-updated first instead."""
    stmt = select(Entry.id).where(Entry.id.in_(_tag_filter_subquery(tag_names)))
    if project_id is not None:
        stmt = stmt.join(Subtopic, Entry.subtopic_id == Subtopic.id).where(Subtopic.project_id == project_id)
    if subtopic_ids is not None:
        stmt = stmt.where(Entry.subtopic_id.in_(subtopic_ids))
    stmt = stmt.order_by(Entry.updated_at.desc()).limit(limit)
    return [row[0] for row in (await session.execute(stmt)).all()]


async def search(
    session: AsyncSession,
    *,
    query: str = "",
    project_slug: str | None = None,
    subtopic_path: str | None = None,
    tags: list[str] | None = None,
    limit: int = 20,
) -> list[Entry]:
    project_id = None
    subtopic_ids = None
    if project_slug:
        project = await get_project_by_slug(session, project_slug)
        project_id = project.id
        if subtopic_path:
            subtopic = await find_subtopic_by_path(session, project, subtopic_path)
            all_subtopics = await get_all_subtopics_for_project(session, project)
            subtopic_ids = collect_descendant_ids(all_subtopics, subtopic.id)

    # OR semantics across multiple tags -- simplest and most forgiving; normalized the same
    # way _set_tags() already normalizes tags on write (strip + lowercase).
    normalized_tags = [t.strip().lower() for t in (tags or []) if t.strip()] or None
    query = query.strip()

    if not query:
        if not normalized_tags:
            return []
        top_ids = await _tag_filtered_ids(session, normalized_tags, project_id, subtopic_ids, limit)
    else:
        fulltext_ids = await _fulltext_candidates(
            session, query, project_id, subtopic_ids, normalized_tags, limit=limit * 2
        )
        vector_ids = await _vector_candidates(
            session, query, project_id, subtopic_ids, normalized_tags, limit=limit * 2
        )
        relation_ids = await _relation_candidates(
            session, [*fulltext_ids, *vector_ids], project_id, subtopic_ids, normalized_tags, limit=limit * 2
        )
        fused = _rrf_merge([fulltext_ids, vector_ids, relation_ids])
        top_ids = [item_id for item_id, _ in fused[:limit]]

    if not top_ids:
        return []

    rows = (
        (await session.execute(select(Entry).options(selectinload(Entry.tags)).where(Entry.id.in_(top_ids))))
        .scalars()
        .all()
    )
    by_id = {e.id: e for e in rows}
    return [by_id[i] for i in top_ids if i in by_id]
