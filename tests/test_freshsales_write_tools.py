from __future__ import annotations

import json

import httpx
import pytest
import respx

from agents.mcp import config as mcp_config
from agents.mcp.tools import freshsales_write_tools as fw

BASE = "https://acme.myfreshworks.test/crm/sales/api"
PERMS = ["fundraising_access"]


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


def _set_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env = {
        "FRESHSALES_DOMAIN": "acme.myfreshworks.test",
        "FRESHSALES_API_KEY": "fk-secret-do-not-log",
        "MCP_TOOL_RESULT_LIMIT": "50",
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _body(route) -> dict:
    return json.loads(route.calls[0].request.content)


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_configured_when_creds_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("FRESHSALES_DOMAIN", "FRESHSALES_API_KEY"):
        monkeypatch.setenv(key, "")

    result = await fw.freshsales_create_contact(
        first_name="Ada", last_name="Lovelace", email="ada@x.io", permissions=PERMS
    )

    assert result["source"] == "freshsales"
    assert result["status"] == "not_configured"


# ---------------------------------------------------------------------------
# 1. create_contact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_contact_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{BASE}/contacts").mock(
            return_value=httpx.Response(
                201,
                json={
                    "contact": {
                        "id": 101,
                        "display_name": "Ada Lovelace",
                        "email": "ada@x.io",
                    }
                },
            )
        )
        result = await fw.freshsales_create_contact(
            first_name="Ada",
            last_name="Lovelace",
            email="ada@x.io",
            phone="555-1",
            job_title="Engineer",
            custom_fields={"tier": "gold"},
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["source"] == "freshsales"
    assert result["data"] == {"id": 101, "name": "Ada Lovelace", "email": "ada@x.io"}
    body = _body(route)["contact"]
    assert body["mobile_number"] == "555-1"
    assert body["custom_field"] == {"tier": "gold"}
    # Auth header preserved on writes.
    assert (
        route.calls[0].request.headers["authorization"]
        == "Token token=fk-secret-do-not-log"
    )


@pytest.mark.asyncio
async def test_create_contact_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{BASE}/contacts").mock(
            return_value=httpx.Response(422, json={"error": "invalid"})
        )
        result = await fw.freshsales_create_contact(
            first_name="Ada", last_name="L", email="bad", permissions=PERMS
        )

    assert result["status"] == "error"
    assert "422" in (result["error"] or "")


# ---------------------------------------------------------------------------
# 2. update_contact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_contact_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.put(f"{BASE}/contacts/101").mock(
            return_value=httpx.Response(
                200, json={"contact": {"id": 101, "job_title": "CTO"}}
            )
        )
        result = await fw.freshsales_update_contact(
            contact_id="101", job_title="CTO", permissions=PERMS
        )

    assert result["status"] == "ok"
    assert result["data"]["contact"]["job_title"] == "CTO"
    # Only supplied fields are sent; None-valued optionals are dropped.
    assert _body(route)["contact"] == {"job_title": "CTO"}


@pytest.mark.asyncio
async def test_update_contact_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.put(f"{BASE}/contacts/999").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        result = await fw.freshsales_update_contact(
            contact_id="999", email="x@y.io", permissions=PERMS
        )

    assert result["status"] == "error"
    assert "404" in (result["error"] or "")


# ---------------------------------------------------------------------------
# 3. create_deal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_deal_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{BASE}/deals").mock(
            return_value=httpx.Response(
                201, json={"deal": {"id": 7, "name": "Big Deal", "amount": 5000}}
            )
        )
        result = await fw.freshsales_create_deal(
            name="Big Deal",
            amount=5000,
            contact_id="101",
            account_id="55",
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["data"] == {"id": 7, "name": "Big Deal", "amount": 5000}
    body = _body(route)["deal"]
    assert body["contacts_added_list"] == ["101"]
    assert body["sales_account_id"] == "55"


@pytest.mark.asyncio
async def test_create_deal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{BASE}/deals").mock(
            return_value=httpx.Response(500, json={"error": "boom"})
        )
        result = await fw.freshsales_create_deal(
            name="X", amount=1, permissions=PERMS
        )

    assert result["status"] == "error"
    assert "500" in (result["error"] or "")


# ---------------------------------------------------------------------------
# 4. update_deal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_deal_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.put(f"{BASE}/deals/7").mock(
            return_value=httpx.Response(
                200, json={"deal": {"id": 7, "deal_stage_id": "3"}}
            )
        )
        result = await fw.freshsales_update_deal(
            deal_id="7", deal_stage_id="3", permissions=PERMS
        )

    assert result["status"] == "ok"
    assert result["data"]["deal"]["deal_stage_id"] == "3"
    assert _body(route)["deal"] == {"deal_stage_id": "3"}


@pytest.mark.asyncio
async def test_update_deal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.put(f"{BASE}/deals/7").mock(
            return_value=httpx.Response(400, json={"error": "bad"})
        )
        result = await fw.freshsales_update_deal(
            deal_id="7", amount=9, permissions=PERMS
        )

    assert result["status"] == "error"
    assert "400" in (result["error"] or "")


# ---------------------------------------------------------------------------
# 5. create_account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_account_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{BASE}/accounts").mock(
            return_value=httpx.Response(
                201, json={"sales_account": {"id": 55, "name": "Acme Inc"}}
            )
        )
        result = await fw.freshsales_create_account(
            name="Acme Inc",
            website="acme.io",
            industry="Tech",
            number_of_employees=200,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["data"] == {"id": 55, "name": "Acme Inc"}
    # Account body is wrapped as sales_account per Freshworks schema.
    assert _body(route)["sales_account"]["name"] == "Acme Inc"


@pytest.mark.asyncio
async def test_create_account_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{BASE}/accounts").mock(
            return_value=httpx.Response(422, json={"error": "dup"})
        )
        result = await fw.freshsales_create_account(name="Acme", permissions=PERMS)

    assert result["status"] == "error"
    assert "422" in (result["error"] or "")


# ---------------------------------------------------------------------------
# 6. create_note
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_note_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{BASE}/notes").mock(
            return_value=httpx.Response(201, json={"note": {"id": 33}})
        )
        result = await fw.freshsales_create_note(
            description="Called the donor",
            targetable_type="Contact",
            targetable_id="101",
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["data"] == {"id": 33}
    body = _body(route)["note"]
    assert body["targetable_type"] == "Contact"
    assert body["targetable_id"] == "101"


@pytest.mark.asyncio
async def test_create_note_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{BASE}/notes").mock(
            return_value=httpx.Response(403, json={"error": "forbidden"})
        )
        result = await fw.freshsales_create_note(
            description="x", targetable_type="Deal", targetable_id="7", permissions=PERMS
        )

    assert result["status"] == "error"
    assert "403" in (result["error"] or "")


# ---------------------------------------------------------------------------
# 7. create_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{BASE}/tasks").mock(
            return_value=httpx.Response(
                201, json={"task": {"id": 44, "title": "Follow up"}}
            )
        )
        result = await fw.freshsales_create_task(
            title="Follow up",
            due_date="2026-07-01",
            owner_id="9",
            targetable_type="Contact",
            targetable_id="101",
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["data"] == {"id": 44, "title": "Follow up"}
    body = _body(route)["task"]
    assert body["due_date"] == "2026-07-01"
    assert body["owner_id"] == "9"


@pytest.mark.asyncio
async def test_create_task_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{BASE}/tasks").mock(
            return_value=httpx.Response(422, json={"error": "missing due_date"})
        )
        result = await fw.freshsales_create_task(
            title="x", due_date="2026-07-01", owner_id="9", permissions=PERMS
        )

    assert result["status"] == "error"
    assert "422" in (result["error"] or "")


# ---------------------------------------------------------------------------
# 8. get_deal_stages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_deal_stages_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/deal_stages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "deal_stages": [
                        {"id": 1, "name": "New", "position": 1, "extra": "ignored"},
                        {"id": 2, "name": "Won", "position": 5},
                    ]
                },
            )
        )
        result = await fw.freshsales_get_deal_stages(permissions=PERMS)

    assert result["status"] == "ok"
    assert result["data"]["count"] == 2
    assert result["data"]["deal_stages"][0] == {
        "id": 1,
        "name": "New",
        "position": 1,
    }


@pytest.mark.asyncio
async def test_get_deal_stages_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/deal_stages").mock(
            return_value=httpx.Response(500, json={"error": "boom"})
        )
        result = await fw.freshsales_get_deal_stages(permissions=PERMS)

    assert result["status"] == "error"
    assert "500" in (result["error"] or "")


# ---------------------------------------------------------------------------
# 9. get_contact_journey
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_contact_journey_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{BASE}/contacts/101").mock(
            return_value=httpx.Response(
                200,
                json={
                    "contact": {"id": 101, "display_name": "Ada"},
                    "deals": [{"id": 7}],
                    "notes": [{"id": 33}],
                    "tasks": [{"id": 44}],
                    "appointments": [{"id": 88}],
                },
            )
        )
        result = await fw.freshsales_get_contact_journey(
            contact_id="101", permissions=PERMS
        )

    assert result["status"] == "ok"
    data = result["data"]
    assert data["contact"]["id"] == 101
    assert data["deals"] == [{"id": 7}]
    assert data["appointments"] == [{"id": 88}]
    assert (
        route.calls[0].request.url.params["include"]
        == "deals,notes,tasks,appointments"
    )


@pytest.mark.asyncio
async def test_get_contact_journey_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/contacts/999").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        result = await fw.freshsales_get_contact_journey(
            contact_id="999", permissions=PERMS
        )

    assert result["status"] == "error"
    assert "404" in (result["error"] or "")


# ---------------------------------------------------------------------------
# 10. search_contacts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_contacts_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{BASE}/contacts/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "contacts": [
                        {"id": 101, "display_name": "Ada", "deals": [{"id": 7}]},
                        {"id": 102, "display_name": "Grace"},
                    ]
                },
            )
        )
        result = await fw.freshsales_search_contacts(
            query="ada", limit=5, permissions=PERMS
        )

    assert result["status"] == "ok"
    assert result["data"]["count"] == 2
    assert result["data"]["query"] == "ada"
    params = route.calls[0].request.url.params
    assert params["q"] == "ada"
    assert params["include"] == "deals"
    assert params["per_page"] == "5"


@pytest.mark.asyncio
async def test_search_contacts_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/contacts/search").mock(
            return_value=httpx.Response(401, json={"error": "unauthorized"})
        )
        result = await fw.freshsales_search_contacts(query="ada", permissions=PERMS)

    assert result["status"] == "error"
    assert "401" in (result["error"] or "")


# ---------------------------------------------------------------------------
# Permission gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_requires_fundraising_access(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    from agents.mcp.permissions import MCPPermissionError

    with pytest.raises(MCPPermissionError):
        await fw.freshsales_create_contact(
            first_name="Ada",
            last_name="L",
            email="ada@x.io",
            permissions=["freshsales_access"],  # read scope only — insufficient
        )
