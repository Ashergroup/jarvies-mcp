# Phase 2C — Build, push, redeploy on ECS

Runbook for shipping the Phase 2A+2B code (DB/tenant + OAuth) to the live ECS
service and verifying it. Run these from a machine with Docker + the AWS CLI
authenticated to account **703671921531**, region **eu-west-1**.

```
AWS account : 703671921531
Region      : eu-west-1
ECR repo    : 703671921531.dkr.ecr.eu-west-1.amazonaws.com/jarvies-mcp
ECS cluster : default
ECS service : jarvies-mcp
Service URL : https://ja-e5a05fec59034d0fa32d8c3dfda06afe.ecs.eu-west-1.on.aws
```

The Dockerfile (`docker/Dockerfile.mcp`) installs deps via `pip install -e .`, so
`msal`, `python-jose[cryptography]`, and `asyncpg` come in from `pyproject.toml`
automatically. It now also copies `.config/clickup_fields.json` (ClickUp field
schema — needed for ClickUp tools) and `scripts/` (so `migrate.py` can be run in
the container).

---

## 1. Build and push the image to ECR

```bash
ACCOUNT=703671921531
REGION=eu-west-1
REPO=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/jarvies-mcp
TAG=$(git rev-parse --short HEAD)   # also tag :latest

# Authenticate Docker to ECR
aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com

# Build for x86 (ECS runs amd64) and push. buildx handles cross-build from an
# ARM/Windows dev box. Run from the repo root (build context = ".").
docker buildx build \
  --platform linux/amd64 \
  -f docker/Dockerfile.mcp \
  -t $REPO:latest \
  -t $REPO:$TAG \
  --push .
```

`--push` builds and pushes in one step. Tagging `:$TAG` (git sha) alongside
`:latest` gives you a traceable, rollback-able image. Verify:

```bash
aws ecr describe-images --repository-name jarvies-mcp --region eu-west-1 \
  --query 'sort_by(imageDetails,&imagePushedAt)[-1].{tags:imageTags,pushed:imagePushedAt}'
```

---

## 2. Add the new environment variables to the task definition

The service is missing: `DATABASE_URL`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`,
`AZURE_REDIRECT_URI`, `AZURE_TENANT_ID`, `JARVIES_TOKEN_SECRET`, `JARVIES_PUBLIC_URL`.

> **Freshsales (flagged):** the Freshsales CRM tools (read + the new write/CRM
> tools) need `FRESHSALES_DOMAIN` and `FRESHSALES_API_KEY`. These are **not** in
> the ECS task definition today (only present commented-out in
> `deploy/apprunner.mcp.yaml` and in `.env.example`). Add both as plaintext
> `environment` entries when the Freshsales integration is in use — or, for
> tenant-scoped use, store them in the `tenant_credentials` table instead (the
> tools resolve credentials DB-first). `FRESHSALES_API_KEY` is a secret; prefer
> the Secrets Manager approach in the optional section at the end.

> **Security recommendation (flagged, not blocking):** `DATABASE_URL`,
> `AZURE_CLIENT_SECRET`, and `JARVIES_TOKEN_SECRET` are secrets. Plaintext task-def
> `environment` values are visible to anyone with `ecs:DescribeTaskDefinition`.
> Prefer ECS `secrets` referencing AWS Secrets Manager / SSM Parameter Store (see
> the optional section at the end). The steps below use plaintext env as the
> spec describes — fine to start, but migrate the three secrets to Secrets
> Manager before this is treated as production-hardened.

### Option A — AWS Console (manual)

1. ECS → **Task definitions** → `jarvies-mcp` → select the latest revision →
   **Create new revision**.
2. Under **Container** → the `jarvies-mcp` container → **Environment variables**.
3. Add each variable (Key / Value). Fill the three `<...>` placeholders with the
   real values before saving — do not paste them anywhere they'd be logged:
   - `DATABASE_URL` = `postgresql://jarvies_admin:<REAL_PASSWORD>@jarvies-db.c58uge2ouuxs.eu-west-1.rds.amazonaws.com:5432/jarvies?sslmode=require`
   - `AZURE_CLIENT_ID` = `82f4503e-369f-4c78-a22b-9eac587d6376`
   - `AZURE_CLIENT_SECRET` = `<AZURE_CLIENT_SECRET>`
   - `AZURE_REDIRECT_URI` = `https://ja-e5a05fec59034d0fa32d8c3dfda06afe.ecs.eu-west-1.on.aws/auth/callback`
   - `AZURE_TENANT_ID` = `d7afc5b8-d7f1-48ba-a6b5-d2f21608bb66`
   - `JARVIES_TOKEN_SECRET` = `<JARVIES_TOKEN_SECRET>` (the value from local `.env`)
   - `JARVIES_PUBLIC_URL` = `https://ja-e5a05fec59034d0fa32d8c3dfda06afe.ecs.eu-west-1.on.aws`
   - Leave the existing `MCP_API_KEYS` and any other current vars untouched.
4. **Create**. Then ECS → **Clusters** → `default` → Services → `jarvies-mcp` →
   **Update service** → set **Revision** to the one you just created →
   check **Force new deployment** → **Update**.

### Option B — AWS CLI

Pull the current task def, inject the new env vars, register a new revision, and
point the service at it. Requires `jq`. **Export the three secrets in your shell
first so they never land in a file:**

```bash
export DB_PW='<REAL_PASSWORD>'
export AZ_SECRET='<AZURE_CLIENT_SECRET>'
export JARVIES_SECRET='<JARVIES_TOKEN_SECRET>'

REGION=eu-west-1
aws ecs describe-task-definition --task-definition jarvies-mcp --region $REGION \
  --query 'taskDefinition' > taskdef.json

# Build the new env list: keep existing vars, add/overwrite the Phase 2C ones.
NEW_ENV=$(jq -n \
  --arg dburl "postgresql://jarvies_admin:${DB_PW}@jarvies-db.c58uge2ouuxs.eu-west-1.rds.amazonaws.com:5432/jarvies?sslmode=require" \
  --arg azid "82f4503e-369f-4c78-a22b-9eac587d6376" \
  --arg azsecret "$AZ_SECRET" \
  --arg azredirect "https://ja-e5a05fec59034d0fa32d8c3dfda06afe.ecs.eu-west-1.on.aws/auth/callback" \
  --arg aztenant "d7afc5b8-d7f1-48ba-a6b5-d2f21608bb66" \
  --arg jsecret "$JARVIES_SECRET" \
  --arg jpublic "https://ja-e5a05fec59034d0fa32d8c3dfda06afe.ecs.eu-west-1.on.aws" \
  '[
     {name:"DATABASE_URL",value:$dburl},
     {name:"AZURE_CLIENT_ID",value:$azid},
     {name:"AZURE_CLIENT_SECRET",value:$azsecret},
     {name:"AZURE_REDIRECT_URI",value:$azredirect},
     {name:"AZURE_TENANT_ID",value:$aztenant},
     {name:"JARVIES_TOKEN_SECRET",value:$jsecret},
     {name:"JARVIES_PUBLIC_URL",value:$jpublic}
   ]')

# Merge: existing env (minus any same-named keys) + new env. Strip the
# read-only fields the register call rejects.
jq --argjson newenv "$NEW_ENV" '
  .containerDefinitions[0].environment =
    ((.containerDefinitions[0].environment // [])
      | map(select(.name as $n | ($newenv | map(.name) | index($n) | not)))) + $newenv
  | {family, taskRoleArn, executionRoleArn, networkMode, containerDefinitions,
     volumes, placementConstraints, requiresCompatibilities, cpu, memory,
     runtimePlatform}
  | with_entries(select(.value != null))
' taskdef.json > taskdef-new.json

aws ecs register-task-definition --region $REGION --cli-input-json file://taskdef-new.json

# Point the service at the newest revision (and roll it out).
aws ecs update-service --cluster default --service jarvies-mcp --region $REGION \
  --task-definition jarvies-mcp --force-new-deployment

# Clean up the local files — they contain the secrets you just injected.
shred -u taskdef.json taskdef-new.json 2>/dev/null || rm -f taskdef.json taskdef-new.json
```

---

## 3. Force a new deployment

(Already triggered by `--force-new-deployment` above; run standalone if you
updated env via the Console without checking that box.)

```bash
aws ecs update-service --cluster default --service jarvies-mcp \
  --region eu-west-1 --force-new-deployment
```

Wait for rollout to stabilise:

```bash
aws ecs wait services-stable --cluster default --services jarvies-mcp --region eu-west-1
```

If a task fails to start, check logs:

```bash
aws logs tail /ecs/jarvies-mcp --since 10m --follow --region eu-west-1
```

(Adjust the log group to whatever the task definition's `awslogs-group` is.)

---

## 4. Run the database migration (one-time, if not already done)

`scripts/migrate.py` is now in the image. With `enableExecuteCommand` on the
service, run it inside a running task (it reads `DATABASE_URL` from the task env):

```bash
TASK=$(aws ecs list-tasks --cluster default --service-name jarvies-mcp \
  --region eu-west-1 --query 'taskArns[0]' --output text)

aws ecs execute-command --cluster default --task "$TASK" \
  --container jarvies-mcp --interactive --region eu-west-1 \
  --command "python scripts/migrate.py"
```

Expected output ends with `migrate: done`. Idempotent — safe to re-run.

---

## 5. Smoke tests (after the deployment is stable)

```bash
BASE=https://ja-e5a05fec59034d0fa32d8c3dfda06afe.ecs.eu-west-1.on.aws

# a. Health — expect {"status":"ok","service":"jarvies",...}
curl -s $BASE/health

# b. OAuth discovery — expect the AS metadata JSON
curl -s $BASE/.well-known/oauth-authorization-server

# c. Authorize — expect HTTP 302 to login.microsoftonline.com
curl -s -o /dev/null -w "%{http_code} %{redirect_url}\n" \
  "$BASE/authorize?client_id=jarvies-claude-client&response_type=code&redirect_uri=$BASE/auth/callback&state=test123&code_challenge=abc123&code_challenge_method=S256"

# d. MCP tools via X-API-Key (backward compat) — expect a tools/list result
curl -s -X POST $BASE/mcp/ \
  -H "X-API-Key: <MCP_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

What "good" looks like:
- (b) `issuer` equals the `JARVIES_PUBLIC_URL` you set, with `authorization_endpoint`,
  `token_endpoint`, `registration_endpoint`, and `code_challenge_methods_supported: ["S256"]`.
- (c) the redirect `Location` points at
  `https://login.microsoftonline.com/common/oauth2/v2.0/authorize` with
  `state=test123` preserved and `client_id` = the Azure app id.
  **Note:** the `code_challenge` in that redirect is **not** `abc123` — Jarvies
  generates its own PKCE pair for the Microsoft leg. That is correct, not a bug.
- (d) the MCP streamable-HTTP transport may answer with `text/event-stream`; the
  JSON-RPC result with the tool list is in the event data. A `401` here means
  `MCP_API_KEYS` isn't set or the key is wrong.

---

## 6. Add the claude.ai custom connector (manual — Kuda)

1. claude.ai → **Settings** → **Connectors** (a.k.a. Integrations) → **Add custom connector** / **Add custom integration**.
2. **Name:** `Jarvies`
3. **URL:** `https://ja-e5a05fec59034d0fa32d8c3dfda06afe.ecs.eu-west-1.on.aws`
   (claude.ai appends `/.well-known/oauth-authorization-server` itself to discover the flow — give it the base URL, not the `/mcp` path.)
4. Save. claude.ai discovers the endpoints, then prompts to authorize.
5. Click **Connect** → you're redirected to **Microsoft sign-in** → sign in with
   the Asher Group / Niche Group account and consent.
6. After the callback completes you're returned to claude.ai with the connector
   **connected**.
7. Verify the **ClickUp tools** (`clickup_list_tasks`, `clickup_compute_pipeline_totals`, etc.)
   appear in claude.ai's tool list and return data scoped to your tenant.

Prerequisites for step 5 to succeed:
- The Azure app registration's **Redirect URI** must exactly equal
  `AZURE_REDIRECT_URI` (`…/auth/callback`).
- `AZURE_CLIENT_SECRET`, `JARVIES_TOKEN_SECRET`, and `DATABASE_URL` must be set in
  the task env (Step 2), and the migration (Step 4) must have run so the tenant
  row exists.

---

## Optional — move the three secrets to Secrets Manager (recommended)

Instead of plaintext `environment`, store each secret and reference it via the
container's `secrets` block:

```bash
aws secretsmanager create-secret --name jarvies/DATABASE_URL --secret-string '<dsn>' --region eu-west-1
aws secretsmanager create-secret --name jarvies/AZURE_CLIENT_SECRET --secret-string '<secret>' --region eu-west-1
aws secretsmanager create-secret --name jarvies/JARVIES_TOKEN_SECRET --secret-string '<secret>' --region eu-west-1
```

Then in the task definition container:

```json
"secrets": [
  {"name": "DATABASE_URL", "valueFrom": "arn:aws:secretsmanager:eu-west-1:703671921531:secret:jarvies/DATABASE_URL"},
  {"name": "AZURE_CLIENT_SECRET", "valueFrom": "arn:aws:secretsmanager:eu-west-1:703671921531:secret:jarvies/AZURE_CLIENT_SECRET"},
  {"name": "JARVIES_TOKEN_SECRET", "valueFrom": "arn:aws:secretsmanager:eu-west-1:703671921531:secret:jarvies/JARVIES_TOKEN_SECRET"}
]
```

The task execution role needs `secretsmanager:GetSecretValue` on those ARNs.
Keep the non-secret vars (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_REDIRECT_URI`,
`JARVIES_PUBLIC_URL`) as plain `environment` entries.
```
