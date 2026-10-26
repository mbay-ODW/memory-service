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


# --- recency boost -------------------------------------------------------------------------

from datetime import date, timedelta  # noqa: E402

from app.config import get_settings  # noqa: E402


def test_recency_factor_shape():
    today = date(2026, 10, 26)
    kw = {"weight": 0.2, "half_life_days": 90}
    assert search_service._recency_factor(today, today, **kw) == pytest.approx(1.2)
    assert search_service._recency_factor(today - timedelta(days=90), today, **kw) == pytest.approx(1.1)
    assert search_service._recency_factor(today - timedelta(days=900), today, **kw) == pytest.approx(1.0, abs=1e-3)
    # a scheduled future appointment counts as "today", not as extra-fresh
    assert search_service._recency_factor(today + timedelta(days=30), today, **kw) == pytest.approx(1.2)
    assert search_service._recency_factor(None, today, **kw) == 1.0
    assert search_service._recency_factor(today, today, weight=0.0, half_life_days=90) == 1.0


async def _two_entries(db_session, *, old_occurred, new_occurred):
    old = await entries_service.upsert_entry(
        db_session,
        project_slug="searchproj",
        subtopic_path="recency",
        title="Dach Angebot alt",
        body_markdown="Erstes Angebot fuer das Dach.",
        actor="claude",
        occurred_at=old_occurred,
    )
    new = await entries_service.upsert_entry(
        db_session,
        project_slug="searchproj",
        subtopic_path="recency",
        title="Dach Angebot neu",
        body_markdown="Nachtragsangebot fuer das Dach.",
        actor="claude",
        occurred_at=new_occurred,
    )
    return old, new


async def test_recency_boost_lifts_newer_entry_among_near_equal_matches(db_session, git_repo_path, sample_project):
    old, new = await _two_entries(
        db_session, old_occurred="2025-01-10", new_occurred=date.today().isoformat()
    )
    # both signals rank the old entry one place above the new one -- near-equal matches
    with (
        patch("app.services.search._fulltext_candidates", return_value=[old.id, new.id]),
        patch("app.services.search._vector_candidates", return_value=[old.id, new.id]),
    ):
        results = await search_service.search(db_session, query="Dach", project_slug="searchproj")
    assert [e.id for e in results] == [new.id, old.id]


async def test_recency_boost_does_not_beat_a_clearly_better_match(db_session, git_repo_path, sample_project):
    old, new = await _two_entries(
        db_session, old_occurred="2025-01-10", new_occurred=date.today().isoformat()
    )
    # old is the top hit in BOTH signals; new only shows up in one
    with (
        patch("app.services.search._fulltext_candidates", return_value=[old.id, new.id]),
        patch("app.services.search._vector_candidates", return_value=[old.id]),
    ):
        results = await search_service.search(db_session, query="Dach", project_slug="searchproj")
    assert [e.id for e in results] == [old.id, new.id]


async def test_recency_falls_back_to_updated_at_without_occurred_at(db_session, git_repo_path, sample_project):
    # new has no event date, but was written just now -> updated_at (today) stands in for it
    old, new = await _two_entries(db_session, old_occurred="2025-01-10", new_occurred=None)
    assert new.occurred_at is None
    with (
        patch("app.services.search._fulltext_candidates", return_value=[old.id, new.id]),
        patch("app.services.search._vector_candidates", return_value=[old.id, new.id]),
    ):
        results = await search_service.search(db_session, query="Dach", project_slug="searchproj")
    assert [e.id for e in results] == [new.id, old.id]


async def test_recency_weight_zero_keeps_pure_rrf_order(db_session, git_repo_path, sample_project, monkeypatch):
    old, new = await _two_entries(
        db_session, old_occurred="2025-01-10", new_occurred=date.today().isoformat()
    )
    monkeypatch.setattr(get_settings(), "search_recency_weight", 0.0)
    with (
        patch("app.services.search._fulltext_candidates", return_value=[old.id, new.id]),
        patch("app.services.search._vector_candidates", return_value=[old.id, new.id]),
    ):
        results = await search_service.search(db_session, query="Dach", project_slug="searchproj")
    assert [e.id for e in results] == [old.id, new.id]


async def test_tag_browse_orders_by_event_date(db_session, git_repo_path, sample_project):
    for title, occurred in (("Mitte", "2026-05-01"), ("Neu", "2026-10-01"), ("Alt", "2025-01-01")):
        await entries_service.upsert_entry(
            db_session,
            project_slug="searchproj",
            subtopic_path="recency",
            title=title,
            body_markdown="...",
            actor="claude",
            tags=["chrono"],
            occurred_at=occurred,
        )
    results = await search_service.search(db_session, tags=["chrono"], project_slug="searchproj")
    assert [e.title for e in results] == ["Neu", "Mitte", "Alt"]
