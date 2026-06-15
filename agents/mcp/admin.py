"""Admin HTTP endpoints for tenant credential management.

A small Starlette route family mounted at ``/admin`` so tenants can be
onboarded without psql. These routes are protected by a dedicated
``X-API-Key`` header checked against ``JARVIES_ADMIN_API_KEY`` — separate from
the MCP ``/mcp`` auth (``MCP_API_KEYS`` / OAuth bearer). The MCP auth
middleware lets ``/admin/*`` through (see ``agents.mcp.auth``) precisely
because these handlers enforce their own key.

Storage: credentials live in ``tenant_credentials``, one row per
``(tenant_id, credential_type)``. The type's primary secret is stored in the
``credential_key`` column and the remaining fields in the ``metadata`` JSONB —
the same layout the ClickUp tool and the seed migration use. Patch semantics:
a POST updates only the fields supplied and merges them into any existing row,
so fields that were not sent are never wiped.
"""

from __future__ import annotations

import hmac
import json
import logging
import uuid
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agents.mcp.config import get_settings
from agents.mcp.database import get_conn

log = logging.getLogger(__name__)

ADMIN_PATH_PREFIX = "/admin"

# API field name -> (credential_type, metadata_key).
# metadata_key is None when the value is stored in the row's credential_key
# column (the credential_type's primary secret); otherwise it is the key under
# which the value is stored inside the metadata JSONB.
_FIELD_MAP: dict[str, tuple[str, str | None]] = {
    "clickup_token": ("clickup", None),
    "xero_client_id": ("xero", "client_id"),
    "xero_client_secret": ("xero", "client_secret"),
    "xero_tenant_id": ("xero", "tenant_id"),
    "xero_refresh_token": ("xero", None),
    "cin7_api_key": ("cin7", None),
    "cin7_account_id": ("cin7", "account_id"),
    "freshsales_api_key": ("freshsales", None),
    "freshsales_domain": ("freshsales", "domain"),
}
_KNOWN_FIELDS = list(_FIELD_MAP.keys())


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _admin_authorized(request: Request) -> bool:
    """Constant-time check of the X-API-Key header against JARVIES_ADMIN_API_KEY."""

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


async def _fetch_tenant(tenant_id: str) -> dict[str, Any] | None:
    """Return {"id", "name", "created_at"} for a tenant, or None if absent."""

    async with get_conn() as conn:
        row = await conn.fetchrow(
            "SELECT id, display_name, created_at FROM tenants WHERE id::text = $1",
            tenant_id,
        )
    if row is None:
        return None
    created_at = row["created_at"]
    return {
        "id": str(row["id"]),
        "name": row["display_name"],
        "created_at": created_at.isoformat() if created_at else None,
    }


async def _fetch_credentials(tenant_id: str) -> dict[str, dict[str, Any]]:
    """Return {credential_type: {"credential_key": ..., "metadata": {...}}}."""

    async with get_conn() as conn:
        rows = await conn.fetch(
            "SELECT credential_type, credential_key, metadata "
            "FROM tenant_credentials WHERE tenant_id::text = $1",
            tenant_id,
        )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        metadata = row["metadata"]
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        result[row["credential_type"]] = {
            "credential_key": row["credential_key"],
            "metadata": metadata or {},
        }
    return result


async def _upsert_credentials(tenant_uuid: str, rows: dict[str, dict[str, Any]]) -> None:
    """Upsert one tenant_credentials row per credential_type in `rows`.

    `rows` maps credential_type -> {"credential_key": ..., "metadata": {...}}.
    Each row is written in full (already merged with any existing values by the
    caller), in a single transaction.
    """

    async with get_conn() as conn:
        async with conn.transaction():
            for credential_type, payload in rows.items():
                await conn.execute(
                    """
                    INSERT INTO tenant_credentials
                        (tenant_id, credential_type, credential_key, metadata)
                    VALUES ($1, $2, $3, $4::jsonb)
                    ON CONFLICT (tenant_id, credential_type)
                    DO UPDATE SET
                        credential_key = EXCLUDED.credential_key,
                        metadata = EXCLUDED.metadata,
                        updated_at = now()
                    """,
                    uuid.UUID(tenant_uuid),
                    credential_type,
                    payload.get("credential_key"),
                    json.dumps(payload.get("metadata") or {}),
                )


async def _list_tenants() -> list[dict[str, Any]]:
    async with get_conn() as conn:
        rows = await conn.fetch(
            "SELECT id, display_name, created_at FROM tenants ORDER BY created_at"
        )
    return [
        {
            "tenant_id": str(row["id"]),
            "name": row["display_name"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def set_credentials(request: Request) -> JSONResponse:
    """POST /admin/tenants/{tenant_id}/credentials — patch-update credentials."""

    if not _admin_authorized(request):
        return _unauthorized()

    tenant_id = request.path_params["tenant_id"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return JSONResponse(
            {"status": "error", "error": "Invalid JSON body"}, status_code=400
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"status": "error", "error": "JSON body must be an object"}, status_code=400
        )

    try:
        tenant = await _fetch_tenant(tenant_id)
        if tenant is None:
            return JSONResponse(
                {"status": "error", "error": "Tenant not found"}, status_code=404
            )

        # Provided = known fields present in the body with a non-null value.
        provided = [f for f in _KNOWN_FIELDS if f in body and body[f] is not None]

        if provided:
            existing = await _fetch_credentials(tenant["id"])
            to_write: dict[str, dict[str, Any]] = {}
            for field in provided:
                credential_type, slot = _FIELD_MAP[field]
                current = to_write.get(credential_type)
                if current is None:
                    base = existing.get(credential_type) or {}
                    # Merge onto existing values so unspecified fields survive.
                    current = {
                        "credential_key": base.get("credential_key"),
                        "metadata": dict(base.get("metadata") or {}),
                    }
                    to_write[credential_type] = current
                if slot is None:
                    current["credential_key"] = body[field]
                else:
                    current["metadata"][slot] = body[field]
            await _upsert_credentials(tenant["id"], to_write)

        return JSONResponse(
            {"status": "ok", "tenant_id": tenant["id"], "updated_fields": provided}
        )
    except Exception:
        log.exception("admin_set_credentials_failed", extra={"tenant_id": tenant_id})
        return JSONResponse(
            {"status": "error", "error": "Database error"}, status_code=500
        )


async def view_credentials(request: Request) -> JSONResponse:
    """GET /admin/tenants/{tenant_id}/credentials — list configured field names."""

    if not _admin_authorized(request):
        return _unauthorized()

    tenant_id = request.path_params["tenant_id"]
    try:
        tenant = await _fetch_tenant(tenant_id)
        if tenant is None:
            return JSONResponse(
                {"status": "error", "error": "Tenant not found"}, status_code=404
            )

        existing = await _fetch_credentials(tenant["id"])
        configured: list[str] = []
        for field in _KNOWN_FIELDS:
            credential_type, slot = _FIELD_MAP[field]
            row = existing.get(credential_type)
            if not row:
                continue
            value = (
                row.get("credential_key")
                if slot is None
                else (row.get("metadata") or {}).get(slot)
            )
            if value is not None and value != "":
                configured.append(field)

        return JSONResponse(
            {"status": "ok", "tenant_id": tenant["id"], "configured": configured}
        )
    except Exception:
        log.exception("admin_view_credentials_failed", extra={"tenant_id": tenant_id})
        return JSONResponse(
            {"status": "error", "error": "Database error"}, status_code=500
        )


async def list_tenants(request: Request) -> JSONResponse:
    """GET /admin/tenants — list all tenants (id, name, created_at)."""

    if not _admin_authorized(request):
        return _unauthorized()
    try:
        tenants = await _list_tenants()
        return JSONResponse({"status": "ok", "tenants": tenants})
    except Exception:
        log.exception("admin_list_tenants_failed")
        return JSONResponse(
            {"status": "error", "error": "Database error"}, status_code=500
        )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def get_admin_routes() -> list[Route]:
    return [
        Route("/admin/tenants", list_tenants, methods=["GET"]),
        Route(
            "/admin/tenants/{tenant_id}/credentials",
            set_credentials,
            methods=["POST"],
        ),
        Route(
            "/admin/tenants/{tenant_id}/credentials",
            view_credentials,
            methods=["GET"],
        ),
    ]


def register_admin_routes(app: Any) -> None:
    """Attach the admin routes to a Starlette app."""

    for route in get_admin_routes():
        app.router.routes.append(route)
