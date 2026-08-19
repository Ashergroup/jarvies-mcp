"""Freshdesk MCP write tools — guardrailed, audited ticket mutations.

Companion to ``freshdesk_tools.py`` (read-only). Five tools, each mutating one
live ticket: ``freshdesk_reply_to_ticket``, ``freshdesk_add_note``,
``freshdesk_update_ticket``, ``freshdesk_assign_ticket``, ``freshdesk_escalate``.
Auth, base URL, credential resolution, and the response envelope are reused from
the read module unchanged.

Five rules hold across every tool here:

* **A reason is required.** Each tool takes a non-empty ``reason``. It is not
  sent to Freshdesk; it is what the audit row records, so a mutation can be
  accounted for afterwards without reconstructing intent from the diff.
* **Every successful write is audited.** One ``audit_log`` row per call, with
  the tenant, the caller, the action, the ticket, and the reason. The row is
  written after the mutation lands; a failure to record is surfaced as
  ``data.audit_logged`` false rather than hidden, because an unrecorded write
  is a gap an auditor needs to see.
* **One ticket per call.** No bulk targets. A list, a tuple, or a
  comma-separated string is refused rather than silently acted on for its first
  element.
* **Freshdesk's own error body is surfaced.** A 400 from Freshdesk names the
  field it rejected; collapsing that to "HTTP 400" throws away the only useful
  part of the response.
* **429 is reported, never retried.** These are non-idempotent writes — a
  retried reply is a second email to the customer. The ``Retry-After`` value is
  handed back in ``data.retry_after_seconds`` for the caller to decide.

The reply guardrail inspects customer-facing text before the API call and
refuses it outright when a rule fires, rather than trusting a model to abstain
from quoting a figure or promising a date. Which rules apply is per-tenant
configuration resolved through ``agents.mcp.tenant_policy`` — the same
ContextVar-then-DB path ``agents.mcp.credentials`` uses for secrets. Nothing
here is specific to any client organisation: the currency list, the extra
blocked phrases, and whether each block is on at all are the tenant's to set,
and a tenant with no policy row gets the restrictive defaults.

The guardrail covers the reply tool and any note posted publicly. A private
note is internal and passes unchecked; a public note reaches the customer and
is treated exactly like a reply regardless of which tool produced it.

Rules are evaluated in a fixed order — currency-symbol amounts, bare decimals,
delivery promises, then the tenant's own phrase list — and the first to fire is
named in the refusal, so a tenant admin reading the message knows which setting
to change.

Illustrative only, not a description of any tenant's configuration: a tenant
whose agents quote from a fixed rate card might leave ``reply_block_prices`` on
so figures always come from a human, while a tenant whose replies routinely
carry reference numbers might switch ``reply_block_bare_decimals`` off and keep
currency detection.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from uuid import UUID

import httpx

from agents.mcp.credentials import resolve_settings
from agents.mcp.database import get_conn
from agents.mcp.integrations import IntegrationResult, not_configured
from agents.mcp.permissions import check_permission
from agents.mcp.tenant import current_tenant
from agents.mcp.tenant_context import use_tenant_context
from agents.mcp.tenant_policy import (
    FreshdeskReplyPolicy,
    resolve_freshdesk_reply_policy,
)
from agents.mcp.tools.freshdesk_tools import (
    PRIORITY_CODES,
    STATUS_CODES,
    FreshdeskService,
    _coerce_code,
    _context,
)

log = logging.getLogger(__name__)

_NOT_CONFIGURED = "FRESHDESK_DOMAIN/FRESHDESK_API_KEY are not configured."

# An amount written against a currency symbol is unambiguous; a bare
# money-shaped decimal is not. They are separate policy flags because the
# second rule is the one that also matches clock times ("10.00") and dotted
# dates, and a tenant should be able to drop the noisy rule without losing
# currency detection.
_MONEY_SHAPED = re.compile(r"(?<![\w.,])\d[\d ,]*[.,]\d{2}(?!\d)")

# Generic commitments about when something will happen. Deliberately free of
# product names, courier names, and service-level figures — those are tenant
# wording and belong in reply_blocked_phrases.
_DELIVERY_PROMISE_PATTERNS: tuple[str, ...] = (
    r"\bwill\s+(?:be\s+)?(?:deliver|arriv|ship|dispatch|despatch)\w*",
    r"\b(?:deliver|arriv|ship|dispatch|despatch)\w*\s+(?:by|on|before|within)\b",
    r"\b(?:delivery|shipping|dispatch|despatch)\s+(?:date|time|window|eta)\b",
    r"\bwithin\s+\d+\s*(?:-\s*\d+\s*)?(?:working\s+|business\s+|calendar\s+)?"
    r"(?:hour|day|week|month)s?\b",
    r"\b(?:next|same)[-\s]day\s+(?:delivery|dispatch|shipping)\b",
    r"\bguarantee\w*\s+(?:delivery|arrival|dispatch|despatch)\b",
    r"\bETA\b",
)

_DELIVERY_PROMISE = re.compile("|".join(_DELIVERY_PROMISE_PATTERNS), re.IGNORECASE)

# What the audit row calls the thing being changed.
_AUDIT_TARGET_TYPE = "freshdesk_ticket"

_INSERT_AUDIT = """
    INSERT INTO audit_log
        (tenant_id, actor_type, actor_id, action, target_type, target_id, metadata)
    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb)
"""


class FreshdeskAPIError(Exception):
    """A Freshdesk response that carries a usable error body.

    ``retry_after`` is populated only for 429, from the ``Retry-After`` header.
    ``details`` holds Freshdesk's own per-field ``errors`` array when present.
    """

    def __init__(
        self,
        status: int,
        message: str,
        retry_after: float | None = None,
        details: list[Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after
        self.details = details or []


@dataclass(frozen=True)
class GuardVerdict:
    """Outcome of inspecting one piece of customer-facing text against policy."""

    allowed: bool
    rule: str | None = None
    detail: str | None = None
    match: str | None = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _extract_error(response: httpx.Response) -> tuple[str, list[Any]]:
    """Return ``(message, per-field errors)`` from a Freshdesk error body.

    Freshdesk answers a rejected write with ``{"description": ..., "errors":
    [{"field": ..., "message": ..., "code": ...}]}``. The per-field array is
    the part that says what to fix, so it is both folded into the message and
    handed back intact.
    """

    try:
        payload = response.json()
    except ValueError:
        text = (response.text or "").strip()
        return (text or response.reason_phrase or "request failed"), []

    if not isinstance(payload, dict):
        return str(payload), []

    description = str(payload.get("description") or payload.get("message") or "").strip()
    errors = payload.get("errors")
    errors = errors if isinstance(errors, list) else []

    parts: list[str] = []
    for entry in errors:
        if not isinstance(entry, dict):
            parts.append(str(entry))
            continue
        field = entry.get("field")
        detail = entry.get("message") or entry.get("code")
        parts.append(f"{field}: {detail}" if field else str(detail))

    if description and parts:
        message = f"{description} ({'; '.join(parts)})"
    elif parts:
        message = "; ".join(parts)
    else:
        message = description or response.reason_phrase or "request failed"
    return message, errors


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        # Retry-After may also be an HTTP-date; the caller gets None rather
        # than a wrong number, and the message still says it was rate limited.
        return None


class FreshdeskWriteService(FreshdeskService):
    """Adds POST/PUT verbs on top of the shared read-only Freshdesk client."""

    async def _send(
        self, method: str, path: str, json_body: dict[str, Any] | None = None
    ) -> Any:
        client = await self._http()
        url = f"{self.base_url}/{path.lstrip('/')}"
        started = time.perf_counter()
        response = await client.request(
            method,
            url,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json=json_body,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        log.info(
            "freshdesk_api_call",
            extra={
                "method": method,
                "path": f"/{path.lstrip('/')}",
                "status": response.status_code,
                "latency_ms": round(latency_ms, 1),
            },
        )

        if response.status_code == 429:
            # Not retried: these writes are not idempotent, and a retried reply
            # is a second email to the customer.
            raise FreshdeskAPIError(
                429,
                "Freshdesk rate limit reached; the write was not retried",
                retry_after=_retry_after_seconds(response),
            )
        if not response.is_success:
            message, details = _extract_error(response)
            raise FreshdeskAPIError(
                response.status_code,
                f"Freshdesk API returned HTTP {response.status_code}: {message}",
                details=details,
            )
        if not response.content:
            return {}
        return response.json()

    async def reply_to_ticket(
        self,
        ticket_id: str | int,
        body: str,
        cc_emails: list[str] | None = None,
        bcc_emails: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST a public reply.

        Freshdesk renders the field as HTML — pass raw HTML tags if you want
        formatting (never entity-escaped like ``&lt;p&gt;``), or plain text
        with line breaks. Not markdown.
        """

        payload: dict[str, Any] = {"body": body}
        if cc_emails:
            payload["cc_emails"] = cc_emails
        if bcc_emails:
            payload["bcc_emails"] = bcc_emails
        reply = await self._send("POST", f"tickets/{ticket_id}/reply", payload)
        return {"reply": reply, "ticket_id": ticket_id}

    async def add_note(
        self,
        ticket_id: str | int,
        body: str,
        private: bool = True,
        notify_emails: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST a note. ``private`` false makes it visible to the requester."""

        payload: dict[str, Any] = {"body": body, "private": private}
        if notify_emails:
            payload["notify_emails"] = notify_emails
        note = await self._send("POST", f"tickets/{ticket_id}/notes", payload)
        return {"note": note, "ticket_id": ticket_id, "private": private}

    async def update_ticket(
        self, ticket_id: str | int, fields: dict[str, Any]
    ) -> dict[str, Any]:
        """PUT ticket fields. ``fields`` is already validated and non-empty."""

        ticket = await self._send("PUT", f"tickets/{ticket_id}", fields)
        return {"ticket": ticket, "ticket_id": ticket_id, "updated": sorted(fields)}


# ---------------------------------------------------------------------------
# Guardrail
# ---------------------------------------------------------------------------


@lru_cache(maxsize=32)
def _currency_pattern(symbols: tuple[str, ...]) -> re.Pattern[str] | None:
    """Compile a detector for the tenant's currency symbols, or None if empty.

    A symbol only counts when it sits against a number, so a bare letter in
    prose is not a false positive. Alphabetic codes get a leading word boundary
    (they must not match mid-word) but not a trailing one, because the amount
    is written flush against them.
    """

    parts: list[str] = []
    for symbol in symbols:
        token = re.escape(symbol)
        if symbol[:1].isalpha():
            parts.append(rf"(?<!\w){token}\s*\d")
            parts.append(rf"\d\s*{token}(?!\w)")
        else:
            parts.append(rf"{token}\s*\d")
            parts.append(rf"\d\s*{token}")
    if not parts:
        return None
    return re.compile("|".join(parts), re.IGNORECASE)


def _snippet(text: str, start: int, end: int, width: int = 24) -> str:
    """Return the match with a little surrounding context, collapsed to one line."""

    left = max(0, start - width // 2)
    right = min(len(text), end + width // 2)
    fragment = " ".join(text[left:right].split())
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return f"{prefix}{fragment}{suffix}"


def evaluate_reply_body(body: str, policy: FreshdeskReplyPolicy) -> GuardVerdict:
    """Inspect customer-facing text against one tenant's policy.

    Rules fire in a fixed order — currency-symbol amounts, bare decimals,
    delivery promises, then the tenant's own phrases — and the first match
    wins, so the refusal always names exactly one rule.
    """

    if policy.reply_block_prices:
        pattern = _currency_pattern(tuple(policy.reply_currency_symbols))
        if pattern is not None:
            found = pattern.search(body)
            if found:
                return GuardVerdict(
                    allowed=False,
                    rule="reply_block_prices",
                    detail="the text quotes an amount against a currency symbol",
                    match=_snippet(body, found.start(), found.end()),
                )

    if policy.reply_block_bare_decimals:
        found = _MONEY_SHAPED.search(body)
        if found:
            return GuardVerdict(
                allowed=False,
                rule="reply_block_bare_decimals",
                detail="the text contains a money-shaped decimal with no currency symbol",
                match=_snippet(body, found.start(), found.end()),
            )

    if policy.reply_block_delivery_promises:
        found = _DELIVERY_PROMISE.search(body)
        if found:
            return GuardVerdict(
                allowed=False,
                rule="reply_block_delivery_promises",
                detail="the text commits to when something will happen",
                match=_snippet(body, found.start(), found.end()),
            )

    lowered = body.lower()
    for phrase in policy.reply_blocked_phrases:
        index = lowered.find(phrase.lower())
        if index >= 0:
            return GuardVerdict(
                allowed=False,
                rule="reply_blocked_phrases",
                detail=f"the text contains the phrase {phrase!r}",
                match=_snippet(body, index, index + len(phrase)),
            )

    return GuardVerdict(allowed=True)


def _refusal(verdict: GuardVerdict, policy: FreshdeskReplyPolicy) -> dict[str, Any]:
    """Build the refusal envelope for text blocked by policy.

    The message names the rule so a tenant admin can act on it without reading
    the source. ``escalation_group_id`` is carried through so the caller can
    hand the ticket straight to ``freshdesk_escalate``.
    """

    message = (
        f"Not sent — blocked by tenant policy rule '{verdict.rule}': "
        f"{verdict.detail} ({verdict.match}). A tenant admin can change this "
        f"rule via POST /admin/tenants/{{tenant_id}}/policy."
    )
    return IntegrationResult(
        source="freshdesk",
        status="error",
        data={
            "blocked": True,
            "rule": verdict.rule,
            "match": verdict.match,
            "escalation_group_id": policy.escalation_group_id,
        },
        error=message,
    ).model_dump()


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def _single_ticket_id(ticket_id: Any) -> str:
    """Return one ticket id, refusing anything that names more than one.

    Bulk targets are rejected outright rather than silently reduced to their
    first element: a caller that meant to act on five tickets should be told it
    cannot, not have one of them mutated.
    """

    if isinstance(ticket_id, (list, tuple, set, frozenset)):
        raise ValueError(
            "ticket_id must be a single ticket — this tool acts on one ticket per call"
        )
    if isinstance(ticket_id, bool) or ticket_id is None:
        raise ValueError("ticket_id is required")
    text = str(ticket_id).strip()
    if not text:
        raise ValueError("ticket_id is required")
    if "," in text or " " in text:
        raise ValueError(
            "ticket_id must be a single ticket — this tool acts on one ticket per call"
        )
    return text


def _require_reason(reason: Any) -> str:
    """Return the trimmed reason, refusing an empty one.

    The reason is the audit row's only account of intent, so an empty string is
    a failed write rather than an unexplained one.
    """

    text = str(reason or "").strip()
    if not text:
        raise ValueError(
            "reason is required — it is recorded in the audit log for this write"
        )
    return text


def _escalation_note(reason: str) -> str:
    """Build the private note that travels with an escalation.

    Labelled so the receiving agent can tell at a glance that this is the
    escalation's rationale and not a customer message. HTML-escaped: the reason
    is caller-authored text and Freshdesk renders note bodies as HTML.
    """

    return f"<b>Escalated via Jarvies</b><br>Reason: {html.escape(reason)}"


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer id")
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer id") from None


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def _write_audit(
    *,
    tenant_id: str | None,
    actor_id: str,
    action: str,
    ticket_id: str,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Record one completed write in ``audit_log``. Returns whether it landed.

    Never raises: the mutation has already happened at this point, so failing
    the tool call would misreport a write that did occur. The failure is logged
    and returned instead, and the tool surfaces it as ``data.audit_logged``
    false so an unrecorded write is visible rather than silent.

    Reply and note bodies are deliberately NOT copied here. Freshdesk is the
    system of record for the conversation, and duplicating customer text into a
    second store widens the personal-data footprint for no audit benefit; the
    row records the reason, the actor, and what changed.
    """

    payload = {"reason": reason, **(metadata or {})}
    try:
        async with get_conn() as conn:
            await conn.execute(
                _INSERT_AUDIT,
                tenant_id,
                "mcp_tool",
                actor_id,
                action,
                _AUDIT_TARGET_TYPE,
                ticket_id,
                json.dumps(payload),
            )
        return True
    except Exception:
        log.warning(
            "freshdesk_audit_write_failed — the write itself succeeded",
            extra={"action": action, "ticket_id": ticket_id, "tenant_id": tenant_id},
        )
        return False


def _audit_tenant_id(context_tenant_id: str | None) -> str | None:
    """Return the tenant id for the audit row, or None when there isn't a real one.

    The resolved tenant wins. ``current_tenant`` is the identity the bearer
    token or ``X-Tenant-ID`` header established — the same one the policy and
    the credentials were read against — so it is the one the audit row must
    name. The tool argument is only a fallback, and it defaults to a
    non-UUID placeholder on the env-var path.

    ``audit_log.tenant_id`` is a UUID column, so anything that is not a UUID is
    recorded as NULL rather than crashing the insert.
    """

    tenant = current_tenant()
    candidate = (tenant or {}).get("id") or context_tenant_id
    try:
        UUID(str(candidate))
    except (ValueError, AttributeError, TypeError):
        return None
    return str(candidate)


# ---------------------------------------------------------------------------
# Invocation helpers
# ---------------------------------------------------------------------------


async def _write_service() -> FreshdeskWriteService | None:
    """Return a configured write service, or None when credentials are missing."""

    settings = (await resolve_settings("freshdesk")).settings
    if not settings.freshdesk_configured:
        return None
    return FreshdeskWriteService(settings)


def _error(message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return IntegrationResult(
        source="freshdesk", status="error", data=data or {}, error=message
    ).model_dump()


async def _attempt(coro) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Run one API call, returning ``(data, error_envelope)`` — one is always None.

    Separated from ``_invoke`` because ``freshdesk_escalate`` makes two calls
    and has to report on each independently rather than abandoning the sequence
    at the first failure.
    """

    try:
        return await coro, None
    except ValueError as exc:
        return None, _error(str(exc))
    except FreshdeskAPIError as exc:
        payload: dict[str, Any] = {"http_status": exc.status}
        if exc.status == 429:
            payload["retry_after_seconds"] = exc.retry_after
            payload["retried"] = False
        if exc.details:
            payload["freshdesk_errors"] = exc.details
        return None, _error(exc.message, payload)
    except httpx.RequestError as exc:
        return None, _error(f"Freshdesk request failed: {exc.__class__.__name__}")


async def _invoke(
    coro,
    *,
    action: str,
    ticket_id: str,
    reason: str,
    tenant_id: str | None,
    actor_id: str,
    audit_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one write, surface its errors faithfully, and audit what landed.

    A 429 comes back with ``data.retry_after_seconds`` and is never retried; a
    4xx/5xx comes back with Freshdesk's own message and per-field errors.
    """

    data, failure = await _attempt(coro)
    if failure is not None:
        return failure

    audited = await _write_audit(
        tenant_id=tenant_id,
        actor_id=actor_id,
        action=action,
        ticket_id=ticket_id,
        reason=reason,
        metadata=audit_metadata,
    )
    return IntegrationResult(
        source="freshdesk", status="ok", data={**data, "audit_logged": audited}
    ).model_dump()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def freshdesk_reply_to_ticket(
    ticket_id: str | int,
    body: str,
    reason: str,
    cc_emails: list[str] | None = None,
    bcc_emails: list[str] | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Send a public reply on one Freshdesk ticket, subject to tenant policy.

    The reply goes to the customer immediately and cannot be recalled. Before
    the API call the body is checked against this tenant's reply policy; if a
    rule fires the reply is refused, no request is made, and the error names
    the rule. A tenant with no policy row gets the restrictive defaults.

    Args:
        ticket_id: One Freshdesk ticket ID. Lists are refused.
        body: Reply body. Freshdesk renders the field as HTML — pass raw
            HTML tags if you want formatting (never entity-escaped like
            &lt;p&gt;), or plain text with line breaks. Not markdown.
        reason: Why this reply is being sent. Recorded in the audit log; not
            sent to Freshdesk. Required.
        cc_emails: Additional recipients copied on the reply.
        bcc_emails: Additional recipients blind-copied on the reply.

    Returns:
        IntegrationResult dict. On success, `data.reply` is the created
        conversation, `data.ticket_id` echoes the target, and
        `data.audit_logged` says whether the audit row landed. On a policy
        refusal, `status` is `error`, `data.blocked` is true, `data.rule` names
        the rule, and `data.escalation_group_id` carries the tenant's
        escalation group for handing to `freshdesk_escalate`. On a rate limit,
        `data.retry_after_seconds` is set and `data.retried` is false.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "freshdesk_reply_to_ticket", context.permissions
        )
        try:
            ticket = _single_ticket_id(ticket_id)
            why = _require_reason(reason)
        except ValueError as exc:
            return _error(str(exc))

        text = (body or "").strip()
        if not text:
            return _error("body cannot be empty")

        resolved = await resolve_freshdesk_reply_policy()
        verdict = evaluate_reply_body(text, resolved.policy)
        if not verdict.allowed:
            log.warning(
                "freshdesk_reply_blocked",
                extra={
                    "rule": verdict.rule,
                    "tenant_id": context.tenant_id,
                    "ticket_id": ticket,
                    "policy_from_db": resolved.from_db,
                },
            )
            return _refusal(verdict, resolved.policy)

        service = await _write_service()
        if service is None:
            return not_configured("freshdesk", _NOT_CONFIGURED)
        try:
            return await _invoke(
                service.reply_to_ticket(
                    ticket_id=ticket,
                    body=text,
                    cc_emails=cc_emails,
                    bcc_emails=bcc_emails,
                ),
                action="freshdesk_reply_to_ticket",
                ticket_id=ticket,
                reason=why,
                tenant_id=_audit_tenant_id(context.tenant_id),
                actor_id=context.user_id,
                audit_metadata={"body_chars": len(text), "cc": len(cc_emails or [])},
            )
        finally:
            await service.aclose()


async def freshdesk_add_note(
    ticket_id: str | int,
    body: str,
    reason: str,
    private: bool = True,
    notify_emails: list[str] | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Add a note to one Freshdesk ticket.

    Notes default to private (internal, invisible to the requester). A private
    note bypasses the reply guardrail because nothing reaches the customer. A
    note posted with `private=false` IS customer-facing and is checked against
    the same policy as a reply — the guardrail follows what the customer can
    see, not which tool was called.

    Args:
        ticket_id: One Freshdesk ticket ID. Lists are refused.
        body: Note body. Freshdesk renders the field as HTML — pass raw
            HTML tags if you want formatting (never entity-escaped like
            &lt;p&gt;), or plain text with line breaks. Not markdown.
        reason: Why this note is being added. Recorded in the audit log;
            not sent to Freshdesk. Required.
        private: Keep the note internal. Defaults to true.
        notify_emails: Agents to notify about the note.

    Returns:
        IntegrationResult dict with `data.note`, `data.ticket_id`,
        `data.private`, and `data.audit_logged`. A public note blocked by
        policy returns the same refusal shape as a blocked reply.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "freshdesk_add_note", context.permissions
        )
        try:
            ticket = _single_ticket_id(ticket_id)
            why = _require_reason(reason)
        except ValueError as exc:
            return _error(str(exc))

        text = (body or "").strip()
        if not text:
            return _error("body cannot be empty")

        if not private:
            resolved = await resolve_freshdesk_reply_policy()
            verdict = evaluate_reply_body(text, resolved.policy)
            if not verdict.allowed:
                log.warning(
                    "freshdesk_public_note_blocked",
                    extra={
                        "rule": verdict.rule,
                        "tenant_id": context.tenant_id,
                        "ticket_id": ticket,
                    },
                )
                return _refusal(verdict, resolved.policy)

        service = await _write_service()
        if service is None:
            return not_configured("freshdesk", _NOT_CONFIGURED)
        try:
            return await _invoke(
                service.add_note(
                    ticket_id=ticket,
                    body=text,
                    private=private,
                    notify_emails=notify_emails,
                ),
                action="freshdesk_add_note",
                ticket_id=ticket,
                reason=why,
                tenant_id=_audit_tenant_id(context.tenant_id),
                actor_id=context.user_id,
                audit_metadata={"private": private, "body_chars": len(text)},
            )
        finally:
            await service.aclose()


async def freshdesk_update_ticket(
    ticket_id: str | int,
    reason: str,
    status: str | int | None = None,
    priority: str | int | None = None,
    tags: list[str] | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Update status, priority, or tags on one Freshdesk ticket.

    Assignment is deliberately not settable here — use `freshdesk_assign_ticket`
    or `freshdesk_escalate`, so a routing change is always audited under its own
    action rather than buried in a field update.

    Args:
        ticket_id: One Freshdesk ticket ID. Lists are refused.
        reason: Why the ticket is being changed. Recorded in the audit log;
            not sent to Freshdesk. Required.
        status: `open`, `pending`, `resolved`, `closed`, or the integer.
        priority: `low`, `medium`, `high`, `urgent`, or the integer.
        tags: Replaces the ticket's tag list wholesale — Freshdesk does not
            merge tags, so send the full set you want.

    Returns:
        IntegrationResult dict with `data.ticket`, `data.updated` (the fields
        sent), and `data.audit_logged`.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "freshdesk_update_ticket", context.permissions
        )
        try:
            ticket = _single_ticket_id(ticket_id)
            why = _require_reason(reason)
            fields: dict[str, Any] = {}
            if status is not None and status != "":
                fields["status"] = _coerce_code(status, STATUS_CODES, "status")
            if priority is not None and priority != "":
                fields["priority"] = _coerce_code(priority, PRIORITY_CODES, "priority")
            if tags is not None:
                if not isinstance(tags, (list, tuple)):
                    raise ValueError("tags must be a list of strings")
                fields["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()]
        except ValueError as exc:
            return _error(str(exc))

        if not fields:
            return _error(
                "nothing to update — supply at least one of status, priority, or tags"
            )

        service = await _write_service()
        if service is None:
            return not_configured("freshdesk", _NOT_CONFIGURED)
        try:
            return await _invoke(
                service.update_ticket(ticket_id=ticket, fields=fields),
                action="freshdesk_update_ticket",
                ticket_id=ticket,
                reason=why,
                tenant_id=_audit_tenant_id(context.tenant_id),
                actor_id=context.user_id,
                audit_metadata={"fields": fields},
            )
        finally:
            await service.aclose()


async def freshdesk_assign_ticket(
    ticket_id: str | int,
    reason: str,
    agent_id: str | int | None = None,
    group_id: str | int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Assign one Freshdesk ticket to an agent, a group, or both.

    Both targets are IDs. Names are not accepted and are never resolved:
    agent and group names differ per client and are renamed freely, so a name
    is not a stable identifier. Discover IDs with `freshdesk_list_agents` and
    `freshdesk_list_groups`.

    Args:
        ticket_id: One Freshdesk ticket ID. Lists are refused.
        reason: Why the ticket is being reassigned. Recorded in the audit log;
            not sent to Freshdesk. Required.
        agent_id: Responder (agent) ID to assign to.
        group_id: Group ID to assign to. At least one of agent_id or group_id
            is required.

    Returns:
        IntegrationResult dict with `data.ticket`, `data.updated`, and
        `data.audit_logged`.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "freshdesk_assign_ticket", context.permissions
        )
        try:
            ticket = _single_ticket_id(ticket_id)
            why = _require_reason(reason)
            fields: dict[str, Any] = {}
            if agent_id is not None and agent_id != "":
                fields["responder_id"] = _positive_int(agent_id, "agent_id")
            if group_id is not None and group_id != "":
                fields["group_id"] = _positive_int(group_id, "group_id")
        except ValueError as exc:
            return _error(str(exc))

        if not fields:
            return _error("supply at least one of agent_id or group_id")

        service = await _write_service()
        if service is None:
            return not_configured("freshdesk", _NOT_CONFIGURED)
        try:
            return await _invoke(
                service.update_ticket(ticket_id=ticket, fields=fields),
                action="freshdesk_assign_ticket",
                ticket_id=ticket,
                reason=why,
                tenant_id=_audit_tenant_id(context.tenant_id),
                actor_id=context.user_id,
                audit_metadata={"fields": fields},
            )
        finally:
            await service.aclose()


async def freshdesk_escalate(
    ticket_id: str | int,
    reason: str,
    group_id: str | int | None = None,
    priority: str | int | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    access_token: str | None = None,
    permissions: list[str] | None = None,
) -> dict[str, Any]:
    """Escalate one Freshdesk ticket to a group, defaulting to tenant policy.

    This is what acts on a blocked reply: the refusal hands back the tenant's
    `escalation_group_id`, and this tool routes the ticket there.

    Two writes, in this order. A private note carrying the reason goes on the
    ticket first, so the context is already there when the ticket lands in the
    receiving group's queue; then the ticket is reassigned. The note is private
    and never reaches the customer, so the reply guardrail does not apply.

    Both are attempted even if the first fails, and the result says exactly
    what landed. When only one succeeds the status is `error` with
    `data.partial` true — the escalation is not complete, and the caller must
    finish it with the specific tool named in the message rather than re-running
    this one, which would duplicate the half that already succeeded.

    With no `group_id` argument the target comes from the tenant's
    `escalation_group_id` policy field. The group is only ever addressed by ID.
    A name is never resolved to a group — group names differ per client and are
    renamed freely. When neither an argument nor a policy value is present the
    call fails and says how to set one; `freshdesk_list_groups` is how an admin
    finds the ID.

    Args:
        ticket_id: One Freshdesk ticket ID. Lists are refused.
        reason: Why the ticket is being escalated. Written into the private
            note and recorded in the audit log. Required.
        group_id: Target group ID. Defaults to the tenant's
            `escalation_group_id` policy field.
        priority: Optionally raise priority at the same time — `low`,
            `medium`, `high`, `urgent`, or the integer.

    Returns:
        IntegrationResult dict. On full success, `data.ticket`, `data.note`,
        `data.updated`, `data.group_source` (`argument` or `tenant_policy`),
        `data.note_added` and `data.assigned` both true, and
        `data.audit_logged`. On partial completion, `status` is `error`,
        `data.partial` is true, and `data.note_added` / `data.assigned` say
        which half landed. When neither lands, `data.partial` is false and the
        error is the underlying Freshdesk failure.
    """

    context = _context(tenant_id, user_id, access_token, permissions)
    with use_tenant_context(context):
        check_permission(
            context.tenant_id, context.user_id, "freshdesk_escalate", context.permissions
        )
        try:
            ticket = _single_ticket_id(ticket_id)
            why = _require_reason(reason)
        except ValueError as exc:
            return _error(str(exc))

        resolved = await resolve_freshdesk_reply_policy()
        group_source = "argument"
        try:
            if group_id is not None and group_id != "":
                target_group = _positive_int(group_id, "group_id")
            elif resolved.policy.escalation_group_id is not None:
                target_group = resolved.policy.escalation_group_id
                group_source = "tenant_policy"
            else:
                return _error(
                    "no escalation group — pass group_id, or set escalation_group_id "
                    "via POST /admin/tenants/{tenant_id}/policy. Use "
                    "freshdesk_list_groups to find the ID.",
                    {"escalation_group_id": None},
                )
            fields: dict[str, Any] = {"group_id": target_group}
            if priority is not None and priority != "":
                fields["priority"] = _coerce_code(priority, PRIORITY_CODES, "priority")
        except ValueError as exc:
            return _error(str(exc))

        service = await _write_service()
        if service is None:
            return not_configured("freshdesk", _NOT_CONFIGURED)
        try:
            # Note first: the receiving group should never see an unexplained
            # ticket appear in its queue.
            note, note_failure = await _attempt(
                service.add_note(
                    ticket_id=ticket, body=_escalation_note(why), private=True
                )
            )
            # Attempted regardless — a note that would not post is no reason to
            # leave the ticket unrouted.
            assignment, assign_failure = await _attempt(
                service.update_ticket(ticket_id=ticket, fields=fields)
            )

            note_added = note_failure is None
            assigned = assign_failure is None

            if not note_added and not assigned:
                failure = assign_failure or note_failure
                failure["data"].update(
                    {"partial": False, "note_added": False, "assigned": False}
                )
                return failure

            audited = await _write_audit(
                tenant_id=_audit_tenant_id(context.tenant_id),
                actor_id=context.user_id,
                action="freshdesk_escalate",
                ticket_id=ticket,
                reason=why,
                metadata={
                    "fields": fields,
                    "group_source": group_source,
                    "note_added": note_added,
                    "assigned": assigned,
                },
            )

            data: dict[str, Any] = {
                "ticket_id": ticket,
                "group_source": group_source,
                "note_added": note_added,
                "assigned": assigned,
                "audit_logged": audited,
            }
            if note_added:
                data["note"] = (note or {}).get("note")
            if assigned:
                data["ticket"] = (assignment or {}).get("ticket")
                data["updated"] = (assignment or {}).get("updated")

            if note_added and assigned:
                return IntegrationResult(
                    source="freshdesk", status="ok", data=data
                ).model_dump()

            data["partial"] = True
            if assigned:
                message = (
                    f"Escalation partially completed: ticket {ticket} was routed to "
                    f"group {fields['group_id']}, but the private note carrying the "
                    f"reason failed ({(note_failure or {}).get('error')}). Do not "
                    f"re-run freshdesk_escalate — it would reassign again. Add the "
                    f"note with freshdesk_add_note."
                )
            else:
                message = (
                    f"Escalation partially completed: the private note was added to "
                    f"ticket {ticket}, but the reassignment to group "
                    f"{fields['group_id']} failed "
                    f"({(assign_failure or {}).get('error')}). Do not re-run "
                    f"freshdesk_escalate — it would duplicate the note. Reassign "
                    f"with freshdesk_assign_ticket."
                )
            log.warning(
                "freshdesk_escalate_partial",
                extra={
                    "ticket_id": ticket,
                    "note_added": note_added,
                    "assigned": assigned,
                    "tenant_id": context.tenant_id,
                },
            )
            return IntegrationResult(
                source="freshdesk", status="error", data=data, error=message
            ).model_dump()
        finally:
            await service.aclose()


def register(mcp: Any) -> None:
    """Register Freshdesk MCP write tools."""

    mcp.tool()(freshdesk_reply_to_ticket)
    mcp.tool()(freshdesk_add_note)
    mcp.tool()(freshdesk_update_ticket)
    mcp.tool()(freshdesk_assign_ticket)
    mcp.tool()(freshdesk_escalate)
