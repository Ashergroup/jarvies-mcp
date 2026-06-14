"""FastMCP server entrypoint for Jarvies, the M365 Agent MCP layer."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

from agents.mcp.auth import MCPAuthMiddleware, log_production_safety_warnings
from agents.mcp.config import get_settings
from agents.mcp.database import close_pool, init_pool
from agents.mcp.oauth import register_oauth_routes
from agents.mcp.proxy import ForwardedProtoMiddleware
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

_settings = get_settings()

# FastMCP's DNS-rebinding protection rejects POST /mcp with 421 "Invalid Host"
# unless the request Host is allowlisted. Behind ECS the public domain
# (ja-...on.aws) is not localhost, so it must be added; the allowlist is derived
# from JARVIES_PUBLIC_URL / AZURE_REDIRECT_URI plus MCP_ALLOWED_HOSTS. Protection
# stays on unless explicitly disabled.
mcp = FastMCP(
    "jarvies",
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=not _settings.disable_dns_rebinding_protection,
        allowed_hosts=_settings.allowed_host_values,
        allowed_origins=_settings.allowed_origin_values,
    ),
)
log.info(
    "mcp_transport_security_configured",
    extra={
        "dns_rebinding_protection": not _settings.disable_dns_rebinding_protection,
        "allowed_hosts": _settings.allowed_host_values,
    },
)


@mcp.tool()
def hello(name: str) -> str:
    """Return a simple greeting used to validate MCP connectivity."""

    return f"Hello {name}"


register_all_tools(mcp)

app = mcp.streamable_http_app()

# FastMCP mounts the Streamable HTTP endpoint at "/mcp" (no trailing slash).
# Clients that POST to "/mcp/" would otherwise hit Starlette's redirect_slashes
# and get a 307 to "/mcp" — and behind TLS-terminating ECS/App Runner that
# Location came back as http://, downgrading the scheme and breaking the client.
# Serve both paths directly and turn the slash redirect off so no redirect is
# emitted at all. (ForwardedProtoMiddleware below also keeps any URL we generate
# on https.)
_mcp_route = next(r for r in app.router.routes if getattr(r, "path", None) == "/mcp")
app.router.routes.append(Route("/mcp/", endpoint=_mcp_route.endpoint))
app.router.redirect_slashes = False

# claude.ai's connector POSTs MCP to the server root ("/") — the URL the user
# enters — rather than "/mcp". The Streamable HTTP transport has no discovery
# field that redirects the MCP path (the endpoint IS the configured URL), so
# serve the same handler at "/" too. Root stays auth-protected (it is NOT in
# auth.PUBLIC_PATHS) so an unauthenticated POST / returns 401, which is what
# drives claude.ai into the OAuth discovery handshake.
app.router.routes.append(Route("/", endpoint=_mcp_route.endpoint))

# Tenant resolution runs inside auth (added first => inner layer). Auth wraps it.
# ForwardedProtoMiddleware is added last so it is the OUTERMOST layer: it fixes
# scope["scheme"] from X-Forwarded-Proto before routing, auth, or the OAuth
# discovery handlers build any absolute URL.
app.add_middleware(TenantResolutionMiddleware)
app.add_middleware(MCPAuthMiddleware)
app.add_middleware(ForwardedProtoMiddleware)


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
