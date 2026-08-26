"""Direct, typed links between entries ("this is the same client as that other entry, filed
twice") -- Postgres-only, no git commit involved. See the `Relation` model docstring in
app/db/models.py for why this isn't mirrored into git the way tags/sources are.
"""

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import RELATION_TYPES, Entry, Relation, Subtopic
from app.db.repository import get_project_by_slug
from app.services import entries as entries_service


async def link_entries(
    session: AsyncSession,
    *,
    from_entry_id,
    to_entry_id,
    relation_type: str,
    note: str | None = None,
    actor: str,
) -> Relation:
    """Get-or-create on (from, to, relation_type): re-linking the same pair+type just updates
    `note`/`created_by` rather than erroring or duplicating -- matches the idempotent-write
    style used everywhere else in this codebase (upsert_entry, create_project)."""
    if relation_type not in RELATION_TYPES:
        raise ValueError(f"invalid relation_type: {relation_type!r}")
    if from_entry_id == to_entry_id:
        raise ValueError("an entry can't be related to itself")

    # confirm both ends actually exist -- raises NotFoundError otherwise
    await entries_service.get_entry_by_id(session, from_entry_id)
    to_entry = await entries_service.get_entry_by_id(session, to_entry_id)

    if relation_type == "supersedes":
        # the superseded entry is now a documented historical fact -- flip it once, here, and
        # leave it flipped regardless of what later happens to this relation (unlink_entries
        # does NOT revert this; see its docstring).
        to_entry.status = "veraltet"

    existing = (
        await session.execute(
            select(Relation).where(
                Relation.from_entry_id == from_entry_id,
                Relation.to_entry_id == to_entry_id,
                Relation.relation_type == relation_type,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.note = note
        existing.created_by = actor
        await session.commit()
        return existing

    relation = Relation(
        from_entry_id=from_entry_id,
        to_entry_id=to_entry_id,
        relation_type=relation_type,
        note=note,
        created_by=actor,
    )
    session.add(relation)
    await session.commit()
    return relation


async def unlink_entries(session: AsyncSession, *, from_entry_id, to_entry_id, relation_type: str) -> bool:
    """Idempotent delete: returns whether a row was actually removed, doesn't raise if the
    relation is already absent -- "make sure these aren't linked" is naturally a no-op-tolerant
    operation, unlike memory_delete_entry where the id being missing IS the point of the call.

    Deliberately does NOT revert a "supersedes" link's status flip -- once an entry is marked
    veraltet, that's a real fact about it, not something tied to whether this link still
    exists."""
    existing = (
        await session.execute(
            select(Relation).where(
                Relation.from_entry_id == from_entry_id,
                Relation.to_entry_id == to_entry_id,
                Relation.relation_type == relation_type,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return False
    await session.delete(existing)
    await session.commit()
    return True


async def get_related_entries(session: AsyncSession, entry_id) -> list[dict]:
    """Every relation touching this entry, in either direction, each annotated with the
    *other* entry's title/subtopic and which direction the link points."""
    rows = list(
        (
            await session.execute(
                select(Relation).where(or_(Relation.from_entry_id == entry_id, Relation.to_entry_id == entry_id))
            )
        ).scalars()
    )
    if not rows:
        return []

    other_ids = {r.to_entry_id if r.from_entry_id == entry_id else r.from_entry_id for r in rows}
    other_entries = {
        e.id: e for e in (await session.execute(select(Entry).where(Entry.id.in_(other_ids)))).scalars()
    }
    paths = await entries_service.get_subtopic_paths(session, list(other_entries.values()))

    results = []
    for r in rows:
        direction = "outgoing" if r.from_entry_id == entry_id else "incoming"
        other_id = r.to_entry_id if direction == "outgoing" else r.from_entry_id
        other = other_entries.get(other_id)
        if other is None:
            continue  # FK-enforced, shouldn't happen; defensive only
        results.append(
            {
                "entry_id": str(other.id),
                "title": other.title,
                "subtopic": paths[other.id],
                "relation_type": r.relation_type,
                "direction": direction,
                "note": r.note,
            }
        )
    return results


async def get_project_relation_graph(session: AsyncSession, project_id) -> dict:
    """Nodes = current entries in this project; edges = relations where BOTH endpoints are in
    this project. A relation to another project's entry is simply omitted -- not shown as a
    dangling edge or a ghost node -- keeping the graph project-scoped. No transitive traversal:
    this is exactly the same single-hop relation data as get_related_entries, just for every
    entry in the project at once instead of one entry at a time."""
    entries = (
        await session.execute(
            select(Entry.id, Entry.title, Entry.status)
            .join(Subtopic, Entry.subtopic_id == Subtopic.id)
            .where(Subtopic.project_id == project_id)
        )
    ).all()
    entry_ids = {row[0] for row in entries}
    if not entry_ids:
        return {"nodes": [], "edges": []}

    relations = (
        await session.execute(
            select(Relation.from_entry_id, Relation.to_entry_id, Relation.relation_type, Relation.note).where(
                Relation.from_entry_id.in_(entry_ids), Relation.to_entry_id.in_(entry_ids)
            )
        )
    ).all()

    return {
        "nodes": [{"id": str(eid), "title": title, "status": status} for eid, title, status in entries],
        "edges": [
            {"from": str(f), "to": str(t), "relation_type": rt, "note": note} for f, t, rt, note in relations
        ],
    }


async def find_similar_entries(
    session: AsyncSession,
    *,
    project_slug: str | None = None,
    threshold: float = 0.90,
    limit: int = 50,
    auto_link: bool = False,
    actor: str = "system",
) -> list[dict]:
    """Scans existing embeddings for near-duplicate pairs (cosine similarity >= threshold)
    within a project (or globally if omitted), excluding pairs already linked by any relation
    type in either direction. Never merges or deletes anything: by default (auto_link=False)
    this only reports candidates for review; even with auto_link=True it only ever creates
    `related_to` links -- `same_as` stays a judgment call for the caller, never something this
    scan asserts on its own. One SQL self-join using pgvector's cosine-distance operator
    directly between two entries' embedding columns -- O(n^2), which is fine at realistic
    homelab scale (dozens to low hundreds of entries per project) and not intended for much
    larger corpora."""
    e1 = aliased(Entry)
    e2 = aliased(Entry)
    max_distance = 1 - threshold
    distance = e1.body_embedding.cosine_distance(e2.body_embedding)

    already_linked = select(Relation.id).where(
        or_(
            and_(Relation.from_entry_id == e1.id, Relation.to_entry_id == e2.id),
            and_(Relation.from_entry_id == e2.id, Relation.to_entry_id == e1.id),
        )
    )

    stmt = (
        select(e1.id, e1.title, e2.id, e2.title, distance)
        .select_from(e1)
        .join(e2, e1.id < e2.id)
        .where(
            e1.status == "aktuell",
            e2.status == "aktuell",
            e1.body_embedding.is_not(None),
            e2.body_embedding.is_not(None),
            distance < max_distance,
            ~already_linked.exists(),
        )
    )
    if project_slug:
        project = await get_project_by_slug(session, project_slug)
        s1 = aliased(Subtopic)
        s2 = aliased(Subtopic)
        stmt = (
            stmt.join(s1, e1.subtopic_id == s1.id)
            .join(s2, e2.subtopic_id == s2.id)
            .where(s1.project_id == project.id, s2.project_id == project.id)
        )
    stmt = stmt.order_by(distance).limit(limit)

    rows = (await session.execute(stmt)).all()

    results = []
    for a_id, a_title, b_id, b_title, dist in rows:
        similarity = round(1 - dist, 4)
        entry = {
            "entry_a": {"id": str(a_id), "title": a_title},
            "entry_b": {"id": str(b_id), "title": b_title},
            "similarity": similarity,
            "linked": False,
        }
        if auto_link:
            await link_entries(
                session,
                from_entry_id=a_id,
                to_entry_id=b_id,
                relation_type="related_to",
                note=f"auto-suggested: similarity {similarity}",
                actor=actor,
            )
            entry["linked"] = True
        results.append(entry)
    return results
