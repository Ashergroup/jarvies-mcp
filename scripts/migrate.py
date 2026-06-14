"""Idempotent schema + seed migration for the Jarvies multi-tenant layer.

Creates the tenants / users / user_tokens / tenant_credentials tables (Phase 2A)
if they do not already exist, then seeds the Asher Group tenant and its ClickUp
credential row. Safe to run repeatedly.

Usage::

    # DATABASE_URL must be set (never hardcode it). For RDS include sslmode:
    #   postgresql://USER:PASS@HOST:5432/jarvies?sslmode=require
    python scripts/migrate.py

The ClickUp credential key is read from CLICKUP_API_KEY (falling back to the
repo's existing CLICKUP_API_TOKEN). asyncpg only — no ORM.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import asyncpg

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a dev convenience only.
    pass


SEED_MICROSOFT_TENANT_ID = "d7afc5b8-d7f1-48ba-a6b5-d2f21608bb66"
SEED_DISPLAY_NAME = "Asher Group / Niche Group"
SEED_CLICKUP_METADATA = {
    "team_id": "90121402212",
    "ir_list_id": "901215521156",
    "pipeline_list_id": "901215521124",
}


DDL_STATEMENTS = [
    # gen_random_uuid() lives in pgcrypto on older servers; built into core on
    # PostgreSQL 13+. Creating the extension is harmless when already present.
    "CREATE EXTENSION IF NOT EXISTS pgcrypto",
    """
    CREATE TABLE IF NOT EXISTS tenants (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        microsoft_tenant_id TEXT UNIQUE,
        display_name TEXT,
        created_at TIMESTAMPTZ DEFAULT now(),
        is_active BOOLEAN DEFAULT true
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID REFERENCES tenants(id),
        microsoft_user_id TEXT,
        email TEXT,
        display_name TEXT,
        created_at TIMESTAMPTZ DEFAULT now(),
        UNIQUE(tenant_id, microsoft_user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_tokens (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id UUID REFERENCES users(id),
        access_token TEXT,
        refresh_token TEXT,
        expires_at TIMESTAMPTZ,
        scope TEXT,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tenant_credentials (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID REFERENCES tenants(id),
        credential_type TEXT,
        credential_key TEXT,
        metadata JSONB,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now(),
        UNIQUE(tenant_id, credential_type)
    )
    """,
]


async def _create_schema(conn: asyncpg.Connection) -> None:
    for statement in DDL_STATEMENTS:
        await conn.execute(statement)
    print("schema: tables ensured (tenants, users, user_tokens, tenant_credentials)")


async def _seed(conn: asyncpg.Connection) -> None:
    tenant_id = await conn.fetchval(
        """
        INSERT INTO tenants (microsoft_tenant_id, display_name)
        VALUES ($1, $2)
        ON CONFLICT (microsoft_tenant_id)
        DO UPDATE SET display_name = EXCLUDED.display_name
        RETURNING id
        """,
        SEED_MICROSOFT_TENANT_ID,
        SEED_DISPLAY_NAME,
    )
    print(f"seed: tenant {SEED_DISPLAY_NAME} -> {tenant_id}")

    clickup_key = os.environ.get("CLICKUP_API_KEY") or os.environ.get("CLICKUP_API_TOKEN")
    if not clickup_key:
        print(
            "seed: WARNING — neither CLICKUP_API_KEY nor CLICKUP_API_TOKEN is set; "
            "inserting clickup credential row with a NULL key. Set the env var and "
            "re-run to populate it."
        )

    await conn.execute(
        """
        INSERT INTO tenant_credentials (tenant_id, credential_type, credential_key, metadata)
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (tenant_id, credential_type)
        DO UPDATE SET
            credential_key = EXCLUDED.credential_key,
            metadata = EXCLUDED.metadata,
            updated_at = now()
        """,
        tenant_id,
        "clickup",
        clickup_key,
        json.dumps(SEED_CLICKUP_METADATA),
    )
    key_state = "set" if clickup_key else "NULL"
    print(f"seed: tenant_credentials clickup row upserted (credential_key={key_state})")


async def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print(
            "ERROR: DATABASE_URL is not set. Export it (with sslmode=require "
            "for RDS) and re-run."
        )
        return 1

    try:
        conn = await asyncpg.connect(dsn=dsn, timeout=15)
    except (OSError, asyncpg.PostgresError) as exc:
        print(f"ERROR: could not connect to the database: {exc.__class__.__name__}: {exc}")
        return 1
    try:
        # One transaction so a partial failure leaves nothing half-applied.
        async with conn.transaction():
            await _create_schema(conn)
            await _seed(conn)
    finally:
        await conn.close()
    print("migrate: done")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
