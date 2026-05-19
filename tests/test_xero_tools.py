from __future__ import annotations

import logging

import httpx
import pytest
import respx

from agents.mcp import config as mcp_config
from agents.mcp.tools import xero_tools


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


def _set_xero_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env = {
        "XERO_CLIENT_ID": "cid-test",
        "XERO_CLIENT_SECRET": "csec-test-do-not-log",
        "XERO_TENANT_ID": "tenant-uuid",
        "XERO_IDENTITY_URL": "https://identity.test/connect/token",
        "XERO_BASE_URL": "https://api.test/api.xro/2.0",
        "MCP_TOOL_RESULT_LIMIT": "50",
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.mark.asyncio
async def test_xero_get_contacts_returns_not_configured_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("XERO_CLIENT_ID", "XERO_CLIENT_SECRET", "XERO_TENANT_ID"):
        monkeypatch.delenv(key, raising=False)

    result = await xero_tools.xero_get_contacts(permissions=["finance_access"])

    assert result["source"] == "xero"
    assert result["status"] == "not_configured"


@pytest.mark.asyncio
async def test_xero_get_invoices_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_xero_env(monkeypatch)
    invoices_payload = {
        "Invoices": [{"InvoiceID": "abc", "Status": "AUTHORISED", "Total": 100.0}],
    }

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://identity.test/connect/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok-123", "expires_in": 1800})
        )
        invoices_route = mock.get("https://api.test/api.xro/2.0/Invoices").mock(
            return_value=httpx.Response(200, json=invoices_payload)
        )

        result = await xero_tools.xero_get_invoices(
            status="AUTHORISED",
            page=1,
            page_size=10,
            permissions=["finance_access"],
        )

    assert result["status"] == "ok"
    assert result["data"]["count"] == 1
    assert result["data"]["invoices"][0]["InvoiceID"] == "abc"
    request = invoices_route.calls[0].request
    assert request.headers["authorization"] == "Bearer tok-123"
    assert request.headers["xero-tenant-id"] == "tenant-uuid"
    assert 'Status=="AUTHORISED"' in request.url.params["where"]


@pytest.mark.asyncio
async def test_xero_get_profit_loss_passes_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_xero_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://identity.test/connect/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
        )
        pl_route = mock.get("https://api.test/api.xro/2.0/Reports/ProfitAndLoss").mock(
            return_value=httpx.Response(200, json={"Reports": [{"ReportName": "P&L"}]})
        )

        result = await xero_tools.xero_get_profit_loss(
            from_date="2026-01-01",
            to_date="2026-03-31",
            permissions=["finance_access"],
        )

    assert result["status"] == "ok"
    assert result["data"]["count"] == 1
    params = pl_route.calls[0].request.url.params
    assert params["fromDate"] == "2026-01-01"
    assert params["toDate"] == "2026-03-31"


@pytest.mark.asyncio
async def test_xero_returns_error_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_xero_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://identity.test/connect/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
        )
        mock.get("https://api.test/api.xro/2.0/Contacts").mock(
            return_value=httpx.Response(401, json={"Detail": "unauthorized"})
        )

        result = await xero_tools.xero_get_contacts(permissions=["finance_access"])

    assert result["source"] == "xero"
    assert result["status"] == "error"
    assert "401" in (result["error"] or "")


@pytest.mark.asyncio
async def test_xero_credentials_not_in_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _set_xero_env(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="agents.mcp.tools.xero_tools")

    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://identity.test/connect/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok-SECRET", "expires_in": 60})
        )
        mock.get("https://api.test/api.xro/2.0/Payments").mock(
            return_value=httpx.Response(200, json={"Payments": []})
        )
        await xero_tools.xero_get_payments(permissions=["finance_access"])

    blob = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    assert "csec-test-do-not-log" not in blob
    assert "tok-SECRET" not in blob


@pytest.mark.asyncio
async def test_xero_token_cached_between_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_xero_env(monkeypatch)
    with respx.mock(assert_all_called=True) as mock:
        token_route = mock.post("https://identity.test/connect/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 1800})
        )
        mock.get("https://api.test/api.xro/2.0/Contacts").mock(
            return_value=httpx.Response(200, json={"Contacts": []})
        )

        service = xero_tools.XeroService()
        await service.get_contacts()
        await service.get_contacts()
        await service.aclose()

    assert token_route.call_count == 1
