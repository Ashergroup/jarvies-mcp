"""Permission checks for all MCP tools.

The MCP layer is intentionally closed by default. A client must supply the
domain-specific permission needed by the tool, or `admin_access`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


class MCPPermissionError(PermissionError):
    """Raised when a tenant/user is not allowed to run an MCP tool."""


@dataclass(frozen=True)
class ToolPolicy:
    """Permission policy for one MCP tool."""

    required_all: set[str] = field(default_factory=set)
    required_any: set[str] = field(default_factory=set)
    write: bool = False


TOOL_POLICIES: dict[str, ToolPolicy] = {
    "hello": ToolPolicy(),
    "m365_search_emails": ToolPolicy(required_any={"m365_access"}),
    "m365_read_email": ToolPolicy(required_any={"m365_access"}),
    "m365_search_sharepoint": ToolPolicy(required_any={"m365_access"}),
    "m365_search_calendar": ToolPolicy(required_any={"m365_access"}),
    "m365_create_email_draft": ToolPolicy(required_any={"m365_access"}, write=True),
    "m365_send_email": ToolPolicy(required_any={"m365_access"}, write=True),
    "m365_create_calendar_event": ToolPolicy(required_any={"m365_access"}, write=True),
    "m365_upload_to_sharepoint": ToolPolicy(required_any={"m365_access"}, write=True),
    "m365_create_sharepoint_folder": ToolPolicy(
        required_any={"m365_access"}, write=True
    ),
    "m365_post_teams_message": ToolPolicy(required_any={"m365_access"}, write=True),
    "xero_get_contacts": ToolPolicy(required_any={"finance_access"}),
    "xero_get_invoices": ToolPolicy(required_any={"finance_access"}),
    "xero_get_payments": ToolPolicy(required_any={"finance_access"}),
    "xero_create_invoice": ToolPolicy(required_any={"finance_access"}, write=True),
    "xero_get_profit_loss": ToolPolicy(required_any={"finance_access"}),
    "cin7_get_inventory": ToolPolicy(required_any={"finance_access"}),
    "cin7_get_stock_levels": ToolPolicy(required_any={"finance_access"}),
    "cin7_get_sales_orders": ToolPolicy(required_any={"finance_access"}),
    "cin7_get_purchase_orders": ToolPolicy(required_any={"finance_access"}),
    "freshsales_get_contacts": ToolPolicy(required_any={"freshsales_access"}),
    "freshsales_get_accounts": ToolPolicy(required_any={"freshsales_access"}),
    "freshsales_get_deals": ToolPolicy(required_any={"freshsales_access"}),
    "freshsales_search": ToolPolicy(required_any={"freshsales_access"}),
    "powerbi_list_reports": ToolPolicy(required_any={"finance_access"}),
    "powerbi_get_report": ToolPolicy(required_any={"finance_access"}),
    "powerbi_run_query": ToolPolicy(required_any={"finance_access"}),
    "finance_list_systems": ToolPolicy(required_any={"finance_access"}),
    "finance_get_integration_status": ToolPolicy(required_any={"finance_access"}),
    "db_read_query": ToolPolicy(required_any={"read_only"}),
    "db_select": ToolPolicy(required_any={"read_only"}),
    "clickup_list_tasks": ToolPolicy(required_any={"fundraising_access"}),
    "clickup_get_task": ToolPolicy(required_any={"fundraising_access"}),
    "clickup_get_tasks_needing_work": ToolPolicy(
        required_any={"fundraising_access"}
    ),
    "clickup_list_subtasks": ToolPolicy(required_any={"fundraising_access"}),
    "clickup_compute_pipeline_totals": ToolPolicy(
        required_any={"fundraising_access"}
    ),
    "clickup_update_task_field": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "clickup_set_status": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "clickup_link_tasks": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "clickup_add_comment": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "clickup_create_subtask": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "clickup_complete_subtask": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "clickup_reopen_subtask": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
}


def normalise_permissions(permissions: list[str] | str | set[str] | None) -> set[str]:
    """Return permissions as a normalized set of strings."""

    if permissions is None:
        return set()
    if isinstance(permissions, str):
        return {part.strip() for part in permissions.split(",") if part.strip()}
    return {str(part).strip() for part in permissions if str(part).strip()}


def check_permission(
    tenant_id: str,
    user_id: str,
    tool_name: str,
    permissions: list[str] | str | set[str] | None = None,
) -> bool:
    """Validate that a tenant/user can execute a tool.

    Args:
        tenant_id: Client tenant identifier.
        user_id: User or service principal identifier.
        tool_name: MCP tool name.
        permissions: Caller permissions such as `m365_access`, `read_only`,
            `finance_access`, or `admin_access`.

    Returns:
        True when the call is allowed.

    Raises:
        MCPPermissionError: If the tool is unknown or the caller lacks access.
    """

    granted = normalise_permissions(permissions)
    policy = TOOL_POLICIES.get(tool_name)
    if policy is None:
        log.warning("permission_unknown_tool", extra={"tool": tool_name, "tenant_id": tenant_id})
        raise MCPPermissionError(f"Unknown MCP tool policy: {tool_name}")

    if "admin_access" in granted:
        log.info("permission_allowed_admin", extra={"tool": tool_name, "tenant_id": tenant_id})
        return True

    if policy.write and "read_only" in granted:
        log.warning(
            "permission_denied_readonly_write",
            extra={"tool": tool_name, "tenant_id": tenant_id, "user_id": user_id},
        )
        raise MCPPermissionError(f"{tool_name} requires write permission; caller is read_only")

    if policy.required_all and not policy.required_all.issubset(granted):
        missing = sorted(policy.required_all - granted)
        raise MCPPermissionError(f"{tool_name} missing permissions: {', '.join(missing)}")

    if policy.required_any and not policy.required_any.intersection(granted):
        needed = ", ".join(sorted(policy.required_any))
        log.warning(
            "permission_denied_missing_scope",
            extra={"tool": tool_name, "tenant_id": tenant_id, "user_id": user_id},
        )
        raise MCPPermissionError(f"{tool_name} requires one of: {needed}")

    log.info("permission_allowed", extra={"tool": tool_name, "tenant_id": tenant_id})
    return True
