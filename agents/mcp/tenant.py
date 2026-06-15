"""Tenant resolution for the MCP HTTP layer (Phase 2A).

Phase 2A resolves the calling organisation from an ``X-Tenant-ID`` header (a
tenant UUID) — no OAuth yet. The resolved tenant row is attached to
``request.state.tenant`` (per the FastAPI/Starlette convention) AND published
on a ContextVar so MCP tool functions, which never receive the HTTP request,
can read it during execution.

When no header is present, or the DB is unavailable, the tenant is ``None`` and
tools fall back to their existing env-var credential behaviour (the
Claude Desktop / X-API-Key path), which keeps all Phase 1 behaviour intact.

``get_tenant_credentials`` is the per-tenant credential lookup used by the tool
layer to swap env credentials for the tenant's own.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from agents.mcp.database import DatabaseNotConfiguredError, get_conn
from agents.mcp.oauth import decode_jarvies_token

log = logging.getLogger(__name__)

TENANT_HEADER = b"x-tenant-id"
AUTH_HEADER = b"authorization"

_current_tenant: ContextVar[dict[str, Any] | None] = ContextVar(
    "mcp_current_tenant",
    default=None,
)

_current_user_id: ContextVar[str | None] = ContextVar(
    "mcp_current_user_id",
    default=None,
)


def current_tenant() -> dict[str, Any] | None:
    """Return the tenant resolved for the current request/tool call, if any."""

    return _current_tenant.get()


def set_current_tenant(tenant: dict[str, Any] | None) -> Any:
    """Set the current tenant; returns the reset token (for symmetry in tests)."""

    return _current_tenant.set(tenant)


def reset_current_tenant(token: Any) -> None:
    """Restore a previous tenant context using the token from ``set_current_tenant``."""

    _current_tenant.reset(token)


def current_user_id() -> str | None:
    """Return the authenticated user id (the bearer token ``sub``) for this call.

    Set from the Jarvies access token by ``TenantResolutionMiddleware``. ``None``
    when no bearer token resolved (e.g. the X-API-Key / local path). Lets tool
    code retrieve per-user stored credentials without the HTTP request object.
    """

    return _current_user_id.get()


def set_current_user_id(user_id: str | None) -> Any:
    """Set the current user id; returns the reset token (for symmetry in tests)."""

    return _current_user_id.set(user_id)


def reset_current_user_id(token: Any) -> None:
    """Restore a previous user-id context using the token from ``set_current_user_id``."""

    _current_user_id.reset(token)


async def load_tenant(tenant_id: str) -> dict[str, Any] | None:
    """Load an active tenant row by id. Returns ``None`` if absent/inactive.

    Returns ``None`` (never raises) when the DB is not configured, so the HTTP
    path degrades to env-var behaviour rather than failing the request.
    """

    try:
        async with get_conn() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, microsoft_tenant_id, display_name, is_active, created_at
                FROM tenants
                WHERE id = $1 AND is_active = true
                """,
                tenant_id,
            )
    except DatabaseNotConfiguredError:
        return None
    except Exception:
        log.exception("tenant_lookup_failed", extra={"tenant_id": tenant_id})
        return None
    if row is None:
        return None
    return {
        "id": str(row["id"]),
        "microsoft_tenant_id": row["microsoft_tenant_id"],
        "display_name": row["display_name"],
        "is_active": row["is_active"],
    }


async def get_tenant_credentials(tenant_id: str, credential_type: str) -> dict[str, Any] | None:
    """Return one credential row for a tenant, or ``None`` if not present.

    Shape::

        {"credential_key": "<token>", "metadata": {"team_id": "...", ...}}

    Returns ``None`` (never raises) when the DB is unavailable so the tool layer
    can fall back to env-var credentials.
    """

    try:
        async with get_conn() as conn:
            row = await conn.fetchrow(
                """
                SELECT credential_key, metadata
                FROM tenant_credentials
                WHERE tenant_id = $1 AND credential_type = $2
                """,
                tenant_id,
                credential_type,
            )
    except DatabaseNotConfiguredError:
        return None
    except Exception:
        log.exception(
            "tenant_credentials_lookup_failed",
            extra={"tenant_id": tenant_id, "credential_type": credential_type},
        )
        return None
    if row is None:
        return None

    metadata = row["metadata"]
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {}
    return {
        "credential_key": row["credential_key"],
        "metadata": metadata or {},
    }


def _header_value(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1").strip()
    return None


class TenantResolutionMiddleware:
    """Pure-ASGI middleware that resolves ``X-Tenant-ID`` for each request.

    Implemented as pure ASGI (not ``BaseHTTPMiddleware``) so the ContextVar it
    sets propagates into the downstream MCP tool execution, where the HTTP
    request object is not available.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        tenant: dict[str, Any] | None = None
        user_id: str | None = None

        # 1. Jarvies OAuth bearer token — the tenant_id claim is authoritative.
        auth_header = _header_value(scope, AUTH_HEADER)
        if auth_header and auth_header.lower().startswith("bearer "):
            claims = decode_jarvies_token(auth_header.split(" ", 1)[1].strip())
            if claims:
                user_id = claims.get("sub")
                if claims.get("tenant_id"):
                    tenant = await load_tenant(claims["tenant_id"])

        # 2. Fall back to the explicit X-Tenant-ID header (Phase 2A test path).
        if tenant is None:
            tenant_id = _header_value(scope, TENANT_HEADER)
            if tenant_id:
                tenant = await load_tenant(tenant_id)
                if tenant is None:
                    log.warning("tenant_header_unresolved", extra={"tenant_id": tenant_id})

        # Starlette's Request.state reads from scope["state"]; populate it so
        # request.state.tenant works for any FastAPI/Starlette route.
        state = scope.setdefault("state", {})
        state["tenant"] = tenant

        token = set_current_tenant(tenant)
        user_token = set_current_user_id(user_id)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_user_id(user_token)
            reset_current_tenant(token)
