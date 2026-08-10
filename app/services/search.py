"""Full-text (Postgres tsvector/GIN) + semantic (pgvector/HNSW) search, merged with
Reciprocal Rank Fusion so neither signal has to "win" outright -- an exact term hit and a
semantic near-miss both surface, which plain vector-only or fulltext-only search would miss."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Entry, Subtopic
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


async def _fulltext_candidates(session: AsyncSession, query: str, project_id, subtopic_ids, limit: int) -> list:
    ts_query = func.websearch_to_tsquery("german", query)
    stmt = select(Entry.id).where(Entry.body_tsvector.op("@@")(ts_query))
    if project_id is not None:
        stmt = stmt.join(Subtopic, Entry.subtopic_id == Subtopic.id).where(Subtopic.project_id == project_id)
    if subtopic_ids is not None:
        stmt = stmt.where(Entry.subtopic_id.in_(subtopic_ids))
    stmt = stmt.order_by(func.ts_rank(Entry.body_tsvector, ts_query).desc()).limit(limit)
    return [row[0] for row in (await session.execute(stmt)).all()]


async def _vector_candidates(session: AsyncSession, query: str, project_id, subtopic_ids, limit: int) -> list:
    query_vec = await embed_query_async(query)
    stmt = select(Entry.id).where(Entry.body_embedding.is_not(None))
    if project_id is not None:
        stmt = stmt.join(Subtopic, Entry.subtopic_id == Subtopic.id).where(Subtopic.project_id == project_id)
    if subtopic_ids is not None:
        stmt = stmt.where(Entry.subtopic_id.in_(subtopic_ids))
    stmt = stmt.order_by(Entry.body_embedding.cosine_distance(query_vec)).limit(limit)
    return [row[0] for row in (await session.execute(stmt)).all()]


async def search(
    session: AsyncSession,
    *,
    query: str,
    project_slug: str | None = None,
    subtopic_path: str | None = None,
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

    fulltext_ids = await _fulltext_candidates(session, query, project_id, subtopic_ids, limit=limit * 2)
    vector_ids = await _vector_candidates(session, query, project_id, subtopic_ids, limit=limit * 2)
    fused = _rrf_merge([fulltext_ids, vector_ids])
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
