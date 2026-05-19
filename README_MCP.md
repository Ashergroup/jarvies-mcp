# Jarvies MCP

Production-ready MCP server layer for the existing M365 Agent ecosystem.

Jarvies adds an MCP access layer for ChatGPT, Claude Desktop, internal copilots, and future agents. It does not rewrite or replace the existing Teams/M365 bot, which remains the M365 Agent. M365 tools are imported lazily from the existing `agents.m365` package and wrapped with tenant context and permission checks.

## Architecture

Clients:

- Teams Bot
- ChatGPT
- Claude Desktop
- Internal AI Agents
- Future Web App

Flow:

```text
Clients -> MCP Server -> Tool Modules -> External APIs + PostgreSQL
```

Tool modules:

- Microsoft 365
- SharePoint
- Outlook
- Calendar
- Xero
- Cin7
- Power BI
- Finance systems
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

## M365 Wrappers

Exposed tools:

- `m365_search_emails`
- `m365_read_email`
- `m365_search_sharepoint`
- `m365_search_calendar`
- `m365_create_email_draft`

Not exposed:

- `send_email`
- `delete_email`

The wrappers import the existing code, for example:

```python
agents.m365.tools.read_tools.search_emails
agents.m365.tools.read_tools.search_sharepoint
agents.m365.tools.write_tools.create_draft
```

If an MCP call supplies `access_token`, the wrapper passes it into the existing connector path for read tools and temporarily patches draft token acquisition for `create_draft`. This preserves the existing Teams bot code and keeps the MCP layer additive.

## Permissions

Every real tool checks:

```python
check_permission(tenant_id, user_id, tool_name, permissions)
```

Supported permissions:

- `read_only`
- `m365_access`
- `finance_access`
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

## Future Integrations

Scaffolded tools return `not_configured` until service clients are added:

- Xero: contacts, invoices, payments, invoice creation, profit/loss
- Cin7: inventory, stock levels, sales orders, purchase orders
- Power BI: reports and query execution
- Finance: integration registry/status

Each module has a service boundary so API-specific clients can be added without changing MCP registration or permission checks.

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

App Runner should expose port `8080`.

## Tests

```powershell
pytest
```

The tests cover permission gates, SQL validation, wrapper behavior with mocked existing M365 functions, and the MCP `hello` validation tool when the MCP SDK is installed.
