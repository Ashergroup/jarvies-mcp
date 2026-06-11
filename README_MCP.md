# Jarvies MCP

Production-ready MCP server layer for the existing M365 Agent ecosystem.

Jarvies adds an MCP access layer for ChatGPT, Claude Desktop, internal copilots, FinPilot, and future agents. It does not rewrite or replace the existing Teams/M365 bot (Zola), which remains the M365 Agent. M365 tools are imported lazily from the existing `agents.m365` package and wrapped with tenant context and permission checks.

## Architecture

Clients:

- M365 Agent (Zola — Teams bot)
- FinPilot (finance Teams bot)
- ChatGPT
- Claude Desktop
- Internal AI Agents
- Future Web App

Flow:

```text
Clients -> MCP Server -> Tool Modules -> External APIs + PostgreSQL
```

Tool modules:

- Microsoft 365 (Outlook, SharePoint, Calendar)
- Xero
- Cin7
- ClickUp
- Freshsales
- Power BI
- Finance (integration registry)
- PostgreSQL

## Local Setup

From this project:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
pip install "mcp[cli]"
```

Copy `.env.example` to `.env` and set at least:

```text
MCP_API_KEYS=<long-local-secret>
M365_AGENT_PATH=C:/Users/DELL/Documents/agents/m365_agent_v2
```

Generate a local MCP API key:

```powershell
.\scripts\generate_mcp_api_key.ps1
```

Then paste the generated value into `.env`:

```text
MCP_API_KEYS=<generated-key>
```

The existing M365 package may also require its own environment variables, such as `ANTHROPIC_API_KEY`, `AZURE_CLIENT_ID`, and `AZURE_TENANT_ID`, because the MCP wrapper imports that code at tool execution time.

For the Xero, Cin7, and Freshsales tools, set the relevant integration credentials in `.env` (see `.env.example`). Tools whose credentials are absent return `status=not_configured` rather than erroring.

## Run

```powershell
uvicorn agents.mcp.server:app --reload --port 8080
```

Local MCP endpoint:

```text
http://localhost:8080/mcp
```

Health endpoint:

```text
http://localhost:8080/health
```

The server is intentionally not public by default. Use:

```text
X-API-Key: <your MCP_API_KEYS value>
```

or:

```text
Authorization: Bearer <api-key-or-jwt>
```

## MCP Validation Tool

The server includes:

```python
@mcp.tool()
def hello(name: str):
    return f"Hello {name}"
```

Use it first to validate connectivity before enabling API-backed tools.

## Exposed Tools

Wrappers live in `agents/mcp/tools/`. All real tools check permissions and tenant context before executing.

**M365** (`m365_tools.py`) — imports the existing `agents.m365` package:

- `m365_search_emails`
- `m365_read_email`
- `m365_search_sharepoint`
- `m365_search_calendar`
- `m365_create_email_draft`

Deliberately NOT exposed: `send_email`, `delete_email`.

**Cin7** (`cin7_tools.py`) — DEAR/Cin7 Core External API v2, plain `httpx`:

- `cin7_get_inventory` — product master catalog (SKUs, names, pricing, status)
- `cin7_get_stock_levels` — on-hand availability per location
- `cin7_get_sales_orders` — sales orders, status/date filters
- `cin7_get_purchase_orders` — purchase orders, status/date filters

**Xero** (`xero_tools.py`) — Accounting API with OAuth2 refresh-token rotation:

- `xero_get_contacts`
- `xero_get_invoices` — status/date/contact filters
- `xero_get_payments`
- `xero_get_profit_loss`
- `xero_create_invoice` — **currently stubbed**, returns `not_configured`. Write path scheduled.

The Xero service persists rotated refresh tokens to `.secrets/xero_refresh_token.txt`; this file takes precedence over `XERO_REFRESH_TOKEN` from env on startup, so `.env` only ever holds the initial bootstrap value.

**ClickUp** (`clickup_tools.py`) — fundraising across two lists, plain `httpx`:

Read tools:

- `clickup_list_tasks(list_key=..., status=..., priority=..., limit=...)` — task summaries from either list, custom fields decoded to human names
- `clickup_get_task(task_id, list_key=...)` — full task detail with subtasks and last 20 comments
- `clickup_get_tasks_needing_work(list_key=..., statuses=None)` — IR defaults to `["ACTIVE"]`; Pipeline defaults to the in-flight statuses
- `clickup_list_subtasks(parent_task_id, include_completed=True)` — subtasks for blocker tracking
- `clickup_compute_pipeline_totals(list_key="fundraising_pipeline")` — `Estimated Amount` aggregated by status and by `Probability`, with a weighted total (High=0.75, Medium=0.40, Low=0.15)

Write tools:

- `clickup_update_task_field(task_id, field_name, value, list_key=...)` — set one custom field by human name with type-aware validation
- `clickup_set_status(task_id, status)` — set the native ClickUp status. Auto-detects which configured list the task lives in and validates `status` against that list's configured statuses
- `clickup_link_tasks(source_task_id, target_task_id, link_field_name, list_key=...)` — set a `task_relationship` field (e.g. IR `"Linked Application"` → Pipeline task, or Pipeline `"Source Funder"` → IR task). Rejects non-relationship fields
- `clickup_add_comment(task_id, comment_text, notify_assignees=False)` — markdown-supporting audit-trail comment
- `clickup_create_subtask(parent_task_id, name, ..., list_key=...)` — create a per-task checklist item
- `clickup_complete_subtask(subtask_id)` / `clickup_reopen_subtask(subtask_id)` — subtask status toggles

### Workflow

Two ClickUp lists feed the fundraising operation:

- **Investor Relations (IR)** — long-term funder relationship database. Statuses: `ACTIVE`, `NOT A FIT`, `DORMANT`. The DORMANT pool reactivates each funding cycle.
- **Fundraising Pipeline** — active grant/funder applications in flight. Statuses are the workflow itself: `LEAD IDENTIFIED → INTRO REQUESTED → INTRO COMPLETED → PROPOSAL SENT → EVALUATION IN PROGRESS → BOARD REVIEW → …`. Subtasks are used as missing-item checklists per application.

Tasks are linked across the two lists via `task_relationship` fields: an IR funder carries a `Linked Application` pointing at the Pipeline task, and the Pipeline task carries a `Source Funder` pointing back at the IR funder.

### Config

Custom-field UUIDs and native statuses are resolved at startup from the multi-list JSON at `CLICKUP_CUSTOM_FIELDS_CONFIG_PATH` (default `.config/clickup_fields.json`). Generate it with:

```powershell
python scripts/setup_clickup_fields.py
```

The setup script is idempotent: it reads each list's metadata (including configured statuses), reuses fields whose names already match, attempts to create the rest, and writes the resolved UUIDs and statuses. If a field cannot be created via API it is reported as PENDING and must be added in the ClickUp UI before re-running. The script never creates or modifies ClickUp native statuses — it only reads and records them.

Required env vars:

- `CLICKUP_API_TOKEN`
- `CLICKUP_TEAM_ID`
- `CLICKUP_IR_LIST_ID`
- `CLICKUP_PIPELINE_LIST_ID`
- `CLICKUP_CUSTOM_FIELDS_CONFIG_PATH` (optional, defaults to `.config/clickup_fields.json`)

If `CLICKUP_API_TOKEN` is missing or the config file is absent, every ClickUp tool returns `{"status": "not_configured", "missing": [...]}` without firing an API call. ClickUp API errors come back as `{"status": "error", "code": <http>, "message": <str>}` — ClickUp tools never raise.

Deliberately NOT exposed: `delete_task`, `delete_comment`, `delete_subtask`, bulk operations, status creation/modification, or anything that mutates list/field/member structure.

ClickUp uses a flat `Authorization: <token>` header (not `Bearer <token>`) — a documented v2 quirk.

**Freshsales** (`freshsales_tools.py`):

- `freshsales_get_contacts`
- `freshsales_get_accounts`
- `freshsales_get_deals`
- `freshsales_search`

**Power BI** (`powerbi_tools.py`):

- `powerbi_list_reports`
- `powerbi_get_report`
- `powerbi_run_query`

**Finance** (`finance_tools.py`) — meta tools for clients to inspect what's wired:

- `finance_list_systems`
- `finance_get_integration_status`

**Database** (`db_tools.py`) — see "Database Tools" below:

- `db_read_query`
- `db_select`

## Permissions

Every real tool checks:

```python
check_permission(tenant_id, user_id, tool_name, permissions)
```

Supported permissions:

- `read_only`
- `m365_access`
- `finance_access`
- `fundraising_access`
- `admin_access`

Write tools are denied when the caller has `read_only` unless they also have `admin_access`.

## Tenant Context

Tool calls support:

- `tenant_id`
- `user_id`
- `access_token`
- `permissions`

For local smoke tests, defaults can be set with:

```text
MCP_DEFAULT_TENANT_ID=local
MCP_DEFAULT_USER_ID=local-user
MCP_DEFAULT_PERMISSIONS=m365_access,read_only
```

For production, pass tenant and user context explicitly from the MCP client or identity gateway.

## Database Tools

Exposed tools:

- `db_read_query`
- `db_select`

Safety controls:

- read-only transaction by default
- rejects writes and DDL
- rejects multiple statements
- rejects SQL comments
- rejects dangerous functions such as `pg_sleep`
- parameterized asyncpg placeholders: `$1`, `$2`, ...
- row limit and statement timeout

Use a dedicated PostgreSQL read-only user for `DATABASE_URL`.

## Deferred / future work

- `xero_create_invoice` — write path (draft invoices only, not authorised). FinPilot Day 5 work item.
- Multi-tenant credentials store (Fernet-encrypted `tenants` + `tenant_credentials` tables).
- Cancel-scope warning on MCP session cleanup — known SDK behaviour, not currently biting.

## Docker

Build:

```powershell
docker build -f docker/Dockerfile.mcp -t jarvies-mcp .
```

Run:

```powershell
docker run --env-file .env -p 8080:8080 jarvies-mcp
```

If the existing M365 bot package is outside the Docker build context, copy it into the image or mount it and set `M365_AGENT_PATH` accordingly.

## AWS App Runner

Use:

```text
deploy/apprunner.mcp.yaml
```

Required production environment variables:

- `ENVIRONMENT=production`
- `MCP_API_KEYS` or `MCP_JWT_SECRET`
- `DATABASE_URL` when DB tools are enabled
- `M365_AGENT_PATH` if M365 tools are enabled
- existing M365/Azure settings required by the bot package
- Xero / Cin7 / Freshsales / Power BI credentials per the integrations in use

App Runner should expose port `8080`.

## Tests

```powershell
pytest
```

The tests cover permission gates, SQL validation, wrapper behaviour with mocked existing M365 functions, the Xero refresh-token rotation, and the MCP `hello` validation tool when the MCP SDK is installed.
