"""Signed-cookie sessions and CSRF tokens for the portal.

No third-party dependency: a session is a JSON payload
``{sub, kind, tenant_id?, exp}`` signed with HMAC-SHA256 over
``settings.jarvies_token_secret`` (the same secret the OAuth layer signs with),
encoded as ``<b64url(payload)>.<b64url(sig)>`` — a compact home-grown token, not
a JWT. ``kind`` is ``headspace_admin`` (sales portal) or ``tenant_user`` (the
Day-5 tenant portal). The cookie is ``jarvies_portal``: HttpOnly, SameSite=Lax,
Secure in production, scoped to ``/portal``, 8h TTL.

CSRF (part D): every state-changing POST carries a hidden token
``HMAC(secret, session-cookie-value)``. Because it is bound to the exact cookie
value, it is unforgeable without the cookie and rotates when the session does.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from agents.mcp.config import get_settings

COOKIE_NAME = "jarvies_portal"
COOKIE_PATH = "/portal"
SESSION_TTL_SECONDS = 8 * 60 * 60  # 8h

VALID_KINDS = ("headspace_admin", "tenant_user")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _secret() -> bytes:
    return get_settings().jarvies_token_secret.encode("utf-8")


def _sign(message: bytes) -> str:
    return _b64e(hmac.new(_secret(), message, hashlib.sha256).digest())


def create_session_cookie(payload: dict[str, Any]) -> str:
    """Return a signed session token for ``payload``.

    ``exp`` is stamped here (now + TTL) unless the caller supplied one. The
    returned string is the cookie *value*; use ``set_session_cookie`` to attach
    it to a response with the correct flags.
    """

    body = dict(payload)
    body.setdefault("exp", int(time.time()) + SESSION_TTL_SECONDS)
    encoded = _b64e(json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{encoded}.{_sign(encoded.encode('ascii'))}"


def _decode(token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    encoded, _, signature = token.partition(".")
    if not hmac.compare_digest(signature, _sign(encoded.encode("ascii"))):
        return None
    try:
        payload = json.loads(_b64d(encoded))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None
    return payload


def read_session(request: Request) -> dict[str, Any] | None:
    """Return the session payload for a request, or ``None``.

    ``None`` on missing/invalid/expired/tampered cookie. Never raises.
    """

    return _decode(request.cookies.get(COOKIE_NAME))


def set_session_cookie(response: Response, payload: dict[str, Any]) -> None:
    """Attach a fresh session cookie for ``payload`` to ``response``."""

    response.set_cookie(
        COOKIE_NAME,
        create_session_cookie(payload),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=get_settings().is_production,
        path=COOKIE_PATH,
    )


def clear_session_cookie(response: Response) -> None:
    """Delete the session cookie (logout)."""

    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def csrf_token(session_cookie_value: str) -> str:
    """Return the CSRF token bound to a session cookie value."""

    return _sign(("csrf:" + session_cookie_value).encode("utf-8"))


def csrf_for_request(request: Request) -> str | None:
    """Return the CSRF token for the request's session, or ``None``."""

    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    return csrf_token(cookie)


def verify_csrf(request: Request, submitted: str | None) -> bool:
    """Constant-time check of a submitted CSRF token against the session cookie."""

    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie or not submitted:
        return False
    return hmac.compare_digest(submitted, csrf_token(cookie))
