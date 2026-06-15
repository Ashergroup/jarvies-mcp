from __future__ import annotations

import httpx
import pytest
import respx

from agents.mcp.permissions import MCPPermissionError, check_permission
from agents.mcp.tools import db_tools, m365_tools, xero_tools

GRAPH = "https://graph.microsoft.com/v1.0"


def test_permission_allows_m365_read() -> None:
    assert check_permission("tenant-a", "user-a", "m365_search_emails", ["m365_access"])


def test_permission_denies_missing_scope() -> None:
    with pytest.raises(MCPPermissionError):
        check_permission("tenant-a", "user-a", "m365_search_emails", ["read_only"])


def test_permission_denies_readonly_write() -> None:
    with pytest.raises(MCPPermissionError):
        check_permission(
            "tenant-a",
            "user-a",
            "m365_create_email_draft",
            ["m365_access", "read_only"],
        )


def test_database_validation_rejects_dangerous_sql() -> None:
    with pytest.raises(db_tools.UnsafeQueryError):
        db_tools._validate_readonly_query("DROP TABLE users")


def test_database_validation_rejects_multiple_statements() -> None:
    with pytest.raises(db_tools.UnsafeQueryError):
        db_tools._validate_readonly_query("SELECT * FROM users; SELECT * FROM tenants")


def test_database_validation_allows_select() -> None:
    assert db_tools._validate_readonly_query("SELECT id FROM users LIMIT 10") == (
        "SELECT id FROM users LIMIT 10"
    )


@pytest.mark.asyncio
async def test_m365_search_emails_calls_graph_directly() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{GRAPH}/me/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "msg-1",
                            "subject": "Board pack",
                            "from": {"emailAddress": {"address": "cfo@nichegroup.africa"}},
                            "receivedDateTime": "2026-06-01T09:00:00Z",
                            "isRead": False,
                            "hasAttachments": True,
                            "bodyPreview": "<p>draft attached</p>",
                            "importance": "high",
                        }
                    ]
                },
            )
        )
        result = await m365_tools.m365_search_emails(
            query="board",
            limit=5,
            tenant_id="tenant-a",
            user_id="user-a",
            access_token="token-123",
            permissions=["m365_access"],
        )

    assert result["status"] == "ok"
    assert result["source"] == "m365"
    assert result["data"]["count"] == 1
    assert result["data"]["emails"][0]["subject"] == "Board pack"
    assert result["data"]["emails"][0]["uri"] == "mail:///messages/msg-1"
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer token-123"
    assert request.url.params["$search"] == '"board"'


@pytest.mark.asyncio
async def test_m365_search_emails_error_on_403() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GRAPH}/me/messages").mock(
            return_value=httpx.Response(403, json={"error": "forbidden"})
        )
        result = await m365_tools.m365_search_emails(
            query="board",
            access_token="token-123",
            permissions=["m365_access"],
        )

    assert result["status"] == "error"
    assert "403" in (result["error"] or "")


@pytest.mark.asyncio
async def test_xero_placeholder_requires_finance_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Override values from any locally-present .env to simulate missing creds.
    for key in ("XERO_CLIENT_ID", "XERO_CLIENT_SECRET", "XERO_TENANT_ID"):
        monkeypatch.setenv(key, "")
    from agents.mcp import config as mcp_config

    mcp_config.get_settings.cache_clear()
    try:
        result = await xero_tools.xero_get_contacts(
            tenant_id="tenant-a",
            user_id="user-a",
            permissions=["finance_access"],
        )
        assert result["status"] == "not_configured"
    finally:
        mcp_config.get_settings.cache_clear()


def test_hello_tool_function_when_mcp_sdk_available() -> None:
    pytest.importorskip("mcp.server.fastmcp")
    from agents.mcp.server import hello

    assert hello("Asher") == "Hello Asher"
