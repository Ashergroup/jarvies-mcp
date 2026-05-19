"""Future finance-system MCP tools."""

from __future__ import annotations

from typing import Any

from agents.mcp.permissions import check_permission
from agents.mcp.tenant_context import build_tenant_context, use_tenant_context

SUPPORTED_SYSTEMS = ["xero", "cin7", "powerbi", "postgresql", "future_finance_systems"]


async def finance_list_systems(
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """List finance and operations systems planned for this MCP platform."""

    context = build_tenant_context(
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        permissions=permissions,
    )
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "finance_list_systems",
            context.permissions,
        )
        return {"systems": SUPPORTED_SYSTEMS, "status": "scaffolded"}


async def finance_get_integration_status(
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return high-level integration status for finance-related modules."""

    context = build_tenant_context(
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        permissions=permissions,
    )
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "finance_get_integration_status",
            context.permissions,
        )
        return {
            "xero": "placeholder",
            "cin7": "placeholder",
            "powerbi": "placeholder",
            "postgresql": "ready_when_database_url_is_set",
        }


def register(mcp: Any) -> None:
    """Register future finance MCP tools."""

    mcp.tool()(finance_list_systems)
    mcp.tool()(finance_get_integration_status)
