"""Tests for the /admin tenant-policy endpoints.

Follows tests/test_admin.py: the handlers' DB access goes through small named
coroutines in ``agents.mcp.admin`` (``_fetch_tenant``, ``_fetch_policies``,
``_upsert_policy``), monkeypatched here so the tests run without a database.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from agents.mcp import admin
from agents.mcp import config as mcp_config
from agents.mcp.tenant_policy import DEFAULT_CURRENCY_SYMBOLS, FRESHDESK_REPLY_POLICY

ADMIN_KEY = "admin-secret-key-do-not-log"
TENANT_ID = "11111111-1111-1111-1111-111111111111"
HEADERS = {"X-API-Key": ADMIN_KEY}
POLICY_URL = f"/admin/tenants/{TENANT_ID}/policy"


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


@pytest.fixture
def tenant_found(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch_tenant(tenant_id: str):
        if tenant_id == TENANT_ID:
            return {"id": TENANT_ID, "name": "Example Tenant", "created_at": None}
        return None

    monkeypatch.setattr(admin, "_fetch_tenant", fake_fetch_tenant)


@pytest.fixture
def policy_store(monkeypatch: pytest.MonkeyPatch) -> dict:
    """In-memory stand-in for tenant_policies; returns the captured writes."""

    store: dict = {"existing": {}, "written": []}

    async def fake_fetch_policies(tenant_id: str):
        return store["existing"]

    async def fake_upsert(tenant_uuid: str, policy_type: str, policy: dict):
        store["written"].append((tenant_uuid, policy_type, policy))
        store["existing"][policy_type] = dict(policy)

    monkeypatch.setattr(admin, "_fetch_policies", fake_fetch_policies)
    monkeypatch.setattr(admin, "_upsert_policy", fake_upsert)
    return store


# ---------------------------------------------------------------------------
# Auth and tenant resolution — same contract as the credentials route
# ---------------------------------------------------------------------------


def test_missing_api_key_returns_401(client: TestClient) -> None:
    assert client.get(POLICY_URL).status_code == 401


def test_wrong_api_key_on_post_returns_401(client: TestClient) -> None:
    resp = client.post(
        POLICY_URL, headers={"X-API-Key": "wrong"}, json={"reply_block_prices": False}
    )
    assert resp.status_code == 401


def test_unknown_tenant_returns_404(
    client: TestClient, tenant_found, policy_store
) -> None:
    resp = client.post(
        "/admin/tenants/does-not-exist/policy",
        headers=HEADERS,
        json={"reply_block_prices": False},
    )
    assert resp.status_code == 404
    assert resp.json() == {"status": "error", "error": "Tenant not found"}


# ---------------------------------------------------------------------------
# GET — effective policy
# ---------------------------------------------------------------------------


def test_get_returns_defaults_for_a_tenant_with_no_policy_row(
    client: TestClient, tenant_found, policy_store
) -> None:
    resp = client.get(POLICY_URL, headers=HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    assert data["policy_type"] == FRESHDESK_REPLY_POLICY
    assert data["configured"] == []
    assert data["policy"] == {
        "reply_block_prices": True,
        "reply_block_bare_decimals": True,
        "reply_block_delivery_promises": True,
        "reply_currency_symbols": list(DEFAULT_CURRENCY_SYMBOLS),
        "reply_blocked_phrases": [],
        "escalation_group_id": None,
    }


def test_get_merges_stored_values_over_the_defaults(
    client: TestClient, tenant_found, policy_store
) -> None:
    policy_store["existing"][FRESHDESK_REPLY_POLICY] = {
        "reply_block_prices": False,
        "escalation_group_id": 77,
    }

    data = client.get(POLICY_URL, headers=HEADERS).json()

    assert data["policy"]["reply_block_prices"] is False
    assert data["policy"]["escalation_group_id"] == 77
    # Untouched fields still report their defaults.
    assert data["policy"]["reply_block_delivery_promises"] is True
    assert sorted(data["configured"]) == ["escalation_group_id", "reply_block_prices"]


# ---------------------------------------------------------------------------
# POST — patch-merge semantics
# ---------------------------------------------------------------------------


def test_the_two_price_flags_are_settable_independently(
    client: TestClient, tenant_found, policy_store
) -> None:
    """A tenant fixing false refusals should not have to drop price blocking."""

    resp = client.post(
        POLICY_URL, headers=HEADERS, json={"reply_block_bare_decimals": False}
    )

    assert resp.status_code == 200
    assert resp.json()["updated_fields"] == ["reply_block_bare_decimals"]
    data = client.get(POLICY_URL, headers=HEADERS).json()
    assert data["policy"]["reply_block_bare_decimals"] is False
    assert data["policy"]["reply_block_prices"] is True


def test_post_writes_only_the_supplied_fields(
    client: TestClient, tenant_found, policy_store
) -> None:
    resp = client.post(POLICY_URL, headers=HEADERS, json={"reply_block_prices": False})

    assert resp.status_code == 200
    assert resp.json()["updated_fields"] == ["reply_block_prices"]
    tenant_uuid, policy_type, document = policy_store["written"][0]
    assert tenant_uuid == TENANT_ID
    assert policy_type == FRESHDESK_REPLY_POLICY
    assert document == {"reply_block_prices": False}


def test_post_merges_onto_the_existing_document(
    client: TestClient, tenant_found, policy_store
) -> None:
    policy_store["existing"][FRESHDESK_REPLY_POLICY] = {
        "reply_block_prices": False,
        "reply_blocked_phrases": ["goodwill credit"],
    }

    client.post(POLICY_URL, headers=HEADERS, json={"escalation_group_id": 88})

    _, _, document = policy_store["written"][0]
    assert document == {
        "reply_block_prices": False,
        "reply_blocked_phrases": ["goodwill credit"],
        "escalation_group_id": 88,
    }


def test_post_accepts_a_replacement_currency_list(
    client: TestClient, tenant_found, policy_store
) -> None:
    resp = client.post(
        POLICY_URL, headers=HEADERS, json={"reply_currency_symbols": ["¥", "₹"]}
    )

    assert resp.status_code == 200
    _, _, document = policy_store["written"][0]
    assert document == {"reply_currency_symbols": ["¥", "₹"]}


def test_post_with_no_known_fields_writes_nothing(
    client: TestClient, tenant_found, policy_store
) -> None:
    resp = client.post(POLICY_URL, headers=HEADERS, json={"unrelated": "value"})

    assert resp.status_code == 200
    assert resp.json()["updated_fields"] == []
    assert policy_store["written"] == []


# ---------------------------------------------------------------------------
# POST — validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({"reply_block_prices": "false"}, "must be true or false"),
        ({"reply_currency_symbols": "R"}, "must be a list of strings"),
        ({"reply_blocked_phrases": [1, 2]}, "must be a list of strings"),
        ({"escalation_group_id": "not-a-number"}, "must be an integer or null"),
        ({"escalation_group_id": True}, "must be an integer or null"),
        ({"reply_block_prices": None}, "may not be null"),
    ],
)
def test_bad_values_are_rejected_with_400(
    client: TestClient, tenant_found, policy_store, payload: dict, fragment: str
) -> None:
    resp = client.post(POLICY_URL, headers=HEADERS, json=payload)

    assert resp.status_code == 400
    assert fragment in resp.json()["error"]
    # Nothing is written when validation fails.
    assert policy_store["written"] == []


def test_null_clears_the_nullable_escalation_group(
    client: TestClient, tenant_found, policy_store
) -> None:
    policy_store["existing"][FRESHDESK_REPLY_POLICY] = {"escalation_group_id": 77}

    resp = client.post(POLICY_URL, headers=HEADERS, json={"escalation_group_id": None})

    assert resp.status_code == 200
    _, _, document = policy_store["written"][0]
    assert document == {"escalation_group_id": None}


def test_invalid_json_body_returns_400(client: TestClient, tenant_found) -> None:
    resp = client.post(
        POLICY_URL,
        headers={**HEADERS, "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert resp.status_code == 400
