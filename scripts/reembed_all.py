"""Recompute body_embedding for every entry. Run this after changing EMBEDDING_MODEL_NAME --
a model swap invalidates every previously stored vector. Doesn't touch body_markdown or git
history, only the search index, so it's safe to re-run at any time.
"""

import asyncio

from sqlalchemy import select

from app.db.base import get_session_factory
from app.db.models import Entry
from app.services.embeddings import embed_passage_async


async def reembed_all(batch_size: int = 50) -> None:
    session_factory = get_session_factory()
    async with session_factory() as session:
        entry_ids = list((await session.execute(select(Entry.id))).scalars())

    print(f"re-embedding {len(entry_ids)} entries...")
    done = 0
    async with session_factory() as session:
        for entry_id in entry_ids:
            entry = await session.get(Entry, entry_id)
            entry.body_embedding = await embed_passage_async(f"{entry.title}\n\n{entry.body_markdown}")
            done += 1
            if done % batch_size == 0:
                await session.commit()
                print(f"  {done}/{len(entry_ids)}")
        await session.commit()
    print(f"done: {done} entries re-embedded")


if __name__ == "__main__":
    asyncio.run(reembed_all())
