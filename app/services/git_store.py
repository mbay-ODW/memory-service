"""Plain (non-bare) git working-tree repo that materializes the entry version history.

One .md file per entry, path = '<project>/<subtopic>/.../<entry-slug>.md'. This is the ONLY
place that touches the git repo — services/entries.py and services/projects.py are the only
callers. Every write here is synchronous (GitPython shells out to `git`); callers run it via
asyncio.to_thread and serialize with `git_write_lock` below, since this is a single shared
working tree, not one-repo-per-writer.
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import frontmatter
from git import Actor, Repo
from git.exc import InvalidGitRepositoryError

from app.config import get_settings

_COMMITTER = Actor("memory-service", "memory-service@internal.local")

# Serializes every write to the shared git working tree across all callers (entries.py,
# projects.py). One process, one repo -- import this rather than making a second lock.
git_write_lock = asyncio.Lock()

_store: "GitStore | None" = None


class GitStore:
    def __init__(self, repo_path: str):
        # resolve() normalizes symlinks (e.g. macOS /tmp -> /private/tmp) so paths we hand to
        # GitPython later always agree with its own resolved working_tree_dir.
        self.repo_path = Path(repo_path).resolve()
        self.repo_path.mkdir(parents=True, exist_ok=True)
        try:
            self.repo = Repo(self.repo_path)
        except InvalidGitRepositoryError:
            self.repo = Repo.init(self.repo_path, initial_branch="main")

    def render(self, *, title: str, subtopic_path: str, tags: list[str], sources: list[str], body_markdown: str) -> str:
        post = frontmatter.Post(
            body_markdown,
            title=title,
            subtopic=subtopic_path,
            tags=tags,
            sources=sources,
            updated=datetime.now(timezone.utc).isoformat(),
        )
        return frontmatter.dumps(post)

    def write_and_commit(
        self,
        relative_path: str,
        content: str,
        message: str,
        author_name: str,
        *,
        old_relative_path: str | None = None,
    ) -> str:
        """old_relative_path is set only when an entry's title (and therefore its slug/
        filename) changed -- the old file is removed in the SAME commit as the new one is
        added, so history reads as a rename rather than a delete-and-recreate."""
        full_path = self.repo_path / relative_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        self.repo.index.add([str(full_path)])

        if old_relative_path and old_relative_path != relative_path:
            old_full_path = self.repo_path / old_relative_path
            if old_full_path.exists():
                self.repo.index.remove([str(old_full_path)], working_tree=True)

        author = Actor(author_name, "memory-service@internal.local")
        commit = self.repo.index.commit(message, author=author, committer=_COMMITTER)
        return commit.hexsha

    def remove_path_and_commit(self, relative_dir: str, message: str, author_name: str) -> str | None:
        """Remove an entire directory (a deleted project or subtopic) in one commit. Returns
        None (no commit made) if the path doesn't exist or has nothing tracked under it -- a
        freshly created project/subtopic may never have had an entry written into it."""
        full_path = self.repo_path / relative_dir
        if not full_path.exists():
            return None
        tracked = self.repo.git.ls_files(str(full_path))
        if not tracked:
            return None

        self.repo.git.rm("-r", str(full_path))
        author = Actor(author_name, "memory-service@internal.local")
        commit = self.repo.index.commit(message, author=author, committer=_COMMITTER)
        return commit.hexsha

    def show_file_at(self, commit_hash: str, relative_path: str) -> str:
        return self.repo.git.show(f"{commit_hash}:{relative_path}")

    def log(self, relative_path: str, max_count: int = 20) -> list[dict]:
        commits = list(self.repo.iter_commits(paths=relative_path, max_count=max_count))
        return [
            {
                "hash": c.hexsha,
                "author": c.author.name,
                "date": c.committed_datetime,
                "message": c.message.strip(),
            }
            for c in commits
        ]


def get_git_store() -> GitStore:
    global _store
    if _store is None:
        _store = GitStore(get_settings().git_repo_path)
    return _store
