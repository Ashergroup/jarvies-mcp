"""Tests for the Freshdesk write tools, their guardrail, and the tenant policy.

The policy resolver's one DB coroutine (``tenant_policy._fetch_policy_row``) and
the audit insert are monkeypatched so these run without a live database,
mirroring the rest of the suite. The tenant identity comes from the
``current_tenant`` ContextVar, set here the way ``TenantResolutionMiddleware``
sets it in production.
"""

from __future__ import annotations

import base64
import inspect
import json

import httpx
import pytest
import respx

from agents.mcp import config as mcp_config
from agents.mcp import tenant, tenant_policy
from agents.mcp.permissions import TOOL_POLICIES, MCPPermissionError, check_permission
from agents.mcp.tenant_policy import FreshdeskReplyPolicy
from agents.mcp.tools import freshdesk_write_tools
from agents.mcp.tools.freshdesk_write_tools import evaluate_reply_body

BASE = "https://acme.freshdesk.test/api/v2"
API_KEY = "fd-secret-do-not-log"
EXPECTED_AUTH = "Basic " + base64.b64encode(f"{API_KEY}:X".encode()).decode()
TENANT_ID = "11111111-1111-1111-1111-111111111111"
TICKET_ID = 4242
REPLY_URL = f"{BASE}/tickets/{TICKET_ID}/reply"
NOTES_URL = f"{BASE}/tickets/{TICKET_ID}/notes"
TICKET_URL = f"{BASE}/tickets/{TICKET_ID}"

# The tools that satisfy the full write-tool contract: write=True, a required
# non-empty ``reason``, and one ticket per call. This tuple parametrises the
# contract tests below, so a tool only belongs here if it honours all three.
WRITE_TOOLS = (
    "freshdesk_reply_to_ticket",
    "freshdesk_add_note",
    "freshdesk_update_ticket",
    "freshdesk_assign_ticket",
    "freshdesk_escalate",
)

# Everything ``register`` advertises, in registration order. Wider than
# WRITE_TOOLS by the two tools that take no ``reason``, and each sits outside
# the contract above for its own reason:
#
#   freshdesk_validate_reply_body  writes nothing, so it is write=False on
#       purpose -- a read_only caller is allowed to ask whether text would pass.
#   stage_ai_reply                 writes only the WhatsApp Automation custom
#       fields and creates no conversation, so there is no send to justify.
#
# Kept separate rather than widening WRITE_TOOLS: putting either one there
# would assert write=True of a read-only tool and would call both with a
# ``reason`` kwarg neither accepts.
REGISTERED_TOOLS = (
    "freshdesk_validate_reply_body",
    "freshdesk_reply_to_ticket",
    "stage_ai_reply",
    "freshdesk_add_note",
    "freshdesk_update_ticket",
    "freshdesk_assign_ticket",
    "freshdesk_escalate",
)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    mcp_config.get_settings.cache_clear()
    yield
    mcp_config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _freshdesk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "FRESHDESK_DOMAIN": "acme.freshdesk.test",
        "FRESHDESK_API_KEY": API_KEY,
        "MCP_TOOL_RESULT_LIMIT": "50",
    }.items():
        monkeypatch.setenv(key, value)
    mcp_config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_db_credential_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep credential resolution off the network.

    Setting a tenant makes ``credentials.resolve_settings`` look for that
    tenant's ``tenant_credentials`` row; without this the suite dials the real
    DATABASE_URL and waits for the refusal. Returning None is the production
    "no row" path, which falls back to the env vars set above.
    """

    async def no_row(tenant_id: str, credential_type: str):
        return None

    monkeypatch.setattr("agents.mcp.credentials.get_tenant_credentials", no_row)


@pytest.fixture(autouse=True)
def audit_rows(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture audit writes instead of inserting them, and return the list."""

    rows: list[dict] = []

    async def fake_audit(**kwargs):
        rows.append(kwargs)
        return True

    monkeypatch.setattr(freshdesk_write_tools, "_write_audit", fake_audit)
    return rows


@pytest.fixture
def as_tenant():
    """Set the current_tenant ContextVar for the duration of one test.

    pytest-asyncio runs the coroutine in its own copied Context, so a token
    taken inside the test cannot be reset from this fixture. Clearing the var
    is enough — and for async tests the copied context is discarded anyway.
    """

    def _set(tenant_id: str = TENANT_ID):
        tenant.set_current_tenant({"id": tenant_id})

    yield _set
    tenant.set_current_tenant(None)


@pytest.fixture
def stored_policy(monkeypatch: pytest.MonkeyPatch):
    """Install a fake tenant_policies row (or None for 'no row')."""

    def _install(document: dict | None):
        async def fake_fetch(tenant_id: str, policy_type: str):
            return document

        monkeypatch.setattr(tenant_policy, "_fetch_policy_row", fake_fetch)

    return _install


# ---------------------------------------------------------------------------
# Permissions and registration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", WRITE_TOOLS)
def test_write_tools_require_support_access(tool: str) -> None:
    assert check_permission("tenant-a", "user-a", tool, ["support_access"])


@pytest.mark.parametrize("tool", WRITE_TOOLS)
def test_write_tools_denied_without_support_access(tool: str) -> None:
    with pytest.raises(MCPPermissionError, match="requires one of: support_access"):
        check_permission("tenant-a", "user-a", tool, ["freshsales_access"])


@pytest.mark.parametrize("tool", WRITE_TOOLS)
def test_write_tools_are_refused_to_read_only_callers(tool: str) -> None:
    assert TOOL_POLICIES[tool].write is True
    with pytest.raises(MCPPermissionError, match="read_only"):
        check_permission("tenant-a", "user-a", tool, ["support_access", "read_only"])


def test_all_write_tools_are_registered() -> None:
    registered: list[str] = []

    class _Recorder:
        def tool(self):
            def decorate(fn):
                registered.append(fn.__name__)
                return fn

            return decorate

    freshdesk_write_tools.register(_Recorder())
    assert registered == list(REGISTERED_TOOLS)


def test_module_carries_no_client_specific_values() -> None:
    """The module ships to every client, so no one client may appear in it."""

    source = inspect.getsource(freshdesk_write_tools).lower()
    for token in ("niche", "asher", "echelon", "octopus", "rhiza", "babuyile"):
        assert token not in source


# ---------------------------------------------------------------------------
# Cross-cutting standards: required reason, one ticket per call
# ---------------------------------------------------------------------------


def _minimal_args(tool: str) -> dict:
    """The smallest valid argument set for each write tool, minus the reason."""

    return {
        "freshdesk_reply_to_ticket": {"body": "A colleague will follow up."},
        "freshdesk_add_note": {"body": "Internal note."},
        "freshdesk_update_ticket": {"status": "pending"},
        "freshdesk_assign_ticket": {"agent_id": 900},
        "freshdesk_escalate": {"group_id": 77},
    }[tool]


@pytest.mark.parametrize("tool", WRITE_TOOLS)
@pytest.mark.asyncio
async def test_every_write_tool_requires_a_reason(
    tool: str, as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy(None)

    with respx.mock:
        route_reply = respx.post(REPLY_URL)
        route_notes = respx.post(NOTES_URL)
        route_put = respx.put(TICKET_URL)
        result = await getattr(freshdesk_write_tools, tool)(
            ticket_id=TICKET_ID,
            reason="   ",
            permissions=["support_access"],
            **_minimal_args(tool),
        )

    assert result["status"] == "error"
    assert "reason is required" in result["error"]
    assert route_reply.call_count == 0
    assert route_notes.call_count == 0
    assert route_put.call_count == 0


@pytest.mark.parametrize("tool", WRITE_TOOLS)
@pytest.mark.asyncio
async def test_every_write_tool_refuses_a_bulk_target(
    tool: str, as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy(None)

    result = await getattr(freshdesk_write_tools, tool)(
        ticket_id=[1, 2, 3],
        reason="bulk attempt",
        permissions=["support_access"],
        **_minimal_args(tool),
    )

    assert result["status"] == "error"
    assert "one ticket per call" in result["error"]


@pytest.mark.parametrize("tool", WRITE_TOOLS)
@pytest.mark.asyncio
async def test_every_write_tool_refuses_a_comma_separated_target(
    tool: str, as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy(None)

    result = await getattr(freshdesk_write_tools, tool)(
        ticket_id="41,42",
        reason="bulk attempt",
        permissions=["support_access"],
        **_minimal_args(tool),
    )

    assert result["status"] == "error"
    assert "one ticket per call" in result["error"]


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_write_records_an_audit_row(
    as_tenant, stored_policy, audit_rows
) -> None:
    as_tenant()
    stored_policy(None)

    with respx.mock:
        respx.put(TICKET_URL).mock(return_value=httpx.Response(200, json={"id": TICKET_ID}))
        result = await freshdesk_write_tools.freshdesk_update_ticket(
            ticket_id=TICKET_ID,
            reason="customer confirmed the issue is resolved",
            status="resolved",
            user_id="agent-7",
            permissions=["support_access"],
        )

    assert result["status"] == "ok"
    assert result["data"]["audit_logged"] is True
    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row["action"] == "freshdesk_update_ticket"
    assert row["ticket_id"] == str(TICKET_ID)
    assert row["tenant_id"] == TENANT_ID
    assert row["actor_id"] == "agent-7"
    assert row["reason"] == "customer confirmed the issue is resolved"
    assert row["metadata"]["fields"] == {"status": 4}


@pytest.mark.asyncio
async def test_a_failed_write_records_nothing(
    as_tenant, stored_policy, audit_rows
) -> None:
    as_tenant()
    stored_policy(None)

    with respx.mock:
        respx.put(TICKET_URL).mock(return_value=httpx.Response(400, json={}))
        await freshdesk_write_tools.freshdesk_update_ticket(
            ticket_id=TICKET_ID,
            reason="attempted change",
            status="resolved",
            permissions=["support_access"],
        )

    assert audit_rows == []


@pytest.mark.asyncio
async def test_audit_failure_is_surfaced_not_hidden(
    as_tenant, stored_policy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write landed; the record did not. The caller must be able to see that."""

    as_tenant()
    stored_policy(None)

    async def failing_audit(**kwargs):
        return False

    monkeypatch.setattr(freshdesk_write_tools, "_write_audit", failing_audit)

    with respx.mock:
        respx.put(TICKET_URL).mock(return_value=httpx.Response(200, json={"id": TICKET_ID}))
        result = await freshdesk_write_tools.freshdesk_update_ticket(
            ticket_id=TICKET_ID,
            reason="status change",
            status="pending",
            permissions=["support_access"],
        )

    assert result["status"] == "ok"
    assert result["data"]["audit_logged"] is False


@pytest.mark.asyncio
async def test_audit_never_carries_the_message_body(
    as_tenant, stored_policy, audit_rows
) -> None:
    as_tenant()
    stored_policy({"reply_block_bare_decimals": False})
    body = "Your account reference is ABC and a colleague will look into it."

    with respx.mock:
        respx.post(REPLY_URL).mock(return_value=httpx.Response(201, json={"id": 1}))
        await freshdesk_write_tools.freshdesk_reply_to_ticket(
            ticket_id=TICKET_ID,
            body=body,
            reason="acknowledging the customer",
            permissions=["support_access"],
        )

    metadata = audit_rows[0]["metadata"]
    assert metadata["body_chars"] == len(body)
    assert body not in str(metadata)


# ---------------------------------------------------------------------------
# Error bodies and 429
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_freshdesk_error_body_is_surfaced(as_tenant, stored_policy) -> None:
    as_tenant()
    stored_policy(None)

    with respx.mock:
        respx.put(TICKET_URL).mock(
            return_value=httpx.Response(
                400,
                json={
                    "description": "Validation failed",
                    "errors": [
                        {
                            "field": "status",
                            "message": "It should be one of these values: 2,3,4,5",
                            "code": "invalid_value",
                        }
                    ],
                },
            )
        )
        result = await freshdesk_write_tools.freshdesk_update_ticket(
            ticket_id=TICKET_ID,
            reason="status change",
            status="pending",
            permissions=["support_access"],
        )

    assert result["status"] == "error"
    assert "Validation failed" in result["error"]
    assert "status: It should be one of these values" in result["error"]
    assert result["data"]["http_status"] == 400
    assert result["data"]["freshdesk_errors"][0]["field"] == "status"


@pytest.mark.asyncio
async def test_rate_limit_surfaces_retry_after_and_does_not_retry(
    as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy({"reply_block_bare_decimals": False})

    with respx.mock:
        route = respx.post(REPLY_URL).mock(
            return_value=httpx.Response(429, headers={"Retry-After": "30"})
        )
        result = await freshdesk_write_tools.freshdesk_reply_to_ticket(
            ticket_id=TICKET_ID,
            body="A colleague will look into this.",
            reason="acknowledging the customer",
            permissions=["support_access"],
        )

    assert result["status"] == "error"
    assert result["data"]["http_status"] == 429
    assert result["data"]["retry_after_seconds"] == 30.0
    assert result["data"]["retried"] is False
    # One attempt only: a retried reply is a second email to the customer.
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_rate_limit_without_a_parseable_retry_after(
    as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy(None)

    with respx.mock:
        respx.put(TICKET_URL).mock(
            return_value=httpx.Response(
                429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
            )
        )
        result = await freshdesk_write_tools.freshdesk_assign_ticket(
            ticket_id=TICKET_ID,
            reason="routing to the right agent",
            agent_id=900,
            permissions=["support_access"],
        )

    assert result["data"]["retry_after_seconds"] is None
    assert "rate limit" in result["error"]


# ---------------------------------------------------------------------------
# Policy defaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_with_no_policy_row_gets_safe_defaults(
    as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy(None)

    resolved = await tenant_policy.resolve_freshdesk_reply_policy()

    assert resolved.from_db is False
    assert resolved.policy.reply_block_prices is True
    assert resolved.policy.reply_block_bare_decimals is True
    assert resolved.policy.reply_block_delivery_promises is True
    assert resolved.policy.reply_currency_symbols == tenant_policy.DEFAULT_CURRENCY_SYMBOLS
    assert resolved.policy.reply_blocked_phrases == ()
    assert resolved.policy.escalation_group_id is None


@pytest.mark.asyncio
async def test_no_policy_row_blocks_a_price_without_calling_the_api(
    as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy(None)

    with respx.mock:
        route = respx.post(REPLY_URL)
        result = await freshdesk_write_tools.freshdesk_reply_to_ticket(
            ticket_id=TICKET_ID,
            body="Happy to help — the replacement unit is $49.99.",
            reason="answering a pricing question",
            permissions=["support_access"],
        )

    assert result["status"] == "error"
    assert result["data"]["blocked"] is True
    assert result["data"]["rule"] == "reply_block_prices"
    assert "reply_block_prices" in result["error"]
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_unresolved_tenant_also_gets_defaults(stored_policy) -> None:
    """No tenant on the ContextVar (the env-var path) is still conservative."""

    stored_policy({"reply_block_prices": False})

    resolved = await tenant_policy.resolve_freshdesk_reply_policy()

    assert resolved.from_db is False
    assert resolved.policy.reply_block_prices is True


@pytest.mark.asyncio
async def test_unreadable_policy_row_falls_back_to_blocking(
    as_tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB failure must not read as 'no guardrails'."""

    as_tenant()

    def exploding_conn(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(tenant_policy, "get_conn", exploding_conn)

    resolved = await tenant_policy.resolve_freshdesk_reply_policy()

    assert resolved.policy.reply_block_prices is True
    assert resolved.policy.reply_block_bare_decimals is True
    assert resolved.policy.reply_block_delivery_promises is True


def test_malformed_stored_value_falls_back_to_its_default() -> None:
    policy = tenant_policy.coerce_freshdesk_reply_policy(
        {"reply_block_prices": "false", "reply_blocked_phrases": ["refund"]}
    )

    assert policy.reply_block_prices is True
    assert policy.reply_blocked_phrases == ("refund",)


def test_every_policy_field_has_a_declared_default() -> None:
    defaults = FreshdeskReplyPolicy().as_dict()
    assert set(defaults) == set(tenant_policy.POLICY_FIELDS)


# ---------------------------------------------------------------------------
# The tenant that has switched a rule off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tenant_with_prices_allowed_sends_a_body_containing_a_price(
    as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy({"reply_block_prices": False, "reply_block_bare_decimals": False})

    with respx.mock:
        route = respx.post(REPLY_URL).mock(
            return_value=httpx.Response(201, json={"id": 9001, "body": "sent"})
        )
        result = await freshdesk_write_tools.freshdesk_reply_to_ticket(
            ticket_id=TICKET_ID,
            body="The replacement unit is $49.99.",
            reason="answering a pricing question the agent approved",
            permissions=["support_access"],
        )

    assert result["status"] == "ok"
    assert result["data"]["reply"]["id"] == 9001
    assert result["data"]["ticket_id"] == str(TICKET_ID)
    assert route.call_count == 1

    request = route.calls[0].request
    assert request.headers["Authorization"] == EXPECTED_AUTH
    assert b"49.99" in request.content


@pytest.mark.asyncio
async def test_switching_prices_off_leaves_other_rules_on(
    as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy({"reply_block_prices": False, "reply_block_bare_decimals": False})

    with respx.mock:
        route = respx.post(REPLY_URL)
        result = await freshdesk_write_tools.freshdesk_reply_to_ticket(
            ticket_id=TICKET_ID,
            body="Your order will be delivered on Tuesday.",
            reason="answering a delivery question",
            permissions=["support_access"],
        )

    assert result["data"]["rule"] == "reply_block_delivery_promises"
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# The two price flags are independent
# ---------------------------------------------------------------------------


def test_symbol_amounts_and_bare_decimals_are_separate_rules() -> None:
    policy = FreshdeskReplyPolicy()

    symbol = evaluate_reply_body("That comes to $450.", policy)
    bare = evaluate_reply_body("Your meeting is at 10.00.", policy)

    assert symbol.rule == "reply_block_prices"
    assert bare.rule == "reply_block_bare_decimals"


def test_dropping_bare_decimals_keeps_currency_detection() -> None:
    """The point of the split: fix false refusals without losing price blocking."""

    policy = FreshdeskReplyPolicy(reply_block_bare_decimals=False)

    assert evaluate_reply_body("Your meeting is at 10.00.", policy).allowed is True
    assert evaluate_reply_body("We received the payment on 2026.08.07.", policy).allowed is True
    assert evaluate_reply_body("That comes to $450.", policy).rule == "reply_block_prices"
    assert evaluate_reply_body("That comes to R1 250,00.", policy).rule == "reply_block_prices"


def test_dropping_currency_detection_keeps_bare_decimals() -> None:
    policy = FreshdeskReplyPolicy(reply_block_prices=False)

    assert evaluate_reply_body("That comes to $450.", policy).allowed is True
    assert (
        evaluate_reply_body("That comes to 450.00.", policy).rule
        == "reply_block_bare_decimals"
    )


def test_both_price_flags_off_allows_an_amount() -> None:
    policy = FreshdeskReplyPolicy(
        reply_block_prices=False, reply_block_bare_decimals=False
    )
    assert evaluate_reply_body("That comes to $449.99.", policy).allowed is True


@pytest.mark.parametrize(
    "body",
    [
        "That comes to R450 in total.",
        "That comes to ZAR 450 in total.",
        "That comes to $450 in total.",
        "That comes to £450 in total.",
        "That comes to €450 in total.",
        "That comes to 450 ZAR in total.",
    ],
)
def test_every_default_symbol_is_detected(body: str) -> None:
    verdict = evaluate_reply_body(body, FreshdeskReplyPolicy())
    assert verdict.rule == "reply_block_prices"


@pytest.mark.parametrize(
    "body",
    ["That comes to 1234.56 in total.", "That comes to 1 250,00 in total."],
)
def test_bare_decimals_are_detected_without_a_symbol(body: str) -> None:
    verdict = evaluate_reply_body(body, FreshdeskReplyPolicy())
    assert verdict.rule == "reply_block_bare_decimals"


@pytest.mark.parametrize(
    "body",
    [
        "Dear Rob, thanks for your patience.",
        "Please see section 5 of the guide.",
        "Reference ABC-450 has been updated.",
    ],
)
def test_prose_without_an_amount_is_not_a_price(body: str) -> None:
    assert evaluate_reply_body(body, FreshdeskReplyPolicy()).allowed is True


def test_a_tenant_can_replace_the_currency_list() -> None:
    policy = FreshdeskReplyPolicy(
        reply_currency_symbols=("¥",), reply_block_bare_decimals=False
    )

    assert evaluate_reply_body("It is ¥450.", policy).allowed is False
    assert evaluate_reply_body("It is $450.", policy).allowed is True


def test_an_empty_currency_list_still_catches_bare_decimals() -> None:
    policy = FreshdeskReplyPolicy(reply_currency_symbols=())

    assert evaluate_reply_body("It is R450.", policy).allowed is True
    assert evaluate_reply_body("It is 450.00.", policy).rule == "reply_block_bare_decimals"


# ---------------------------------------------------------------------------
# Delivery promises and tenant phrases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Your parcel will arrive shortly.",
        "It will be dispatched today.",
        "We will ship it this afternoon.",
        "It should be delivered by Friday.",
        "We resolve these within 3 business days.",
        "You will get next-day delivery on this.",
        "We guarantee delivery before the weekend.",
        "The ETA is with our logistics team.",
    ],
)
def test_delivery_promises_are_blocked_by_default(body: str) -> None:
    verdict = evaluate_reply_body(body, FreshdeskReplyPolicy())
    assert verdict.rule == "reply_block_delivery_promises"


def test_delivery_promises_can_be_switched_off() -> None:
    policy = FreshdeskReplyPolicy(reply_block_delivery_promises=False)
    assert evaluate_reply_body("Your parcel will arrive shortly.", policy).allowed is True


def test_tenant_phrases_are_matched_case_insensitively() -> None:
    policy = FreshdeskReplyPolicy(reply_blocked_phrases=("full refund",))

    verdict = evaluate_reply_body("We can offer a Full Refund here.", policy)

    assert verdict.rule == "reply_blocked_phrases"
    assert "full refund" in (verdict.detail or "").lower()


def test_rule_order_is_fixed_so_one_rule_is_named() -> None:
    policy = FreshdeskReplyPolicy(reply_blocked_phrases=("refund",))
    verdict = evaluate_reply_body("A refund of $20.00 will be shipped today.", policy)

    assert verdict.rule == "reply_block_prices"


# ---------------------------------------------------------------------------
# Notes — the guardrail follows what the customer can see
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_private_note_bypasses_the_guardrail(
    as_tenant, stored_policy, audit_rows
) -> None:
    as_tenant()
    stored_policy(None)

    with respx.mock:
        route = respx.post(NOTES_URL).mock(
            return_value=httpx.Response(201, json={"id": 5001})
        )
        result = await freshdesk_write_tools.freshdesk_add_note(
            ticket_id=TICKET_ID,
            body="Customer was quoted $49.99 by phone; confirm before replying.",
            reason="recording what the customer was told",
            permissions=["support_access"],
        )

    assert result["status"] == "ok"
    assert result["data"]["private"] is True
    assert route.call_count == 1
    assert audit_rows[0]["metadata"]["private"] is True


@pytest.mark.asyncio
async def test_public_note_is_guardrailed_like_a_reply(
    as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy(None)

    with respx.mock:
        route = respx.post(NOTES_URL)
        result = await freshdesk_write_tools.freshdesk_add_note(
            ticket_id=TICKET_ID,
            body="Customer was quoted $49.99 by phone.",
            reason="sharing the quote with the customer",
            private=False,
            permissions=["support_access"],
        )

    assert result["status"] == "error"
    assert result["data"]["rule"] == "reply_block_prices"
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# update / assign
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_ticket_coerces_names_to_freshdesk_codes(
    as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy(None)

    with respx.mock:
        route = respx.put(TICKET_URL).mock(
            return_value=httpx.Response(200, json={"id": TICKET_ID})
        )
        result = await freshdesk_write_tools.freshdesk_update_ticket(
            ticket_id=TICKET_ID,
            reason="triage",
            status="pending",
            priority="urgent",
            tags=["billing", " escalated "],
            permissions=["support_access"],
        )

    assert result["status"] == "ok"
    sent = route.calls[0].request.content
    assert b'"status": 3' in sent.replace(b'"status":3', b'"status": 3')
    assert b"urgent" not in sent  # names are coerced, not passed through
    assert result["data"]["updated"] == ["priority", "status", "tags"]


@pytest.mark.asyncio
async def test_update_ticket_with_no_fields_is_refused(
    as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy(None)

    result = await freshdesk_write_tools.freshdesk_update_ticket(
        ticket_id=TICKET_ID, reason="nothing to do", permissions=["support_access"]
    )

    assert result["status"] == "error"
    assert "nothing to update" in result["error"]


@pytest.mark.asyncio
async def test_stage_ai_reply_writes_only_whatsapp_fields(
    as_tenant, audit_rows
) -> None:
    as_tenant()
    with respx.mock:
        respx.get(TICKET_URL).mock(
            return_value=httpx.Response(
                200, json={"id": TICKET_ID, "source": 13, "custom_fields": {}}
            )
        )
        route = respx.put(TICKET_URL).mock(
            return_value=httpx.Response(200, json={"id": TICKET_ID})
        )
        result = await freshdesk_write_tools.stage_ai_reply(
            ticket_id=TICKET_ID,
            reply_body="We have checked this for you.",
            permissions=["support_access"],
        )

    assert result["status"] == "ok"
    payload = json.loads(route.calls[0].request.content)
    assert payload == {
        "custom_fields": {
            "cf_cf_ai_reply_body": "We have checked this for you.",
            "cf_cf_ai_reply_ready": True,
            "cf_cf_ai_reply_status": "staged",
        }
    }
    assert audit_rows[0]["action"] == "stage_ai_reply"


@pytest.mark.asyncio
async def test_stage_ai_reply_duplicate_is_idempotent(as_tenant, audit_rows) -> None:
    as_tenant()
    body = "Already staged."
    with respx.mock:
        respx.get(TICKET_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": TICKET_ID,
                    "source": 13,
                    "custom_fields": {
                        "cf_cf_ai_reply_body": body,
                        "cf_cf_ai_reply_status": "staged",
                    },
                },
            )
        )
        route = respx.put(TICKET_URL)
        result = await freshdesk_write_tools.stage_ai_reply(
            ticket_id=TICKET_ID,
            reply_body=body,
            permissions=["support_access"],
        )

    assert result["status"] == "ok"
    assert result["data"]["idempotent"] is True
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_stage_ai_reply_refuses_non_whatsapp_ticket(as_tenant) -> None:
    as_tenant()
    with respx.mock:
        respx.get(TICKET_URL).mock(
            return_value=httpx.Response(
                200, json={"id": TICKET_ID, "source": 1, "custom_fields": {}}
            )
        )
        route = respx.put(TICKET_URL)
        result = await freshdesk_write_tools.stage_ai_reply(
            ticket_id=TICKET_ID,
            reply_body="This must not be staged.",
            permissions=["support_access"],
        )

    assert result["status"] == "error"
    assert "not a WhatsApp ticket" in result["error"]
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_assign_requires_a_target(as_tenant, stored_policy) -> None:
    as_tenant()
    stored_policy(None)

    result = await freshdesk_write_tools.freshdesk_assign_ticket(
        ticket_id=TICKET_ID, reason="routing", permissions=["support_access"]
    )

    assert result["status"] == "error"
    assert "agent_id or group_id" in result["error"]


@pytest.mark.asyncio
async def test_assign_refuses_a_group_name(as_tenant, stored_policy) -> None:
    """Names vary per client and are never resolved to an ID."""

    as_tenant()
    stored_policy(None)

    result = await freshdesk_write_tools.freshdesk_assign_ticket(
        ticket_id=TICKET_ID,
        reason="routing",
        group_id="Tier 2 Support",
        permissions=["support_access"],
    )

    assert result["status"] == "error"
    assert "group_id must be an integer id" in result["error"]


@pytest.mark.asyncio
async def test_assign_sends_both_targets(as_tenant, stored_policy) -> None:
    as_tenant()
    stored_policy(None)

    with respx.mock:
        route = respx.put(TICKET_URL).mock(
            return_value=httpx.Response(200, json={"id": TICKET_ID})
        )
        result = await freshdesk_write_tools.freshdesk_assign_ticket(
            ticket_id=TICKET_ID,
            reason="routing to the billing specialist",
            agent_id=900,
            group_id=77,
            permissions=["support_access"],
        )

    assert result["status"] == "ok"
    assert result["data"]["updated"] == ["group_id", "responder_id"]
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# Escalation — the thing a blocked reply hands off to
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalate_falls_back_to_the_policy_group(
    as_tenant, stored_policy, audit_rows
) -> None:
    as_tenant()
    stored_policy({"escalation_group_id": 77})

    with respx.mock:
        note_route = respx.post(NOTES_URL).mock(
            return_value=httpx.Response(201, json={"id": 5002})
        )
        route = respx.put(TICKET_URL).mock(
            return_value=httpx.Response(200, json={"id": TICKET_ID})
        )
        result = await freshdesk_write_tools.freshdesk_escalate(
            ticket_id=TICKET_ID,
            reason="policy blocked the reply; needs a human",
            permissions=["support_access"],
        )

    assert result["status"] == "ok"
    assert result["data"]["group_source"] == "tenant_policy"
    assert result["data"]["note_added"] is True
    assert result["data"]["assigned"] is True
    assert b'"group_id"' in route.calls[0].request.content
    assert b"77" in route.calls[0].request.content
    assert note_route.call_count == 1
    assert audit_rows[0]["action"] == "freshdesk_escalate"
    assert audit_rows[0]["metadata"]["note_added"] is True
    assert audit_rows[0]["metadata"]["assigned"] is True


@pytest.mark.asyncio
async def test_escalate_posts_a_private_note_carrying_the_reason(
    as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy({"escalation_group_id": 77})

    with respx.mock:
        note_route = respx.post(NOTES_URL).mock(
            return_value=httpx.Response(201, json={"id": 5002})
        )
        respx.put(TICKET_URL).mock(return_value=httpx.Response(200, json={"id": TICKET_ID}))
        await freshdesk_write_tools.freshdesk_escalate(
            ticket_id=TICKET_ID,
            reason="customer disputes the charge",
            permissions=["support_access"],
        )

    sent = json.loads(note_route.calls[0].request.content)
    assert sent["private"] is True
    assert "customer disputes the charge" in sent["body"]


@pytest.mark.asyncio
async def test_escalation_note_is_written_before_the_reassignment(
    as_tenant, stored_policy
) -> None:
    """The receiving group must not see an unexplained ticket appear."""

    as_tenant()
    stored_policy({"escalation_group_id": 77})
    order: list[str] = []

    def record_note(request):
        order.append("note")
        return httpx.Response(201, json={"id": 5002})

    def record_assign(request):
        order.append("assign")
        return httpx.Response(200, json={"id": TICKET_ID})

    with respx.mock:
        respx.post(NOTES_URL).mock(side_effect=record_note)
        respx.put(TICKET_URL).mock(side_effect=record_assign)
        await freshdesk_write_tools.freshdesk_escalate(
            ticket_id=TICKET_ID, reason="needs a human", permissions=["support_access"]
        )

    assert order == ["note", "assign"]


@pytest.mark.asyncio
async def test_escalation_note_escapes_html_in_the_reason(
    as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy({"escalation_group_id": 77})

    with respx.mock:
        note_route = respx.post(NOTES_URL).mock(
            return_value=httpx.Response(201, json={"id": 5002})
        )
        respx.put(TICKET_URL).mock(return_value=httpx.Response(200, json={"id": TICKET_ID}))
        await freshdesk_write_tools.freshdesk_escalate(
            ticket_id=TICKET_ID,
            reason="<script>alert(1)</script> escalate please",
            permissions=["support_access"],
        )

    body = json.loads(note_route.calls[0].request.content)["body"]
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


@pytest.mark.asyncio
async def test_escalate_reports_partial_when_only_the_note_lands(
    as_tenant, stored_policy, audit_rows
) -> None:
    as_tenant()
    stored_policy({"escalation_group_id": 77})

    with respx.mock:
        respx.post(NOTES_URL).mock(return_value=httpx.Response(201, json={"id": 5002}))
        respx.put(TICKET_URL).mock(return_value=httpx.Response(500, json={}))
        result = await freshdesk_write_tools.freshdesk_escalate(
            ticket_id=TICKET_ID, reason="needs a human", permissions=["support_access"]
        )

    assert result["status"] == "error"
    assert result["data"]["partial"] is True
    assert result["data"]["note_added"] is True
    assert result["data"]["assigned"] is False
    assert "freshdesk_assign_ticket" in result["error"]
    assert "duplicate the note" in result["error"]
    # Something landed, so it is on the record.
    assert audit_rows[0]["metadata"]["assigned"] is False


@pytest.mark.asyncio
async def test_escalate_reports_partial_when_only_the_assignment_lands(
    as_tenant, stored_policy, audit_rows
) -> None:
    as_tenant()
    stored_policy({"escalation_group_id": 77})

    with respx.mock:
        respx.post(NOTES_URL).mock(return_value=httpx.Response(500, json={}))
        respx.put(TICKET_URL).mock(return_value=httpx.Response(200, json={"id": TICKET_ID}))
        result = await freshdesk_write_tools.freshdesk_escalate(
            ticket_id=TICKET_ID, reason="needs a human", permissions=["support_access"]
        )

    assert result["status"] == "error"
    assert result["data"]["partial"] is True
    assert result["data"]["note_added"] is False
    assert result["data"]["assigned"] is True
    assert "freshdesk_add_note" in result["error"]
    assert "reassign again" in result["error"]
    assert audit_rows[0]["metadata"]["note_added"] is False


@pytest.mark.asyncio
async def test_escalate_when_neither_write_lands_is_not_partial(
    as_tenant, stored_policy, audit_rows
) -> None:
    as_tenant()
    stored_policy({"escalation_group_id": 77})

    with respx.mock:
        respx.post(NOTES_URL).mock(return_value=httpx.Response(500, json={}))
        respx.put(TICKET_URL).mock(return_value=httpx.Response(500, json={}))
        result = await freshdesk_write_tools.freshdesk_escalate(
            ticket_id=TICKET_ID, reason="needs a human", permissions=["support_access"]
        )

    assert result["status"] == "error"
    assert result["data"]["partial"] is False
    assert result["data"]["note_added"] is False
    assert result["data"]["assigned"] is False
    # Nothing changed, so nothing is recorded.
    assert audit_rows == []


@pytest.mark.asyncio
async def test_escalate_prefers_an_explicit_group(as_tenant, stored_policy) -> None:
    as_tenant()
    stored_policy({"escalation_group_id": 77})

    with respx.mock:
        respx.post(NOTES_URL).mock(return_value=httpx.Response(201, json={"id": 5002}))
        route = respx.put(TICKET_URL).mock(
            return_value=httpx.Response(200, json={"id": TICKET_ID})
        )
        result = await freshdesk_write_tools.freshdesk_escalate(
            ticket_id=TICKET_ID,
            reason="specific team needed",
            group_id=88,
            permissions=["support_access"],
        )

    assert result["data"]["group_source"] == "argument"
    assert b"88" in route.calls[0].request.content


@pytest.mark.asyncio
async def test_escalate_never_resolves_a_group_name(as_tenant, stored_policy) -> None:
    """Group names differ per client, so a name is not a stable identifier."""

    as_tenant()
    stored_policy({"escalation_group_id": 77})

    with respx.mock:
        route = respx.put(TICKET_URL)
        result = await freshdesk_write_tools.freshdesk_escalate(
            ticket_id=TICKET_ID,
            reason="escalating",
            group_id="Tier 2 Support",
            permissions=["support_access"],
        )

    assert result["status"] == "error"
    assert "group_id must be an integer id" in result["error"]
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_escalate_without_a_group_anywhere_explains_how_to_set_one(
    as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy(None)

    with respx.mock:
        route = respx.put(TICKET_URL)
        result = await freshdesk_write_tools.freshdesk_escalate(
            ticket_id=TICKET_ID, reason="escalating", permissions=["support_access"]
        )

    assert result["status"] == "error"
    assert "escalation_group_id" in result["error"]
    assert "freshdesk_list_groups" in result["error"]
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_escalate_can_raise_priority_at_the_same_time(
    as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy({"escalation_group_id": 77})

    with respx.mock:
        respx.post(NOTES_URL).mock(return_value=httpx.Response(201, json={"id": 5002}))
        route = respx.put(TICKET_URL).mock(
            return_value=httpx.Response(200, json={"id": TICKET_ID})
        )
        result = await freshdesk_write_tools.freshdesk_escalate(
            ticket_id=TICKET_ID,
            reason="customer is at risk of churn",
            priority="urgent",
            permissions=["support_access"],
        )

    assert result["status"] == "ok"
    assert result["data"]["updated"] == ["group_id", "priority"]
    assert b"4" in route.calls[0].request.content


@pytest.mark.asyncio
async def test_a_blocked_reply_hands_the_group_to_escalate(
    as_tenant, stored_policy
) -> None:
    """The end-to-end path the escalation field exists for."""

    as_tenant()
    stored_policy({"escalation_group_id": 77})

    with respx.mock:
        respx.post(REPLY_URL)
        blocked = await freshdesk_write_tools.freshdesk_reply_to_ticket(
            ticket_id=TICKET_ID,
            body="The replacement is $49.99.",
            reason="answering a pricing question",
            permissions=["support_access"],
        )

        assert blocked["data"]["escalation_group_id"] == 77

        respx.post(NOTES_URL).mock(return_value=httpx.Response(201, json={"id": 5002}))
        respx.put(TICKET_URL).mock(
            return_value=httpx.Response(200, json={"id": TICKET_ID})
        )
        escalated = await freshdesk_write_tools.freshdesk_escalate(
            ticket_id=TICKET_ID,
            reason=f"reply blocked by {blocked['data']['rule']}",
            group_id=blocked["data"]["escalation_group_id"],
            permissions=["support_access"],
        )

    assert escalated["status"] == "ok"
    assert escalated["data"]["group_source"] == "argument"


# ---------------------------------------------------------------------------
# Configuration and transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_body_is_rejected_before_any_policy_lookup() -> None:
    result = await freshdesk_write_tools.freshdesk_reply_to_ticket(
        ticket_id=TICKET_ID,
        body="   ",
        reason="acknowledging",
        permissions=["support_access"],
    )

    assert result["status"] == "error"
    assert "body cannot be empty" in result["error"]


@pytest.mark.asyncio
async def test_not_configured_when_credentials_are_missing(
    monkeypatch: pytest.MonkeyPatch, as_tenant, stored_policy
) -> None:
    as_tenant()
    stored_policy(None)
    for key in ("FRESHDESK_DOMAIN", "FRESHDESK_API_KEY"):
        monkeypatch.setenv(key, "")
    mcp_config.get_settings.cache_clear()

    result = await freshdesk_write_tools.freshdesk_reply_to_ticket(
        ticket_id=TICKET_ID,
        body="A colleague will follow up.",
        reason="acknowledging",
        permissions=["support_access"],
    )

    assert result["status"] == "not_configured"


@pytest.mark.asyncio
async def test_transport_failure_surfaces_as_an_error(as_tenant, stored_policy) -> None:
    as_tenant()
    stored_policy(None)

    with respx.mock:
        respx.put(TICKET_URL).mock(side_effect=httpx.ConnectError("boom"))
        result = await freshdesk_write_tools.freshdesk_assign_ticket(
            ticket_id=TICKET_ID,
            reason="routing",
            agent_id=900,
            permissions=["support_access"],
        )

    assert result["status"] == "error"
    assert "ConnectError" in result["error"]
