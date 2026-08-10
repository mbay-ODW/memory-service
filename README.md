# memory-service

Self-hosted, versioned project memory for Cowork. Postgres (full-text + vector search) as the
query store, a plain git working-tree repo as the version-history store, one FastAPI process
exposing both a web UI and an MCP server (mounted at `/mcp`).

## Local development

```bash
docker compose up --build
```

- App: http://localhost:8000 (`/healthz`)
- Postgres: localhost:5433 (user/db `memory`)

Run migrations:

```bash
docker compose exec app alembic upgrade head
```

Seed dummy data (all 5 projects, nested subtopics):

```bash
docker compose exec app python -m scripts.seed_dummy_data
```

Run tests (needs `db` up, uses a separate `memory_test` database):

```bash
docker compose up -d db
DATABASE_URL=postgresql+asyncpg://memory:memory_dev_password@localhost:5433/memory_test \
  python -m pytest
```

## Architecture

See `/Users/mbayram/.claude/plans/lazy-humming-possum.md` for the full design doc. Short version:

- `app/db/models.py` — SQLAlchemy models: `projects`, `subtopics` (self-referential, nestable),
  `entries` (fulltext `body_tsvector` + `body_embedding vector(384)`), `entry_versions`,
  `sources` (dedup key: `UNIQUE(source_type, source_ref)`), `tags`.
- `app/services/` — all business logic (`entries.upsert_entry` is the only write path — DB txn +
  git commit + `entry_versions` row, in that order). Both the web routes and the MCP tools call
  into this layer; neither talks to the DB or git directly.
- `app/mcp/tools.py` — `memory_search`, `memory_get`, `memory_upsert`, `memory_list_open`,
  `memory_history`, `memory_check_sources`.
- `app/web/` — Jinja2 + htmx server-rendered UI, no SPA build step.
