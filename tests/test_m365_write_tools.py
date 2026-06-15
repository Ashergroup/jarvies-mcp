from __future__ import annotations

import logging

import httpx
import pytest
import respx

from agents.mcp import config as mcp_config
from agents.mcp import tenant as mcp_tenant
from agents.mcp.tools import m365_tools, m365_write_tools

GRAPH = "https://graph.microsoft.com/v1.0"
PERMS = ["m365_access"]
TOKEN = "fake-graph-token-do-not-log"
REAL_USER_ID = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# m365_send_email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_email_happy_path() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{GRAPH}/me/sendMail").mock(
            return_value=httpx.Response(202)
        )
        result = await m365_write_tools.m365_send_email(
            to=["a@nichegroup.africa"],
            subject="Hello",
            body="Body text",
            cc=["b@nichegroup.africa"],
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["data"]["sent"] is True
    assert result["data"]["to"] == ["a@nichegroup.africa"]
    sent = route.calls[0].request
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_send_email_error_on_403() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{GRAPH}/me/sendMail").mock(
            return_value=httpx.Response(403, json={"error": "forbidden"})
        )
        result = await m365_write_tools.m365_send_email(
            to=["a@nichegroup.africa"],
            subject="Hello",
            body="Body",
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "error"
    assert "403" in (result["error"] or "")


# ---------------------------------------------------------------------------
# m365_create_calendar_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_calendar_event_happy_path() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{GRAPH}/me/events").mock(
            return_value=httpx.Response(
                201, json={"id": "evt1", "webLink": "https://outlook/evt1"}
            )
        )
        result = await m365_write_tools.m365_create_calendar_event(
            subject="Board prep",
            start_iso="2026-07-01T09:00:00",
            end_iso="2026-07-01T10:00:00",
            attendees=["cfo@nichegroup.africa"],
            location="Room 1",
            is_online_meeting=True,
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["data"]["event_id"] == "evt1"
    assert result["data"]["web_url"] == "https://outlook/evt1"
    body = route.calls[0].request.content.decode()
    assert "teamsForBusiness" in body


@pytest.mark.asyncio
async def test_create_calendar_event_error_on_400() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{GRAPH}/me/events").mock(
            return_value=httpx.Response(400, json={"error": "bad request"})
        )
        result = await m365_write_tools.m365_create_calendar_event(
            subject="Bad",
            start_iso="not-a-date",
            end_iso="also-bad",
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "error"
    assert "400" in (result["error"] or "")


# ---------------------------------------------------------------------------
# m365_upload_to_sharepoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_to_sharepoint_happy_path(tmp_path) -> None:
    local = tmp_path / "report.txt"
    local.write_text("hello sharepoint", encoding="utf-8")
    folder_url = "https://contoso.sharepoint.com/sites/Finance/Shared%20Documents/Reports"
    share_id = m365_write_tools._encode_share_url(folder_url)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GRAPH}/shares/{share_id}/driveItem").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "folder1",
                    "name": "Reports",
                    "parentReference": {"driveId": "drive1"},
                },
            )
        )
        mock.route(
            method="PUT",
            url__regex=r"https://graph\.microsoft\.com/v1\.0/drives/drive1/items/folder1.*content",
        ).mock(
            return_value=httpx.Response(
                201,
                json={"id": "file1", "name": "report.txt", "webUrl": "https://sp/file1"},
            )
        )
        result = await m365_write_tools.m365_upload_to_sharepoint(
            file_path=str(local),
            destination_folder_url=folder_url,
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["data"]["item_id"] == "file1"
    assert result["data"]["web_url"] == "https://sp/file1"


@pytest.mark.asyncio
async def test_upload_to_sharepoint_error_on_resolve_403(tmp_path) -> None:
    local = tmp_path / "report.txt"
    local.write_text("hello", encoding="utf-8")
    folder_url = "https://contoso.sharepoint.com/sites/Finance/Reports"
    share_id = m365_write_tools._encode_share_url(folder_url)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GRAPH}/shares/{share_id}/driveItem").mock(
            return_value=httpx.Response(403, json={"error": "forbidden"})
        )
        result = await m365_write_tools.m365_upload_to_sharepoint(
            file_path=str(local),
            destination_folder_url=folder_url,
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "error"
    assert "403" in (result["error"] or "")


# ---------------------------------------------------------------------------
# m365_create_sharepoint_folder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_sharepoint_folder_happy_path() -> None:
    parent_url = "https://contoso.sharepoint.com/sites/Finance/Shared%20Documents"
    share_id = m365_write_tools._encode_share_url(parent_url)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GRAPH}/shares/{share_id}/driveItem").mock(
            return_value=httpx.Response(
                200,
                json={"id": "parent1", "parentReference": {"driveId": "drive1"}},
            )
        )
        mock.post(f"{GRAPH}/drives/drive1/items/parent1/children").mock(
            return_value=httpx.Response(
                201,
                json={"id": "newfolder", "name": "Q3", "webUrl": "https://sp/q3"},
            )
        )
        result = await m365_write_tools.m365_create_sharepoint_folder(
            parent_folder_url=parent_url,
            folder_name="Q3",
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["data"]["folder_id"] == "newfolder"
    assert result["data"]["name"] == "Q3"


@pytest.mark.asyncio
async def test_create_sharepoint_folder_error_on_resolve_404() -> None:
    parent_url = "https://contoso.sharepoint.com/sites/Finance/Missing"
    share_id = m365_write_tools._encode_share_url(parent_url)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GRAPH}/shares/{share_id}/driveItem").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        result = await m365_write_tools.m365_create_sharepoint_folder(
            parent_folder_url=parent_url,
            folder_name="Q3",
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "error"
    assert "404" in (result["error"] or "")


# ---------------------------------------------------------------------------
# m365_post_teams_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_teams_message_channel_happy_path() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{GRAPH}/teams/team1/channels/channel1/messages").mock(
            return_value=httpx.Response(
                201, json={"id": "msg1", "webUrl": "https://teams/msg1"}
            )
        )
        result = await m365_write_tools.m365_post_teams_message(
            channel_or_chat_id="team1/channel1",
            message="Standup at 9",
            subject="Daily",
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["data"]["message_id"] == "msg1"
    body = route.calls[0].request.content.decode()
    assert "Daily" in body  # subject honoured on channel posts


@pytest.mark.asyncio
async def test_post_teams_message_chat_error_on_403() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.route(
            method="POST",
            url__regex=r"https://graph\.microsoft\.com/v1\.0/chats/.+/messages",
        ).mock(return_value=httpx.Response(403, json={"error": "forbidden"}))
        result = await m365_write_tools.m365_post_teams_message(
            channel_or_chat_id="19:chat-abc@thread.v2",
            message="hi",
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "error"
    assert "403" in (result["error"] or "")


# ---------------------------------------------------------------------------
# Cross-cutting: no token, and token never logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_access_token_returns_error() -> None:
    # No explicit token, no user_id, no auth context → no token resolvable.
    result = await m365_write_tools.m365_send_email(
        to=["a@nichegroup.africa"],
        subject="x",
        body="y",
        permissions=PERMS,
    )
    assert result["status"] == "error"
    assert result["error"] == "No M365 access token available — please reconnect via OAuth"


# ---------------------------------------------------------------------------
# _get_m365_token helper — token resolution priority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_m365_token_prefers_explicit_token() -> None:
    # Explicit token wins and no DB lookup is attempted.
    result = await m365_write_tools._get_m365_token("explicit-tok", REAL_USER_ID, "t")
    assert result == "explicit-tok"


@pytest.mark.asyncio
async def test_get_m365_token_falls_back_to_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_lookup(user_id: str) -> str:
        assert user_id == REAL_USER_ID
        return "stored-tok"

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token", fake_lookup)
    result = await m365_write_tools._get_m365_token(None, REAL_USER_ID, "t")
    assert result == "stored-tok"


@pytest.mark.asyncio
async def test_get_m365_token_uses_authenticated_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_lookup(user_id: str) -> str:
        assert user_id == REAL_USER_ID
        return "ctx-tok"

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token", fake_lookup)
    # No explicit token; user_id arg is the default placeholder, but the
    # bearer-token identity is published on the context var.
    token = mcp_tenant.set_current_user_id(REAL_USER_ID)
    try:
        result = await m365_write_tools._get_m365_token(None, "local-user", None)
    finally:
        mcp_tenant.reset_current_user_id(token)
    assert result == "ctx-tok"


@pytest.mark.asyncio
async def test_get_m365_token_none_when_no_identity() -> None:
    # Default placeholder user_id and no context → no DB hit, no token.
    result = await m365_write_tools._get_m365_token(None, "local-user", None)
    assert result is None


@pytest.mark.asyncio
async def test_send_email_uses_stored_token(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_lookup(user_id: str) -> str:
        return "stored-graph-token"

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token", fake_lookup)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{GRAPH}/me/sendMail").mock(
            return_value=httpx.Response(202)
        )
        result = await m365_write_tools.m365_send_email(
            to=["a@nichegroup.africa"],
            subject="Hi",
            body="Body",
            user_id=REAL_USER_ID,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert route.calls[0].request.headers["Authorization"] == "Bearer stored-graph-token"


@pytest.mark.asyncio
async def test_read_tool_uses_stored_token(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_lookup(user_id: str) -> str:
        return "stored-graph-token"

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token", fake_lookup)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{GRAPH}/me/messages").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        result = await m365_tools.m365_search_emails(
            query="board",
            user_id=REAL_USER_ID,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert route.calls[0].request.headers["Authorization"] == "Bearer stored-graph-token"


@pytest.mark.asyncio
async def test_token_not_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="agents.mcp.tools.m365_write_tools")
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{GRAPH}/me/sendMail").mock(return_value=httpx.Response(202))
        await m365_write_tools.m365_send_email(
            to=["a@nichegroup.africa"],
            subject="s",
            body="b",
            access_token=TOKEN,
            permissions=PERMS,
        )

    blob = "\n".join(
        record.getMessage() + str(record.__dict__) for record in caplog.records
    )
    assert TOKEN not in blob
