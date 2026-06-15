"""Tests for the /admin tenant-credential endpoints.

The handlers' DB access goes through small named coroutines in
``agents.mcp.admin`` (``_fetch_tenant``, ``_fetch_credentials``,
``_upsert_credentials``, ``_list_tenants``); these are monkeypatched so the
tests run without a live database, mirroring the rest of the suite.
"""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from agents.mcp import admin
from agents.mcp import config as mcp_config

ADMIN_KEY = "admin-secret-key-do-not-log"
TENANT_ID = "11111111-1111-1111-1111-111111111111"
HEADERS = {"X-API-Key": ADMIN_KEY}


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("JARVIES_ADMIN_API_KEY", ADMIN_KEY)
    mcp_config.get_settings.cache_clear()
    app = Starlette(routes=admin.get_admin_routes())
    return TestClient(app)


def _patch_tenant_found(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_tenant(tenant_id: str):
        if tenant_id == TENANT_ID:
            return {"id": TENANT_ID, "name": "Asher Group", "created_at": None}
        return None

    monkeypatch.setattr(admin, "_fetch_tenant", fake_fetch_tenant)


# ---------------------------------------------------------------------------
# Auth — 401
# ---------------------------------------------------------------------------


def test_missing_api_key_returns_401(client: TestClient) -> None:
    resp = client.get("/admin/tenants")
    assert resp.status_code == 401
    assert resp.json()["status"] == "error"


def test_wrong_api_key_returns_401(client: TestClient) -> None:
    resp = client.get("/admin/tenants", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 401


def test_wrong_api_key_on_post_returns_401(client: TestClient) -> None:
    resp = client.post(
        f"/admin/tenants/{TENANT_ID}/credentials",
        headers={"X-API-Key": "wrong"},
        json={"clickup_token": "x"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Tenant not found — 404
# ---------------------------------------------------------------------------


def test_set_credentials_tenant_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tenant_found(monkeypatch)
    resp = client.post(
        "/admin/tenants/does-not-exist/credentials",
        headers=HEADERS,
        json={"clickup_token": "x"},
    )
    assert resp.status_code == 404
    assert resp.json() == {"status": "error", "error": "Tenant not found"}


# ---------------------------------------------------------------------------
# Successful upsert
# ---------------------------------------------------------------------------


def test_set_credentials_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tenant_found(monkeypatch)
    captured: dict = {}

    async def fake_fetch_credentials(tenant_id: str):
        return {}

    async def fake_upsert(tenant_uuid: str, rows: dict):
        captured["tenant_uuid"] = tenant_uuid
        captured["rows"] = rows

    monkeypatch.setattr(admin, "_fetch_credentials", fake_fetch_credentials)
    monkeypatch.setattr(admin, "_upsert_credentials", fake_upsert)

    resp = client.post(
        f"/admin/tenants/{TENANT_ID}/credentials",
        headers=HEADERS,
        json={
            "clickup_token": "cu-token",
            "xero_client_id": "xero-id",
            "xero_refresh_token": "xero-refresh",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["tenant_id"] == TENANT_ID
    assert set(data["updated_fields"]) == {
        "clickup_token",
        "xero_client_id",
        "xero_refresh_token",
    }

    # Storage mapping: primary secret -> credential_key, rest -> metadata.
    assert captured["tenant_uuid"] == TENANT_ID
    assert captured["rows"]["clickup"]["credential_key"] == "cu-token"
    assert captured["rows"]["xero"]["credential_key"] == "xero-refresh"
    assert captured["rows"]["xero"]["metadata"]["client_id"] == "xero-id"


# ---------------------------------------------------------------------------
# Partial update — only sent fields change; others preserved (patch semantics)
# ---------------------------------------------------------------------------


def test_partial_update_preserves_unsent_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tenant_found(monkeypatch)
    captured: dict = {}

    async def fake_fetch_credentials(tenant_id: str):
        # Existing xero row already has all four fields populated.
        return {
            "xero": {
                "credential_key": "old-refresh",
                "metadata": {
                    "client_id": "old-id",
                    "client_secret": "old-secret",
                    "tenant_id": "old-tid",
                },
            }
        }

    async def fake_upsert(tenant_uuid: str, rows: dict):
        captured["rows"] = rows

    monkeypatch.setattr(admin, "_fetch_credentials", fake_fetch_credentials)
    monkeypatch.setattr(admin, "_upsert_credentials", fake_upsert)

    # Send exactly two fields.
    resp = client.post(
        f"/admin/tenants/{TENANT_ID}/credentials",
        headers=HEADERS,
        json={"xero_client_id": "new-id", "xero_refresh_token": "new-refresh"},
    )
    assert resp.status_code == 200
    assert set(resp.json()["updated_fields"]) == {
        "xero_client_id",
        "xero_refresh_token",
    }

    xero = captured["rows"]["xero"]
    # The two sent fields changed...
    assert xero["credential_key"] == "new-refresh"
    assert xero["metadata"]["client_id"] == "new-id"
    # ...and the two unsent fields were preserved, not wiped.
    assert xero["metadata"]["client_secret"] == "old-secret"
    assert xero["metadata"]["tenant_id"] == "old-tid"


# ---------------------------------------------------------------------------
# GET credentials — field names only, never values
# ---------------------------------------------------------------------------


def test_view_credentials_returns_names_not_values(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_tenant_found(monkeypatch)

    async def fake_fetch_credentials(tenant_id: str):
        return {
            "clickup": {
                "credential_key": "super-secret-token",
                "metadata": {"team_id": "team-123"},
            },
            "xero": {
                "credential_key": "refresh-secret",
                "metadata": {"client_id": "cid-secret", "client_secret": "cs-secret"},
            },
        }

    monkeypatch.setattr(admin, "_fetch_credentials", fake_fetch_credentials)

    resp = client.get(f"/admin/tenants/{TENANT_ID}/credentials", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["tenant_id"] == TENANT_ID
    assert set(data["configured"]) == {
        "clickup_token",
        "xero_refresh_token",
        "xero_client_id",
        "xero_client_secret",
    }

    # No secret value may appear anywhere in the response.
    blob = json.dumps(data)
    for secret in ("super-secret-token", "refresh-secret", "cid-secret", "cs-secret"):
        assert secret not in blob


# ---------------------------------------------------------------------------
# GET /admin/tenants — list
# ---------------------------------------------------------------------------


def test_list_tenants(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_list_tenants():
        return [
            {
                "tenant_id": TENANT_ID,
                "name": "Asher Group",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]

    monkeypatch.setattr(admin, "_list_tenants", fake_list_tenants)

    resp = client.get("/admin/tenants", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["tenants"][0]["tenant_id"] == TENANT_ID
    assert data["tenants"][0]["name"] == "Asher Group"


# ---------------------------------------------------------------------------
# DB error — 500
# ---------------------------------------------------------------------------


def test_db_error_returns_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(admin, "_list_tenants", boom)

    resp = client.get("/admin/tenants", headers=HEADERS)
    assert resp.status_code == 500
    assert resp.json()["status"] == "error"
