"""The 6 MCP tools. Every one is a thin wrapper over app/services/* -- no DB or git access
happens here directly. Each tool opens its own session (MCP tool calls aren't FastAPI routes,
so there's no request-scoped Depends() session injection)."""

import uuid

from app.core.security import current_actor
from app.db.base import get_session_factory
from app.mcp.server import mcp
from app.services import entries as entries_service
from app.services import search as search_service
from app.services import sources as sources_service


def _entry_summary(entry) -> dict:
    return {
        "id": str(entry.id),
        "title": entry.title,
        "slug": entry.slug,
        "status": entry.status,
        "follow_up_status": entry.follow_up_status,
        "tags": [t.name for t in entry.tags],
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        "updated_by": entry.updated_by,
    }


def _entry_detail(entry) -> dict:
    return {**_entry_summary(entry), "body_markdown": entry.body_markdown}


def _version_summary(version) -> dict:
    return {
        "id": str(version.id),
        "git_commit_hash": version.git_commit_hash,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "created_by": version.created_by,
    }


@mcp.tool()
async def memory_search(
    query: str, project: str | None = None, subtopic: str | None = None, limit: int = 10
) -> list[dict]:
    """Full-text + semantic search across memory entries. Scope to a project (and optionally
    a subtopic within it) to narrow results; omit both to search everything."""
    async with get_session_factory()() as session:
        results = await search_service.search(
            session, query=query, project_slug=project, subtopic_path=subtopic, limit=limit
        )
        return [_entry_summary(e) for e in results]


@mcp.tool()
async def memory_get(project: str, subtopic: str | None = None) -> list[dict]:
    """Current ('aktuell') entries for a project, or for one subtopic (and its nested
    children) within it. Call this at the start of a task before researching or answering."""
    async with get_session_factory()() as session:
        results = await entries_service.get_entries(session, project_slug=project, subtopic_path=subtopic)
        return [_entry_detail(e) for e in results]


@mcp.tool()
async def memory_upsert(
    project: str,
    subtopic: str,
    title: str,
    body_markdown: str,
    sources: list[dict] | None = None,
    tags: list[str] | None = None,
    follow_up_status: str | None = None,
) -> dict:
    """Create or update a memory entry, identified by (subtopic, title) within a project.
    Missing subtopic levels (e.g. 'kunde-mueller/vorgang-2026-08') are auto-created. Writes go
    to Postgres AND the internal git history in the same call. `sources` is a list of
    {"type": "mail"|"whatsapp"|"signal"|"paperless"|"nextcloud"|"hero", "ref": "..."}, used for
    provenance and by memory_check_sources for daily-sync dedup. `follow_up_status` is one of
    null (don't change), "offen", "wartet", or "none" (clear it)."""
    source_pairs = [(s["type"], s["ref"]) for s in (sources or [])]
    async with get_session_factory()() as session:
        actor = current_actor()
        entry = await entries_service.upsert_entry(
            session,
            project_slug=project,
            subtopic_path=subtopic,
            title=title,
            body_markdown=body_markdown,
            actor=actor,
            sources=source_pairs or None,
            tags=tags,
            follow_up_status=follow_up_status,
        )
        return _entry_detail(entry)


@mcp.tool()
async def memory_list_open(project: str | None = None) -> list[dict]:
    """Entries flagged as needing attention (follow_up_status = 'offen' or 'wartet'),
    optionally scoped to one project."""
    async with get_session_factory()() as session:
        results = await entries_service.list_open(session, project_slug=project)
        return [_entry_summary(e) for e in results]


@mcp.tool()
async def memory_history(entry_id: str, limit: int = 20) -> list[dict]:
    """Version history (most recent first) for one entry, including the git commit hash of
    each version."""
    try:
        entry_uuid = uuid.UUID(entry_id)
    except ValueError as exc:
        raise ValueError(f"invalid entry_id (must be a UUID): {entry_id!r}") from exc
    async with get_session_factory()() as session:
        versions = await entries_service.get_history(session, entry_uuid, limit=limit)
        return [_version_summary(v) for v in versions]


@mcp.tool()
async def memory_check_sources(source_type: str, source_refs: list[str]) -> dict[str, bool]:
    """Batch dedup check for daily sync tasks: which of these source refs (e.g. mail message
    ids) are already logged in memory? Only detects refs seen before -- it does NOT detect
    edits to an already-logged item under the same ref."""
    async with get_session_factory()() as session:
        return await sources_service.check_sources(session, source_type, source_refs)
