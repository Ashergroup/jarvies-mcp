"""HTTP authentication middleware for the MCP server.

This layer protects the Streamable HTTP endpoint. Tool-level permissions are
checked separately in `agents.mcp.permissions`.
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from agents.mcp.config import get_settings
from agents.mcp.oauth import OAUTH_PUBLIC_PATHS, decode_jarvies_token

try:  # PyJWT is optional until JWT auth is enabled.
    import jwt
except Exception:  # pragma: no cover - exercised only when dependency is absent.
    jwt = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


# Note: "/" is intentionally NOT public. The MCP Streamable HTTP endpoint is
# served at the root (claude.ai POSTs there), so an unauthenticated request must
# get a 401 with a WWW-Authenticate challenge to start the OAuth flow.
PUBLIC_PATHS = {"/health"} | OAUTH_PUBLIC_PATHS


def log_production_safety_warnings() -> None:
    """Warn loudly about unsafe production configuration. Never raises.

    Called once at startup. In production this surfaces two classes of mistake
    that would otherwise only show up as runtime auth behaviour:
      - no API key / JWT secret configured (the /mcp endpoint will 503), and
      - `admin_access` baked into the default permission set (every credential-
        less call would run with full write access).
    """

    settings = get_settings()
    if not settings.is_production:
        return

    if not settings.api_key_values and not settings.jwt_secret:
        log.error(
            "mcp_production_no_auth_configured: ENVIRONMENT=production but neither "
            "MCP_API_KEYS nor MCP_JWT_SECRET is set; /mcp will return 503 until one "
            "is configured."
        )

    if "admin_access" in settings.default_permission_values:
        log.error(
            "mcp_production_admin_default_permissions: MCP_DEFAULT_PERMISSIONS includes "
            "admin_access in production; callers without explicit permissions would run "
            "with full write access. Remove admin_access from the default set."
        )

    if settings.allow_unauthenticated:
        log.warning(
            "mcp_production_allow_unauthenticated_set: MCP_ALLOW_UNAUTHENTICATED=true is "
            "ignored in production (auth stays enforced), but it should be false to avoid "
            "confusion."
        )


class MCPAuthMiddleware(BaseHTTPMiddleware):
    """Reject unauthenticated MCP HTTP requests.

    Supported modes:
      - `X-API-Key: <key>` or `Authorization: Bearer <api-key>` for testing.
      - JWT Bearer token verification when `MCP_JWT_SECRET` is configured.
      - Explicit local bypass with `MCP_ALLOW_UNAUTHENTICATED=true`.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        settings = get_settings()
        path = request.url.path.rstrip("/") or "/"

        if path in PUBLIC_PATHS or request.method == "OPTIONS":
            return await call_next(request)

        if settings.allow_unauthenticated and not settings.is_production:
            log.warning("mcp_auth_bypassed_non_production", extra={"path": request.url.path})
            return await call_next(request)

        if _api_key_allowed(request):
            request.state.auth_method = "api_key"
            return await call_next(request)

        bearer = _bearer_token(request)
        # Jarvies-issued OAuth access token (Phase 2B). Tenant resolution from
        # its tenant_id claim happens in TenantResolutionMiddleware.
        if bearer and decode_jarvies_token(bearer) is not None:
            request.state.auth_method = "jarvies_token"
            return await call_next(request)

        if bearer and _jwt_allowed(bearer):
            request.state.auth_method = "jwt"
            return await call_next(request)

        if (
            not settings.api_key_values
            and not settings.jwt_secret
            and not settings.jarvies_token_secret
        ):
            status_code = 503 if settings.is_production else 401
            return JSONResponse(
                {
                    "error": "mcp_auth_not_configured",
                    "message": "Configure MCP_API_KEYS or MCP_JWT_SECRET before exposing /mcp.",
                },
                status_code=status_code,
            )

        return JSONResponse(
            {"error": "unauthorized", "message": "Missing or invalid MCP credentials."},
            status_code=401,
            headers={"WWW-Authenticate": _www_authenticate(request)},
        )


def _www_authenticate(request: Request) -> str:
    """Build the RFC 9728 Bearer challenge pointing at the resource metadata.

    Directs the MCP client to /.well-known/oauth-protected-resource so it can
    discover the authorization server and run the OAuth flow.
    """

    settings = get_settings()
    base = (
        settings.public_base_url.rstrip("/")
        if settings.public_base_url
        else str(request.base_url).rstrip("/")
    )
    return f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    return header.split(" ", 1)[1].strip()


def _api_key_allowed(request: Request) -> bool:
    settings = get_settings()
    keys = settings.api_key_values
    if not keys:
        return False

    supplied = request.headers.get("x-api-key") or _bearer_token(request)
    allowed = bool(supplied and supplied in keys)
    if not allowed and supplied:
        log.warning("mcp_api_key_rejected")
    return allowed


def _jwt_allowed(token: str) -> bool:
    settings = get_settings()
    if not settings.jwt_secret:
        return False
    if jwt is None:
        log.error("mcp_jwt_dependency_missing")
        return False

    options = {"verify_aud": bool(settings.jwt_audience)}
    try:
        jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256", "RS256"],
            audience=settings.jwt_audience or None,
            issuer=settings.jwt_issuer or None,
            options=options,
        )
    except Exception as exc:
        log.warning("mcp_jwt_rejected", extra={"error": str(exc)})
        return False
    return True
