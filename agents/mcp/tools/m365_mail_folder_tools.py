"""Microsoft 365 mailbox-folder, SharePoint-folder, and Teams tools.

Same pattern as `m365_tools.py` / `m365_write_tools.py`: every tool calls
Microsoft Graph (`https://graph.microsoft.com/v1.0`) directly via httpx, using
the per-request user OAuth token resolved by
`m365_write_tools._get_m365_token`. No Zola dependency.

Signature: required business params first, then optional `access_token`,
`permissions`, `tenant_id`, `user_id`. Return shape matches the other
integration tools:
    success → {"status": "ok", "source": "m365", "data": {...}}
    failure → {"status": "error", "source": "m365", "error": "..."}

Required *delegated* Graph permissions (Azure scopes list — Kuda grants these
on the app registration manually; not yet consented):
- m365_list_mail_folders       → Mail.ReadWrite
- m365_create_mail_folder      → Mail.ReadWrite
- m365_move_email              → Mail.ReadWrite
- m365_list_sharepoint_folders → Files.Read.All
- m365_search_teams_chat       → Chat.Read
- m365_create_teams_channel    → Channel.Create

NEW scopes this module needs that the existing tools did not already require:
    Mail.ReadWrite, Chat.Read, Channel.Create
(Files.Read.All is implied by the existing Files.ReadWrite.All.)

Graph limitations found (see report / docstrings):
- `GET /chats/{id}/messages` does not support `$search`/`$filter` on body
  content, so `m365_search_teams_chat` filters client-side after fetching.
- Private Teams channel creation is provisioned asynchronously by Graph; the
  returned channel id may not be immediately queryable.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from agents.mcp.permissions import check_permission
from agents.mcp.tenant_context import use_tenant_context
from agents.mcp.tools.m365_tools import _strip_html_to_preview
from agents.mcp.tools.m365_write_tools import (
    _NO_TOKEN_MESSAGE,
    _context,
    _encode_share_url,
    _err,
    _get_m365_token,
    _graph_request,
    _ok,
)

log = logging.getLogger(__name__)

# Defensive cap on how many chats/messages we fan out over in a single search.
_MAX_CHATS_SCANNED = 50
_MAX_MESSAGES_PER_CHAT = 50


def _map_mail_folder(f: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f.get("id", ""),
        "displayName": f.get("displayName", ""),
        "totalItemCount": f.get("totalItemCount", 0),
        "unreadItemCount": f.get("unreadItemCount", 0),
    }


async def m365_list_mail_folders(
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """List the signed-in user's mail folders via `GET /me/mailFolders`.

    Requires the delegated `Mail.ReadWrite` scope.

    Returns:
        `{"status": "ok", "data": {"folders": [{"id", "displayName",
        "totalItemCount", "unreadItemCount"}], "count": N}}` or an error.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "m365_list_mail_folders", context.permissions
        )
        token = await _get_m365_token(
            context.access_token, context.user_id, context.tenant_id
        )
        if not token:
            return _err(_NO_TOKEN_MESSAGE)

        try:
            data = await _graph_request(
                "GET", "/me/mailFolders", token, params={"$top": 50}
            )
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph list mail folders failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")

        folders = [_map_mail_folder(f) for f in data.get("value", [])]
        return _ok({"folders": folders, "count": len(folders)})


async def m365_create_mail_folder(
    folder_name: str,
    parent_folder_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a mail folder via `POST /me/mailFolders`.

    Requires the delegated `Mail.ReadWrite` scope.

    Args:
        folder_name: Display name for the new folder.
        parent_folder_id: Optional parent folder id. When given, the folder is
            created as a subfolder via `/me/mailFolders/{id}/childFolders`.

    Returns:
        `{"status": "ok", "data": {"id", "displayName", "parent_folder_id"}}`
        or an error.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "m365_create_mail_folder", context.permissions
        )
        token = await _get_m365_token(
            context.access_token, context.user_id, context.tenant_id
        )
        if not token:
            return _err(_NO_TOKEN_MESSAGE)
        if not folder_name:
            return _err("folder_name is required")

        path = (
            f"/me/mailFolders/{parent_folder_id}/childFolders"
            if parent_folder_id
            else "/me/mailFolders"
        )
        payload = {"displayName": folder_name}

        try:
            created = await _graph_request("POST", path, token, json=payload)
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph create mail folder failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")

        return _ok(
            {
                "id": created.get("id", ""),
                "displayName": created.get("displayName", folder_name),
                "parent_folder_id": parent_folder_id,
            }
        )


async def m365_move_email(
    message_uri: str,
    destination_folder_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Move an email to another folder via `POST /me/messages/{id}/move`.

    Requires the delegated `Mail.ReadWrite` scope. The move creates a copy in
    the destination folder and returns a new message id.

    Args:
        message_uri: A `mail:///messages/{id}` URI (as produced by the read
            tools).
        destination_folder_id: Target folder id from `m365_list_mail_folders`.

    Returns:
        `{"status": "ok", "data": {"uri", "message_id", "destination_folder_id",
        "moved": True}}` or an error.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "m365_move_email", context.permissions
        )
        token = await _get_m365_token(
            context.access_token, context.user_id, context.tenant_id
        )
        if not token:
            return _err(_NO_TOKEN_MESSAGE)
        if not message_uri.startswith("mail:///messages/"):
            return _err(f"Not an email URI: {message_uri}")
        if not destination_folder_id:
            return _err("destination_folder_id is required")
        msg_id = message_uri.removeprefix("mail:///messages/")

        try:
            moved = await _graph_request(
                "POST",
                f"/me/messages/{msg_id}/move",
                token,
                json={"destinationId": destination_folder_id},
            )
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph move email failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")

        new_id = moved.get("id", "")
        return _ok(
            {
                "uri": f"mail:///messages/{new_id}" if new_id else message_uri,
                "message_id": new_id,
                "destination_folder_id": destination_folder_id,
                "moved": True,
            }
        )


async def m365_list_sharepoint_folders(
    folder_url: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """List child folders of a SharePoint folder via Microsoft Graph.

    Requires the delegated `Files.Read.All` scope. The folder URL is resolved
    through `/shares/{id}/driveItem/children`; only child items that are
    folders are returned.

    Args:
        folder_url: Full SharePoint folder sharing/absolute URL.

    Returns:
        `{"status": "ok", "data": {"folders": [{"name", "id", "web_url",
        "created_at"}], "count": N}}` or an error.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "m365_list_sharepoint_folders", context.permissions
        )
        token = await _get_m365_token(
            context.access_token, context.user_id, context.tenant_id
        )
        if not token:
            return _err(_NO_TOKEN_MESSAGE)
        if not folder_url:
            return _err("folder_url is required")

        share_id = _encode_share_url(folder_url)
        try:
            data = await _graph_request(
                "GET",
                f"/shares/{share_id}/driveItem/children",
                token,
                params={"$select": "id,name,webUrl,createdDateTime,folder"},
            )
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph list sharepoint folders failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")

        folders = [
            {
                "name": item.get("name", ""),
                "id": item.get("id", ""),
                "web_url": item.get("webUrl"),
                "created_at": item.get("createdDateTime"),
            }
            for item in data.get("value", [])
            if "folder" in item
        ]
        return _ok({"folders": folders, "count": len(folders)})


async def m365_search_teams_chat(
    query: str,
    limit: int = 10,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Search the signed-in user's Teams chat messages for `query`.

    Requires the delegated `Chat.Read` scope. Lists the user's chats via
    `GET /me/chats?$expand=members`, then reads each chat's messages via
    `GET /chats/{id}/messages`.

    Graph limitation: `/chats/{id}/messages` does not support `$search` on
    message body, so the query is matched client-side (case-insensitive
    substring on the message text) after fetching.

    Args:
        query: Text to match within chat message bodies.
        limit: Max matching messages to return (default 10).

    Returns:
        `{"status": "ok", "data": {"messages": [...], "count": N}}` or an error.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "m365_search_teams_chat", context.permissions
        )
        token = await _get_m365_token(
            context.access_token, context.user_id, context.tenant_id
        )
        if not token:
            return _err(_NO_TOKEN_MESSAGE)
        if not query:
            return _err("query is required")

        size = max(1, int(limit))
        needle = query.lower()

        try:
            chats_data = await _graph_request(
                "GET", "/me/chats", token, params={"$expand": "members"}
            )
            results: list[dict[str, Any]] = []
            for chat in chats_data.get("value", [])[:_MAX_CHATS_SCANNED]:
                if len(results) >= size:
                    break
                chat_id = chat.get("id")
                if not chat_id:
                    continue
                msgs = await _graph_request(
                    "GET",
                    f"/chats/{chat_id}/messages",
                    token,
                    params={"$top": _MAX_MESSAGES_PER_CHAT},
                )
                for m in msgs.get("value", []):
                    content = (m.get("body") or {}).get("content", "") or ""
                    if needle not in content.lower():
                        continue
                    sender = ((m.get("from") or {}).get("user") or {}).get("displayName")
                    results.append(
                        {
                            "chat_id": chat_id,
                            "message_id": m.get("id", ""),
                            "from": sender,
                            "created_at": m.get("createdDateTime"),
                            "preview": _strip_html_to_preview(content),
                            "web_url": m.get("webUrl"),
                        }
                    )
                    if len(results) >= size:
                        break
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph search teams chat failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")

        return _ok({"messages": results, "count": len(results)})


async def m365_create_teams_channel(
    team_id: str,
    channel_name: str,
    description: str | None = None,
    is_private: bool = False,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a Teams channel via `POST /teams/{team-id}/channels`.

    Requires the delegated `Channel.Create` scope. Private channels are
    provisioned asynchronously by Graph; the returned id may not be immediately
    queryable.

    Args:
        team_id: The team (group) id to create the channel in.
        channel_name: Display name for the channel.
        description: Optional channel description.
        is_private: When True, creates a private channel (membershipType
            `private`); otherwise a standard channel.

    Returns:
        `{"status": "ok", "data": {"id", "displayName", "web_url",
        "membership_type"}}` or an error.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "m365_create_teams_channel", context.permissions
        )
        token = await _get_m365_token(
            context.access_token, context.user_id, context.tenant_id
        )
        if not token:
            return _err(_NO_TOKEN_MESSAGE)
        if not team_id:
            return _err("team_id is required")
        if not channel_name:
            return _err("channel_name is required")

        membership_type = "private" if is_private else "standard"
        payload: dict[str, Any] = {
            "displayName": channel_name,
            "membershipType": membership_type,
        }
        if description:
            payload["description"] = description

        try:
            created = await _graph_request(
                "POST", f"/teams/{team_id}/channels", token, json=payload
            )
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph create teams channel failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")

        return _ok(
            {
                "id": created.get("id", ""),
                "displayName": created.get("displayName", channel_name),
                "web_url": created.get("webUrl"),
                "membership_type": created.get("membershipType", membership_type),
            }
        )


def register(mcp: Any) -> None:
    """Register M365 mailbox-folder, SharePoint-folder, and Teams tools."""

    mcp.tool()(m365_list_mail_folders)
    mcp.tool()(m365_create_mail_folder)
    mcp.tool()(m365_move_email)
    mcp.tool()(m365_list_sharepoint_folders)
    mcp.tool()(m365_search_teams_chat)
    mcp.tool()(m365_create_teams_channel)
