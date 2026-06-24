"""Freshsales MCP write tools — full CRM read/write for client-journey management.

Companion to ``freshsales_tools.py`` (read-only). These tools add create/update
across contacts, deals, accounts, notes, tasks, plus deal-stage lookup and two
composite read helpers (contact journey, contact search).

Everything reuses the read module's primitives so behaviour stays identical:

- Auth: ``Authorization: Token token={api_key}`` header (no OAuth/refresh).
- Base URL: ``https://{FRESHSALES_DOMAIN}/crm/sales/api`` (see ``FreshsalesService``).
- Credentials: resolved DB-first via ``credentials.resolve_settings("freshsales")``
  — the tenant's ``tenant_credentials`` row when present, else env vars.
- Return shape: ``{"status": ..., "source": "freshsales", "data": {...}}`` via the
  shared ``integrations`` envelope; HTTP/transport errors become an error envelope.

Freshworks CRM API notes / limitations found (flagged for the operator):
- Account creation hits ``/accounts`` per spec; the Freshworks entity is actually
  ``sales_account`` (the read tool lists ``/sales_accounts``). The request body is
  wrapped as ``{"sales_account": {...}}`` accordingly.
- A contact has no single ``phone`` field — ``phone`` is sent as ``mobile_number``.
- ``company_name`` on a contact and ``industry`` on an account are passed through
  as-is; Freshworks may instead expect a linked ``sales_account`` / an
  ``industry_type_id``. Verify against the live workspace schema.
- A deal links a contact via ``contacts_added_list`` and an account via
  ``sales_account_id`` (there is no scalar ``contact_id`` on a deal).
- Deal stages are read from ``/deal_stages``; some Freshworks tenants expose them
  under ``/selector/deal_stages`` instead.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agents.mcp.credentials import resolve_settings
from agents.mcp.integrations import not_configured
from agents.mcp.permissions import check_permission
from agents.mcp.tenant_context import use_tenant_context
from agents.mcp.tools.freshsales_tools import FreshsalesService, _call, _context

log = logging.getLogger(__name__)

_NOT_CONFIGURED = "FRESHSALES_DOMAIN/FRESHSALES_API_KEY are not configured."


class FreshsalesWriteService(FreshsalesService):
    """Adds POST/PUT verbs on top of the shared read-only Freshsales client."""

    async def _send(
        self, method: str, path: str, json_body: dict[str, Any] | None = None
    ) -> Any:
        client = await self._http()
        url = f"{self.base_url}/{path.lstrip('/')}"
        started = time.perf_counter()
        response = await client.request(
            method,
            url,
            headers={**self._headers(), "Content-Type": "application/json"},
            json=json_body,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        log.info(
            "freshsales_api_call",
            extra={
                "method": method,
                "path": f"/{path.lstrip('/')}",
                "status": response.status_code,
                "latency_ms": round(latency_ms, 1),
            },
        )
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()

    # --- writes -----------------------------------------------------------
    async def create_contact(self, body: dict[str, Any]) -> Any:
        return await self._send("POST", "contacts", {"contact": body})

    async def update_contact(self, contact_id: str, body: dict[str, Any]) -> Any:
        return await self._send("PUT", f"contacts/{contact_id}", {"contact": body})

    async def create_deal(self, body: dict[str, Any]) -> Any:
        return await self._send("POST", "deals", {"deal": body})

    async def update_deal(self, deal_id: str, body: dict[str, Any]) -> Any:
        return await self._send("PUT", f"deals/{deal_id}", {"deal": body})

    async def create_account(self, body: dict[str, Any]) -> Any:
        return await self._send("POST", "accounts", {"sales_account": body})

    async def create_note(self, body: dict[str, Any]) -> Any:
        return await self._send("POST", "notes", {"note": body})

    async def create_task(self, body: dict[str, Any]) -> Any:
        return await self._send("POST", "tasks", {"task": body})

    # --- composite reads --------------------------------------------------
    async def get_deal_stages(self) -> Any:
        return await self._get("deal_stages")

    async def get_contact_journey(self, contact_id: str) -> Any:
        return await self._get(
            f"contacts/{contact_id}",
            params={"include": "deals,notes,tasks,appointments"},
        )

    async def search_contacts(self, query: str, per_page: int) -> Any:
        return await self._get(
            "contacts/search",
            params={"q": query, "include": "deals", "per_page": per_page},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compact(body: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is ``None`` so optional params are omitted."""

    return {k: v for k, v in body.items() if v is not None}


def _unwrap(payload: Any, key: str) -> dict[str, Any]:
    """Return ``payload[key]`` when present, else the payload itself."""

    if isinstance(payload, dict):
        inner = payload.get(key)
        if isinstance(inner, dict):
            return inner
        return payload
    return {}


def _full_name(contact: dict[str, Any]) -> str | None:
    display = contact.get("display_name")
    if display:
        return display
    parts = [contact.get("first_name"), contact.get("last_name")]
    joined = " ".join(p for p in parts if p)
    return joined or None


def _coerce_list(payload: Any, key: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


async def _run(tool_name: str, ctx: tuple, action) -> dict[str, Any]:
    """Shared boilerplate: permission gate, credential resolution, dispatch.

    ``ctx`` is ``(tenant_id, user_id, access_token, permissions)``. ``action`` is
    a callable taking the service and returning a coroutine that yields the
    ``data`` payload; ``_call`` wraps it in the ok/error envelope.
    """

    context = _context(*ctx)
    with use_tenant_context(context):
        check_permission(context.tenant_id, context.user_id, tool_name, context.permissions)
        settings = (await resolve_settings("freshsales")).settings
        if not settings.freshsales_configured:
            return not_configured("freshsales", _NOT_CONFIGURED)
        service = FreshsalesWriteService(settings)
        try:
            return await _call(action(service))
        finally:
            await service.aclose()


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


async def freshsales_create_contact(
    first_name: str,
    last_name: str,
    email: str,
    phone: str | None = None,
    company_name: str | None = None,
    job_title: str | None = None,
    custom_fields: dict[str, Any] | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a Freshsales contact (``POST /contacts``).

    Returns the new contact's ``id``, ``name``, and ``email``.
    """

    body = _compact(
        {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "mobile_number": phone,
            "company_name": company_name,
            "job_title": job_title,
            "custom_field": custom_fields,
        }
    )

    async def _action(service: FreshsalesWriteService) -> dict[str, Any]:
        contact = _unwrap(await service.create_contact(body), "contact")
        return {
            "id": contact.get("id"),
            "name": _full_name(contact),
            "email": contact.get("email"),
        }

    return await _run(
        "freshsales_create_contact",
        (tenant_id, user_id, access_token, permissions),
        _action,
    )


async def freshsales_update_contact(
    contact_id: str,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    job_title: str | None = None,
    custom_fields: dict[str, Any] | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Update a Freshsales contact (``PUT /contacts/{id}``). Returns the contact."""

    body = _compact(
        {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "mobile_number": phone,
            "job_title": job_title,
            "custom_field": custom_fields,
        }
    )

    async def _action(service: FreshsalesWriteService) -> dict[str, Any]:
        contact = _unwrap(await service.update_contact(contact_id, body), "contact")
        return {"contact": contact}

    return await _run(
        "freshsales_update_contact",
        (tenant_id, user_id, access_token, permissions),
        _action,
    )


# ---------------------------------------------------------------------------
# Deals
# ---------------------------------------------------------------------------


async def freshsales_create_deal(
    name: str,
    amount: float,
    contact_id: str | None = None,
    account_id: str | None = None,
    expected_close: str | None = None,
    deal_stage_id: str | None = None,
    owner_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a Freshsales deal (``POST /deals``).

    Returns the new deal's ``id``, ``name``, and ``amount``.
    """

    body = _compact(
        {
            "name": name,
            "amount": amount,
            # A deal links contacts via a list, not a scalar field.
            "contacts_added_list": [contact_id] if contact_id else None,
            "sales_account_id": account_id,
            "expected_close": expected_close,
            "deal_stage_id": deal_stage_id,
            "owner_id": owner_id,
        }
    )

    async def _action(service: FreshsalesWriteService) -> dict[str, Any]:
        deal = _unwrap(await service.create_deal(body), "deal")
        return {
            "id": deal.get("id"),
            "name": deal.get("name"),
            "amount": deal.get("amount"),
        }

    return await _run(
        "freshsales_create_deal",
        (tenant_id, user_id, access_token, permissions),
        _action,
    )


async def freshsales_update_deal(
    deal_id: str,
    name: str | None = None,
    amount: float | None = None,
    expected_close: str | None = None,
    deal_stage_id: str | None = None,
    owner_id: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Update a Freshsales deal (``PUT /deals/{id}``). Returns the deal."""

    body = _compact(
        {
            "name": name,
            "amount": amount,
            "expected_close": expected_close,
            "deal_stage_id": deal_stage_id,
            "owner_id": owner_id,
        }
    )

    async def _action(service: FreshsalesWriteService) -> dict[str, Any]:
        deal = _unwrap(await service.update_deal(deal_id, body), "deal")
        return {"deal": deal}

    return await _run(
        "freshsales_update_deal",
        (tenant_id, user_id, access_token, permissions),
        _action,
    )


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


async def freshsales_create_account(
    name: str,
    website: str | None = None,
    phone: str | None = None,
    industry: str | None = None,
    number_of_employees: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a Freshsales account (``POST /accounts``). Returns ``id`` and ``name``."""

    body = _compact(
        {
            "name": name,
            "website": website,
            "phone": phone,
            "industry": industry,
            "number_of_employees": number_of_employees,
        }
    )

    async def _action(service: FreshsalesWriteService) -> dict[str, Any]:
        account = _unwrap(await service.create_account(body), "sales_account")
        return {"id": account.get("id"), "name": account.get("name")}

    return await _run(
        "freshsales_create_account",
        (tenant_id, user_id, access_token, permissions),
        _action,
    )


# ---------------------------------------------------------------------------
# Notes & tasks
# ---------------------------------------------------------------------------


async def freshsales_create_note(
    description: str,
    targetable_type: str,
    targetable_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Attach a note to a Contact/Deal/Account (``POST /notes``). Returns ``id``."""

    body = {
        "description": description,
        "targetable_type": targetable_type,
        "targetable_id": targetable_id,
    }

    async def _action(service: FreshsalesWriteService) -> dict[str, Any]:
        note = _unwrap(await service.create_note(body), "note")
        return {"id": note.get("id")}

    return await _run(
        "freshsales_create_note",
        (tenant_id, user_id, access_token, permissions),
        _action,
    )


async def freshsales_create_task(
    title: str,
    due_date: str,
    owner_id: str,
    targetable_type: str | None = None,
    targetable_id: str | None = None,
    description: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Create a Freshsales task (``POST /tasks``). Returns ``id`` and ``title``."""

    body = _compact(
        {
            "title": title,
            "due_date": due_date,
            "owner_id": owner_id,
            "targetable_type": targetable_type,
            "targetable_id": targetable_id,
            "description": description,
        }
    )

    async def _action(service: FreshsalesWriteService) -> dict[str, Any]:
        task = _unwrap(await service.create_task(body), "task")
        return {"id": task.get("id"), "title": task.get("title")}

    return await _run(
        "freshsales_create_task",
        (tenant_id, user_id, access_token, permissions),
        _action,
    )


# ---------------------------------------------------------------------------
# Lookups & composites
# ---------------------------------------------------------------------------


async def freshsales_get_deal_stages(
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """List deal stages (``GET /deal_stages``) — ``id``, ``name``, ``position``."""

    async def _action(service: FreshsalesWriteService) -> dict[str, Any]:
        payload = await service.get_deal_stages()
        stages = [
            {
                "id": s.get("id"),
                "name": s.get("name"),
                "position": s.get("position"),
            }
            for s in _coerce_list(payload, "deal_stages")
            if isinstance(s, dict)
        ]
        return {"deal_stages": stages, "count": len(stages)}

    return await _run(
        "freshsales_get_deal_stages",
        (tenant_id, user_id, access_token, permissions),
        _action,
    )


async def freshsales_get_contact_journey(
    contact_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Full client journey for one contact.

    Composite read: ``GET /contacts/{id}?include=deals,notes,tasks,appointments``.
    Returns ``{"contact", "deals", "notes", "tasks", "appointments"}``. The
    included collections are sideloaded by Freshworks at the top level; we also
    look inside the contact object as a fallback.
    """

    async def _action(service: FreshsalesWriteService) -> dict[str, Any]:
        payload = await service.get_contact_journey(contact_id)
        contact = _unwrap(payload, "contact")
        top = payload if isinstance(payload, dict) else {}

        def _pick(key: str) -> list[Any]:
            return _coerce_list(top, key) or _coerce_list(contact, key)

        return {
            "contact": contact,
            "deals": _pick("deals"),
            "notes": _pick("notes"),
            "tasks": _pick("tasks"),
            "appointments": _pick("appointments"),
        }

    return await _run(
        "freshsales_get_contact_journey",
        (tenant_id, user_id, access_token, permissions),
        _action,
    )


async def freshsales_search_contacts(
    query: str,
    limit: int = 10,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Search contacts with their deals (``GET /contacts/search?q=...&include=deals``).

    Returns matching contacts (each carrying its associated deals) and a count.
    """

    per_page = max(1, int(limit))

    async def _action(service: FreshsalesWriteService) -> dict[str, Any]:
        payload = await service.search_contacts(query, per_page)
        contacts = (
            payload if isinstance(payload, list) else _coerce_list(payload, "contacts")
        )
        contacts = contacts[:per_page]
        return {"contacts": contacts, "count": len(contacts), "query": query}

    return await _run(
        "freshsales_search_contacts",
        (tenant_id, user_id, access_token, permissions),
        _action,
    )


def register(mcp: Any) -> None:
    """Register Freshsales write/CRM MCP tools."""

    mcp.tool()(freshsales_create_contact)
    mcp.tool()(freshsales_update_contact)
    mcp.tool()(freshsales_create_deal)
    mcp.tool()(freshsales_update_deal)
    mcp.tool()(freshsales_create_account)
    mcp.tool()(freshsales_create_note)
    mcp.tool()(freshsales_create_task)
    mcp.tool()(freshsales_get_deal_stages)
    mcp.tool()(freshsales_get_contact_journey)
    mcp.tool()(freshsales_search_contacts)
