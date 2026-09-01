from fastapi.testclient import TestClient

from app.main import app

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}
_HEADERS = {"Accept": "application/json, text/event-stream"}


def test_mcp_rejects_missing_token():
    with TestClient(app) as client:
        resp = client.post("/mcp", json=_INITIALIZE, headers=_HEADERS)
    assert resp.status_code == 401


def test_mcp_rejects_wrong_token():
    with TestClient(app) as client:
        resp = client.post("/mcp", json=_INITIALIZE, headers={**_HEADERS, "Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_mcp_accepts_dev_token():
    with TestClient(app) as client:
        resp = client.post(
            "/mcp", json=_INITIALIZE, headers={**_HEADERS, "Authorization": "Bearer test-token"}
        )
    assert resp.status_code == 200


def test_web_routes_reject_missing_remote_user():
    """The web UI must not be readable without the forward-auth header. Reads matter as
    much as writes here: the dashboard, search and entry pages are the bulk of the data."""
    with TestClient(app) as client:
        for path in ("/", "/search?q=test", "/projects/anything", "/entries/new"):
            assert client.get(path).status_code == 401, path


def test_web_routes_accept_remote_user():
    with TestClient(app) as client:
        resp = client.get("/", headers={"Remote-User": "someone"})
    assert resp.status_code == 200


def test_healthz_does_not_require_auth():
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200


def test_post_mcp_does_not_redirect():
    """Regression test: real MCP clients (Claude's own connector, verified live) don't follow
    redirects, so a POST to the bare "/mcp" path (no trailing slash) must be handled directly
    -- not 307'd to "/mcp/" the way a naive path="/" + mount("/mcp", ...) setup does."""
    with TestClient(app, follow_redirects=False) as client:
        resp = client.post("/mcp", json=_INITIALIZE, headers={**_HEADERS, "Authorization": "Bearer test-token"})
    assert resp.status_code == 200
