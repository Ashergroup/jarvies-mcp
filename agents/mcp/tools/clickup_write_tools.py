"""ClickUp workspace-structure write tools — spaces, folders, lists, forms.

Companion to ``clickup_tools.py``. These tools manage ClickUp workspace
structure (spaces / folders / lists / forms / members) rather than fundraising
task content, so they do not need the custom-fields config file the task tools
load — only a ClickUp API token and (for team-scoped calls) the team id.

Pattern matches ``clickup_tools.py`` exactly:
- Per-tenant credentials are resolved DB-first via ``_resolve_settings`` (the
  ``clickup`` row in ``tenant_credentials``), with the ``CLICKUP_API_TOKEN`` /
  ``CLICKUP_TEAM_ID`` env vars as the fallback.
- HTTP runs through ``ClickUpService`` (raw ``Authorization`` token, NOT
  Bearer — a v2 quirk), with the same retry/error handling.
- Return shape: ``{"status": "ok", ...}`` on success, ``{"status": "error",
  "code": <http>, "message": <str>}`` on API error, or
  ``{"status": "not_configured", "missing": [...]}`` when credentials are
  absent. Tools never raise for API errors.

ClickUp API limitations found:
- A ClickUp *space* has no scalar status — it exposes a ``statuses`` array plus
  ``private``/``archived`` flags. ``clickup_get_spaces`` passes through whatever
  ``status`` the API returns (usually absent).
- ``DELETE /task/{id}`` returns an empty body on success; the tool synthesises
  the ``deleted`` envelope.
- A form is created via the generic ``POST /list/{id}/view`` (``type: form``);
  the created view is returned under a ``view`` key.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from agents.mcp.config import MCPSettings
from agents.mcp.permissions import check_permission
from agents.mcp.tenant_context import use_tenant_context
from agents.mcp.tools.clickup_tools import (
    ClickUpAPIError,
    ClickUpService,
    _context,
    _error,
    _not_configured,
    _ok,
    _resolve_settings,
)

log = logging.getLogger(__name__)


class ClickUpWriteService(ClickUpService):
    """Adds workspace-structure endpoints on top of the shared v2 client."""

    async def get_spaces(self, team_id: str) -> dict[str, Any]:
        return await self._request("GET", f"team/{team_id}/space")

    async def get_folders(self, space_id: str) -> dict[str, Any]:
        return await self._request("GET", f"space/{space_id}/folder")

    async def create_folder(self, space_id: str, name: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"space/{space_id}/folder", json_body={"name": name}
        )

    async def create_list_in_folder(
        self, folder_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request("POST", f"folder/{folder_id}/list", json_body=body)

    async def create_list_in_space(
        self, space_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request("POST", f"space/{space_id}/list", json_body=body)

    async def create_space(self, team_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"team/{team_id}/space", json_body=body)

    async def delete_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"task/{task_id}")

    async def get_members(self, list_id: str) -> dict[str, Any]:
        return await self._request("GET", f"list/{list_id}/member")

    async def create_form(self, list_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"list/{list_id}/view", json_body=body)


def _missing_token(settings: MCPSettings) -> list[str]:
    """These tools only require the API token (no custom-fields config)."""

    return [] if settings.clickup_api_token else ["CLICKUP_API_TOKEN"]


async def _run(fn, tool_name: str, settings: MCPSettings) -> dict[str, Any]:
    service = ClickUpWriteService(settings)
    try:
        return await fn(service)
    except ClickUpAPIError as exc:
        log.warning("clickup_api_error", extra={"tool": tool_name, "code": exc.code})
        return _error(exc.code, exc.message)
    except httpx.RequestError as exc:
        log.warning(
            "clickup_request_error",
            extra={"tool": tool_name, "exception": exc.__class__.__name__},
        )
        return _error(0, f"ClickUp request failed: {exc.__class__.__name__}")
    finally:
        await service.aclose()


def _view_of(payload: Any) -> dict[str, Any]:
    """ClickUp returns a created view under a ``view`` key (or bare)."""

    if isinstance(payload, dict):
        view = payload.get("view")
        if isinstance(view, dict):
            return view
        return payload
    return {}


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


async def clickup_get_spaces(
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """List the spaces in the configured team (``GET /team/{id}/space``).

    The team id is resolved from the ``clickup`` tenant credential metadata, or
    the ``CLICKUP_TEAM_ID`` env var as a fallback.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "clickup_get_spaces", context.permissions
        )
        settings = await _resolve_settings()
        missing = _missing_token(settings)
        if missing:
            return _not_configured(missing)
        team_id = settings.clickup_team_id
        if not team_id:
            return _not_configured(["CLICKUP_TEAM_ID"])

        async def _do(service: ClickUpWriteService) -> dict[str, Any]:
            payload = await service.get_spaces(team_id)
            spaces = payload.get("spaces", []) if isinstance(payload, dict) else []
            results = [
                {
                    "id": s.get("id", ""),
                    "name": s.get("name", ""),
                    "status": s.get("status"),
                }
                for s in spaces
            ]
            return _ok(count=len(results), spaces=results)

        return await _run(_do, "clickup_get_spaces", settings)


async def clickup_get_folders(
    space_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """List the folders in a space (``GET /space/{id}/folder``)."""

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "clickup_get_folders", context.permissions
        )
        settings = await _resolve_settings()
        missing = _missing_token(settings)
        if missing:
            return _not_configured(missing)
        if not space_id:
            return _error(400, "space_id is required")

        async def _do(service: ClickUpWriteService) -> dict[str, Any]:
            payload = await service.get_folders(space_id)
            folders = payload.get("folders", []) if isinstance(payload, dict) else []
            results = [
                {
                    "id": f.get("id", ""),
                    "name": f.get("name", ""),
                    "task_count": f.get("task_count"),
                }
                for f in folders
            ]
            return _ok(space_id=space_id, count=len(results), folders=results)

        return await _run(_do, "clickup_get_folders", settings)


async def clickup_get_members(
    list_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """List the members of a list (``GET /list/{id}/member``)."""

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "clickup_get_members", context.permissions
        )
        settings = await _resolve_settings()
        missing = _missing_token(settings)
        if missing:
            return _not_configured(missing)
        if not list_id:
            return _error(400, "list_id is required")

        async def _do(service: ClickUpWriteService) -> dict[str, Any]:
            payload = await service.get_members(list_id)
            members = payload.get("members", []) if isinstance(payload, dict) else []
            results = [
                {
                    "id": m.get("id", ""),
                    "username": m.get("username"),
                    "email": m.get("email"),
                }
                for m in members
            ]
            return _ok(list_id=list_id, count=len(results), members=results)

        return await _run(_do, "clickup_get_members", settings)


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


async def clickup_create_folder(
    space_id: str,
    folder_name: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a folder in a space (``POST /space/{id}/folder``)."""

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "clickup_create_folder", context.permissions
        )
        settings = await _resolve_settings()
        missing = _missing_token(settings)
        if missing:
            return _not_configured(missing)
        if not space_id:
            return _error(400, "space_id is required")
        if not folder_name:
            return _error(400, "folder_name is required")

        async def _do(service: ClickUpWriteService) -> dict[str, Any]:
            created = await service.create_folder(space_id, folder_name)
            return _ok(
                folder_id=created.get("id", ""),
                name=created.get("name", folder_name),
            )

        return await _run(_do, "clickup_create_folder", settings)


async def clickup_create_list(
    list_name: str,
    folder_id: str | None = None,
    space_id: str | None = None,
    status: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a list in a folder (``POST /folder/{id}/list``) or directly in a
    space (``POST /space/{id}/list``).

    Provide ``folder_id`` or ``space_id``. When both are given, ``folder_id``
    wins.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "clickup_create_list", context.permissions
        )
        settings = await _resolve_settings()
        missing = _missing_token(settings)
        if missing:
            return _not_configured(missing)
        if not list_name:
            return _error(400, "list_name is required")
        if not folder_id and not space_id:
            return _error(400, "one of folder_id or space_id is required")

        body: dict[str, Any] = {"name": list_name}
        if status:
            body["status"] = status

        async def _do(service: ClickUpWriteService) -> dict[str, Any]:
            if folder_id:
                created = await service.create_list_in_folder(folder_id, body)
            else:
                created = await service.create_list_in_space(space_id, body)
            return _ok(
                list_id=created.get("id", ""),
                name=created.get("name", list_name),
            )

        return await _run(_do, "clickup_create_list", settings)


async def clickup_create_space(
    space_name: str,
    is_private: bool = False,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a space in the configured team (``POST /team/{id}/space``)."""

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "clickup_create_space", context.permissions
        )
        settings = await _resolve_settings()
        missing = _missing_token(settings)
        if missing:
            return _not_configured(missing)
        team_id = settings.clickup_team_id
        if not team_id:
            return _not_configured(["CLICKUP_TEAM_ID"])
        if not space_name:
            return _error(400, "space_name is required")

        body: dict[str, Any] = {"name": space_name, "private": bool(is_private)}

        async def _do(service: ClickUpWriteService) -> dict[str, Any]:
            created = await service.create_space(team_id, body)
            return _ok(
                space_id=created.get("id", ""),
                name=created.get("name", space_name),
            )

        return await _run(_do, "clickup_create_space", settings)


async def clickup_delete_task(
    task_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Delete a task (``DELETE /task/{id}``)."""

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "clickup_delete_task", context.permissions
        )
        settings = await _resolve_settings()
        missing = _missing_token(settings)
        if missing:
            return _not_configured(missing)
        if not task_id:
            return _error(400, "task_id is required")

        async def _do(service: ClickUpWriteService) -> dict[str, Any]:
            await service.delete_task(task_id)
            return _ok(deleted=True, task_id=task_id)

        return await _run(_do, "clickup_delete_task", settings)


async def clickup_create_form(
    list_id: str,
    form_name: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a form view on a list (``POST /list/{id}/view``, ``type: form``)."""

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "clickup_create_form", context.permissions
        )
        settings = await _resolve_settings()
        missing = _missing_token(settings)
        if missing:
            return _not_configured(missing)
        if not list_id:
            return _error(400, "list_id is required")
        if not form_name:
            return _error(400, "form_name is required")

        body = {"name": form_name, "type": "form"}

        async def _do(service: ClickUpWriteService) -> dict[str, Any]:
            created = await service.create_form(list_id, body)
            view = _view_of(created)
            return _ok(
                form_id=view.get("id", ""),
                name=view.get("name", form_name),
            )

        return await _run(_do, "clickup_create_form", settings)


def register(mcp: Any) -> None:
    """Register ClickUp workspace-structure tools."""

    mcp.tool()(clickup_get_spaces)
    mcp.tool()(clickup_get_folders)
    mcp.tool()(clickup_get_members)
    mcp.tool()(clickup_create_folder)
    mcp.tool()(clickup_create_list)
    mcp.tool()(clickup_create_space)
    mcp.tool()(clickup_delete_task)
    mcp.tool()(clickup_create_form)
