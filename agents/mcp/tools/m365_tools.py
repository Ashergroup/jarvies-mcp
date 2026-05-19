"""MCP wrappers for the existing Microsoft 365 agent tools.

These wrappers import and call the existing `agents.m365` functions at runtime.
They do not duplicate business logic and they do not expose send/delete tools.
"""

from __future__ import annotations

import importlib
import logging
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

from anyio import to_thread

from agents.mcp.config import get_settings
from agents.mcp.permissions import check_permission
from agents.mcp.tenant_context import build_tenant_context, use_tenant_context

log = logging.getLogger(__name__)
_M365_CALL_LOCK = threading.RLock()


def _ensure_existing_agent_importable() -> None:
    """Add the existing M365 agent project to Python's import path if present."""

    settings = get_settings()
    agent_path = settings.m365_agent_path
    if not agent_path:
        return

    agent_path = Path(agent_path)
    if not agent_path.exists():
        return

    path_text = str(agent_path)
    if path_text not in sys.path:
        sys.path.insert(0, path_text)

    agents_dir = agent_path / "agents"
    if agents_dir.exists():
        try:
            import agents as agents_pkg

            agents_path = getattr(agents_pkg, "__path__", None)
            if agents_path is not None and str(agents_dir) not in list(agents_path):
                agents_path.append(str(agents_dir))
        except Exception:
            log.exception("m365_agents_package_path_extension_failed")


def _load_existing_module(module_name: str) -> ModuleType:
    _ensure_existing_agent_importable()
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # pragma: no cover - exact failure depends on local env.
        raise RuntimeError(
            "Could not import the existing M365 agent module. Set M365_AGENT_PATH "
            "to the project containing agents/m365, and provide any environment "
            "variables required by that existing bot package."
        ) from exc


def _load_read_tools() -> ModuleType:
    return _load_existing_module("agents.m365.tools.read_tools")


def _load_write_tools() -> ModuleType:
    return _load_existing_module("agents.m365.tools.write_tools")


@contextmanager
def _m365_call_scope(
    access_token: str | None,
    write_tools: ModuleType | None = None,
) -> Iterator[None]:
    """Temporarily supply an OAuth token to the existing M365 tool modules."""

    connector = _load_existing_module("agents.m365.tools.connector")
    previous_active_token = getattr(connector, "_active_bot_token", None)
    previous_acquire_token: Callable[..., str] | None = None

    if hasattr(connector, "set_active_token"):
        connector.set_active_token(access_token)

    if access_token and write_tools is not None and hasattr(write_tools, "acquire_token"):
        previous_acquire_token = write_tools.acquire_token
        write_tools.acquire_token = lambda *_, **__: access_token  # type: ignore[method-assign]

    try:
        yield
    finally:
        if previous_acquire_token is not None and write_tools is not None:
            write_tools.acquire_token = previous_acquire_token  # type: ignore[method-assign]
        if hasattr(connector, "set_active_token"):
            connector.set_active_token(previous_active_token)


def _limit(value: int | None) -> int:
    settings = get_settings()
    if value is None:
        return min(10, settings.tool_result_limit)
    return max(1, min(int(value), settings.tool_result_limit))


async def m365_search_emails(
    query: str | None = None,
    sender: str | None = None,
    after_iso: str | None = None,
    unread_only: bool = False,
    limit: int = 10,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search Outlook email by wrapping the existing M365 `search_emails` tool."""

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
            "m365_search_emails",
            context.permissions,
        )
        read_tools = _load_read_tools()

        def call_existing() -> list[dict[str, Any]]:
            with _M365_CALL_LOCK, _m365_call_scope(context.access_token):
                return read_tools.search_emails(
                    query=query,
                    sender=sender,
                    after_iso=after_iso,
                    unread_only=unread_only,
                    limit=_limit(limit),
                )

        return await to_thread.run_sync(call_existing)


async def m365_read_email(
    uri: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Read a single Outlook email body using the existing M365 read tool."""

    context = build_tenant_context(
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        permissions=permissions,
    )
    with use_tenant_context(context):
        check_permission(context.tenant_id, context.user_id, "m365_read_email", context.permissions)
        read_tools = _load_read_tools()

        def call_existing() -> dict[str, Any]:
            with _M365_CALL_LOCK, _m365_call_scope(context.access_token):
                return read_tools.read_email_body(uri)

        return await to_thread.run_sync(call_existing)


async def m365_search_sharepoint(
    query: str,
    file_type: str | None = None,
    limit: int = 10,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search SharePoint by wrapping the existing M365 `search_sharepoint` tool."""

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
            "m365_search_sharepoint",
            context.permissions,
        )
        read_tools = _load_read_tools()

        def call_existing() -> list[dict[str, Any]]:
            with _M365_CALL_LOCK, _m365_call_scope(context.access_token):
                return read_tools.search_sharepoint(
                    query=query,
                    file_type=file_type,
                    limit=_limit(limit),
                )

        return await to_thread.run_sync(call_existing)


async def m365_search_calendar(
    query: str | None = None,
    after_iso: str | None = None,
    before_iso: str | None = None,
    limit: int = 10,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search Outlook Calendar by wrapping the existing M365 `search_calendar` tool."""

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
            "m365_search_calendar",
            context.permissions,
        )
        read_tools = _load_read_tools()

        def call_existing() -> list[dict[str, Any]]:
            with _M365_CALL_LOCK, _m365_call_scope(context.access_token):
                return read_tools.search_calendar(
                    query=query,
                    after_iso=after_iso,
                    before_iso=before_iso,
                    limit=_limit(limit),
                )

        return await to_thread.run_sync(call_existing)


async def m365_create_email_draft(
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    in_reply_to_uri: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Create an Outlook draft using the existing M365 `create_draft` tool.

    This wrapper does not send email. It keeps the existing external-recipient
    safety behavior and runs in unattended mode by default for cloud use.
    """

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
            "m365_create_email_draft",
            context.permissions,
        )
        write_tools = _load_write_tools()

        def call_existing() -> dict[str, Any]:
            with _M365_CALL_LOCK, _m365_call_scope(context.access_token, write_tools=write_tools):
                if hasattr(write_tools, "set_mode"):
                    write_tools.set_mode(
                        unattended=get_settings().m365_draft_unattended,
                        read_only=False,
                    )
                return write_tools.create_draft(
                    to=to,
                    subject=subject,
                    body=body,
                    cc=cc,
                    in_reply_to_uri=in_reply_to_uri,
                )

        return await to_thread.run_sync(call_existing)


def register(mcp: Any) -> None:
    """Register M365 MCP wrappers."""

    mcp.tool()(m365_search_emails)
    mcp.tool()(m365_read_email)
    mcp.tool()(m365_search_sharepoint)
    mcp.tool()(m365_search_calendar)
    mcp.tool()(m365_create_email_draft)
