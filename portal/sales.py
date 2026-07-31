"""Headspace sales-portal routes (all under ``/portal/sales``).

Session-authenticated (``kind='headspace_admin'``) server-rendered admin UI for
managing tenants and licenses. DB access goes through ``get_conn`` (monkeypatched
in tests). Every state change and its audit_log row are written in one
transaction. All HTML is produced by ``portal.templates``.
"""

from __future__ import annotations

import functools
import hmac
from collections.abc import Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from agents.mcp.config import get_settings
from agents.mcp.database import get_conn
from portal.audit import write_audit
from portal.sessions import (
    clear_session_cookie,
    csrf_for_request,
    read_session,
    set_session_cookie,
    verify_csrf,
)
from portal.templates import (
    render_dashboard,
    render_error,
    render_invite,
    render_licenses,
    render_login,
)

ADMIN_KIND = "headspace_admin"
LOGIN_PATH = "/portal/sales/login"
DASHBOARD_PATH = "/portal/sales"

# Newest-license-per-tenant summary via LEFT JOIN LATERAL, on top of the
# _list_consent_tenants column shape (plus is_active).
_DASHBOARD_SQL = """
    SELECT t.id, t.microsoft_tenant_id, t.display_name, t.plan, t.status,
           t.is_active, t.consented_at, t.created_at,
           l.plan AS license_plan, l.seat_count AS license_seats,
           l.status AS license_status
    FROM tenants t
    LEFT JOIN LATERAL (
        SELECT plan, seat_count, status
        FROM licenses
        WHERE licenses.tenant_id = t.id
        ORDER BY created_at DESC
        LIMIT 1
    ) l ON true
    ORDER BY t.created_at
"""
_INSERT_LICENSE_SQL = """
    INSERT INTO licenses (tenant_id, plan, seat_count, ends_at, notes, status, created_by)
    VALUES ($1::uuid, $2, $3, $4::timestamptz, $5, 'active', $6)
    RETURNING id
"""


# ---------------------------------------------------------------------------
# Auth + CSRF helpers (no middleware — enforced per route)
# ---------------------------------------------------------------------------


def _require_admin(handler: Callable) -> Callable:
    """Wrap a handler so only a ``headspace_admin`` session reaches it.

    Anything else (no session, expired, wrong kind) redirects to the login page.
    The wrapped handler is called as ``handler(request, session)``.
    """

    @functools.wraps(handler)
    async def wrapper(request: Request) -> Response:
        session = read_session(request)
        if not session or session.get("kind") != ADMIN_KIND:
            return RedirectResponse(LOGIN_PATH, status_code=303)
        return await handler(request, session)

    return wrapper


def _forbidden() -> HTMLResponse:
    return HTMLResponse(
        render_error("Invalid or missing CSRF token.", status_label="Forbidden"),
        status_code=403,
    )


def _not_found(message: str = "Not found.") -> HTMLResponse:
    return HTMLResponse(render_error(message, status_label="Not found"), status_code=404)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


async def login_get(request: Request) -> HTMLResponse:
    return HTMLResponse(render_login())


async def login_post(request: Request) -> Response:
    form = await request.form()
    supplied = form.get("admin_key") or ""
    expected = get_settings().admin_api_key
    # Empty admin_api_key disables login entirely.
    if not expected:
        return HTMLResponse(
            render_login("Admin access is not configured on this server."),
            status_code=401,
        )
    if not hmac.compare_digest(str(supplied), expected):
        return HTMLResponse(render_login("Invalid admin key."), status_code=401)

    response = RedirectResponse(DASHBOARD_PATH, status_code=303)
    set_session_cookie(response, {"sub": "headspace", "kind": ADMIN_KIND})
    return response


async def logout(request: Request) -> Response:
    response = RedirectResponse(LOGIN_PATH, status_code=303)
    clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Dashboard + tenant/license management
# ---------------------------------------------------------------------------


@_require_admin
async def dashboard(request: Request, session: dict[str, Any]) -> HTMLResponse:
    async with get_conn() as conn:
        rows = await conn.fetch(_DASHBOARD_SQL)
    tenants = [dict(row) for row in rows]
    return HTMLResponse(render_dashboard(tenants, csrf_for_request(request)))


@_require_admin
async def toggle_status(request: Request, session: dict[str, Any]) -> Response:
    form = await request.form()
    if not verify_csrf(request, form.get("csrf")):
        return _forbidden()
    tenant_id = request.path_params["tenant_id"]
    async with get_conn() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT status FROM tenants WHERE id::text = $1", tenant_id)
        if row is None:
            return _not_found("Tenant not found.")
        new_status = "suspended" if row["status"] == "active" else "active"
        await conn.execute(
            "UPDATE tenants SET status = $1 WHERE id::text = $2", new_status, tenant_id
        )
        await write_audit(
            conn,
            tenant_id=tenant_id,
            actor_type=ADMIN_KIND,
            actor_id=session.get("sub", ""),
            action="tenant.status",
            target_type="tenant",
            target_id=tenant_id,
            metadata={"status": new_status},
        )
    return RedirectResponse(DASHBOARD_PATH, status_code=303)


@_require_admin
async def licenses_page(request: Request, session: dict[str, Any]) -> Response:
    tenant_id = request.path_params["tenant_id"]
    async with get_conn() as conn:
        tenant = await conn.fetchrow(
            "SELECT id, display_name, status FROM tenants WHERE id::text = $1", tenant_id
        )
        if tenant is None:
            return _not_found("Tenant not found.")
        rows = await conn.fetch(
            "SELECT id, plan, seat_count, status, starts_at, ends_at, notes "
            "FROM licenses WHERE tenant_id::text = $1 ORDER BY created_at DESC",
            tenant_id,
        )
    return HTMLResponse(
        render_licenses(
            dict(tenant), [dict(r) for r in rows], tenant_id, csrf_for_request(request)
        )
    )


@_require_admin
async def create_license(request: Request, session: dict[str, Any]) -> Response:
    form = await request.form()
    if not verify_csrf(request, form.get("csrf")):
        return _forbidden()
    tenant_id = request.path_params["tenant_id"]
    plan = (form.get("plan") or "").strip()
    ends_at = (form.get("ends_at") or "").strip() or None
    notes = (form.get("notes") or "").strip() or None
    try:
        seat_count = max(1, int(form.get("seat_count") or 1))
    except (TypeError, ValueError):
        seat_count = 1

    async with get_conn() as conn, conn.transaction():
        row = await conn.fetchrow(
            _INSERT_LICENSE_SQL,
            tenant_id,
            plan,
            seat_count,
            ends_at,
            notes,
            session.get("sub", ""),
        )
        await write_audit(
            conn,
            tenant_id=tenant_id,
            actor_type=ADMIN_KIND,
            actor_id=session.get("sub", ""),
            action="license.create",
            target_type="license",
            target_id=str(row["id"]) if row else None,
            metadata={"plan": plan, "seat_count": seat_count},
        )
    return RedirectResponse(
        f"/portal/sales/tenants/{tenant_id}/licenses", status_code=303
    )


@_require_admin
async def cancel_license(request: Request, session: dict[str, Any]) -> Response:
    form = await request.form()
    if not verify_csrf(request, form.get("csrf")):
        return _forbidden()
    license_id = request.path_params["license_id"]
    async with get_conn() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT tenant_id FROM licenses WHERE id::text = $1", license_id
        )
        if row is None:
            return _not_found("License not found.")
        tenant_id = str(row["tenant_id"])
        await conn.execute(
            "UPDATE licenses SET status = 'cancelled' WHERE id::text = $1", license_id
        )
        await write_audit(
            conn,
            tenant_id=tenant_id,
            actor_type=ADMIN_KIND,
            actor_id=session.get("sub", ""),
            action="license.cancel",
            target_type="license",
            target_id=license_id,
        )
    return RedirectResponse(
        f"/portal/sales/tenants/{tenant_id}/licenses", status_code=303
    )


@_require_admin
async def invite(request: Request, session: dict[str, Any]) -> HTMLResponse:
    settings = get_settings()
    base = settings.public_base_url.rstrip("/") or str(request.base_url).rstrip("/")
    return HTMLResponse(render_invite(f"{base}/auth/start"))


def get_sales_routes() -> list[Route]:
    """Return the sales-portal routes."""

    return [
        Route(LOGIN_PATH, login_get, methods=["GET"]),
        Route(LOGIN_PATH, login_post, methods=["POST"]),
        Route("/portal/sales/logout", logout, methods=["GET"]),
        Route(DASHBOARD_PATH, dashboard, methods=["GET"]),
        Route(
            "/portal/sales/tenants/{tenant_id}/status", toggle_status, methods=["POST"]
        ),
        Route(
            "/portal/sales/tenants/{tenant_id}/licenses",
            licenses_page,
            methods=["GET"],
        ),
        Route(
            "/portal/sales/tenants/{tenant_id}/licenses",
            create_license,
            methods=["POST"],
        ),
        Route(
            "/portal/sales/licenses/{license_id}/cancel",
            cancel_license,
            methods=["POST"],
        ),
        Route("/portal/sales/invite", invite, methods=["GET"]),
    ]
