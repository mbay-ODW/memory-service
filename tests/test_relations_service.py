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
