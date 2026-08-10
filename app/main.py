from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware import Middleware

from app.core.logging import configure_logging
from app.core.security import BearerAuthMiddleware
from app.mcp.server import mcp
from app.web.routes import router as web_router

configure_logging()

# path="/mcp" here + app.mount("/", mcp_app) below (registered LAST, see bottom of file) is
# deliberate, and the OPPOSITE of the first version of this file. That version used
# path="/" + mount("/mcp", ...): a request to the bare "/mcp" (no trailing slash) then had to
# be redirected by Starlette to "/mcp/" so the sub-app's root route would match -- real MCP
# clients (verified: Claude's own connector) don't follow that redirect and just report the
# call as failed. Registering the sub-app's own route directly at "/mcp" and mounting it at
# "/" makes the path match exactly, with no redirect ever involved. Verified against a
# running TestClient with follow_redirects=False, not assumed.
mcp_app = mcp.http_app(path="/mcp", middleware=[Middleware(BearerAuthMiddleware)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # required: this is what actually starts/stops FastMCP's session manager. Omitting it
    # leaves /mcp mounted but silently non-functional.
    async with mcp_app.lifespan(app):
        yield


app = FastAPI(title="memory-service", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


# NOT "/static": that path is reserved by the shared Traefik rule template (proxied to
# Authelia's own consent-screen assets on every MCP host here) -- harmless for services with
# no web UI of their own, but this one has real assets that would collide with Authelia's.
app.mount(
    "/assets", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="assets"
)
app.include_router(web_router)
# Mounted LAST and at "/": Starlette matches routes in registration order, so the concrete
# routes above (healthz, /assets, web_router's paths) are tried first and only requests that
# don't match any of them (i.e. "/mcp") fall through to this mount.
app.mount("/", mcp_app)
