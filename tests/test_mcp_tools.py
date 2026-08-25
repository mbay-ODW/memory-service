import pytest
from fastmcp import Client
from sqlalchemy import select

from app.db.base import get_session_factory
from app.db.models import Project
from app.mcp.server import mcp

pytestmark = pytest.mark.usefixtures("git_repo_path")


@pytest.fixture(autouse=True)
async def mcp_project(_prepare_database):
    """MCP tools open their own sessions (not the rollback-per-test db_session fixture), so
    this commits for real -- get-or-create keeps repeated local test runs idempotent."""
    session_factory = get_session_factory()
    async with session_factory() as session:
        project = (
            await session.execute(select(Project).where(Project.slug == "mcptest"))
        ).scalar_one_or_none()
        if project is None:
            session.add(Project(slug="mcptest", name="MCP Test Project", sensitivity_level="niedrig"))
            await session.commit()


@pytest.fixture
async def mcp_client():
    async with Client(mcp) as client:
        yield client


async def test_memory_upsert_get_history_and_check_sources(mcp_client):
    created = await mcp_client.call_tool(
        "memory_upsert",
        {
            "project": "mcptest",
            "subtopic": "topic-x",
            "title": "MCP Test Entry",
            "body_markdown": "Inhalt ueber den MCP-Test.",
            "sources": [{"type": "mail", "ref": "mcp-msg-1"}],
            "tags": ["mcp"],
        },
    )
    assert created.data["title"] == "MCP Test Entry"
    assert created.data["tags"] == ["mcp"]
    assert created.data["subtopic"] == "topic-x"
    entry_id = created.data["id"]

    got = await mcp_client.call_tool("memory_get", {"project": "mcptest", "subtopic": "topic-x"})
    match = next(e for e in got.data if e["id"] == entry_id)
    assert match["subtopic"] == "topic-x"

    history = await mcp_client.call_tool("memory_history", {"entry_id": entry_id})
    assert len(history.data) == 1
    assert history.data[0]["git_commit_hash"]

    dedup = await mcp_client.call_tool(
        "memory_check_sources", {"source_type": "mail", "source_refs": ["mcp-msg-1", "mcp-msg-unknown"]}
    )
    assert dedup.data == {"mcp-msg-1": True, "mcp-msg-unknown": False}


async def test_memory_upsert_is_idempotent_by_title(mcp_client):
    first = await mcp_client.call_tool(
        "memory_upsert",
        {"project": "mcptest", "subtopic": "topic-x", "title": "Wiederholter Eintrag", "body_markdown": "v1"},
    )
    second = await mcp_client.call_tool(
        "memory_upsert",
        {"project": "mcptest", "subtopic": "topic-x", "title": "Wiederholter Eintrag", "body_markdown": "v2"},
    )
    assert first.data["id"] == second.data["id"]
    assert second.data["body_markdown"] == "v2"


async def test_memory_search(mcp_client):
    await mcp_client.call_tool(
        "memory_upsert",
        {
            "project": "mcptest",
            "subtopic": "topic-x",
            "title": "Suchbarer Eintrag Waermepumpe",
            "body_markdown": "Ein Eintrag zur Waermepumpe fuer die Suche.",
        },
    )
    results = await mcp_client.call_tool("memory_search", {"query": "Waermepumpe", "project": "mcptest"})
    assert any("Waermepumpe" in e["title"] for e in results.data)


async def test_memory_list_open(mcp_client):
    await mcp_client.call_tool(
        "memory_upsert",
        {
            "project": "mcptest",
            "subtopic": "topic-x",
            "title": "Offener MCP Punkt",
            "body_markdown": "Wartet auf Rueckmeldung.",
            "follow_up_status": "offen",
        },
    )
    open_items = await mcp_client.call_tool("memory_list_open", {"project": "mcptest"})
    assert any(e["title"] == "Offener MCP Punkt" for e in open_items.data)


async def test_memory_create_project_then_upsert_into_it(mcp_client):
    created = await mcp_client.call_tool(
        "memory_create_project",
        {"name": "MCP Neues Projekt", "sensitivity_level": "niedrig", "description": "Via MCP erstellt."},
    )
    assert created.data["slug"] == "mcp-neues-projekt"
    assert created.data["description"] == "Via MCP erstellt."

    # the whole point: memory_upsert must work against a project that didn't exist a moment ago
    entry = await mcp_client.call_tool(
        "memory_upsert",
        {
            "project": "mcp-neues-projekt",
            "subtopic": "erstes-thema",
            "title": "Erster Eintrag",
            "body_markdown": "...",
        },
    )
    assert entry.data["title"] == "Erster Eintrag"


async def test_memory_create_project_rejects_duplicate(mcp_client):
    await mcp_client.call_tool(
        "memory_create_project", {"name": "Doppeltes MCP Projekt", "sensitivity_level": "niedrig"}
    )
    with pytest.raises(Exception):
        await mcp_client.call_tool(
            "memory_create_project", {"name": "Doppeltes MCP Projekt", "sensitivity_level": "niedrig"}
        )


async def test_memory_delete_entry(mcp_client):
    created = await mcp_client.call_tool(
        "memory_upsert",
        {
            "project": "mcptest",
            "subtopic": "topic-x",
            "title": "Zu Loeschender MCP Eintrag",
            "body_markdown": "...",
        },
    )
    entry_id = created.data["id"]

    deleted = await mcp_client.call_tool("memory_delete_entry", {"entry_id": entry_id})
    assert deleted.data == {"deleted": True, "id": entry_id}

    with pytest.raises(Exception):
        await mcp_client.call_tool("memory_history", {"entry_id": entry_id})


async def test_memory_delete_entry_rejects_invalid_uuid(mcp_client):
    with pytest.raises(Exception):
        await mcp_client.call_tool("memory_delete_entry", {"entry_id": "not-a-uuid"})


async def test_updating_via_the_returned_subtopic_does_not_duplicate(mcp_client):
    """Regression test for the exact bug reported live: memory_get/memory_search didn't expose
    `subtopic`, so a caller updating an existing entry had to guess it -- and a wrong guess
    silently created a duplicate under the guessed path instead of erroring."""
    created = await mcp_client.call_tool(
        "memory_upsert",
        {
            "project": "mcptest",
            "subtopic": "kunde-mueller/vorgang-2026-08",
            "title": "Bug-Report Regressionstest",
            "body_markdown": "v1",
        },
    )
    entry_id = created.data["id"]

    # a caller that READS the entry and passes its exact subtopic back must update in place
    found = await mcp_client.call_tool("memory_get", {"project": "mcptest"})
    match = next(e for e in found.data if e["id"] == entry_id)
    assert match["subtopic"] == "kunde-mueller/vorgang-2026-08"

    updated = await mcp_client.call_tool(
        "memory_upsert",
        {
            "project": "mcptest",
            "subtopic": match["subtopic"],
            "title": "Bug-Report Regressionstest",
            "body_markdown": "v2",
        },
    )
    assert updated.data["id"] == entry_id  # same entry, not a new one
    assert updated.data["body_markdown"] == "v2"

    history = await mcp_client.call_tool("memory_history", {"entry_id": entry_id})
    assert len(history.data) == 2  # create + update, one entry throughout

    # by contrast, GUESSING a wrong/empty subtopic is exactly what created duplicates in
    # production -- confirm that still lands somewhere else (documents the actual footgun,
    # not a bug in this fix: memory_upsert can't know the caller "meant" the same entry).
    guessed = await mcp_client.call_tool(
        "memory_upsert",
        {
            "project": "mcptest",
            "subtopic": "",
            "title": "Bug-Report Regressionstest",
            "body_markdown": "guessed wrong",
        },
    )
    assert guessed.data["id"] != entry_id


async def _create_entry(mcp_client, title, subtopic="topic-x"):
    created = await mcp_client.call_tool(
        "memory_upsert",
        {"project": "mcptest", "subtopic": subtopic, "title": title, "body_markdown": "..."},
    )
    return created.data["id"]


async def test_memory_link_entries_and_get_related_round_trip(mcp_client):
    a_id = await _create_entry(mcp_client, "Relations A")
    b_id = await _create_entry(mcp_client, "Relations B", subtopic="topic-y")

    linked = await mcp_client.call_tool(
        "memory_link_entries",
        {"from_entry_id": a_id, "to_entry_id": b_id, "relation_type": "same_as", "note": "gleicher Kunde"},
    )
    assert linked.data == {
        "from_entry_id": a_id,
        "to_entry_id": b_id,
        "relation_type": "same_as",
        "note": "gleicher Kunde",
    }

    related_from_a = await mcp_client.call_tool("memory_get_related", {"entry_id": a_id})
    assert len(related_from_a.data) == 1
    assert related_from_a.data[0]["entry_id"] == b_id
    assert related_from_a.data[0]["direction"] == "outgoing"
    assert related_from_a.data[0]["subtopic"] == "topic-y"

    related_from_b = await mcp_client.call_tool("memory_get_related", {"entry_id": b_id})
    assert len(related_from_b.data) == 1
    assert related_from_b.data[0]["entry_id"] == a_id
    assert related_from_b.data[0]["direction"] == "incoming"


async def test_memory_unlink_entries(mcp_client):
    a_id = await _create_entry(mcp_client, "Unlink A")
    b_id = await _create_entry(mcp_client, "Unlink B")
    await mcp_client.call_tool(
        "memory_link_entries", {"from_entry_id": a_id, "to_entry_id": b_id, "relation_type": "mentions"}
    )

    unlinked = await mcp_client.call_tool(
        "memory_unlink_entries", {"from_entry_id": a_id, "to_entry_id": b_id, "relation_type": "mentions"}
    )
    assert unlinked.data == {"unlinked": True}

    unlinked_again = await mcp_client.call_tool(
        "memory_unlink_entries", {"from_entry_id": a_id, "to_entry_id": b_id, "relation_type": "mentions"}
    )
    assert unlinked_again.data == {"unlinked": False}

    related = await mcp_client.call_tool("memory_get_related", {"entry_id": a_id})
    assert related.data == []


async def test_memory_link_entries_rejects_invalid_relation_type(mcp_client):
    a_id = await _create_entry(mcp_client, "Invalid Type A")
    b_id = await _create_entry(mcp_client, "Invalid Type B")
    with pytest.raises(Exception):
        await mcp_client.call_tool(
            "memory_link_entries", {"from_entry_id": a_id, "to_entry_id": b_id, "relation_type": "not-a-type"}
        )


async def test_memory_link_entries_rejects_self_link(mcp_client):
    a_id = await _create_entry(mcp_client, "Self Link A")
    with pytest.raises(Exception):
        await mcp_client.call_tool(
            "memory_link_entries", {"from_entry_id": a_id, "to_entry_id": a_id, "relation_type": "related_to"}
        )
