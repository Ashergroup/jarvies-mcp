from __future__ import annotations

import logging

import httpx
import pytest
import respx

from agents.mcp import config as mcp_config
from agents.mcp.tools import m365_write_tools

GRAPH = "https://graph.microsoft.com/v1.0"
PERMS = ["m365_access"]
TOKEN = "fake-graph-token-do-not-log"


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
    result = await m365_write_tools.m365_send_email(
        to=["a@nichegroup.africa"],
        subject="x",
        body="y",
        permissions=PERMS,
    )
    assert result["status"] == "error"
    assert "access_token" in (result["error"] or "")


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
