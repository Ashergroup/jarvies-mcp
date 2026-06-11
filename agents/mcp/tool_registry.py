"""Tool registration for the MCP server."""

from __future__ import annotations

import logging
from typing import Any

from agents.mcp.tools import (
    cin7_tools,
    clickup_tools,
    db_tools,
    finance_tools,
    freshsales_tools,
    m365_tools,
    powerbi_tools,
    xero_tools,
)

log = logging.getLogger(__name__)


def register_all_tools(mcp: Any) -> None:
    """Register all MCP tool modules against a FastMCP instance."""

    for module in (
        m365_tools,
        xero_tools,
        cin7_tools,
        freshsales_tools,
        powerbi_tools,
        finance_tools,
        db_tools,
        clickup_tools,
    ):
        module.register(mcp)
        log.info("mcp_tools_registered", extra={"tool_module": module.__name__})
