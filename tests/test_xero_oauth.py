"""Tests for the hosted Xero OAuth authorization-code flow.

The handlers' DB and HTTP access goes through small named coroutines in
``agents.mcp.xero_oauth`` (``_fetch_tenant``, ``_fetch_xero_metadata``,
``_store_state``, ``_consume_state``, ``_save_connection``, ``_exchange_code``,
``_fetch_connections``); these are monkeypatched so the tests run without a live
database or network, mirroring ``tests/test_admin_consent.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from agents.mcp import auth as mcp_auth
from agents.mcp import config as mcp_config
from agents.mcp import xero_oauth

PUBLIC_URL = "https://ja-e5a05fec59034d0fa32d8c3dfda06afe.ecs.eu-west-1.on.aws"
EXPECTED_REDIRECT = f"{PUBLIC_URL}/oauth/xero/callback"
# Fernet-shaped key: 32 urlsafe-base64 bytes. Only presence is asserted here.
ENCRYPTION_KEY = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJK="
TENANT_ID = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "xero-client-abc"
CLIENT_SECRET = "xero-client-secret-do-not-log"
ORG_GUID = "44e9b855-38fb-476b-a337-c2c9a0efc51f"
ORG_GUID_2 = "99999999-1111-2222-3333-444444444444"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIES_PUBLIC_URL", PUBLIC_URL)
    monkeypatch.setenv("JARVIES_ENCRYPTION_KEY", ENCRYPTION_KEY)
    mcp_config.get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(Starlette(routes=xero_oauth.get_xero_oauth_routes()))


@pytest.fixture
def tenant(monkeypatch: pytest.MonkeyPatch) -> dict:
    """An active tenant with Xero client credentials already configured."""

    record = {"id": TENANT_ID, "name": "Asher Group", "status": "active"}

    async def fake_fetch_tenant(tenant_id: str):
        return record if tenant_id == TENANT_ID else None

    monkeypatch.setattr(xero_oauth, "_fetch_tenant", fake_fetch_tenant)
    return record


@pytest.fixture
def metadata(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Mutable stand-in for tenant_credentials.metadata."""

    store = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}

    async def fake_fetch_metadata(tenant_id: str):
        return dict(store)

    monkeypatch.setattr(xero_oauth, "_fetch_xero_metadata", fake_fetch_metadata)
    return store


@pytest.fixture
def states(monkeypatch: pytest.MonkeyPatch) -> dict:
    """In-memory oauth_states store with single-use semantics."""

    store: dict = {}

    async def fake_store(state, tenant_id, expires_at):
        store[state] = {
            "state": state,
            "tenant_hint": tenant_id,
            "expires_at": expires_at,
        }

    async def fake_consume(state):
        row = store.pop(state, None)  # single-use: deleted on read
        if row is None:
            return None
        if row["expires_at"] is not None and row["expires_at"] < datetime.now(UTC):
            return None
        return row

    monkeypatch.setattr(xero_oauth, "_store_state", fake_store)
    monkeypatch.setattr(xero_oauth, "_consume_state", fake_consume)
    return store


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Capture what _save_connection was asked to write."""

    captured: dict = {}

    async def fake_save(tenant_id, refresh_token, metadata, orgs):
        captured.update(
            tenant_id=tenant_id,
            refresh_token=refresh_token,
            metadata=metadata,
            orgs=orgs,
        )

    monkeypatch.setattr(xero_oauth, "_save_connection", fake_save)
    return captured


def _stub_xero(
    monkeypatch: pytest.MonkeyPatch,
    *,
    token_status: int = 200,
    token_payload: Any = None,
    connections_status: int = 200,
    connections_payload: Any = None,
) -> dict:
    """Stub both Xero HTTP calls. Returns a dict recording the exchange args."""

    seen: dict = {}
    if token_payload is None:
        token_payload = {
            "access_token": "xero-access-token",
            "refresh_token": "xero-refresh-token",
            "expires_in": 1800,
        }
    if connections_payload is None:
        connections_payload = [{"tenantId": ORG_GUID, "tenantName": "Asher Group Pty"}]

    async def fake_exchange(client_id, client_secret, code, redirect_uri):
        seen.update(
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
        return token_status, token_payload

    async def fake_connections(access_token):
        seen["access_token"] = access_token
        return connections_status, connections_payload

    monkeypatch.setattr(xero_oauth, "_exchange_code", fake_exchange)
    monkeypatch.setattr(xero_oauth, "_fetch_connections", fake_connections)
    return seen


def _seed_state(states: dict, name: str, *, tenant_id: str = TENANT_ID, minutes: int = 5):
    states[name] = {
        "state": name,
        "tenant_hint": tenant_id,
        "expires_at": datetime.now(UTC) + timedelta(minutes=minutes),
    }


# ---------------------------------------------------------------------------
# /oauth/xero/start
# ---------------------------------------------------------------------------


def test_start_happy_path_redirect_params(
    client: TestClient, env: None, tenant: dict, metadata: dict, states: dict
) -> None:
    resp = client.get(
        f"/oauth/xero/start?tenant_id={TENANT_ID}", follow_redirects=False
    )
    assert resp.status_code == 302

    parsed = urlparse(resp.headers["location"])
    assert parsed.scheme == "https"
    assert parsed.netloc == "login.xero.com"
    assert parsed.path == "/identity/connect/authorize"

    qs = parse_qs(parsed.query)
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == [CLIENT_ID]
    assert qs["redirect_uri"] == [EXPECTED_REDIRECT]
    assert qs["scope"] == [xero_oauth.SCOPES]
    # Granular scopes plus offline_access (needed for a refresh token).
    assert "offline_access" in qs["scope"][0]
    assert "accounting.contacts.read" in qs["scope"][0]

    # State persisted against the tenant, with a TTL in the future.
    state = qs["state"][0]
    assert states[state]["tenant_hint"] == TENANT_ID
    assert states[state]["expires_at"] > datetime.now(UTC)


def test_start_missing_tenant_id_errors(
    client: TestClient, env: None, tenant: dict, metadata: dict, states: dict
) -> None:
    resp = client.get("/oauth/xero/start", follow_redirects=False)
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "tenant_id" in resp.text
    assert not states  # nothing minted


def test_start_unknown_tenant_errors(
    client: TestClient, env: None, tenant: dict, metadata: dict, states: dict
) -> None:
    resp = client.get(
        "/oauth/xero/start?tenant_id=00000000-0000-0000-0000-000000000000",
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "does not exist" in resp.text
    assert not states


def test_start_suspended_tenant_errors(
    client: TestClient,
    env: None,
    tenant: dict,
    metadata: dict,
    states: dict,
) -> None:
    tenant["status"] = "suspended"
    resp = client.get(
        f"/oauth/xero/start?tenant_id={TENANT_ID}", follow_redirects=False
    )
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "not active" in resp.text
    assert not states


def test_start_missing_client_id_errors(
    client: TestClient,
    env: None,
    tenant: dict,
    metadata: dict,
    states: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_client_id(tenant_id: str):
        return {"client_secret": CLIENT_SECRET}

    monkeypatch.setattr(xero_oauth, "_fetch_xero_metadata", no_client_id)

    resp = client.get(
        f"/oauth/xero/start?tenant_id={TENANT_ID}", follow_redirects=False
    )
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "client id" in resp.text
    assert not states


def test_start_without_encryption_key_fails_before_redirect(
    client: TestClient,
    tenant: dict,
    metadata: dict,
    states: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The whole point of the guard: refuse up front rather than discovering it in
    # the callback, after Xero has already consumed the authorization code.
    monkeypatch.setenv("JARVIES_PUBLIC_URL", PUBLIC_URL)
    monkeypatch.setenv("JARVIES_ENCRYPTION_KEY", "")
    mcp_config.get_settings.cache_clear()

    resp = client.get(
        f"/oauth/xero/start?tenant_id={TENANT_ID}", follow_redirects=False
    )
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "JARVIES_ENCRYPTION_KEY" in resp.text
    assert not states  # no state minted, no redirect issued


def test_start_without_public_base_url_errors_not_relative_redirect(
    client: TestClient,
    tenant: dict,
    metadata: dict,
    states: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIES_PUBLIC_URL", "")
    monkeypatch.setenv("JARVIES_ENCRYPTION_KEY", ENCRYPTION_KEY)
    mcp_config.get_settings.cache_clear()

    resp = client.get(
        f"/oauth/xero/start?tenant_id={TENANT_ID}", follow_redirects=False
    )
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "JARVIES_PUBLIC_URL" in resp.text
    assert not states


# ---------------------------------------------------------------------------
# /oauth/xero/callback
# ---------------------------------------------------------------------------


def test_callback_happy_path_merges_metadata_and_saves(
    client: TestClient,
    env: None,
    metadata: dict,
    states: dict,
    saved: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_xero(monkeypatch)
    _seed_state(states, "s-good")

    resp = client.get("/oauth/xero/callback?code=auth-code-1&state=s-good")
    assert resp.status_code == 200
    assert "Xero Connected" in resp.text
    assert ORG_GUID in resp.text
    assert "Asher Group Pty" in resp.text

    assert saved["tenant_id"] == TENANT_ID
    assert saved["refresh_token"] == "xero-refresh-token"

    # The two invariants: existing client credentials survive the write, and the
    # selected org GUID lands in metadata.tenant_id (the Xero-tenant-id header).
    assert saved["metadata"]["client_id"] == CLIENT_ID
    assert saved["metadata"]["client_secret"] == CLIENT_SECRET
    assert saved["metadata"]["tenant_id"] == ORG_GUID

    assert saved["orgs"] == [{"xero_tenant_id": ORG_GUID, "org_name": "Asher Group Pty"}]
    assert "s-good" not in states  # single-use


def test_callback_preserves_unrelated_metadata_keys(
    client: TestClient,
    env: None,
    states: dict,
    saved: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Anything already in the JSONB must survive — the upsert replaces it wholesale.
    async def rich_metadata(tenant_id: str):
        return {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "tenant_id": "stale-org-guid",
            "some_other_key": "keep-me",
        }

    monkeypatch.setattr(xero_oauth, "_fetch_xero_metadata", rich_metadata)
    _stub_xero(monkeypatch)
    _seed_state(states, "s-rich")

    resp = client.get("/oauth/xero/callback?code=auth-code-1&state=s-rich")
    assert resp.status_code == 200
    assert saved["metadata"]["some_other_key"] == "keep-me"
    assert saved["metadata"]["client_id"] == CLIENT_ID
    # The stale org GUID is replaced by the newly selected one.
    assert saved["metadata"]["tenant_id"] == ORG_GUID


def test_callback_exchange_uses_stored_client_creds_and_same_redirect_uri(
    client: TestClient,
    env: None,
    metadata: dict,
    states: dict,
    saved: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_xero(monkeypatch)
    _seed_state(states, "s-args")

    client.get("/oauth/xero/callback?code=auth-code-xyz&state=s-args")

    assert seen["client_id"] == CLIENT_ID
    assert seen["client_secret"] == CLIENT_SECRET
    assert seen["code"] == "auth-code-xyz"
    # Must byte-match what /start sent, or Xero rejects the exchange.
    assert seen["redirect_uri"] == EXPECTED_REDIRECT
    assert seen["access_token"] == "xero-access-token"


def test_callback_multiple_orgs_first_is_live(
    client: TestClient,
    env: None,
    metadata: dict,
    states: dict,
    saved: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_xero(
        monkeypatch,
        connections_payload=[
            {"tenantId": ORG_GUID, "tenantName": "First Org"},
            {"tenantId": ORG_GUID_2, "tenantName": "Second Org"},
        ],
    )
    _seed_state(states, "s-multi")

    resp = client.get("/oauth/xero/callback?code=auth-code-1&state=s-multi")
    assert resp.status_code == 200

    # Phase-1 shortcut: first returned org wins and is reported as Live.
    assert saved["metadata"]["tenant_id"] == ORG_GUID
    assert [o["xero_tenant_id"] for o in saved["orgs"]] == [ORG_GUID, ORG_GUID_2]
    assert "Live" in resp.text
    assert "2 organisations were authorised" in resp.text
    assert "Second Org" in resp.text


def test_callback_xero_error_param_short_circuits(
    client: TestClient, env: None, states: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_save(*args, **kwargs):
        raise AssertionError("nothing may be saved on a Xero error")

    monkeypatch.setattr(xero_oauth, "_save_connection", fail_save)
    _seed_state(states, "s-err")

    resp = client.get(
        "/oauth/xero/callback?error=access_denied"
        "&error_description=User+declined+the+consent&state=s-err"
    )
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "User declined the consent" in resp.text
    # Handled before the state is touched, so the row is still there to expire.
    assert "s-err" in states


def test_callback_unknown_state_errors(
    client: TestClient, env: None, states: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_save(*args, **kwargs):
        raise AssertionError("nothing may be saved for an unknown state")

    monkeypatch.setattr(xero_oauth, "_save_connection", fail_save)

    resp = client.get("/oauth/xero/callback?code=abc&state=never-minted")
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "invalid or has expired" in resp.text


def test_callback_missing_state_errors(client: TestClient, env: None, states: dict) -> None:
    resp = client.get("/oauth/xero/callback?code=abc")
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "no state" in resp.text.lower()


def test_callback_expired_state_errors_nothing_saved(
    client: TestClient, env: None, states: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_save(*args, **kwargs):
        raise AssertionError("nothing may be saved for an expired state")

    monkeypatch.setattr(xero_oauth, "_save_connection", fail_save)
    _seed_state(states, "s-old", minutes=-1)

    resp = client.get("/oauth/xero/callback?code=abc&state=s-old")
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "invalid or has expired" in resp.text
    assert "s-old" not in states  # deleted even though expired


def test_callback_state_is_single_use(
    client: TestClient,
    env: None,
    metadata: dict,
    states: dict,
    saved: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_xero(monkeypatch)
    _seed_state(states, "s-once")

    first = client.get("/oauth/xero/callback?code=abc&state=s-once")
    assert "Xero Connected" in first.text

    second = client.get("/oauth/xero/callback?code=abc&state=s-once")
    assert "Connection Failed" in second.text
    assert "invalid or has expired" in second.text


def test_callback_tenant_comes_from_state_not_query(
    client: TestClient,
    env: None,
    metadata: dict,
    states: dict,
    saved: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_xero(monkeypatch)
    _seed_state(states, "s-auth", tenant_id=TENANT_ID)

    # An attacker-supplied tenant_id in the query must be ignored entirely.
    client.get(
        "/oauth/xero/callback?code=abc&state=s-auth"
        "&tenant_id=99999999-9999-9999-9999-999999999999"
    )
    assert saved["tenant_id"] == TENANT_ID


def test_callback_missing_code_errors(
    client: TestClient, env: None, metadata: dict, states: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_save(*args, **kwargs):
        raise AssertionError("nothing may be saved without a code")

    monkeypatch.setattr(xero_oauth, "_save_connection", fail_save)
    _seed_state(states, "s-nocode")

    resp = client.get("/oauth/xero/callback?state=s-nocode")
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "authorization code" in resp.text


def test_callback_token_exchange_non_200_errors(
    client: TestClient,
    env: None,
    metadata: dict,
    states: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_save(*args, **kwargs):
        raise AssertionError("nothing may be saved when the exchange fails")

    monkeypatch.setattr(xero_oauth, "_save_connection", fail_save)
    _stub_xero(
        monkeypatch,
        token_status=400,
        token_payload={"error": "invalid_grant"},
    )
    _seed_state(states, "s-badtoken")

    resp = client.get("/oauth/xero/callback?code=abc&state=s-badtoken")
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "HTTP 400" in resp.text


def test_callback_token_response_without_refresh_token_errors(
    client: TestClient,
    env: None,
    metadata: dict,
    states: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_save(*args, **kwargs):
        raise AssertionError("nothing may be saved without a refresh token")

    monkeypatch.setattr(xero_oauth, "_save_connection", fail_save)
    _stub_xero(monkeypatch, token_payload={"access_token": "only-access"})
    _seed_state(states, "s-norefresh")

    resp = client.get("/oauth/xero/callback?code=abc&state=s-norefresh")
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "expected tokens" in resp.text


def test_callback_connections_non_200_errors(
    client: TestClient,
    env: None,
    metadata: dict,
    states: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_save(*args, **kwargs):
        raise AssertionError("nothing may be saved when /connections fails")

    monkeypatch.setattr(xero_oauth, "_save_connection", fail_save)
    _stub_xero(monkeypatch, connections_status=403, connections_payload={"error": "nope"})
    _seed_state(states, "s-badconn")

    resp = client.get("/oauth/xero/callback?code=abc&state=s-badconn")
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "HTTP 403" in resp.text


def test_callback_empty_connections_errors(
    client: TestClient,
    env: None,
    metadata: dict,
    states: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_save(*args, **kwargs):
        raise AssertionError("nothing may be saved with no organisations")

    monkeypatch.setattr(xero_oauth, "_save_connection", fail_save)
    _stub_xero(monkeypatch, connections_payload=[])
    _seed_state(states, "s-noorgs")

    resp = client.get("/oauth/xero/callback?code=abc&state=s-noorgs")
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "No Xero organisations" in resp.text


def test_callback_encryption_not_configured_is_legible(
    client: TestClient,
    env: None,
    metadata: dict,
    states: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.mcp.crypto import CryptoNotConfiguredError

    async def raise_crypto(*args, **kwargs):
        raise CryptoNotConfiguredError("JARVIES_ENCRYPTION_KEY is not configured")

    monkeypatch.setattr(xero_oauth, "_save_connection", raise_crypto)
    _stub_xero(monkeypatch)
    _seed_state(states, "s-nokey")

    resp = client.get("/oauth/xero/callback?code=abc&state=s-nokey")
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "JARVIES_ENCRYPTION_KEY" in resp.text
    assert "Nothing was stored" in resp.text


def test_callback_save_failure_reports_nothing_stored(
    client: TestClient,
    env: None,
    metadata: dict,
    states: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(xero_oauth, "_save_connection", boom)
    _stub_xero(monkeypatch)
    _seed_state(states, "s-dbfail")

    resp = client.get("/oauth/xero/callback?code=abc&state=s-dbfail")
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "Nothing was stored" in resp.text


def test_callback_missing_client_secret_errors(
    client: TestClient,
    env: None,
    states: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_secret(tenant_id: str):
        return {"client_id": CLIENT_ID}

    async def fail_save(*args, **kwargs):
        raise AssertionError("nothing may be saved without a client secret")

    monkeypatch.setattr(xero_oauth, "_fetch_xero_metadata", no_secret)
    monkeypatch.setattr(xero_oauth, "_save_connection", fail_save)
    _seed_state(states, "s-nosecret")

    resp = client.get("/oauth/xero/callback?code=abc&state=s-nosecret")
    assert resp.status_code == 200
    assert "Connection Failed" in resp.text
    assert "no longer configured" in resp.text


def test_callback_never_leaks_secrets_into_the_page(
    client: TestClient,
    env: None,
    metadata: dict,
    states: dict,
    saved: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_xero(monkeypatch)
    _seed_state(states, "s-leak")

    resp = client.get("/oauth/xero/callback?code=abc&state=s-leak")
    assert resp.status_code == 200
    assert CLIENT_SECRET not in resp.text
    assert "xero-refresh-token" not in resp.text
    assert "xero-access-token" not in resp.text


# ---------------------------------------------------------------------------
# Middleware bypass list + state scoping
# ---------------------------------------------------------------------------


def test_xero_oauth_paths_bypass_mcp_auth() -> None:
    assert "/oauth/xero/start" in mcp_auth.PUBLIC_PATHS
    assert "/oauth/xero/callback" in mcp_auth.PUBLIC_PATHS


def test_bypass_is_an_exact_set_not_a_prefix() -> None:
    # A prefix bypass would silently expose future /oauth/xero/* routes.
    paths = xero_oauth.XERO_OAUTH_PUBLIC_PATHS
    assert paths == {"/oauth/xero/start", "/oauth/xero/callback"}
    assert "/oauth/xero/anything-else" not in mcp_auth.PUBLIC_PATHS


def test_state_purposes_are_distinct_per_flow() -> None:
    from agents.mcp import admin_consent

    assert xero_oauth.STATE_PURPOSE == "xero_oauth"
    assert admin_consent.STATE_PURPOSE == "ms_consent"
    assert xero_oauth.STATE_PURPOSE != admin_consent.STATE_PURPOSE


def test_schema_statements_registered_in_both_places() -> None:
    from agents.mcp.portal_schema import PORTAL_DDL_STATEMENTS

    joined = " ".join(PORTAL_DDL_STATEMENTS)
    assert "xero_client_orgs" in joined
    assert "purpose" in joined

    migrate_path = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "scripts" / "migrate.py"
    )
    migrate_src = migrate_path.read_text(encoding="utf-8")
    assert "xero_client_orgs" in migrate_src
    assert "ADD COLUMN IF NOT EXISTS purpose" in migrate_src
