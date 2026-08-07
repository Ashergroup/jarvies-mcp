"""Admin HTTP endpoints for tenant credential and policy management.

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

Guardrail policy lives alongside, under the same auth and the same patch-merge
semantics, in ``tenant_policies`` (one row per ``(tenant_id, policy_type)``, the
document in the ``policy`` JSONB — see ``agents.mcp.tenant_policy``). Policy
values are returned in full where credentials return field names only: policy
carries no secret, and an admin cannot fix a guardrail they cannot read.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agents.mcp import credentials, tenant_policy
from agents.mcp.config import get_settings
from agents.mcp.database import get_conn
from agents.mcp.tenant_policy import (
    NULLABLE_POLICY_FIELDS,
    POLICY_FIELDS,
    PolicyFieldError,
    coerce_freshdesk_reply_policy,
    validate_policy_field,
)

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
    "freshdesk_api_key": ("freshdesk", None),
    "freshdesk_domain": ("freshdesk", "domain"),
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
    """Return {credential_type: {"credential_key": <plaintext>, "metadata": {...}}}.

    ``credential_key`` is the DECRYPTED primary secret: encrypted rows are
    decrypted (via the shared ``credentials.decrypt_credential_row``) so the
    patch-merge in ``set_credentials`` preserves an existing secret when a
    partial update omits it. Legacy plaintext rows pass through unchanged. The
    decrypted value never leaves the server (``view_credentials`` returns field
    names only).
    """

    async with get_conn() as conn:
        rows = await conn.fetch(
            "SELECT credential_type, credential_key, credential_ciphertext, "
            "key_version, metadata FROM tenant_credentials WHERE tenant_id::text = $1",
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
            "credential_key": credentials.decrypt_credential_row(
                {
                    "credential_key": row["credential_key"],
                    "credential_ciphertext": row["credential_ciphertext"],
                    "key_version": row["key_version"],
                }
            ),
            "metadata": metadata or {},
        }
    return result


async def _upsert_credentials(tenant_uuid: str, rows: dict[str, dict[str, Any]]) -> None:
    """Upsert one tenant_credentials row per credential_type in `rows`.

    `rows` maps credential_type -> {"credential_key": ..., "metadata": {...}}.
    Delegates to the shared ``credentials.upsert_tenant_credentials`` so the
    primary secret is encrypted at rest (credential_ciphertext + key_version)
    and never stored as plaintext — the admin API no longer writes the table
    directly.
    """

    await credentials.upsert_tenant_credentials(tenant_uuid, rows)


async def _fetch_policies(tenant_id: str) -> dict[str, dict[str, Any]]:
    """Return {policy_type: <stored document>} for a tenant."""

    return await tenant_policy.fetch_tenant_policies(tenant_id)


async def _upsert_policy(
    tenant_uuid: str, policy_type: str, policy: dict[str, Any]
) -> None:
    """Write one tenant_policies row (already merged by the caller)."""

    await tenant_policy.upsert_tenant_policy(tenant_uuid, policy_type, policy)


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


async def set_policy(request: Request) -> JSONResponse:
    """POST /admin/tenants/{tenant_id}/policy — patch-update guardrail policy.

    Same auth and same patch-merge semantics as ``set_credentials``: only the
    fields supplied are written, merged onto the tenant's existing document, so
    unsent fields survive.

    One deliberate difference. ``set_credentials`` reads a null as "not
    supplied", because a null secret can only mean "leave it alone". Here null
    is a legal value for a nullable field — it is how an admin clears
    ``escalation_group_id`` — so a null on a nullable field is applied, and a
    null on any other field is a 400 rather than a silent no-op.
    """

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

    # Validate before touching the database so a bad field changes nothing.
    provided: list[str] = []
    coerced: dict[str, Any] = {}
    for field in POLICY_FIELDS:
        if field not in body:
            continue
        value = body[field]
        if value is None and field not in NULLABLE_POLICY_FIELDS:
            return JSONResponse(
                {"status": "error", "error": f"{field} may not be null"},
                status_code=400,
            )
        try:
            result = validate_policy_field(field, value)
        except PolicyFieldError as exc:
            return JSONResponse(
                {"status": "error", "error": str(exc)}, status_code=400
            )
        coerced[field] = list(result) if isinstance(result, tuple) else result
        provided.append(field)

    try:
        tenant = await _fetch_tenant(tenant_id)
        if tenant is None:
            return JSONResponse(
                {"status": "error", "error": "Tenant not found"}, status_code=404
            )

        if provided:
            existing = await _fetch_policies(tenant["id"])
            to_write: dict[str, dict[str, Any]] = {}
            for field in provided:
                policy_type = POLICY_FIELDS[field]
                document = to_write.get(policy_type)
                if document is None:
                    # Merge onto the stored document so unsent fields survive.
                    document = dict(existing.get(policy_type) or {})
                    to_write[policy_type] = document
                document[field] = coerced[field]
            for policy_type, document in to_write.items():
                await _upsert_policy(tenant["id"], policy_type, document)

        return JSONResponse(
            {"status": "ok", "tenant_id": tenant["id"], "updated_fields": provided}
        )
    except Exception:
        log.exception("admin_set_policy_failed", extra={"tenant_id": tenant_id})
        return JSONResponse(
            {"status": "error", "error": "Database error"}, status_code=500
        )


async def view_policy(request: Request) -> JSONResponse:
    """GET /admin/tenants/{tenant_id}/policy — return the effective policy.

    Unlike ``view_credentials``, which returns field names only, this returns
    values: policy carries no secret, and an admin cannot correct a guardrail
    they cannot read. ``policy`` is what the tools will actually apply —
    stored values merged over the defaults — and ``configured`` lists the
    fields the tenant has explicitly set, so defaults are distinguishable from
    deliberate choices that happen to match them.
    """

    if not _admin_authorized(request):
        return _unauthorized()

    tenant_id = request.path_params["tenant_id"]
    try:
        tenant = await _fetch_tenant(tenant_id)
        if tenant is None:
            return JSONResponse(
                {"status": "error", "error": "Tenant not found"}, status_code=404
            )

        policies = await _fetch_policies(tenant["id"])
        stored = policies.get(tenant_policy.FRESHDESK_REPLY_POLICY) or {}
        effective = coerce_freshdesk_reply_policy(stored).as_dict()
        configured = [field for field in POLICY_FIELDS if field in stored]

        return JSONResponse(
            {
                "status": "ok",
                "tenant_id": tenant["id"],
                "policy_type": tenant_policy.FRESHDESK_REPLY_POLICY,
                "policy": effective,
                "configured": configured,
            }
        )
    except Exception:
        log.exception("admin_view_policy_failed", extra={"tenant_id": tenant_id})
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
        Route("/admin/tenants/{tenant_id}/policy", set_policy, methods=["POST"]),
        Route("/admin/tenants/{tenant_id}/policy", view_policy, methods=["GET"]),
    ]


def register_admin_routes(app: Any) -> None:
    """Attach the admin routes to a Starlette app."""

    for route in get_admin_routes():
        app.router.routes.append(route)
