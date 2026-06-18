from __future__ import annotations

import httpx
import pytest
import respx

from agents.mcp import config as mcp_config
from agents.mcp.tools import m365_mail_folder_tools as mft

GRAPH = "https://graph.microsoft.com/v1.0"
PERMS = ["m365_access"]
TOKEN = "fake-graph-token-do-not-log"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# m365_list_mail_folders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_mail_folders_happy_path() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{GRAPH}/me/mailFolders").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "inbox",
                            "displayName": "Inbox",
                            "totalItemCount": 42,
                            "unreadItemCount": 3,
                        }
                    ]
                },
            )
        )
        result = await mft.m365_list_mail_folders(access_token=TOKEN, permissions=PERMS)

    assert result["status"] == "ok"
    assert result["data"]["count"] == 1
    folder = result["data"]["folders"][0]
    assert folder["id"] == "inbox"
    assert folder["unreadItemCount"] == 3
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_list_mail_folders_error_on_403() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GRAPH}/me/mailFolders").mock(
            return_value=httpx.Response(403, json={"error": "forbidden"})
        )
        result = await mft.m365_list_mail_folders(access_token=TOKEN, permissions=PERMS)

    assert result["status"] == "error"
    assert "403" in (result["error"] or "")


# ---------------------------------------------------------------------------
# m365_create_mail_folder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_mail_folder_happy_path() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{GRAPH}/me/mailFolders").mock(
            return_value=httpx.Response(
                201, json={"id": "newf", "displayName": "Clients"}
            )
        )
        result = await mft.m365_create_mail_folder(
            folder_name="Clients", access_token=TOKEN, permissions=PERMS
        )

    assert result["status"] == "ok"
    assert result["data"]["id"] == "newf"
    assert result["data"]["displayName"] == "Clients"
    body = route.calls[0].request.content.decode()
    assert "Clients" in body


@pytest.mark.asyncio
async def test_create_mail_folder_subfolder_uses_child_path() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{GRAPH}/me/mailFolders/parent1/childFolders").mock(
            return_value=httpx.Response(
                201, json={"id": "childf", "displayName": "Sub"}
            )
        )
        result = await mft.m365_create_mail_folder(
            folder_name="Sub",
            parent_folder_id="parent1",
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["data"]["parent_folder_id"] == "parent1"
    assert route.called


@pytest.mark.asyncio
async def test_create_mail_folder_error_on_400() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{GRAPH}/me/mailFolders").mock(
            return_value=httpx.Response(400, json={"error": "bad"})
        )
        result = await mft.m365_create_mail_folder(
            folder_name="X", access_token=TOKEN, permissions=PERMS
        )

    assert result["status"] == "error"
    assert "400" in (result["error"] or "")


# ---------------------------------------------------------------------------
# m365_move_email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_email_happy_path() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{GRAPH}/me/messages/msg1/move").mock(
            return_value=httpx.Response(201, json={"id": "msg1-moved"})
        )
        result = await mft.m365_move_email(
            message_uri="mail:///messages/msg1",
            destination_folder_id="archive",
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["data"]["message_id"] == "msg1-moved"
    assert result["data"]["uri"] == "mail:///messages/msg1-moved"
    body = route.calls[0].request.content.decode()
    assert "archive" in body


@pytest.mark.asyncio
async def test_move_email_rejects_bad_uri() -> None:
    result = await mft.m365_move_email(
        message_uri="not-a-uri",
        destination_folder_id="archive",
        access_token=TOKEN,
        permissions=PERMS,
    )
    assert result["status"] == "error"
    assert "Not an email URI" in (result["error"] or "")


@pytest.mark.asyncio
async def test_move_email_error_on_404() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{GRAPH}/me/messages/msg1/move").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        result = await mft.m365_move_email(
            message_uri="mail:///messages/msg1",
            destination_folder_id="archive",
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "error"
    assert "404" in (result["error"] or "")


# ---------------------------------------------------------------------------
# m365_list_sharepoint_folders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sharepoint_folders_happy_path() -> None:
    folder_url = "https://contoso.sharepoint.com/sites/Finance/Shared%20Documents"
    share_id = mft._encode_share_url(folder_url)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GRAPH}/shares/{share_id}/driveItem/children").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "f1",
                            "name": "Reports",
                            "webUrl": "https://sp/Reports",
                            "createdDateTime": "2026-01-01T00:00:00Z",
                            "folder": {"childCount": 2},
                        },
                        {
                            "id": "file1",
                            "name": "budget.xlsx",
                            "webUrl": "https://sp/budget",
                            "createdDateTime": "2026-01-02T00:00:00Z",
                            "file": {},
                        },
                    ]
                },
            )
        )
        result = await mft.m365_list_sharepoint_folders(
            folder_url=folder_url, access_token=TOKEN, permissions=PERMS
        )

    assert result["status"] == "ok"
    # The file is filtered out — only the folder is returned.
    assert result["data"]["count"] == 1
    assert result["data"]["folders"][0]["name"] == "Reports"
    assert result["data"]["folders"][0]["web_url"] == "https://sp/Reports"


@pytest.mark.asyncio
async def test_list_sharepoint_folders_error_on_403() -> None:
    folder_url = "https://contoso.sharepoint.com/sites/Finance/Missing"
    share_id = mft._encode_share_url(folder_url)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GRAPH}/shares/{share_id}/driveItem/children").mock(
            return_value=httpx.Response(403, json={"error": "forbidden"})
        )
        result = await mft.m365_list_sharepoint_folders(
            folder_url=folder_url, access_token=TOKEN, permissions=PERMS
        )

    assert result["status"] == "error"
    assert "403" in (result["error"] or "")


# ---------------------------------------------------------------------------
# m365_search_teams_chat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_teams_chat_happy_path() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GRAPH}/me/chats").mock(
            return_value=httpx.Response(200, json={"value": [{"id": "chat1"}]})
        )
        mock.get(f"{GRAPH}/chats/chat1/messages").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "m1",
                            "body": {"content": "Quarterly <b>budget</b> review"},
                            "from": {"user": {"displayName": "Alice"}},
                            "createdDateTime": "2026-06-01T09:00:00Z",
                            "webUrl": "https://teams/m1",
                        },
                        {
                            "id": "m2",
                            "body": {"content": "Lunch?"},
                            "from": {"user": {"displayName": "Bob"}},
                            "createdDateTime": "2026-06-01T12:00:00Z",
                        },
                    ]
                },
            )
        )
        result = await mft.m365_search_teams_chat(
            query="budget", access_token=TOKEN, permissions=PERMS
        )

    assert result["status"] == "ok"
    # Only the message containing "budget" matches (client-side filter).
    assert result["data"]["count"] == 1
    msg = result["data"]["messages"][0]
    assert msg["message_id"] == "m1"
    assert msg["from"] == "Alice"
    assert "budget" in msg["preview"].lower()


@pytest.mark.asyncio
async def test_search_teams_chat_error_on_403() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GRAPH}/me/chats").mock(
            return_value=httpx.Response(403, json={"error": "forbidden"})
        )
        result = await mft.m365_search_teams_chat(
            query="budget", access_token=TOKEN, permissions=PERMS
        )

    assert result["status"] == "error"
    assert "403" in (result["error"] or "")


# ---------------------------------------------------------------------------
# m365_create_teams_channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_teams_channel_happy_path() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{GRAPH}/teams/team1/channels").mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": "ch1",
                    "displayName": "Project X",
                    "webUrl": "https://teams/ch1",
                    "membershipType": "private",
                },
            )
        )
        result = await mft.m365_create_teams_channel(
            team_id="team1",
            channel_name="Project X",
            description="X work",
            is_private=True,
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["data"]["id"] == "ch1"
    assert result["data"]["membership_type"] == "private"
    body = route.calls[0].request.content.decode()
    assert "private" in body
    assert "X work" in body


@pytest.mark.asyncio
async def test_create_teams_channel_error_on_403() -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{GRAPH}/teams/team1/channels").mock(
            return_value=httpx.Response(403, json={"error": "forbidden"})
        )
        result = await mft.m365_create_teams_channel(
            team_id="team1",
            channel_name="Project X",
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "error"
    assert "403" in (result["error"] or "")


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_token_returns_error() -> None:
    result = await mft.m365_list_mail_folders(permissions=PERMS)
    assert result["status"] == "error"
    assert result["error"] == "No M365 access token available — please reconnect via OAuth"
