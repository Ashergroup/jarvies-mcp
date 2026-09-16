"""Hosted Xero OAuth 2.0 authorization-code flow (per-tenant, browser redirect).

Replaces the interactive local dance in ``scripts/xero_auth_setup.py`` for hosted
use: a tenant connects their Xero organisation through two browser routes instead
of someone running a script and pasting values into ``.env``.

* ``GET /oauth/xero/start?tenant_id=<uuid>`` — validate the tenant and its stored
  Xero client id, mint a single-use ``state``, redirect to Xero's authorize
  endpoint.
* ``GET /oauth/xero/callback?code=&state=`` — consume the state, exchange the code
  for tokens, read the connected organisations from ``/connections``, and write
  the refresh token plus the org inventory in ONE transaction.

Structure mirrors ``agents.mcp.admin_consent``: module-level constants, small
named DB/HTTP coroutines so unit tests can monkeypatch them without a live
database or network, and ``get_xero_oauth_routes`` / ``register_xero_oauth_routes``
at the bottom. ``oauth_states`` rows are single-use — read and deleted in one
transaction — and are scoped by ``purpose`` so this flow and the Microsoft
admin-consent flow can never consume each other's state.

Credential storage is the existing ``tenant_credentials`` layout (see
``agents.mcp.credentials._CREDENTIAL_MAP``): the refresh token is the row's
primary secret (encrypted), while ``client_id``, ``client_secret`` and the
selected Xero org GUID live in the ``metadata`` JSONB under ``client_id``,
``client_secret`` and ``tenant_id``. This module never invents new storage.

Two invariants the callback must hold, both of which break a tenant silently if
missed:

1. ``metadata`` is MERGED onto the existing row, never replaced. The upsert's
   ``metadata = EXCLUDED.metadata`` is a wholesale overwrite, so writing a bare
   ``{"tenant_id": ...}`` would destroy ``client_id``/``client_secret`` and break
   every subsequent call immediately after showing a success page.
2. ``metadata["tenant_id"]`` must be set to the selected org GUID. That value is
   what ``XeroService`` sends as the ``Xero-tenant-id`` header on every request
   (``agents.mcp.tools.xero_tools``), so a valid refresh token without it leaves
   all Xero tools non-functional.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from agents.mcp.config import get_settings
from agents.mcp.credentials import upsert_tenant_credentials
from agents.mcp.crypto import CryptoNotConfiguredError
from agents.mcp.database import get_conn

log = logging.getLogger(__name__)

# Xero OAuth2 endpoints. Mirrors scripts/xero_auth_setup.py:56-58 — keep the two
# in step if Xero ever moves them.
AUTHORIZE_BASE = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"

# Granular scopes. Mirrors scripts/xero_auth_setup.py:59-65. Xero apps created
# since March 2026 reject the legacy broad scope names (e.g.
# `accounting.transactions`) as invalid_scope, so the granular names are required.
# NOTE: deliberately NOT settings.xero_scopes — that value carries the legacy
# broad set and is used only by the client_credentials path in xero_tools.
SCOPES = (
    "offline_access "
    "accounting.contacts.read "
    "accounting.invoices.read "
    "accounting.payments.read "
    "accounting.reports.profitandloss.read"
)

START_PATH = "/oauth/xero/start"
CALLBACK_PATH = "/oauth/xero/callback"

STATE_TTL_MINUTES = 10

# oauth_states.purpose value for this flow. The Microsoft admin-consent flow uses
# the column default ('ms_consent'); both flows filter on their own purpose so a
# state minted by one can never be consumed by the other.
STATE_PURPOSE = "xero_oauth"

# Bypass the MCP auth middleware for these two paths. Exact set, not a prefix:
# a prefix bypass would silently make any future /oauth/xero/* route public.
# /start is reached by a browser with no MCP token; /callback is called by Xero.
# Trust comes from the single-use `state`, not from the path being public.
XERO_OAUTH_PUBLIC_PATHS = {START_PATH, CALLBACK_PATH}


# ---------------------------------------------------------------------------
# DB access (small named coroutines — monkeypatched in unit tests)
# ---------------------------------------------------------------------------


async def _fetch_tenant(tenant_id: str) -> dict[str, Any] | None:
    """Return ``{"id", "name", "status"}`` for a tenant, or None if absent."""

    async with get_conn() as conn:
        row = await conn.fetchrow(
            "SELECT id, display_name, status FROM tenants WHERE id::text = $1",
            tenant_id,
        )
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "name": row["display_name"],
        "status": row["status"],
    }


async def _fetch_xero_metadata(tenant_id: str) -> dict[str, Any]:
    """Return the tenant's Xero ``metadata`` JSONB, or ``{}`` when there is no row.

    Deliberately NOT ``tenant.get_tenant_credentials``: that helper swallows DB
    errors and returns None so tool calls can fall back to env vars. Here a DB
    failure must NOT look like "no credentials configured" — silently treating it
    as empty would let the callback write a row that wipes ``client_id`` and
    ``client_secret``. Errors propagate to the caller's error page.

    Only ``metadata`` is read. The existing refresh token (the row's encrypted
    primary secret) is about to be replaced, so it is never decrypted here.
    """

    async with get_conn() as conn:
        row = await conn.fetchrow(
            "SELECT metadata FROM tenant_credentials "
            "WHERE tenant_id::text = $1 AND credential_type = 'xero'",
            tenant_id,
        )
    if row is None:
        return {}
    metadata = row["metadata"]
    if isinstance(metadata, str):
        # asyncpg returns JSONB as str unless a codec is registered.
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    return dict(metadata or {})


async def _store_state(state: str, tenant_id: str, expires_at: datetime) -> None:
    """Persist a single-use state row for this flow.

    The Jarvies tenant uuid rides in ``tenant_hint`` (the existing TEXT column on
    ``oauth_states``) so no new state table is needed. ``purpose`` scopes the row
    to this flow.
    """

    async with get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO oauth_states (state, tenant_hint, purpose, expires_at)
            VALUES ($1, $2, $3, $4)
            """,
            state,
            tenant_id,
            STATE_PURPOSE,
            expires_at,
        )


async def _consume_state(state: str) -> dict[str, Any] | None:
    """Fetch and delete a state row for this flow atomically (single-use).

    Returns the row when the state is known, unexpired, and belongs to THIS flow
    (``purpose = 'xero_oauth'``); otherwise None. Both the SELECT and the DELETE
    filter on ``purpose``, so presenting a Microsoft-consent state here deletes
    nothing and fails closed — that state stays available to its own flow. Any
    matching row is deleted even when expired, so a state can never be reused.
    """

    async with get_conn() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT state, tenant_hint, expires_at FROM oauth_states "
            "WHERE state = $1 AND purpose = $2",
            state,
            STATE_PURPOSE,
        )
        if row is not None:
            await conn.execute(
                "DELETE FROM oauth_states WHERE state = $1 AND purpose = $2",
                state,
                STATE_PURPOSE,
            )
    if row is None:
        return None
    expires_at = row["expires_at"]
    if expires_at is not None and expires_at < datetime.now(UTC):
        return None
    return dict(row)


async def _save_connection(
    tenant_id: str,
    refresh_token: str,
    metadata: dict[str, Any],
    orgs: list[dict[str, str]],
) -> None:
    """Write the refresh token, merged metadata, and org inventory atomically.

    One transaction on one connection so a partial connect is impossible: either
    the tenant has both a usable refresh token and a matching org list, or the
    write is rolled back entirely and the caller shows an error page.

    ``metadata`` must ALREADY be the merged document (existing row's metadata plus
    this flow's additions) — this function writes it as given, and the underlying
    upsert replaces the JSONB wholesale.

    ``orgs`` is ordered; ``orgs[0]`` is the live organisation. Every existing row
    for the tenant is deactivated first so a reconnect returning a shorter list
    cannot leave a stale ``active`` row behind.

    Raises ``CryptoNotConfiguredError`` when no encryption key is configured (via
    ``upsert_tenant_credentials``); the caller turns that into a legible page.
    """

    async with get_conn() as conn, conn.transaction():
        await upsert_tenant_credentials(
            tenant_id,
            {"xero": {"credential_key": refresh_token, "metadata": metadata}},
            conn=conn,
        )
        # Clear previous actives before re-inserting; see docstring.
        await conn.execute(
            "UPDATE xero_client_orgs SET active = false WHERE tenant_id::text = $1",
            tenant_id,
        )
        for index, org in enumerate(orgs):
            await conn.execute(
                """
                INSERT INTO xero_client_orgs
                    (tenant_id, xero_tenant_id, org_name, active)
                VALUES ($1::uuid, $2, $3, $4)
                ON CONFLICT (tenant_id, xero_tenant_id)
                DO UPDATE SET org_name = EXCLUDED.org_name,
                              active = EXCLUDED.active
                """,
                tenant_id,
                org["xero_tenant_id"],
                org.get("org_name"),
                index == 0,
            )


# ---------------------------------------------------------------------------
# Xero HTTP calls (named so tests can monkeypatch them without network access)
# ---------------------------------------------------------------------------


def _parse_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"raw_body": response.text[:2000]}


async def _exchange_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> tuple[int, Any]:
    """Exchange an authorization code for tokens. Returns ``(status, payload)``.

    Mirrors scripts/xero_auth_setup.py:345-354 — HTTP Basic client authentication
    (not form-body credentials, which is what the refresh_token path in
    xero_tools uses). ``redirect_uri`` must byte-match the value sent to the
    authorize endpoint or Xero rejects the exchange.
    """

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    timeout = get_settings().integration_http_timeout_seconds
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    log.info(
        "xero_oauth_token_exchange",
        extra={"status": response.status_code, "grant": "authorization_code"},
    )
    return response.status_code, _parse_json(response)


async def _fetch_connections(access_token: str) -> tuple[int, Any]:
    """List the organisations the user authorised. Returns ``(status, payload)``.

    Mirrors scripts/xero_auth_setup.py:367-374. A 200 payload is a JSON array of
    objects carrying ``tenantId`` / ``tenantName`` / ``tenantType``.
    """

    timeout = get_settings().integration_http_timeout_seconds
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(
            CONNECTIONS_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
    log.info("xero_oauth_connections", extra={"status": response.status_code})
    return response.status_code, _parse_json(response)


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

# Inline Lucide icons (circle-check / circle-alert) rather than emoji.
_ICON_OK = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" '
    'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>'
)
_ICON_ERROR = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" '
    'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/>'
    '<line x1="12" x2="12.01" y1="16" y2="16"/></svg>'
)

_BASE_STYLE = """
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: Arial, sans-serif; background: #f5f7fa;
           display: flex; align-items: center; justify-content: center;
           min-height: 100vh; padding: 24px; }
    .card { background: white; padding: 48px 40px; border-radius: 16px;
            text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,0.08);
            max-width: 520px; width: 100%; }
    h1 { font-size: 26px; margin-bottom: 12px; font-weight: 700; }
    p { color: #555; font-size: 16px; line-height: 1.6; }
    .icon { margin-bottom: 20px; }
    table { width: 100%; border-collapse: collapse; margin-top: 24px;
            text-align: left; }
    th, td { padding: 10px 12px; border-bottom: 1px solid #eef1f5;
             font-size: 14px; }
    th { color: #667; font-weight: 600; font-size: 12px;
         text-transform: uppercase; letter-spacing: 0.04em; }
    code { background: #eef1f5; padding: 2px 6px; border-radius: 6px;
           font-size: 13px; }
    .pill { display: inline-block; padding: 3px 10px; border-radius: 20px;
            font-size: 12px; font-weight: 600; }
    .pill.live { background: #e6f4ea; color: #1e7e34; }
    .pill.idle { background: #eef1f5; color: #667; }
    .reason { background: #fdf0ef; color: #c0392b; padding: 12px 16px;
              border-radius: 8px; font-size: 13px; margin-top: 20px;
              text-align: left; }
"""

_SUCCESS_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Xero Connected</title>
  <style>
{style}
    h1 { color: #1a3a6b; }
    .icon { color: #1e7e34; }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{icon}</div>
    <h1>Xero Connected</h1>
    <p>{intro}</p>
    <table>
      <thead><tr><th>Organisation</th><th>Xero tenant id</th><th></th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
</body>
</html>"""

_ERROR_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Jarvies — Xero Connection Error</title>
  <style>
{style}
    h1 { color: #c0392b; }
    .icon { color: #c0392b; }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{icon}</div>
    <h1>Connection Failed</h1>
    <p>We could not connect this Xero organisation to Jarvies.</p>
    <div class="reason">{error_reason}</div>
  </div>
</body>
</html>"""


def _error_page(reason: str) -> str:
    # .replace (not .format) so the CSS braces in the template survive.
    return (
        _ERROR_HTML.replace("{style}", _BASE_STYLE)
        .replace("{icon}", _ICON_ERROR)
        .replace("{error_reason}", html.escape(reason))
    )


def _html_error(reason: str) -> HTMLResponse:
    return HTMLResponse(_error_page(reason))


def _success_page(orgs: list[dict[str, str]]) -> str:
    rows = []
    for index, org in enumerate(orgs):
        live = index == 0
        pill = (
            '<span class="pill live">Live</span>'
            if live
            else '<span class="pill idle">Connected</span>'
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(org.get('org_name') or '—')}</td>"
            f"<td><code>{html.escape(org['xero_tenant_id'])}</code></td>"
            f"<td>{pill}</td>"
            "</tr>"
        )
    if len(orgs) == 1:
        intro = "Jarvies can now read this organisation's Xero data."
    else:
        intro = (
            f"{len(orgs)} organisations were authorised. Jarvies reads from the "
            "one marked Live; the others are recorded but not yet selectable."
        )
    return (
        _SUCCESS_HTML.replace("{style}", _BASE_STYLE)
        .replace("{icon}", _ICON_OK)
        .replace("{intro}", html.escape(intro))
        .replace("{rows}", "".join(rows))
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _redirect_uri() -> str | None:
    """Return the absolute callback URI, or None when no public base is set.

    Derived from JARVIES_PUBLIC_URL. Returning None (rather than building a
    relative URI) is deliberate: `/auth/start` in admin_consent silently emits
    `redirect_uri=/auth/callback` when the base is empty, which Xero would reject
    only after bouncing the user through a sign-in. Callers fail before redirecting.
    """

    base = get_settings().public_base_url.rstrip("/")
    if not base:
        return None
    return f"{base}{CALLBACK_PATH}"


async def xero_start(request: Request) -> Response:
    """GET /oauth/xero/start?tenant_id=<uuid> — redirect the tenant to Xero.

    Every precondition is checked BEFORE the redirect, so a tenant is never sent
    through a Xero sign-in whose result cannot be stored. Config checks come
    first (no DB round-trip needed to know they fail).
    """

    settings = get_settings()

    tenant_id = request.query_params.get("tenant_id")
    if not tenant_id:
        return _html_error(
            "This link is missing its tenant_id parameter. Ask your Jarvies "
            "administrator for the correct connection link."
        )

    # Fail here, not in the callback: without an encryption key the refresh token
    # cannot be stored, and discovering that after Xero has consumed the
    # authorization code means the whole dance has to be repeated.
    if not settings.encryption_configured:
        log.error("xero_oauth_start_no_encryption_key", extra={"tenant_id": tenant_id})
        return _html_error(
            "Credential encryption is not configured on this server "
            "(JARVIES_ENCRYPTION_KEY). Connecting Xero would not be able to store "
            "the result, so the connection was not started."
        )

    redirect_uri = _redirect_uri()
    if redirect_uri is None:
        log.error("xero_oauth_start_no_public_base_url", extra={"tenant_id": tenant_id})
        return _html_error(
            "This server does not know its own public URL (JARVIES_PUBLIC_URL), "
            "so it cannot tell Xero where to send you back."
        )

    try:
        tenant = await _fetch_tenant(tenant_id)
    except Exception:
        log.exception("xero_oauth_tenant_lookup_failed", extra={"tenant_id": tenant_id})
        return _html_error(
            "We could not start the connection. Please try again in a moment."
        )
    if tenant is None:
        return _html_error("This tenant does not exist.")
    if (tenant.get("status") or "").lower() != "active":
        return _html_error(
            "This tenant is not active. Reactivate it before connecting Xero."
        )

    try:
        metadata = await _fetch_xero_metadata(tenant_id)
    except Exception:
        log.exception("xero_oauth_metadata_lookup_failed", extra={"tenant_id": tenant_id})
        return _html_error(
            "We could not start the connection. Please try again in a moment."
        )
    client_id = metadata.get("client_id")
    if not client_id:
        return _html_error(
            "No Xero client id is configured for this tenant. Add the Xero app's "
            "client id and secret before connecting."
        )

    state = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(minutes=STATE_TTL_MINUTES)
    try:
        await _store_state(state, tenant["id"], expires_at)
    except Exception:
        log.exception("xero_oauth_state_store_failed", extra={"tenant_id": tenant_id})
        return _html_error(
            "We could not start the connection. Please try again in a moment."
        )

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    }
    log.info("xero_oauth_start", extra={"tenant_id": tenant["id"]})
    return RedirectResponse(f"{AUTHORIZE_BASE}?{urlencode(params)}", status_code=302)


async def xero_callback(request: Request) -> Response:
    """GET /oauth/xero/callback?code=&state= — finish the connection."""

    params = request.query_params

    # Xero reports denial / failure with `error` (+ optional description) and no
    # code. Handle it before anything else.
    error = params.get("error")
    if error:
        return _html_error(params.get("error_description") or error)

    state = params.get("state")
    if not state:
        return _html_error(
            "The sign-in response was incomplete (no state). Please start again."
        )

    try:
        row = await _consume_state(state)
    except Exception:
        log.exception("xero_oauth_state_consume_failed")
        return _html_error(
            "A server error occurred while validating your sign-in. Please try again."
        )
    if row is None:
        return _html_error(
            "This connection link is invalid or has expired. Please start again."
        )

    # The tenant comes from the consumed state row, never from a query parameter:
    # the state is what was authenticated, a query param is attacker-controlled.
    tenant_id = row.get("tenant_hint")
    if not tenant_id:
        log.error("xero_oauth_state_missing_tenant")
        return _html_error(
            "This connection link is not associated with a tenant. Please start again."
        )

    code = params.get("code")
    if not code:
        return _html_error(
            "Xero did not return an authorization code. Please start again."
        )

    redirect_uri = _redirect_uri()
    if redirect_uri is None:
        log.error("xero_oauth_callback_no_public_base_url", extra={"tenant_id": tenant_id})
        return _html_error(
            "This server does not know its own public URL (JARVIES_PUBLIC_URL), "
            "so it cannot complete the token exchange."
        )

    # Existing metadata is read BEFORE the write and merged into below. A bare
    # overwrite here would destroy client_id/client_secret.
    try:
        metadata = await _fetch_xero_metadata(tenant_id)
    except Exception:
        log.exception("xero_oauth_metadata_lookup_failed", extra={"tenant_id": tenant_id})
        return _html_error(
            "We could not read this tenant's existing Xero configuration. "
            "Nothing was changed. Please try again in a moment."
        )
    client_id = metadata.get("client_id")
    client_secret = metadata.get("client_secret")
    if not client_id or not client_secret:
        return _html_error(
            "This tenant's Xero client id and secret are no longer configured, "
            "so the authorization could not be completed."
        )

    try:
        status, payload = await _exchange_code(
            client_id, client_secret, code, redirect_uri
        )
    except httpx.HTTPError:
        log.exception("xero_oauth_token_exchange_error", extra={"tenant_id": tenant_id})
        return _html_error("We could not reach Xero to complete the connection.")
    if status != 200:
        log.error(
            "xero_oauth_token_exchange_rejected",
            extra={"tenant_id": tenant_id, "status": status},
        )
        return _html_error(
            f"Xero rejected the authorization (HTTP {status}). Please start again."
        )

    access_token = payload.get("access_token") if isinstance(payload, dict) else None
    refresh_token = payload.get("refresh_token") if isinstance(payload, dict) else None
    if not access_token or not refresh_token:
        log.error("xero_oauth_token_response_incomplete", extra={"tenant_id": tenant_id})
        return _html_error(
            "Xero's response did not include the expected tokens. Please start again."
        )

    try:
        status, connections = await _fetch_connections(access_token)
    except httpx.HTTPError:
        log.exception("xero_oauth_connections_error", extra={"tenant_id": tenant_id})
        return _html_error("We could not read your Xero organisations from Xero.")
    if status != 200 or not isinstance(connections, list):
        log.error(
            "xero_oauth_connections_rejected",
            extra={"tenant_id": tenant_id, "status": status},
        )
        return _html_error(
            f"Xero would not list your organisations (HTTP {status}). Please start again."
        )

    orgs = [
        {
            "xero_tenant_id": str(item.get("tenantId")),
            "org_name": item.get("tenantName"),
        }
        for item in connections
        if isinstance(item, dict) and item.get("tenantId")
    ]
    if not orgs:
        return _html_error(
            "No Xero organisations were authorised. Please start again and select "
            "an organisation when Xero asks."
        )

    # PHASE 1 SHORTCUT: the first organisation Xero returns becomes the live one.
    # Everything else is recorded with active=false. Replace this with an explicit
    # org picker when multi-org routing lands — until then a tenant with several
    # organisations has no way to choose a different one.
    selected = orgs[0]

    merged_metadata = dict(metadata)
    # This is the value XeroService sends as the Xero-tenant-id header. Without
    # it every Xero tool call fails even with a valid refresh token.
    merged_metadata["tenant_id"] = selected["xero_tenant_id"]

    try:
        await _save_connection(tenant_id, refresh_token, merged_metadata, orgs)
    except CryptoNotConfiguredError:
        log.error("xero_oauth_no_encryption_key", extra={"tenant_id": tenant_id})
        return _html_error(
            "Credential encryption is not configured on this server "
            "(JARVIES_ENCRYPTION_KEY), so the Xero connection could not be saved. "
            "Nothing was stored. Ask your administrator to configure the key, then "
            "start again."
        )
    except Exception:
        log.exception("xero_oauth_save_failed", extra={"tenant_id": tenant_id})
        return _html_error(
            "We could not save the Xero connection. Nothing was stored. Please try "
            "again in a moment."
        )

    log.info(
        "xero_oauth_connected",
        extra={"tenant_id": tenant_id, "org_count": len(orgs)},
    )
    return HTMLResponse(_success_page(orgs))


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def get_xero_oauth_routes() -> list[Route]:
    return [
        Route(START_PATH, xero_start, methods=["GET"]),
        Route(CALLBACK_PATH, xero_callback, methods=["GET"]),
    ]


def register_xero_oauth_routes(app: Any) -> None:
    """Attach the hosted Xero OAuth routes to a Starlette app."""

    for route in get_xero_oauth_routes():
        app.router.routes.append(route)
