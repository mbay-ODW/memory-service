# Lessons — memory-service

Non-obvious gotchas hit while building this repo. Read before touching app/db, app/services,
or tests/conftest.py.

## A read tool that omits a field needed to construct the matching write call WILL cause silent duplicates

`memory_get`/`memory_search`/`memory_list_open` originally returned entries without their
`subtopic` path, even though `memory_upsert` is keyed by `(subtopic, title)`. This wasn't
caught by any test because every test *already knew* the subtopic it had written -- nothing
exercised the actual failure mode: a caller reading an entry back, not knowing its subtopic,
and having to guess when updating it. A wrong guess doesn't error (there's no way for
`upsert_entry` to know the caller "meant" an existing entry under a different path) -- it
silently creates a new entry under the guessed path. This produced real duplicate entries in
production (confirmed: 5 pairs in the `geb` project) before anyone noticed, because each
individual write looked completely successful.

**The general lesson: for any tool whose write path is keyed by more than the primary id,
every read path MUST return the full key, not just the id.** Don't assume a caller can
reconstruct a compound key from context — across MCP tool calls in particular, the caller has
no state except what a previous response handed back. When adding a new key field to a
write operation, grep for every read/list function returning that entity and add the field
there too, in the same change.

## TrueNAS deploy: the Traefik rule template assumes "no web UI of its own" — this service breaks that assumption

## TrueNAS deploy: the Traefik rule template assumes "no web UI of its own" — this service breaks that assumption

Every other self-hosted MCP server here (signal-mcp, instagram-*, mail-mcp, ...) is a pure
API/MCP backend with nothing meaningful at `/` or `/static`, so their shared Traefik
file-provider rule template routes `Path(/)` and `PathPrefix(/static)` to Authelia's own
login/consent UI unconditionally -- harmless for them. This service has a real dashboard at
`/` and real CSS/JS under what would've been `/static`, so copying the template verbatim
silently shadowed both. Fixed by (1) renaming the app's static mount to `/assets` (see
`app/main.py`) instead of touching the shared `/static` convention every other client relies
on, and (2) dropping the `-root` router from `/mnt/apps/traefik/data/traefik2/rules/
memory-service.yml` entirely. **If this service ever needs another top-level path added to
its own routes, check it doesn't collide with the reserved auth-flow paths first**:
`/authorize`, `/api/oidc`, `/api`, `/consent`, `/static`, `/.well-known`.

## Portainer `updateLocalStack` does NOT preserve a stack's env vars if you omit `env`

Redeploying stack 59 without re-passing the `env` array (thinking it would just reuse what
was already stored) silently cleared `DB_PASSWORD`/`OIDC_CLIENT_SECRET` to empty, which broke
the DB connection on the very next container recreate (`InvalidPasswordError` -- Postgres
itself was untouched and still had the real password, only the app's interpolated connection
string went empty). Fixed by re-issuing the update with the full `env` array. **Always pass
the complete `env` array on every `updateLocalStack` call**, even ones that only bump the
image tag.

## AsyncSession + relationship attributes: never touch them synchronously unless you KNOW they're loaded

Every `MissingGreenlet` error hit during this build traced back to the same root cause: code
synchronously reading an ORM relationship attribute (`entry.tags`) that turned out not to be
loaded, forcing SQLAlchemy to attempt a lazy-load query outside of an awaited call — which
`AsyncSession` can't do (no greenlet context to piggyback on).

Concrete traps that look safe but aren't:
- `entry.tags = [...]` on a **persistent** object: the setter first lazy-loads the CURRENT
  collection to diff against. Fix: mutate the `entry_tags` association table directly with
  `delete()`/`insert()`, never assign the relationship.
- `session.get(Entry, id, options=[selectinload(...)])` when `id` is **already in the
  session's identity map** (e.g. right after your own `flush()`): `get()` short-circuits and
  returns the cached object AS-IS, silently ignoring the loader option. Use
  `select(Entry).options(selectinload(...)).where(...)` instead — a real query always applies
  eager-load options, `get()` doesn't when it takes the identity-map fast path.
- `session.refresh(obj, attribute_names=["tags"])`: only guarantees the NAMED attributes;
  other previously-loaded attributes (`updated_at`, etc.) can come back expired, then raise
  MissingGreenlet on the NEXT sync access. Prefer a full re-fetch via `select()`.
- Reading `entry.tags` again after a `flush()` that touched other columns on the same row:
  don't assume it's still loaded even if you loaded it earlier in the same function. If you
  need current tag names, query them explicitly (`select(Tag.name).join(entry_tags)...`)
  rather than trusting the in-memory relationship state.

**Rule of thumb:** if a function needs to read data that might come from a relationship,
write an explicit `await session.execute(select(...))` for it. Don't lean on "it was probably
already loaded."

## pytest: session-scoped async loop is required, not optional, given here

`app/db/base.py`'s engine (and its asyncpg connection pool) is a **process-lifetime
singleton** — same as it is under real uvicorn. pytest-asyncio defaults to a fresh event loop
per test function, which reuses that already-loop-bound pool across loops → asyncpg errors
("Task attached to a different loop"). Fixed via
`asyncio_default_fixture_loop_scope = "session"` / `asyncio_default_test_loop_scope =
"session"` in `pyproject.toml`. Don't remove these.

## FastAPI's `TestClient` runs in its OWN separate thread+loop — don't use it for DB-touching web UI tests

`starlette.testclient.TestClient` (used in `tests/test_auth.py`, fine there since those
requests never touch the DB) spins up its own anyio thread/loop. Once the DB engine singleton
is already bound to pytest's session loop (by any earlier test), a `TestClient` request that
hits the DB collides with it the same way the loop-scope issue above does.
`tests/test_web_ui.py` uses `httpx.AsyncClient(transport=httpx.ASGITransport(app=app))` from
an `async def` test instead — same loop as everything else, no lifespan needed since web
routes don't depend on it (only `/mcp` does).

## `tests/conftest.py`'s `db_session` fixture needs the `after_transaction_end` listener

The "join a session into an external transaction" recipe (SQLAlchemy docs) needs an
`event.listens_for(session.sync_session, "after_transaction_end")` handler that manually
restarts the SAVEPOINT after the session under test calls `commit()`. Without it, the
FIRST `commit()` inside a test works, but any DB access afterward fails with MissingGreenlet
trying to open a new savepoint outside proper async context. This is documented upstream but
easy to drop when copying the recipe from memory — don't.

## macOS-only: `/tmp` symlink breaks GitPython path checks

`tempfile.mkdtemp()` on macOS returns a path under `/var/folders/...`, which is itself a
symlink to `/private/var/folders/...`. GitPython resolves `repo.working_tree_dir` via
realpath, so a path handed to `index.add()` built from the un-resolved tmp path fails
GitPython's "is this path inside the repo?" check. Fixed in `GitStore.__init__` by resolving
`repo_path` with `.resolve()` before use. Doesn't affect the real deployment (`/data/git-repo`
isn't under a symlinked directory), but does affect every local test run.

## Data model: `upsert_entry` vs. `update_entry` are NOT interchangeable

`upsert_entry` (the MCP tool's write path) is keyed by `(subtopic_id, slugify(title))`.
Calling it again with a DIFFERENT title creates a NEW entry — it doesn't rename the existing
one. That's correct for MCP callers (they don't have an entry_id to reference), but WRONG for
the web UI's edit form, where a human editing the title of entry X expects entry X to be
renamed. `update_entry` (keyed by entry_id) exists specifically for that case, including
renaming the underlying git file in the same commit (`git_store.write_and_commit`'s
`old_relative_path` param) rather than leaving an orphaned file under the old slug. If you add
another write path, ask which of the two identity semantics it needs BEFORE reusing either
function.
