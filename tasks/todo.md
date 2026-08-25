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

## Checkpoint A verification — done
- [x] `pytest` green (22 tests) run twice back-to-back against the same DB (proves no
      state-leak between runs)
- [x] Rebuild the production Docker image with all Phase 0-4 code, confirm it starts clean
- [x] Run migrations + seed script against the dev `memory` DB (not `memory_test`)
- [x] Visual pass through the running web UI in an actual browser
- [x] `scripts/reembed_all.py` dry-run against seed data
- [x] Repo pushed to `github.com/mbay-ODW/memory-service` (private), CI green (test + build)

## Checkpoint B — TrueNAS deployment (done, 2026-08-10)
- [x] GHCR package flipped to Public (manual, user did it)
- [x] `/mnt/apps/memory-service/{db,git}` created
- [x] Authelia OIDC client `memory-service` added (bcrypt secret, config validated before
      restart), `docker restart authelia`
- [x] Portainer local stack 59 (`db` + `memory-service`, `traefik` net + internal net)
- [x] Traefik file-provider rule `memory-service.yml` — **deviates from the signal-mcp
      template on purpose**: no `-root` router and `/static` renamed to `/assets` in the app,
      because unlike the other MCP servers here, this one has its own dashboard + static
      assets that the template's blanket "send `/` and `/static` to Authelia" would shadow.
      Caught live (user noticed `/` showed Authelia's login page) and fixed in both the
      Traefik rule and the app (commit `7bdbeac`).
- [x] Migrations run against the production DB (starts empty — dev seed data was never
      pushed here, intentionally)
- [x] Live smoke test: `/` → dashboard, `/assets/style.css` → 200, `/mcp` without token →
      401, `/.well-known/oauth-authorization-server` → 200
- [x] **Lesson for next redeploy**: `updateLocalStack` does NOT preserve a stack's previously
      set env vars if you omit `env` on the call — it clears them. Broke the DB connection
      once for exactly this reason (mid-deploy). Always re-pass the full `env` array
      (`DB_PASSWORD`, `OIDC_CLIENT_SECRET`) on every `updateLocalStack` call, even ones that
      only change the image tag.

Connector URL: `https://memory-service.your-domain.example/mcp`

## Not started (needs separate go-ahead)
Phase 6 (Cowork pilot wiring: connector + Standing Instruction + Scheduled Task for Interne
IT), Phase 7 (RLS hardening, audit logging, before Privat/GEB/Steuer rollout).

## Feature: entry relations (done locally, 2026-08-25)
- [x] Migration `0003_entry_relations` (`entry_relations` table, run + verified locally)
- [x] `app/db/models.py` — `Relation` model, `RELATION_TYPES`
- [x] `app/services/relations.py` — `link_entries`/`unlink_entries`/`get_related_entries`
- [x] `app/mcp/tools.py` — `memory_link_entries`/`memory_unlink_entries`/`memory_get_related`
- [x] Web UI: read-only "Verknüpfte Einträge" section on the entry page
- [x] Tests: `test_relations_service.py` (8), `test_mcp_tools.py` (+4), `test_web_ui.py` (+1)
      — full suite 60/60 green
- [x] README MCP tools table + hygiene grep sweep (clean)
- [ ] Not shipped to TrueNAS yet — same build → push → pull → migrate → redeploy sequence as
      every prior feature, re-passing `env` on `updateLocalStack`
- [ ] Still open: whether to clean up the 5 known `geb` duplicate pairs now via
      `memory_delete_entry`/`memory_link_entries(same_as)`, or wait for a possible future
      `memory_merge_entries` tool — Murat hasn't decided yet
