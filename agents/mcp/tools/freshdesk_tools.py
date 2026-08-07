"""Freshdesk MCP tools — read-only helpdesk access.

Freshdesk (helpdesk) is a different product from Freshsales (CRM, see
``freshsales_tools``): separate API, separate host, separate credentials. The two
share nothing but the vendor name.

Auth: HTTP basic with the API key as the username and the literal ``X`` as the
password (confirmed against the current Freshdesk v2 reference). Base URL is
``https://{domain}/api/v2``, where ``FRESHDESK_DOMAIN`` may be a bare subdomain
(``acme``, expanded to ``acme.freshdesk.com``) or a full host.

Four v2 API constraints shape this module, all confirmed in the current docs:

* ``GET /tickets`` filters on ``requester_id`` / ``email`` / ``company_id`` /
  ``updated_since`` only — NOT on status, priority, or agent. Those live on the
  filter endpoint ``GET /search/tickets?query="..."``, so
  ``freshdesk_list_tickets`` chooses its endpoint from the arguments given and
  reports which one it used in ``data.endpoint``.
* The filter endpoint is fixed at 30 results per page, capped at 10 pages,
  excludes archived tickets, and lags writes by a few minutes (index delay).
* There is NO public full-text search over subject/description. The Freshdesk UI
  has it; the v2 API does not expose it. ``freshdesk_search_tickets`` therefore
  scans pages of ``GET /tickets?include=description`` and matches locally,
  returning what it scanned so the caller can see the bound it worked within.
* ``GET /tickets`` returns only the last 30 days of tickets unless
  ``updated_since`` is supplied.

Reads only. No ticket creation, no replies or notes, no status/priority/assignee
changes — every tool here is ``write=False`` under the ``support_access`` scope.
Those writes live in the companion ``freshdesk_write_tools``, which is where the
per-tenant reply guardrail and the audit trail sit.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from agents.mcp.config import MCPSettings, get_settings
from agents.mcp.credentials import resolve_settings
from agents.mcp.integrations import error as integration_error
from agents.mcp.integrations import not_configured, ok
from agents.mcp.permissions import check_permission
from agents.mcp.tenant_context import build_tenant_context, use_tenant_context

log = logging.getLogger(__name__)

# Freshdesk encodes status and priority as integers. Callers may pass either the
# integer or the name.
STATUS_CODES: dict[str, int] = {"open": 2, "pending": 3, "resolved": 4, "closed": 5}
PRIORITY_CODES: dict[str, int] = {"low": 1, "medium": 2, "high": 3, "urgent": 4}
# Neither resolved nor closed: the set an SLA can still be breached against.
UNRESOLVED_STATUS_CODES = (STATUS_CODES["open"], STATUS_CODES["pending"])

MAX_PER_PAGE = 100  # GET /tickets hard ceiling.
FILTER_PER_PAGE = 30  # GET /search/tickets fixed page size.
FILTER_MAX_PAGES = 10  # GET /search/tickets hard page cap.
SEARCH_MAX_SCAN_PAGES = 10  # Self-imposed ceiling on the local-match scan.

NO_FULLTEXT_NOTE = (
    "Freshdesk's v2 API exposes no full-text ticket search; subject/description "
    "matching is done locally over the pages scanned."
)


class FreshdeskService:
    """Read-only Freshdesk helpdesk client.

    One ``httpx.AsyncClient`` per service instance, created lazily, carrying the
    basic-auth pair for the lifetime of the instance.
    """

    def __init__(self, settings: MCPSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        return f"https://{_normalise_domain(self._settings.freshdesk_domain)}/api/v2"

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._settings.integration_http_timeout_seconds,
                # API key as username, literal "X" as password.
                auth=(self._settings.freshdesk_api_key, "X"),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        client = await self._http()
        url = f"{self.base_url}/{path.lstrip('/')}"
        started = time.perf_counter()
        response = await client.get(
            url,
            headers={"Accept": "application/json"},
            params={k: v for k, v in (params or {}).items() if v is not None},
        )
        latency_ms = (time.perf_counter() - started) * 1000
        log.info(
            "freshdesk_api_call",
            extra={
                "method": "GET",
                "path": f"/{path.lstrip('/')}",
                "status": response.status_code,
                "latency_ms": round(latency_ms, 1),
            },
        )
        response.raise_for_status()
        return response.json()

    # -- tickets ------------------------------------------------------------

    async def list_tickets(
        self,
        status: str | int | None = None,
        priority: str | int | None = None,
        agent_id: str | int | None = None,
        requester_id: str | int | None = None,
        updated_since: str | None = None,
        page: int = 1,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """List tickets, routing to whichever endpoint supports the filters given."""

        size = _resolve_page_size(page_size, self._settings)
        status_code = _coerce_code(status, STATUS_CODES, "status")
        priority_code = _coerce_code(priority, PRIORITY_CODES, "priority")

        if status_code or priority_code or agent_id:
            return await self._filter_tickets(
                status_code=status_code,
                priority_code=priority_code,
                agent_id=agent_id,
                requester_id=requester_id,
                updated_since=updated_since,
                page=page,
                size=size,
            )

        payload = await self._get(
            "tickets",
            params={
                "requester_id": requester_id,
                "updated_since": updated_since,
                "page": page,
                "per_page": min(size, MAX_PER_PAGE),
                "order_by": "updated_at",
                "order_type": "desc",
            },
        )
        tickets = _coerce_list(payload, "tickets")[:size]
        return {
            "tickets": tickets,
            "count": len(tickets),
            "page": page,
            "page_size": min(size, MAX_PER_PAGE),
            "endpoint": "tickets",
            "requester_filtered_locally": False,
        }

    async def _filter_tickets(
        self,
        *,
        status_code: int | None,
        priority_code: int | None,
        agent_id: str | int | None,
        requester_id: str | int | None,
        updated_since: str | None,
        page: int,
        size: int,
    ) -> dict[str, Any]:
        """Use ``GET /search/tickets`` for status/priority/agent filtering.

        ``requester_id`` is not a filter-API field, so when it is combined with
        one of those filters it is applied locally to the returned page and the
        result flags ``requester_filtered_locally``.
        """

        clauses: list[str] = []
        if status_code:
            clauses.append(f"status:{status_code}")
        if priority_code:
            clauses.append(f"priority:{priority_code}")
        if agent_id:
            clauses.append(f"agent_id:{agent_id}")
        if updated_since:
            clauses.append(f"updated_at:>'{_as_date(updated_since)}'")
        query = " AND ".join(clauses)

        capped_page = max(1, min(int(page), FILTER_MAX_PAGES))
        payload = await self._get(
            "search/tickets",
            # Freshdesk requires the query itself to be double-quoted.
            params={"query": f'"{query}"', "page": capped_page},
        )
        results = _coerce_list(payload, "results")
        total = payload.get("total") if isinstance(payload, dict) else None

        filtered_locally = False
        if requester_id:
            results = [
                t
                for t in results
                if str(t.get("requester_id")) == str(requester_id)
            ]
            filtered_locally = True

        tickets = results[: min(size, FILTER_PER_PAGE)]
        return {
            "tickets": tickets,
            "count": len(tickets),
            "total": total,
            "page": capped_page,
            "page_size": min(size, FILTER_PER_PAGE),
            "endpoint": "search/tickets",
            "query": query,
            "page_limit": FILTER_MAX_PAGES,
            "requester_filtered_locally": filtered_locally,
        }

    async def get_ticket(
        self,
        ticket_id: str | int,
        include_conversations: bool = True,
        conversation_limit: int | None = None,
    ) -> dict[str, Any]:
        """Return one ticket plus its conversation thread.

        The thread comes from ``/tickets/{id}/conversations`` rather than the
        ``include=conversations`` embed, because the embed silently caps at ten
        entries.
        """

        ticket = await self._get(f"tickets/{ticket_id}")
        conversations: list[Any] = []
        if include_conversations:
            size = min(
                _resolve_page_size(conversation_limit, self._settings), MAX_PER_PAGE
            )
            payload = await self._get(
                f"tickets/{ticket_id}/conversations",
                params={"page": 1, "per_page": size},
            )
            conversations = _coerce_list(payload, "conversations")[:size]

        return {
            "ticket": ticket,
            "conversations": conversations,
            "conversation_count": len(conversations),
        }

    async def search_tickets(
        self,
        query: str,
        updated_since: str | None = None,
        max_pages: int = 3,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """Substring-match ``query`` against ticket subject and description.

        Scans up to ``max_pages`` pages of 100 tickets each (newest first) and
        matches locally — see ``NO_FULLTEXT_NOTE``. The result reports how much
        was scanned and whether the scan reached the end of the ticket list, so
        an empty result is never mistaken for "no such ticket exists".
        """

        needle = (query or "").strip().lower()
        if not needle:
            raise ValueError("query cannot be empty")

        size = _resolve_page_size(page_size, self._settings)
        pages = max(1, min(int(max_pages), SEARCH_MAX_SCAN_PAGES))
        matches: list[Any] = []
        scanned = 0
        pages_scanned = 0
        scan_exhausted = False

        for page in range(1, pages + 1):
            payload = await self._get(
                "tickets",
                params={
                    "include": "description",
                    "updated_since": updated_since,
                    "page": page,
                    "per_page": MAX_PER_PAGE,
                    "order_by": "updated_at",
                    "order_type": "desc",
                },
            )
            batch = _coerce_list(payload, "tickets")
            scanned += len(batch)
            pages_scanned = page
            matches.extend(t for t in batch if _ticket_matches(t, needle))
            if len(batch) < MAX_PER_PAGE:
                scan_exhausted = True
                break

        tickets = matches[:size]
        return {
            "tickets": tickets,
            "count": len(tickets),
            "match_count": len(matches),
            "query": query,
            "scanned": scanned,
            "pages_scanned": pages_scanned,
            "scan_exhausted": scan_exhausted,
            "truncated": not scan_exhausted or len(matches) > len(tickets),
            "note": NO_FULLTEXT_NOTE,
        }

    async def ticket_summary(self, as_of: str | None = None) -> dict[str, Any]:
        """Return ticket counts by status and priority, plus SLA breach counts.

        Counts are the filter endpoint's own ``total`` for one query per status
        and per priority — authoritative totals, not a sample of a scanned page.
        Overdue is measured as ``due_by`` (resolution) and ``fr_due_by`` (first
        response) earlier than ``as_of``, restricted to open/pending tickets.
        Ten API calls per invocation.
        """

        reference = _as_date(as_of) if as_of else _today_utc()
        unresolved = " OR ".join(
            f"status:{code}" for code in UNRESOLVED_STATUS_CODES
        )

        by_status = {
            name: await self._filter_total(f"status:{code}")
            for name, code in STATUS_CODES.items()
        }
        by_priority = {
            name: await self._filter_total(f"priority:{code}")
            for name, code in PRIORITY_CODES.items()
        }
        overdue = await self._filter_total(
            f"({unresolved}) AND due_by:<'{reference}'"
        )
        first_response_overdue = await self._filter_total(
            f"({unresolved}) AND fr_due_by:<'{reference}'"
        )

        return {
            "by_status": by_status,
            "by_priority": by_priority,
            "open_and_pending": _sum_counts(by_status, ("open", "pending")),
            "overdue": overdue,
            "first_response_overdue": first_response_overdue,
            "as_of": reference,
            "sla_basis": (
                "due_by / fr_due_by earlier than as_of, open and pending tickets "
                "only. The Freshdesk filter API compares dates, not timestamps, "
                "and excludes archived tickets."
            ),
        }

    async def _filter_total(self, query: str) -> int | None:
        """Return the filter endpoint's ``total`` for one query."""

        payload = await self._get("search/tickets", params={"query": f'"{query}"'})
        if isinstance(payload, dict) and isinstance(payload.get("total"), int):
            return payload["total"]
        return len(_coerce_list(payload, "results"))

    # -- agents -------------------------------------------------------------

    async def list_agents(
        self, page: int = 1, page_size: int | None = None
    ) -> dict[str, Any]:
        size = _resolve_page_size(page_size, self._settings)
        payload = await self._get(
            "agents",
            params={"page": page, "per_page": min(size, MAX_PER_PAGE)},
        )
        agents = _coerce_list(payload, "agents")[:size]
        return {
            "agents": agents,
            "count": len(agents),
            "page": page,
            "page_size": min(size, MAX_PER_PAGE),
        }

    # -- groups -------------------------------------------------------------

    async def list_groups(
        self, page: int = 1, page_size: int | None = None
    ) -> dict[str, Any]:
        """List agent groups — the id/name pairs the escalation policy needs."""

        size = _resolve_page_size(page_size, self._settings)
        payload = await self._get(
            "groups",
            params={"page": page, "per_page": min(size, MAX_PER_PAGE)},
        )
        groups = _coerce_list(payload, "groups")[:size]
        return {
            "groups": groups,
            "count": len(groups),
            "page": page,
            "page_size": min(size, MAX_PER_PAGE),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_domain(domain: str) -> str:
    """Return the Freshdesk host for a configured domain value.

    Accepts ``acme``, ``acme.freshdesk.com``, or ``https://acme.freshdesk.com/``.
    """

    host = (domain or "").strip().rstrip("/")
    host = host.split("://", 1)[-1].split("/", 1)[0]
    if host and "." not in host:
        host = f"{host}.freshdesk.com"
    return host


def _coerce_code(
    value: str | int | None, mapping: dict[str, int], kind: str
) -> int | None:
    """Return the Freshdesk integer code for a name or integer, or None."""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"Unsupported Freshdesk {kind}: {value!r}")
    if isinstance(value, int):
        code = value
    else:
        text = str(value).strip().lower()
        if text.isdigit():
            code = int(text)
        elif text in mapping:
            return mapping[text]
        else:
            allowed = ", ".join(sorted(mapping))
            raise ValueError(f"Unknown Freshdesk {kind} '{value}' — expected one of: {allowed}")
    if code not in mapping.values():
        allowed = ", ".join(str(v) for v in sorted(mapping.values()))
        raise ValueError(f"Unknown Freshdesk {kind} code {code} — expected one of: {allowed}")
    return code


def _as_date(value: str) -> str:
    """Return the ``YYYY-MM-DD`` prefix of an ISO date/datetime string.

    The filter endpoint compares dates only, so a timestamp is truncated rather
    than silently rejected by Freshdesk.
    """

    return str(value).strip()[:10]


def _today_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _coerce_list(payload: Any, key: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        # The filter endpoint returns its rows under "results".
        results = payload.get("results")
        if isinstance(results, list):
            return results
    return []


def _ticket_matches(ticket: Any, needle: str) -> bool:
    if not isinstance(ticket, dict):
        return False
    haystack = " ".join(
        str(ticket.get(field) or "")
        for field in ("subject", "description_text", "description")
    )
    return needle in haystack.lower()


def _sum_counts(counts: dict[str, int | None], keys: tuple[str, ...]) -> int:
    return sum(counts.get(key) or 0 for key in keys)


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


async def _call(coro, source: str = "freshdesk") -> dict[str, Any]:
    try:
        data = await coro
    except ValueError as exc:
        return integration_error(source, str(exc))
    except httpx.HTTPStatusError as exc:
        return integration_error(
            source, f"Freshdesk API returned HTTP {exc.response.status_code}"
        )
    except httpx.RequestError as exc:
        return integration_error(source, f"Freshdesk request failed: {exc.__class__.__name__}")
    return ok(source, data)


async def _service() -> FreshdeskService | None:
    """Return a configured service, or None when credentials are missing.

    Credentials resolve through the shared per-tenant resolver: the tenant's
    ``tenant_credentials`` row when present, otherwise ``FRESHDESK_DOMAIN`` /
    ``FRESHDESK_API_KEY`` from the environment.
    """

    settings = (await resolve_settings("freshdesk")).settings
    if not settings.freshdesk_configured:
        return None
    return FreshdeskService(settings)


def _not_configured() -> dict[str, Any]:
    return not_configured(
        "freshdesk", "FRESHDESK_DOMAIN/FRESHDESK_API_KEY are not configured."
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def freshdesk_list_tickets(
    status: str | int | None = None,
    priority: str | int | None = None,
    agent_id: str | int | None = None,
    requester_id: str | int | None = None,
    updated_since: str | None = None,
    page: int = 1,
    page_size: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return Freshdesk tickets, filtered and paginated.

    Args:
        status: `open`, `pending`, `resolved`, `closed`, or the Freshdesk
            integer (2/3/4/5).
        priority: `low`, `medium`, `high`, `urgent`, or the integer (1/2/3/4).
        agent_id: Assigned agent (responder) ID.
        requester_id: Requester ID. Supported directly by the list endpoint; if
            combined with status/priority/agent it is applied locally to the
            returned page and `data.requester_filtered_locally` is true.
        updated_since: ISO datetime — tickets updated at or after this point.
            Without it Freshdesk returns only the last 30 days.
        page, page_size: Pagination. page_size is capped at
            MCP_TOOL_RESULT_LIMIT, and at 100 (list) or 30 (filter) by the API.

    Returns:
        IntegrationResult dict with `data.tickets`, `data.count`, the
        `data.endpoint` used (`tickets` or `search/tickets`), and — on the filter
        path — `data.total` and the `data.query` sent.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "freshdesk_list_tickets",
            context.permissions,
        )
        service = await _service()
        if service is None:
            return _not_configured()
        try:
            return await _call(
                service.list_tickets(
                    status=status,
                    priority=priority,
                    agent_id=agent_id,
                    requester_id=requester_id,
                    updated_since=updated_since,
                    page=page,
                    page_size=page_size,
                )
            )
        finally:
            await service.aclose()


async def freshdesk_get_ticket(
    ticket_id: str | int,
    include_conversations: bool = True,
    conversation_limit: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return one Freshdesk ticket with its conversation thread.

    Args:
        ticket_id: Freshdesk ticket ID.
        include_conversations: Fetch the thread (a second API call). Set false
            for the ticket record alone.
        conversation_limit: Max thread entries, capped at MCP_TOOL_RESULT_LIMIT
            and at 100 by the API.

    Returns:
        IntegrationResult dict with `data.ticket`, `data.conversations`, and
        `data.conversation_count`.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "freshdesk_get_ticket",
            context.permissions,
        )
        service = await _service()
        if service is None:
            return _not_configured()
        try:
            return await _call(
                service.get_ticket(
                    ticket_id=ticket_id,
                    include_conversations=include_conversations,
                    conversation_limit=conversation_limit,
                )
            )
        finally:
            await service.aclose()


async def freshdesk_search_tickets(
    query: str,
    updated_since: str | None = None,
    max_pages: int = 3,
    page_size: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Find tickets whose subject or description contains `query`.

    Freshdesk's v2 API has no public full-text ticket search, so this scans
    pages of the ticket list (newest first, description embedded) and matches
    locally. Narrow the scan with `updated_since` on a busy helpdesk.

    Args:
        query: Case-insensitive substring matched against subject,
            description_text, and description.
        updated_since: ISO datetime bound for the scan. Without it Freshdesk
            only serves the last 30 days.
        max_pages: Pages of 100 tickets to scan (1-10, default 3).
        page_size: Max matches returned, capped at MCP_TOOL_RESULT_LIMIT.

    Returns:
        IntegrationResult dict with `data.tickets`, `data.count`,
        `data.match_count`, and the scan's own bounds — `data.scanned`,
        `data.pages_scanned`, `data.scan_exhausted`, `data.truncated`. When
        `scan_exhausted` is false the scan hit its page limit, so absence of a
        match is not proof of absence.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "freshdesk_search_tickets",
            context.permissions,
        )
        service = await _service()
        if service is None:
            return _not_configured()
        try:
            return await _call(
                service.search_tickets(
                    query=query,
                    updated_since=updated_since,
                    max_pages=max_pages,
                    page_size=page_size,
                )
            )
        finally:
            await service.aclose()


async def freshdesk_list_agents(
    page: int = 1,
    page_size: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return Freshdesk agents.

    Args:
        page, page_size: Pagination. page_size is capped at
            MCP_TOOL_RESULT_LIMIT and at 100 by the API.

    Returns:
        IntegrationResult dict with `data.agents`, `data.count`, plus pagination.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "freshdesk_list_agents",
            context.permissions,
        )
        service = await _service()
        if service is None:
            return _not_configured()
        try:
            return await _call(service.list_agents(page=page, page_size=page_size))
        finally:
            await service.aclose()


async def freshdesk_get_ticket_summary(
    as_of: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return ticket counts by status and priority, plus SLA breach counts.

    Each count is the Freshdesk filter endpoint's own `total` for that status or
    priority, so the figures are complete rather than a sample of one scanned
    page. Ten API calls per invocation.

    Args:
        as_of: ISO date/datetime the SLA comparison runs against; defaults to
            today (UTC). The filter API compares dates, not timestamps.

    Returns:
        IntegrationResult dict with `data.by_status`, `data.by_priority`,
        `data.open_and_pending`, `data.overdue` (resolution SLA breached),
        `data.first_response_overdue`, the `data.as_of` date used, and
        `data.sla_basis` stating how overdue was measured.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "freshdesk_get_ticket_summary",
            context.permissions,
        )
        service = await _service()
        if service is None:
            return _not_configured()
        try:
            return await _call(service.ticket_summary(as_of=as_of))
        finally:
            await service.aclose()


async def freshdesk_list_groups(
    page: int = 1,
    page_size: int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Return Freshdesk agent groups, with their IDs.

    The escalation policy addresses a group by ID, never by name — group names
    differ per client and are renamed freely, so a name is not a stable
    identifier. This tool is how an admin discovers the ID to put in
    `escalation_group_id` via POST /admin/tenants/{tenant_id}/policy.

    Args:
        page, page_size: Pagination. page_size is capped at
            MCP_TOOL_RESULT_LIMIT and at 100 by the API.

    Returns:
        IntegrationResult dict with `data.groups`, `data.count`, plus
        pagination. Each group carries at least `id` and `name`.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id,
            context.user_id,
            "freshdesk_list_groups",
            context.permissions,
        )
        service = await _service()
        if service is None:
            return _not_configured()
        try:
            return await _call(service.list_groups(page=page, page_size=page_size))
        finally:
            await service.aclose()


def register(mcp: Any) -> None:
    """Register Freshdesk MCP tools (read-only)."""

    mcp.tool()(freshdesk_list_tickets)
    mcp.tool()(freshdesk_get_ticket)
    mcp.tool()(freshdesk_search_tickets)
    mcp.tool()(freshdesk_list_agents)
    mcp.tool()(freshdesk_get_ticket_summary)
    mcp.tool()(freshdesk_list_groups)
