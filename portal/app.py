"""Portal route aggregation.

``build_portal_routes`` returns every portal ``Route`` for mounting into the
existing MCP Starlette app (see ``agents.mcp.server``). There is no separate
uvicorn app — the portal shares the one server and is exempted from the MCP auth
middleware by the ``/portal`` path prefix (it enforces its own sessions).
"""

from __future__ import annotations

from starlette.routing import Route

from portal.sales import get_sales_routes


def build_portal_routes() -> list[Route]:
    """Return all portal routes (sales portal today; tenant portal on Day 5)."""

    return [*get_sales_routes()]
