"""The MCP tools. Every one is a thin wrapper over app/services/* -- no DB or git access
happens here directly. Each tool opens its own session (MCP tool calls aren't FastAPI routes,
so there's no request-scoped Depends() session injection)."""

import uuid

from app.core.security import current_actor
from app.db.base import get_session_factory
from app.mcp.server import mcp
from app.services import entries as entries_service
from app.services import projects as projects_service
from app.services import relations as relations_service
from app.services import search as search_service
from app.services import sources as sources_service


def _project_summary(project) -> dict:
    return {
        "id": str(project.id),
        "slug": project.slug,
        "name": project.name,
        "sensitivity_level": project.sensitivity_level,
        "description": project.description,
    }


def _entry_summary(entry, subtopic_path: str) -> dict:
    return {
        "id": str(entry.id),
        "title": entry.title,
        "slug": entry.slug,
        # the exact path to pass back into memory_upsert's `subtopic` param to update THIS
        # entry -- without it, a caller has no way to know where an existing entry actually
        # lives, and a wrong guess doesn't error, it silently creates a duplicate elsewhere.
        "subtopic": subtopic_path,
        "status": entry.status,
        "follow_up_status": entry.follow_up_status,
        "tags": [t.name for t in entry.tags],
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
        "updated_by": entry.updated_by,
    }


def _entry_detail(entry, subtopic_path: str) -> dict:
    return {**_entry_summary(entry, subtopic_path), "body_markdown": entry.body_markdown}


async def _entry_summaries(session, entries: list) -> list[dict]:
    paths = await entries_service.get_subtopic_paths(session, entries)
    return [_entry_summary(e, paths[e.id]) for e in entries]


async def _entry_details(session, entries: list) -> list[dict]:
    paths = await entries_service.get_subtopic_paths(session, entries)
    return [_entry_detail(e, paths[e.id]) for e in entries]


def _version_summary(version) -> dict:
    return {
        "id": str(version.id),
        "git_commit_hash": version.git_commit_hash,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "created_by": version.created_by,
    }


@mcp.tool()
async def memory_search(
    query: str = "",
    project: str | None = None,
    subtopic: str | None = None,
    tags: list[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    """Full-text + semantic search across memory entries. Scope to a project (and optionally
    a subtopic within it) to narrow results; omit both to search everything. `tags` filters to
    entries carrying ANY of the given tags (OR, not AND); `query` can be left empty to browse
    purely by tag, sorted by most recently updated. Each result includes its exact `subtopic`
    path -- pass that back verbatim into memory_upsert's `subtopic` param to update this entry.
    Guessing the subtopic instead doesn't error, it silently creates a duplicate entry under
    the guessed path. If a result turns out to be about the same real-world thing as an entry
    you're about to create (same client under a different title, say), prefer
    memory_link_entries(..., relation_type="same_as") over creating a second entry."""
    async with get_session_factory()() as session:
        results = await search_service.search(
            session, query=query, project_slug=project, subtopic_path=subtopic, tags=tags, limit=limit
        )
        return await _entry_summaries(session, results)


@mcp.tool()
async def memory_get(project: str, subtopic: str | None = None) -> list[dict]:
    """Current ('aktuell') entries for a project, or for one subtopic (and its nested
    children) within it. Call this at the start of a task before researching or answering.
    Each result includes its exact `subtopic` path -- pass that back verbatim into
    memory_upsert's `subtopic` param to update this entry. Guessing the subtopic instead
    doesn't error, it silently creates a duplicate entry under the guessed path."""
    async with get_session_factory()() as session:
        results = await entries_service.get_entries(session, project_slug=project, subtopic_path=subtopic)
        return await _entry_details(session, results)


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
    """Create or update a memory entry, identified by (subtopic, title) within a project. To
    update an entry you already found via memory_get/memory_search, pass its exact `subtopic`
    value from that result -- a different or empty subtopic doesn't error, it creates a
    separate new entry instead of updating the one you meant. Missing subtopic levels (e.g.
    'kunde-mueller/vorgang-2026-08') are auto-created for genuinely new entries; an empty
    subtopic falls back to a generic "allgemein" bucket. Writes go to Postgres AND the internal
    git history in the same call. `sources` is a list of {"type":
    "mail"|"whatsapp"|"signal"|"paperless"|"nextcloud"|"hero", "ref": "..."}, used for
    provenance and by memory_check_sources for daily-sync dedup. `follow_up_status` is one of
    null (don't change), "offen", "wartet", or "none" (clear it). The returned entry's
    `subtopic` field confirms exactly where it landed."""
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
        return (await _entry_details(session, [entry]))[0]


@mcp.tool()
async def memory_list_open(project: str | None = None) -> list[dict]:
    """Entries flagged as needing attention (follow_up_status = 'offen' or 'wartet'),
    optionally scoped to one project."""
    async with get_session_factory()() as session:
        results = await entries_service.list_open(session, project_slug=project)
        return await _entry_summaries(session, results)


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
async def memory_delete_entry(entry_id: str) -> dict:
    """Permanently delete one entry (its DB row and its git file, in one commit). Git history
    remains the recovery path afterward -- there's no soft-delete/archive state yet, so use
    this for entries created by mistake or genuinely no longer wanted, not for "this is
    outdated but still worth a record of" (edit the entry instead)."""
    try:
        entry_uuid = uuid.UUID(entry_id)
    except ValueError as exc:
        raise ValueError(f"invalid entry_id (must be a UUID): {entry_id!r}") from exc
    async with get_session_factory()() as session:
        actor = current_actor()
        await entries_service.delete_entry(session, entry_id=entry_uuid, actor=actor)
        return {"deleted": True, "id": entry_id}


@mcp.tool()
async def memory_check_sources(source_type: str, source_refs: list[str]) -> dict[str, bool]:
    """Batch dedup check for daily sync tasks: which of these source refs (e.g. mail message
    ids) are already logged in memory? Only detects refs seen before -- it does NOT detect
    edits to an already-logged item under the same ref."""
    async with get_session_factory()() as session:
        return await sources_service.check_sources(session, source_type, source_refs)


@mcp.tool()
async def memory_create_project(
    name: str, sensitivity_level: str, description: str | None = None
) -> dict:
    """Create a new project (slug is derived from `name`). `sensitivity_level` must be one of
    "niedrig", "mittel", "hoch". This is the ONLY project-management operation exposed over
    MCP -- renaming and deleting a project are web-UI-only, human-confirmed actions, not
    something to do unprompted mid-task. Use this when a note genuinely doesn't fit any
    existing project (check with memory_search / memory_get first)."""
    async with get_session_factory()() as session:
        project = await projects_service.create_project(
            session, name=name, sensitivity_level=sensitivity_level, description=description
        )
        return _project_summary(project)


def _parse_entry_id(entry_id: str, param_name: str = "entry_id") -> uuid.UUID:
    try:
        return uuid.UUID(entry_id)
    except ValueError as exc:
        raise ValueError(f"invalid {param_name} (must be a UUID): {entry_id!r}") from exc


@mcp.tool()
async def memory_link_entries(
    from_entry_id: str, to_entry_id: str, relation_type: str, note: str | None = None
) -> dict:
    """Record a direct link between two entries: "related_to" (generic), "same_as" (these are
    the same real-world thing, filed under different titles -- the fix for accidental
    duplicates), "follow_up_of" (from_entry follows up on to_entry), "mentions" (from_entry
    references to_entry in passing), "supersedes" (from_entry replaces to_entry -- this
    automatically flips to_entry's status to "veraltet", since the old one is now a documented
    historical fact, not current), "causes"/"fixes"/"contradicts" (causal relationships between
    two documented facts or events). Re-linking the same pair with the same relation_type just
    updates the note, it doesn't create a duplicate link. Both entries must already exist.
    Prefer this over creating a near-duplicate entry whenever memory_search/memory_get turns up
    something that's really the same thing under a different title."""
    async with get_session_factory()() as session:
        actor = current_actor()
        relation = await relations_service.link_entries(
            session,
            from_entry_id=_parse_entry_id(from_entry_id, "from_entry_id"),
            to_entry_id=_parse_entry_id(to_entry_id, "to_entry_id"),
            relation_type=relation_type,
            note=note,
            actor=actor,
        )
        return {
            "from_entry_id": str(relation.from_entry_id),
            "to_entry_id": str(relation.to_entry_id),
            "relation_type": relation.relation_type,
            "note": relation.note,
        }


@mcp.tool()
async def memory_unlink_entries(from_entry_id: str, to_entry_id: str, relation_type: str) -> dict:
    """Remove a specific link between two entries. Safe to call even if the link doesn't exist
    (returns {"unlinked": false} rather than erroring)."""
    async with get_session_factory()() as session:
        removed = await relations_service.unlink_entries(
            session,
            from_entry_id=_parse_entry_id(from_entry_id, "from_entry_id"),
            to_entry_id=_parse_entry_id(to_entry_id, "to_entry_id"),
            relation_type=relation_type,
        )
        return {"unlinked": removed}


@mcp.tool()
async def memory_get_related(entry_id: str) -> list[dict]:
    """Every entry directly linked to this one (via memory_link_entries), in either direction,
    with the relation type, direction ("outgoing" = this entry points at the other one,
    "incoming" = the other one points at this entry), and any note. Does not follow chains of
    links (no transitive/multi-hop traversal) -- only direct links to this one entry."""
    async with get_session_factory()() as session:
        return await relations_service.get_related_entries(session, _parse_entry_id(entry_id))


@mcp.tool()
async def memory_find_similar(
    project: str | None = None, threshold: float = 0.90, auto_link: bool = False
) -> list[dict]:
    """Scan for likely-duplicate entries (near-identical embeddings), optionally scoped to one
    project; omit `project` to scan everything. `threshold` is a cosine-similarity cutoff
    (0-1, higher = stricter) -- tune it if results are too noisy or too sparse. By default
    (auto_link=False) this only REPORTS candidate pairs with a similarity score for you to
    review -- it writes nothing. Even with auto_link=True it only ever creates a `related_to`
    link, never `same_as` -- deciding two entries are genuinely the same real-world thing stays
    a judgment call for you after looking at the actual content, not something this scan
    asserts on its own. Never merges, deletes, or overwrites anything."""
    async with get_session_factory()() as session:
        actor = current_actor()
        return await relations_service.find_similar_entries(
            session, project_slug=project, threshold=threshold, auto_link=auto_link, actor=actor
        )
