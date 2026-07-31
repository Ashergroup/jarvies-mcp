"""Tests for portal signed-cookie sessions and CSRF (portal.sessions)."""

from __future__ import annotations

import time

import pytest

from agents.mcp import config as mcp_config
from portal import sessions

SECRET = "portal-token-secret-do-not-log"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIES_TOKEN_SECRET", SECRET)
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


class _FakeRequest:
    """Minimal stand-in exposing the ``.cookies`` mapping the module reads."""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}


def _request_with_session(payload: dict) -> tuple[_FakeRequest, str]:
    token = sessions.create_session_cookie(payload)
    return _FakeRequest({sessions.COOKIE_NAME: token}), token


# ---------------------------------------------------------------------------
# Session round-trip / expiry / tamper / kind
# ---------------------------------------------------------------------------


def test_session_round_trip() -> None:
    request, _ = _request_with_session(
        {"sub": "headspace", "kind": "headspace_admin", "tenant_id": "t-1"}
    )
    session = sessions.read_session(request)
    assert session is not None
    assert session["sub"] == "headspace"
    assert session["kind"] == "headspace_admin"
    assert session["tenant_id"] == "t-1"
    assert "exp" in session


def test_session_expired_returns_none() -> None:
    token = sessions.create_session_cookie(
        {"sub": "x", "kind": "tenant_user", "exp": int(time.time()) - 10}
    )
    request = _FakeRequest({sessions.COOKIE_NAME: token})
    assert sessions.read_session(request) is None


def test_session_tampered_returns_none() -> None:
    _, token = _request_with_session({"sub": "x", "kind": "headspace_admin"})
    encoded, _, signature = token.partition(".")
    # Flip the payload but keep the old signature -> HMAC mismatch.
    tampered = encoded[:-2] + ("AA" if not encoded.endswith("AA") else "BB") + "." + signature
    request = _FakeRequest({sessions.COOKIE_NAME: tampered})
    assert sessions.read_session(request) is None


def test_session_missing_cookie_returns_none() -> None:
    assert sessions.read_session(_FakeRequest({})) is None


def test_session_wrong_kind_is_distinguishable() -> None:
    # A tenant_user session round-trips faithfully, so the sales guard
    # (kind == 'headspace_admin') can reject it.
    request, _ = _request_with_session({"sub": "u-1", "kind": "tenant_user"})
    session = sessions.read_session(request)
    assert session is not None
    assert session["kind"] == "tenant_user"
    assert session["kind"] != "headspace_admin"


def test_session_signed_with_different_secret_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, token = _request_with_session({"sub": "x", "kind": "headspace_admin"})
    # Rotate the secret: a token signed under the old one no longer verifies.
    monkeypatch.setenv("JARVIES_TOKEN_SECRET", "a-completely-different-secret")
    mcp_config.get_settings.cache_clear()
    assert sessions.read_session(_FakeRequest({sessions.COOKIE_NAME: token})) is None


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_csrf_verify_ok() -> None:
    request, token = _request_with_session({"sub": "x", "kind": "headspace_admin"})
    csrf = sessions.csrf_for_request(request)
    assert csrf is not None
    assert sessions.verify_csrf(request, csrf) is True


def test_csrf_reject_wrong_token() -> None:
    request, _ = _request_with_session({"sub": "x", "kind": "headspace_admin"})
    assert sessions.verify_csrf(request, "not-the-token") is False
    assert sessions.verify_csrf(request, None) is False


def test_csrf_bound_to_cookie_value() -> None:
    # A CSRF token minted for one session must not validate against another.
    req_a, _ = _request_with_session({"sub": "a", "kind": "headspace_admin"})
    csrf_a = sessions.csrf_for_request(req_a)
    req_b, _ = _request_with_session({"sub": "b", "kind": "headspace_admin"})
    assert sessions.verify_csrf(req_b, csrf_a) is False


def test_csrf_none_without_cookie() -> None:
    assert sessions.csrf_for_request(_FakeRequest({})) is None
