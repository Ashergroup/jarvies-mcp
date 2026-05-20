"""Xero MCP tools — read-only Accounting API access.

SDK choice: raw httpx. The official `xero-python` package bundles a generated
OpenAPI client per API. We support two OAuth2 token paths and a small in-memory
cache, which is one POST per refresh — the SDK's surface area is more weight
than benefit. Switching to `xero-python` later only changes the token + HTTP
call paths.

Lifecycle:
- One process-wide `XeroService` singleton, created lazily via `_get_service()`.
  All tool calls share the same `httpx.AsyncClient` and access-token cache,
  so successive Xero tools in a single run reuse the cached access token
  (typically one /connect/token call per ~30 minutes of activity, not per
  tool call). Tests reset the singleton via `_reset_service()`.

Auth paths (chosen at call time):
- Path A — authorization_code + refresh_token (preferred). When a refresh
  token is available, the service refreshes against
  `https://identity.xero.com/connect/token` with `grant_type=refresh_token`.
  Xero rotates the refresh token on every refresh; the new value is
  persisted to `.secrets/xero_refresh_token.txt` (atomic write, mode 0600
  on POSIX). On startup, that file takes precedence over `XERO_REFRESH_TOKEN`
  from env, so `.env` only ever holds the *initial bootstrap* value from the
  OAuth dance.
- Path B — client_credentials (Custom Connection, machine-to-machine). Used
  when no refresh token is set but client_id/secret/tenant_id are all
  present. Some Xero regions don't allow Custom Connections; Path A is the
  primary path going forward.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

from agents.mcp.config import MCPSettings, get_settings
from agents.mcp.integrations import error as integration_error
from agents.mcp.integrations import not_configured, ok
from agents.mcp.permissions import check_permission
from agents.mcp.tenant_context import build_tenant_context, use_tenant_context

log = logging.getLogger(__name__)

_TOKEN_REFRESH_SKEW_SECONDS = 60.0

# Repo-root-relative location for the rotated refresh token. The repo root is
# three levels up from this file: agents/mcp/tools/xero_tools.py → ../../../
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_REFRESH_TOKEN_PATH = _REPO_ROOT / ".secrets" / "xero_refresh_token.txt"


def _refresh_token_path() -> Path:
    """Resolve the on-disk path for the persisted refresh token.

    Honours `XERO_REFRESH_TOKEN_FILE` if set, otherwise the project default.
    Tests use the env var to redirect into `tmp_path`.
    """

    override = os.environ.get("XERO_REFRESH_TOKEN_FILE")
    return Path(override) if override else _DEFAULT_REFRESH_TOKEN_PATH


def _read_persisted_refresh_token() -> str:
    """Return the persisted refresh token, or '' if the file is absent/empty."""

    path = _refresh_token_path()
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except OSError:
        return ""
    return text


def _write_persisted_refresh_token(value: str) -> None:
    """Atomically persist a new refresh token to disk. Never logs the value."""

    path = _refresh_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8")
    if os.name != "nt":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


class _CachedToken:
    """Single Xero access token kept in memory until shortly before expiry."""

    __slots__ = ("access_token", "expires_at")

    def __init__(self, access_token: str, expires_at: float) -> None:
        self.access_token = access_token
        self.expires_at = expires_at

    def is_fresh(self, now: float) -> bool:
        return now + _TOKEN_REFRESH_SKEW_SECONDS < self.expires_at


class XeroService:
    """Read-only Xero Accounting API client.

    Designed to live as a process-wide singleton (see `_get_service()`).
    Owns one shared `httpx.AsyncClient`, an access-token cache keyed by
    tenant id, and the current refresh token (which may rotate during the
    lifetime of the service).
    """

    def __init__(self, settings: MCPSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None
        self._tokens: dict[str, _CachedToken] = {}
        # File on disk wins over env so we never use a stale .env value after
        # the very first rotation.
        persisted = _read_persisted_refresh_token()
        self._refresh_token: str = persisted or self._settings.xero_refresh_token

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._settings.integration_http_timeout_seconds,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _access_token(self) -> str:
        tenant_id = self._settings.xero_tenant_id
        cached = self._tokens.get(tenant_id)
        now = time.monotonic()
        if cached is not None and cached.is_fresh(now):
            return cached.access_token

        if self._refresh_token:
            return await self._refresh_with_refresh_token(now)
        return await self._refresh_with_client_credentials(now)

    async def _refresh_with_refresh_token(self, now: float) -> str:
        """Path A — exchange the stored refresh token for a fresh access token."""

        tenant_id = self._settings.xero_tenant_id
        client = await self._http()
        started = time.perf_counter()
        response = await client.post(
            self._settings.xero_identity_url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._settings.xero_client_id,
                "client_secret": self._settings.xero_client_secret,
            },
        )
        latency_ms = (time.perf_counter() - started) * 1000
        log.info(
            "xero_api_call",
            extra={
                "method": "POST",
                "path": "/connect/token",
                "grant": "refresh_token",
                "status": response.status_code,
                "latency_ms": round(latency_ms, 1),
            },
        )
        response.raise_for_status()
        payload = response.json()
        access_token = payload["access_token"]
        expires_in = float(payload.get("expires_in", 1800))

        new_refresh = payload.get("refresh_token")
        if new_refresh and new_refresh != self._refresh_token:
            try:
                _write_persisted_refresh_token(new_refresh)
                self._refresh_token = new_refresh
                log.warning(
                    "Xero rotated refresh token; new value persisted to %s",
                    _refresh_token_path(),
                )
            except OSError as exc:
                # If persistence fails we still update in-memory state so the
                # next call in this process uses the new value, but we surface
                # a clear warning. We DO NOT log the token itself.
                self._refresh_token = new_refresh
                log.warning(
                    "Xero rotated refresh token but persistence to %s failed (%s); "
                    "token kept in memory only — restart will lose it",
                    _refresh_token_path(),
                    exc.__class__.__name__,
                )

        self._tokens[tenant_id] = _CachedToken(access_token, now + expires_in)
        return access_token

    async def _refresh_with_client_credentials(self, now: float) -> str:
        """Path B — Custom Connection client_credentials grant (legacy fallback)."""

        tenant_id = self._settings.xero_tenant_id
        basic = base64.b64encode(
            f"{self._settings.xero_client_id}:{self._settings.xero_client_secret}".encode()
        ).decode("ascii")
        client = await self._http()

        started = time.perf_counter()
        response = await client.post(
            self._settings.xero_identity_url,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={"grant_type": "client_credentials", "scope": self._settings.xero_scopes},
        )
        latency_ms = (time.perf_counter() - started) * 1000
        log.info(
            "xero_api_call",
            extra={
                "method": "POST",
                "path": "/connect/token",
                "grant": "client_credentials",
                "status": response.status_code,
                "latency_ms": round(latency_ms, 1),
            },
        )
        response.raise_for_status()
        payload = response.json()
        access_token = payload["access_token"]
        expires_in = float(payload.get("expires_in", 1800))
        self._tokens[tenant_id] = _CachedToken(access_token, now + expires_in)
        return access_token

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = await self._access_token()
        client = await self._http()
        url = f"{self._settings.xero_base_url.rstrip('/')}/{path.lstrip('/')}"
        started = time.perf_counter()
        response = await client.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Xero-tenant-id": self._settings.xero_tenant_id,
                "Accept": "application/json",
            },
            params={k: v for k, v in (params or {}).items() if v is not None},
        )
        latency_ms = (time.perf_counter() - started) * 1000
        log.info(
            "xero_api_call",
            extra={
                "method": "GET",
                "path": f"/api.xro/2.0/{path.lstrip('/')}",
                "status": response.status_code,
                "latency_ms": round(latency_ms, 1),
            },
        )
        response.raise_for_status()
        return response.json()

    async def get_contacts(self, page: int = 1, page_size: int | None = None) -> dict[str, Any]:
        size = _resolve_page_size(page_size, self._settings)
        payload = await self._get(
            "Contacts",
            params={"page": page, "pageSize": size, "summaryOnly": "true"},
        )
        contacts = payload.get("Contacts", [])[:size]
        return {"contacts": contacts, "count": len(contacts), "page": page, "page_size": size}

    async def get_invoices(
        self,
        status: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        contact_id: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        size = _resolve_page_size(page_size, self._settings)
        where_parts: list[str] = []
        if status:
            where_parts.append(f'Status=="{status}"')
        if date_from:
            where_parts.append(f"Date>=DateTime({date_from.replace('-', ',')})")
        if date_to:
            where_parts.append(f"Date<=DateTime({date_to.replace('-', ',')})")
        if contact_id:
            where_parts.append(f'Contact.ContactID==Guid("{contact_id}")')

        payload = await self._get(
            "Invoices",
            params={
                "page": page,
                "pageSize": size,
                "where": " AND ".join(where_parts) if where_parts else None,
                "summaryOnly": "true",
            },
        )
        invoices = payload.get("Invoices", [])[:size]
        return {"invoices": invoices, "count": len(invoices), "page": page, "page_size": size}

    async def get_payments(self, page: int = 1, page_size: int | None = None) -> dict[str, Any]:
        size = _resolve_page_size(page_size, self._settings)
        payload = await self._get("Payments", params={"page": page, "pageSize": size})
        payments = payload.get("Payments", [])[:size]
        return {"payments": payments, "count": len(payments), "page": page, "page_size": size}

    async def get_profit_loss(
        self,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        payload = await self._get(
            "Reports/ProfitAndLoss",
            params={"fromDate": from_date, "toDate": to_date},
        )
        reports = payload.get("Reports", [])
        return {"reports": reports, "count": len(reports)}


_service: XeroService | None = None


def _get_service() -> XeroService:
    """Return the process-wide XeroService singleton, creating it on first call."""

    global _service
    if _service is None:
        _service = XeroService()
    return _service


async def _reset_service() -> None:
    """Discard the singleton and close its HTTP client. Test-only helper."""

    global _service
    if _service is not None:
        await _service.aclose()
        _service = None


def _resolve_page_size(value: int | None, settings: MCPSettings) -> int:
    if value is None:
        return settings.tool_result_limit
    return max(1, min(int(value), settings.tool_result_limit))


def _context(
    tenant_id: str | None,
    user_id: str | None,
    access_token: str | None,
    permissions: list[str] | None,
):
    return build_tenant_context(
        tenant_id=tenant_id,
        user_id=user_id,
        access_token=access_token,
        permissions=permissions,
    )


async def _call(coro, source: str = "xero") -> dict[str, Any]:
    try:
        data = await coro
    except httpx.HTTPStatusError as exc:
        return integration_error(source, f"Xero API returned HTTP {exc.response.status_code}")
    except httpx.RequestError as exc:
        return integration_error(source, f"Xero request failed: {exc.__class__.__name__}")
    return ok(source, data)


_NOT_CONFIGURED_MESSAGE = (
    "Xero is not configured. Set XERO_CLIENT_ID, XERO_CLIENT_SECRET, "
    "XERO_TENANT_ID and either XERO_REFRESH_TOKEN (run "
    "`python scripts/xero_auth_setup.py` to obtain one) or use a "
    "Custom Connection."
)


async def xero_get_contacts(
    page: int = 1,
    page_size: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return Xero contacts (summary view).

    Args:
        page: Xero contact page index (1-based).
        page_size: Cap on returned contacts. Defaults to MCP_TOOL_RESULT_LIMIT.

    Returns:
        IntegrationResult dict with `data.contacts`, `data.count`, `data.page`,
        `data.page_size`, or `status=not_configured` when credentials are missing.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "xero_get_contacts",
            context.permissions,
        )
        settings = get_settings()
        if not settings.xero_configured:
            return not_configured("xero", _NOT_CONFIGURED_MESSAGE)
        return await _call(_get_service().get_contacts(page=page, page_size=page_size))


async def xero_get_invoices(
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    contact_id: str | None = None,
    page: int = 1,
    page_size: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return Xero invoices, optionally filtered.

    Args:
        status: Xero invoice status (e.g. AUTHORISED, PAID, DRAFT, VOIDED).
        date_from: ISO date `YYYY-MM-DD` — invoices on or after this date.
        date_to: ISO date `YYYY-MM-DD` — invoices on or before this date.
        contact_id: Xero ContactID to filter to one contact.
        page, page_size: Pagination, capped at MCP_TOOL_RESULT_LIMIT.

    Returns:
        IntegrationResult dict with `data.invoices`, `data.count`, plus
        pagination fields.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "xero_get_invoices",
            context.permissions,
        )
        settings = get_settings()
        if not settings.xero_configured:
            return not_configured("xero", _NOT_CONFIGURED_MESSAGE)
        return await _call(
            _get_service().get_invoices(
                status=status,
                date_from=date_from,
                date_to=date_to,
                contact_id=contact_id,
                page=page,
                page_size=page_size,
            )
        )


async def xero_get_payments(
    page: int = 1,
    page_size: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return recent Xero payments.

    Args:
        page, page_size: Pagination, capped at MCP_TOOL_RESULT_LIMIT.

    Returns:
        IntegrationResult dict with `data.payments`, `data.count`, plus
        pagination fields.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "xero_get_payments",
            context.permissions,
        )
        settings = get_settings()
        if not settings.xero_configured:
            return not_configured("xero", _NOT_CONFIGURED_MESSAGE)
        return await _call(_get_service().get_payments(page=page, page_size=page_size))


async def xero_create_invoice(
    payload: dict[str, Any],
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a Xero invoice — currently disabled in this read-only layer."""

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "xero_create_invoice",
            context.permissions,
        )
        return not_configured(
            "xero",
            "xero_create_invoice is intentionally read-only in this MCP layer.",
        )


async def xero_get_profit_loss(
    from_date: str | None = None,
    to_date: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return a Xero Profit and Loss report for the requested date range.

    Args:
        from_date: ISO date `YYYY-MM-DD` start of the reporting window.
        to_date: ISO date `YYYY-MM-DD` end of the reporting window.

    Returns:
        IntegrationResult dict with `data.reports`, `data.count`.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "xero_get_profit_loss",
            context.permissions,
        )
        settings = get_settings()
        if not settings.xero_configured:
            return not_configured("xero", _NOT_CONFIGURED_MESSAGE)
        return await _call(
            _get_service().get_profit_loss(from_date=from_date, to_date=to_date)
        )


def register(mcp: Any) -> None:
    """Register Xero MCP tools."""

    mcp.tool()(xero_get_contacts)
    mcp.tool()(xero_get_invoices)
    mcp.tool()(xero_get_payments)
    mcp.tool()(xero_create_invoice)
    mcp.tool()(xero_get_profit_loss)
