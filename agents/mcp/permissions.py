"""Permission checks for all MCP tools.

The MCP layer is intentionally closed by default. A client must supply the
domain-specific permission needed by the tool, or `admin_access`.

Domain scopes: `m365_access`, `finance_access`, `freshsales_access`,
`fundraising_access`, `db_access`, `support_access`.

`read_only` is NOT a domain scope. It is a write ceiling: a caller that passes
it is refused every `write=True` tool (unless it also holds `admin_access`), and
it grants nothing on its own.
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
    "m365_upload_base64_to_sharepoint": ToolPolicy(
        required_any={"m365_access"}, write=True
    ),
    "m365_download_file": ToolPolicy(required_any={"m365_access"}),
    "m365_create_sharepoint_folder": ToolPolicy(
        required_any={"m365_access"}, write=True
    ),
    "m365_post_teams_message": ToolPolicy(required_any={"m365_access"}, write=True),
    "m365_list_mail_folders": ToolPolicy(required_any={"m365_access"}),
    "m365_create_mail_folder": ToolPolicy(required_any={"m365_access"}, write=True),
    "m365_move_email": ToolPolicy(required_any={"m365_access"}, write=True),
    "m365_list_sharepoint_folders": ToolPolicy(required_any={"m365_access"}),
    "m365_search_teams_chat": ToolPolicy(required_any={"m365_access"}),
    "m365_create_teams_channel": ToolPolicy(required_any={"m365_access"}, write=True),
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
    "freshsales_create_contact": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "freshsales_update_contact": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "freshsales_create_deal": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "freshsales_update_deal": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "freshsales_create_account": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "freshsales_create_note": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "freshsales_create_task": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "freshsales_get_deal_stages": ToolPolicy(required_any={"fundraising_access"}),
    "freshsales_get_contact_journey": ToolPolicy(required_any={"fundraising_access"}),
    "freshsales_search_contacts": ToolPolicy(required_any={"fundraising_access"}),
    # Freshdesk (helpdesk) — read-only family under its own support_access scope,
    # kept separate from the Freshsales (CRM) scopes above.
    "freshdesk_list_tickets": ToolPolicy(required_any={"support_access"}),
    "freshdesk_get_ticket": ToolPolicy(required_any={"support_access"}),
    "freshdesk_search_tickets": ToolPolicy(required_any={"support_access"}),
    "freshdesk_list_agents": ToolPolicy(required_any={"support_access"}),
    "freshdesk_get_ticket_summary": ToolPolicy(required_any={"support_access"}),
    "freshdesk_list_groups": ToolPolicy(required_any={"support_access"}),
    # Freshdesk writes. Every one mutates a live ticket — a reply and a public
    # note reach the customer — so all are write=True and refused to a
    # read_only caller before the reply guardrail is ever consulted.
    "freshdesk_reply_to_ticket": ToolPolicy(
        required_any={"support_access"}, write=True
    ),
    "freshdesk_add_note": ToolPolicy(required_any={"support_access"}, write=True),
    "freshdesk_update_ticket": ToolPolicy(required_any={"support_access"}, write=True),
    "freshdesk_assign_ticket": ToolPolicy(required_any={"support_access"}, write=True),
    "freshdesk_escalate": ToolPolicy(required_any={"support_access"}, write=True),
    "powerbi_list_reports": ToolPolicy(required_any={"finance_access"}),
    "powerbi_get_report": ToolPolicy(required_any={"finance_access"}),
    "powerbi_run_query": ToolPolicy(required_any={"finance_access"}),
    "finance_list_systems": ToolPolicy(required_any={"finance_access"}),
    "finance_get_integration_status": ToolPolicy(required_any={"finance_access"}),
    # `db_access` is the domain scope for the PostgreSQL tools. It replaced
    # `read_only`, which is not a domain scope at all but the write ceiling
    # applied below — granting it was never meant to be the way to reach the DB
    # tools, and it stopped working the moment `read_only` left
    # MCP_DEFAULT_PERMISSIONS to unblock the write tools.
    "db_read_query": ToolPolicy(required_any={"db_access"}),
    "db_select": ToolPolicy(required_any={"db_access"}),
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
    "clickup_get_spaces": ToolPolicy(required_any={"fundraising_access"}),
    "clickup_get_folders": ToolPolicy(required_any={"fundraising_access"}),
    "clickup_get_members": ToolPolicy(required_any={"fundraising_access"}),
    "clickup_get_lists": ToolPolicy(required_any={"fundraising_access"}),
    "clickup_list_tasks_by_id": ToolPolicy(required_any={"fundraising_access"}),
    "clickup_create_folder": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "clickup_create_list": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "clickup_create_space": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "clickup_delete_task": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "clickup_create_form": ToolPolicy(
        required_any={"fundraising_access"}, write=True
    ),
    "clickup_create_task": ToolPolicy(
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
        permissions: Caller permissions — one or more domain scopes such as
            `m365_access`, `finance_access`, or `db_access`; optionally
            `read_only` to cap the call at reads; or `admin_access`, which
            bypasses every check.

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
