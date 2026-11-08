"""Login for the web UI: OpenID Connect authorization code flow with PKCE.

The app establishes who the user is on its own instead of believing a Remote-User header.
That header could be sent by anything that can reach the container directly, which is
exactly what an app-level check is supposed to rule out. Only the signed session cookie
set at the end of this flow counts. Header mode exists for setups with a trusted proxy and
has to be switched on explicitly.
"""

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from app.config import Settings, get_settings

router = APIRouter(prefix="/auth")

_HTTP_TIMEOUT = 15.0


def _http_client() -> httpx.AsyncClient:
    """Separate function so tests can swap in a mock transport."""
    return httpx.AsyncClient(timeout=_HTTP_TIMEOUT)


def _redirect_uri(settings: Settings) -> str:
    # From configuration, never from the request: a forged Host header must not be able to
    # choose where the authorization code is delivered.
    return f"{settings.web_public_url.rstrip('/')}/auth/callback"


def _safe_next(target: str | None) -> str:
    """Only paths on this site, so the login cannot be used as an open redirect."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    return target


def require_web_user(request: Request) -> str:
    """Authentication gate for the web UI; returns the actor used for writes.

    Wired as a router-level dependency on purpose: attaching it only where an actor is
    needed would gate the writes and leave every read route open, and reads are the larger
    half of the surface.

    WEB_AUTH_REQUIRED=false turns it off for local development.
    """
    settings = get_settings()
    if not settings.web_auth_required:
        return "user:web"

    if settings.web_auth_mode == "header":
        remote_user = request.headers.get("remote-user")
        if remote_user:
            return f"user:{remote_user}"
        raise HTTPException(status_code=401, detail="unauthenticated")

    user = request.session.get("user")
    if user:
        return f"user:{user}"

    # Browsers get sent to the login, API-style callers and htmx get a plain 401.
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        raise HTTPException(
            status_code=303, headers={"Location": f"/auth/login?next={quote(target, safe='')}"}
        )
    raise HTTPException(status_code=401, detail="unauthenticated")


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _decode_jwt_payload(token: str) -> dict:
    """Claims of an ID token received directly from the token endpoint over TLS.

    OIDC Core 3.1.3.7 allows TLS server validation in place of the signature check for
    exactly this case. The issuer, audience, expiry and nonce are still verified below.
    """
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _valid_id_token(claims: dict, settings: Settings, nonce: str) -> bool:
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    return (
        claims.get("iss") == settings.web_oidc_issuer.rstrip("/")
        and settings.web_oidc_client_id in audiences
        and isinstance(claims.get("exp"), (int, float))
        and claims["exp"] > time.time()
        and secrets.compare_digest(str(claims.get("nonce", "")), nonce)
    )


@router.get("/login")
async def login(request: Request, next: str | None = None) -> RedirectResponse:
    settings = get_settings()
    if settings.web_auth_mode != "oidc":
        raise HTTPException(status_code=404)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    request.session["oidc"] = {
        "state": state,
        "nonce": nonce,
        "verifier": verifier,
        "next": _safe_next(next),
    }
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.web_oidc_client_id,
            "redirect_uri": _redirect_uri(settings),
            "scope": "openid profile groups email",
            "state": state,
            "nonce": nonce,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    return RedirectResponse(
        f"{settings.web_oidc_issuer.rstrip('/')}/api/oidc/authorization?{query}", status_code=303
    )


@router.get("/callback", response_model=None)
async def callback(
    request: Request, code: str | None = None, state: str | None = None
) -> RedirectResponse | PlainTextResponse:
    settings = get_settings()
    if settings.web_auth_mode != "oidc":
        raise HTTPException(status_code=404)

    pending = request.session.pop("oidc", None)
    if not pending or not code or not state:
        return PlainTextResponse("Anmeldung fehlgeschlagen.", status_code=400)
    if not secrets.compare_digest(state, pending["state"]):
        return PlainTextResponse("Anmeldung fehlgeschlagen.", status_code=400)

    issuer = settings.web_oidc_issuer.rstrip("/")
    try:
        async with _http_client() as client:
            token_response = await client.post(
                f"{issuer}/api/oidc/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": _redirect_uri(settings),
                    "code_verifier": pending["verifier"],
                },
                auth=(settings.web_oidc_client_id, settings.web_oidc_client_secret),
            )
            token_response.raise_for_status()
            tokens = token_response.json()

            if not _valid_id_token(
                _decode_jwt_payload(tokens["id_token"]), settings, pending["nonce"]
            ):
                return PlainTextResponse("Anmeldung fehlgeschlagen.", status_code=400)

            userinfo_response = await client.get(
                f"{issuer}/api/oidc/userinfo",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            userinfo_response.raise_for_status()
            userinfo = userinfo_response.json()
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        return PlainTextResponse("Anmeldung fehlgeschlagen.", status_code=502)

    username = userinfo.get("preferred_username")
    if not username:
        return PlainTextResponse("Anmeldung fehlgeschlagen.", status_code=403)

    # Tokens are deliberately not kept: the session only has to say who logged in.
    request.session.clear()
    request.session["user"] = username
    return RedirectResponse(pending["next"], status_code=303)


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    settings = get_settings()
    target = f"{settings.web_oidc_issuer.rstrip('/')}/logout" if settings.web_oidc_issuer else "/"
    return RedirectResponse(target, status_code=303)
