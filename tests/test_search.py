import pytest

from app.db.models import Project
from app.services import entries as entries_service
from app.services import search as search_service


@pytest.fixture
async def sample_project(db_session):
    project = Project(slug="searchproj", name="Search Project", sensitivity_level="niedrig")
    db_session.add(project)
    await db_session.flush()
    return project


async def test_search_fulltext_hit(db_session, git_repo_path, sample_project):
    await entries_service.upsert_entry(
        db_session,
        project_slug="searchproj",
        subtopic_path="topic",
        title="Heizungstausch Wärmepumpe",
        body_markdown="Der Kunde plant den Einbau einer Wärmepumpe im Herbst.",
        actor="claude",
    )
    await entries_service.upsert_entry(
        db_session,
        project_slug="searchproj",
        subtopic_path="topic",
        title="Steuererklärung 2026",
        body_markdown="Unterlagen für die Einkommensteuererklärung sammeln.",
        actor="claude",
    )

    results = await search_service.search(db_session, query="Wärmepumpe", project_slug="searchproj")
    assert results
    assert results[0].title == "Heizungstausch Wärmepumpe"


async def test_search_semantic_hit(db_session, git_repo_path, sample_project):
    await entries_service.upsert_entry(
        db_session,
        project_slug="searchproj",
        subtopic_path="topic",
        title="Fenstertausch Angebot",
        body_markdown="Neue dreifachverglaste Fenster für das Obergeschoss angefragt.",
        actor="claude",
    )
    await entries_service.upsert_entry(
        db_session,
        project_slug="searchproj",
        subtopic_path="topic",
        title="Steuererklärung 2026",
        body_markdown="Unterlagen für die Einkommensteuererklärung sammeln.",
        actor="claude",
    )

    # semantically related query that deliberately avoids the exact words "Fenster"/"Angebot"
    results = await search_service.search(
        db_session, query="Wärmedämmung der Glasflächen im Obergeschoss", project_slug="searchproj"
    )
    assert results
    assert results[0].title == "Fenstertausch Angebot"


async def test_check_sources_dedup(db_session, git_repo_path, sample_project):
    from app.services import sources as sources_service

    entry = await entries_service.upsert_entry(
        db_session,
        project_slug="searchproj",
        subtopic_path="topic",
        title="Mit Quelle",
        body_markdown="...",
        actor="claude",
        sources=[("mail", "msg-abc")],
    )
    assert entry.id

    result = await sources_service.check_sources(db_session, "mail", ["msg-abc", "msg-unknown"])
    assert result == {"msg-abc": True, "msg-unknown": False}
