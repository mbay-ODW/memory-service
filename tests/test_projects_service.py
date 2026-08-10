import pytest

from app.services import entries as entries_service
from app.services import projects as projects_service
from app.services.git_store import get_git_store


async def test_create_project(db_session, git_repo_path):
    project = await projects_service.create_project(
        db_session, name="Neues Projekt", sensitivity_level="mittel", description="Kontext-Text."
    )
    assert project.slug == "neues-projekt"
    assert project.description == "Kontext-Text."


async def test_create_project_rejects_duplicate_slug(db_session, git_repo_path):
    await projects_service.create_project(db_session, name="Doppelt", sensitivity_level="niedrig")
    with pytest.raises(ValueError):
        await projects_service.create_project(db_session, name="Doppelt", sensitivity_level="niedrig")


async def test_create_project_rejects_invalid_sensitivity(db_session, git_repo_path):
    with pytest.raises(ValueError):
        await projects_service.create_project(db_session, name="X", sensitivity_level="ultra")


async def test_rename_project_keeps_slug(db_session, git_repo_path):
    project = await projects_service.create_project(db_session, name="Alter Name", sensitivity_level="niedrig")
    renamed = await projects_service.rename_project(
        db_session, project_slug=project.slug, name="Neuer Name", description="Neue Beschreibung"
    )
    assert renamed.id == project.id
    assert renamed.slug == "alter-name"
    assert renamed.name == "Neuer Name"
    assert renamed.description == "Neue Beschreibung"


async def test_delete_project_cascades_db_and_removes_git_dir(db_session, git_repo_path):
    project = await projects_service.create_project(db_session, name="Loeschprojekt", sensitivity_level="niedrig")
    await entries_service.upsert_entry(
        db_session,
        project_slug=project.slug,
        subtopic_path="thema",
        title="Ein Eintrag",
        body_markdown="Inhalt.",
        actor="tester",
    )
    git_dir = get_git_store().repo_path / project.slug
    assert git_dir.exists()

    await projects_service.delete_project(db_session, project_slug=project.slug, actor="tester")

    from app.db.repository import NotFoundError, get_project_by_slug

    with pytest.raises(NotFoundError):
        await get_project_by_slug(db_session, project.slug)
    assert not git_dir.exists()


async def test_rename_subtopic_keeps_slug(db_session, git_repo_path):
    project = await projects_service.create_project(db_session, name="Subprojekt", sensitivity_level="niedrig")
    entry = await entries_service.upsert_entry(
        db_session,
        project_slug=project.slug,
        subtopic_path="mein-thema",
        title="Eintrag",
        body_markdown="...",
        actor="tester",
    )
    subtopic_id = entry.subtopic_id

    renamed = await projects_service.rename_subtopic(db_session, subtopic_id=subtopic_id, name="Mein Neues Thema")
    assert renamed.id == subtopic_id
    assert renamed.slug == "mein-thema"
    assert renamed.name == "Mein Neues Thema"


async def test_delete_subtopic_cascades_and_removes_git_dir(db_session, git_repo_path):
    project = await projects_service.create_project(db_session, name="Subloeschen", sensitivity_level="niedrig")
    entry = await entries_service.upsert_entry(
        db_session,
        project_slug=project.slug,
        subtopic_path="a/b",
        title="Verschachtelt",
        body_markdown="...",
        actor="tester",
    )
    parent_subtopic_id = entry.subtopic_id  # this is "b"; delete "a" (its parent) instead
    from app.db.models import Subtopic

    b = await db_session.get(Subtopic, parent_subtopic_id)
    a_id = b.parent_subtopic_id
    git_dir = get_git_store().repo_path / project.slug / "a"
    assert git_dir.exists()

    await projects_service.delete_subtopic(db_session, subtopic_id=a_id, actor="tester")

    remaining = await db_session.get(Subtopic, parent_subtopic_id)
    assert remaining is None  # child "b" cascaded away too
    assert not git_dir.exists()


async def test_get_project_stats(db_session, git_repo_path):
    project = await projects_service.create_project(db_session, name="Statsprojekt", sensitivity_level="niedrig")
    await entries_service.upsert_entry(
        db_session, project_slug=project.slug, subtopic_path="t1", title="Eins", body_markdown="12345", actor="t"
    )
    await entries_service.upsert_entry(
        db_session, project_slug=project.slug, subtopic_path="t2", title="Zwei", body_markdown="1234567890", actor="t"
    )
    stats = await projects_service.get_project_stats(db_session, project)
    assert stats["entry_count"] == 2
    assert stats["char_count"] == 15
    assert stats["subtopic_count"] == 2


async def test_get_subtopic_stats_map_rolls_up_descendants(db_session, git_repo_path):
    project = await projects_service.create_project(db_session, name="Rollupprojekt", sensitivity_level="niedrig")
    parent_entry = await entries_service.upsert_entry(
        db_session, project_slug=project.slug, subtopic_path="parent", title="P", body_markdown="12345", actor="t"
    )
    child_entry = await entries_service.upsert_entry(
        db_session,
        project_slug=project.slug,
        subtopic_path="parent/child",
        title="C",
        body_markdown="1234567890",
        actor="t",
    )

    stats_map = await projects_service.get_subtopic_stats_map(db_session, project)
    assert stats_map[parent_entry.subtopic_id]["entry_count"] == 2  # parent + child rolled up
    assert stats_map[parent_entry.subtopic_id]["char_count"] == 15
    assert stats_map[child_entry.subtopic_id]["entry_count"] == 1
    assert stats_map[child_entry.subtopic_id]["char_count"] == 10
