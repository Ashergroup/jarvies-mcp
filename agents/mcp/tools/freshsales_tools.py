"""Freshsales MCP tools — read-only CRM access.

Auth: Freshsales uses an API-key token header
(`Authorization: Token token={api_key}`) — no OAuth dance, no refresh. A plain
`httpx.AsyncClient` with the header preset is enough. Base URL is constructed
from `FRESHSALES_DOMAIN` (e.g. `yourco.myfreshworks.com`) as
`https://{domain}/crm/sales/api/`.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from agents.mcp.config import MCPSettings, get_settings
from agents.mcp.credentials import resolve_settings
from agents.mcp.integrations import error as integration_error
from agents.mcp.integrations import not_configured, ok
from agents.mcp.permissions import check_permission
from agents.mcp.tenant_context import build_tenant_context, use_tenant_context

log = logging.getLogger(__name__)


class FreshsalesService:
    """Read-only Freshsales CRM client.

    One `httpx.AsyncClient` per service instance, created lazily.

    Note: the Freshsales list endpoints (`/contacts`, `/sales_accounts`, `/deals`)
    do NOT accept unfiltered listing — they return 403 unless the caller addresses
    a specific view via `/{entity}/view/{view_id}`. When the tool layer omits a
    view_id, this service auto-resolves a default view (preferring "All …") via
    `/{entity}/filters` and caches the result for the lifetime of the service.
    """

    def __init__(self, settings: MCPSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None
        self._default_views: dict[str, int] = {}

    @property
    def base_url(self) -> str:
        return f"https://{self._settings.freshsales_domain.rstrip('/')}/crm/sales/api"

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

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Token token={self._settings.freshsales_api_key}",
        }

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        client = await self._http()
        url = f"{self.base_url}/{path.lstrip('/')}"
        started = time.perf_counter()
        response = await client.get(
            url,
            headers=self._headers(),
            params={k: v for k, v in (params or {}).items() if v is not None},
        )
        latency_ms = (time.perf_counter() - started) * 1000
        log.info(
            "freshsales_api_call",
            extra={
                "method": "GET",
                "path": f"/{path.lstrip('/')}",
                "status": response.status_code,
                "latency_ms": round(latency_ms, 1),
            },
        )
        response.raise_for_status()
        return response.json()

    async def _default_view_id(self, entity: str) -> str:
        if entity in self._default_views:
            return str(self._default_views[entity])
        payload = await self._get(f"{entity}/filters")
        filters = payload.get("filters") if isinstance(payload, dict) else []
        if not isinstance(filters, list) or not filters:
            raise RuntimeError(f"Freshsales returned no views for {entity}")
        preferred = next(
            (
                f
                for f in filters
                if isinstance(f, dict)
                and isinstance(f.get("name"), str)
                and f["name"].lower().startswith("all ")
            ),
            None,
        )
        chosen = preferred or filters[0]
        view_id = chosen["id"]
        self._default_views[entity] = view_id
        return str(view_id)

    async def get_contacts(
        self,
        view_id: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        size = _resolve_page_size(page_size, self._settings)
        resolved_view = view_id or await self._default_view_id("contacts")
        payload = await self._get(
            f"contacts/view/{resolved_view}",
            params={"page": page, "per_page": size},
        )
        contacts = _coerce_list(payload, "contacts")[:size]
        return {
            "contacts": contacts,
            "count": len(contacts),
            "page": page,
            "page_size": size,
            "view_id": resolved_view,
        }

    async def get_accounts(
        self,
        view_id: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        size = _resolve_page_size(page_size, self._settings)
        resolved_view = view_id or await self._default_view_id("sales_accounts")
        payload = await self._get(
            f"sales_accounts/view/{resolved_view}",
            params={"page": page, "per_page": size},
        )
        accounts = _coerce_list(payload, "sales_accounts")[:size]
        return {
            "accounts": accounts,
            "count": len(accounts),
            "page": page,
            "page_size": size,
            "view_id": resolved_view,
        }

    async def get_deals(
        self,
        view_id: str | None = None,
        stage: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        size = _resolve_page_size(page_size, self._settings)
        resolved_view = view_id or await self._default_view_id("deals")
        payload = await self._get(
            f"deals/view/{resolved_view}",
            params={
                "page": page,
                "per_page": size,
                "deal_stage": stage,
                "updated_since": date_from,
                "updated_until": date_to,
            },
        )
        deals = _coerce_list(payload, "deals")[:size]
        return {
            "deals": deals,
            "count": len(deals),
            "page": page,
            "page_size": size,
            "view_id": resolved_view,
        }

    async def search(
        self,
        query: str,
        include: str = "contact,sales_account,deal",
        page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        size = _resolve_page_size(page_size, self._settings)
        payload = await self._get(
            "search",
            params={"q": query, "include": include, "per_page": size},
        )
        results = payload if isinstance(payload, list) else _coerce_list(payload, "results")
        results = results[:size]
        return {
            "results": results,
            "count": len(results),
            "query": query,
            "page": page,
            "page_size": size,
        }


def _coerce_list(payload: Any, key: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


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


async def _call(coro, source: str = "freshsales") -> dict[str, Any]:
    try:
        data = await coro
    except httpx.HTTPStatusError as exc:
        return integration_error(
            source, f"Freshsales API returned HTTP {exc.response.status_code}"
        )
    except httpx.RequestError as exc:
        return integration_error(source, f"Freshsales request failed: {exc.__class__.__name__}")
    return ok(source, data)


async def freshsales_get_contacts(
    view_id: str | None = None,
    page: int = 1,
    page_size: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return Freshsales contacts.

    Args:
        view_id: Freshsales saved-view ID for contacts. When omitted, the
            service auto-resolves a default view (prefers "All Contacts")
            because the Freshsales API rejects unfiltered `/contacts` listing
            with 403.
        page, page_size: Pagination, page_size capped at MCP_TOOL_RESULT_LIMIT.

    Returns:
        IntegrationResult dict with `data.contacts`, `data.count`, plus the
        `data.view_id` used, plus pagination fields, or `status=not_configured`
        when credentials missing.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "freshsales_get_contacts",
            context.permissions,
        )
        settings = (await resolve_settings("freshsales")).settings
        if not settings.freshsales_configured:
            return not_configured(
                "freshsales", "FRESHSALES_DOMAIN/FRESHSALES_API_KEY are not configured."
            )
        service = FreshsalesService(settings)
        try:
            return await _call(
                service.get_contacts(view_id=view_id, page=page, page_size=page_size)
            )
        finally:
            await service.aclose()


async def freshsales_get_accounts(
    view_id: str | None = None,
    page: int = 1,
    page_size: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return Freshsales sales accounts.

    Args:
        view_id: Optional Freshsales saved-view ID for sales accounts. When
            omitted, the service auto-resolves a default view (prefers "All
            Accounts") because the API rejects unfiltered listing.
        page, page_size: Pagination, page_size capped at MCP_TOOL_RESULT_LIMIT.

    Returns:
        IntegrationResult dict with `data.accounts`, `data.count`, plus the
        `data.view_id` used, plus pagination fields.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "freshsales_get_accounts",
            context.permissions,
        )
        settings = (await resolve_settings("freshsales")).settings
        if not settings.freshsales_configured:
            return not_configured(
                "freshsales", "FRESHSALES_DOMAIN/FRESHSALES_API_KEY are not configured."
            )
        service = FreshsalesService(settings)
        try:
            return await _call(
                service.get_accounts(view_id=view_id, page=page, page_size=page_size)
            )
        finally:
            await service.aclose()


async def freshsales_get_deals(
    view_id: str | None = None,
    stage: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return Freshsales deals, optionally filtered.

    Args:
        view_id: Freshsales saved-view ID for deals. When omitted, the service
            auto-resolves a default view (prefers "All Deals") because the
            Freshsales API rejects unfiltered `/deals` listing with 403.
        stage: Filter by deal stage name or ID.
        date_from: ISO datetime — deals updated on or after this point.
        date_to: ISO datetime — deals updated on or before this point.
        page, page_size: Pagination, page_size capped at MCP_TOOL_RESULT_LIMIT.

    Returns:
        IntegrationResult dict with `data.deals`, `data.count`, plus pagination.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "freshsales_get_deals",
            context.permissions,
        )
        settings = (await resolve_settings("freshsales")).settings
        if not settings.freshsales_configured:
            return not_configured(
                "freshsales", "FRESHSALES_DOMAIN/FRESHSALES_API_KEY are not configured."
            )
        service = FreshsalesService(settings)
        try:
            return await _call(
                service.get_deals(
                    view_id=view_id,
                    stage=stage,
                    date_from=date_from,
                    date_to=date_to,
                    page=page,
                    page_size=page_size,
                )
            )
        finally:
            await service.aclose()


async def freshsales_search(
    query: str,
    include: str = "contact,sales_account,deal",
    page: int = 1,
    page_size: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Run a Freshsales cross-entity search.

    Args:
        query: The search string sent as `q=...`.
        include: Comma-separated entity types to include (default covers
            contacts, sales accounts, and deals).
        page, page_size: Pagination, page_size capped at MCP_TOOL_RESULT_LIMIT.

    Returns:
        IntegrationResult dict with `data.results`, `data.count`, the original
        `data.query`, plus pagination fields.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "freshsales_search",
            context.permissions,
        )
        settings = (await resolve_settings("freshsales")).settings
        if not settings.freshsales_configured:
            return not_configured(
                "freshsales", "FRESHSALES_DOMAIN/FRESHSALES_API_KEY are not configured."
            )
        service = FreshsalesService(settings)
        try:
            return await _call(
                service.search(
                    query=query, include=include, page=page, page_size=page_size
                )
            )
        finally:
            await service.aclose()


def register(mcp: Any) -> None:
    """Register Freshsales MCP tools."""

    mcp.tool()(freshsales_get_contacts)
    mcp.tool()(freshsales_get_accounts)
    mcp.tool()(freshsales_get_deals)
    mcp.tool()(freshsales_search)
