import pytest

from app.db.models import Project
from app.services import entries as entries_service
from app.services.git_store import get_git_store


@pytest.fixture
async def sample_project(db_session):
    project = Project(slug="testproj", name="Test Project", sensitivity_level="niedrig")
    db_session.add(project)
    await db_session.flush()
    return project


async def test_upsert_entry_creates_new(db_session, git_repo_path, sample_project):
    entry = await entries_service.upsert_entry(
        db_session,
        project_slug="testproj",
        subtopic_path="topic-a",
        title="Erste Notiz",
        body_markdown="Inhalt der ersten Notiz.",
        actor="claude:task-1",
        sources=[("mail", "msg-100")],
        tags=["wichtig"],
    )
    assert entry.id is not None
    assert entry.slug == "erste-notiz"
    assert entry.status == "aktuell"
    assert entry.body_embedding is not None
    assert len(entry.body_embedding) == 384
    assert {t.name for t in entry.tags} == {"wichtig"}

    history = await entries_service.get_history(db_session, entry.id)
    assert len(history) == 1
    assert history[0].git_commit_hash


async def test_upsert_entry_updates_existing_not_duplicate(db_session, git_repo_path, sample_project):
    first = await entries_service.upsert_entry(
        db_session,
        project_slug="testproj",
        subtopic_path="topic-a",
        title="Erste Notiz",
        body_markdown="v1",
        actor="claude:task-1",
    )
    second = await entries_service.upsert_entry(
        db_session,
        project_slug="testproj",
        subtopic_path="topic-a",
        title="Erste Notiz",
        body_markdown="v2",
        actor="claude:task-2",
    )
    assert first.id == second.id
    assert second.body_markdown == "v2"

    history = await entries_service.get_history(db_session, second.id)
    assert len(history) == 2


async def test_upsert_entry_auto_creates_nested_subtopics(db_session, git_repo_path, sample_project):
    entry = await entries_service.upsert_entry(
        db_session,
        project_slug="testproj",
        subtopic_path="kunde/vorgang-1",
        title="Nested",
        body_markdown="Verschachtelter Eintrag.",
        actor="claude",
    )
    found = await entries_service.get_entries(db_session, project_slug="testproj", subtopic_path="kunde")
    assert entry.id in {e.id for e in found}


async def test_list_open_filters_follow_up(db_session, git_repo_path, sample_project):
    await entries_service.upsert_entry(
        db_session,
        project_slug="testproj",
        subtopic_path="topic-a",
        title="Offener Punkt",
        body_markdown="Wartet auf Rückmeldung.",
        actor="claude",
        follow_up_status="offen",
    )
    await entries_service.upsert_entry(
        db_session,
        project_slug="testproj",
        subtopic_path="topic-a",
        title="Erledigter Punkt",
        body_markdown="Fertig.",
        actor="claude",
    )
    open_entries = await entries_service.list_open(db_session, project_slug="testproj")
    assert {e.title for e in open_entries} == {"Offener Punkt"}


async def test_update_entry_renames_in_place_and_moves_git_file(db_session, git_repo_path, sample_project):
    entry = await entries_service.upsert_entry(
        db_session,
        project_slug="testproj",
        subtopic_path="topic-a",
        title="Alter Titel",
        body_markdown="Inhalt.",
        actor="claude",
    )
    old_path = "testproj/topic-a/alter-titel.md"
    assert (get_git_store().repo_path / old_path).exists()

    renamed = await entries_service.update_entry(
        db_session,
        entry_id=entry.id,
        title="Neuer Titel",
        body_markdown="Inhalt.",
        actor="user:murat",
    )

    assert renamed.id == entry.id
    assert renamed.slug == "neuer-titel"
    assert renamed.title == "Neuer Titel"

    new_path = "testproj/topic-a/neuer-titel.md"
    assert (get_git_store().repo_path / new_path).exists()
    assert not (get_git_store().repo_path / old_path).exists()

    history = await entries_service.get_history(db_session, entry.id)
    assert len(history) == 2  # create + rename-update, same entry throughout


async def test_update_entry_rejects_slug_collision(db_session, git_repo_path, sample_project):
    await entries_service.upsert_entry(
        db_session,
        project_slug="testproj",
        subtopic_path="topic-a",
        title="Bestehender Eintrag",
        body_markdown="...",
        actor="claude",
    )
    other = await entries_service.upsert_entry(
        db_session,
        project_slug="testproj",
        subtopic_path="topic-a",
        title="Anderer Eintrag",
        body_markdown="...",
        actor="claude",
    )

    with pytest.raises(ValueError):
        await entries_service.update_entry(
            db_session,
            entry_id=other.id,
            title="Bestehender Eintrag",
            body_markdown="...",
            actor="user:murat",
        )


async def test_delete_entry_removes_db_row_and_git_file(db_session, git_repo_path, sample_project):
    entry = await entries_service.upsert_entry(
        db_session,
        project_slug="testproj",
        subtopic_path="topic-a",
        title="Zu Loeschender Eintrag",
        body_markdown="Inhalt.",
        actor="claude",
    )
    git_path = get_git_store().repo_path / "testproj/topic-a/zu-loeschender-eintrag.md"
    assert git_path.exists()

    await entries_service.delete_entry(db_session, entry_id=entry.id, actor="tester")

    from app.db.repository import NotFoundError

    with pytest.raises(NotFoundError):
        await entries_service.get_entry_by_id(db_session, entry.id)
    assert not git_path.exists()


@pytest.mark.parametrize("empty_subtopic", ["", "  ", "/", "///", None])
async def test_upsert_entry_with_empty_subtopic_falls_back_to_allgemein(
    db_session, git_repo_path, sample_project, empty_subtopic
):
    entry = await entries_service.upsert_entry(
        db_session,
        project_slug="testproj",
        subtopic_path=empty_subtopic,
        title="Eintrag ohne Unterthema",
        body_markdown="...",
        actor="claude",
    )
    found = await entries_service.get_entries(db_session, project_slug="testproj", subtopic_path="allgemein")
    assert entry.id in {e.id for e in found}
    assert (get_git_store().repo_path / "testproj/allgemein/eintrag-ohne-unterthema.md").exists()


async def test_upsert_entry_empty_subtopic_is_idempotent(db_session, git_repo_path, sample_project):
    first = await entries_service.upsert_entry(
        db_session, project_slug="testproj", subtopic_path="", title="Wiederholt", body_markdown="v1", actor="claude"
    )
    second = await entries_service.upsert_entry(
        db_session, project_slug="testproj", subtopic_path="", title="Wiederholt", body_markdown="v2", actor="claude"
    )
    assert first.id == second.id
    assert second.body_markdown == "v2"


async def test_get_subtopic_paths_returns_full_path_per_entry(db_session, git_repo_path, sample_project):
    nested = await entries_service.upsert_entry(
        db_session,
        project_slug="testproj",
        subtopic_path="kunde-mueller/vorgang-2026-08",
        title="Verschachtelt",
        body_markdown="...",
        actor="claude",
    )
    root = await entries_service.upsert_entry(
        db_session, project_slug="testproj", subtopic_path="", title="Wurzel", body_markdown="...", actor="claude"
    )

    paths = await entries_service.get_subtopic_paths(db_session, [nested, root])
    assert paths[nested.id] == "kunde-mueller/vorgang-2026-08"
    assert paths[root.id] == "allgemein"


async def test_upsert_entry_does_not_reset_a_superseded_entrys_status(db_session, git_repo_path, sample_project):
    entry = await entries_service.upsert_entry(
        db_session, project_slug="testproj", subtopic_path="topic-a", title="Alt", body_markdown="v1", actor="claude"
    )
    entry.status = "veraltet"
    await db_session.flush()

    updated = await entries_service.upsert_entry(
        db_session, project_slug="testproj", subtopic_path="topic-a", title="Alt", body_markdown="v2", actor="claude"
    )
    assert updated.id == entry.id
    assert updated.status == "veraltet"


async def test_update_entry_does_not_reset_a_superseded_entrys_status(db_session, git_repo_path, sample_project):
    entry = await entries_service.upsert_entry(
        db_session, project_slug="testproj", subtopic_path="topic-a", title="Alt", body_markdown="v1", actor="claude"
    )
    entry.status = "veraltet"
    await db_session.flush()

    updated = await entries_service.update_entry(
        db_session, entry_id=entry.id, title="Alt Umbenannt", body_markdown="v2", actor="claude"
    )
    assert updated.status == "veraltet"


async def test_occurred_at_set_kept_cleared_and_written_to_git(db_session, git_repo_path, sample_project):
    from datetime import date
    from pathlib import Path

    def _write(**kwargs):
        return entries_service.upsert_entry(
            db_session,
            project_slug="testproj",
            subtopic_path="topic-occ",
            title="Begehung Mueller",
            body_markdown="Begehung fand statt.",
            actor="claude",
            **kwargs,
        )

    entry = await _write(occurred_at="2026-10-24")
    assert entry.occurred_at == date(2026, 10, 24)
    git_file = Path(git_repo_path) / "testproj" / "topic-occ" / "begehung-mueller.md"
    assert "occurred: '2026-10-24'" in git_file.read_text(encoding="utf-8")

    # None = leave untouched (a wording tidy-up must not lose the event date)
    entry = await _write()
    assert entry.occurred_at == date(2026, 10, 24)

    # a full timestamp (e.g. a mail's Date header) is truncated to its date
    entry = await _write(occurred_at="2026-10-25T18:30:00+02:00")
    assert entry.occurred_at == date(2026, 10, 25)

    entry = await _write(occurred_at="none")
    assert entry.occurred_at is None
    assert "occurred:" not in git_file.read_text(encoding="utf-8")


async def test_occurred_at_rejects_garbage(db_session, git_repo_path, sample_project):
    with pytest.raises(ValueError, match="occurred_at"):
        await entries_service.upsert_entry(
            db_session,
            project_slug="testproj",
            subtopic_path="topic-occ",
            title="Kaputtes Datum",
            body_markdown="...",
            actor="claude",
            occurred_at="letzten Dienstag",
        )


async def test_update_entry_sets_occurred_at(db_session, git_repo_path, sample_project):
    from datetime import date

    entry = await entries_service.upsert_entry(
        db_session,
        project_slug="testproj",
        subtopic_path="topic-occ",
        title="Web-Edit",
        body_markdown="...",
        actor="claude",
    )
    assert entry.occurred_at is None
    updated = await entries_service.update_entry(
        db_session, entry_id=entry.id, title="Web-Edit", body_markdown="...", actor="web", occurred_at="2026-09-01"
    )
    assert updated.occurred_at == date(2026, 9, 1)
