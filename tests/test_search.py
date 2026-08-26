from unittest.mock import patch

import pytest

from app.db.models import Project
from app.services import entries as entries_service
from app.services import relations as relations_service
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


async def test_search_surfaces_directly_linked_entry_via_relation_boost(db_session, git_repo_path, sample_project):
    """Isolates the new third RRF list: fulltext/vector are mocked to return only the direct
    hit, so any additional entry in the results can only have come from `_relation_candidates`.
    (Real fulltext/vector recall is already covered by the two tests above -- mocking here
    avoids the test being at the mercy of the actual embedding model's ranking behavior, which
    has no similarity threshold and would otherwise make "not a vector match" hard to construct
    deterministically at unit-test scale.)"""
    matched = await entries_service.upsert_entry(
        db_session, project_slug="searchproj", subtopic_path="topic", title="Matched", body_markdown="...", actor="c"
    )
    linked = await entries_service.upsert_entry(
        db_session, project_slug="searchproj", subtopic_path="topic", title="Linked", body_markdown="...", actor="c"
    )
    await relations_service.link_entries(
        db_session, from_entry_id=matched.id, to_entry_id=linked.id, relation_type="related_to", actor="c"
    )

    with (
        patch("app.services.search._fulltext_candidates", return_value=[matched.id]),
        patch("app.services.search._vector_candidates", return_value=[matched.id]),
    ):
        results = await search_service.search(db_session, query="irrelevant", project_slug="searchproj", limit=5)

    assert {r.id for r in results} == {matched.id, linked.id}


async def test_search_relation_boost_respects_project_scope(db_session, git_repo_path, sample_project):
    other_project = Project(slug="otherscopeproj", name="Other Scope Project", sensitivity_level="niedrig")
    db_session.add(other_project)
    await db_session.flush()

    matched = await entries_service.upsert_entry(
        db_session, project_slug="searchproj", subtopic_path="topic", title="Scope Matched", body_markdown="...", actor="c"
    )
    other = await entries_service.upsert_entry(
        db_session, project_slug="otherscopeproj", subtopic_path="topic", title="Scope Other", body_markdown="...", actor="c"
    )
    await relations_service.link_entries(
        db_session, from_entry_id=matched.id, to_entry_id=other.id, relation_type="related_to", actor="c"
    )

    with (
        patch("app.services.search._fulltext_candidates", return_value=[matched.id]),
        patch("app.services.search._vector_candidates", return_value=[matched.id]),
    ):
        results = await search_service.search(db_session, query="irrelevant", project_slug="searchproj", limit=5)

    assert {r.id for r in results} == {matched.id}  # other-project entry must not leak in


async def test_search_or_semantics_across_tags(db_session, git_repo_path, sample_project):
    red = await entries_service.upsert_entry(
        db_session,
        project_slug="searchproj",
        subtopic_path="topic",
        title="Rot",
        body_markdown="...",
        actor="c",
        tags=["rot"],
    )
    blue = await entries_service.upsert_entry(
        db_session,
        project_slug="searchproj",
        subtopic_path="topic",
        title="Blau",
        body_markdown="...",
        actor="c",
        tags=["blau"],
    )
    await entries_service.upsert_entry(
        db_session,
        project_slug="searchproj",
        subtopic_path="topic",
        title="Gruen",
        body_markdown="...",
        actor="c",
        tags=["gruen"],
    )

    results = await search_service.search(db_session, project_slug="searchproj", tags=["rot", "blau"])
    assert {r.id for r in results} == {red.id, blue.id}


async def test_search_pure_tag_browse_no_query(db_session, git_repo_path, sample_project):
    entry = await entries_service.upsert_entry(
        db_session,
        project_slug="searchproj",
        subtopic_path="topic",
        title="Nur Tag",
        body_markdown="Beliebiger Inhalt ohne Bezug zur Suchanfrage.",
        actor="c",
        tags=["besonders"],
    )
    results = await search_service.search(db_session, query="", project_slug="searchproj", tags=["besonders"])
    assert [r.id for r in results] == [entry.id]


async def test_search_empty_query_and_no_tags_returns_empty(db_session, git_repo_path, sample_project):
    results = await search_service.search(db_session, query="", project_slug="searchproj")
    assert results == []
