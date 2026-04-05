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

## Work Plan

### Phase 1 — Python: Dependency

**1.1** Add `fastmcp` to project dependencies.

```
uv add fastmcp
```

Verify `fastmcp` appears in `pyproject.toml` under `[project.dependencies]`.

---

### Phase 2 — Python: Configuration

**2.1** Add `mcp_api_key` field to `config.py` `Settings` class.

- Field type: `str | None = None`
- Sourced from env var `MCP_API_KEY`
- No default — absence disables the MCP route at startup

**2.2** Add `MCP_API_KEY=<local-dev-key>` to `.env` (not committed).

---

### Phase 3 — Python: MCP Server Module

**3.1** Create `mcp/server.py`.

- Instantiate a `FastMCP` app named `"kev-nvd-rag"`
- Define tool `retrieve(query: str) -> str` — calls `generate_embedding` then
  `vector_store.search`, identical logic to `rag/agent.py:retrieve`
- Define tool `query(sql: str) -> str` — SELECT-only guard, 100-row cap,
  identical logic to `rag/agent.py:query`
- Both tools accept the shared asyncpg pool and OpenAI client via lifespan
  context (no new connection pool; reuse the one initialised in `app.py`)

**3.2** Create `mcp/__init__.py` (empty, marks package).

**3.3** Write `X-API-Key` authentication middleware in `mcp/server.py`.

- ASGI middleware that reads `X-API-Key` from request headers
- Compares to `settings.mcp_api_key` using `secrets.compare_digest`
- Returns HTTP 401 if key is absent or incorrect
- Skips auth if `settings.mcp_api_key` is `None` and logs a startup warning
  (allows local dev without a key)

---

### Phase 4 — Python: Mount into Chainlit

**4.1** In `app.py`, after Chainlit initialises its FastAPI app, mount the MCP
ASGI app at `/mcp`.

```python
# Pseudocode — exact API depends on FastMCP version
from chainlit.server import app as fastapi_app
from mcp.server import build_mcp_asgi_app

fastapi_app.mount("/mcp", build_mcp_asgi_app())
```

`build_mcp_asgi_app()` wraps the FastMCP Streamable HTTP transport with the
auth middleware from 3.3 and injects the shared pool/client from app lifespan.

**4.2** Confirm the existing `/healthz` route is unaffected (no route collision).

---

### Phase 5 — Local Testing

**5.1** Start the app locally and confirm `/mcp` responds.

```bash
uv run chainlit run app.py
curl -s http://localhost:8000/mcp  # expect 401 without key
curl -s -H "X-API-Key: <dev-key>" http://localhost:8000/mcp  # expect MCP response
```

**5.2** Run the MCP Inspector against the local server to confirm both tools
are discoverable and return correct results.

```bash
npx @modelcontextprotocol/inspector http://localhost:8000/mcp
# Tools list should show: retrieve, query
# Test retrieve: query="log4j remote code execution"
# Test query: sql="SELECT cve_id, vendor_project FROM kev_vulnerabilities LIMIT 5"
```

**5.3** Confirm a disallowed SQL statement returns an error (not a 500).

```bash
curl -X POST -H "X-API-Key: <dev-key>" \
  -H "Content-Type: application/json" \
  -d '{"tool":"query","arguments":{"sql":"DROP TABLE kev_vulnerabilities"}}' \
  http://localhost:8000/mcp
# Expected: tool result containing "Error: Only SELECT statements are permitted."
```

---

### Phase 6 — Azure Infrastructure: Key Vault Secret

**6.1** Add provisioning instructions for the new secret to
`docs/deploy-azure-app-service.md` (Step 4.1 — alongside existing secrets).

```bash
az keyvault secret set \
  --vault-name kv-chainlit-rag-dev \
  --name mcp-api-key \
  --value "$MCP_API_KEY"
```

This is a manual, one-time step — same pattern as all other secrets. Generate
a random key (minimum 32 bytes, base64 or hex encoded).

**6.2** Add the Key Vault reference app setting to
`infra/modules/app-service.bicep` in the `appSettings` array.

```bicep
{
  name: 'MCP_API_KEY'
  value: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=mcp-api-key)'
}
```

No other Bicep changes are needed (no new resources, no new role assignments —
the managed identity already has `Key Vault Secrets User`).

---

### Phase 7 — Deploy

**7.1** Set the Key Vault secret in the target environment (run once, before or
after deploy — the app handles a missing key gracefully with a startup warning).

```bash
MCP_API_KEY=$(openssl rand -hex 32)
az keyvault secret set \
  --vault-name kv-chainlit-rag-dev \
  --name mcp-api-key \
  --value "$MCP_API_KEY"
```

Store the generated key in a password manager — this is the credential external
agents will use.

**7.2** Push to `main`. The existing Azure Pipelines run handles build, Bicep
deploy (incremental — picks up the new app setting), and container restart.

No pipeline YAML changes are needed.

**7.3** Verify the new app setting resolved in Key Vault after deploy.

```bash
az webapp config appsettings list \
  --name app-chainlit-rag-dev \
  --resource-group rg-chainlit-rag-dev \
  --query "[?name=='MCP_API_KEY']" -o table
# Value column should show "@Microsoft.KeyVault(...)", status: Resolved
```

**7.4** Smoke-test the live endpoint.

```bash
# 401 without key
curl -s -o /dev/null -w "%{http_code}" \
  https://app-chainlit-rag-dev.azurewebsites.net/mcp

# Tool list with key
curl -s -H "X-API-Key: <mcp-api-key>" \
  https://app-chainlit-rag-dev.azurewebsites.net/mcp
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

**8.2** Update `docs/deploy-azure-app-service.md`.

- Add Step 4.1 for setting `mcp-api-key` in Key Vault (within the existing
  Step 4 secrets provisioning section)
- Add a verification step in the Verification section:
  `curl -H "X-API-Key: ..." https://<app>.azurewebsites.net/mcp`

**8.3** Add `[MCP server](docs/mcp-server.md)` to the Docs list in `CLAUDE.md`.

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
| `mcp/__init__.py` | New (empty package marker) |
| `mcp/server.py` | New — FastMCP app, tools, auth middleware, ASGI factory |
| `app.py` | Mount `/mcp` into Chainlit's FastAPI app |
| `infra/modules/app-service.bicep` | Add `MCP_API_KEY` KV reference app setting |
| `docs/deploy-azure-app-service.md` | Add step 4.1 (secret) and verification step |
| `docs/mcp-server.md` | New — operational guide |
| `CLAUDE.md` | Add link to `docs/mcp-server.md` |
