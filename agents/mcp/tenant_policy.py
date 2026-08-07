"""Shared per-tenant policy resolver for tool guardrails.

Guardrails are tenant configuration, not constants. Two client organisations on
the same deployment can hold opposite positions on whether an automated reply
may quote a price, and neither position belongs in code. This module resolves
that configuration along the same path ``agents.mcp.credentials`` resolves
secrets — the ``current_tenant`` ContextVar set by ``TenantResolutionMiddleware``,
then the tenant's DB row, then a declared fallback — so the codebase has one
tenant-scoped lookup mechanism rather than two.

Policy is not a credential: it carries no secret, is never encrypted, and the
admin API returns it in full. It therefore gets its own table,
``tenant_policies`` (one row per ``(tenant_id, policy_type)``, the document in
the ``policy`` JSONB), instead of being folded into ``tenant_credentials``,
whose upsert path encrypts the primary value and stores it write-only.

Defaults are restrictive on purpose. A tenant with no policy row is a tenant
nobody has configured yet, so every block is on and the tenant-specific
allowances are empty: a new tenant is conservative until an admin says
otherwise. The same holds when the database is unreachable or a stored value is
malformed — resolution falls back to the safe default rather than to "no
guardrail". Resolution never raises.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from agents.mcp.database import DatabaseNotConfiguredError, get_conn
from agents.mcp.tenant import current_tenant

log = logging.getLogger(__name__)

# policy_type for the Freshdesk reply guardrails. One policy_type per tool
# family, mirroring credential_type in agents.mcp.credentials.
FRESHDESK_REPLY_POLICY = "freshdesk_reply"

# Symbols the price guard looks for by default. A list, not a single currency:
# the deployment is multi-tenant and no tenant's currency is the product's.
# Tenants override the list wholesale via the admin policy route.
DEFAULT_CURRENCY_SYMBOLS: tuple[str, ...] = ("R", "ZAR", "$", "£", "€")

# Idempotent DDL for the policy table. Mirrored in scripts/migrate.py and
# agents/mcp/portal_schema.py, matching how the rest of the schema is kept
# applicable both at startup and from the standalone migration script.
TENANT_POLICY_DDL = """
    CREATE TABLE IF NOT EXISTS tenant_policies (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
        policy_type TEXT NOT NULL,
        policy JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ DEFAULT now(),
        updated_at TIMESTAMPTZ DEFAULT now(),
        UNIQUE(tenant_id, policy_type)
    )
    """


class PolicyFieldError(ValueError):
    """A policy field was supplied with a value of the wrong type.

    Raised by ``validate_policy_field`` so the admin route can answer 400 with
    the offending field named. The resolver never surfaces this: a malformed
    value already in the database falls back to that field's default.
    """


@dataclass(frozen=True)
class FreshdeskReplyPolicy:
    """Guardrail configuration for one tenant's Freshdesk replies.

    Every default blocks. ``reply_blocked_phrases`` defaults empty because it
    holds tenant-specific wording that no default could guess; the other blocks
    are on until an admin turns them off.

    Price detection is split across two flags on purpose.
    ``reply_block_prices`` covers an amount written against a currency symbol,
    which is unambiguous. ``reply_block_bare_decimals`` covers a money-shaped
    decimal with no symbol, which is the rule that also catches clock times and
    dotted dates. Separating them means a tenant hitting false refusals on
    "10.00" can switch off the noisy rule without giving up currency detection
    entirely.
    """

    reply_block_prices: bool = True
    reply_block_bare_decimals: bool = True
    reply_block_delivery_promises: bool = True
    reply_currency_symbols: tuple[str, ...] = DEFAULT_CURRENCY_SYMBOLS
    reply_blocked_phrases: tuple[str, ...] = ()
    escalation_group_id: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the policy as JSON-serialisable primitives."""

        return {
            "reply_block_prices": self.reply_block_prices,
            "reply_block_bare_decimals": self.reply_block_bare_decimals,
            "reply_block_delivery_promises": self.reply_block_delivery_promises,
            "reply_currency_symbols": list(self.reply_currency_symbols),
            "reply_blocked_phrases": list(self.reply_blocked_phrases),
            "escalation_group_id": self.escalation_group_id,
        }


@dataclass(frozen=True)
class ResolvedPolicy:
    """Outcome of policy resolution for one tool call."""

    policy: FreshdeskReplyPolicy
    from_db: bool


# ---------------------------------------------------------------------------
# Field coercion
# ---------------------------------------------------------------------------


def _coerce_bool(field: str, value: Any) -> bool:
    # Deliberately strict: "false" as a string is a configuration mistake, and
    # accepting it loosely is the direction that silently disables a guardrail.
    if isinstance(value, bool):
        return value
    raise PolicyFieldError(f"{field} must be true or false")


def _coerce_str_list(field: str, value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise PolicyFieldError(f"{field} must be a list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise PolicyFieldError(f"{field} must be a list of strings")
        text = item.strip()
        if text:
            items.append(text)
    return tuple(items)


def _coerce_group_id(field: str, value: Any) -> int | None:
    if value is None:
        return None
    # bool is an int subclass; True as a group id is a mistake, not a 1.
    if isinstance(value, bool):
        raise PolicyFieldError(f"{field} must be an integer or null")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise PolicyFieldError(f"{field} must be an integer or null")


_FIELD_COERCERS: dict[str, Callable[[str, Any], Any]] = {
    "reply_block_prices": _coerce_bool,
    "reply_block_bare_decimals": _coerce_bool,
    "reply_block_delivery_promises": _coerce_bool,
    "reply_currency_symbols": _coerce_str_list,
    "reply_blocked_phrases": _coerce_str_list,
    "escalation_group_id": _coerce_group_id,
}

# API field name -> policy_type, the policy counterpart of admin._FIELD_MAP.
# A second tool family adds its fields here against its own policy_type.
POLICY_FIELDS: dict[str, str] = {
    field: FRESHDESK_REPLY_POLICY for field in _FIELD_COERCERS
}

# Fields for which JSON null is a legal value rather than "not supplied".
NULLABLE_POLICY_FIELDS: frozenset[str] = frozenset({"escalation_group_id"})


def validate_policy_field(field: str, value: Any) -> Any:
    """Return the coerced value for one policy field.

    Raises:
        PolicyFieldError: The field is unknown, or the value has the wrong type.
    """

    coerce = _FIELD_COERCERS.get(field)
    if coerce is None:
        raise PolicyFieldError(f"Unknown policy field: {field}")
    return coerce(field, value)


def coerce_freshdesk_reply_policy(raw: Any) -> FreshdeskReplyPolicy:
    """Build a policy from a stored document, defaulting anything unusable.

    A field that is absent, or present with a value of the wrong type, takes
    its default — which blocks. A malformed row therefore degrades to the safe
    configuration instead of failing the call or disabling a guardrail.
    """

    defaults = FreshdeskReplyPolicy()
    if not isinstance(raw, dict):
        return defaults

    values: dict[str, Any] = {}
    for field, coerce in _FIELD_COERCERS.items():
        if field not in raw:
            continue
        try:
            values[field] = coerce(field, raw[field])
        except PolicyFieldError:
            log.warning(
                "tenant_policy_field_invalid — falling back to the safe default",
                extra={"field": field, "policy_type": FRESHDESK_REPLY_POLICY},
            )
    return replace(defaults, **values)


# ---------------------------------------------------------------------------
# DB access (small named coroutines — monkeypatched in unit tests)
# ---------------------------------------------------------------------------


async def _fetch_policy_row(tenant_id: str, policy_type: str) -> dict[str, Any] | None:
    """Return the stored policy document for a tenant, or None.

    Never raises: a missing DB configuration, an unreachable database, or an
    unparseable document all resolve to ``None``, which the caller reads as
    "no policy row" and answers with the safe defaults.
    """

    try:
        async with get_conn() as conn:
            row = await conn.fetchrow(
                """
                SELECT policy
                FROM tenant_policies
                WHERE tenant_id::text = $1 AND policy_type = $2
                """,
                tenant_id,
                policy_type,
            )
    except DatabaseNotConfiguredError:
        return None
    except Exception:
        log.exception(
            "tenant_policy_lookup_failed",
            extra={"tenant_id": tenant_id, "policy_type": policy_type},
        )
        return None
    if row is None:
        return None

    policy = row["policy"]
    if isinstance(policy, str):
        try:
            policy = json.loads(policy)
        except json.JSONDecodeError:
            log.warning(
                "tenant_policy_unparseable",
                extra={"tenant_id": tenant_id, "policy_type": policy_type},
            )
            return None
    return policy if isinstance(policy, dict) else None


async def fetch_tenant_policies(tenant_id: str) -> dict[str, dict[str, Any]]:
    """Return ``{policy_type: <stored document>}`` for one tenant.

    Used by the admin route to patch-merge. Unlike ``_fetch_policy_row`` this
    is allowed to raise — the admin handler turns a DB failure into a 500
    rather than silently writing over a row it could not read.
    """

    async with get_conn() as conn:
        rows = await conn.fetch(
            "SELECT policy_type, policy FROM tenant_policies WHERE tenant_id::text = $1",
            tenant_id,
        )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        policy = row["policy"]
        if isinstance(policy, str):
            try:
                policy = json.loads(policy)
            except json.JSONDecodeError:
                policy = {}
        result[row["policy_type"]] = policy if isinstance(policy, dict) else {}
    return result


async def upsert_tenant_policy(
    tenant_id: str, policy_type: str, policy: dict[str, Any]
) -> None:
    """Write one tenant's policy document, replacing any existing row.

    ``policy`` is stored as given — the caller is responsible for having merged
    it onto the existing document, matching the credential upsert contract.
    """

    async with get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO tenant_policies (tenant_id, policy_type, policy)
            VALUES ($1, $2, $3::jsonb)
            ON CONFLICT (tenant_id, policy_type)
            DO UPDATE SET policy = EXCLUDED.policy, updated_at = now()
            """,
            uuid.UUID(tenant_id),
            policy_type,
            json.dumps(policy),
        )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


async def resolve_freshdesk_reply_policy() -> ResolvedPolicy:
    """Return this tenant's reply policy, or the safe defaults.

    ``tenant_id`` comes from the ``current_tenant`` ContextVar — the same
    source ``credentials.resolve_settings`` uses, set by
    ``TenantResolutionMiddleware``. With no tenant resolved (the env-var /
    X-API-Key path) or no policy row, the defaults apply and ``from_db`` is
    false.
    """

    tenant = current_tenant()
    tenant_id = tenant["id"] if tenant else None
    if not tenant_id:
        return ResolvedPolicy(policy=FreshdeskReplyPolicy(), from_db=False)

    raw = await _fetch_policy_row(tenant_id, FRESHDESK_REPLY_POLICY)
    if raw is None:
        return ResolvedPolicy(policy=FreshdeskReplyPolicy(), from_db=False)
    return ResolvedPolicy(policy=coerce_freshdesk_reply_policy(raw), from_db=True)
