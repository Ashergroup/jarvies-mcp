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
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from agents.mcp.config import get_settings
from agents.mcp.database import get_conn
from agents.mcp.permissions import check_permission
from agents.mcp.tenant import current_user_id
from agents.mcp.tenant_context import build_tenant_context, use_tenant_context

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Returned when no Microsoft access token can be resolved for a tool call.
_NO_TOKEN_MESSAGE = "No M365 access token available — please reconnect via OAuth"

# Microsoft identity-platform token endpoint (per-tenant). Used to refresh an
# expired delegated access token from a stored refresh token.
_MS_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

# Delegated scopes requested on refresh — the write/read surface this module
# needs, plus offline_access so Microsoft keeps issuing refresh tokens.
_REFRESH_SCOPE = (
    "offline_access Mail.ReadWrite Mail.Send Calendars.ReadWrite "
    "Files.ReadWrite.All Sites.ReadWrite.All ChannelMessage.Send Chat.ReadWrite"
)

# Refresh proactively once the stored token is within this window of expiry.
_REFRESH_SKEW_SECONDS = 300


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


async def _lookup_user_token(user_id: str) -> str | None:
    """Return the stored Microsoft access token for a user, or None.

    Reads the row persisted at OAuth-callback time (see
    ``agents.mcp.oauth._persist_identity``) from ``user_tokens``. Never raises:
    a missing or unreachable database degrades to None so the caller surfaces
    the standard reconnect error.
    """

    try:
        async with get_conn() as conn:
            row = await conn.fetchrow(
                "SELECT access_token FROM user_tokens "
                "WHERE user_id::text = $1 ORDER BY updated_at DESC LIMIT 1",
                user_id,
            )
    except Exception:
        log.warning("m365_token_lookup_failed")
        return None
    if row is None:
        return None
    return row["access_token"] or None


async def _lookup_user_token_record(user_id: str) -> dict[str, Any] | None:
    """Return the stored Microsoft token row for a user, or None.

    Unlike ``_lookup_user_token`` (which yields only the access token), this
    returns the access token plus the ``refresh_token`` and ``expires_at``
    needed to drive a proactive refresh. Never raises: a missing or unreachable
    database degrades to None so the caller falls back to its other paths.
    """

    try:
        async with get_conn() as conn:
            row = await conn.fetchrow(
                "SELECT access_token, refresh_token, expires_at FROM user_tokens "
                "WHERE user_id::text = $1 ORDER BY updated_at DESC LIMIT 1",
                user_id,
            )
    except Exception:
        log.warning("m365_token_lookup_failed")
        return None
    if row is None:
        return None
    return dict(row)


def _token_is_stale(expires_at: datetime | None) -> bool:
    """True when a stored token is expired or within the refresh skew window."""

    if expires_at is None:
        return False
    return expires_at <= datetime.now(UTC) + timedelta(seconds=_REFRESH_SKEW_SECONDS)


async def _persist_refreshed_token(
    user_id: str,
    access_token: str,
    refresh_token: str,
    expires_in: int | None,
) -> None:
    """Write a refreshed M365 token set back to ``user_tokens``.

    Mirrors the Xero rotation write-back in
    ``credentials.persist_xero_refresh_token``: best-effort, so any failure is
    logged and swallowed and the in-flight Graph call still proceeds with the
    new in-memory token. Token values are never logged.
    """

    expires_at = None
    if expires_in:
        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
    try:
        async with get_conn() as conn:
            await conn.execute(
                """
                UPDATE user_tokens
                SET access_token = $1, refresh_token = $2, expires_at = $3,
                    updated_at = NOW()
                WHERE user_id::text = $4
                """,
                access_token,
                refresh_token,
                expires_at,
                user_id,
            )
        log.info("m365_token_refreshed", extra={"user_id": user_id})
    except Exception:
        log.warning(
            "m365_token_refresh_persist_failed — token kept in memory for this call",
            extra={"user_id": user_id},
        )


async def _maybe_refresh_token(
    user_id: str,
    record: dict[str, Any],
    *,
    force: bool = False,
) -> str | None:
    """Proactively refresh a stored access token, returning the new one.

    Refreshes when the stored token is expired/near-expiry, or when ``force``
    (a Graph 401 retry). On success, persists the rotated access/refresh tokens
    back to ``user_tokens`` and returns the new access token. On any failure —
    no refresh token, missing Azure config, a non-2xx token response, transport
    error — logs a warning and returns None so the caller keeps using the
    existing token. Never breaks the tool call.
    """

    refresh_token = record.get("refresh_token")
    if not refresh_token:
        return None
    if not force and not _token_is_stale(record.get("expires_at")):
        return None

    settings = get_settings()
    if not (
        settings.azure_client_id
        and settings.azure_client_secret
        and settings.azure_tenant_id
    ):
        return None

    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": settings.azure_client_id,
        "client_secret": settings.azure_client_secret,
        "scope": _REFRESH_SCOPE,
    }
    try:
        async with httpx.AsyncClient(
            timeout=settings.integration_http_timeout_seconds
        ) as client:
            response = await client.post(
                _MS_TOKEN_URL.format(tenant=settings.azure_tenant_id), data=form
            )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        log.warning(
            "m365_token_refresh_failed — using existing token",
            extra={"user_id": user_id},
        )
        return None

    new_access = payload.get("access_token")
    if not new_access:
        log.warning(
            "m365_token_refresh_missing_access_token — using existing token",
            extra={"user_id": user_id},
        )
        return None
    # Microsoft may or may not rotate the refresh token; reuse the old one when
    # it does not return a fresh value.
    new_refresh = payload.get("refresh_token") or refresh_token
    await _persist_refreshed_token(
        user_id, new_access, new_refresh, payload.get("expires_in")
    )
    return new_access


async def _get_m365_token(
    access_token: str | None,
    user_id: str | None,
    tenant_id: str | None,
    *,
    force_refresh: bool = False,
) -> str | None:
    """Resolve the Microsoft access token for an M365 tool call.

    Priority:
      1. An explicitly supplied ``access_token`` (backward compatible — the
         existing test/Claude-Desktop path). Explicit tokens are returned as-is
         and never refreshed.
      2. The token stored for the authenticated user at OAuth-callback time.
         The user is taken from the request's bearer-token identity
         (``tenant.current_user_id``), or an explicit non-default ``user_id``.
         The stored token is proactively refreshed when expired or within five
         minutes of expiry — or unconditionally when ``force_refresh`` is set
         (the one-shot retry path after a Graph 401). A failed refresh falls
         back to the existing token (best effort).

    Returns None when neither is available; callers then return
    ``_NO_TOKEN_MESSAGE``. ``tenant_id`` is accepted for call-site symmetry.
    """

    if access_token:
        return access_token

    effective_user_id = current_user_id()
    if not effective_user_id and user_id and user_id != get_settings().default_user_id:
        effective_user_id = user_id
    if not effective_user_id:
        return None

    record = await _lookup_user_token_record(effective_user_id)
    if record is not None:
        refreshed = await _maybe_refresh_token(
            effective_user_id, record, force=force_refresh
        )
        if refreshed:
            return refreshed
        if record.get("access_token"):
            return record["access_token"]

    # Fall back to the access-token-only lookup (keeps partial rows and the
    # existing token-resolution tests working when no record is available).
    return await _lookup_user_token(effective_user_id)


async def _get_upn(user_id: str | None) -> str | None:
    """Resolve the signed-in user's UPN (email) for `/users/{upn}/...` paths.

    Every M365 mailbox/calendar/chat Graph call must target `/users/{upn}/...`
    rather than `/me/...` so each tool resolves the *same* mailbox object.
    Mixing the two draws message ids and folder ids from different mailbox
    contexts, which are not interchangeable — e.g. `m365_move_email` fails
    because a message id from `m365_search_emails` and a folder id from
    `m365_list_mail_folders` came from different mailbox contexts.

    The user is resolved the same way as ``_get_m365_token``: the request's
    bearer-token identity (``tenant.current_user_id``), or an explicit
    non-default ``user_id``. The email is then read from the ``users`` table.

    Returns the email string, or None when no user identity is available, the
    lookup fails, or the row has no email. A None return is logged as a warning
    because callers then fall back to `/me/`, which reintroduces the
    cross-mailbox-context risk this helper exists to remove.
    """

    effective_user_id = current_user_id()
    if not effective_user_id and user_id and user_id != get_settings().default_user_id:
        effective_user_id = user_id
    if not effective_user_id:
        log.warning("m365_upn_no_identity — falling back to /me/")
        return None

    try:
        async with get_conn() as conn:
            row = await conn.fetchrow(
                "SELECT email FROM users WHERE id::text = $1",
                effective_user_id,
            )
    except Exception:
        log.warning("m365_upn_lookup_failed — falling back to /me/")
        return None
    if row is None or not row["email"]:
        log.warning("m365_upn_not_found — falling back to /me/")
        return None
    return row["email"]


def _mailbox_base(upn: str | None) -> str:
    """Build the Graph mailbox base path: `/users/{upn}` or `/me` fallback."""

    return f"/users/{upn}" if upn else "/me"


async def _retry_on_401(context: Any, token: str, attempt: Any) -> Any:
    """Run ``attempt(token)``; on a Graph 401 refresh the token and retry once.

    ``attempt`` is an async callable that performs the tool's Graph request(s)
    with the supplied token. A 401 means the access token was rejected (expired
    or revoked) after resolution; we re-resolve with ``force_refresh`` — which
    rotates the stored token via ``_get_m365_token`` — and retry exactly once.
    Any other status, or a second failure, propagates to the caller's handler.
    Never loops.
    """

    try:
        return await attempt(token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 401:
            raise
    new_token = await _get_m365_token(
        context.access_token,
        context.user_id,
        context.tenant_id,
        force_refresh=True,
    )
    return await attempt(new_token or token)


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
    """Send an email via Microsoft Graph `POST /users/{upn}/sendMail`.

    Unlike `m365_create_email_draft`, this delivers the message immediately
    and saves a copy to Sent Items. Requires the delegated `Mail.Send` scope.

    Args:
        to: Non-empty list of recipient email addresses.
        subject: Email subject.
        body: Plain-text body.
        cc: Optional list of CC recipients.
        in_reply_to_uri: Optional `mail:///messages/{id}` URI of the message
            this is a reply to. `sendMail` does not thread natively, so the
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
        token = await _get_m365_token(
            context.access_token, context.user_id, context.tenant_id
        )
        if not token:
            return _err(_NO_TOKEN_MESSAGE)
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

        mbox = _mailbox_base(await _get_upn(context.user_id))

        async def _attempt(tok: str) -> dict[str, Any]:
            return await _graph_request("POST", f"{mbox}/sendMail", tok, json=payload)

        try:
            await _retry_on_401(context, token, _attempt)
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
    """Create a calendar event via Microsoft Graph `POST /users/{upn}/events`.

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
        token = await _get_m365_token(
            context.access_token, context.user_id, context.tenant_id
        )
        if not token:
            return _err(_NO_TOKEN_MESSAGE)

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

        mbox = _mailbox_base(await _get_upn(context.user_id))

        async def _attempt(tok: str) -> dict[str, Any]:
            return await _graph_request("POST", f"{mbox}/events", tok, json=event)

        try:
            created = await _retry_on_401(context, token, _attempt)
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
        token = await _get_m365_token(
            context.access_token, context.user_id, context.tenant_id
        )
        if not token:
            return _err(_NO_TOKEN_MESSAGE)

        name = file_name or os.path.basename(file_path)
        if not name:
            return _err("Could not determine an upload file name")
        try:
            with open(file_path, "rb") as handle:
                content = handle.read()
        except OSError as exc:
            return _err(f"Could not read local file {file_path!r}: {exc.__class__.__name__}")

        async def _attempt(tok: str) -> dict[str, Any] | None:
            drive_id, folder_id = await _resolve_drive_item(destination_folder_url, tok)
            if not drive_id or not folder_id:
                return None
            return await _graph_request(
                "PUT",
                f"/drives/{drive_id}/items/{folder_id}:/{name}:/content",
                tok,
                content=content,
                headers={"Content-Type": "application/octet-stream"},
            )

        try:
            uploaded = await _retry_on_401(context, token, _attempt)
            if uploaded is None:
                return _err(
                    "Could not resolve destination folder from "
                    f"{destination_folder_url!r}"
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
        token = await _get_m365_token(
            context.access_token, context.user_id, context.tenant_id
        )
        if not token:
            return _err(_NO_TOKEN_MESSAGE)

        payload = {
            "name": folder_name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }

        async def _attempt(tok: str) -> dict[str, Any] | None:
            drive_id, parent_id = await _resolve_drive_item(parent_folder_url, tok)
            if not drive_id or not parent_id:
                return None
            return await _graph_request(
                "POST",
                f"/drives/{drive_id}/items/{parent_id}/children",
                tok,
                json=payload,
            )

        try:
            created = await _retry_on_401(context, token, _attempt)
            if created is None:
                return _err(
                    f"Could not resolve parent folder from {parent_folder_url!r}"
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
        token = await _get_m365_token(
            context.access_token, context.user_id, context.tenant_id
        )
        if not token:
            return _err(_NO_TOKEN_MESSAGE)

        payload: dict[str, Any] = {"body": {"contentType": "html", "content": message}}
        if "/" in channel_or_chat_id:
            team_id, channel_id = channel_or_chat_id.split("/", 1)
            if subject:
                payload["subject"] = subject
            path = f"/teams/{team_id}/channels/{channel_id}/messages"
        else:
            path = f"/chats/{channel_or_chat_id}/messages"

        async def _attempt(tok: str) -> dict[str, Any]:
            return await _graph_request("POST", path, tok, json=payload)

        try:
            created = await _retry_on_401(context, token, _attempt)
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
