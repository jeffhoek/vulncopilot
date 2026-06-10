# Deploying to Azure App Service

This guide walks through deploying the CISA KEV + NVD RAG chatbot to Azure App Service as a Linux container, using Azure Container Registry for images, Azure Key Vault for secrets, and Timescale Cloud (hosted pgvector) as the database. CI/CD runs via Azure Pipelines with Workload Identity Federation — no static credentials.

Two requirements shape the infrastructure design:
- **WebSocket support** (Chainlit) → ARR sticky sessions (`clientAffinityEnabled: true`)
- **120s+ startup time** (pgvector schema init) → `WEBSITE_CONTAINER_START_TIME_LIMIT: 230`

---

## Architecture

```
GitHub (source) → Azure Pipelines → ACR (images)
                                        ↓
                              App Service (Linux container)
                                ↓              ↓
                           Key Vault    Timescale Cloud
                                ↑         (pgvector)
                     User-Assigned Managed Identity (RBAC)

Azure Policy  → governs resource group
Azure Bicep   → provisions all infrastructure (Resource Manager)
```

---

## Resource Names

Pattern: `{type}-chainlit-rag-{env}` (globally unique resources drop hyphens)

| Resource | Dev |
|---|---|
| Resource Group | `rg-chainlit-rag-dev` |
| Managed Identity | `id-chainlit-rag-dev` |
| Container Registry | `acrchainlitragdev` |
| App Service Plan | `asp-chainlit-rag-dev` |
| App Service | `app-chainlit-rag-dev` |
| Key Vault | `kv-chainlit-rag-dev` |

> Storage Account and Blob Container have been removed. RAG data lives in Timescale Cloud (pgvector).

---

## Prerequisites

- Azure CLI (`az`) authenticated to the target subscription
- Azure DevOps project created (e.g., `chainlit-rag`)
- Contributor access on the target resource group (or subscription for first deploy)
- `az bicep upgrade` run at least once (Bicep CLI 0.18+ required for `.bicepparam`)
- Timescale Cloud service provisioned with pgvector extension enabled

---

## Step 1: Create the Resource Group (one-time)

```bash
az group create \
  --name rg-chainlit-rag-dev \
  --location eastus \
  --tags environment=dev application=chainlit-rag
```

### 1.1 Register resource providers (one-time, subscription-scoped)

The infrastructure uses three resource provider namespaces that may not be
registered on a fresh subscription. Bicep can't register them for you — an
unregistered namespace fails the deploy with `MissingSubscriptionRegistration`.

| Namespace | Needed for |
|---|---|
| `Microsoft.App` | Container Apps Environment + scheduled ETL job |
| `Microsoft.OperationalInsights` | Log Analytics workspace (backs the Container Apps env) |
| `Microsoft.Communication` | Azure Communication Services + Email (ETL results email) |

Register them once (idempotent; `--wait` blocks until `Registered`, ~1–5 min each):

```bash
az account set --subscription <subscription-id>
for ns in Microsoft.App Microsoft.OperationalInsights Microsoft.Communication; do
  az provider register --namespace "$ns" --wait
done

# Verify
for ns in Microsoft.App Microsoft.OperationalInsights Microsoft.Communication; do
  echo "$ns: $(az provider show -n $ns --query registrationState -o tsv)"
done
```

> Provider registration requires `*/register/action` at **subscription** scope
> (Owner/Contributor have it). The pipeline's resource-group-scoped service principal
> does not, so do this once with a subscription-privileged login. The pipeline's
> "Register resource providers" step only *attempts* registration for namespaces that
> aren't already `Registered`, so it won't fail on permissions once this is done.

---

## Step 2: Azure DevOps Setup (one-time)

Two portals are used here: **Azure DevOps** (dev.azure.com) for pipelines/service connections/environments, and **Azure Portal** (portal.azure.com) for Azure resources and Entra ID.

### 2.0 Connect Azure DevOps to your Entra ID tenant (new organizations only)

In **Azure DevOps** → **Organization Settings** → **Microsoft Entra** → **Connect directory** → select your tenant → confirm.

### 2.1 Create service connections

In **Azure DevOps** → your project → **Project Settings** → **Pipelines** → **Service connections** → **New service connection**:

**GitHub** (`github-chainlit-rag`):
- Type: **GitHub**, auth: **Grant authorization**, OAuth: **AzurePipelines**
- Click **Authorize**, name it `github-chainlit-rag`, check **Grant access permission to all pipelines** → **Save**

**Azure Resource Manager** (`azure-chainlit-rag`):
- Type: **Azure Resource Manager**, identity: **App registration (automatic)**, credential: **Workload identity federation**
- Scope: **Subscription**, resource group: `rg-chainlit-rag-dev`
- Name it `azure-chainlit-rag`, check **Grant access permission to all pipelines** → **Save**

> If the resource group dropdown shows "Loading..." indefinitely, complete step 2.0 first.

### 2.2 Create the Pipeline

In **Azure DevOps** → **Pipelines** → **New pipeline** → **GitHub** → select `chainlit-pydanticai-rag` → **Existing Azure Pipelines YAML file** → branch `main`, path `/azure-pipelines.yml` → **Continue** → **Save** (do not run yet).

### 2.3 Add the pipeline service principal Object ID to the pipeline

Saving the ARM service connection creates an **Enterprise Application** in Entra ID. You need its Object ID — this is different from the App Registration's Object ID shown in the Azure DevOps service connection details.

In **Azure Portal** → **Microsoft Entra ID** → **Enterprise applications** → search for the auto-generated name matching your org and project (e.g. `jeffreyscotthoekman0908-chainlit-pg-<guid>`) → **Overview** → copy the **Object ID**.

Set it as a pipeline UI variable (no YAML edit needed):

In **Azure DevOps** → **Pipelines** → select the pipeline → **Edit** → **Variables** (top-right) → **New variable** → name: `PIPELINE_SP_OBJECT_ID`, value: `<object-id>`, uncheck **Keep this value secret** → **Save**.

### 2.4 Create the Deployment Environment

In **Azure DevOps** → **Pipelines** → **Environments** → **New environment** → name: `chainlit-rag-dev`, resource: **None** → **Create**.

### 2.5 Grant the pipeline service principal Owner on the resource group

The Bicep `rbac` module creates role assignments, which requires `Owner` (not `Contributor`) for the first run. After the first successful deploy, Bicep downgrades the pipeline SP to the minimal roles it needs (ACR Push, Website Contributor).

```bash
az role assignment create \
  --role "Owner" \
  --assignee-object-id <PIPELINE_SP_OBJECT_ID> \
  --assignee-principal-type ServicePrincipal \
  --scope /subscriptions/<subscription-id>/resourceGroups/rg-chainlit-rag-dev
```

---

## Step 3: Deploy Infrastructure (Bicep)

The pipeline does this automatically on every push to `main`. To deploy manually:

### Set env
```
PIPELINE_SP_OBJECT_ID=<pipeline-sp-object-id>
```

### Dry run — shows what will change
```bash
az deployment group what-if \
  --resource-group rg-chainlit-rag-dev \
  --template-file infra/main.bicep \
  --parameters infra/parameters.dev.bicepparam \
  --parameters pipelineServicePrincipalObjectId=$PIPELINE_SP_OBJECT_ID
```

### Apply
```bash
az deployment group create \
  --resource-group rg-chainlit-rag-dev \
  --template-file infra/main.bicep \
  --parameters infra/parameters.dev.bicepparam \
  --parameters pipelineServicePrincipalObjectId=$PIPELINE_SP_OBJECT_ID \
  --mode Incremental
```

**Module deployment order** (enforced by `dependsOn` in `main.bicep`):

1. `identity` — User-Assigned Managed Identity (outputs feed everything else)
2. `acr` — Container Registry (admin disabled; Managed Identity pull only)
3. `keyVault` — Key Vault (RBAC authorization model, soft delete 7 days)
4. `appService` — App Service Plan (B2) + Web App with all app settings and KV references
5. `rbac` — All role assignments (must complete before App Service resolves KV refs)
6. `policy` — Azure Policy assignments (HTTPS-only, require `environment`/`application` tags)
7. `etlJob` — Container Apps Environment + scheduled ETL job (depends on `rbac` for ACR pull / KV read)

---

## Step 4: Provision Key Vault Secrets (one-time)

Secrets are not created by Bicep. Run these after the first successful infrastructure deploy.

### 4.0 Grant yourself write access to Key Vault (one-time)

The Key Vault uses the RBAC authorization model — no one has access by default, including the person who deployed it. The Bicep `rbac` module only grants the App Service's managed identity read access. You must explicitly grant yourself write access before you can set secrets.

```bash
USER_OID=$(az ad signed-in-user show --query id -o tsv)

az role assignment create \
  --role "Key Vault Secrets Officer" \
  --assignee-object-id $USER_OID \
  --assignee-principal-type User \
  --scope $(az keyvault show \
      --name kv-chainlit-rag-dev \
      --resource-group rg-chainlit-rag-dev \
      --query id -o tsv)
```

### 4.1 Set the secrets

Use the bash `for` loop with `read` shell built-in to securely enter the env vars:
```bash
for var in ANTHROPIC_API_KEY OPENAI_API_KEY APP_PASSWORD CHAINLIT_AUTH_SECRET PG_DATABASE_URL PG_DATABASE_URL_READONLY MCP_API_KEY LOGFIRE_TOKEN NVD_API_KEY; do
  echo "$var" && read -rs $var
done
```

> **Two database URLs, two roles** (see [docs/supabase-readonly-role.md](supabase-readonly-role.md)):
> - `PG_DATABASE_URL` → **write/admin** role (`app_etl`/`postgres`) → stored as `database-url`, used by the **ETL job**. Needs DDL since the loaders create tables/indexes.
> - `PG_DATABASE_URL_READONLY` → **read-only** role (`app_readonly`) → stored as `database-url-readonly`, used by the **live app**. The app runs with `DB_INIT_SCHEMA=false` so it never attempts schema DDL.
>
> Create the schema once with the admin connection (or let the ETL job's first run
> create it) **before** the read-only app serves traffic.

Create the Azure Key Vault secrets:
```bash
az keyvault secret set \
  --vault-name kv-chainlit-rag-dev \
  --name anthropic-api-key \
  --value "$ANTHROPIC_API_KEY"

az keyvault secret set \
  --vault-name kv-chainlit-rag-dev \
  --name openai-api-key \
  --value "$OPENAI_API_KEY"

az keyvault secret set \
  --vault-name kv-chainlit-rag-dev \
  --name app-password \
  --value "$APP_PASSWORD"

az keyvault secret set \
  --vault-name kv-chainlit-rag-dev \
  --name chainlit-auth-secret \
  --value "$CHAINLIT_AUTH_SECRET"

az keyvault secret set \
  --vault-name kv-chainlit-rag-dev \
  --name database-url \
  --value "$PG_DATABASE_URL"

az keyvault secret set \
  --vault-name kv-chainlit-rag-dev \
  --name database-url-readonly \
  --value "$PG_DATABASE_URL_READONLY"

az keyvault secret set \
  --vault-name kv-chainlit-rag-dev \
  --name mcp-api-key \
  --value "$MCP_API_KEY"

az keyvault secret set \
  --vault-name kv-chainlit-rag-dev \
  --name logfire-token \
  --value "$LOGFIRE_TOKEN"

az keyvault secret set \
  --vault-name kv-chainlit-rag-dev \
  --name nvd-api-key \
  --value "$NVD_API_KEY"
```

> `database-url-readonly` must exist before the pipeline runs — the App Service
> resolves it as its `PG_DATABASE_URL`. If absent, the KV reference fails and the
> app can't connect. (The ETL job uses the separate `database-url` write/admin secret.)

> `mcp-api-key` must exist before the pipeline runs — if absent, the App Service
> will start with a warning and the `/mcp` route will reject all requests with 401.

> `logfire-token` must exist before the pipeline runs — if absent, the App Service
> will fail to resolve the KV reference and Logfire tracing will not start.
> Retrieve the token from your Logfire project settings under **API Tokens**.
> `LOGFIRE_ENABLED` is controlled by `logfireEnabled` in `infra/parameters.dev.bicepparam`
> (defaults to `false` in the Bicep module; set to `true` in the param file to enable).

> `nvd-api-key` is consumed by the scheduled ETL job (see [Scheduled ETL Refresh](#scheduled-etl-refresh-container-apps-job)),
> not the App Service. The job still runs without it, but NVD throttles unauthenticated
> callers to 5 req/30s (vs 50 with a key), so a multi-week backfill will be far slower.
> Request a free key at <https://nvd.nist.gov/developers/request-an-api-key>.

Restart the App Service to re-resolve the Key Vault references:

```bash
az webapp restart \
  --name app-chainlit-rag-dev \
  --resource-group rg-chainlit-rag-dev
```

---

## Step 5: Load KEV and NVD Data (one-time)

Set `DATABASE_URL` in your `.env` pointing to Timescale Cloud, then follow the [Data Loading guide](data-loading.md) to populate the database.

> **Timescale Cloud note:** The connection string must include `?sslmode=require` — connections without SSL will be refused.

---

## Step 6: Trigger the Pipeline

Push to `main` to trigger all three stages automatically:

```
Build       → docker build + push to ACR (tags: <git-sha-short>, latest)
DeployInfra → Bicep what-if + incremental deploy
DeployApp   → az webapp config container set + restart + /healthz poll (240s)
```

To trigger manually without a code change:

```bash
az pipelines run --name chainlit-pydanticai-rag
```

---

## Verification

```bash
# 1. All resources provisioned
az resource list --resource-group rg-chainlit-rag-dev -o table

# 2. KV references resolved (look for "Resolved" in the value column, not "Failed")
az webapp config appsettings list \
  --name app-chainlit-rag-dev \
  --resource-group rg-chainlit-rag-dev \
  --query "[?contains(value, '@Microsoft.KeyVault')]" -o table

# 3. Image present in ACR
az acr repository show-tags \
  --name acrchainlitragdev \
  --repository chainlit-pydanticai-rag \
  -o table

# 4. Health check responds 200
curl -v https://app-chainlit-rag-dev.azurewebsites.net/healthz

# 5. App logs confirm successful DB connection
az webapp log tail \
  --name app-chainlit-rag-dev \
  --resource-group rg-chainlit-rag-dev
# Expected: successful startup with no DB connection errors
# Test query: "What vulnerabilities affect Apache?"

# 6. WebSocket: open app in browser → DevTools → Network → WS tab → active connection

# 7. Sticky sessions: check browser cookies for ARRAffinity cookie after login

# 8. Logfire KV reference resolved and tracing active
az webapp config appsettings list \
  --name app-chainlit-rag-dev \
  --resource-group rg-chainlit-rag-dev \
  --query "[?name=='LOGFIRE_TOKEN']" -o table
# Expected: value shows "@Microsoft.KeyVault(...)" — if "Failed", check KV permissions
# Then send a message in the app and confirm traces appear at https://logfire.pydantic.dev
```

---

## Redeploying

**New code:** push to `main` — pipeline handles everything.

**Updated secret value:**

```bash
az keyvault secret set \
  --vault-name kv-chainlit-rag-dev \
  --name <secret-name> \
  --value "<new-value>"

az webapp restart \
  --name app-chainlit-rag-dev \
  --resource-group rg-chainlit-rag-dev
```

**Reload KEV/NVD data** (e.g., after CISA publishes new entries):

```bash
uv run python scripts/load_kev.py
uv run python scripts/load_nvd.py
# No app restart needed — data is queried live from Timescale Cloud
```

For an automated weekly refresh instead of running these by hand, see
[Scheduled ETL Refresh](#scheduled-etl-refresh-container-apps-job) below.

---

## Scheduled ETL Refresh (Container Apps Job)

The KEV and NVD datasets need periodic refreshes. Rather than running the loaders
by hand, a **Container Apps Job** (`job-chainlit-rag-etl-<env>`) runs them on a cron
schedule — by default Mondays 06:00 UTC. It reuses the web app's container image,
managed identity, ACR, and Key Vault, and scales to zero between runs (you pay only
for the few minutes each weekly run takes).

Provisioned by `infra/modules/etl-job.bicep` and wired into `main.bicep` as Step 7.

### Why a Container Apps Job (not Functions / WebJobs)

- **No timeout ceiling that matters** — `replicaTimeout` is set to 2h; a multi-week
  catch-up backfill won't be killed mid-run. Azure Functions on the Consumption plan
  caps at 10 minutes, which is too short for a large Phase 2 modified-CVE sweep.
- **Reuses the existing stack** — same image in ACR, same user-assigned identity
  (already has `AcrPull` + `Key Vault Secrets User` from the `rbac` module), same
  Key Vault secrets. No new auth model, no new role assignments.
- **Serverless billing** — the job has no idle replicas between Mondays.

### What it runs

```
load_nvd_full.py --incremental   # full-NVD incremental FIRST
load_kev.py                      # then KEV catalog
load_nvd.py                      # then KEV-scoped NVD enrichment
```

> **Order is deliberate.** `load_kev.py` / `load_nvd.py` upsert KEV CVEs into
> `nvd_vulnerabilities` with recent `last_modified` / `published` dates. The full
> incremental derives its start from `MAX(last_modified)` / `MAX(published)` across
> that table, so if the KEV loaders run *first* they advance the high-water mark and
> the incremental only re-scans the last day — silently skipping everything else.
> Running `load_nvd_full.py --incremental` first avoids the poisoned watermark.

### Secrets

The job reads three Key Vault secrets via the managed identity (Container Apps
secret refs, not App Service `@Microsoft.KeyVault(...)` references):

| Env var | Key Vault secret | Notes |
|---|---|---|
| `OPENAI_API_KEY` | `openai-api-key` | embeddings (shared with the web app) |
| `PG_DATABASE_URL` | `database-url` | target database (shared with the web app) |
| `NVD_API_KEY` | `nvd-api-key` | NVD rate limit 50/30s vs 5/30s — add in Step 4.1 |

### Adjusting the schedule

Override `etlCronExpression` (UTC) at deploy time or in `parameters.dev.bicepparam`:

```bicep
param etlCronExpression = '0 6 * * 1'   // default: Mondays 06:00 UTC
```

### Operating the job

```bash
# Trigger an immediate run (e.g. to catch up after a missed window)
az containerapp job start \
  --name job-chainlit-rag-etl-dev \
  --resource-group rg-chainlit-rag-dev

# List recent executions and their status
az containerapp job execution list \
  --name job-chainlit-rag-etl-dev \
  --resource-group rg-chainlit-rag-dev \
  --query "[].{name:name, status:properties.status, start:properties.startTime}" -o table

# Stream logs for a specific execution
az containerapp job logs show \
  --name job-chainlit-rag-etl-dev \
  --resource-group rg-chainlit-rag-dev \
  --execution <execution-name> --follow
```

> **Recovering from a long gap.** A weekly schedule keeps the watermark current, so
> each run only syncs ~7 days. If runs were paused for weeks, the incremental's
> auto-derived start may be too recent (especially if KEV loaders ran in between) —
> identify the last full-sync date from the per-day `last_modified` counts and pass
> `--since <date>` for a one-off manual catch-up before relying on the schedule again.
> See the [Data Loading guide](data-loading.md) for the `--since` workflow.

---

## MCP Server

The `/mcp` endpoint is co-hosted on the same App Service container as the Chainlit UI. No additional Azure resources are required.

### Architecture

```
Azure App Service (existing container)
├── / → Chainlit WebSocket UI           (unchanged)
├── /healthz → health check             (unchanged)
└── /mcp → FastMCP Streamable HTTP      (new)
      ├── tool: retrieve                (semantic search)
      └── tool: query                   (direct SQL)
```

All traffic uses the same HTTPS endpoint, same managed identity, same Key Vault references, and the same asyncpg connection pool.

### Transport

Streamable HTTP is used instead of SSE — it is stateless per-request and avoids Azure App Service idle timeout issues with long-lived SSE connections. Requires MCP spec 2025-03-26+ clients (Claude Desktop, etc.).

### Authentication

The `/mcp` route requires an `X-API-Key` header. The key is stored in Key Vault as `mcp-api-key` and injected via the Key Vault reference pattern (see Step 4.1 above). Generate a key with:

```bash
openssl rand -hex 32
```

See [docs/mcp-server.md](mcp-server.md) for the full MCP server operational guide, including tool reference and client connection examples.

---

## Troubleshooting

**KV reference not resolving (App Service shows "Failed")**
- Confirm the managed identity has `Key Vault Secrets User` on the vault: `az role assignment list --scope <kv-resource-id>`
- Confirm `AZURE_CLIENT_ID` app setting matches the MI's **client ID** (not object/principal ID)
- Re-run the Bicep `rbac` module, then restart the app

**Container pull failing (App Service can't pull from ACR)**
- Both `acrUseManagedIdentityCreds: true` AND `acrUserManagedIdentityID` must be set together — missing `acrUserManagedIdentityID` causes App Service to try the system-assigned MI (not enabled)
- Confirm the MI has `AcrPull` on the registry: `az role assignment list --scope <acr-resource-id>`

**`DefaultAzureCredential` picks wrong identity**
- `AZURE_CLIENT_ID` must be set to the user-assigned MI's **client ID** (not object/principal ID)
- Without it, the SDK falls through to system-assigned MI and fails

**Container startup timeout**
- `WEBSITE_CONTAINER_START_TIME_LIMIT: 230` allows 230s; if startup still times out, check logs for DB connection errors
- The B2 plan has `alwaysOn: true`, so cold starts only happen after a restart or deploy

**Database connection fails on startup**
- Confirm `PG_DATABASE_URL` app setting is set and the KV reference resolved (`az webapp config appsettings list`)
- Confirm the Timescale Cloud connection string includes `?sslmode=require`
- Timescale Cloud requires SSL — connections without it will be refused
- Check Timescale Cloud connection limits and allowed IP ranges if the app is blocked

**Bicep deploy fails with `.bicepparam` syntax error**
- Run `az bicep upgrade` to ensure Bicep CLI 0.18+
- The pipeline's DeployInfra stage runs `az bicep upgrade` automatically before deploying

**Policy assignment fails with authorization error**
- `Microsoft.Authorization/policyAssignments` requires `Resource Policy Contributor` on the resource group
- Run the `policy` module manually the first time with a higher-privileged identity, or grant the pipeline SP that role temporarily

**ETL job only syncs the last day (misses weeks of CVEs)**
- The KEV loaders ran before the full incremental and advanced the high-water mark — confirm the job runs `load_nvd_full.py --incremental` *first* (it does in `etl-job.bicep`)
- For a one-off catch-up, run the job's loader manually with `--since <last-full-sync-date>`; find that date from the per-day `last_modified` counts (see [Data Loading guide](data-loading.md))

**ETL job fails pulling the image or reading secrets**
- The job uses the *same* managed identity as the App Service — confirm `AcrPull` and `Key Vault Secrets User` role assignments exist (they're created by the `rbac` module at resource-group scope)
- Confirm `nvd-api-key`, `openai-api-key`, and `database-url` all exist in Key Vault (`az keyvault secret list --vault-name kv-chainlit-rag-dev -o table`)

**ETL job run terminated before completing**
- A large backfill may exceed `replicaTimeout` (default 7200s/2h) — raise it in `etl-job.bicep`, or split the catch-up into smaller `--since` windows run manually
