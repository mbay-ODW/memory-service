"""End-to-end web UI tests via ASGI transport (not FastAPI's TestClient): TestClient runs the
app in its own separate thread/event loop, which conflicts with the process-wide DB engine
singleton once it's already bound to pytest's session-scoped loop by other tests (asyncpg
connections are loop-bound) -- httpx.AsyncClient + ASGITransport shares the current loop
instead. Web routes don't need app.main's lifespan (that only starts the MCP session
manager), so it's fine to skip it here.
"""

import re

import httpx
import pytest
from sqlalchemy import select

from app.db.base import get_session_factory
from app.db.models import Project
from app.main import app

pytestmark = pytest.mark.usefixtures("git_repo_path")


@pytest.fixture(autouse=True)
async def web_project(_prepare_database):
    session_factory = get_session_factory()
    async with session_factory() as session:
        project = (
            await session.execute(select(Project).where(Project.slug == "webtest"))
        ).scalar_one_or_none()
        if project is None:
            session.add(Project(slug="webtest", name="Web Test Project", sensitivity_level="mittel"))
            await session.commit()


@pytest.fixture
async def web_client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def test_full_web_ui_flow(web_client):
    resp = await web_client.get("/projects/webtest")
    assert resp.status_code == 200

    resp = await web_client.post(
        "/entries/new",
        data={
            "project": "webtest",
            "subtopic": "kunde-x/vorgang-1",
            "title": "Web UI Flow Eintrag",
            "body_markdown": "# Hallo\n\nEin **Test**.",
            "tags": "web, test",
            "follow_up_status": "offen",
        },
    )
    assert resp.status_code == 303
    entry_url = resp.headers["location"]

    resp = await web_client.get(entry_url)
    assert resp.status_code == 200
    assert "Web UI Flow Eintrag" in resp.text
    assert "<strong>Test</strong>" in resp.text

    entry_id = entry_url.rsplit("/", 1)[-1]

    resp = await web_client.get(f"/entries/{entry_id}/edit")
    assert resp.status_code == 200

    resp = await web_client.post(
        f"/entries/{entry_id}/edit",
        data={
            "title": "Web UI Flow Eintrag Umbenannt",
            "body_markdown": "# Hallo\n\nGeaendert.",
            "tags": "web",
            "follow_up_status": "wartet",
        },
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == entry_url  # same entry id despite the title change

    resp = await web_client.get(f"/entries/{entry_id}/history")
    assert resp.status_code == 200
    version_ids = re.findall(r"/history/([0-9a-f-]{36})/diff", resp.text)
    assert len(version_ids) == 2

    resp = await web_client.get(f"/entries/{entry_id}/history/{version_ids[0]}/diff")
    assert resp.status_code == 200
    assert "diff" in resp.text.lower()

    resp = await web_client.get("/search", params={"q": "Umbenannt"})
    assert resp.status_code == 200
    assert "Web UI Flow Eintrag Umbenannt" in resp.text

    resp = await web_client.get("/")
    assert resp.status_code == 200
    assert "Web UI Flow Eintrag Umbenannt" in resp.text  # shows up in the open-items queue


async def test_edit_title_collision_shows_error_not_500(web_client):
    await web_client.post(
        "/entries/new",
        data={
            "project": "webtest",
            "subtopic": "collision-topic",
            "title": "Erster Eintrag",
            "body_markdown": "a",
        },
    )
    resp = await web_client.post(
        "/entries/new",
        data={
            "project": "webtest",
            "subtopic": "collision-topic",
            "title": "Zweiter Eintrag",
            "body_markdown": "b",
        },
    )
    second_id = resp.headers["location"].rsplit("/", 1)[-1]

    resp = await web_client.post(
        f"/entries/{second_id}/edit",
        data={"title": "Erster Eintrag", "body_markdown": "b"},
    )
    assert resp.status_code == 409
    assert "bereits" in resp.text or "already" in resp.text.lower() or "existiert" in resp.text.lower()


async def test_unknown_project_returns_404(web_client):
    resp = await web_client.get("/projects/does-not-exist")
    assert resp.status_code == 404
