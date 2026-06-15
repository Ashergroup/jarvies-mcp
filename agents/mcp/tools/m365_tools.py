"""Microsoft 365 read tools — direct Microsoft Graph REST calls.

These tools call Microsoft Graph (`https://graph.microsoft.com/v1.0`) directly
via httpx, using the per-request user OAuth token supplied as `access_token` —
the same pattern as the Xero/Cin7 tools and the M365 write tools in
`m365_write_tools.py`. They do NOT import or wrap the Zola M365 agent.

Required delegated Graph permissions, per tool:
- m365_search_emails       → Mail.Read
- m365_read_email          → Mail.Read
- m365_search_calendar     → Calendars.Read
- m365_search_sharepoint   → Files.Read.All / Sites.Read.All
- m365_create_email_draft  → Mail.ReadWrite (creates a draft; never sends)

Return shape matches the other integration tools:
    success → {"status": "ok", "source": "m365", "data": {...}}
    failure → {"status": "error", "source": "m365", "error": "..."}
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from agents.mcp.config import get_settings
from agents.mcp.permissions import check_permission
from agents.mcp.tenant_context import use_tenant_context
from agents.mcp.tools.m365_write_tools import (
    _context,
    _err,
    _graph_request,
    _ok,
)

log = logging.getLogger(__name__)


def _limit(value: int | None) -> int:
    settings = get_settings()
    if value is None:
        return min(10, settings.tool_result_limit)
    return max(1, min(int(value), settings.tool_result_limit))


def _strip_html_to_preview(html_or_text: str, max_chars: int = 200) -> str:
    """Cheap HTML-strip used for previews and HTML email bodies."""

    text = re.sub(r"<[^>]+>", " ", html_or_text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _map_email_summary(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "uri": f"mail:///messages/{m.get('id', '')}",
        "subject": m.get("subject") or "(no subject)",
        "sender": ((m.get("from") or {}).get("emailAddress") or {}).get(
            "address", "unknown"
        ),
        "received_at": m.get("receivedDateTime"),
        "is_read": m.get("isRead", False),
        "has_attachments": m.get("hasAttachments", False),
        "preview": _strip_html_to_preview(m.get("bodyPreview", "")),
        "importance": m.get("importance", "normal"),
    }


def _map_event(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "uri": f"calendar:///events/{e.get('id', '')}",
        "subject": e.get("subject") or "(no subject)",
        "organizer": ((e.get("organizer") or {}).get("emailAddress") or {}).get(
            "address", "unknown"
        ),
        "start": (e.get("start") or {}).get("dateTime"),
        "end": (e.get("end") or {}).get("dateTime"),
        "attendees": [
            ((a.get("emailAddress") or {}).get("address", ""))
            for a in (e.get("attendees") or [])
        ],
        "location": (e.get("location") or {}).get("displayName"),
        "is_online": e.get("isOnlineMeeting", False),
    }


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
) -> dict[str, Any]:
    """Search the signed-in user's Outlook mailbox via `GET /me/messages`.

    Args:
        query: Free-text `$search` across the mailbox.
        sender: Filter on sender address (partial match via `contains`).
        after_iso: ISO-8601 datetime — only mail received at or after this.
        unread_only: When True, restrict to unread mail.
        limit: Max results, capped at MCP_TOOL_RESULT_LIMIT.

    Returns:
        `{"status": "ok", "data": {"emails": [...], "count": N}}` or an error.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "m365_search_emails", context.permissions
        )
        token = context.access_token
        if not token:
            return _err("No access_token supplied for the Microsoft Graph call")

        size = _limit(limit)
        params: dict[str, Any] = {
            "$top": min(size, 50),
            "$select": (
                "id,subject,from,receivedDateTime,isRead,"
                "hasAttachments,bodyPreview,importance"
            ),
        }
        filters: list[str] = []
        if sender:
            escaped = sender.replace("'", "''")
            filters.append(f"contains(from/emailAddress/address, '{escaped}')")
        if after_iso:
            filters.append(f"receivedDateTime ge {after_iso}")
        if unread_only:
            filters.append("isRead eq false")
        if filters:
            params["$filter"] = " and ".join(filters)

        headers_extra: dict[str, str] = {}
        if query:
            params["$search"] = f'"{query}"'
            headers_extra["ConsistencyLevel"] = "eventual"
        # Graph rejects $orderby alongside contains() filters or $search.
        if not sender and not query:
            params["$orderby"] = "receivedDateTime desc"

        try:
            data = await _graph_request(
                "GET",
                "/me/messages",
                token,
                params=params,
                headers=headers_extra or None,
            )
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph search emails failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")

        emails = [_map_email_summary(m) for m in data.get("value", [])][:size]
        return _ok({"emails": emails, "count": len(emails)})


async def m365_read_email(
    uri: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Read one email body via `GET /me/messages/{id}`.

    Args:
        uri: A `mail:///messages/{id}` URI from `m365_search_emails`.

    Returns:
        `{"status": "ok", "data": {"uri", "subject", "sender",
        "received_at", "body_text"}}` or an error.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "m365_read_email", context.permissions
        )
        token = context.access_token
        if not token:
            return _err("No access_token supplied for the Microsoft Graph call")
        if not uri.startswith("mail:///messages/"):
            return _err(f"Not an email URI: {uri}")
        msg_id = uri.removeprefix("mail:///messages/")

        try:
            m = await _graph_request(
                "GET",
                f"/me/messages/{msg_id}",
                token,
                params={"$select": "id,subject,from,receivedDateTime,body"},
            )
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph read email failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")

        body = m.get("body") or {}
        content = body.get("content", "")
        body_text = (
            _strip_html_to_preview(content, max_chars=100_000)
            if body.get("contentType") == "html"
            else content
        )
        return _ok(
            {
                "uri": uri,
                "subject": m.get("subject") or "(no subject)",
                "sender": ((m.get("from") or {}).get("emailAddress") or {}).get(
                    "address", "unknown"
                ),
                "received_at": m.get("receivedDateTime"),
                "body_text": body_text,
            }
        )


async def m365_search_calendar(
    query: str | None = None,
    after_iso: str | None = None,
    before_iso: str | None = None,
    limit: int = 10,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Search the signed-in user's calendar via `GET /me/events`.

    Graph does not support `$search` on `/me/events`, so a date `$filter` is
    applied server-side and `query` is matched client-side on
    subject/location/organizer.

    Returns:
        `{"status": "ok", "data": {"events": [...], "count": N}}` or an error.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "m365_search_calendar", context.permissions
        )
        token = context.access_token
        if not token:
            return _err("No access_token supplied for the Microsoft Graph call")

        size = _limit(limit)
        params: dict[str, Any] = {
            "$top": min(50, max(size * 5, size)) if query else min(size, 50),
            "$select": "id,subject,organizer,start,end,attendees,location,isOnlineMeeting",
            "$orderby": "start/dateTime asc",
        }
        filters: list[str] = []
        if after_iso:
            filters.append(f"end/dateTime ge '{after_iso}'")
        if before_iso:
            filters.append(f"start/dateTime le '{before_iso}'")
        if filters:
            params["$filter"] = " and ".join(filters)

        try:
            data = await _graph_request("GET", "/me/events", token, params=params)
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph search calendar failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")

        events_raw = data.get("value", [])
        if query:
            q = query.lower()

            def matches(e: dict[str, Any]) -> bool:
                parts = [
                    e.get("subject", "") or "",
                    (e.get("location", {}) or {}).get("displayName", "") or "",
                    ((e.get("organizer", {}) or {}).get("emailAddress", {}) or {}).get(
                        "address", ""
                    )
                    or "",
                ]
                return any(q in p.lower() for p in parts)

            events_raw = [e for e in events_raw if matches(e)]

        events = [_map_event(e) for e in events_raw[:size]]
        return _ok({"events": events, "count": len(events)})


async def m365_search_sharepoint(
    query: str,
    file_type: str | None = None,
    limit: int = 10,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Search SharePoint/OneDrive content via `POST /search/query`.

    Args:
        query: Free-text search across document content/name/metadata.
        file_type: Optional extension filter (without the dot), e.g. `docx`.
        limit: Max results, capped at MCP_TOOL_RESULT_LIMIT.

    Returns:
        `{"status": "ok", "data": {"documents": [...], "count": N}}` or an error.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "m365_search_sharepoint", context.permissions
        )
        token = context.access_token
        if not token:
            return _err("No access_token supplied for the Microsoft Graph call")

        size = _limit(limit)
        payload = {
            "requests": [
                {
                    "entityTypes": ["driveItem"],
                    "query": {"queryString": query},
                    "from": 0,
                    "size": min(size, 50),
                }
            ]
        }
        try:
            data = await _graph_request("POST", "/search/query", token, json=payload)
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph search sharepoint failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")

        results: list[dict[str, Any]] = []
        for response in data.get("value", []):
            for container in response.get("hitsContainers", []):
                for hit in container.get("hits", []):
                    res = hit.get("resource") or {}
                    name = res.get("name", "")
                    if not name:
                        continue
                    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                    if file_type and ext != file_type.lower():
                        continue
                    parent = res.get("parentReference") or {}
                    created_by = res.get("createdBy") or {}
                    results.append(
                        {
                            "uri": f"file:///{parent.get('driveId', 'unknown')}/{res.get('id', '')}",
                            "name": name,
                            "file_type": ext,
                            "author": (created_by.get("user") or {}).get("email"),
                            "modified_at": res.get("lastModifiedDateTime"),
                            "web_url": res.get("webUrl"),
                        }
                    )
                    if len(results) >= size:
                        return _ok({"documents": results, "count": len(results)})
        return _ok({"documents": results, "count": len(results)})


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
    """Create an Outlook draft via `POST /me/messages`. Never sends.

    The message lands in the Drafts folder; sending is a separate action
    (`m365_send_email`). Requires the delegated `Mail.ReadWrite` scope.

    Args:
        to: Non-empty list of recipient email addresses.
        subject: Draft subject.
        body: Plain-text body.
        cc: Optional list of CC recipients.
        in_reply_to_uri: Optional `mail:///messages/{id}` reference, recorded
            on the draft as the `x-in-reply-to-uri` internet header.

    Returns:
        `{"status": "ok", "data": {"uri", "web_url", "to", "subject"}}` or an
        error.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "m365_create_email_draft", context.permissions
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

        try:
            created = await _graph_request("POST", "/me/messages", token, json=message)
        except httpx.HTTPStatusError as exc:
            return _err(f"Graph create draft failed: HTTP {exc.response.status_code}")
        except httpx.RequestError as exc:
            return _err(f"Graph request failed: {exc.__class__.__name__}")

        draft_id = created.get("id", "")
        return _ok(
            {
                "uri": f"mail:///messages/{draft_id}" if draft_id else "",
                "web_url": created.get("webLink"),
                "to": to,
                "subject": subject,
            }
        )


def register(mcp: Any) -> None:
    """Register M365 read MCP tools."""

    mcp.tool()(m365_search_emails)
    mcp.tool()(m365_read_email)
    mcp.tool()(m365_search_sharepoint)
    mcp.tool()(m365_search_calendar)
    mcp.tool()(m365_create_email_draft)
