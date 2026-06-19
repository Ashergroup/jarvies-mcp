"""Tests for the Microsoft admin-consent onboarding flow (#8).

The handlers' DB access goes through small named coroutines in
``agents.mcp.admin_consent`` (``_store_state``, ``_consume_state``,
``_upsert_tenant``, ``_list_consent_tenants``); these are monkeypatched with an
in-memory state store so the tests run without a live database, mirroring
``tests/test_admin.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from agents.mcp import admin_consent, oauth
from agents.mcp import auth as mcp_auth
from agents.mcp import config as mcp_config

ADMIN_KEY = "admin-secret-key-do-not-log"
PUBLIC_URL = "https://jarvies.example.com"
CLIENT_ID = "azure-client-abc"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIES_ADMIN_API_KEY", ADMIN_KEY)
    monkeypatch.setenv("JARVIES_PUBLIC_URL", PUBLIC_URL)
    monkeypatch.setenv("AZURE_CLIENT_ID", CLIENT_ID)
    mcp_config.get_settings.cache_clear()


@pytest.fixture
def states(monkeypatch: pytest.MonkeyPatch) -> dict:
    """In-memory oauth_states store with single-use semantics."""

    store: dict = {}

    async def fake_store(state, tenant_hint, plan, expires_at):
        store[state] = {
            "state": state,
            "tenant_hint": tenant_hint,
            "plan": plan,
            "expires_at": expires_at,
        }

    async def fake_consume(state):
        row = store.pop(state, None)  # single-use: deleted on read
        if row is None:
            return None
        if row["expires_at"] is not None and row["expires_at"] < datetime.now(UTC):
            return None
        return row

    monkeypatch.setattr(admin_consent, "_store_state", fake_store)
    monkeypatch.setattr(admin_consent, "_consume_state", fake_consume)
    return store


@pytest.fixture
def client() -> TestClient:
    routes = [
        *admin_consent.get_consent_routes(),
        Route("/auth/callback", oauth.auth_callback, methods=["GET"]),
    ]
    return TestClient(Starlette(routes=routes))


# ---------------------------------------------------------------------------
# /auth/start
# ---------------------------------------------------------------------------


def test_auth_start_stores_state_and_redirects(
    client: TestClient, env: None, states: dict
) -> None:
    resp = client.get(
        "/auth/start?tenant_hint=Asher%20Group&plan=premium", follow_redirects=False
    )
    assert resp.status_code == 302

    location = resp.headers["location"]
    parsed = urlparse(location)
    assert parsed.scheme == "https"
    assert parsed.netloc == "login.microsoftonline.com"
    assert parsed.path == "/common/adminconsent"

    qs = parse_qs(parsed.query)
    assert qs["client_id"] == [CLIENT_ID]
    assert qs["redirect_uri"] == [f"{PUBLIC_URL}/auth/callback"]
    assert qs["prompt"] == ["select_account"]
    state = qs["state"][0]

    # State persisted with the hint + plan from the query.
    assert state in states
    assert states[state]["tenant_hint"] == "Asher Group"
    assert states[state]["plan"] == "premium"


def test_auth_start_defaults_plan_to_standard(
    client: TestClient, env: None, states: dict
) -> None:
    resp = client.get("/auth/start", follow_redirects=False)
    assert resp.status_code == 302
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    assert states[state]["plan"] == "standard"
    assert states[state]["tenant_hint"] is None


# ---------------------------------------------------------------------------
# /auth/callback — admin consent branch
# ---------------------------------------------------------------------------


def test_callback_valid_consent_upserts_tenant(
    client: TestClient, env: None, states: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    states["s-good"] = {
        "state": "s-good",
        "tenant_hint": "Asher Group",
        "plan": "premium",
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }
    captured: dict = {}

    async def fake_upsert(microsoft_tenant_id, display_name, plan):
        captured.update(
            microsoft_tenant_id=microsoft_tenant_id, display_name=display_name, plan=plan
        )

    monkeypatch.setattr(admin_consent, "_upsert_tenant", fake_upsert)

    resp = client.get("/auth/callback?admin_consent=True&tenant=ms-tid-123&state=s-good")
    assert resp.status_code == 200
    assert "Jarvies Connected" in resp.text
    assert "ms-tid-123" in resp.text
    assert "premium" in resp.text

    assert captured == {
        "microsoft_tenant_id": "ms-tid-123",
        "display_name": "Asher Group",
        "plan": "premium",
    }
    # Single-use: the state row is gone after a successful consent.
    assert "s-good" not in states


def test_callback_expired_state_errors_no_upsert(
    client: TestClient, env: None, states: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    states["s-old"] = {
        "state": "s-old",
        "tenant_hint": None,
        "plan": "standard",
        "expires_at": datetime.now(UTC) - timedelta(minutes=1),
    }

    async def fail_upsert(*args, **kwargs):
        raise AssertionError("tenant must not be upserted for an expired state")

    monkeypatch.setattr(admin_consent, "_upsert_tenant", fail_upsert)

    resp = client.get("/auth/callback?admin_consent=True&tenant=ms-tid-123&state=s-old")
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "invalid or has expired" in resp.text
    assert "s-old" not in states  # deleted even though expired


def test_callback_missing_state_errors(client: TestClient, env: None, states: dict) -> None:
    resp = client.get("/auth/callback?admin_consent=True&tenant=ms-tid-123")
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "no state" in resp.text.lower()


def test_callback_microsoft_error_shows_reason(
    client: TestClient, env: None, states: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    states["s-err"] = {
        "state": "s-err",
        "tenant_hint": None,
        "plan": "standard",
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }

    async def fail_upsert(*args, **kwargs):
        raise AssertionError("no tenant on a Microsoft error")

    monkeypatch.setattr(admin_consent, "_upsert_tenant", fail_upsert)

    resp = client.get(
        "/auth/callback?error=access_denied"
        "&error_description=AADSTS650056%3A+admin+did+not+consent&state=s-err"
    )
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "AADSTS650056" in resp.text
    assert "s-err" not in states  # state consumed before erroring


def test_callback_state_is_single_use(
    client: TestClient, env: None, states: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    states["s-once"] = {
        "state": "s-once",
        "tenant_hint": None,
        "plan": "standard",
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }

    async def ok_upsert(*args, **kwargs):
        return None

    monkeypatch.setattr(admin_consent, "_upsert_tenant", ok_upsert)

    first = client.get("/auth/callback?admin_consent=True&tenant=ms-tid-9&state=s-once")
    assert first.status_code == 200
    assert "Jarvies Connected" in first.text

    second = client.get("/auth/callback?admin_consent=True&tenant=ms-tid-9&state=s-once")
    assert second.status_code == 200
    assert "Connection Failed" in second.text
    assert "invalid or has expired" in second.text


def test_callback_with_code_runs_existing_flow_unchanged(
    client: TestClient, env: None, states: dict
) -> None:
    # A `code` callback is the user OAuth flow; the admin branch must defer.
    # With no matching in-memory pending auth, the existing handler returns its
    # JSON 404 ("unknown or expired state") — proving the admin branch did not
    # intercept it (it would have returned an HTML page).
    resp = client.get("/auth/callback?code=abc123&state=unknown-state")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "not_found"


# ---------------------------------------------------------------------------
# /auth/tenants
# ---------------------------------------------------------------------------


def test_auth_tenants_with_valid_key(
    client: TestClient, env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list():
        return [
            {
                "tenant_id": "uuid-1",
                "microsoft_tenant_id": "ms-1",
                "display_name": "Asher Group",
                "plan": "standard",
                "status": "active",
                "consented_at": "2026-06-19T00:00:00+00:00",
                "created_at": "2026-06-19T00:00:00+00:00",
            }
        ]

    monkeypatch.setattr(admin_consent, "_list_consent_tenants", fake_list)

    resp = client.get("/auth/tenants", headers={"X-API-Key": ADMIN_KEY})
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["count"] == 1
    assert data["tenants"][0]["microsoft_tenant_id"] == "ms-1"
    assert data["tenants"][0]["plan"] == "standard"


def test_auth_tenants_without_key_returns_401(client: TestClient, env: None) -> None:
    resp = client.get("/auth/tenants")
    assert resp.status_code == 401
    assert resp.json()["status"] == "error"


# ---------------------------------------------------------------------------
# Middleware bypass list
# ---------------------------------------------------------------------------


def test_consent_paths_bypass_mcp_auth() -> None:
    # /auth/start and /auth/tenants must be in the MCP auth bypass list, and
    # /auth/callback stays public (via OAUTH_PUBLIC_PATHS).
    assert "/auth/start" in mcp_auth.PUBLIC_PATHS
    assert "/auth/tenants" in mcp_auth.PUBLIC_PATHS
    assert "/auth/callback" in mcp_auth.PUBLIC_PATHS
