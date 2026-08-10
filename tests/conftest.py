import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://memory:memory_dev_password@localhost:5433/memory_test"
)
os.environ.setdefault("AUTH_MODE", "dev")
os.environ.setdefault("DEV_BEARER_TOKEN", "test-token")

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()


def _maintenance_url(database_url: str) -> str:
    """Same server, 'postgres' maintenance DB -- needed to CREATE DATABASE the test DB."""
    parts = urlsplit(database_url)
    return urlunsplit(parts._replace(path="/postgres"))


async def _ensure_test_database_exists(database_url: str) -> None:
    target_db = urlsplit(database_url).path.lstrip("/")
    engine = create_async_engine(_maintenance_url(database_url), isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        exists = (
            await conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": target_db})
        ).first()
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{target_db}"'))
    await engine.dispose()


def _run_migrations(database_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    os.environ["DATABASE_URL"] = database_url
    get_settings.cache_clear()
    command.upgrade(cfg, "head")


async def _truncate_all_tables(database_url: str) -> None:
    """Clean slate for every test session. MCP-tool tests commit for real (they open their
    own sessions via get_session_factory(), not the rollback-per-test db_session fixture),
    so without this, re-running pytest locally against the same long-lived memory_test
    database accumulates rows across runs -- e.g. extra entry_versions from a prior run's
    upsert of the same title, which then breaks a "history has exactly 1 version" assertion."""
    from app.db.base import Base

    engine = create_async_engine(database_url)
    table_names = [t.name for t in Base.metadata.sorted_tables if t.name != "alembic_version"]
    async with engine.begin() as conn:
        if table_names:
            await conn.execute(text(f"TRUNCATE TABLE {', '.join(table_names)} RESTART IDENTITY CASCADE"))
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _prepare_database():
    import asyncio

    database_url = get_settings().database_url
    asyncio.run(_ensure_test_database_exists(database_url))
    _run_migrations(database_url)
    asyncio.run(_truncate_all_tables(database_url))
    yield


@pytest_asyncio.fixture
async def db_session(_prepare_database):
    """One test = one transaction, rolled back at the end (savepoint-based so code under test
    can call session.commit() freely without actually persisting across tests). This is
    SQLAlchemy's documented "join a session into an external transaction" recipe -- the
    after_transaction_end listener is required, not optional: without it, the session's
    savepoint isn't restarted after a commit() inside the test, and the next statement fails
    with MissingGreenlet trying to open one itself outside proper async context."""
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.base import get_engine

    engine = get_engine()
    async with engine.connect() as conn:
        outer_tx = await conn.begin()
        await conn.begin_nested()

        session = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")

        @event.listens_for(session.sync_session, "after_transaction_end")
        def _restart_savepoint(sync_session, transaction):
            if conn.closed:
                return
            if not conn.sync_connection.in_nested_transaction():
                conn.sync_connection.begin_nested()

        try:
            yield session
        finally:
            await session.close()
            await outer_tx.rollback()


@pytest.fixture
def git_repo_path(monkeypatch):
    tmp_dir = tempfile.mkdtemp(prefix="memory-service-test-git-")
    monkeypatch.setenv("GIT_REPO_PATH", tmp_dir)
    get_settings.cache_clear()

    from app.services import git_store

    git_store._store = None
    yield tmp_dir

    git_store._store = None
    shutil.rmtree(tmp_dir, ignore_errors=True)
