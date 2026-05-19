from __future__ import annotations

import logging

import httpx
import pytest
import respx

from agents.mcp import config as mcp_config
from agents.mcp.tools import freshsales_tools


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


def _set_freshsales_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env = {
        "FRESHSALES_DOMAIN": "acme.myfreshworks.test",
        "FRESHSALES_API_KEY": "fk-secret-do-not-log",
        "MCP_TOOL_RESULT_LIMIT": "50",
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.mark.asyncio
async def test_freshsales_get_contacts_returns_not_configured_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Override values from any locally-present .env to simulate missing creds.
    for key in ("FRESHSALES_DOMAIN", "FRESHSALES_API_KEY"):
        monkeypatch.setenv(key, "")

    result = await freshsales_tools.freshsales_get_contacts(
        permissions=["freshsales_access"]
    )

    assert result["source"] == "freshsales"
    assert result["status"] == "not_configured"


@pytest.mark.asyncio
async def test_freshsales_get_contacts_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_freshsales_env(monkeypatch)
    payload = {
        "contacts": [
            {"id": 1, "first_name": "Ada"},
            {"id": 2, "first_name": "Grace"},
        ]
    }

    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "https://acme.myfreshworks.test/crm/sales/api/contacts/filters"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "filters": [
                        {"id": 11, "name": "My Contacts"},
                        {"id": 22, "name": "All Contacts"},
                    ]
                },
            )
        )
        route = mock.get(
            "https://acme.myfreshworks.test/crm/sales/api/contacts/view/22"
        ).mock(return_value=httpx.Response(200, json=payload))

        result = await freshsales_tools.freshsales_get_contacts(
            page=2,
            page_size=25,
            permissions=["freshsales_access"],
        )

    assert result["status"] == "ok"
    assert result["source"] == "freshsales"
    assert result["data"]["count"] == 2
    assert result["data"]["view_id"] == "22"
    request = route.calls[0].request
    assert request.headers["authorization"] == "Token token=fk-secret-do-not-log"
    assert request.url.params["page"] == "2"
    assert request.url.params["per_page"] == "25"


@pytest.mark.asyncio
async def test_freshsales_get_contacts_uses_view_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_freshsales_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(
            "https://acme.myfreshworks.test/crm/sales/api/contacts/view/9876"
        ).mock(return_value=httpx.Response(200, json={"contacts": [{"id": 7}]}))

        result = await freshsales_tools.freshsales_get_contacts(
            view_id="9876", permissions=["freshsales_access"]
        )

    assert result["status"] == "ok"
    assert result["data"]["count"] == 1
    assert route.called


@pytest.mark.asyncio
async def test_freshsales_get_accounts_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_freshsales_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "https://acme.myfreshworks.test/crm/sales/api/sales_accounts/filters"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "filters": [
                        {"id": 31, "name": "My Accounts"},
                        {"id": 42, "name": "All Accounts"},
                    ]
                },
            )
        )
        mock.get(
            "https://acme.myfreshworks.test/crm/sales/api/sales_accounts/view/42"
        ).mock(
            return_value=httpx.Response(
                200, json={"sales_accounts": [{"id": 1, "name": "Acme Inc"}]}
            )
        )
        result = await freshsales_tools.freshsales_get_accounts(
            permissions=["freshsales_access"]
        )

    assert result["status"] == "ok"
    assert result["data"]["accounts"][0]["name"] == "Acme Inc"
    assert result["data"]["view_id"] == "42"


@pytest.mark.asyncio
async def test_freshsales_get_deals_passes_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_freshsales_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "https://acme.myfreshworks.test/crm/sales/api/deals/filters"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "filters": [
                        {"id": 51, "name": "All Deals"},
                        {"id": 52, "name": "Open Deals"},
                    ]
                },
            )
        )
        route = mock.get(
            "https://acme.myfreshworks.test/crm/sales/api/deals/view/51"
        ).mock(
            return_value=httpx.Response(
                200, json={"deals": [{"id": 1, "deal_stage_id": "won"}]}
            )
        )
        result = await freshsales_tools.freshsales_get_deals(
            stage="won",
            date_from="2026-01-01",
            date_to="2026-04-30",
            permissions=["freshsales_access"],
        )

    assert result["status"] == "ok"
    assert result["data"]["view_id"] == "51"
    params = route.calls[0].request.url.params
    assert params["deal_stage"] == "won"
    assert params["updated_since"] == "2026-01-01"
    assert params["updated_until"] == "2026-04-30"


@pytest.mark.asyncio
async def test_freshsales_search_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_freshsales_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://acme.myfreshworks.test/crm/sales/api/search").mock(
            return_value=httpx.Response(
                200,
                json=[{"type": "contact", "id": 5}, {"type": "deal", "id": 9}],
            )
        )
        result = await freshsales_tools.freshsales_search(
            query="acme", permissions=["freshsales_access"]
        )

    assert result["status"] == "ok"
    assert result["data"]["count"] == 2
    assert result["data"]["query"] == "acme"


@pytest.mark.asyncio
async def test_freshsales_returns_error_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_freshsales_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        # First call is /filters for view auto-resolution — bubble the 401 there.
        mock.get(
            "https://acme.myfreshworks.test/crm/sales/api/contacts/filters"
        ).mock(return_value=httpx.Response(401, json={"error": "unauthorized"}))

        result = await freshsales_tools.freshsales_get_contacts(
            permissions=["freshsales_access"]
        )

    assert result["status"] == "error"
    assert "401" in (result["error"] or "")


@pytest.mark.asyncio
async def test_freshsales_returns_error_on_403(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_freshsales_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "https://acme.myfreshworks.test/crm/sales/api/deals/filters"
        ).mock(return_value=httpx.Response(403, json={"error": "forbidden"}))
        result = await freshsales_tools.freshsales_get_deals(
            permissions=["freshsales_access"]
        )

    assert result["status"] == "error"
    assert "403" in (result["error"] or "")


@pytest.mark.asyncio
async def test_freshsales_credentials_not_in_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _set_freshsales_env(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="agents.mcp.tools.freshsales_tools")

    with respx.mock(assert_all_called=True) as mock:
        mock.get(
            "https://acme.myfreshworks.test/crm/sales/api/sales_accounts/filters"
        ).mock(
            return_value=httpx.Response(
                200, json={"filters": [{"id": 99, "name": "All Accounts"}]}
            )
        )
        mock.get(
            "https://acme.myfreshworks.test/crm/sales/api/sales_accounts/view/99"
        ).mock(
            return_value=httpx.Response(
                200, json={"sales_accounts": [{"id": 1, "name": "SEC-LEAK-TEST"}]}
            )
        )
        await freshsales_tools.freshsales_get_accounts(permissions=["freshsales_access"])

    blob = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    assert "fk-secret-do-not-log" not in blob
    assert "SEC-LEAK-TEST" not in blob
