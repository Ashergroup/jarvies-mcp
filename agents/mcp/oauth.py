"""OAuth 2.0 authorization-server endpoints for Jarvies (Phase 2B).

Jarvies acts as an OAuth 2.0 AS so claude.ai can authenticate users via
Microsoft (multi-tenant /common endpoint) and receive a Jarvies-issued access
token (HS256 JWT) scoped to a tenant.

Two PKCE legs are involved and MUST NOT be conflated:

* claude.ai <-> Jarvies: the caller's code_challenge is stored at /authorize
  and verified at /token against the caller's code_verifier.
* Jarvies <-> Microsoft: Jarvies generates its OWN PKCE pair for the Microsoft
  leg (we are the only party that has its verifier at callback time). The
  caller's challenge is deliberately NOT forwarded to Microsoft — doing so would
  make the Microsoft token exchange impossible to complete, since the caller's
  verifier never reaches Jarvies.

State stores are in-memory dicts with TTLs; they reset on restart, which is
acceptable for the current single-product connector. No token values, secrets,
or PKCE verifiers are ever logged.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import anyio
from jose import JWTError
from jose import jwt as jose_jwt
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from agents.mcp.config import get_settings
from agents.mcp.database import DatabaseNotConfiguredError, get_conn

log = logging.getLogger(__name__)

# Static client for the permissive single-product dynamic registration.
STATIC_CLIENT_ID = "jarvies-claude-client"
STATIC_CLIENT_SECRET = ""

# Microsoft authorize endpoint + scopes. Reserved OIDC scopes go in the raw
# authorize redirect; MSAL adds them itself for the token exchange, so the MSAL
# call uses only the resource scope.
MS_AUTHORIZE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MS_REDIRECT_SCOPE = "openid profile email offline_access User.Read"
MSAL_SCOPES = ["User.Read"]

JARVIES_TOKEN_TTL_SECONDS = 28_800  # 8h
PENDING_AUTH_TTL_SECONDS = 600  # 10 min
PENDING_CODE_TTL_SECONDS = 300  # 5 min

# state -> pending authorization (caller redirect/challenge + Jarvies' MS verifier)
_pending_auths: dict[str, dict[str, Any]] = {}
# jarvies auth code -> issued token + caller PKCE challenge
_pending_codes: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# PKCE + token helpers
# ---------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def pkce_challenge(verifier: str) -> str:
    """Return the S256 code challenge for a verifier (base64url, no padding)."""

    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """Constant-time check that ``S256(code_verifier) == code_challenge``."""

    if not code_verifier or not code_challenge:
        return False
    return secrets.compare_digest(pkce_challenge(code_verifier), code_challenge)


def create_jarvies_token(user_id: str, tenant_id: str, ttl: int = JARVIES_TOKEN_TTL_SECONDS) -> str:
    """Sign a Jarvies access token (HS256) carrying the user + tenant."""

    now = int(time.time())
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "iat": now,
        "exp": now + ttl,
    }
    return jose_jwt.encode(payload, get_settings().jarvies_token_secret, algorithm="HS256")


def decode_jarvies_token(token: str) -> dict[str, Any] | None:
    """Verify a Jarvies token's signature + expiry. Returns claims or ``None``.

    Returns ``None`` (never raises) for missing secret, bad signature, expiry,
    or any malformed token, so callers can treat it as a simple gate.
    """

    secret = get_settings().jarvies_token_secret
    if not secret or not token:
        return None
    try:
        return jose_jwt.decode(token, secret, algorithms=["HS256"])
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# In-memory store helpers (TTL pruning on access)
# ---------------------------------------------------------------------------


def _prune(store: dict[str, dict[str, Any]], ttl: int) -> None:
    cutoff = time.time() - ttl
    expired = [k for k, v in store.items() if v.get("created_at", 0) < cutoff]
    for k in expired:
        store.pop(k, None)


def _take(store: dict[str, dict[str, Any]], key: str, ttl: int) -> dict[str, Any] | None:
    """Pop and return a non-expired entry, or ``None``."""

    _prune(store, ttl)
    entry = store.pop(key, None)
    if entry is None:
        return None
    if entry.get("created_at", 0) < time.time() - ttl:
        return None
    return entry


def _base_url(request: Request) -> str:
    settings = get_settings()
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _discovery_document(base_url: str) -> dict[str, Any]:
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "registration_endpoint": f"{base_url}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
    }


def _protected_resource_document(base_url: str) -> dict[str, Any]:
    """RFC 9728 protected-resource metadata.

    Tells the MCP client (claude.ai) which authorization server guards this
    resource. We are both the resource and the auth server, so both point at the
    same base URL. ``resource`` is the URL the client connected to (the server
    root — claude.ai POSTs MCP to ``/``), which it echoes as the RFC 8707
    ``resource`` parameter when requesting a token.
    """

    return {
        "resource": base_url,
        "authorization_servers": [base_url],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"],
    }


# ---------------------------------------------------------------------------
# Discovery + registration
# ---------------------------------------------------------------------------


async def oauth_metadata(request: Request) -> JSONResponse:
    return JSONResponse(_discovery_document(_base_url(request)))


async def openid_configuration(request: Request) -> JSONResponse:
    # claude.ai probes both; identical content.
    return JSONResponse(_discovery_document(_base_url(request)))


async def protected_resource_metadata(request: Request) -> JSONResponse:
    return JSONResponse(_protected_resource_document(_base_url(request)))


async def register(request: Request) -> JSONResponse:
    """Permissive RFC 7591 dynamic registration: every caller gets one client."""

    return JSONResponse(
        {
            "client_id": STATIC_CLIENT_ID,
            "client_secret": STATIC_CLIENT_SECRET,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        }
    )


# ---------------------------------------------------------------------------
# Authorization endpoint
# ---------------------------------------------------------------------------


async def authorize(request: Request) -> Response:
    params = request.query_params
    redirect_uri = params.get("redirect_uri")
    state = params.get("state")
    code_challenge = params.get("code_challenge")
    code_challenge_method = params.get("code_challenge_method", "S256")

    if not redirect_uri or not state:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "redirect_uri and state required"},
            status_code=400,
        )
    if not code_challenge or code_challenge_method != "S256":
        return JSONResponse(
            {"error": "invalid_request", "error_description": "S256 PKCE code_challenge required"},
            status_code=400,
        )

    settings = get_settings()

    # Jarvies' own PKCE pair for the Microsoft leg.
    ms_verifier = _b64url(secrets.token_bytes(48))
    ms_challenge = pkce_challenge(ms_verifier)

    _prune(_pending_auths, PENDING_AUTH_TTL_SECONDS)
    _pending_auths[state] = {
        "client_redirect_uri": redirect_uri,
        "client_code_challenge": code_challenge,
        "ms_code_verifier": ms_verifier,
        "created_at": time.time(),
    }

    ms_params = {
        "client_id": settings.azure_client_id,
        "response_type": "code",
        "redirect_uri": settings.azure_redirect_uri,
        "scope": MS_REDIRECT_SCOPE,
        "state": state,
        "code_challenge": ms_challenge,
        "code_challenge_method": "S256",
    }
    return RedirectResponse(f"{MS_AUTHORIZE_URL}?{urlencode(ms_params)}", status_code=302)


# ---------------------------------------------------------------------------
# Microsoft callback
# ---------------------------------------------------------------------------


async def auth_callback(request: Request) -> Response:
    params = request.query_params
    code = params.get("code")
    state = params.get("state")

    if not state:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "state required"},
            status_code=400,
        )
    pending = _take(_pending_auths, state, PENDING_AUTH_TTL_SECONDS)
    if pending is None:
        return JSONResponse(
            {"error": "not_found", "error_description": "unknown or expired state"},
            status_code=404,
        )
    if not code:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "code required"},
            status_code=400,
        )

    result = await _exchange_ms_code(code, pending["ms_code_verifier"])
    if "access_token" not in result:
        log.warning("ms_token_exchange_failed", extra={"error": result.get("error")})
        return JSONResponse(
            {"error": "ms_exchange_failed", "error_description": result.get("error", "unknown")},
            status_code=502,
        )

    claims = result.get("id_token_claims") or {}
    oid = claims.get("oid")
    tid = claims.get("tid")
    email = claims.get("email") or claims.get("preferred_username")
    name = claims.get("name")
    if not oid or not tid:
        return JSONResponse(
            {"error": "invalid_id_token", "error_description": "missing oid/tid"},
            status_code=400,
        )

    try:
        user_id, tenant_id = await _persist_identity(
            tid=tid,
            oid=oid,
            email=email,
            name=name,
            access_token=result.get("access_token"),
            refresh_token=result.get("refresh_token"),
            expires_in=result.get("expires_in"),
            scope=result.get("scope"),
        )
    except DatabaseNotConfiguredError:
        return JSONResponse(
            {"error": "server_error", "error_description": "database not configured"},
            status_code=500,
        )

    jarvies_token = create_jarvies_token(user_id, tenant_id)
    jarvies_code = secrets.token_hex(32)
    _prune(_pending_codes, PENDING_CODE_TTL_SECONDS)
    _pending_codes[jarvies_code] = {
        "jarvies_token": jarvies_token,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "code_challenge": pending["client_code_challenge"],
        "created_at": time.time(),
    }

    client_redirect = pending["client_redirect_uri"]
    sep = "&" if "?" in client_redirect else "?"
    query = urlencode({"code": jarvies_code, "state": state})
    return RedirectResponse(f"{client_redirect}{sep}{query}", status_code=302)


async def _exchange_ms_code(code: str, ms_code_verifier: str) -> dict[str, Any]:
    """Exchange a Microsoft auth code for tokens via MSAL (run off the loop)."""

    import msal

    settings = get_settings()

    def _do() -> dict[str, Any]:
        client = msal.ConfidentialClientApplication(
            client_id=settings.azure_client_id,
            client_credential=settings.azure_client_secret,
            authority=settings.azure_authority,
        )
        return client.acquire_token_by_authorization_code(
            code,
            scopes=MSAL_SCOPES,
            redirect_uri=settings.azure_redirect_uri,
            code_verifier=ms_code_verifier,
        )

    return await anyio.to_thread.run_sync(functools.partial(_do))


async def _persist_identity(
    *,
    tid: str,
    oid: str,
    email: str | None,
    name: str | None,
    access_token: str | None,
    refresh_token: str | None,
    expires_in: int | None,
    scope: str | None,
) -> tuple[str, str]:
    """Upsert tenant + user and store the Microsoft tokens. Returns (user_id, tenant_id)."""

    expires_at = None
    if expires_in:
        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))

    async with get_conn() as conn, conn.transaction():
        tenant_id = await conn.fetchval(
            """
            INSERT INTO tenants (microsoft_tenant_id, display_name)
            VALUES ($1, $1)
            ON CONFLICT (microsoft_tenant_id)
            DO UPDATE SET microsoft_tenant_id = EXCLUDED.microsoft_tenant_id
            RETURNING id
            """,
            tid,
        )
        user_id = await conn.fetchval(
            """
            INSERT INTO users (tenant_id, microsoft_user_id, email, display_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (tenant_id, microsoft_user_id)
            DO UPDATE SET email = EXCLUDED.email, display_name = EXCLUDED.display_name
            RETURNING id
            """,
            tenant_id,
            oid,
            email,
            name,
        )
        # Replace any prior tokens for this user (no unique key on user_tokens).
        await conn.execute("DELETE FROM user_tokens WHERE user_id = $1", user_id)
        await conn.execute(
            """
            INSERT INTO user_tokens (user_id, access_token, refresh_token, expires_at, scope)
            VALUES ($1, $2, $3, $4, $5)
            """,
            user_id,
            access_token,
            refresh_token,
            expires_at,
            scope,
        )
    return str(user_id), str(tenant_id)


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


async def token(request: Request) -> JSONResponse:
    form = await request.form()
    grant_type = form.get("grant_type")
    code = form.get("code")
    code_verifier = form.get("code_verifier")

    if grant_type != "authorization_code":
        return JSONResponse(
            {"error": "unsupported_grant_type"},
            status_code=400,
        )
    if not code:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "code required"},
            status_code=400,
        )

    entry = _take(_pending_codes, code, PENDING_CODE_TTL_SECONDS)
    if entry is None:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "unknown or expired code"},
            status_code=400,
        )

    if not verify_pkce(code_verifier or "", entry["code_challenge"]):
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "PKCE verification failed"},
            status_code=400,
        )

    return JSONResponse(
        {
            "access_token": entry["jarvies_token"],
            "token_type": "bearer",
            "expires_in": JARVIES_TOKEN_TTL_SECONDS,
            "scope": "mcp",
        }
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

OAUTH_PUBLIC_PATHS = {
    "/.well-known/oauth-authorization-server",
    "/.well-known/openid-configuration",
    "/.well-known/oauth-protected-resource",
    "/register",
    "/authorize",
    "/auth/callback",
    "/token",
}


def get_oauth_routes() -> list[Route]:
    return [
        Route("/.well-known/oauth-authorization-server", oauth_metadata, methods=["GET"]),
        Route("/.well-known/openid-configuration", openid_configuration, methods=["GET"]),
        Route(
            "/.well-known/oauth-protected-resource",
            protected_resource_metadata,
            methods=["GET"],
        ),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize, methods=["GET"]),
        Route("/auth/callback", auth_callback, methods=["GET"]),
        Route("/token", token, methods=["POST"]),
    ]


def register_oauth_routes(app: Any) -> None:
    """Attach the OAuth routes to a Starlette app."""

    for route in get_oauth_routes():
        app.router.routes.append(route)
