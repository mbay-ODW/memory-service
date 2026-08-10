# memory-service

Self-hosted, versioned project memory for [Claude Cowork](https://claude.ai) (or any MCP
client). One place for Claude to read and write structured notes across projects — searchable,
versioned, editable by a human, and independent of any single chat session.

## Why this exists

Cowork's built-in memory is scoped to a session and doesn't reliably survive the split between
local-desktop sessions and cloud-run scheduled tasks — a fact learned the hard way, not a
theoretical concern. If a cloud task researches something today, a local session tomorrow has
no way to know. There's no version history, no way to search across everything Claude has
learned, and no way for a human to look at what got stored without asking Claude to recite it.

memory-service is the fix: an explicit, external store. Every Cowork session — local, cloud,
scheduled, interactive — talks to the same MCP server. Writes land in Postgres *and* a git
repository in the same operation, so every change is both instantly queryable and permanently
versioned. A human can browse, search, and edit the same data through a plain web UI, no prompt
required.

## Architecture

One FastAPI process serves three things from the same codebase, sharing one database
connection pool and one write path:

```
┌─────────────────────────────────────────────────────┐
│                  FastAPI process                     │
│                                                       │
│   web UI (Jinja2+htmx)      MCP server (/mcp)         │
│         │                          │                  │
│         └──────────┬───────────────┘                  │
│                     ▼                                  │
│            app/services/*.py                          │
│      (the ONLY code that touches DB or git)            │
│                     │                                  │
│         ┌───────────┴───────────┐                      │
│         ▼                       ▼                      │
│    Postgres                  git repo                  │
│  (query + search)        (version history)              │
└─────────────────────────────────────────────────────┘
```

**Single write path.** Every mutation — whether it comes from an MCP tool call or a web form
submission — goes through `app/services/*.py` and nowhere else. Neither the MCP layer
(`app/mcp/tools.py`) nor the web routes (`app/web/routes.py`) touch the database or the git
repo directly; they're both thin wrappers around the same service functions. That means the two
surfaces can never drift apart in behavior, and there's exactly one place to look for how a
write actually happens (`app/services/entries.py`'s `upsert_entry`/`update_entry`).

**Hybrid search, not just one or the other.** Every entry gets both a Postgres `tsvector`
(full-text, GIN-indexed) and a `pgvector` embedding (`intfloat/multilingual-e5-small`,
HNSW-indexed), computed locally on CPU — no embeddings API call, no data leaving the box. A
search merges both rankings with Reciprocal Rank Fusion, so an exact keyword match and a
semantically related note that doesn't share any words both surface. See
`app/services/search.py`.

**Git is the real history; Postgres is the fast index into it.** Every write does: DB
transaction → render+commit a markdown file (one `.md` per entry, YAML frontmatter) → insert an
`entry_versions` row recording the resulting commit hash → commit the transaction. If the git
commit fails, the DB transaction rolls back — the two are never allowed to disagree about what
the latest version is. `entry_versions` exists purely so "show me the last 5 changes" is an
indexed SQL query instead of a `git log` shell-out; the commit hash it stores is how you get
back to the actual git object if you need the full diff. See `app/services/git_store.py`.

**Everything nests, nothing is hardcoded.** Projects contain subtopics, subtopics can nest
arbitrarily deep (`kunde-mueller/vorgang-2026-08/...`), and subtopic paths auto-create on first
write — an agent doesn't need a separate "create subtopic" call before it can file a note under
one. Projects don't auto-create (a project carries a `sensitivity_level` that has real
access-control implications later, so creating one is a deliberate action — either a human in
the web UI, or the one `memory_create_project` MCP tool).

## MCP tools

| Tool | Purpose |
|---|---|
| `memory_search` | Full-text + semantic search, optionally scoped to a project/subtopic |
| `memory_get` | Current entries for a project or subtopic (call this before answering) |
| `memory_upsert` | Create or update an entry, identified by (subtopic, title) |
| `memory_list_open` | Entries flagged as needing follow-up |
| `memory_history` | Version history for one entry |
| `memory_check_sources` | Batch dedup check for daily sync tasks (has this mail/message already been logged?) |
| `memory_create_project` | Create a new project — the only structural MCP tool; rename/delete are web-UI-only, human-confirmed actions |

## Quickstart

```bash
docker compose up --build
```

- App: http://localhost:8000
- Postgres: localhost:5433 (user/db `memory`)

```bash
# migrate, then seed 5 example projects with nested subtopics and sample entries
docker compose exec app alembic upgrade head
docker compose exec app python -m scripts.seed_dummy_data
```

Run tests (spins up its own `memory_test` database on the same Postgres):

```bash
docker compose up -d db
DATABASE_URL=postgresql+asyncpg://memory:memory_dev_password@localhost:5433/memory_test \
  python -m pytest
```

## Repo layout

| Path | What's there |
|---|---|
| `app/db/models.py` | SQLAlchemy models: `projects`, `subtopics` (self-referential), `entries` (tsvector + vector columns), `entry_versions`, `sources`, `tags` |
| `app/services/` | All business logic — `entries.py`, `projects.py`, `search.py`, `embeddings.py`, `git_store.py`, `sources.py` |
| `app/mcp/tools.py` | The 7 MCP tools, each a thin wrapper over `services/*` |
| `app/web/` | Jinja2 + htmx server-rendered UI — no SPA build step, no CDN dependencies (EasyMDE and htmx are vendored) |
| `alembic/versions/` | Schema migrations |
| `tests/` | pytest suite — service-layer, MCP-layer (via `fastmcp.Client`), and web-UI (via `httpx.ASGITransport`) tests |
| `tasks/lessons.md` | Real engineering gotchas hit and fixed while building this — async SQLAlchemy footguns, an MCP-client redirect bug, a Traefik routing collision. Worth a read if you're extending this. |

## Status

Built and running in production for one real deployment (Postgres + git-backed history + web
UI + MCP server, behind Authelia OIDC via Traefik). Not yet hardened for multi-tenant use —
row-level security by `project_id` is designed but not yet implemented (currently
application-layer filtering only); see `tasks/lessons.md` and the design doc referenced in
`tasks/todo.md` for what's done versus planned.
