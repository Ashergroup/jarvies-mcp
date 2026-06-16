"""Cin7 MCP tools — read-only inventory and order access.

Auth: Cin7 Core (formerly DEAR Systems) uses two custom headers,
`api-auth-accountid` and `api-auth-applicationkey`. There is no OAuth dance,
so a plain `httpx.AsyncClient` with the headers preset is enough.

Base URL: the DEAR-style auth headers correspond to the External API v2 at
`https://inventory.dearsystems.com/ExternalApi/v2`. Endpoint paths are
case-sensitive (e.g. `saleList`, `purchaseList`, `ProductAvailability`,
`ref/productavailability`) and live at the API root — there is no `/v1/`
prefix. Env var names match finpilot-agent so existing `.env` files are
compatible (override `CIN7_BASE_URL` if your account uses a different host).
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


class Cin7Service:
    """Read-only Cin7 Core API client.

    One `httpx.AsyncClient` per service instance, created lazily and reused
    across calls. Errors from `httpx` are surfaced to the caller via the
    `_call` wrapper in the MCP tool layer.
    """

    def __init__(self, settings: MCPSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None

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
            "api-auth-accountid": self._settings.cin7_account_id,
            "api-auth-applicationkey": self._settings.cin7_api_key,
        }

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        client = await self._http()
        url = f"{self._settings.cin7_base_url.rstrip('/')}/{path.lstrip('/')}"
        started = time.perf_counter()
        response = await client.get(
            url,
            headers=self._headers(),
            params={k: v for k, v in (params or {}).items() if v is not None},
        )
        latency_ms = (time.perf_counter() - started) * 1000
        log.info(
            "cin7_api_call",
            extra={
                "method": "GET",
                "path": f"/{path.lstrip('/')}",
                "status": response.status_code,
                "latency_ms": round(latency_ms, 1),
            },
        )
        response.raise_for_status()
        return response.json()

    async def get_inventory(
        self,
        sku: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        # DEAR/Cin7 Core has a single availability endpoint (`/ref/productavailability`)
        # which `get_stock_levels` uses. For "inventory" we hit the product master
        # catalog (`/product`) which lists SKUs, names, pricing and status — the data
        # most callers mean when they say "inventory".
        size = _resolve_page_size(page_size, self._settings)
        payload = await self._get(
            "product",
            params={"Page": page, "Limit": size, "Sku": sku},
        )
        items = _coerce_list(payload, "Products")[:size]
        return {"inventory": items, "count": len(items), "page": page, "page_size": size}

    async def get_stock_levels(
        self,
        location_id: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        size = _resolve_page_size(page_size, self._settings)
        payload = await self._get(
            "ref/productavailability",
            params={"Page": page, "Limit": size, "Location": location_id},
        )
        items = _coerce_list(payload, "ProductAvailabilityList")[:size]
        return {
            "stock_levels": items,
            "count": len(items),
            "page": page,
            "page_size": size,
            "location_id": location_id,
        }

    async def get_sales_orders(
        self,
        status: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        size = _resolve_page_size(page_size, self._settings)
        payload = await self._get(
            "saleList",
            params={
                "Page": page,
                "Limit": size,
                "Status": status,
                "UpdatedSince": date_from,
                "CreatedBefore": date_to,
            },
        )
        items = _coerce_list(payload, "SaleList")[:size]
        return {"sales_orders": items, "count": len(items), "page": page, "page_size": size}

    async def get_purchase_orders(
        self,
        status: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        size = _resolve_page_size(page_size, self._settings)
        payload = await self._get(
            "purchaseList",
            params={
                "Page": page,
                "Limit": size,
                "Status": status,
                "UpdatedSince": date_from,
                "CreatedBefore": date_to,
            },
        )
        items = _coerce_list(payload, "PurchaseList")[:size]
        return {"purchase_orders": items, "count": len(items), "page": page, "page_size": size}


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


async def _call(coro, source: str = "cin7") -> dict[str, Any]:
    try:
        data = await coro
    except httpx.HTTPStatusError as exc:
        return integration_error(source, f"Cin7 API returned HTTP {exc.response.status_code}")
    except httpx.RequestError as exc:
        return integration_error(source, f"Cin7 request failed: {exc.__class__.__name__}")
    return ok(source, data)


async def cin7_get_inventory(
    sku: str | None = None,
    page: int = 1,
    page_size: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return Cin7 product availability rows.

    Args:
        sku: Optional SKU filter; omit for all products.
        page: 1-based page index.
        page_size: Cap on returned rows; defaults to MCP_TOOL_RESULT_LIMIT.

    Returns:
        IntegrationResult dict with `data.inventory`, `data.count`, plus
        pagination fields, or `status=not_configured` when credentials are missing.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "cin7_get_inventory",
            context.permissions,
        )
        settings = (await resolve_settings("cin7")).settings
        if not settings.cin7_configured:
            return not_configured("cin7", "CIN7_API_KEY/CIN7_ACCOUNT_ID are not configured.")
        service = Cin7Service(settings)
        try:
            return await _call(service.get_inventory(sku=sku, page=page, page_size=page_size))
        finally:
            await service.aclose()


async def cin7_get_stock_levels(
    location_id: str | None = None,
    page: int = 1,
    page_size: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return Cin7 stock levels grouped by location.

    Args:
        location_id: Optional location filter; omit for all locations.
        page, page_size: Pagination, page_size capped at MCP_TOOL_RESULT_LIMIT.

    Returns:
        IntegrationResult dict with `data.stock_levels`, `data.count`, plus
        pagination fields and the requested `data.location_id`.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "cin7_get_stock_levels",
            context.permissions,
        )
        settings = (await resolve_settings("cin7")).settings
        if not settings.cin7_configured:
            return not_configured("cin7", "CIN7_API_KEY/CIN7_ACCOUNT_ID are not configured.")
        service = Cin7Service(settings)
        try:
            return await _call(
                service.get_stock_levels(
                    location_id=location_id, page=page, page_size=page_size
                )
            )
        finally:
            await service.aclose()


async def cin7_get_sales_orders(
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return Cin7 sales orders, optionally filtered.

    Args:
        status: Cin7 sale status (e.g. DRAFT, AUTHORISED, PARKED, VOIDED).
        date_from: ISO date `YYYY-MM-DD` — sales updated on or after this date.
        date_to: ISO date `YYYY-MM-DD` — sales created on or before this date.
        page, page_size: Pagination, page_size capped at MCP_TOOL_RESULT_LIMIT.

    Returns:
        IntegrationResult dict with `data.sales_orders`, `data.count`, plus
        pagination fields.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "cin7_get_sales_orders",
            context.permissions,
        )
        settings = (await resolve_settings("cin7")).settings
        if not settings.cin7_configured:
            return not_configured("cin7", "CIN7_API_KEY/CIN7_ACCOUNT_ID are not configured.")
        service = Cin7Service(settings)
        try:
            return await _call(
                service.get_sales_orders(
                    status=status,
                    date_from=date_from,
                    date_to=date_to,
                    page=page,
                    page_size=page_size,
                )
            )
        finally:
            await service.aclose()


async def cin7_get_purchase_orders(
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return Cin7 purchase orders, optionally filtered.

    Args:
        status: Cin7 purchase status (e.g. DRAFT, AUTHORISED, ORDERED, RECEIVED).
        date_from: ISO date `YYYY-MM-DD` — purchases updated on or after this date.
        date_to: ISO date `YYYY-MM-DD` — purchases created on or before this date.
        page, page_size: Pagination, page_size capped at MCP_TOOL_RESULT_LIMIT.

    Returns:
        IntegrationResult dict with `data.purchase_orders`, `data.count`, plus
        pagination fields.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "cin7_get_purchase_orders",
            context.permissions,
        )
        settings = (await resolve_settings("cin7")).settings
        if not settings.cin7_configured:
            return not_configured("cin7", "CIN7_API_KEY/CIN7_ACCOUNT_ID are not configured.")
        service = Cin7Service(settings)
        try:
            return await _call(
                service.get_purchase_orders(
                    status=status,
                    date_from=date_from,
                    date_to=date_to,
                    page=page,
                    page_size=page_size,
                )
            )
        finally:
            await service.aclose()


def register(mcp: Any) -> None:
    """Register Cin7 MCP tools."""

    mcp.tool()(cin7_get_inventory)
    mcp.tool()(cin7_get_stock_levels)
    mcp.tool()(cin7_get_sales_orders)
    mcp.tool()(cin7_get_purchase_orders)
