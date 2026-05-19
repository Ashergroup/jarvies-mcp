from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agents.mcp.permissions import MCPPermissionError, check_permission
from agents.mcp.tools import db_tools, m365_tools, xero_tools


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
async def test_m365_search_emails_wraps_existing_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {}

    def fake_search_emails(**kwargs):
        calls.update(kwargs)
        return [{"subject": "Board pack"}]

    @contextmanager
    def fake_scope(access_token, write_tools=None):
        assert access_token == "token-123"
        yield

    monkeypatch.setattr(
        m365_tools,
        "_load_read_tools",
        lambda: SimpleNamespace(search_emails=fake_search_emails),
    )
    monkeypatch.setattr(m365_tools, "_m365_call_scope", fake_scope)

    result = await m365_tools.m365_search_emails(
        query="board",
        limit=5,
        tenant_id="tenant-a",
        user_id="user-a",
        access_token="token-123",
        permissions=["m365_access"],
    )

    assert result == [{"subject": "Board pack"}]
    assert calls["query"] == "board"
    assert calls["limit"] == 5


@pytest.mark.asyncio
async def test_xero_placeholder_requires_finance_access() -> None:
    result = await xero_tools.xero_get_contacts(
        tenant_id="tenant-a",
        user_id="user-a",
        permissions=["finance_access"],
    )
    assert result["status"] == "not_configured"


def test_hello_tool_function_when_mcp_sdk_available() -> None:
    pytest.importorskip("mcp.server.fastmcp")
    from agents.mcp.server import hello

    assert hello("Asher") == "Hello Asher"
