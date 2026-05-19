"""Power BI MCP tools and service layer placeholders."""

from __future__ import annotations

from typing import Any

from agents.mcp.permissions import check_permission
from agents.mcp.tenant_context import build_tenant_context, use_tenant_context


class PowerBIService:
    """Service boundary for future Power BI REST/XMLA integration."""

    async def list_reports(self, workspace_id: str | None = None) -> dict[str, Any]:
        return _not_configured("powerbi_list_reports", {"workspace_id": workspace_id})

    async def get_report(self, report_id: str) -> dict[str, Any]:
        return _not_configured("powerbi_get_report", {"report_id": report_id})

    async def run_query(self, dataset_id: str, dax_query: str) -> dict[str, Any]:
        return _not_configured(
            "powerbi_run_query",
            {"dataset_id": dataset_id, "query_chars": len(dax_query)},
        )


def _not_configured(tool_name: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "status": "not_configured",
        "tool": tool_name,
        "message": "Power BI integration is scaffolded but no API client is configured yet.",
        "details": details or {},
    }


def _context(
    tenant_id: str | None,
    user_id: str | None,
    access_token: str | None,
    permissions: list[str] | None,
):
    return build_tenant_context(
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        permissions=permissions,
    )


async def powerbi_list_reports(
    workspace_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """List Power BI reports once Power BI credentials are configured."""

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "powerbi_list_reports",
            context.permissions,
        )
        return await PowerBIService().list_reports(workspace_id=workspace_id)


async def powerbi_get_report(
    report_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch Power BI report metadata by report id."""

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "powerbi_get_report",
            context.permissions,
        )
        return await PowerBIService().get_report(report_id=report_id)


async def powerbi_run_query(
    dataset_id: str,
    dax_query: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Run a Power BI DAX query once the Power BI service is configured."""

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "powerbi_run_query",
            context.permissions,
        )
        return await PowerBIService().run_query(dataset_id=dataset_id, dax_query=dax_query)


def register(mcp: Any) -> None:
    """Register Power BI MCP tools."""

    mcp.tool()(powerbi_list_reports)
    mcp.tool()(powerbi_get_report)
    mcp.tool()(powerbi_run_query)
