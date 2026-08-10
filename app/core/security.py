"""Bearer-token auth for the /mcp sub-app. Runs as ASGI middleware (not a per-tool FastMCP
dependency) so it works the same regardless of FastMCP's internal tool-calling API -- it's a
plain reverse-proxy-style gate in front of the mounted app, mirroring how Authelia sits in
front of every other self-hosted MCP server here. "dev" mode checks a static token (local
dev only); "oidc" mode introspects against Authelia, exactly like signal-mcp/hero-mcp.
"""

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings


class AuthError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


async def verify_bearer_token(request: Request) -> str:
    """Returns the identified actor (used as git author / entries.updated_by), or raises AuthError."""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise AuthError(401, "missing bearer token")
    token = auth_header[len("bearer ") :].strip()

    settings = get_settings()
    if settings.auth_mode == "dev":
        if token != settings.dev_bearer_token:
            raise AuthError(401, "invalid token")
        return "dev-user"

    if not settings.oidc_introspection_url:
        raise AuthError(500, "OIDC introspection not configured")

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(
                settings.oidc_introspection_url,
                data={"token": token},
                auth=(settings.oidc_client_id, settings.oidc_client_secret),
            )
        except httpx.HTTPError as exc:
            raise AuthError(502, f"introspection request failed: {exc}") from exc

    if resp.status_code != 200:
        raise AuthError(401, "token introspection failed")
    payload = resp.json()
    if not payload.get("active"):
        raise AuthError(401, "inactive token")
    return payload.get("sub") or payload.get("client_id") or "mcp-caller"


class BearerAuthMiddleware:
    """Starlette-style ASGI middleware; rejects unauthenticated HTTP requests before they
    reach the wrapped app. Non-HTTP scopes (lifespan) pass through untouched."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        try:
            actor = await verify_bearer_token(request)
        except AuthError as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["actor"] = actor
        await self.app(scope, receive, send)


def current_actor() -> str:
    """Read the actor stamped by BearerAuthMiddleware onto the current MCP request. Falls
    back to a generic label outside a real HTTP request (in-process test transports have no
    ASGI scope at all) or if the middleware didn't run for some reason -- never crashes the
    tool call over attribution."""
    from fastmcp.server.dependencies import get_http_request

    try:
        request = get_http_request()
    except Exception:
        return "mcp-caller"
    return request.scope.get("state", {}).get("actor", "mcp-caller")
