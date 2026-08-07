from __future__ import annotations

import base64
import logging

import httpx
import pytest
import respx

from agents.mcp import config as mcp_config
from agents.mcp.permissions import TOOL_POLICIES, MCPPermissionError, check_permission
from agents.mcp.tools import freshdesk_tools

BASE = "https://acme.freshdesk.test/api/v2"
API_KEY = "fd-secret-do-not-log"
EXPECTED_AUTH = "Basic " + base64.b64encode(f"{API_KEY}:X".encode()).decode()

FRESHDESK_TOOLS = (
    "freshdesk_list_tickets",
    "freshdesk_get_ticket",
    "freshdesk_search_tickets",
    "freshdesk_list_agents",
    "freshdesk_get_ticket_summary",
)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


def _set_freshdesk_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    env = {
        "FRESHDESK_DOMAIN": "acme.freshdesk.test",
        "FRESHDESK_API_KEY": API_KEY,
        "MCP_TOOL_RESULT_LIMIT": "50",
    }
    env.update(overrides)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _ticket(ticket_id: int, **fields: object) -> dict[str, object]:
    base = {
        "id": ticket_id,
        "subject": f"Ticket {ticket_id}",
        "status": 2,
        "priority": 1,
        "requester_id": 500,
        "responder_id": 900,
    }
    base.update(fields)
    return base


# ---------------------------------------------------------------------------
# Permissions and registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", FRESHDESK_TOOLS)
def test_support_access_permits_every_freshdesk_tool(tool: str) -> None:
    assert check_permission("tenant-a", "user-a", tool, ["support_access"])


@pytest.mark.parametrize("tool", FRESHDESK_TOOLS)
def test_freshdesk_tools_denied_without_support_access(tool: str) -> None:
    with pytest.raises(MCPPermissionError, match="requires one of: support_access"):
        check_permission("tenant-a", "user-a", tool, ["freshsales_access"])


@pytest.mark.parametrize("tool", FRESHDESK_TOOLS)
def test_freshdesk_tools_are_reads_so_read_only_still_permits_them(tool: str) -> None:
    assert TOOL_POLICIES[tool].write is False
    assert check_permission("tenant-a", "user-a", tool, ["support_access", "read_only"])


def test_all_freshdesk_tools_are_registered() -> None:
    registered: list[str] = []

    class _Recorder:
        def tool(self):
            def decorate(fn):
                registered.append(fn.__name__)
                return fn

            return decorate

    freshdesk_tools.register(_Recorder())
    assert registered == list(FRESHDESK_TOOLS)


def test_freshdesk_module_exposes_no_write_helpers() -> None:
    exported = [name for name in dir(freshdesk_tools) if name.startswith("freshdesk_")]
    assert sorted(exported) == sorted(FRESHDESK_TOOLS)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_not_configured_when_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Override values from any locally-present .env to simulate missing creds.
    for key in ("FRESHDESK_DOMAIN", "FRESHDESK_API_KEY"):
        monkeypatch.setenv(key, "")

    result = await freshdesk_tools.freshdesk_list_tickets(permissions=["support_access"])

    assert result["source"] == "freshdesk"
    assert result["status"] == "not_configured"


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("acme", "acme.freshdesk.com"),
        ("acme.freshdesk.com", "acme.freshdesk.com"),
        ("https://acme.freshdesk.com/", "acme.freshdesk.com"),
    ],
)
def test_domain_normalisation(configured: str, expected: str) -> None:
    assert freshdesk_tools._normalise_domain(configured) == expected


@pytest.mark.asyncio
async def test_bare_subdomain_targets_freshdesk_com(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_freshdesk_env(monkeypatch, FRESHDESK_DOMAIN="acme")

    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://acme.freshdesk.com/api/v2/agents").mock(
            return_value=httpx.Response(200, json=[{"id": 1}])
        )
        result = await freshdesk_tools.freshdesk_list_agents(
            permissions=["support_access"]
        )

    assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# freshdesk_list_tickets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tickets_uses_list_endpoint_when_unfiltered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_freshdesk_env(monkeypatch)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{BASE}/tickets").mock(
            return_value=httpx.Response(200, json=[_ticket(1), _ticket(2)])
        )
        result = await freshdesk_tools.freshdesk_list_tickets(
            requester_id=500,
            updated_since="2026-07-01T00:00:00Z",
            page=2,
            page_size=25,
            permissions=["support_access"],
        )

    assert result["status"] == "ok"
    assert result["data"]["endpoint"] == "tickets"
    assert result["data"]["count"] == 2
    request = route.calls[0].request
    assert request.headers["authorization"] == EXPECTED_AUTH
    assert request.url.params["requester_id"] == "500"
    assert request.url.params["updated_since"] == "2026-07-01T00:00:00Z"
    assert request.url.params["page"] == "2"
    assert request.url.params["per_page"] == "25"


@pytest.mark.asyncio
async def test_list_tickets_switches_to_filter_endpoint_for_status_and_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_freshdesk_env(monkeypatch)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{BASE}/search/tickets").mock(
            return_value=httpx.Response(
                200, json={"total": 7, "results": [_ticket(3, status=3, priority=4)]}
            )
        )
        result = await freshdesk_tools.freshdesk_list_tickets(
            status="pending",
            priority="urgent",
            agent_id=900,
            updated_since="2026-07-01T09:30:00Z",
            permissions=["support_access"],
        )

    assert result["data"]["endpoint"] == "search/tickets"
    assert result["data"]["total"] == 7
    assert result["data"]["count"] == 1
    # Names map to Freshdesk's integer codes; the timestamp is truncated to a date.
    assert result["data"]["query"] == (
        "status:3 AND priority:4 AND agent_id:900 AND updated_at:>'2026-07-01'"
    )
    assert route.calls[0].request.url.params["query"] == (
        '"status:3 AND priority:4 AND agent_id:900 AND updated_at:>\'2026-07-01\'"'
    )


@pytest.mark.asyncio
async def test_list_tickets_accepts_integer_status_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_freshdesk_env(monkeypatch)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/search/tickets").mock(
            return_value=httpx.Response(200, json={"total": 1, "results": [_ticket(4)]})
        )
        result = await freshdesk_tools.freshdesk_list_tickets(
            status=5, permissions=["support_access"]
        )

    assert result["data"]["query"] == "status:5"


@pytest.mark.asyncio
async def test_list_tickets_filters_requester_locally_on_filter_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_freshdesk_env(monkeypatch)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/search/tickets").mock(
            return_value=httpx.Response(
                200,
                json={
                    "total": 2,
                    "results": [
                        _ticket(5, requester_id=500),
                        _ticket(6, requester_id=999),
                    ],
                },
            )
        )
        result = await freshdesk_tools.freshdesk_list_tickets(
            status="open", requester_id=500, permissions=["support_access"]
        )

    # requester_id is not a filter-API field, so it is applied to the page.
    assert result["data"]["query"] == "status:2"
    assert result["data"]["count"] == 1
    assert result["data"]["tickets"][0]["id"] == 5
    assert result["data"]["requester_filtered_locally"] is True


@pytest.mark.asyncio
async def test_list_tickets_caps_filter_page_at_api_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_freshdesk_env(monkeypatch)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{BASE}/search/tickets").mock(
            return_value=httpx.Response(200, json={"total": 0, "results": []})
        )
        result = await freshdesk_tools.freshdesk_list_tickets(
            status="open", page=40, permissions=["support_access"]
        )

    assert route.calls[0].request.url.params["page"] == "10"
    assert result["data"]["page"] == 10
    assert result["data"]["page_limit"] == 10


@pytest.mark.asyncio
async def test_list_tickets_rejects_unknown_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_freshdesk_env(monkeypatch)

    with respx.mock(assert_all_called=False):
        result = await freshdesk_tools.freshdesk_list_tickets(
            status="escalated", permissions=["support_access"]
        )

    assert result["status"] == "error"
    assert "escalated" in (result["error"] or "")


# ---------------------------------------------------------------------------
# freshdesk_get_ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_ticket_returns_conversation_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_freshdesk_env(monkeypatch)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/tickets/42").mock(
            return_value=httpx.Response(
                200, json=_ticket(42, description_text="printer offline")
            )
        )
        thread = mock.get(f"{BASE}/tickets/42/conversations").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": 1, "body_text": "first reply"},
                    {"id": 2, "body_text": "second reply"},
                ],
            )
        )
        result = await freshdesk_tools.freshdesk_get_ticket(
            ticket_id=42, conversation_limit=10, permissions=["support_access"]
        )

    assert result["status"] == "ok"
    assert result["data"]["ticket"]["id"] == 42
    assert result["data"]["conversation_count"] == 2
    assert thread.calls[0].request.url.params["per_page"] == "10"


@pytest.mark.asyncio
async def test_get_ticket_can_skip_the_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_freshdesk_env(monkeypatch)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/tickets/7").mock(
            return_value=httpx.Response(200, json=_ticket(7))
        )
        result = await freshdesk_tools.freshdesk_get_ticket(
            ticket_id=7, include_conversations=False, permissions=["support_access"]
        )

    assert result["data"]["conversations"] == []
    assert result["data"]["conversation_count"] == 0


# ---------------------------------------------------------------------------
# freshdesk_search_tickets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_tickets_matches_subject_and_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_freshdesk_env(monkeypatch)
    page = [
        _ticket(1, subject="VPN outage in Cape Town"),
        _ticket(2, subject="Laptop request", description_text="cannot reach the VPN"),
        _ticket(3, subject="Payroll query", description_text="March payslip"),
    ]

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{BASE}/tickets").mock(
            return_value=httpx.Response(200, json=page)
        )
        result = await freshdesk_tools.freshdesk_search_tickets(
            query="vpn", permissions=["support_access"]
        )

    assert result["status"] == "ok"
    assert [t["id"] for t in result["data"]["tickets"]] == [1, 2]
    assert result["data"]["match_count"] == 2
    assert result["data"]["scanned"] == 3
    assert result["data"]["pages_scanned"] == 1
    # A short page means the scan reached the end of the list.
    assert result["data"]["scan_exhausted"] is True
    assert result["data"]["truncated"] is False
    assert route.calls[0].request.url.params["include"] == "description"


@pytest.mark.asyncio
async def test_search_tickets_reports_truncation_when_page_limit_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_freshdesk_env(monkeypatch)
    full_page = [_ticket(i, subject="unrelated") for i in range(100)]

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{BASE}/tickets").mock(
            return_value=httpx.Response(200, json=full_page)
        )
        result = await freshdesk_tools.freshdesk_search_tickets(
            query="vpn", max_pages=2, permissions=["support_access"]
        )

    assert len(route.calls) == 2
    assert result["data"]["count"] == 0
    assert result["data"]["scanned"] == 200
    assert result["data"]["pages_scanned"] == 2
    assert result["data"]["scan_exhausted"] is False
    assert result["data"]["truncated"] is True
    assert "no full-text" in result["data"]["note"]


@pytest.mark.asyncio
async def test_search_tickets_rejects_empty_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_freshdesk_env(monkeypatch)

    with respx.mock(assert_all_called=False):
        result = await freshdesk_tools.freshdesk_search_tickets(
            query="   ", permissions=["support_access"]
        )

    assert result["status"] == "error"
    assert "query cannot be empty" in (result["error"] or "")


# ---------------------------------------------------------------------------
# freshdesk_list_agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_agents_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_freshdesk_env(monkeypatch)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{BASE}/agents").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": 900, "contact": {"name": "Thandi", "email": "t@acme.test"}},
                    {"id": 901, "contact": {"name": "Sipho", "email": "s@acme.test"}},
                ],
            )
        )
        result = await freshdesk_tools.freshdesk_list_agents(
            page_size=2, permissions=["support_access"]
        )

    assert result["data"]["count"] == 2
    assert result["data"]["agents"][0]["contact"]["name"] == "Thandi"
    assert route.calls[0].request.url.params["per_page"] == "2"


# ---------------------------------------------------------------------------
# freshdesk_get_ticket_summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ticket_summary_counts_by_status_priority_and_sla(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_freshdesk_env(monkeypatch)
    totals = {
        "status:2": 12,
        "status:3": 4,
        "status:4": 30,
        "status:5": 108,
        "priority:1": 60,
        "priority:2": 70,
        "priority:3": 20,
        "priority:4": 4,
        "(status:2 OR status:3) AND due_by:<'2026-08-05'": 5,
        "(status:2 OR status:3) AND fr_due_by:<'2026-08-05'": 2,
    }
    seen: list[str] = []

    def _respond(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"].strip('"')
        seen.append(query)
        return httpx.Response(200, json={"total": totals[query], "results": []})

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/search/tickets").mock(side_effect=_respond)
        result = await freshdesk_tools.freshdesk_get_ticket_summary(
            as_of="2026-08-05T12:00:00Z", permissions=["support_access"]
        )

    data = result["data"]
    assert data["by_status"] == {"open": 12, "pending": 4, "resolved": 30, "closed": 108}
    assert data["by_priority"] == {"low": 60, "medium": 70, "high": 20, "urgent": 4}
    assert data["open_and_pending"] == 16
    assert data["overdue"] == 5
    assert data["first_response_overdue"] == 2
    assert data["as_of"] == "2026-08-05"
    assert len(seen) == 10


@pytest.mark.asyncio
async def test_ticket_summary_defaults_as_of_to_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_freshdesk_env(monkeypatch)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/search/tickets").mock(
            return_value=httpx.Response(200, json={"total": 0, "results": []})
        )
        result = await freshdesk_tools.freshdesk_get_ticket_summary(
            permissions=["support_access"]
        )

    assert result["data"]["as_of"] == freshdesk_tools._today_utc()


@pytest.mark.asyncio
async def test_ticket_summary_falls_back_to_counting_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_freshdesk_env(monkeypatch)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/search/tickets").mock(
            return_value=httpx.Response(200, json={"results": [_ticket(1)]})
        )
        result = await freshdesk_tools.freshdesk_get_ticket_summary(
            as_of="2026-08-05", permissions=["support_access"]
        )

    assert result["data"]["by_status"]["open"] == 1


# ---------------------------------------------------------------------------
# Error handling and secret hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_error_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_freshdesk_env(monkeypatch)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/tickets").mock(
            return_value=httpx.Response(401, json={"message": "unauthorized"})
        )
        result = await freshdesk_tools.freshdesk_list_tickets(
            permissions=["support_access"]
        )

    assert result["status"] == "error"
    assert "401" in (result["error"] or "")


@pytest.mark.asyncio
async def test_returns_error_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_freshdesk_env(monkeypatch)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/search/tickets").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "30"})
        )
        result = await freshdesk_tools.freshdesk_list_tickets(
            status="open", permissions=["support_access"]
        )

    assert result["status"] == "error"
    assert "429" in (result["error"] or "")


@pytest.mark.asyncio
async def test_returns_error_on_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_freshdesk_env(monkeypatch)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/agents").mock(side_effect=httpx.ConnectError("boom"))
        result = await freshdesk_tools.freshdesk_list_agents(
            permissions=["support_access"]
        )

    assert result["status"] == "error"
    assert "ConnectError" in (result["error"] or "")


@pytest.mark.asyncio
async def test_credentials_and_ticket_content_not_in_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _set_freshdesk_env(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="agents.mcp.tools.freshdesk_tools")

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/tickets").mock(
            return_value=httpx.Response(
                200, json=[_ticket(1, subject="SEC-LEAK-TEST")]
            )
        )
        await freshdesk_tools.freshdesk_list_tickets(permissions=["support_access"])

    blob = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
    assert API_KEY not in blob
    assert "SEC-LEAK-TEST" not in blob
