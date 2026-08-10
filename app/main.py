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

# path="/" here + app.mount("/mcp", mcp_app) below is deliberate: FastMCP registers its route
# relative to the sub-app's own root, so mounting at "/mcp" is what makes the final route
# "/mcp" (not "/mcp/mcp"). Verified against a running TestClient, not assumed.
mcp_app = mcp.http_app(path="/", middleware=[Middleware(BearerAuthMiddleware)])


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
app.mount("/mcp", mcp_app)
