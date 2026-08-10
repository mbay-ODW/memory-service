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


def test_healthz_does_not_require_auth():
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
