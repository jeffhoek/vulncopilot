# MCP Server — Option A: Same Service, Azure App Service

Add a Model Context Protocol (MCP) server endpoint (`/mcp`) to the existing
Chainlit application. The MCP server exposes the two existing RAG tools —
`retrieve` (semantic search) and `query` (SQL) — so external agents can query
the KEV/NVD data without a separate deployment or database connection.

## Architecture

```
Azure App Service (existing container)
├── / → Chainlit WebSocket UI           (unchanged)
├── /healthz → health check             (unchanged)
└── /mcp → FastMCP Streamable HTTP      (new)
      ├── tool: retrieve                (mirrors rag/agent.py)
      └── tool: query                   (mirrors rag/agent.py)
```

All traffic uses the same HTTPS endpoint, same managed identity, same Key Vault
references, and the same asyncpg connection pool. No new Azure resources are
required.

### Transport choice: Streamable HTTP over SSE

MCP supports two HTTP transports. Streamable HTTP is preferred here:

| | SSE | Streamable HTTP |
|---|---|---|
| Azure App Service idle timeout | Can drop long-lived connections | Stateless per-request, no timeout risk |
| Implementation | `mcp.sse_app()` | `mcp.streamable_http_app()` |
| Client compatibility | Older MCP clients | MCP spec 2025-03-26+ (Claude Desktop, etc.) |

### Authentication

The `/mcp` route requires an `X-API-Key` header. The key is stored in Key Vault
as `mcp-api-key` and injected via the same Key Vault reference pattern used by
all other secrets.

---

## Manual Steps Overview

Two steps in this plan require human action with authenticated credentials.
Both are flagged inline with `> MANUAL STEP` callouts.

| Step | Auth required | When |
|---|---|---|
| 0.2 — Set Key Vault secret | `az login` + Key Vault Secrets Officer role on the vault | Before development begins (can be done now) |
| 7.1 — Push to `main` | Git push access to the repository | After all code and tests pass |

All other steps are code edits, local commands, or verification checks that
run under the developer's own shell session without additional authentication.

---

## Work Plan

### Phase 0 — Upfront Manual Steps (do these before writing any code)

These steps are sequenced first because they require human authentication and
can block later phases if left until deploy time.

**0.1** Generate the MCP API key locally. No authentication required.

```bash
MCP_API_KEY=$(openssl rand -hex 32)
echo $MCP_API_KEY
```

Store the output in a password manager immediately. This value is used in
steps 0.2 and 0.3 and is the credential external agents will present.

**0.2** Add `MCP_API_KEY` to `.env` for local development. No authentication
required — this is a local file edit.

```bash
echo "MCP_API_KEY=$MCP_API_KEY" >> .env
```

`.env` is already in `.gitignore` and will not be committed.

---

> **MANUAL STEP — Azure CLI authentication required**
>
> **0.3** Set the secret in Key Vault.
>
> **Prerequisites:**
> - `az login` completed and pointing at the correct subscription
> - Your identity has the `Key Vault Secrets Officer` role on
>   `kv-chainlit-rag-dev` (see Step 4.0 in `docs/deploy-azure-app-service.md`
>   for how to grant yourself this role if needed)
>
> ```bash
> az keyvault secret set \
>   --vault-name kv-chainlit-rag-dev \
>   --name mcp-api-key \
>   --value "$MCP_API_KEY"
> ```
>
> This can be run now, before any code is written. The App Service will not
> attempt to resolve this secret until the Bicep app setting is added in
> Phase 6 and the pipeline runs.

---

### Phase 1 — Python: Dependency

**1.1** Add `fastmcp` to project dependencies.

```bash
uv add fastmcp
```

Verify `fastmcp` appears in `pyproject.toml` under `[project.dependencies]`.

---

### Phase 2 — Python: Configuration

**2.1** Add `mcp_api_key` field to `config.py` `Settings` class.

- Field type: `str | None = None`
- Sourced from env var `MCP_API_KEY`
- No default — absence causes a startup warning and skips auth enforcement
  (permits local dev without a key set)

---

### Phase 3 — Python: MCP Server Module

> **Note:** The package is named `mcp_server/` (not `mcp/`) to avoid shadowing
> the `mcp` SDK package that fastmcp imports internally.

**3.1** Create `mcp_server/server.py`.

- Instantiate a `FastMCP` app named `"kev-nvd-rag"`
- Define tool `retrieve(query: str) -> str` — calls `generate_embedding` then
  `vector_store.search`, identical logic to `rag/agent.py:retrieve`
- Define tool `query(sql: str) -> str` — SELECT-only guard, 100-row cap,
  identical logic to `rag/agent.py:query`
- Both tools read from a module-level `McpContext` dataclass (pool +
  openai_client + vector_store). Context is injected via `set_mcp_context()`
  called from the `app.py` lifespan (no new connection pool).

**3.2** Create `mcp_server/__init__.py` (empty, marks package).

**3.3** Write `X-API-Key` authentication middleware in `mcp_server/server.py`.

- `BaseHTTPMiddleware` subclass that reads `X-API-Key` from request headers
- Compares to `settings.mcp_api_key` using `secrets.compare_digest`
- Returns HTTP 401 if key is absent or incorrect
- If `settings.mcp_api_key` is `None`, logs a startup warning and skips
  auth enforcement (local dev only — never `None` in Azure via KV reference)

---

### Phase 4 — Python: Mount into Chainlit

**4.1** In `app.py`, after Chainlit initialises its FastAPI app, call
`set_mcp_context()` from the lifespan to inject the shared pool/client, then
mount the MCP ASGI app at `/mcp`.

```python
from chainlit.server import app as fastapi_app
from mcp_server.server import build_mcp_asgi_app, set_mcp_context

# Inside the lifespan, after pool and openai_client are created:
set_mcp_context(pool, openai_client)

fastapi_app.mount("/mcp", build_mcp_asgi_app())
```

`build_mcp_asgi_app()` returns the FastMCP Streamable HTTP ASGI app
(`mcp.http_app(transport="streamable-http")`) wrapped with `ApiKeyMiddleware`.

**4.2** Confirm the existing `/healthz` route is unaffected (no route collision).

---

### Phase 5 — Local Testing

**5.1** Start the app locally and confirm the auth middleware responds correctly.

```bash
uv run chainlit run app.py
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/mcp
# Expected: 401
```

**5.2** Run the MCP Inspector against the local server to confirm both tools
are discoverable, return correct results, and that disallowed SQL returns a
tool-level error (not an HTTP 500).

```bash
npx @modelcontextprotocol/inspector
# In the UI: Transport Type = Streamable HTTP (Direct connection via Proxy)
# URL: http://localhost:8000/mcp
# Authentication > Custom Headers: X-API-Key = $MCP_API_KEY
# Click Connect
```

In the **Tools** tab:
- Tool list should show: `retrieve`, `query`
- Test `retrieve`: `query="log4j remote code execution"`
- Test `query`: `sql="SELECT cve_id, vendor_project FROM kev_vulnerabilities LIMIT 5"`
- Test `query` with disallowed statement: `sql="DROP TABLE kev_vulnerabilities"`
  — result must contain `"Error: Only SELECT statements are permitted."` (tool-level
  error in the result body, not an HTTP 500)

---

### Phase 6 — Azure Infrastructure: Bicep App Setting

**6.1** Add the Key Vault reference app setting to
`infra/modules/app-service.bicep` in the `appSettings` array.

```bicep
{
  name: 'MCP_API_KEY'
  value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=mcp-api-key)'
}
```

No other Bicep changes are needed. No new resources, no new role assignments —
the managed identity already has `Key Vault Secrets User` on the vault.

**6.2** Update `docs/deploy-azure-app-service.md` to document the new secret.

- Add the `az keyvault secret set --name mcp-api-key` command alongside the
  existing secrets in Step 4.1
- Note that this secret must exist before the pipeline runs, or the App Service
  will start with a warning and the `/mcp` route will reject all requests

---

### Phase 7 — Deploy

---

> **MANUAL STEP — Git push access required**
>
> **7.1** Push to `main` to trigger the Azure Pipeline.
>
> The existing three-stage pipeline (Build → DeployInfra → DeployApp) handles
> everything: image build, Bicep incremental deploy (picks up the new
> `MCP_API_KEY` app setting from 6.1), and App Service restart.
>
> No pipeline YAML changes are needed.

---

**7.2** Verify the new app setting resolved in Key Vault after the pipeline
completes. Requires `az login`.

```bash
az webapp config appsettings list \
  --name app-chainlit-rag-dev \
  --resource-group rg-chainlit-rag-dev \
  --query "[?name=='MCP_API_KEY']" -o table
# Value column must show "@Microsoft.KeyVault(...)" with status: Resolved
# "Failed" here means the secret name in KV does not match, or MI lacks access
```

**7.3** Smoke-test the live endpoint. No Azure auth required — uses only the
MCP API key from step 0.1.

```bash
# Confirm 401 without key (GET is sufficient to trigger auth check)
curl -s -o /dev/null -w "%{http_code}" \
  https://app-chainlit-rag-dev.azurewebsites.net/mcp
# Expected: 401
```
```bash
# Confirm MCP server responds with a valid initialize result
# Note: Streamable HTTP requires POST, JSON-RPC body, and both Accept types
curl -s -X POST \
  -H "X-API-Key: $MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.0.1"}}}' \
  https://app-chainlit-rag-dev.azurewebsites.net/mcp
```
Expected:
```bash
event: message
data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26","capabilities":{"experimental":{},"prompts":{"listChanged":true},"resources":{"subscribe":false,"listChanged":true},"tools":{"listChanged":true},"extensions":{"io.modelcontextprotocol/ui":{}}},"serverInfo":{"name":"kev-nvd-rag","version":"3.1.0"}}}
```

---

### Phase 8 — Documentation

**8.1** Create `docs/mcp-server.md` — operational guide covering:

- What the MCP server exposes and why
- Tool reference: `retrieve` and `query` (parameters, return format, limits)
- How to connect from Claude Desktop (`claude_desktop_config.json` snippet)
- How to connect from a Pydantic AI agent (MCP client config snippet)
- Authentication: how to generate a key, where it is stored, how to rotate it
- Troubleshooting: 401 causes, tool errors, connection timeouts

**8.2** Add `[MCP server](docs/mcp-server.md)` to the Docs list in `CLAUDE.md`.

---

## Constraints and Non-Goals

- **No new Azure resources.** Same App Service, same ACR image, same Key Vault.
- **No second DB connection pool.** The MCP server reuses the pool from the
  Chainlit app lifespan.
- **No changes to the Chainlit UI.** The chat interface is unaffected.
- **No changes to the pipeline YAML.** The existing three-stage pipeline
  (Build → DeployInfra → DeployApp) handles everything.
- **No A2A protocol.** Out of scope for this plan.

---

## File Change Summary

| File | Change |
|---|---|
| `pyproject.toml` | Add `fastmcp` dependency |
| `.env` | Add `MCP_API_KEY` (local dev, not committed) |
| `config.py` | Add `mcp_api_key: str \| None = None` |
| `mcp_server/__init__.py` | New (empty package marker) |
| `mcp_server/server.py` | New — FastMCP app, tools, auth middleware, ASGI factory |
| `app.py` | Mount `/mcp` into Chainlit's FastAPI app |
| `infra/modules/app-service.bicep` | Add `MCP_API_KEY` KV reference app setting |
| `docs/deploy-azure-app-service.md` | Add `mcp-api-key` secret to Step 4.1 |
| `docs/mcp-server.md` | New — operational guide |
| `CLAUDE.md` | Add link to `docs/mcp-server.md` |
