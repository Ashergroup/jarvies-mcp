"""Tests for the Phase 2B OAuth 2.0 endpoints + Bearer-token auth."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agents.mcp import config as mcp_config
from agents.mcp import oauth, tenant
from agents.mcp.auth import MCPAuthMiddleware
from agents.mcp.tenant import TenantResolutionMiddleware

JARVIES_SECRET = "test-secret-0123456789abcdef0123456789abcdef"
AZURE_CLIENT_ID = "82f4503e-369f-4c78-a22b-9eac587d6376"
AZURE_REDIRECT_URI = "https://app.example.aws/auth/callback"


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JARVIES_TOKEN_SECRET", JARVIES_SECRET)
    monkeypatch.setenv("AZURE_CLIENT_ID", AZURE_CLIENT_ID)
    monkeypatch.setenv("AZURE_REDIRECT_URI", AZURE_REDIRECT_URI)
    monkeypatch.setenv("MCP_API_KEYS", "")
    monkeypatch.setenv("MCP_JWT_SECRET", "")
    monkeypatch.setenv("MCP_ALLOW_UNAUTHENTICATED", "false")
    mcp_config.get_settings.cache_clear()
    oauth._pending_auths.clear()
    oauth._pending_codes.clear()
    yield
    oauth._pending_auths.clear()
    oauth._pending_codes.clear()
    mcp_config.get_settings.cache_clear()


def _oauth_client() -> TestClient:
    app = Starlette(routes=oauth.get_oauth_routes())
    return TestClient(app)


# ---------------------------------------------------------------------------
# Discovery + registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"],
)
def test_discovery_shape(path: str) -> None:
    resp = _oauth_client().get(path)
    assert resp.status_code == 200
    body = resp.json()
    base = "http://testserver"
    assert body == {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
    }


def test_protected_resource_metadata_points_at_auth_server() -> None:
    resp = _oauth_client().get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    base = "http://testserver"
    assert body == {
        "resource": base,
        "authorization_servers": [base],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
    }


def test_register_returns_static_client() -> None:
    resp = _oauth_client().post(
        "/register",
        json={
            "client_name": "claude",
            "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["client_id"] == "jarvies-claude-client"
    assert body["client_secret"] == ""
    assert body["token_endpoint_auth_method"] == "none"
    assert body["grant_types"] == ["authorization_code"]
    # The caller's redirect_uris MUST be echoed back, or claude.ai aborts.
    assert body["redirect_uris"] == ["https://claude.ai/api/mcp/auth_callback"]
    assert body["client_name"] == "claude"


def test_register_without_redirect_uris_returns_empty_list() -> None:
    resp = _oauth_client().post("/register", json={"client_name": "claude"})
    assert resp.status_code == 201
    assert resp.json()["redirect_uris"] == []


# ---------------------------------------------------------------------------
# Authorization endpoint
# ---------------------------------------------------------------------------


def test_authorize_redirects_to_microsoft() -> None:
    client = _oauth_client()
    resp = client.get(
        "/authorize",
        params={
            "client_id": "jarvies-claude-client",
            "redirect_uri": "https://claude.ai/api/callback",
            "response_type": "code",
            "state": "state-xyz",
            "code_challenge": "caller-challenge",
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize")

    q = parse_qs(urlparse(location).query)
    assert q["client_id"] == [AZURE_CLIENT_ID]
    assert q["redirect_uri"] == [AZURE_REDIRECT_URI]
    assert q["response_type"] == ["code"]
    assert q["state"] == ["state-xyz"]
    assert q["code_challenge_method"] == ["S256"]
    assert "User.Read" in q["scope"][0]
    # A PKCE challenge is forwarded to Microsoft (Jarvies' own, not the caller's).
    assert q["code_challenge"][0]
    assert q["code_challenge"][0] != "caller-challenge"

    # The caller's redirect + challenge are stored for /token verification.
    pending = oauth._pending_auths["state-xyz"]
    assert pending["client_redirect_uri"] == "https://claude.ai/api/callback"
    assert pending["client_code_challenge"] == "caller-challenge"


def test_authorize_requires_pkce() -> None:
    resp = _oauth_client().get(
        "/authorize",
        params={"redirect_uri": "https://claude.ai/cb", "state": "s1"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------


def test_callback_invalid_state_returns_404() -> None:
    resp = _oauth_client().get(
        "/auth/callback",
        params={"code": "ms-code", "state": "no-such-state"},
        follow_redirects=False,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


def _seed_pending_code(code: str, verifier: str) -> str:
    """Place a pending code whose challenge matches ``verifier``; returns token."""

    jarvies_token = oauth.create_jarvies_token("user-1", "tenant-1")
    oauth._pending_codes[code] = {
        "jarvies_token": jarvies_token,
        "user_id": "user-1",
        "tenant_id": "tenant-1",
        "code_challenge": oauth.pkce_challenge(verifier),
        "created_at": time.time(),
    }
    return jarvies_token


def test_token_invalid_code_returns_400() -> None:
    resp = _oauth_client().post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": "does-not-exist",
            "code_verifier": "whatever",
            "redirect_uri": "https://claude.ai/cb",
            "client_id": "jarvies-claude-client",
        },
    )
    assert resp.status_code == 400


def test_token_invalid_verifier_returns_400() -> None:
    _seed_pending_code("code-abc", verifier="the-real-verifier")
    resp = _oauth_client().post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": "code-abc",
            "code_verifier": "WRONG-verifier",
            "redirect_uri": "https://claude.ai/cb",
            "client_id": "jarvies-claude-client",
        },
    )
    assert resp.status_code == 400


def test_token_happy_path_returns_access_token() -> None:
    expected = _seed_pending_code("code-ok", verifier="the-real-verifier")
    resp = _oauth_client().post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": "code-ok",
            "code_verifier": "the-real-verifier",
            "redirect_uri": "https://claude.ai/cb",
            "client_id": "jarvies-claude-client",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == expected
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 28800
    assert body["scope"] == "mcp"
    # One-time use: the code is consumed.
    assert "code-ok" not in oauth._pending_codes


# ---------------------------------------------------------------------------
# Bearer-token auth via the middleware stack
# ---------------------------------------------------------------------------

FAKE_TENANT = {
    "id": "e9ba18f4-ad01-4830-96a4-61a6068df989",
    "microsoft_tenant_id": "d7afc5b8-d7f1-48ba-a6b5-d2f21608bb66",
    "display_name": "Asher Group / Niche Group",
    "is_active": True,
}


def _protected_app() -> Starlette:
    async def protected(request: Request) -> JSONResponse:
        return JSONResponse({"tenant": getattr(request.state, "tenant", "MISSING")})

    app = Starlette(routes=[Route("/protected", protected)])
    app.add_middleware(TenantResolutionMiddleware)  # inner
    app.add_middleware(MCPAuthMiddleware)  # outer (auth first)
    return app


def test_bearer_valid_jwt_sets_request_state_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load_tenant(tenant_id: str):
        return FAKE_TENANT if tenant_id == FAKE_TENANT["id"] else None

    monkeypatch.setattr(tenant, "load_tenant", fake_load_tenant)

    token = oauth.create_jarvies_token(user_id="user-1", tenant_id=FAKE_TENANT["id"])
    client = TestClient(_protected_app())
    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["tenant"] == FAKE_TENANT


def test_protected_resource_is_public_but_root_is_not() -> None:
    from agents.mcp.auth import PUBLIC_PATHS

    assert "/.well-known/oauth-protected-resource" in PUBLIC_PATHS
    # Root serves the MCP endpoint and must stay protected to drive OAuth.
    assert "/" not in PUBLIC_PATHS


def test_unauthorized_response_carries_resource_metadata_challenge() -> None:
    client = TestClient(_protected_app())
    resp = client.get("/protected")
    assert resp.status_code == 401
    challenge = resp.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'resource_metadata="' in challenge
    assert challenge.endswith('/.well-known/oauth-protected-resource"')


def test_bearer_expired_jwt_returns_401() -> None:
    token = oauth.create_jarvies_token(user_id="user-1", tenant_id=FAKE_TENANT["id"], ttl=-10)
    client = TestClient(_protected_app())
    resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_bearer_tampered_jwt_returns_401() -> None:
    token = oauth.create_jarvies_token(user_id="user-1", tenant_id=FAKE_TENANT["id"])
    tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    client = TestClient(_protected_app())
    resp = client.get("/protected", headers={"Authorization": f"Bearer {tampered}"})
    assert resp.status_code == 401
