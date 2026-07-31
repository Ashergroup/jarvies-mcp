"""Tests for the Headspace sales portal (portal.sales).

Starlette TestClient over the portal routes, with the DB layer replaced by an
in-memory fake connection (monkeypatching ``portal.sales.get_conn``) so no live
database is needed — mirroring tests/test_admin.py and tests/test_credentials.py.
The fake records every query, so a write and its audit_log row are both visible.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from agents.mcp import config as mcp_config
from portal import sales, sessions
from portal.app import build_portal_routes

ADMIN_KEY = "sales-admin-key-do-not-log"
TOKEN_SECRET = "portal-token-secret"
TENANT_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIES_ADMIN_API_KEY", ADMIN_KEY)
    monkeypatch.setenv("JARVIES_TOKEN_SECRET", TOKEN_SECRET)
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(Starlette(routes=build_portal_routes()))


# ---------------------------------------------------------------------------
# Fake DB
# ---------------------------------------------------------------------------


class _FakeTxn:
    async def __aenter__(self) -> _FakeTxn:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeConn:
    def __init__(self, *, fetch_rows=None, fetchrow_results=None) -> None:
        self.fetch_rows = fetch_rows or []
        self._fetchrow_results = list(fetchrow_results or [])
        self.queries: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args):
        self.queries.append((query, args))
        return self.fetch_rows

    async def fetchrow(self, query: str, *args):
        self.queries.append((query, args))
        return self._fetchrow_results.pop(0) if self._fetchrow_results else None

    async def execute(self, query: str, *args) -> None:
        self.queries.append((query, args))

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()


class _FakeConnCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


def _patch_conn(monkeypatch: pytest.MonkeyPatch, conn: _FakeConn) -> None:
    monkeypatch.setattr(sales, "get_conn", lambda: _FakeConnCtx(conn))


def _login(client: TestClient) -> None:
    resp = client.post(
        "/portal/sales/login", data={"admin_key": ADMIN_KEY}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.cookies.get(sessions.COOKIE_NAME) is not None


def _csrf(client: TestClient) -> str:
    return sessions.csrf_token(client.cookies.get(sessions.COOKIE_NAME))


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------


def test_login_success_sets_cookie_and_redirects(client: TestClient) -> None:
    resp = client.post(
        "/portal/sales/login", data={"admin_key": ADMIN_KEY}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal/sales"
    assert resp.cookies.get(sessions.COOKIE_NAME) is not None


def test_login_wrong_key_401(client: TestClient) -> None:
    resp = client.post(
        "/portal/sales/login", data={"admin_key": "nope"}, follow_redirects=False
    )
    assert resp.status_code == 401
    assert "Invalid admin key" in resp.text
    assert resp.cookies.get(sessions.COOKIE_NAME) is None


def test_login_empty_admin_key_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JARVIES_ADMIN_API_KEY", "")
    mcp_config.get_settings.cache_clear()
    resp = client.post(
        "/portal/sales/login", data={"admin_key": "anything"}, follow_redirects=False
    )
    assert resp.status_code == 401
    assert "not configured" in resp.text


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_dashboard_unauthenticated_redirects_to_login(client: TestClient) -> None:
    resp = client.get("/portal/sales", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal/sales/login"


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_dashboard_renders_tenant_rows(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    conn = _FakeConn(
        fetch_rows=[
            {
                "id": TENANT_ID,
                "microsoft_tenant_id": "ms-tid-1",
                "display_name": "Asher Group",
                "plan": "standard",
                "status": "active",
                "is_active": True,
                "consented_at": None,
                "created_at": None,
                "license_plan": "premium",
                "license_seats": 25,
                "license_status": "active",
            }
        ]
    )
    _patch_conn(monkeypatch, conn)

    resp = client.get("/portal/sales")
    assert resp.status_code == 200
    assert "Asher Group" in resp.text
    assert "premium" in resp.text
    # Active tenant offers a Suspend action.
    assert "Suspend" in resp.text


# ---------------------------------------------------------------------------
# Status toggle
# ---------------------------------------------------------------------------


def test_status_toggle_writes_update_and_audit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    conn = _FakeConn(fetchrow_results=[{"status": "active"}])
    _patch_conn(monkeypatch, conn)

    resp = client.post(
        f"/portal/sales/tenants/{TENANT_ID}/status",
        data={"csrf": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/portal/sales"

    joined = " || ".join(q for q, _ in conn.queries)
    assert "UPDATE tenants SET status" in joined
    assert "INSERT INTO audit_log" in joined
    # active -> suspended
    update_call = next(
        (args for q, args in conn.queries if "UPDATE tenants SET status" in q), None
    )
    assert update_call is not None
    assert update_call[0] == "suspended"


# ---------------------------------------------------------------------------
# License create
# ---------------------------------------------------------------------------


def test_license_create_writes_insert_and_audit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    conn = _FakeConn(fetchrow_results=[{"id": 42}])  # INSERT ... RETURNING id
    _patch_conn(monkeypatch, conn)

    resp = client.post(
        f"/portal/sales/tenants/{TENANT_ID}/licenses",
        data={
            "csrf": _csrf(client),
            "plan": "premium",
            "seat_count": "10",
            "ends_at": "",
            "notes": "annual",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/portal/sales/tenants/{TENANT_ID}/licenses"

    joined = " || ".join(q for q, _ in conn.queries)
    assert "INSERT INTO licenses" in joined
    assert "INSERT INTO audit_log" in joined


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_status_toggle_missing_csrf_403(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    conn = _FakeConn(fetchrow_results=[{"status": "active"}])
    _patch_conn(monkeypatch, conn)

    resp = client.post(
        f"/portal/sales/tenants/{TENANT_ID}/status", data={}, follow_redirects=False
    )
    assert resp.status_code == 403
    # No DB writes happened.
    assert conn.queries == []


def test_status_toggle_wrong_csrf_403(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _login(client)
    conn = _FakeConn(fetchrow_results=[{"status": "active"}])
    _patch_conn(monkeypatch, conn)

    resp = client.post(
        f"/portal/sales/tenants/{TENANT_ID}/status",
        data={"csrf": "forged"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert conn.queries == []


# ---------------------------------------------------------------------------
# Middleware exemption contract
# ---------------------------------------------------------------------------


def test_portal_prefix_is_auth_exempt() -> None:
    from agents.mcp import auth as mcp_auth

    assert mcp_auth.PORTAL_PATH_PREFIX == "/portal"
