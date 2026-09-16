from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

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
# All mailbox Graph calls target /users/{upn}/... — never /me/.
UPN = "user@nichegroup.africa"
USERS = f"/users/{UPN}"

# Per-tenant Microsoft token endpoint for the configured AZURE_TENANT_ID below.
AZURE_TENANT = "azure-tid-test"
TOKEN_URL = f"https://login.microsoftonline.com/{AZURE_TENANT}/oauth2/v2.0/token"


def _set_azure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the Azure app credentials the refresh path reads from env."""

    monkeypatch.setenv("AZURE_CLIENT_ID", "client-123")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret-xyz")
    monkeypatch.setenv("AZURE_TENANT_ID", AZURE_TENANT)
    mcp_config.get_settings.cache_clear()


def _stored_record(access_token: str, refresh_token: str, expires_at: datetime) -> dict:
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
    }


class _FakeConn:
    def __init__(self, recorder: list) -> None:
        self._recorder = recorder

    async def execute(self, query: str, *args) -> None:
        self._recorder.append((query, args))


class _FakeConnCtx:
    def __init__(self, recorder: list) -> None:
        self._recorder = recorder

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._recorder)

    async def __aexit__(self, *exc) -> bool:
        return False


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


@pytest.fixture
def upn(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make `_get_upn` resolve a known UPN so calls target /users/{upn}/....

    Patched in both the write module and the read module (`m365_tools`), which
    binds its own `_get_upn` reference at import time.
    """

    async def fake_upn(user_id: str | None = None) -> str:
        return UPN

    monkeypatch.setattr(m365_write_tools, "_get_upn", fake_upn)
    monkeypatch.setattr(m365_tools, "_get_upn", fake_upn)
    return UPN


# ---------------------------------------------------------------------------
# m365_send_email
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_email_happy_path(upn: str) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{GRAPH}{USERS}/sendMail").mock(
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
async def test_send_email_error_on_403(upn: str) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{GRAPH}{USERS}/sendMail").mock(
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


@pytest.mark.asyncio
async def test_send_email_reply_uses_native_graph_reply_endpoint(upn: str) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{GRAPH}{USERS}/messages/original-1/reply").mock(
            return_value=httpx.Response(202)
        )
        result = await m365_write_tools.m365_send_email(
            to=["a@nichegroup.africa"],
            subject="Re: Hello",
            body="Reply body",
            cc=["b@nichegroup.africa"],
            in_reply_to_uri="mail:///messages/original-1",
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert route.calls[0].request.url.path.endswith("/messages/original-1/reply")
    payload = json.loads(route.calls[0].request.content)
    assert payload["comment"] == "Reply body"
    assert payload["message"]["toRecipients"][0]["emailAddress"]["address"] == (
        "a@nichegroup.africa"
    )
    assert "internetMessageHeaders" not in payload["message"]


# ---------------------------------------------------------------------------
# m365_create_calendar_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_calendar_event_happy_path(upn: str) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{GRAPH}{USERS}/events").mock(
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
async def test_create_calendar_event_error_on_400(upn: str) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{GRAPH}{USERS}/events").mock(
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
# m365_download_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_file_happy_path() -> None:
    file_url = "https://contoso.sharepoint.com/sites/Finance/Shared%20Documents/rfq.pdf"
    share_id = m365_write_tools._encode_share_url(file_url)
    payload = b"%PDF-1.7 compliance doc bytes"

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GRAPH}/shares/{share_id}/driveItem").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "item1",
                    "name": "rfq.pdf",
                    "file": {"mimeType": "application/pdf"},
                    "size": len(payload),
                    "parentReference": {"driveId": "drive1"},
                },
            )
        )
        mock.get(f"{GRAPH}/drives/drive1/items/item1/content").mock(
            return_value=httpx.Response(200, content=payload)
        )
        result = await m365_write_tools.m365_download_file(
            file_url=file_url,
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["data"]["filename"] == "rfq.pdf"
    assert result["data"]["mime_type"] == "application/pdf"
    assert result["data"]["size_bytes"] == len(payload)
    import base64 as _b64

    assert _b64.b64decode(result["data"]["content_b64"]) == payload


@pytest.mark.asyncio
async def test_download_file_falls_back_to_content_type_header() -> None:
    file_url = "https://contoso.sharepoint.com/sites/Finance/note.bin"
    share_id = m365_write_tools._encode_share_url(file_url)

    with respx.mock(assert_all_called=True) as mock:
        # No `file` facet in the resolve response → mime falls back to the
        # download response's Content-Type header.
        mock.get(f"{GRAPH}/shares/{share_id}/driveItem").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "item1",
                    "name": "note.bin",
                    "parentReference": {"driveId": "drive1"},
                },
            )
        )
        mock.get(f"{GRAPH}/drives/drive1/items/item1/content").mock(
            return_value=httpx.Response(
                200, content=b"abc", headers={"Content-Type": "text/plain"}
            )
        )
        result = await m365_write_tools.m365_download_file(
            file_url=file_url,
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["data"]["mime_type"] == "text/plain"


@pytest.mark.asyncio
async def test_download_file_error_on_resolve_404() -> None:
    file_url = "https://contoso.sharepoint.com/sites/Finance/missing.pdf"
    share_id = m365_write_tools._encode_share_url(file_url)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GRAPH}/shares/{share_id}/driveItem").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )
        result = await m365_write_tools.m365_download_file(
            file_url=file_url,
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "error"
    assert "404" in (result["error"] or "")


@pytest.mark.asyncio
async def test_download_file_error_when_drive_unresolvable() -> None:
    file_url = "https://contoso.sharepoint.com/sites/Finance/odd.pdf"
    share_id = m365_write_tools._encode_share_url(file_url)

    with respx.mock(assert_all_called=True) as mock:
        # Resolve succeeds but yields no driveId → cannot build the content path.
        mock.get(f"{GRAPH}/shares/{share_id}/driveItem").mock(
            return_value=httpx.Response(200, json={"id": "item1", "name": "odd.pdf"})
        )
        result = await m365_write_tools.m365_download_file(
            file_url=file_url,
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "error"
    assert "Could not resolve file" in (result["error"] or "")


# ---------------------------------------------------------------------------
# m365_upload_base64_to_sharepoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_base64_happy_path() -> None:
    import base64 as _b64

    folder_url = "https://contoso.sharepoint.com/sites/Finance/Shared%20Documents/Submissions"
    share_id = m365_write_tools._encode_share_url(folder_url)
    raw = b"%PDF compiled submission"
    content_b64 = _b64.b64encode(raw).decode("ascii")

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GRAPH}/shares/{share_id}/driveItem").mock(
            return_value=httpx.Response(
                200,
                json={"id": "folder1", "parentReference": {"driveId": "drive1"}},
            )
        )
        route = mock.route(
            method="PUT",
            url__regex=r"https://graph\.microsoft\.com/v1\.0/drives/drive1/items/folder1.*content",
        ).mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": "file1",
                    "name": "submission.pdf",
                    "size": len(raw),
                    "webUrl": "https://sp/submission.pdf",
                },
            )
        )
        result = await m365_write_tools.m365_upload_base64_to_sharepoint(
            folder_url=folder_url,
            filename="submission.pdf",
            content_b64=content_b64,
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert result["data"]["file_url"] == "https://sp/submission.pdf"
    assert result["data"]["filename"] == "submission.pdf"
    assert result["data"]["size_bytes"] == len(raw)
    # The decoded bytes — not the base64 text — are what gets PUT.
    assert route.calls[0].request.content == raw


@pytest.mark.asyncio
async def test_upload_base64_rejects_invalid_base64() -> None:
    result = await m365_write_tools.m365_upload_base64_to_sharepoint(
        folder_url="https://contoso.sharepoint.com/sites/Finance/Docs",
        filename="x.pdf",
        content_b64="not!!valid!!base64",
        access_token=TOKEN,
        permissions=PERMS,
    )
    assert result["status"] == "error"
    assert "base64" in (result["error"] or "")


@pytest.mark.asyncio
async def test_upload_base64_error_on_resolve_403() -> None:
    import base64 as _b64

    folder_url = "https://contoso.sharepoint.com/sites/Finance/Locked"
    share_id = m365_write_tools._encode_share_url(folder_url)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{GRAPH}/shares/{share_id}/driveItem").mock(
            return_value=httpx.Response(403, json={"error": "forbidden"})
        )
        result = await m365_write_tools.m365_upload_base64_to_sharepoint(
            folder_url=folder_url,
            filename="x.pdf",
            content_b64=_b64.b64encode(b"data").decode("ascii"),
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "error"
    assert "403" in (result["error"] or "")


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
async def test_send_email_uses_stored_token(
    monkeypatch: pytest.MonkeyPatch, upn: str
) -> None:
    async def fake_lookup(user_id: str) -> str:
        return "stored-graph-token"

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token", fake_lookup)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{GRAPH}{USERS}/sendMail").mock(
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
async def test_read_tool_uses_stored_token(
    monkeypatch: pytest.MonkeyPatch, upn: str
) -> None:
    async def fake_lookup(user_id: str) -> str:
        return "stored-graph-token"

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token", fake_lookup)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{GRAPH}{USERS}/messages").mock(
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
async def test_token_not_in_logs(
    caplog: pytest.LogCaptureFixture, upn: str
) -> None:
    caplog.set_level(logging.DEBUG, logger="agents.mcp.tools.m365_write_tools")
    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{GRAPH}{USERS}/sendMail").mock(return_value=httpx.Response(202))
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


# ---------------------------------------------------------------------------
# Auto-refresh of the stored M365 token (#4D)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proactive_refresh_on_near_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_azure_env(monkeypatch)
    near = datetime.now(UTC) + timedelta(minutes=2)  # inside the 5-minute skew

    async def fake_record(user_id: str) -> dict:
        return _stored_record("old-access", "rt-1", near)

    persisted: dict = {}

    async def fake_persist(
        user_id, access_token, refresh_token, expires_in, scope=None
    ) -> None:
        persisted.update(
            user_id=user_id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=expires_in,
            scope=scope,
        )

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token_record", fake_record)
    monkeypatch.setattr(m365_write_tools, "_persist_refreshed_token", fake_persist)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "rt-2",
                    "expires_in": 3600,
                },
            )
        )
        token = await m365_write_tools._get_m365_token(None, REAL_USER_ID, None)

    assert token == "new-access"
    body = route.calls[0].request.content.decode()
    assert "grant_type=refresh_token" in body
    assert "refresh_token=rt-1" in body
    assert "client_id=client-123" in body
    assert persisted == {
        "user_id": REAL_USER_ID,
        "access_token": "new-access",
        "refresh_token": "rt-2",
        "expires_in": 3600,
        # Absent from this token response, so nothing overwrites the stored scope.
        "scope": None,
    }


@pytest.mark.asyncio
async def test_refresh_on_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_azure_env(monkeypatch)
    past = datetime.now(UTC) - timedelta(minutes=1)

    async def fake_record(user_id: str) -> dict:
        return _stored_record("old-access", "rt-1", past)

    async def fake_persist(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token_record", fake_record)
    monkeypatch.setattr(m365_write_tools, "_persist_refreshed_token", fake_persist)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "new-access"})
        )
        token = await m365_write_tools._get_m365_token(None, REAL_USER_ID, None)

    assert token == "new-access"


@pytest.mark.asyncio
async def test_no_refresh_when_token_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_azure_env(monkeypatch)
    fresh = datetime.now(UTC) + timedelta(hours=1)

    async def fake_record(user_id: str) -> dict:
        return _stored_record("still-good", "rt-1", fresh)

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token_record", fake_record)

    # assert_all_called defaults True with no routes registered → asserts that
    # the token endpoint is never hit for a still-valid token.
    with respx.mock:
        token = await m365_write_tools._get_m365_token(None, REAL_USER_ID, None)

    assert token == "still-good"


@pytest.mark.asyncio
async def test_refresh_failure_falls_back_to_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_azure_env(monkeypatch)
    past = datetime.now(UTC) - timedelta(minutes=1)

    async def fake_record(user_id: str) -> dict:
        return _stored_record("old-access", "rt-1", past)

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token_record", fake_record)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        token = await m365_write_tools._get_m365_token(None, REAL_USER_ID, None)

    # Best effort: a failed refresh must not break the call — keep the old token.
    assert token == "old-access"


@pytest.mark.asyncio
async def test_persist_refreshed_token_executes_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder: list = []
    monkeypatch.setattr(m365_write_tools, "get_conn", lambda: _FakeConnCtx(recorder))

    await m365_write_tools._persist_refreshed_token(
        REAL_USER_ID, "new-access", "rt-2", 3600, "Mail.Read Mail.Send"
    )

    assert len(recorder) == 1
    query, args = recorder[0]
    assert "UPDATE user_tokens" in query
    assert args[0] == "new-access"
    assert args[1] == "rt-2"
    assert isinstance(args[2], datetime)
    # The granted scope is persisted (Gate 3): the column was previously
    # write-once-never-read, which made a narrowed grant undetectable.
    assert args[3] == "Mail.Read Mail.Send"
    assert "scope = COALESCE($4, scope)" in query
    assert args[4] == REAL_USER_ID


@pytest.mark.asyncio
async def test_persist_refreshed_token_keeps_existing_scope_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token response without a scope field must not blank the stored scope."""

    recorder: list = []
    monkeypatch.setattr(m365_write_tools, "get_conn", lambda: _FakeConnCtx(recorder))

    await m365_write_tools._persist_refreshed_token(
        REAL_USER_ID, "new-access", "rt-2", 3600
    )

    _query, args = recorder[0]
    assert args[3] is None  # COALESCE leaves the stored value in place


@pytest.mark.asyncio
async def test_persist_refreshed_token_swallows_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(m365_write_tools, "get_conn", boom)
    # Must not raise.
    await m365_write_tools._persist_refreshed_token(
        REAL_USER_ID, "new-access", "rt-2", 3600
    )


@pytest.mark.asyncio
async def test_send_email_retries_once_on_401(
    monkeypatch: pytest.MonkeyPatch, upn: str
) -> None:
    _set_azure_env(monkeypatch)
    fresh = datetime.now(UTC) + timedelta(hours=1)

    async def fake_record(user_id: str) -> dict:
        return _stored_record("old-access", "rt-1", fresh)

    async def fake_persist(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token_record", fake_record)
    monkeypatch.setattr(m365_write_tools, "_persist_refreshed_token", fake_persist)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "new-access"})
        )
        mail = mock.post(f"{GRAPH}{USERS}/sendMail").mock(
            side_effect=[
                httpx.Response(401, json={"error": "InvalidAuthenticationToken"}),
                httpx.Response(202),
            ]
        )
        result = await m365_write_tools.m365_send_email(
            to=["a@nichegroup.africa"],
            subject="s",
            body="b",
            user_id=REAL_USER_ID,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    assert mail.call_count == 2
    # First attempt used the stale token; the retry used the refreshed one.
    assert mail.calls[0].request.headers["Authorization"] == "Bearer old-access"
    assert mail.calls[1].request.headers["Authorization"] == "Bearer new-access"


@pytest.mark.asyncio
async def test_send_email_stops_after_one_retry_on_repeated_401(
    monkeypatch: pytest.MonkeyPatch, upn: str
) -> None:
    _set_azure_env(monkeypatch)
    fresh = datetime.now(UTC) + timedelta(hours=1)

    async def fake_record(user_id: str) -> dict:
        return _stored_record("old-access", "rt-1", fresh)

    async def fake_persist(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token_record", fake_record)
    monkeypatch.setattr(m365_write_tools, "_persist_refreshed_token", fake_persist)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "new-access"})
        )
        mail = mock.post(f"{GRAPH}{USERS}/sendMail").mock(
            return_value=httpx.Response(401, json={"error": "InvalidAuthenticationToken"})
        )
        result = await m365_write_tools.m365_send_email(
            to=["a@nichegroup.africa"],
            subject="s",
            body="b",
            user_id=REAL_USER_ID,
            permissions=PERMS,
        )

    assert result["status"] == "error"
    assert "401" in (result["error"] or "")
    # Original call + exactly one retry — never loops.
    assert mail.call_count == 2


# ---------------------------------------------------------------------------
# _get_upn helper — tenant-scoped UPN resolution, fail-closed (#8B, c-i + c-iii)
#
# `_get_upn` never returns None and never falls back to /me/. Every resolution
# failure raises M365IdentityError carrying a distinct `m365_upn_*` code, and
# the users lookup is scoped to the request's tenant so a valid user UUID from
# another tenant cannot resolve a mailbox.
# ---------------------------------------------------------------------------

TENANT_ROW_ID = "33333333-3333-3333-3333-333333333333"
OTHER_TENANT_ROW_ID = "44444444-4444-4444-4444-444444444444"


class _FakeRowConn:
    """Fake conn keyed on (user_id, tenant_id), asserting the tenant predicate.

    A key miss returns None exactly as asyncpg does, which is what the
    cross-tenant rejection test relies on.
    """

    def __init__(self, rows: dict[tuple[str, str], dict | None] | None = None) -> None:
        self._rows = rows or {}
        self.calls: list[tuple] = []

    async def fetchrow(self, query: str, *args) -> dict | None:
        assert "SELECT email FROM users" in query
        # The tenant predicate must be present — this is the (c-iii) guard.
        assert "tenant_id::text = $2" in query
        self.calls.append(args)
        return self._rows.get((args[0], args[1]))


class _FakeRowConnCtx:
    def __init__(self, conn: _FakeRowConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeRowConn:
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


@contextmanager
def _identity(user_id: str | None, tenant_row_id: str | None):
    """Publish a user + tenant on the request context vars, then restore both."""

    user_token = mcp_tenant.set_current_user_id(user_id)
    tenant = {"id": tenant_row_id, "is_active": True} if tenant_row_id else None
    tenant_token = mcp_tenant.set_current_tenant(tenant)
    try:
        yield
    finally:
        mcp_tenant.reset_current_tenant(tenant_token)
        mcp_tenant.reset_current_user_id(user_token)


def _conn_with(
    monkeypatch: pytest.MonkeyPatch, rows: dict[tuple[str, str], dict | None]
) -> _FakeRowConn:
    conn = _FakeRowConn(rows)
    monkeypatch.setattr(m365_write_tools, "get_conn", lambda: _FakeRowConnCtx(conn))
    return conn


# --- happy path -------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_upn_returns_email_for_user_in_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _conn_with(monkeypatch, {(REAL_USER_ID, TENANT_ROW_ID): {"email": UPN}})
    with _identity(REAL_USER_ID, TENANT_ROW_ID):
        result = await m365_write_tools._get_upn("local-user")
    assert result == UPN
    # Lookup was tenant-scoped with both bind params, in order.
    assert conn.calls == [(REAL_USER_ID, TENANT_ROW_ID)]


@pytest.mark.asyncio
async def test_get_upn_uses_explicit_non_default_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No bearer identity; an explicit non-placeholder user_id is honoured. The
    # tenant still has to come from the request context.
    _conn_with(monkeypatch, {(REAL_USER_ID, TENANT_ROW_ID): {"email": UPN}})
    with _identity(None, TENANT_ROW_ID):
        result = await m365_write_tools._get_upn(REAL_USER_ID)
    assert result == UPN


# --- fail-closed paths, one per m365_upn_* code -----------------------------


@pytest.mark.asyncio
async def test_get_upn_raises_when_no_identity(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Path 1: placeholder user, no bearer identity → no DB lookup at all."""

    monkeypatch.setenv("MCP_DEFAULT_USER_ID", "local-user")
    mcp_config.get_settings.cache_clear()
    caplog.set_level(logging.WARNING, logger="agents.mcp.tools.m365_write_tools")
    with (
        _identity(None, TENANT_ROW_ID),
        pytest.raises(m365_write_tools.M365IdentityError) as exc,
    ):
        await m365_write_tools._get_upn("local-user")
    assert "m365_upn_no_identity" in str(exc.value)
    assert any("m365_upn_no_identity" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_get_upn_raises_when_no_tenant(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Path 2: user identity present but no tenant → lookup cannot be scoped."""

    caplog.set_level(logging.WARNING, logger="agents.mcp.tools.m365_write_tools")
    with (
        _identity(REAL_USER_ID, None),
        pytest.raises(m365_write_tools.M365IdentityError) as exc,
    ):
        await m365_write_tools._get_upn(REAL_USER_ID)
    assert "m365_upn_no_tenant" in str(exc.value)
    assert any("m365_upn_no_tenant" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_get_upn_raises_on_db_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Path 3: the lookup itself raises → terminal, not a fallback."""

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(m365_write_tools, "get_conn", boom)
    caplog.set_level(logging.WARNING, logger="agents.mcp.tools.m365_write_tools")
    with (
        _identity(REAL_USER_ID, TENANT_ROW_ID),
        pytest.raises(m365_write_tools.M365IdentityError) as exc,
    ):
        await m365_write_tools._get_upn(REAL_USER_ID)
    assert "m365_upn_lookup_failed" in str(exc.value)
    assert "RuntimeError" in str(exc.value)
    assert any("m365_upn_lookup_failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_get_upn_raises_when_row_not_found(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Path 4: no row for this (user_id, tenant_id) pair."""

    _conn_with(monkeypatch, {})
    caplog.set_level(logging.WARNING, logger="agents.mcp.tools.m365_write_tools")
    with (
        _identity(REAL_USER_ID, TENANT_ROW_ID),
        pytest.raises(m365_write_tools.M365IdentityError) as exc,
    ):
        await m365_write_tools._get_upn(REAL_USER_ID)
    assert "m365_upn_not_found" in str(exc.value)
    assert any("m365_upn_not_found" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_get_upn_raises_when_row_has_no_email(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Path 5: row exists but email is NULL — distinct from not-found."""

    _conn_with(monkeypatch, {(REAL_USER_ID, TENANT_ROW_ID): {"email": None}})
    caplog.set_level(logging.WARNING, logger="agents.mcp.tools.m365_write_tools")
    with (
        _identity(REAL_USER_ID, TENANT_ROW_ID),
        pytest.raises(m365_write_tools.M365IdentityError) as exc,
    ):
        await m365_write_tools._get_upn(REAL_USER_ID)
    assert "m365_upn_no_email" in str(exc.value)
    assert any("m365_upn_no_email" in r.getMessage() for r in caplog.records)


# --- cross-tenant rejection (c-iii) -----------------------------------------


@pytest.mark.asyncio
async def test_get_upn_rejects_user_guid_from_another_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid user UUID presented under the wrong tenant must not resolve.

    The row exists under TENANT_ROW_ID. Requesting it while the request context
    carries OTHER_TENANT_ROW_ID must miss, because the predicate binds both.
    Without the tenant clause this returned the other tenant's mailbox UPN.
    """

    conn = _conn_with(monkeypatch, {(REAL_USER_ID, TENANT_ROW_ID): {"email": UPN}})
    with (
        _identity(REAL_USER_ID, OTHER_TENANT_ROW_ID),
        pytest.raises(m365_write_tools.M365IdentityError) as exc,
    ):
        await m365_write_tools._get_upn(REAL_USER_ID)
    assert "m365_upn_not_found" in str(exc.value)
    assert conn.calls == [(REAL_USER_ID, OTHER_TENANT_ROW_ID)]


# --- /me/ is unreachable ----------------------------------------------------


def test_mailbox_base_refuses_empty_upn() -> None:
    """`_mailbox_base` can no longer construct /me under any input."""

    assert m365_write_tools._mailbox_base(UPN) == USERS
    for empty in ("", None):
        with pytest.raises(m365_write_tools.M365IdentityError) as exc:
            m365_write_tools._mailbox_base(empty)  # type: ignore[arg-type]
        assert "m365_upn_empty" in str(exc.value)


@pytest.mark.asyncio
async def test_tool_returns_error_envelope_and_makes_no_graph_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An identity failure surfaces as a normal error envelope, not a raise.

    Critically, no Graph request is attempted — the old code would have sent the
    call to /me/ using whichever mailbox owns the token.
    """

    _conn_with(monkeypatch, {})
    with respx.mock(assert_all_called=False) as mock:
        me = mock.post(f"{GRAPH}/me/sendMail").mock(return_value=httpx.Response(202))
        users = mock.post(f"{GRAPH}{USERS}/sendMail").mock(
            return_value=httpx.Response(202)
        )
        with _identity(REAL_USER_ID, TENANT_ROW_ID):
            result = await m365_write_tools.m365_send_email(
                to=["a@nichegroup.africa"],
                subject="s",
                body="b",
                user_id=REAL_USER_ID,
                access_token=TOKEN,
                permissions=PERMS,
            )

    assert result["status"] == "error"
    assert "m365_upn_not_found" in result["error"]
    assert me.call_count == 0
    assert users.call_count == 0


@pytest.mark.asyncio
async def test_no_me_in_graph_url_when_upn_available(upn: str) -> None:
    """When a UPN resolves, no Graph URL may contain /me/ — it must use /users/."""

    with respx.mock(assert_all_called=True) as mock:
        send = mock.post(f"{GRAPH}{USERS}/sendMail").mock(
            return_value=httpx.Response(202)
        )
        result = await m365_write_tools.m365_send_email(
            to=["a@nichegroup.africa"],
            subject="s",
            body="b",
            user_id=REAL_USER_ID,
            access_token=TOKEN,
            permissions=PERMS,
        )

    assert result["status"] == "ok"
    requested = str(send.calls[0].request.url)
    assert f"{USERS}/sendMail" in requested
    assert "/me/" not in requested
