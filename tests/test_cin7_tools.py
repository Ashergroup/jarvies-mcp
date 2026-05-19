from __future__ import annotations

import logging

import httpx
import pytest
import respx

from agents.mcp import config as mcp_config
from agents.mcp.tools import cin7_tools


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


def _set_cin7_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env = {
        "CIN7_API_KEY": "ck-secret-do-not-log",
        "CIN7_ACCOUNT_ID": "acct-uuid",
        "CIN7_BASE_URL": "https://api.cin7.test/ExternalApi/v2",
        "MCP_TOOL_RESULT_LIMIT": "50",
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.mark.asyncio
async def test_cin7_get_inventory_returns_not_configured_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Override values from any locally-present .env to simulate missing creds.
    for key in ("CIN7_API_KEY", "CIN7_ACCOUNT_ID"):
        monkeypatch.setenv(key, "")

    result = await cin7_tools.cin7_get_inventory(permissions=["finance_access"])

    assert result["source"] == "cin7"
    assert result["status"] == "not_configured"


@pytest.mark.asyncio
async def test_cin7_get_inventory_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_cin7_env(monkeypatch)
    payload = {
        "Products": [
            {"SKU": "ABC-1", "Name": "Widget", "Status": "Active"},
            {"SKU": "ABC-2", "Name": "Gadget", "Status": "Active"},
        ]
    }

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("https://api.cin7.test/ExternalApi/v2/product").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = await cin7_tools.cin7_get_inventory(
            sku="ABC-1",
            page=1,
            page_size=25,
            permissions=["finance_access"],
        )

    assert result["status"] == "ok"
    assert result["source"] == "cin7"
    assert result["data"]["count"] == 2
    assert result["data"]["inventory"][0]["SKU"] == "ABC-1"
    request = route.calls[0].request
    assert request.headers["api-auth-accountid"] == "acct-uuid"
    assert request.headers["api-auth-applicationkey"] == "ck-secret-do-not-log"
    assert request.url.params["Sku"] == "ABC-1"
    assert request.url.params["Limit"] == "25"


@pytest.mark.asyncio
async def test_cin7_get_sales_orders_passes_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_cin7_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("https://api.cin7.test/ExternalApi/v2/saleList").mock(
            return_value=httpx.Response(
                200,
                json={"SaleList": [{"ID": "s1", "Status": "AUTHORISED"}]},
            )
        )
        result = await cin7_tools.cin7_get_sales_orders(
            status="AUTHORISED",
            date_from="2026-01-01",
            date_to="2026-03-31",
            permissions=["finance_access"],
        )

    assert result["status"] == "ok"
    assert result["data"]["count"] == 1
    assert result["data"]["sales_orders"][0]["ID"] == "s1"
    params = route.calls[0].request.url.params
    assert params["Status"] == "AUTHORISED"
    assert params["UpdatedSince"] == "2026-01-01"
    assert params["CreatedBefore"] == "2026-03-31"


@pytest.mark.asyncio
async def test_cin7_get_purchase_orders_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_cin7_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.cin7.test/ExternalApi/v2/purchaseList").mock(
            return_value=httpx.Response(
                200, json={"PurchaseList": [{"ID": "p1"}, {"ID": "p2"}]}
            )
        )
        result = await cin7_tools.cin7_get_purchase_orders(permissions=["finance_access"])

    assert result["status"] == "ok"
    assert result["data"]["count"] == 2


@pytest.mark.asyncio
async def test_cin7_returns_error_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_cin7_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.cin7.test/ExternalApi/v2/product").mock(
            return_value=httpx.Response(401, json={"error": "unauthorized"})
        )
        result = await cin7_tools.cin7_get_inventory(permissions=["finance_access"])

    assert result["status"] == "error"
    assert result["source"] == "cin7"
    assert "401" in (result["error"] or "")


@pytest.mark.asyncio
async def test_cin7_returns_error_on_403(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_cin7_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.cin7.test/ExternalApi/v2/saleList").mock(
            return_value=httpx.Response(403, json={"error": "forbidden"})
        )
        result = await cin7_tools.cin7_get_sales_orders(permissions=["finance_access"])

    assert result["status"] == "error"
    assert "403" in (result["error"] or "")


@pytest.mark.asyncio
async def test_cin7_credentials_not_in_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _set_cin7_env(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="agents.mcp.tools.cin7_tools")

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.cin7.test/ExternalApi/v2/ref/productavailability").mock(
            return_value=httpx.Response(
                200, json={"ProductAvailabilityList": [{"SKU": "SEC-LEAK-TEST"}]}
            )
        )
        await cin7_tools.cin7_get_stock_levels(permissions=["finance_access"])

    blob = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    assert "ck-secret-do-not-log" not in blob
    assert "acct-uuid" not in blob
    assert "SEC-LEAK-TEST" not in blob
