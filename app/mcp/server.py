from fastmcp import FastMCP

mcp = FastMCP("memory-service")

# Importing tools registers them onto `mcp` via @mcp.tool() decorators. Import happens here
# (not at module top of tools.py's own use) to avoid a circular import, since tools.py imports
# `mcp` from this module.
from app.mcp import tools  # noqa: E402,F401
