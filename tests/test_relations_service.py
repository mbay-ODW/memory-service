import pytest

from app.db.models import Project
from app.services import entries as entries_service
from app.services import relations as relations_service


@pytest.fixture
async def sample_project(db_session):
    project = Project(slug="relproj", name="Relations Test Project", sensitivity_level="niedrig")
    db_session.add(project)
    await db_session.flush()
    return project


@pytest.fixture
async def two_entries(db_session, git_repo_path, sample_project):
    a = await entries_service.upsert_entry(
        db_session, project_slug="relproj", subtopic_path="kunden", title="Kunde A", body_markdown="...", actor="t"
    )
    b = await entries_service.upsert_entry(
        db_session,
        project_slug="relproj",
        subtopic_path="allgemein",
        title="Kunde A Duplikat",
        body_markdown="...",
        actor="t",
    )
    return a, b


async def test_link_entries(db_session, two_entries):
    a, b = two_entries
    relation = await relations_service.link_entries(
        db_session, from_entry_id=b.id, to_entry_id=a.id, relation_type="same_as", note="gleicher Kunde", actor="t"
    )
    assert relation.relation_type == "same_as"
    assert relation.note == "gleicher Kunde"


async def test_link_entries_is_idempotent_updates_note(db_session, two_entries):
    a, b = two_entries
    first = await relations_service.link_entries(
        db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type="related_to", note="v1", actor="t1"
    )
    second = await relations_service.link_entries(
        db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type="related_to", note="v2", actor="t2"
    )
    assert first.id == second.id  # same row, not a duplicate
    assert second.note == "v2"
    assert second.created_by == "t2"


async def test_link_entries_rejects_invalid_type(db_session, two_entries):
    a, b = two_entries
    with pytest.raises(ValueError):
        await relations_service.link_entries(
            db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type="not-a-real-type", actor="t"
        )


async def test_link_entries_rejects_self_link(db_session, two_entries):
    a, _ = two_entries
    with pytest.raises(ValueError):
        await relations_service.link_entries(
            db_session, from_entry_id=a.id, to_entry_id=a.id, relation_type="related_to", actor="t"
        )


async def test_link_entries_rejects_unknown_entry(db_session, two_entries):
    import uuid

    a, _ = two_entries
    with pytest.raises(Exception):
        await relations_service.link_entries(
            db_session, from_entry_id=a.id, to_entry_id=uuid.uuid4(), relation_type="related_to", actor="t"
        )


async def test_unlink_entries_is_idempotent(db_session, two_entries):
    a, b = two_entries
    await relations_service.link_entries(
        db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type="mentions", actor="t"
    )
    removed_first = await relations_service.unlink_entries(
        db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type="mentions"
    )
    assert removed_first is True

    removed_second = await relations_service.unlink_entries(
        db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type="mentions"
    )
    assert removed_second is False  # already gone, no error


async def test_get_related_entries_both_directions(db_session, two_entries):
    a, b = two_entries
    await relations_service.link_entries(
        db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type="same_as", note="dup", actor="t"
    )

    from_a = await relations_service.get_related_entries(db_session, a.id)
    assert len(from_a) == 1
    assert from_a[0]["entry_id"] == str(b.id)
    assert from_a[0]["direction"] == "outgoing"
    assert from_a[0]["subtopic"] == "allgemein"
    assert from_a[0]["note"] == "dup"

    from_b = await relations_service.get_related_entries(db_session, b.id)
    assert len(from_b) == 1
    assert from_b[0]["entry_id"] == str(a.id)
    assert from_b[0]["direction"] == "incoming"
    assert from_b[0]["subtopic"] == "kunden"


async def test_deleting_entry_cascades_relations(db_session, two_entries):
    a, b = two_entries
    await relations_service.link_entries(
        db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type="related_to", actor="t"
    )
    await entries_service.delete_entry(db_session, entry_id=b.id, actor="t")

    remaining = await relations_service.get_related_entries(db_session, a.id)
    assert remaining == []


@pytest.mark.parametrize("relation_type", ["causes", "fixes", "contradicts"])
async def test_link_entries_accepts_causal_types(db_session, two_entries, relation_type):
    a, b = two_entries
    relation = await relations_service.link_entries(
        db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type=relation_type, actor="t"
    )
    assert relation.relation_type == relation_type


async def test_link_entries_still_rejects_unknown_type_after_vocabulary_expansion(db_session, two_entries):
    a, b = two_entries
    with pytest.raises(Exception):
        await relations_service.link_entries(
            db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type="definitely-not-a-type", actor="t"
        )


async def test_link_supersedes_flips_status(db_session, two_entries):
    a, b = two_entries
    assert b.status == "aktuell"
    await relations_service.link_entries(
        db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type="supersedes", actor="t"
    )
    await db_session.refresh(b)
    assert b.status == "veraltet"


async def test_link_supersedes_is_idempotent(db_session, two_entries):
    a, b = two_entries
    for _ in range(2):
        await relations_service.link_entries(
            db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type="supersedes", actor="t"
        )
    await db_session.refresh(b)
    assert b.status == "veraltet"


async def test_unlink_supersedes_does_not_revert_status(db_session, two_entries):
    a, b = two_entries
    await relations_service.link_entries(
        db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type="supersedes", actor="t"
    )
    await relations_service.unlink_entries(db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type="supersedes")
    await db_session.refresh(b)
    assert b.status == "veraltet"


async def test_get_project_relation_graph_nodes_and_edges(db_session, sample_project, two_entries):
    a, b = two_entries
    await relations_service.link_entries(
        db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type="same_as", note="dup", actor="t"
    )

    graph = await relations_service.get_project_relation_graph(db_session, sample_project.id)

    assert {n["id"] for n in graph["nodes"]} == {str(a.id), str(b.id)}
    assert graph["edges"] == [
        {"from": str(a.id), "to": str(b.id), "relation_type": "same_as", "note": "dup"}
    ]


async def test_get_project_relation_graph_excludes_cross_project_relations(db_session, sample_project, two_entries):
    a, b = two_entries
    other_project = Project(slug="relgraphother", name="Other Graph Project", sensitivity_level="niedrig")
    db_session.add(other_project)
    await db_session.flush()
    other_entry = await entries_service.upsert_entry(
        db_session, project_slug="relgraphother", subtopic_path="topic", title="Other", body_markdown="...", actor="t"
    )
    await relations_service.link_entries(
        db_session, from_entry_id=a.id, to_entry_id=other_entry.id, relation_type="related_to", actor="t"
    )

    graph = await relations_service.get_project_relation_graph(db_session, sample_project.id)

    assert {n["id"] for n in graph["nodes"]} == {str(a.id), str(b.id)}
    assert graph["edges"] == []  # the cross-project relation is omitted entirely


@pytest.fixture
async def similar_pair(db_session, git_repo_path, sample_project):
    body = "Der Kunde Herr Mueller moechte eine Waermepumpe einbauen lassen und bittet um ein Angebot."
    a = await entries_service.upsert_entry(
        db_session, project_slug="relproj", subtopic_path="kunden", title="Herr Mueller Beratungstermin",
        body_markdown=body, actor="t",
    )
    b = await entries_service.upsert_entry(
        db_session, project_slug="relproj", subtopic_path="allgemein", title="Herr Mueller Beratungstermin Kopie",
        body_markdown=body, actor="t",
    )
    return a, b


async def test_find_similar_entries_finds_near_duplicate_pair(db_session, similar_pair):
    a, b = similar_pair
    results = await relations_service.find_similar_entries(db_session, project_slug="relproj")
    assert len(results) == 1
    pair_ids = {results[0]["entry_a"]["id"], results[0]["entry_b"]["id"]}
    assert pair_ids == {str(a.id), str(b.id)}
    assert results[0]["similarity"] >= 0.90
    assert results[0]["linked"] is False


async def test_find_similar_entries_ignores_dissimilar_pair(db_session, similar_pair):
    await entries_service.upsert_entry(
        db_session,
        project_slug="relproj",
        subtopic_path="kunden",
        title="Steuererklaerung 2026",
        body_markdown="Unterlagen fuer die Einkommensteuererklaerung beim Finanzamt einreichen.",
        actor="t",
    )
    results = await relations_service.find_similar_entries(db_session, project_slug="relproj")
    # only the near-duplicate pair from similar_pair should show up, not the unrelated third entry
    assert len(results) == 1


async def test_find_similar_entries_excludes_already_linked_pairs(db_session, similar_pair):
    a, b = similar_pair
    await relations_service.link_entries(
        db_session, from_entry_id=a.id, to_entry_id=b.id, relation_type="related_to", actor="t"
    )
    results = await relations_service.find_similar_entries(db_session, project_slug="relproj")
    assert results == []


async def test_find_similar_entries_respects_project_scope(db_session, git_repo_path, similar_pair):
    other_project = Project(slug="simscopeother", name="Other Sim Scope Project", sensitivity_level="niedrig")
    db_session.add(other_project)
    await db_session.flush()

    results = await relations_service.find_similar_entries(db_session, project_slug="simscopeother")
    assert results == []  # the similar pair lives in relproj, not this project


async def test_find_similar_entries_auto_link_creates_relation(db_session, similar_pair):
    a, b = similar_pair
    results = await relations_service.find_similar_entries(db_session, project_slug="relproj", auto_link=True, actor="t")
    assert len(results) == 1
    assert results[0]["linked"] is True

    related = await relations_service.get_related_entries(db_session, a.id)
    assert len(related) == 1
    assert related[0]["relation_type"] == "related_to"
    assert related[0]["entry_id"] == str(b.id)
