"""Tests for the Phase 2A database / tenant layer.

Two tiers:

* Live-DB tests (pool init, migrate idempotency, seeded credential lookup) run
  only when ``DATABASE_URL`` is set AND the database is reachable; otherwise
  they skip, so the suite stays green in environments without Postgres.
* Behaviour tests (tenant middleware attaches to request.state; ClickUp tools
  use tenant credentials when a tenant is resolved and fall back to env vars
  when not) run everywhere — they mock the DB lookups.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path

import asyncpg
import httpx
import pytest
import respx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agents.mcp import config as mcp_config
from agents.mcp import database, tenant
from agents.mcp.tools import clickup_tools

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Live-DB availability gate
# ---------------------------------------------------------------------------


async def _try_connect() -> bool:
    dsn = os.environ.get("DATABASE_URL")
    conn = await asyncio.wait_for(asyncpg.connect(dsn=dsn), timeout=5)
    await conn.close()
    return True


def _db_available() -> bool:
    if not os.environ.get("DATABASE_URL"):
        return False
    try:
        return asyncio.run(_try_connect())
    except Exception:
        return False


_DB_AVAILABLE = _db_available()
requires_db = pytest.mark.skipif(
    not _DB_AVAILABLE,
    reason="DATABASE_URL not set or database unreachable",
)


def _load_migrate():
    spec = importlib.util.spec_from_file_location(
        "jarvies_migrate", REPO_ROOT / "scripts" / "migrate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def _close_pool_after():
    yield
    await database.close_pool()


# ---------------------------------------------------------------------------
# Live-DB tests
# ---------------------------------------------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_pool_init_and_get_conn(_close_pool_after) -> None:
    pool = await database.init_pool()
    assert pool is not None
    # init is idempotent — same pool object.
    assert await database.get_pool() is pool
    async with database.get_conn() as conn:
        assert await conn.fetchval("SELECT 1") == 1


@requires_db
@pytest.mark.asyncio
async def test_migrate_runs_idempotently(_close_pool_after) -> None:
    migrate = _load_migrate()
    assert await migrate.main() == 0
    # Second run must not error (CREATE IF NOT EXISTS + ON CONFLICT upserts).
    assert await migrate.main() == 0


@requires_db
@pytest.mark.asyncio
async def test_get_tenant_credentials_for_seeded_tenant(_close_pool_after) -> None:
    migrate = _load_migrate()
    await migrate.main()

    async with database.get_conn() as conn:
        tenant_id = await conn.fetchval(
            "SELECT id FROM tenants WHERE microsoft_tenant_id = $1",
            migrate.SEED_MICROSOFT_TENANT_ID,
        )
    assert tenant_id is not None

    creds = await tenant.get_tenant_credentials(str(tenant_id), "clickup")
    assert creds is not None
    assert creds["metadata"]["team_id"] == "90121402212"
    assert creds["metadata"]["ir_list_id"] == "901215521156"
    assert creds["metadata"]["pipeline_list_id"] == "901215521124"


# ---------------------------------------------------------------------------
# Tenant middleware → request.state (no DB; load_tenant is mocked)
# ---------------------------------------------------------------------------


def _build_state_echo_app() -> Starlette:
    async def echo(request: Request) -> JSONResponse:
        t = getattr(request.state, "tenant", "MISSING")
        return JSONResponse({"tenant": t})

    app = Starlette(routes=[Route("/echo", echo)])
    app.add_middleware(tenant.TenantResolutionMiddleware)
    return app


def test_middleware_attaches_tenant_when_header_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = {
        "id": "11111111-1111-1111-1111-111111111111",
        "microsoft_tenant_id": "d7afc5b8-d7f1-48ba-a6b5-d2f21608bb66",
        "display_name": "Asher Group / Niche Group",
        "is_active": True,
    }

    async def fake_load_tenant(tenant_id: str):
        return fake if tenant_id == fake["id"] else None

    monkeypatch.setattr(tenant, "load_tenant", fake_load_tenant)

    client = TestClient(_build_state_echo_app())
    resp = client.get("/echo", headers={"X-Tenant-ID": fake["id"]})
    assert resp.status_code == 200
    assert resp.json()["tenant"] == fake


def test_middleware_tenant_is_none_without_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_load_tenant(tenant_id: str):  # pragma: no cover - not called
        raise AssertionError("load_tenant should not be called without a header")

    monkeypatch.setattr(tenant, "load_tenant", fake_load_tenant)

    client = TestClient(_build_state_echo_app())
    resp = client.get("/echo")
    assert resp.status_code == 200
    assert resp.json()["tenant"] is None


# ---------------------------------------------------------------------------
# ClickUp tools: per-tenant credentials vs env-var fallback
# ---------------------------------------------------------------------------

IR_LIST_ID = "ir-list-uuid"


def _write_ir_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "lists": {
            "investor_relations": {
                "list_id": IR_LIST_ID,
                "statuses": ["ACTIVE", "NOT A FIT", "DORMANT"],
                "fields": {},
            },
            "fundraising_pipeline": {
                "list_id": "pl-list-uuid",
                "statuses": ["LEAD IDENTIFIED"],
                "fields": {},
            },
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _set_env(monkeypatch: pytest.MonkeyPatch, fields_path: Path) -> None:
    env = {
        "CLICKUP_API_TOKEN": "env-token",
        "CLICKUP_TEAM_ID": "env-team",
        "CLICKUP_IR_LIST_ID": IR_LIST_ID,
        "CLICKUP_PIPELINE_LIST_ID": "pl-list-uuid",
        "CLICKUP_BASE_URL": "https://api.clickup.test/api/v2",
        "CLICKUP_CUSTOM_FIELDS_CONFIG_PATH": str(fields_path),
        "MCP_TOOL_RESULT_LIMIT": "50",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.mark.asyncio
async def test_clickup_uses_tenant_credentials_when_tenant_resolved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_ir_config(fields_path)
    _set_env(monkeypatch, fields_path)

    tenant_ir_list = "tenant-ir-list"

    async def fake_creds(tenant_id: str, credential_type: str):
        assert credential_type == "clickup"
        return {
            "credential_key": "tenant-token",
            "metadata": {
                "team_id": "tenant-team",
                "ir_list_id": tenant_ir_list,
                "pipeline_list_id": "tenant-pl-list",
            },
        }

    monkeypatch.setattr(clickup_tools, "get_tenant_credentials", fake_creds)

    token = tenant.set_current_tenant({"id": "tenant-uuid", "display_name": "T"})
    try:
        with respx.mock(assert_all_called=True) as mock:
            route = mock.get(
                f"https://api.clickup.test/api/v2/list/{tenant_ir_list}/task"
            ).mock(return_value=httpx.Response(200, json={"tasks": []}))
            result = await clickup_tools.clickup_list_tasks(
                list_key="investor_relations",
                permissions=["fundraising_access"],
            )
    finally:
        tenant.reset_current_tenant(token)

    assert result["status"] == "ok"
    # The tenant's token and list id were used, not the env values.
    request = route.calls[0].request
    assert request.headers["authorization"] == "tenant-token"
    assert tenant_ir_list in str(request.url)


@pytest.mark.asyncio
async def test_clickup_falls_back_to_env_when_no_tenant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fields_path = tmp_path / "clickup_fields.json"
    _write_ir_config(fields_path)
    _set_env(monkeypatch, fields_path)

    # No tenant context set → must use env token + env list id.
    assert tenant.current_tenant() is None

    async def fail_creds(tenant_id: str, credential_type: str):  # pragma: no cover
        raise AssertionError("get_tenant_credentials must not be called without a tenant")

    monkeypatch.setattr(clickup_tools, "get_tenant_credentials", fail_creds)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(
            f"https://api.clickup.test/api/v2/list/{IR_LIST_ID}/task"
        ).mock(return_value=httpx.Response(200, json={"tasks": []}))
        result = await clickup_tools.clickup_list_tasks(
            list_key="investor_relations",
            permissions=["fundraising_access"],
        )

    assert result["status"] == "ok"
    request = route.calls[0].request
    assert request.headers["authorization"] == "env-token"
    assert IR_LIST_ID in str(request.url)
