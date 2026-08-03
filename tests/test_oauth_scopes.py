"""Scope-consistency and refresh-failure tests (Gate 3, stages 1-3).

Two defects are locked down here:

1. THE SCOPE INVARIANT. The authorize request must cover every scope the refresh
   asks for. Before Gate 3 it did not — consent covered ``User.Read`` while the
   refresh asked for seven more — so every refresh returned AADSTS65001, which was
   swallowed into a silent fallback to the narrower token and surfaced only as an
   unexplained Graph 403. This invariant is the thing future staging steps will
   break; it belongs in the suite, not in a comment.

2. THE BLANKET EXCEPT. ``_maybe_refresh_token`` caught
   ``(httpx.HTTPError, ValueError)`` in one branch and logged a single warning, so
   a permanent consent failure was indistinguishable from a transient 5xx. The
   consent case now clears the stored token and forces re-consent; everything else
   is logged distinctly and never swallowed into that message.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from agents.mcp import config as mcp_config
from agents.mcp import oauth
from agents.mcp.tools import m365_write_tools

REAL_USER_ID = "33333333-3333-3333-3333-333333333333"
AZURE_TENANT = "azure-tid-scopes"
TOKEN_URL = f"https://login.microsoftonline.com/{AZURE_TENANT}/oauth2/v2.0/token"

# Scopes deferred to stages 4-5. Each requires tenant admin consent, so none may
# appear in the stage 1-3 sets. Sites.ReadWrite.All additionally has no call site.
_DEFERRED_SCOPES = ("Files.ReadWrite.All", "Sites.ReadWrite.All", "Channel.Create")

# Reserved OIDC scopes: valid in the authorize redirect, rejected by MSAL.
_RESERVED_SCOPES = ("openid", "profile", "email", "offline_access")


def _set_azure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-scopes")
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "secret-scopes")
    monkeypatch.setenv("AZURE_TENANT_ID", AZURE_TENANT)
    mcp_config.get_settings.cache_clear()


def _stored_record(
    access_token: str,
    refresh_token: str,
    expires_at: datetime,
    scope: str | None = None,
) -> dict:
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "scope": scope,
    }


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 1. The scope invariant
# ---------------------------------------------------------------------------


def test_refresh_scope_is_subset_of_authorize_scope() -> None:
    """THE invariant. A refresh can only return what consent covered.

    Any future staging step that widens _REFRESH_SCOPE without widening
    MS_REDIRECT_SCOPE must fail here rather than in production as a Graph 403.
    """

    refresh = set(m365_write_tools._REFRESH_SCOPE.split()) - {"offline_access"}
    authorize = set(oauth.MS_REDIRECT_SCOPE.split())

    missing = refresh - authorize
    assert not missing, (
        "These scopes are requested on refresh but never consented at authorize "
        f"time: {sorted(missing)}. Add them to oauth._GRAPH_DELEGATED_SCOPES and "
        "grant the matching delegated permission on the Azure app registration."
    )


def test_msal_scopes_match_the_graph_scope_list() -> None:
    """The token exchange must ask for the same Graph scopes as the redirect."""

    assert set(oauth.MSAL_SCOPES) == set(oauth._GRAPH_DELEGATED_SCOPES)


def test_msal_scopes_exclude_reserved_oidc_scopes() -> None:
    """MSAL injects the reserved scopes itself and rejects them if passed."""

    for reserved in _RESERVED_SCOPES:
        assert reserved not in oauth.MSAL_SCOPES


def test_authorize_scope_includes_reserved_oidc_scopes() -> None:
    """The raw redirect must carry them — MSAL is not involved at that step."""

    redirect = set(oauth.MS_REDIRECT_SCOPE.split())
    for reserved in _RESERVED_SCOPES:
        assert reserved in redirect


def test_refresh_scope_requests_offline_access() -> None:
    """Without offline_access Microsoft stops issuing refresh tokens."""

    assert "offline_access" in m365_write_tools._REFRESH_SCOPE.split()


@pytest.mark.parametrize("scope", _DEFERRED_SCOPES)
def test_deferred_admin_consent_scopes_are_absent(scope: str) -> None:
    """Stages 4-5 stay out of stages 1-3.

    Each of these needs a tenant administrator. Adding one silently would turn a
    user-consent rollout into an admin-consent one.
    """

    assert scope not in oauth.MS_REDIRECT_SCOPE.split()
    assert scope not in oauth.MSAL_SCOPES
    assert scope not in m365_write_tools._REFRESH_SCOPE.split()


def test_stage_1_to_3_scope_set_is_exactly_as_agreed() -> None:
    """Pin the agreed stage 1-3 set so a drive-by edit is visible in review."""

    assert oauth._GRAPH_DELEGATED_SCOPES == [
        "User.Read",
        "Mail.ReadWrite",
        "Mail.Send",
        "Calendars.ReadWrite",
        "ChannelMessage.Send",
        "Chat.ReadWrite",
    ]


# ---------------------------------------------------------------------------
# 2. _missing_scopes — the reader that makes a narrow grant visible
# ---------------------------------------------------------------------------


def test_missing_scopes_detects_a_narrow_grant() -> None:
    missing = m365_write_tools._missing_scopes("openid profile User.Read")
    assert "mail.readwrite" in [m.lower() for m in missing]
    assert "chat.readwrite" in [m.lower() for m in missing]


def test_missing_scopes_empty_for_a_full_grant() -> None:
    assert m365_write_tools._missing_scopes(m365_write_tools._REFRESH_SCOPE) == []


def test_missing_scopes_is_case_insensitive() -> None:
    """Microsoft echoes scopes back with its own casing."""

    granted = m365_write_tools._REFRESH_SCOPE.lower()
    assert m365_write_tools._missing_scopes(granted) == []


def test_missing_scopes_treats_unknown_as_not_narrow() -> None:
    """A legacy row with no scope tells us nothing — do not cry wolf on it."""

    assert m365_write_tools._missing_scopes(None) == []
    assert m365_write_tools._missing_scopes("") == []
    assert m365_write_tools._missing_scopes("   ") == []


def test_missing_scopes_ignores_offline_access() -> None:
    """offline_access is not always echoed even when a refresh token was issued."""

    granted = " ".join(
        s for s in m365_write_tools._REFRESH_SCOPE.split() if s != "offline_access"
    )
    assert m365_write_tools._missing_scopes(granted) == []


# ---------------------------------------------------------------------------
# 3. The AADSTS65001 / invalid_grant branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"error": "invalid_grant", "error_description": "AADSTS65001: The user or "
         "administrator has not consented to use the application."},
        {"error": "invalid_grant"},
        {"error": "consent_required", "error_description": "AADSTS65001: no consent"},
    ],
    ids=["both", "invalid_grant_only", "aadsts65001_only"],
)
async def test_consent_failure_clears_token_and_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    body: dict,
) -> None:
    """A consent failure is permanent: clear the grant, do not keep retrying."""

    _set_azure_env(monkeypatch)
    past = datetime.now(UTC) - timedelta(minutes=1)
    cleared: list[str] = []

    async def fake_clear(user_id: str) -> None:
        cleared.append(user_id)

    monkeypatch.setattr(m365_write_tools, "_clear_user_token", fake_clear)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(TOKEN_URL).mock(return_value=httpx.Response(400, json=body))
        with caplog.at_level(logging.ERROR, logger=m365_write_tools.log.name):
            result = await m365_write_tools._maybe_refresh_token(
                REAL_USER_ID,
                _stored_record("old-access", "rt-1", past),
            )

    assert result is None
    assert cleared == [REAL_USER_ID], "the stale grant must be cleared"
    assert any(
        "m365_token_scope_consent_required" in r.message for r in caplog.records
    ), "the consent failure must be logged at ERROR under its own event name"
    assert all(r.levelno >= logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_consent_failure_log_names_the_required_scope(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The operator must be able to see WHAT was required, not just that it failed."""

    _set_azure_env(monkeypatch)
    past = datetime.now(UTC) - timedelta(minutes=1)

    async def fake_clear(user_id: str) -> None:
        return None

    monkeypatch.setattr(m365_write_tools, "_clear_user_token", fake_clear)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        with caplog.at_level(logging.ERROR, logger=m365_write_tools.log.name):
            await m365_write_tools._maybe_refresh_token(
                REAL_USER_ID, _stored_record("old-access", "rt-1", past)
            )

    record = next(
        r for r in caplog.records if "m365_token_scope_consent_required" in r.message
    )
    assert getattr(record, "required_scope", None) == m365_write_tools._REFRESH_SCOPE


# ---------------------------------------------------------------------------
# 4. Non-consent errors are NOT swallowed into the same branch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_non_consent_http_error_is_distinct_and_does_not_clear(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    """A transient status must not be reported as, or treated as, a consent failure."""

    _set_azure_env(monkeypatch)
    past = datetime.now(UTC) - timedelta(minutes=1)
    cleared: list[str] = []

    async def fake_clear(user_id: str) -> None:
        cleared.append(user_id)

    monkeypatch.setattr(m365_write_tools, "_clear_user_token", fake_clear)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=httpx.Response(status, json={"error": "temporarily_unavailable"})
        )
        with caplog.at_level(logging.DEBUG, logger=m365_write_tools.log.name):
            result = await m365_write_tools._maybe_refresh_token(
                REAL_USER_ID, _stored_record("old-access", "rt-1", past)
            )

    assert result is None
    # The grant is still good — a 5xx says nothing about consent.
    assert cleared == [], "a transient failure must NOT clear the stored grant"
    messages = " ".join(r.message for r in caplog.records)
    assert "m365_token_refresh_http_error" in messages
    assert "m365_token_scope_consent_required" not in messages
    # Logged, not silently dropped.
    assert any(r.levelno >= logging.ERROR for r in caplog.records)
    status_codes = [getattr(r, "status_code", None) for r in caplog.records]
    assert status in status_codes, "the actual status must be diagnosable from the log"


@pytest.mark.asyncio
async def test_transport_error_is_its_own_branch(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No response to inspect — must not be mistaken for a consent failure."""

    _set_azure_env(monkeypatch)
    past = datetime.now(UTC) - timedelta(minutes=1)
    cleared: list[str] = []

    async def fake_clear(user_id: str) -> None:
        cleared.append(user_id)

    monkeypatch.setattr(m365_write_tools, "_clear_user_token", fake_clear)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("dns failure"))
        with caplog.at_level(logging.DEBUG, logger=m365_write_tools.log.name):
            result = await m365_write_tools._maybe_refresh_token(
                REAL_USER_ID, _stored_record("old-access", "rt-1", past)
            )

    assert result is None
    assert cleared == []
    messages = " ".join(r.message for r in caplog.records)
    assert "m365_token_refresh_transport_error" in messages
    assert "m365_token_scope_consent_required" not in messages


@pytest.mark.asyncio
async def test_malformed_json_is_its_own_branch(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 2xx with a non-JSON body is a malformed response, not a rejected request."""

    _set_azure_env(monkeypatch)
    past = datetime.now(UTC) - timedelta(minutes=1)
    cleared: list[str] = []

    async def fake_clear(user_id: str) -> None:
        cleared.append(user_id)

    monkeypatch.setattr(m365_write_tools, "_clear_user_token", fake_clear)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, content=b"<html>gateway</html>")
        )
        with caplog.at_level(logging.DEBUG, logger=m365_write_tools.log.name):
            result = await m365_write_tools._maybe_refresh_token(
                REAL_USER_ID, _stored_record("old-access", "rt-1", past)
            )

    assert result is None
    assert cleared == []
    messages = " ".join(r.message for r in caplog.records)
    assert "m365_token_refresh_malformed_response" in messages
    assert "m365_token_scope_consent_required" not in messages


# ---------------------------------------------------------------------------
# 5. A cleared token forces the reconnect message on the NEXT call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleared_token_yields_no_token_message_on_next_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a clear, the next call resolves no token and callers surface
    _NO_TOKEN_MESSAGE — the user is driven back through /authorize instead of
    retrying a doomed refresh forever."""

    _set_azure_env(monkeypatch)

    # Simulate the post-clear row: present, but every token column NULL.
    async def cleared_record(user_id: str) -> dict:
        return _stored_record(None, None, None, None)  # type: ignore[arg-type]

    async def no_bare_token(user_id: str) -> None:
        return None

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token_record", cleared_record)
    monkeypatch.setattr(m365_write_tools, "_lookup_user_token", no_bare_token)

    with respx.mock:  # no routes → asserts the token endpoint is never called
        token = await m365_write_tools._get_m365_token(None, REAL_USER_ID, None)

    assert token is None


@pytest.mark.asyncio
async def test_no_token_resolves_to_the_reconnect_error_through_a_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a cleared grant surfaces the standard reconnect message."""

    _set_azure_env(monkeypatch)

    async def cleared_record(user_id: str) -> dict:
        return _stored_record(None, None, None, None)  # type: ignore[arg-type]

    async def no_bare_token(user_id: str) -> None:
        return None

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token_record", cleared_record)
    monkeypatch.setattr(m365_write_tools, "_lookup_user_token", no_bare_token)
    monkeypatch.setattr(m365_write_tools, "current_user_id", lambda: REAL_USER_ID)

    result = await m365_write_tools.m365_send_email(
        to=["someone@example.com"],
        subject="s",
        body="b",
        permissions=["m365_access"],
    )

    assert result["status"] == "error"
    assert result["error"] == m365_write_tools._NO_TOKEN_MESSAGE


@pytest.mark.asyncio
async def test_clear_user_token_issues_the_null_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder: list = []

    class _FakeConn:
        async def execute(self, query: str, *args) -> None:
            recorder.append((query, args))

    class _FakeConnCtx:
        async def __aenter__(self) -> _FakeConn:
            return _FakeConn()

        async def __aexit__(self, *exc) -> bool:
            return False

    monkeypatch.setattr(m365_write_tools, "get_conn", lambda: _FakeConnCtx())

    await m365_write_tools._clear_user_token(REAL_USER_ID)

    assert len(recorder) == 1
    query, args = recorder[0]
    assert "UPDATE user_tokens" in query
    assert "access_token = NULL" in query
    assert "refresh_token = NULL" in query
    assert "scope = NULL" in query
    assert args == (REAL_USER_ID,)


@pytest.mark.asyncio
async def test_clear_user_token_swallows_db_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed clear must not break the in-flight tool call."""

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(m365_write_tools, "get_conn", boom)
    await m365_write_tools._clear_user_token(REAL_USER_ID)  # must not raise


# ---------------------------------------------------------------------------
# 6. The stored grant is actually READ
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_narrow_stored_grant_is_logged_on_use(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The scope column was write-once-never-read; this is the read."""

    _set_azure_env(monkeypatch)
    fresh = datetime.now(UTC) + timedelta(hours=1)

    async def narrow_record(user_id: str) -> dict:
        # The pre-Gate-3 grant: User.Read only.
        return _stored_record("tok", "rt", fresh, "openid profile User.Read")

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token_record", narrow_record)

    # respx.mock with no routes asserts the token endpoint is never called; the
    # stored token is fresh, so no refresh should be attempted.
    with respx.mock, caplog.at_level(logging.ERROR, logger=m365_write_tools.log.name):
        token = await m365_write_tools._get_m365_token(None, REAL_USER_ID, None)

    assert token == "tok"  # still returned — logging must not break the call
    record = next(
        r
        for r in caplog.records
        if "m365_stored_grant_narrower_than_required" in r.message
    )
    missing = getattr(record, "missing_scopes", "")
    assert "Mail.ReadWrite".lower() in missing.lower()


@pytest.mark.asyncio
async def test_full_stored_grant_logs_nothing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_azure_env(monkeypatch)
    fresh = datetime.now(UTC) + timedelta(hours=1)

    async def full_record(user_id: str) -> dict:
        return _stored_record("tok", "rt", fresh, m365_write_tools._REFRESH_SCOPE)

    monkeypatch.setattr(m365_write_tools, "_lookup_user_token_record", full_record)

    with respx.mock, caplog.at_level(logging.ERROR, logger=m365_write_tools.log.name):
        await m365_write_tools._get_m365_token(None, REAL_USER_ID, None)

    assert not [
        r
        for r in caplog.records
        if "m365_stored_grant_narrower_than_required" in r.message
    ]


@pytest.mark.asyncio
async def test_refresh_granting_fewer_scopes_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 2xx can still grant a subset. That downgrade must not be silent."""

    _set_azure_env(monkeypatch)
    past = datetime.now(UTC) - timedelta(minutes=1)
    persisted: dict = {}

    async def fake_persist(
        user_id, access_token, refresh_token, expires_in, scope=None
    ) -> None:
        persisted.update(scope=scope)

    monkeypatch.setattr(m365_write_tools, "_persist_refreshed_token", fake_persist)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "rt-2",
                    "expires_in": 3600,
                    "scope": "User.Read Mail.ReadWrite",  # narrower than requested
                },
            )
        )
        with caplog.at_level(logging.ERROR, logger=m365_write_tools.log.name):
            token = await m365_write_tools._maybe_refresh_token(
                REAL_USER_ID, _stored_record("old", "rt-1", past)
            )

    assert token == "new-access"  # the call still proceeds
    assert persisted["scope"] == "User.Read Mail.ReadWrite"
    record = next(
        r for r in caplog.records if "m365_token_scope_narrowed" in r.message
    )
    missing = getattr(record, "missing_scopes", "").lower()
    assert "mail.send" in missing
    assert "chat.readwrite" in missing
