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
    assert 'id="copy-content-button"' in resp.text
    assert "# Hallo\n\nEin **Test**." in resp.text  # raw markdown source, in the hidden textarea

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


async def test_create_edit_delete_project_via_ui(web_client):
    resp = await web_client.post(
        "/projects/new",
        data={"name": "UI Test Projekt", "sensitivity_level": "mittel", "description": "Kontext fuer Tester."},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/projects/ui-test-projekt"

    resp = await web_client.get("/projects/ui-test-projekt")
    assert resp.status_code == 200
    assert "Kontext fuer Tester." in resp.text
    assert "0 Einträge" in resp.text

    await web_client.post(
        "/entries/new",
        data={
            "project": "ui-test-projekt",
            "subtopic": "thema-a",
            "title": "Ein Eintrag",
            "body_markdown": "12345",
        },
    )

    resp = await web_client.get("/projects/ui-test-projekt")
    assert "1 Einträge · 5 Zeichen · 1 Unterthemen" in resp.text

    resp = await web_client.post(
        "/projects/ui-test-projekt/edit",
        data={"name": "UI Test Projekt Umbenannt", "description": "Neuer Kontext."},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/projects/ui-test-projekt"  # slug unchanged

    resp = await web_client.get("/projects/ui-test-projekt")
    assert "UI Test Projekt Umbenannt" in resp.text
    assert "Neuer Kontext." in resp.text

    # wrong confirm -> error, not a 500, project still there
    resp = await web_client.post("/projects/ui-test-projekt/delete", data={"confirm_slug": "falsch"})
    assert resp.status_code == 409
    resp = await web_client.get("/projects/ui-test-projekt")
    assert resp.status_code == 200

    # correct confirm -> deleted
    resp = await web_client.post("/projects/ui-test-projekt/delete", data={"confirm_slug": "ui-test-projekt"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    resp = await web_client.get("/projects/ui-test-projekt")
    assert resp.status_code == 404


async def test_rename_and_delete_subtopic_via_ui(web_client):
    await web_client.post(
        "/projects/new", data={"name": "Subthema UI Test", "sensitivity_level": "niedrig", "description": ""}
    )
    resp = await web_client.post(
        "/entries/new",
        data={
            "project": "subthema-ui-test",
            "subtopic": "mein-thema",
            "title": "Eintrag",
            "body_markdown": "...",
        },
    )
    entry_id = resp.headers["location"].rsplit("/", 1)[-1]
    # pull the subtopic id via the tree's edit link on the project page (not present on the entry page)
    project_resp = await web_client.get("/projects/subthema-ui-test")
    subtopic_id = re.search(r"/subtopics/([0-9a-f-]{36})/edit", project_resp.text).group(1)

    resp = await web_client.post(f"/subtopics/{subtopic_id}/edit", data={"name": "Mein Umbenanntes Thema"})
    assert resp.status_code == 303
    resp = await web_client.get("/projects/subthema-ui-test")
    assert "Mein Umbenanntes Thema" in resp.text

    # wrong confirm -> error
    resp = await web_client.post(f"/subtopics/{subtopic_id}/delete", data={"confirm_slug": "falsch"})
    assert resp.status_code == 409

    # correct confirm -> gone, and its entry cascaded away
    resp = await web_client.post(f"/subtopics/{subtopic_id}/delete", data={"confirm_slug": "mein-thema"})
    assert resp.status_code == 303
    resp = await web_client.get(f"/entries/{entry_id}")
    assert resp.status_code == 404


async def test_entry_page_shows_related_entries(web_client):
    async def create_entry(subtopic, title):
        resp = await web_client.post(
            "/entries/new",
            data={"project": "webtest", "subtopic": subtopic, "title": title, "body_markdown": "..."},
        )
        return resp.headers["location"].rsplit("/", 1)[-1]

    a_id = await create_entry("rel-a", "Verknuepfung A")
    b_id = await create_entry("rel-b", "Verknuepfung B")

    resp = await web_client.get(f"/entries/{a_id}")
    assert "Verknüpfte Einträge" not in resp.text

    from app.db.base import get_session_factory
    from app.services import relations as relations_service

    async with get_session_factory()() as session:
        import uuid

        await relations_service.link_entries(
            session,
            from_entry_id=uuid.UUID(a_id),
            to_entry_id=uuid.UUID(b_id),
            relation_type="same_as",
            note="gleicher Kunde",
            actor="webtest",
        )

    resp = await web_client.get(f"/entries/{a_id}")
    assert resp.status_code == 200
    assert "Verknüpfte Einträge" in resp.text
    assert "Verknuepfung B" in resp.text
    assert "same_as" in resp.text

    resp = await web_client.get(f"/entries/{b_id}")
    assert "Verknuepfung A" in resp.text
    assert "incoming" in resp.text


async def test_project_graph_page_and_data_route(web_client):
    resp = await web_client.post(
        "/entries/new",
        data={"project": "webtest", "subtopic": "graph-topic", "title": "Graph Eintrag", "body_markdown": "..."},
    )
    assert resp.status_code == 303

    page_resp = await web_client.get("/projects/webtest/graph")
    assert page_resp.status_code == 200
    assert "relation-graph" in page_resp.text

    data_resp = await web_client.get("/projects/webtest/graph/data")
    assert data_resp.status_code == 200
    body = data_resp.json()
    assert any(n["title"] == "Graph Eintrag" for n in body["nodes"])
    assert "edges" in body


async def test_search_page_filters_by_tag(web_client):
    await web_client.post(
        "/entries/new",
        data={
            "project": "webtest",
            "subtopic": "tag-search",
            "title": "Getaggter Eintrag",
            "body_markdown": "...",
            "tags": "besonders-wichtig",
        },
    )
    await web_client.post(
        "/entries/new",
        data={"project": "webtest", "subtopic": "tag-search", "title": "Ungetaggt", "body_markdown": "..."},
    )

    resp = await web_client.get("/search", params={"tags": "besonders-wichtig"})
    assert resp.status_code == 200
    assert "Getaggter Eintrag" in resp.text
    assert "Ungetaggt" not in resp.text


async def test_upload_document_prefills_form_and_saves_with_document_source(web_client):
    form_resp = await web_client.get("/entries/upload", params={"project": "webtest"})
    assert form_resp.status_code == 200

    upload_resp = await web_client.post(
        "/entries/upload",
        data={"project": "webtest", "subtopic": "upload-topic"},
        files={"file": ("bericht.txt", b"Hallo aus dem hochgeladenen Dokument.", "text/plain")},
    )
    assert upload_resp.status_code == 200
    assert "Hallo aus dem hochgeladenen Dokument." in upload_resp.text
    assert 'value="bericht"' in upload_resp.text  # guessed title from the filename stem

    save_resp = await web_client.post(
        "/entries/new",
        data={
            "project": "webtest",
            "subtopic": "upload-topic",
            "title": "Bericht",
            "body_markdown": "Hallo aus dem hochgeladenen Dokument.",
            "source_ref": "bericht.txt",
        },
    )
    assert save_resp.status_code == 303
    entry_url = save_resp.headers["location"]

    entry_resp = await web_client.get(entry_url)
    assert "document:bericht.txt" in entry_resp.text


async def test_upload_document_rejects_oversized_file(web_client):
    from app.config import get_settings

    oversized = b"x" * (get_settings().upload_max_bytes + 1)
    resp = await web_client.post(
        "/entries/upload",
        data={"project": "webtest", "subtopic": "upload-topic"},
        files={"file": ("big.txt", oversized, "text/plain")},
    )
    assert resp.status_code == 413


async def test_upload_document_rejects_unsupported_extension(web_client):
    resp = await web_client.post(
        "/entries/upload",
        data={"project": "webtest", "subtopic": "upload-topic"},
        files={"file": ("virus.exe", b"whatever", "application/octet-stream")},
    )
    assert resp.status_code == 422
