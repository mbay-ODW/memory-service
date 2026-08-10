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
    entry_id = created.data["id"]

    got = await mcp_client.call_tool("memory_get", {"project": "mcptest", "subtopic": "topic-x"})
    assert any(e["id"] == entry_id for e in got.data)

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
