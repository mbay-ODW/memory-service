# memory-service build — Checkpoint A (local, no TrueNAS deploy)

Full design doc: `/Users/mbayram/.claude/plans/lazy-humming-possum.md`

## Phase 0 — Scaffold & local loop
- [x] pyproject.toml, Dockerfile, docker-compose.yml (vectorchord postgres)
- [x] app/config.py, app/db/base.py, app/main.py (`/healthz`)
- [x] CI workflows (build.yml, test.yml)
- [x] `docker compose up --build` → healthy container + DB

## Phase 1 — Schema
- [x] app/db/models.py (6 tables + entry_tags)
- [x] alembic/versions/0001_initial.py — `vector` ext, HNSW index, generated `body_tsvector`
      (verified `german` text-search config + `vector` 0.8.1 both available in the vectorchord
      image before committing to it)
- [x] scripts/seed_dummy_data.py — all 5 projects, nested subtopics, sample entries

## Phase 2 — Core services
- [x] git_store.py, entries.py (upsert_entry, update_entry, _finalize_write), embeddings.py,
      search.py, sources.py
- [x] tests/test_git_store.py, test_entries_service.py, test_search.py

## Phase 3 — MCP layer
- [x] app/mcp/{server,tools}.py — all 6 tools + memory_check_sources
- [x] app/core/security.py — bearer/OIDC auth as ASGI middleware in front of `/mcp`
- [x] tests/test_mcp_tools.py, test_auth.py
- [x] Verified live in the built Docker image: 401 without token, 200 + working MCP
      initialize handshake with the dev bearer token

## Phase 4 — Web UI
- [x] app/web/routes.py + templates (dashboard, project tree, entry view/edit/new, search,
      history, diff)
- [x] Vendored EasyMDE + htmx locally (no CDN)
- [x] update_entry() added (title-rename-in-place, git file rename) — upsert_entry alone
      would have silently created a duplicate entry on a title edit from the UI
- [x] tests/test_web_ui.py — full flow incl. rename, diff, search, 404, 409-on-collision

## Checkpoint A verification (in progress)
- [x] `pytest` green (22 tests) run twice back-to-back against the same DB (proves no
      state-leak between runs)
- [ ] Rebuild the production Docker image with all Phase 0-4 code, confirm it starts clean
- [ ] Run migrations + seed script against the dev `memory` DB (not `memory_test`)
- [ ] Visual pass through the running web UI in an actual browser
- [ ] `scripts/reembed_all.py` dry-run against seed data

## Explicitly NOT started (Checkpoint B — needs separate go-ahead)
Phase 5 (TrueNAS deploy: Portainer stack, Traefik rule, Authelia client), Phase 6 (Cowork
pilot wiring), Phase 7 (RLS hardening, audit logging).
