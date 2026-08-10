from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Entry, Source


async def check_sources(session: AsyncSession, source_type: str, source_refs: list[str]) -> dict[str, bool]:
    """Batch dedup check for daily dispatch tasks: which of these source_refs are already
    logged? Only catches new refs — an edit to an already-logged item under the same ref is
    a known, documented gap (see app/services/entries.py)."""
    if not source_refs:
        return {}
    rows = await session.execute(
        select(Source.source_ref).where(Source.source_type == source_type, Source.source_ref.in_(source_refs))
    )
    known = {r[0] for r in rows}
    return {ref: (ref in known) for ref in source_refs}


async def register_sources(session: AsyncSession, entry: Entry, sources: list[tuple[str, str]]) -> None:
    """Upsert (source_type, source_ref) rows and point them at this entry. `sources` is a list
    of (source_type, source_ref) pairs — an entry can cite refs from more than one channel.
    If a ref was previously logged against a different entry (the topic got re-classified),
    it's simply re-pointed here rather than duplicated — sources are a dedup index, not a
    strict one-entry-per-ref ledger."""
    for source_type, ref in sources:
        existing = (
            await session.execute(
                select(Source).where(Source.source_type == source_type, Source.source_ref == ref)
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(Source(entry_id=entry.id, source_type=source_type, source_ref=ref))
        else:
            existing.entry_id = entry.id
