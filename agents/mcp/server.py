"""FastMCP server entrypoint for Jarvies, the M365 Agent MCP layer."""

from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from agents.mcp.auth import MCPAuthMiddleware, log_production_safety_warnings
from agents.mcp.config import get_settings
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
app.add_middleware(MCPAuthMiddleware)


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
log.info("mcp_server_ready", extra={"endpoint": "/mcp"})
