# Deploying Jarvies MCP to AWS App Runner

Phase 1 deploy: ship the server as-is with the current `X-API-Key` auth, from a
private GitHub repo, into **eu-west-1 (Ireland)**. No OAuth, no multi-tenancy —
those are Phase 2/3.

This guide is the runbook for the actual deploy. Read it top to bottom once
before clicking anything in the Console.

---

## 1. Prerequisites

- An AWS account with permission to create App Runner services and IAM roles.
- The code pushed to the private GitHub repo
  `https://github.com/Ashergroup/jarvies-mcp.git` (see the push commands printed
  by the setup step / the project root instructions).
- Git installed locally.
- A GitHub connection authorised in App Runner (Console creates this on first
  use — see step 3).
- The list of secret values ready to paste (MCP API key, ClickUp token, Xero /
  Cin7 / Freshsales creds as applicable). **Generate a fresh MCP API key** with
  `scripts/generate_mcp_api_key.ps1` — do not reuse a local dev key.

Build strategy: **App Runner source build** driven by
[`deploy/apprunner.mcp.yaml`](apprunner.mcp.yaml) (`runtime: python311`). The
repo also ships a working `docker/Dockerfile.mcp` if you later switch to image
based deploys, but the two are not used together — pick one. Phase 1 uses the
source build.

---

## 2. What the deploy does NOT include

- **M365 tools do not work in this deploy.** They import the external
  `agents.m365` package (set by `M365_AGENT_PATH`), which is not part of this
  repo and not present on the container. Calls to `m365_*` tools will raise a
  runtime error until that package is bundled in a later phase. Everything else
  (Xero, Cin7, ClickUp, Freshsales, Power BI, DB, finance, `hello`) runs
  normally when its credentials are set.
- No OAuth endpoints. Auth is `X-API-Key` (or `Authorization: Bearer <key>`).

---

## 3. Step-by-step: deploy from GitHub source

1. **App Runner Console** → region **eu-west-1** → *Create service*.
2. **Source**: *Source code repository* → *Add new* GitHub connection →
   authorise → pick `Ashergroup/jarvies-mcp`, branch `main`.
3. **Deployment trigger**: *Automatic* (redeploys on push to `main`) or *Manual*.
   Automatic is fine for Phase 1.
4. **Configure build**: choose *Use a configuration file*. App Runner reads
   `apprunner.mcp.yaml` from the repo root by default — if the Console asks for
   a path, point it at `deploy/apprunner.mcp.yaml`. (If your Console version
   only auto-detects a root `apprunner.yaml`, copy the file to the repo root, or
   set the config file path field to `deploy/apprunner.mcp.yaml`.)
5. **Service settings**:
   - Service name: `jarvies-mcp`.
   - CPU/Memory: **1 vCPU / 2 GB** is comfortable; **0.25 vCPU / 0.5 GB** is the
     floor and works but is tight (see Gotchas — needs ~512 MB minimum).
   - Port: **8080** (already set by the config file).
6. **Environment variables** — THIS IS THE STEP THAT BREAKS DEPLOYS IF SKIPPED.
   Add every required secret here *before* the first deploy (see section 4).
   The config file deliberately ships secrets as commented entries, so they are
   not set unless you add them in the Console.
7. **Health check**: protocol HTTP, path `/health` (no auth required on that
   path). Defaults for interval/threshold are fine.
8. *Create & deploy*. First build takes **10–15 minutes**.

---

## 4. Environment variables

### Required (deploy is broken / locked without these)

| Var | Why |
|-----|-----|
| `MCP_API_KEYS` | The X-API-Key auth. **Without it the `/mcp` endpoint returns 503 in production.** Comma-separated; any one value is accepted. |

### Set in the config file already (do not duplicate in Console)

`ENVIRONMENT=production`, `PORT=8080`, `MCP_LOG_LEVEL=INFO`,
`MCP_ALLOW_UNAUTHENTICATED=false`, `MCP_DEFAULT_TENANT_ID`,
`MCP_DEFAULT_USER_ID`, `MCP_DEFAULT_PERMISSIONS=read_only`,
`CLICKUP_CUSTOM_FIELDS_CONFIG_PATH`, `MCP_DB_READONLY=true`.

> `MCP_DEFAULT_PERMISSIONS` is `read_only` on purpose. **Do not** add
> `admin_access` to the default set in production — callers should pass explicit
> permissions per request. The server logs an ERROR at startup if it sees
> `admin_access` in the default set in production.

### Required per integration you actually use

| Integration | Vars |
|-------------|------|
| ClickUp (fundraising) | `CLICKUP_API_TOKEN`, `CLICKUP_TEAM_ID`, `CLICKUP_IR_LIST_ID`, `CLICKUP_PIPELINE_LIST_ID` |
| Xero | `XERO_CLIENT_ID`, `XERO_CLIENT_SECRET`, `XERO_REFRESH_TOKEN`, `XERO_TENANT_ID` |
| Cin7 | `CIN7_ACCOUNT_ID`, `CIN7_API_KEY` |
| Freshsales | `FRESHSALES_DOMAIN`, `FRESHSALES_API_KEY` |
| M365 tools | `ANTHROPIC_API_KEY` (+ the external `agents.m365` package — not available in Phase 1) |

Any integration whose credentials are absent returns
`{"status": "not_configured", ...}` instead of erroring — so it is safe to
deploy with only the integrations you need set.

### Optional

- `DATABASE_URL` — only when the DB tools (`db_read_query`, `db_select`) are
  used. Use a dedicated read-only PostgreSQL role.

### Declared for Phase 2 (leave unset in Phase 1)

- `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`.

> **Precedence note:** values set in the App Runner Console take effect for the
> service. The config file only sets the non-secret defaults listed above; it
> never carries secrets. Keep secrets in the Console only.

---

## 5. Verify the deploy succeeded

App Runner gives the service a default HTTPS domain like
`https://xxxxxxxx.eu-west-1.awsapprunner.com`. HTTPS is automatic — no certs to
manage. Use that as `<apprunner-url>` below.

```bash
# Health check (no auth) — expect: {"status":"ok","service":"jarvies",...}
curl https://<apprunner-url>/health

# MCP endpoint check (with auth — expects X-API-Key)
curl -X POST https://<apprunner-url>/mcp/ \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <key>" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}},"id":1}'
```

The second call returns a JSON-RPC result with the server's
`protocolVersion`, `capabilities`, and `serverInfo` (name `jarvies`). The MCP
streamable-HTTP transport may respond with an SSE `text/event-stream` body and
set an `mcp-session-id` header — that is expected; the JSON-RPC payload is in
the event data.

Negative checks worth running once:

```bash
# No key → 401 unauthorized (proves auth is on)
curl -i -X POST https://<apprunner-url>/mcp/ -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{},"id":1}'

# If you ever see 503 mcp_auth_not_configured → MCP_API_KEYS is not set.
```

---

## 6. Point Claude Desktop at the deployed server

Claude Desktop talks to a remote MCP server over streamable HTTP. In
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "jarvies": {
      "type": "http",
      "url": "https://<apprunner-url>/mcp/",
      "headers": {
        "X-API-Key": "<your MCP_API_KEYS value>"
      }
    }
  }
}
```

Restart Claude Desktop. Validate connectivity by calling the `hello` tool first
(it needs no integration credentials). On a client that only supports a stdio
bridge, use `mcp-remote` to wrap the URL and pass the same header.

---

## 7. Logs

- **App Runner Console** → service → *Logs* tab. Two streams:
  - *Event log* — build/deploy lifecycle (use this when a deploy fails).
  - *Application log* — stdout from uvicorn / the app, including the startup
    safety warnings (`mcp_production_no_auth_configured`,
    `mcp_production_admin_default_permissions`).
- Both stream to **CloudWatch Logs** under
  `/aws/apprunner/jarvies-mcp/<id>/application` and `.../service`. Query there
  with Logs Insights for anything beyond the last screenful.

---

## 8. Cost estimate

App Runner bills two things:

- **Provisioned memory** — billed whenever the instance exists (even idle), at
  roughly **$0.007/GB-hour** in eu-west-1.
- **Active CPU** — billed only while handling requests, at roughly
  **$0.064/vCPU-hour**.

Rough monthly figures (~730 hrs), eu-west-1, prices approximate — confirm
against the current AWS App Runner pricing page:

| Config | Idle memory cost/mo | + light CPU use | Notes |
|--------|--------------------|-----------------|-------|
| 0.25 vCPU / 0.5 GB | ~$2.50 | a few $ | floor; tight |
| 1 vCPU / 2 GB | ~$10 | ~$15–25 | comfortable |

A low-traffic internal MCP server lands in the **single-to-low-double-digit
dollars per month** range. There is no free tier guarantee — assume you pay for
provisioned memory 24/7. App Runner can scale to zero only via *pause*, which is
manual; auto-scaling does not zero out memory billing.

---

## 9. Common gotchas

- **First deploy takes 10–15 min.** The build installs deps from scratch.
  Subsequent deploys are faster.
- **Set ALL required env vars BEFORE the first deploy.** A missing `MCP_API_KEYS`
  makes `/mcp` return 503; the deploy itself will look "successful" but the
  endpoint is locked. Re-check section 4.
- **Memory floor ~512 MB.** 0.25 vCPU / 0.5 GB works but is tight with the MCP
  SDK + integrations loaded; bump to 2 GB if you see OOM restarts.
- **HTTPS is automatic** via the default `*.awsapprunner.com` domain. No certs.
- **Health check path is `/health`** and is public (no auth). If you point the
  health check at `/mcp` it will fail and App Runner will roll the deploy back.
- **Config file location.** If the Console doesn't pick up
  `deploy/apprunner.mcp.yaml`, either set the config-file path field explicitly
  or copy it to a root `apprunner.yaml`.
- **M365 tools error** in this deploy (external package not bundled) — expected
  in Phase 1. Don't treat their failure as a broken deploy.
- **Trailing slash on `/mcp/`.** The streamable-HTTP app mounts at `/mcp/`; use
  the trailing slash in client config and curl tests.
