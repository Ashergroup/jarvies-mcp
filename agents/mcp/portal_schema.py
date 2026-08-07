"""Idempotent portal schema for the Jarvies multi-tenant portal (Phase 3).

Mirrors ``agents.mcp.admin_consent.ensure_consent_schema``: the DDL below is
applied best-effort at server startup so a running server converges onto the
portal schema without a manual migration step. The same statements are also
embedded in ``scripts/migrate.py`` so a fresh database can be provisioned
standalone.

Every statement is idempotent (``IF NOT EXISTS`` / ``ADD COLUMN IF NOT EXISTS``)
so repeated application is a no-op. The base tables (``tenants``, ``users``,
``tenant_credentials``) are created by ``scripts/migrate.py``; the ``ALTER``s
here assume they already exist, matching how ``ensure_consent_schema`` alters
``tenants``.
"""

from __future__ import annotations

import logging

from agents.mcp.database import get_conn
from agents.mcp.tenant_policy import TENANT_POLICY_DDL

log = logging.getLogger(__name__)

# Portal schema. Kept in the same order as scripts/migrate.py's appended block
# so the two stay easy to diff.
PORTAL_DDL_STATEMENTS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'member'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS permissions JSONB DEFAULT '[]'::jsonb",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT true",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS invited_by UUID REFERENCES users(id)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS invited_at TIMESTAMPTZ",
    """
    CREATE TABLE IF NOT EXISTS licenses (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID REFERENCES tenants(id),
        plan TEXT,
        seat_count INT DEFAULT 1,
        starts_at TIMESTAMPTZ DEFAULT now(),
        ends_at TIMESTAMPTZ,
        status TEXT DEFAULT 'active',
        notes TEXT,
        created_at TIMESTAMPTZ DEFAULT now(),
        created_by TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_licenses_tenant ON licenses(tenant_id)",
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id BIGSERIAL PRIMARY KEY,
        tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
        actor_type TEXT NOT NULL,
        actor_id TEXT NOT NULL,
        action TEXT NOT NULL,
        target_type TEXT,
        target_id TEXT,
        metadata JSONB,
        created_at TIMESTAMPTZ DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_tenant_created "
    "ON audit_log(tenant_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS connector_installs (
        tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
        connector_key TEXT NOT NULL,
        enabled BOOLEAN DEFAULT true,
        installed_at TIMESTAMPTZ DEFAULT now(),
        installed_by TEXT,
        PRIMARY KEY (tenant_id, connector_key)
    )
    """,
    "ALTER TABLE tenant_credentials ADD COLUMN IF NOT EXISTS credential_ciphertext BYTEA",
    "ALTER TABLE tenant_credentials ADD COLUMN IF NOT EXISTS key_version INT DEFAULT 1",
    # Per-tenant tool guardrail configuration. Separate from tenant_credentials
    # because it holds no secret and is returned in full by the admin API; see
    # agents.mcp.tenant_policy for the resolution path.
    TENANT_POLICY_DDL,
]


async def ensure_portal_schema() -> None:
    """Apply the portal schema idempotently. Best-effort, never raises.

    Called at server startup after the pool is initialised. A failure here is
    logged and swallowed so the server still starts (the portal features then
    surface a clear error rather than taking the process down).
    """

    try:
        async with get_conn() as conn:
            for statement in PORTAL_DDL_STATEMENTS:
                await conn.execute(statement)
        log.info("portal_schema_ensured")
    except Exception:
        log.warning("portal_schema_failed")
