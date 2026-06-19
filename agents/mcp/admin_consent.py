"""Microsoft admin-consent onboarding flow (multi-tenant).

A small Starlette route family that lets a Microsoft 365 administrator grant
Jarvies tenant-wide admin consent and register their tenant, separate from the
per-user OAuth flow in ``agents.mcp.oauth``:

* ``GET /auth/start`` — mint a single-use ``state``, persist it, and redirect to
  Microsoft's ``/common/adminconsent`` endpoint with ``prompt=select_account``
  (forces the account picker so the admin signs in with the correct
  organisational account / tenant).
* ``GET /auth/callback`` — the admin-consent response is handled by
  ``handle_admin_consent`` which ``oauth.auth_callback`` calls at the very top.
  It only claims callbacks that are NOT the user OAuth flow (which always
  carries ``code``); the existing flow runs unchanged below.
* ``GET /auth/tenants`` — list consented tenants; protected by the same
  ``X-API-Key`` / ``JARVIES_ADMIN_API_KEY`` as ``/admin/*``.

DB access goes through small named coroutines (``_store_state``,
``_consume_state``, ``_upsert_tenant``, ``_list_consent_tenants``) so unit tests
can monkeypatch them without a live database, mirroring ``agents.mcp.admin``.
``oauth_states`` rows are single-use: read and deleted in one transaction.
"""

from __future__ import annotations

import hmac
import html
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from agents.mcp.config import get_settings
from agents.mcp.database import get_conn

log = logging.getLogger(__name__)

# Microsoft tenant-wide admin-consent endpoint (multi-tenant /common).
MS_ADMIN_CONSENT_URL = "https://login.microsoftonline.com/common/adminconsent"

STATE_TTL_MINUTES = 10

# Schema for the admin-consent flow. Mirrors the DDL added to
# scripts/migrate.py; applied idempotently at startup via ensure_consent_schema.
CONSENT_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS oauth_states (
        state TEXT PRIMARY KEY,
        tenant_hint TEXT,
        plan TEXT DEFAULT 'standard',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL
    )
    """,
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan TEXT",
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS consented_at TIMESTAMPTZ",
    "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'",
]


async def ensure_consent_schema() -> None:
    """Apply the admin-consent schema idempotently. Best-effort, never raises.

    Called at server startup after the pool is initialised. A failure here is
    logged and swallowed so the server still starts (the consent flow will then
    surface a clear error page instead of taking the process down).
    """

    try:
        async with get_conn() as conn:
            for statement in CONSENT_DDL_STATEMENTS:
                await conn.execute(statement)
        log.info("oauth_consent_schema_ensured")
    except Exception:
        log.warning("oauth_consent_schema_failed")


# ---------------------------------------------------------------------------
# Auth (X-API-Key) — same contract as /admin/*
# ---------------------------------------------------------------------------


def _admin_authorized(request: Request) -> bool:
    """Constant-time check of X-API-Key against JARVIES_ADMIN_API_KEY."""

    expected = get_settings().admin_api_key
    if not expected:
        return False
    supplied = request.headers.get("x-api-key", "")
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _unauthorized() -> JSONResponse:
    return JSONResponse({"status": "error", "error": "unauthorized"}, status_code=401)


# ---------------------------------------------------------------------------
# DB access (small named coroutines — monkeypatched in unit tests)
# ---------------------------------------------------------------------------


async def _store_state(
    state: str,
    tenant_hint: str | None,
    plan: str,
    expires_at: datetime,
) -> None:
    async with get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO oauth_states (state, tenant_hint, plan, expires_at)
            VALUES ($1, $2, $3, $4)
            """,
            state,
            tenant_hint,
            plan,
            expires_at,
        )


async def _consume_state(state: str) -> dict[str, Any] | None:
    """Fetch and delete a state row atomically (single-use).

    Returns the row dict when the state is known and unexpired, else None. Any
    matching row is deleted even when expired, so a state can never be reused.
    """

    async with get_conn() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT state, tenant_hint, plan, expires_at FROM oauth_states "
            "WHERE state = $1",
            state,
        )
        if row is not None:
            await conn.execute("DELETE FROM oauth_states WHERE state = $1", state)
    if row is None:
        return None
    expires_at = row["expires_at"]
    if expires_at is not None and expires_at < datetime.now(UTC):
        return None
    return dict(row)


async def _upsert_tenant(
    microsoft_tenant_id: str,
    display_name: str | None,
    plan: str,
) -> None:
    async with get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO tenants (microsoft_tenant_id, display_name, plan, consented_at, status)
            VALUES ($1, $2, $3, NOW(), 'active')
            ON CONFLICT (microsoft_tenant_id)
            DO UPDATE SET plan = $3, consented_at = NOW(), status = 'active',
                          display_name = COALESCE($2, tenants.display_name)
            """,
            microsoft_tenant_id,
            display_name,
            plan,
        )


async def _list_consent_tenants() -> list[dict[str, Any]]:
    async with get_conn() as conn:
        rows = await conn.fetch(
            "SELECT id, microsoft_tenant_id, display_name, plan, status, "
            "consented_at, created_at FROM tenants ORDER BY created_at"
        )
    return [
        {
            "tenant_id": str(row["id"]),
            "microsoft_tenant_id": row["microsoft_tenant_id"],
            "display_name": row["display_name"],
            "plan": row["plan"],
            "status": row["status"],
            "consented_at": (
                row["consented_at"].isoformat() if row["consented_at"] else None
            ),
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


_SUCCESS_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Jarvies Connected</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: Arial, sans-serif; background: #f5f7fa;
           display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .card { background: white; padding: 48px 40px; border-radius: 16px;
            text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,0.08);
            max-width: 480px; width: 90%; }
    .icon { font-size: 56px; margin-bottom: 20px; }
    h1 { color: #1a3a6b; font-size: 26px; margin-bottom: 12px; font-weight: 700; }
    p { color: #555; font-size: 16px; line-height: 1.6; }
    .plan { background: #e8f0fe; color: #1a3a6b; padding: 6px 16px;
            border-radius: 20px; font-size: 14px; font-weight: 600;
            display: inline-block; margin-top: 20px; }
    .tenant { color: #888; font-size: 13px; margin-top: 12px; }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">✅</div>
    <h1>Jarvies Connected</h1>
    <p>Your Microsoft 365 tenant has been successfully connected to Jarvies.<br><br>
       Your administrator can now assign users and configure access.</p>
    <div class="plan">{plan}</div>
    <div class="tenant">{tenant_id}</div>
  </div>
</body>
</html>"""


_ERROR_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Jarvies — Connection Error</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: Arial, sans-serif; background: #f5f7fa;
           display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .card { background: white; padding: 48px 40px; border-radius: 16px;
            text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,0.08);
            max-width: 480px; width: 90%; }
    .icon { font-size: 56px; margin-bottom: 20px; }
    h1 { color: #c0392b; font-size: 26px; margin-bottom: 12px; font-weight: 700; }
    p { color: #555; font-size: 15px; line-height: 1.6; }
    .reason { background: #fdf0ef; color: #c0392b; padding: 12px 16px;
              border-radius: 8px; font-size: 13px; margin-top: 20px; text-align: left; }
    .retry { margin-top: 24px; }
    .retry a { color: #1a3a6b; font-size: 14px; }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">❌</div>
    <h1>Connection Failed</h1>
    <p>We could not connect your Microsoft 365 tenant to Jarvies.</p>
    <div class="reason">{error_reason}</div>
    <div class="retry"><a href="/auth/start">Try again</a></div>
  </div>
</body>
</html>"""


def _success_page(plan: str, tenant_id: str) -> str:
    # .replace (not .format) so the CSS braces in the template are left intact.
    return _SUCCESS_HTML.replace("{plan}", html.escape(plan)).replace(
        "{tenant_id}", html.escape(tenant_id)
    )


def _error_page(reason: str) -> str:
    return _ERROR_HTML.replace("{error_reason}", html.escape(reason))


def _html_error(reason: str) -> HTMLResponse:
    return HTMLResponse(_error_page(reason))


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def _public_base() -> str:
    return get_settings().public_base_url.rstrip("/")


async def auth_start(request: Request) -> Response:
    """Begin admin consent: persist a state and redirect to Microsoft."""

    settings = get_settings()
    state = secrets.token_urlsafe(32)
    tenant_hint = request.query_params.get("tenant_hint")
    plan = request.query_params.get("plan", "standard")
    expires_at = datetime.now(UTC) + timedelta(minutes=STATE_TTL_MINUTES)

    try:
        await _store_state(state, tenant_hint, plan, expires_at)
    except Exception:
        log.exception("oauth_state_store_failed")
        return _html_error(
            "We could not start the connection. Please try again in a moment."
        )

    params = {
        "client_id": settings.azure_client_id,
        "redirect_uri": f"{_public_base()}/auth/callback",
        "state": state,
        # Force the account picker so the admin selects the correct
        # organisational account / tenant (prevents wrong-account logins).
        "prompt": "select_account",
    }
    return RedirectResponse(f"{MS_ADMIN_CONSENT_URL}?{urlencode(params)}", status_code=302)


async def handle_admin_consent(request: Request) -> Response | None:
    """Handle a Microsoft admin-consent callback, or defer to the user flow.

    Returns an ``HTMLResponse`` when this is an admin-consent callback (success
    or error), or ``None`` to let ``oauth.auth_callback`` run its existing user
    OAuth flow unchanged. The user flow is identified by the ``code`` param, so
    any callback carrying ``code`` is never touched here.
    """

    params = request.query_params

    # The user OAuth flow always carries `code`; leave it entirely to oauth.py.
    if params.get("code"):
        return None

    admin_consent = params.get("admin_consent")
    error = params.get("error")
    # Only claim callbacks that look like an admin-consent response. Anything
    # else (no code, no admin_consent, no error) defers to the existing flow.
    if admin_consent is None and error is None:
        return None

    state = params.get("state")
    if not state:
        return _html_error(
            "The sign-in link was incomplete (no state). Please start again."
        )

    try:
        row = await _consume_state(state)
    except Exception:
        log.exception("oauth_state_consume_failed")
        return _html_error(
            "A server error occurred while validating your sign-in. Please try again."
        )
    if row is None:
        return _html_error(
            "This sign-in link is invalid or has expired. Please start again."
        )

    plan = row.get("plan") or "standard"

    # Microsoft reports denial / failure via error + error_description.
    if error:
        return _html_error(params.get("error_description") or error)

    microsoft_tenant_id = params.get("tenant")
    if not microsoft_tenant_id:
        return _html_error(
            "Microsoft did not return a tenant id. Please try the connection again."
        )

    try:
        await _upsert_tenant(microsoft_tenant_id, row.get("tenant_hint"), plan)
    except Exception:
        log.exception("consent_tenant_upsert_failed")
        return _html_error(
            "We could not save your connection. Please try again in a moment."
        )

    log.info(
        "tenant_consented",
        extra={"microsoft_tenant_id": microsoft_tenant_id, "plan": plan},
    )
    return HTMLResponse(_success_page(plan, microsoft_tenant_id))


async def auth_tenants(request: Request) -> JSONResponse:
    """GET /auth/tenants — list consented tenants. X-API-Key protected."""

    if not _admin_authorized(request):
        return _unauthorized()
    try:
        tenants = await _list_consent_tenants()
    except Exception:
        log.exception("auth_tenants_failed")
        return JSONResponse(
            {"status": "error", "error": "Database error"}, status_code=500
        )
    return JSONResponse({"status": "ok", "count": len(tenants), "tenants": tenants})


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

# Bypass the MCP auth middleware for these (alongside /admin/*). /auth/start is
# fully public; /auth/tenants enforces its own X-API-Key in the handler.
CONSENT_PUBLIC_PATHS = {"/auth/start", "/auth/tenants"}


def get_consent_routes() -> list[Route]:
    return [
        Route("/auth/start", auth_start, methods=["GET"]),
        Route("/auth/tenants", auth_tenants, methods=["GET"]),
    ]


def register_consent_routes(app: Any) -> None:
    """Attach the admin-consent routes to a Starlette app."""

    for route in get_consent_routes():
        app.router.routes.append(route)
