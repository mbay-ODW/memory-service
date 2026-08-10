from app.services.git_store import GitStore


def test_write_and_commit_roundtrip(git_repo_path):
    store = GitStore(git_repo_path)

    rendered = store.render(
        title="Test Entry",
        subtopic_path="estrich",
        tags=["baustelle"],
        sources=["mail:msg-1"],
        body_markdown="Erster Stand.",
    )
    commit_hash = store.write_and_commit(
        "ferienhaus/estrich/test-entry.md", rendered, "create: ferienhaus/estrich/test-entry", "claude"
    )

    assert len(commit_hash) == 40
    content = store.show_file_at(commit_hash, "ferienhaus/estrich/test-entry.md")
    assert "Erster Stand." in content
    assert "title: Test Entry" in content


def test_write_and_commit_creates_history(git_repo_path):
    store = GitStore(git_repo_path)
    path = "ferienhaus/estrich/test-entry.md"

    first = store.write_and_commit(path, store.render(title="T", subtopic_path="estrich", tags=[], sources=[], body_markdown="v1"), "create", "claude")
    second = store.write_and_commit(path, store.render(title="T", subtopic_path="estrich", tags=[], sources=[], body_markdown="v2"), "update", "murat")

    log = store.log(path)
    assert [c["hash"] for c in log] == [second, first]
    assert log[0]["author"] == "murat"
    assert "v1" in store.show_file_at(first, path)
    assert "v2" in store.show_file_at(second, path)
