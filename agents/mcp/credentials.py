"""Shared per-tenant credential resolver for the DB-backed integrations.

Xero, Cin7, and Freshsales resolve their credentials here: the tenant's row in
``tenant_credentials`` when present, otherwise the process environment variables
(so the existing single-tenant Asher Group config keeps working until migrated).

This replicates the ClickUp tenant pattern (the ``current_tenant`` ContextVar +
``tenant.get_tenant_credentials``) in ONE shared place instead of duplicating it
per integration. Never raises: any DB problem or missing config resolves to
``None`` / base settings, and the calling tool returns its normal
not-configured error.

Storage layout (matches the admin endpoint and the seed migration): one
``tenant_credentials`` row per ``(tenant_id, credential_type)``, the primary
secret in ``credential_key`` and the rest in the ``metadata`` JSONB.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from agents.mcp.config import MCPSettings, get_settings
from agents.mcp.database import get_conn
from agents.mcp.tenant import current_tenant, get_tenant_credentials

log = logging.getLogger(__name__)

# credential_type -> (settings field holding the primary secret,
#                     {metadata slot: settings field}).
# Used both to build the env-var fallback and to overlay a DB row back onto
# MCPSettings so each integration's existing service code is unchanged.
_CREDENTIAL_MAP: dict[str, tuple[str, dict[str, str]]] = {
    "xero": (
        "xero_refresh_token",
        {
            "client_id": "xero_client_id",
            "client_secret": "xero_client_secret",
            "tenant_id": "xero_tenant_id",
        },
    ),
    "cin7": ("cin7_api_key", {"account_id": "cin7_account_id"}),
    "freshsales": ("freshsales_api_key", {"domain": "freshsales_domain"}),
}


@dataclass(frozen=True)
class ResolvedCredentials:
    """Outcome of credential resolution for one integration call."""

    settings: MCPSettings
    from_db: bool


def _env_credentials(credential_type: str) -> dict | None:
    """Build the credential dict from env-var settings, or None if no secret."""

    key_field, meta_map = _CREDENTIAL_MAP[credential_type]
    settings = get_settings()
    key = getattr(settings, key_field, None)
    if not key:
        return None
    metadata = {slot: getattr(settings, field, None) for slot, field in meta_map.items()}
    return {"credential_key": key, "metadata": metadata}


async def _resolve(
    tenant_id: str | None, credential_type: str
) -> tuple[dict | None, bool]:
    """Core resolver — returns ``(creds, from_db)``.

    The tenant's DB row (when it has a ``credential_key``) wins; otherwise the
    env-var fallback; otherwise ``(None, False)``. Never raises — the DB query
    (``tenant.get_tenant_credentials``) already swallows DB errors as ``None``.
    """

    if credential_type not in _CREDENTIAL_MAP:
        return None, False
    if tenant_id:
        row = await get_tenant_credentials(tenant_id, credential_type)
        if row and row.get("credential_key"):
            return {
                "credential_key": row.get("credential_key"),
                "metadata": row.get("metadata") or {},
            }, True
    return _env_credentials(credential_type), False


async def _get_tenant_credentials(
    tenant_id: str | None, credential_type: str
) -> dict | None:
    """Shared resolver (the one the tools go through).

    Returns ``{"credential_key": ..., "metadata": {...}}`` from the tenant's
    ``tenant_credentials`` row, else the env-var fallback, else ``None``. Never
    raises; ``None`` means the tool should return a clean not-configured error.
    """

    creds, _ = await _resolve(tenant_id, credential_type)
    return creds


async def resolve_settings(credential_type: str) -> ResolvedCredentials:
    """Return ``MCPSettings`` with this tenant's credentials overlaid.

    ``tenant_id`` is taken from the ``current_tenant`` ContextVar (set by
    ``TenantResolutionMiddleware``). When the credentials come from the DB they
    are overlaid onto base settings via ``model_copy``; otherwise base settings
    (carrying the env vars) are returned unchanged, so the env path — including
    Asher Group's existing config and every existing test — behaves exactly as
    before.
    """

    base = get_settings()
    tenant = current_tenant()
    tenant_id = tenant["id"] if tenant else None
    creds, from_db = await _resolve(tenant_id, credential_type)
    if not from_db or creds is None:
        return ResolvedCredentials(settings=base, from_db=False)

    key_field, meta_map = _CREDENTIAL_MAP[credential_type]
    meta = creds.get("metadata") or {}
    update: dict[str, str] = {
        key_field: creds.get("credential_key") or getattr(base, key_field)
    }
    for slot, field in meta_map.items():
        value = meta.get(slot)
        if value:
            update[field] = value
    return ResolvedCredentials(settings=base.model_copy(update=update), from_db=True)


async def persist_xero_refresh_token(tenant_id: str, new_refresh_token: str) -> None:
    """Write a rotated Xero refresh token back to the tenant's DB row.

    Xero rotates the refresh token on every use, so the new value from a
    successful ``/token`` response must replace the stored one or the next call
    reuses a consumed (single-use) token and gets a 400. Only used for
    DB-sourced tenants — the env/singleton path keeps its on-disk file.

    Best-effort: any failure is logged and swallowed so the original Xero call
    still succeeds. The token value itself is never logged.
    """

    try:
        async with get_conn() as conn:
            await conn.execute(
                """
                UPDATE tenant_credentials
                SET credential_key = $1, updated_at = NOW()
                WHERE tenant_id::text = $2 AND credential_type = 'xero'
                """,
                new_refresh_token,
                tenant_id,
            )
        log.info("xero_refresh_token_persisted", extra={"tenant_id": tenant_id})
    except Exception:
        log.warning(
            "xero_refresh_token_persist_failed — token kept in memory for this call",
            extra={"tenant_id": tenant_id},
        )
