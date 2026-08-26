"""Direct, typed links between entries ("this is the same client as that other entry, filed
twice") -- Postgres-only, no git commit involved. See the `Relation` model docstring in
app/db/models.py for why this isn't mirrored into git the way tags/sources are.
"""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RELATION_TYPES, Entry, Relation
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
