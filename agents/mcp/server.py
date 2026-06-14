"""FastMCP server entrypoint for Jarvies, the M365 Agent MCP layer."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from agents.mcp.auth import MCPAuthMiddleware, log_production_safety_warnings
from agents.mcp.config import get_settings
from agents.mcp.database import close_pool, init_pool
from agents.mcp.oauth import register_oauth_routes
from agents.mcp.tenant import TenantResolutionMiddleware
from agents.mcp.tool_registry import register_all_tools


def configure_logging() -> None:
    """Configure structured-enough logging for local and cloud runtime."""

    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )


configure_logging()
log = logging.getLogger(__name__)
log_production_safety_warnings()

mcp = FastMCP(
    "jarvies",
    json_response=True,
)


@mcp.tool()
def hello(name: str) -> str:
    """Return a simple greeting used to validate MCP connectivity."""

    return f"Hello {name}"


register_all_tools(mcp)

app = mcp.streamable_http_app()

# Tenant resolution runs inside auth (added first => inner layer). Auth is the
# outermost middleware so unauthenticated requests are rejected before any DB
# tenant lookup happens.
app.add_middleware(TenantResolutionMiddleware)
app.add_middleware(MCPAuthMiddleware)


# Wrap FastMCP's own lifespan (it manages the MCP session manager) so the DB
# pool is opened on startup and closed on shutdown without displacing it. Pool
# init is best-effort: if DATABASE_URL is unset or the DB is unreachable we log
# and continue, leaving tenant features off and env-var credentials in force.
_mcp_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan(app_: Any) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.database_url:
        try:
            await init_pool()
        except Exception:
            log.exception("db_pool_init_failed_continuing")
    else:
        log.warning("database_url_not_set_tenant_features_disabled")
    try:
        async with _mcp_lifespan(app_):
            yield
    finally:
        await close_pool()


app.router.lifespan_context = _lifespan


async def health(_: Any) -> JSONResponse:
    """Health check endpoint for Docker and AWS App Runner."""

    settings = get_settings()
    return JSONResponse(
        {
            "status": "ok",
            "service": "jarvies",
            "environment": settings.environment,
            "mcp_endpoint": "/mcp",
        }
    )


app.add_route("/health", health, methods=["GET"])

# OAuth 2.0 authorization-server endpoints (Phase 2B). These paths are public
# (listed in auth.PUBLIC_PATHS) so the discovery/flow can run before a token
# exists.
register_oauth_routes(app)

log.info("mcp_server_ready", extra={"endpoint": "/mcp"})
