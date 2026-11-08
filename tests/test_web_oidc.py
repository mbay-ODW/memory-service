"""OIDC login of the web UI. The identity provider is a mock transport; nothing here
leaves the process."""

import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.config import get_settings
from app.main import app
from app.web import auth

ISSUER = "https://idp.example"


def _id_token(**overrides) -> str:
    claims = {"iss": ISSUER, "aud": ["web-client"], "exp": time.time() + 300, **overrides}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


@pytest.fixture
def oidc_mode(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "web_auth_mode", "oidc")
    monkeypatch.setattr(settings, "web_public_url", "https://app.example")
    monkeypatch.setattr(settings, "web_oidc_issuer", ISSUER)
    monkeypatch.setattr(settings, "web_oidc_client_id", "web-client")
    monkeypatch.setattr(settings, "web_oidc_client_secret", "secret")


@pytest.fixture
async def anon():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def _mock_idp(monkeypatch, nonce_holder, username="murat", token_overrides=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token"):
            claims = {"nonce": nonce_holder["nonce"], **(token_overrides or {})}
            return httpx.Response(200, json={"id_token": _id_token(**claims), "access_token": "a"})
        return httpx.Response(200, json={"preferred_username": username} if username else {})

    monkeypatch.setattr(
        auth, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def _start_login(client, next_path="/search?q=x"):
    response = await client.get("/auth/login", params={"next": next_path})
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return {"state": query["state"][0], "nonce": query["nonce"][0], "query": query}


async def test_remote_user_header_is_ignored(oidc_mode, anon):
    assert (await anon.get("/", headers={"Remote-User": "murat"})).status_code == 401


async def test_browser_is_sent_to_login(oidc_mode, anon):
    response = await anon.get("/search?q=a", headers={"Accept": "text/html"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/auth/login?next=")


async def test_login_uses_configured_redirect_and_pkce(oidc_mode, anon):
    query = (await _start_login(anon))["query"]
    assert query["redirect_uri"] == ["https://app.example/auth/callback"]
    assert query["code_challenge_method"] == ["S256"]


async def test_full_login_grants_access(oidc_mode, anon, monkeypatch):
    started = await _start_login(anon, "/search?q=x")
    _mock_idp(monkeypatch, started)
    response = await anon.get("/auth/callback", params={"code": "c", "state": started["state"]})
    assert response.status_code == 303
    assert response.headers["location"] == "/search?q=x"
    assert (await anon.get("/")).status_code == 200


async def test_wrong_state_is_rejected(oidc_mode, anon, monkeypatch):
    started = await _start_login(anon)
    _mock_idp(monkeypatch, started)
    response = await anon.get("/auth/callback", params={"code": "c", "state": "falsch"})
    assert response.status_code == 400
    assert (await anon.get("/")).status_code == 401


@pytest.mark.parametrize(
    "overrides",
    [
        {"nonce": "falsch"},
        {"iss": "https://evil.example"},
        {"aud": ["anderer-client"]},
        {"exp": time.time() - 10},
    ],
)
async def test_invalid_id_token_is_rejected(oidc_mode, anon, monkeypatch, overrides):
    started = await _start_login(anon)
    _mock_idp(monkeypatch, started, token_overrides=overrides)
    response = await anon.get("/auth/callback", params={"code": "c", "state": started["state"]})
    assert response.status_code == 400
    assert (await anon.get("/")).status_code == 401


async def test_missing_username_is_rejected(oidc_mode, anon, monkeypatch):
    started = await _start_login(anon)
    _mock_idp(monkeypatch, started, username=None)
    response = await anon.get("/auth/callback", params={"code": "c", "state": started["state"]})
    assert response.status_code == 403


@pytest.mark.parametrize("target", ["https://evil.example", "//evil.example", "/\\evil.example"])
async def test_next_cannot_leave_the_site(target):
    assert auth._safe_next(target) == "/"


async def test_logout_ends_the_session(oidc_mode, anon, monkeypatch):
    started = await _start_login(anon)
    _mock_idp(monkeypatch, started)
    await anon.get("/auth/callback", params={"code": "c", "state": started["state"]})
    assert (await anon.post("/auth/logout")).status_code == 303
    assert (await anon.get("/")).status_code == 401
