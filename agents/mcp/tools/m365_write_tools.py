"""Microsoft 365 write tools — direct Microsoft Graph REST calls.

These tools do NOT wrap the Zola M365 agent. They call Microsoft Graph
(`https://graph.microsoft.com/v1.0`) directly using the per-request user
OAuth token supplied via `access_token` — the same pattern every other
Jarvies tool uses. The read tools in `m365_tools.py` call Graph directly too
and share the helpers in this module. Zola will later be rewired to call
Jarvies for these operations.

Auth: each tool needs the signed-in user's delegated Graph access token
passed as `access_token`. The Azure AD app registration must have the
delegated permission listed on each tool granted and admin-consented,
otherwise Graph returns 403 and the tool returns `{"status": "error", ...}`.

Required delegated Graph permissions, per tool:
- m365_send_email             → Mail.Send
- m365_create_calendar_event  → Calendars.ReadWrite
- m365_upload_to_sharepoint   → Files.ReadWrite.All
- m365_create_sharepoint_folder → Files.ReadWrite.All
- m365_post_teams_message     → ChannelMessage.Send (channel) / Chat.ReadWrite (chat)

Return shape matches the other integration tools:
    success → {"status": "ok", "source": "m365", "data": {...}}
    failure → {"status": "error", "source": "m365", "error": "..."}
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any

import httpx

from agents.mcp.config import get_settings
from agents.mcp.permissions import check_permission
from agents.mcp.tenant_context import build_tenant_context, use_tenant_context

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    """Build a success envelope matching the other integration tools."""

    return {"status": "ok", "source": "m365", "data": data}


def _err(message: str) -> dict[str, Any]:
    """Build an error envelope matching the other integration tools."""

    return {"status": "error", "source": "m365", "error": message}


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


async def _graph_request(
    method: str,
    path: str,
    token: str,
    *,
    json: dict[str, Any] | None = None,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Issue one Microsoft Graph request and return the parsed JSON body.

    Raises `httpx.HTTPStatusError` on non-2xx and `httpx.RequestError` on
    transport failure; callers translate those into the error envelope. The
    access token is never logged.
    """

    settings = get_settings()
    url = f"{GRAPH_BASE}/{path.lstrip('/')}"
    request_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if json is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)

    async with httpx.AsyncClient(
        timeout=settings.integration_http_timeout_seconds
    ) as client:
        started = time.perf_counter()
        response = await client.request(
            method,
            url,
            json=json,
            content=content,
            headers=request_headers,
            params={k: v for k, v in (params or {}).items() if v is not None},
        )
        latency_ms = (time.perf_counter() - started) * 1000
        log.info(
            "m365_graph_call",
            extra={
                "method": method,
                "path": f"/{path.lstrip('/')}",
                "status": response.status_code,
                "latency_ms": round(latency_ms, 1),
            },
        )
        response.raise_for_status()
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}


def _encode_share_url(url: str) -> str:
    """Encode a sharing/absolute URL into a Graph share id (`u!<base64url>`)."""

    b64 = base64.b64encode(url.encode("utf-8")).decode("ascii")
    return "u!" + b64.rstrip("=").replace("/", "_").replace("+", "-")


async def _resolve_drive_item(url: str, token: str) -> tuple[str, str]:
    """Resolve a SharePoint folder URL to its (drive_id, item_id).

    Graph's path-based drive endpoints need a drive id and item id, but the
    public tool surface only takes a folder URL. We resolve the URL through
    `/shares/{id}/driveItem`, which returns the item plus its parentReference
    (carrying the drive id). Returns empty strings if either id is absent.
    """

    share_id = _encode_share_url(url)
    item = await _graph_request(
        "GET",
        f"/shares/{share_id}/driveItem",
        token,
        params={"$select": "id,name,parentReference"},
    )
    drive_id = (item.get("parentReference") or {}).get("driveId", "")
    item_id = item.get("id", "")
    return drive_id, item_id


async def m365_send_email(
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
    """Send an email via Microsoft Graph `POST /me/sendMail`.

    Unlike `m365_create_email_draft`, this delivers the message immediately
    and saves a copy to Sent Items. Requires the delegated `Mail.Send` scope.

    Args:
        to: Non-empty list of recipient email addresses.
        subject: Email subject.
        body: Plain-text body.
        cc: Optional list of CC recipients.
        in_reply_to_uri: Optional `mail:///messages/{id}` URI of the message
            this is a reply to. `/me/sendMail` does not thread natively, so the
            value is recorded on the sent message as the custom internet header
            `x-in-reply-to-uri` for traceability.

    Returns:
        `{"status": "ok", "data": {"sent": True, "to", "subject"}}` or an
        error envelope.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "m365_send_email", context.permissions
        )
        token = context.access_token
        if not token:
            return _err("No access_token supplied for the Microsoft Graph call")
        if not isinstance(to, list) or not to:
            return _err("to must be a non-empty list of email addresses")

        message: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        }
        if cc:
            message["ccRecipients"] = [{"emailAddress": {"address": a}} for a in cc]
        if in_reply_to_uri:
            message["internetMessageHeaders"] = [
                {"name": "x-in-reply-to-uri", "value": in_reply_to_uri}
            ]
        payload = {"message": message, "saveToSentItems": True}

        try:
            await _graph_request("POST", "/me/sendMail", token, json=payload)
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph sendMail failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")
        return _ok({"sent": True, "to": to, "subject": subject})


async def m365_create_calendar_event(
    subject: str,
    start_iso: str,
    end_iso: str,
    attendees: list[str] | None = None,
    body: str | None = None,
    location: str | None = None,
    is_online_meeting: bool = False,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a calendar event via Microsoft Graph `POST /me/events`.

    Requires the delegated `Calendars.ReadWrite` scope.

    Args:
        subject: Event title.
        start_iso: Event start as an ISO-8601 datetime, treated as UTC.
        end_iso: Event end as an ISO-8601 datetime, treated as UTC.
        attendees: Optional list of attendee email addresses (added as
            required attendees).
        body: Optional HTML body / description.
        location: Optional location display name.
        is_online_meeting: When True, requests a Teams online meeting.

    Returns:
        `{"status": "ok", "data": {"event_id", "web_url", "subject"}}` or an
        error envelope.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "m365_create_calendar_event",
            context.permissions,
        )
        token = context.access_token
        if not token:
            return _err("No access_token supplied for the Microsoft Graph call")

        event: dict[str, Any] = {
            "subject": subject,
            "start": {"dateTime": start_iso, "timeZone": "UTC"},
            "end": {"dateTime": end_iso, "timeZone": "UTC"},
        }
        if body:
            event["body"] = {"contentType": "HTML", "content": body}
        if location:
            event["location"] = {"displayName": location}
        if attendees:
            event["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"} for a in attendees
            ]
        if is_online_meeting:
            event["isOnlineMeeting"] = True
            event["onlineMeetingProvider"] = "teamsForBusiness"

        try:
            created = await _graph_request("POST", "/me/events", token, json=event)
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph create event failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")
        return _ok(
            {
                "event_id": created.get("id", ""),
                "web_url": created.get("webLink"),
                "subject": subject,
            }
        )


async def m365_upload_to_sharepoint(
    file_path: str,
    destination_folder_url: str,
    file_name: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Upload a local file to a SharePoint folder via Microsoft Graph.

    Requires the delegated `Files.ReadWrite.All` scope. The destination folder
    URL is resolved to its drive/item ids via `/shares/{id}/driveItem`, then
    the bytes are uploaded with
    `PUT /drives/{drive-id}/items/{folder-id}:/{name}:/content`. Suitable for
    simple uploads (small files); very large files would need an upload
    session, which is out of scope here.

    Args:
        file_path: Local filesystem path to the file to upload.
        destination_folder_url: SharePoint folder sharing/absolute URL.
        file_name: Optional name for the uploaded file; defaults to the local
            file's basename.

    Returns:
        `{"status": "ok", "data": {"item_id", "name", "web_url"}}` or an error
        envelope.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "m365_upload_to_sharepoint",
            context.permissions,
        )
        token = context.access_token
        if not token:
            return _err("No access_token supplied for the Microsoft Graph call")

        name = file_name or os.path.basename(file_path)
        if not name:
            return _err("Could not determine an upload file name")
        try:
            with open(file_path, "rb") as handle:
                content = handle.read()
        except OSError as exc:
            return _err(f"Could not read local file {file_path!r}: {exc.__class__.__name__}")

        try:
            drive_id, folder_id = await _resolve_drive_item(destination_folder_url, token)
            if not drive_id or not folder_id:
                return _err(
                    "Could not resolve destination folder from "
                    f"{destination_folder_url!r}"
                )
            uploaded = await _graph_request(
                "PUT",
                f"/drives/{drive_id}/items/{folder_id}:/{name}:/content",
                token,
                content=content,
                headers={"Content-Type": "application/octet-stream"},
            )
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph upload failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")
        return _ok(
            {
                "item_id": uploaded.get("id", ""),
                "name": uploaded.get("name", name),
                "web_url": uploaded.get("webUrl"),
            }
        )


async def m365_create_sharepoint_folder(
    parent_folder_url: str,
    folder_name: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a folder in SharePoint via Microsoft Graph.

    Requires the delegated `Files.ReadWrite.All` scope. The parent folder URL
    is resolved to its drive/item ids via `/shares/{id}/driveItem`, then the
    folder is created with `POST /drives/{drive-id}/items/{parent-id}/children`.

    Args:
        parent_folder_url: SharePoint parent folder sharing/absolute URL.
        folder_name: Name of the folder to create.

    Returns:
        `{"status": "ok", "data": {"folder_id", "name", "web_url"}}` or an
        error envelope.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "m365_create_sharepoint_folder",
            context.permissions,
        )
        token = context.access_token
        if not token:
            return _err("No access_token supplied for the Microsoft Graph call")

        try:
            drive_id, parent_id = await _resolve_drive_item(parent_folder_url, token)
            if not drive_id or not parent_id:
                return _err(
                    f"Could not resolve parent folder from {parent_folder_url!r}"
                )
            payload = {
                "name": folder_name,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "fail",
            }
            created = await _graph_request(
                "POST",
                f"/drives/{drive_id}/items/{parent_id}/children",
                token,
                json=payload,
            )
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph create folder failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")
        return _ok(
            {
                "folder_id": created.get("id", ""),
                "name": created.get("name", folder_name),
                "web_url": created.get("webUrl"),
            }
        )


async def m365_post_teams_message(
    channel_or_chat_id: str,
    message: str,
    subject: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Post a message to a Teams channel or chat via Microsoft Graph.

    Routing on `channel_or_chat_id`:
    - `"{team-id}/{channel-id}"` (contains a slash) → channel post via
      `POST /teams/{team-id}/channels/{channel-id}/messages`. Requires the
      delegated `ChannelMessage.Send` scope; `subject` is honoured.
    - otherwise treated as a chat id →
      `POST /chats/{chat-id}/messages`. Requires the delegated
      `Chat.ReadWrite` scope; `subject` is ignored (chats have no subject).

    Args:
        channel_or_chat_id: Either `"{team-id}/{channel-id}"` for a channel,
            or a chat id for a 1:1/group chat.
        message: Message body (sent as HTML content).
        subject: Optional subject; applies to channel posts only.

    Returns:
        `{"status": "ok", "data": {"message_id", "web_url"}}` or an error
        envelope.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "m365_post_teams_message",
            context.permissions,
        )
        token = context.access_token
        if not token:
            return _err("No access_token supplied for the Microsoft Graph call")

        payload: dict[str, Any] = {"body": {"contentType": "html", "content": message}}
        if "/" in channel_or_chat_id:
            team_id, channel_id = channel_or_chat_id.split("/", 1)
            if subject:
                payload["subject"] = subject
            path = f"/teams/{team_id}/channels/{channel_id}/messages"
        else:
            path = f"/chats/{channel_or_chat_id}/messages"

        try:
            created = await _graph_request("POST", path, token, json=payload)
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph post message failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")
        return _ok(
            {
                "message_id": created.get("id", ""),
                "web_url": created.get("webUrl"),
            }
        )


def register(mcp: Any) -> None:
    """Register M365 write MCP tools."""

    mcp.tool()(m365_send_email)
    mcp.tool()(m365_create_calendar_event)
    mcp.tool()(m365_upload_to_sharepoint)
    mcp.tool()(m365_create_sharepoint_folder)
    mcp.tool()(m365_post_teams_message)
