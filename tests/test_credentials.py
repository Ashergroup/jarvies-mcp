"""Tests for the shared per-tenant credential resolver (agents.mcp.credentials).

Covers the three resolution paths the prompt requires: DB row, env-var
fallback, and missing credentials (None). Plus integration checks that each of
Xero / Cin7 / Freshsales actually uses DB credentials when a tenant is resolved
and env credentials otherwise.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from agents.mcp import config as mcp_config
from agents.mcp import credentials
from agents.mcp import tenant as mcp_tenant
from agents.mcp.tools import cin7_tools, freshsales_tools, xero_tools

TENANT = {"id": "33333333-3333-3333-3333-333333333333", "display_name": "Acme"}


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


@pytest.fixture
def _with_tenant():
    token = mcp_tenant.set_current_tenant(TENANT)
    try:
        yield
    finally:
        mcp_tenant.reset_current_tenant(token)


# ---------------------------------------------------------------------------
# Resolver: DB path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_returns_db_row(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_db(tenant_id: str, credential_type: str):
        assert tenant_id == TENANT["id"]
        assert credential_type == "xero"
        return {
            "credential_key": "db-refresh",
            "metadata": {"client_id": "db-cid", "client_secret": "db-csec"},
        }

    monkeypatch.setattr(credentials, "get_tenant_credentials", fake_db)
    creds = await credentials._get_tenant_credentials(TENANT["id"], "xero")
    assert creds == {
        "credential_key": "db-refresh",
        "metadata": {"client_id": "db-cid", "client_secret": "db-csec"},
    }


# ---------------------------------------------------------------------------
# Resolver: env-var fallback (no DB row)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIN7_API_KEY", "env-cin7-key")
    monkeypatch.setenv("CIN7_ACCOUNT_ID", "env-cin7-acct")
    mcp_config.get_settings.cache_clear()

    async def no_row(tenant_id: str, credential_type: str):
        return None

    monkeypatch.setattr(credentials, "get_tenant_credentials", no_row)
    creds = await credentials._get_tenant_credentials(TENANT["id"], "cin7")
    assert creds == {
        "credential_key": "env-cin7-key",
        "metadata": {"account_id": "env-cin7-acct"},
    }


@pytest.mark.asyncio
async def test_resolver_falls_back_to_env_when_no_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRESHSALES_API_KEY", "env-fs-key")
    monkeypatch.setenv("FRESHSALES_DOMAIN", "env.myfreshworks.com")
    mcp_config.get_settings.cache_clear()

    # tenant_id None → never touches the DB, straight to env.
    creds = await credentials._get_tenant_credentials(None, "freshsales")
    assert creds == {
        "credential_key": "env-fs-key",
        "metadata": {"domain": "env.myfreshworks.com"},
    }


# ---------------------------------------------------------------------------
# Resolver: missing credentials -> None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("CIN7_API_KEY", "CIN7_ACCOUNT_ID"):
        monkeypatch.setenv(key, "")
    mcp_config.get_settings.cache_clear()

    async def no_row(tenant_id: str, credential_type: str):
        return None

    monkeypatch.setattr(credentials, "get_tenant_credentials", no_row)
    creds = await credentials._get_tenant_credentials(TENANT["id"], "cin7")
    assert creds is None


@pytest.mark.asyncio
async def test_resolver_db_query_error_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # tenant.get_tenant_credentials already swallows DB errors as None; emulate
    # that and confirm the resolver still serves env creds rather than raising.
    monkeypatch.setenv("CIN7_API_KEY", "env-key")
    monkeypatch.setenv("CIN7_ACCOUNT_ID", "env-acct")
    mcp_config.get_settings.cache_clear()

    async def returns_none(tenant_id: str, credential_type: str):
        return None

    monkeypatch.setattr(credentials, "get_tenant_credentials", returns_none)
    creds = await credentials._get_tenant_credentials(TENANT["id"], "cin7")
    assert creds["credential_key"] == "env-key"


# ---------------------------------------------------------------------------
# Integration: each tool uses DB credentials when a tenant is resolved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cin7_uses_db_credentials(
    monkeypatch: pytest.MonkeyPatch, _with_tenant
) -> None:
    monkeypatch.setenv("CIN7_BASE_URL", "https://api.cin7.test/ExternalApi/v2")
    mcp_config.get_settings.cache_clear()

    async def fake_db(tenant_id: str, credential_type: str):
        assert credential_type == "cin7"
        return {"credential_key": "tenant-cin7-key", "metadata": {"account_id": "tenant-acct"}}

    monkeypatch.setattr(credentials, "get_tenant_credentials", fake_db)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("https://api.cin7.test/ExternalApi/v2/product").mock(
            return_value=httpx.Response(200, json={"Products": []})
        )
        result = await cin7_tools.cin7_get_inventory(permissions=["finance_access"])

    assert result["status"] == "ok"
    request = route.calls[0].request
    assert request.headers["api-auth-applicationkey"] == "tenant-cin7-key"
    assert request.headers["api-auth-accountid"] == "tenant-acct"


@pytest.mark.asyncio
async def test_freshsales_uses_db_credentials(
    monkeypatch: pytest.MonkeyPatch, _with_tenant
) -> None:
    async def fake_db(tenant_id: str, credential_type: str):
        assert credential_type == "freshsales"
        return {
            "credential_key": "tenant-fs-key",
            "metadata": {"domain": "tenant.myfreshworks.com"},
        }

    monkeypatch.setattr(credentials, "get_tenant_credentials", fake_db)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(
            "https://tenant.myfreshworks.com/crm/sales/api/search"
        ).mock(return_value=httpx.Response(200, json=[]))
        result = await freshsales_tools.freshsales_search(
            query="acme", permissions=["freshsales_access"]
        )

    assert result["status"] == "ok"
    request = route.calls[0].request
    assert request.headers["authorization"] == "Token token=tenant-fs-key"
    assert "tenant.myfreshworks.com" in str(request.url)


@pytest.mark.asyncio
async def test_xero_uses_db_credentials(
    monkeypatch: pytest.MonkeyPatch, _with_tenant
) -> None:
    monkeypatch.setenv("XERO_IDENTITY_URL", "https://identity.test/connect/token")
    monkeypatch.setenv("XERO_BASE_URL", "https://api.test/api.xro/2.0")
    mcp_config.get_settings.cache_clear()

    async def fake_db(tenant_id: str, credential_type: str):
        assert credential_type == "xero"
        return {
            "credential_key": "tenant-refresh-token",
            "metadata": {
                "client_id": "tenant-cid",
                "client_secret": "tenant-csec",
                "tenant_id": "tenant-xero-id",
            },
        }

    monkeypatch.setattr(credentials, "get_tenant_credentials", fake_db)

    with respx.mock(assert_all_called=True) as mock:
        token_route = mock.post("https://identity.test/connect/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
        )
        mock.get("https://api.test/api.xro/2.0/Contacts").mock(
            return_value=httpx.Response(200, json={"Contacts": []})
        )
        result = await xero_tools.xero_get_contacts(permissions=["finance_access"])

    assert result["status"] == "ok"
    body = token_route.calls[0].request.content.decode()
    # The tenant's DB refresh token was used, not any env/file value.
    assert "grant_type=refresh_token" in body
    assert "refresh_token=tenant-refresh-token" in body
    assert "client_id=tenant-cid" in body


# ---------------------------------------------------------------------------
# Xero refresh-token rotation persisted back to the DB (#5C)
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, recorder: list) -> None:
        self._recorder = recorder

    async def execute(self, query: str, *args) -> None:
        self._recorder.append((query, args))


class _FakeConnCtx:
    def __init__(self, recorder: list) -> None:
        self._recorder = recorder

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._recorder)

    async def __aexit__(self, *exc) -> bool:
        return False


@pytest.mark.asyncio
async def test_persist_xero_refresh_token_executes_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder: list = []
    monkeypatch.setattr(credentials, "get_conn", lambda: _FakeConnCtx(recorder))

    await credentials.persist_xero_refresh_token(TENANT["id"], "rotated-token")

    assert len(recorder) == 1
    query, args = recorder[0]
    assert "UPDATE tenant_credentials" in query
    assert "credential_key = $1" in query
    assert args == ("rotated-token", TENANT["id"])


@pytest.mark.asyncio
async def test_persist_xero_refresh_token_swallows_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(credentials, "get_conn", boom)
    # Must not raise.
    await credentials.persist_xero_refresh_token(TENANT["id"], "rotated-token")


@pytest.mark.asyncio
async def test_xero_rotation_persisted_to_db_after_refresh(
    monkeypatch: pytest.MonkeyPatch, _with_tenant
) -> None:
    monkeypatch.setenv("XERO_IDENTITY_URL", "https://identity.test/connect/token")
    monkeypatch.setenv("XERO_BASE_URL", "https://api.test/api.xro/2.0")
    mcp_config.get_settings.cache_clear()

    async def fake_db(tenant_id: str, credential_type: str):
        return {
            "credential_key": "old-refresh",
            "metadata": {
                "client_id": "cid",
                "client_secret": "csec",
                "tenant_id": "xero-tid",
            },
        }

    monkeypatch.setattr(credentials, "get_tenant_credentials", fake_db)

    # Capture the write-back UPDATE without a real DB.
    recorder: list = []
    monkeypatch.setattr(credentials, "get_conn", lambda: _FakeConnCtx(recorder))

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://identity.test/connect/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "tok",
                    "expires_in": 1800,
                    "refresh_token": "rotated-new-token",
                },
            )
        )
        mock.get("https://api.test/api.xro/2.0/Contacts").mock(
            return_value=httpx.Response(200, json={"Contacts": []})
        )
        result = await xero_tools.xero_get_contacts(permissions=["finance_access"])

    assert result["status"] == "ok"
    # The rotated token was persisted back to the tenant's DB row.
    assert len(recorder) == 1
    query, args = recorder[0]
    assert "UPDATE tenant_credentials" in query
    assert args == ("rotated-new-token", TENANT["id"])


@pytest.mark.asyncio
async def test_xero_call_succeeds_even_if_rotation_persist_fails(
    monkeypatch: pytest.MonkeyPatch, _with_tenant
) -> None:
    monkeypatch.setenv("XERO_IDENTITY_URL", "https://identity.test/connect/token")
    monkeypatch.setenv("XERO_BASE_URL", "https://api.test/api.xro/2.0")
    mcp_config.get_settings.cache_clear()

    async def fake_db(tenant_id: str, credential_type: str):
        return {
            "credential_key": "old-refresh",
            "metadata": {"client_id": "cid", "client_secret": "csec", "tenant_id": "x"},
        }

    monkeypatch.setattr(credentials, "get_tenant_credentials", fake_db)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(credentials, "get_conn", boom)

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://identity.test/connect/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "tok",
                    "expires_in": 1800,
                    "refresh_token": "rotated-new-token",
                },
            )
        )
        mock.get("https://api.test/api.xro/2.0/Contacts").mock(
            return_value=httpx.Response(200, json={"Contacts": []})
        )
        result = await xero_tools.xero_get_contacts(permissions=["finance_access"])

    # Persistence failed, but the original Xero call still succeeded.
    assert result["status"] == "ok"
