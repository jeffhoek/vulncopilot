# MCP Server — Operational Guide

The MCP server exposes the two RAG tools (`retrieve` and `query`) over the
[Model Context Protocol](https://modelcontextprotocol.io/) so external agents
can query the KEV/NVD vulnerability data without a browser or separate
database connection.

The server is co-hosted with the Chainlit app on the same Azure App Service
container. All requests go to the same HTTPS endpoint — no new Azure resources
or connection pools are involved.

---

## Endpoint

```
https://app-chainlit-rag-dev.azurewebsites.net/mcp
```

Transport: **Streamable HTTP** (MCP spec 2025-03-26+).
All requests must be `POST` with `Content-Type: application/json` and
`Accept: application/json, text/event-stream`.

---

## Authentication

All requests require an `X-API-Key` header. The key is stored in Azure Key
Vault as `mcp-api-key` and injected into the App Service at runtime.

Requests without the header, or with an incorrect key, receive `HTTP 401`.

---

## Tool Reference

### `retrieve`

Semantic search across the KEV and NVD knowledge base using pgvector embeddings.

| Parameter | Type | Description |
|---|---|---|
| `query` | `string` | Natural language search query |

Returns: plain-text excerpts from matching documents, separated by `---`.

**Example:** `query="log4j remote code execution"`

---

### `query`

Execute a read-only SQL SELECT against the vulnerability database.

| Parameter | Type | Description |
|---|---|---|
| `sql` | `string` | A `SELECT` statement |

**Constraints:**
- Only `SELECT` statements are permitted. Any other statement returns a
  tool-level error: `Error: Only SELECT statements are permitted.`
- Results are capped at **100 rows**. A `LIMIT` clause above 100 is silently
  lowered; queries without `LIMIT` have one appended automatically.

Returns: results as a pipe-delimited table, or an error message string.

**Schema summary** (full schema in `config.py:system_prompt`):

```sql
kev_vulnerabilities (
  cve_id, vendor_project, product, vulnerability_name,
  short_description, required_action, notes,
  date_added, due_date, known_ransomware_campaign_use, cwes
)

nvd_vulnerabilities (
  cve_id, description, cvss_v31_score, cvss_v31_severity,
  cvss_v31_vector, cvss_v2_score, cvss_v2_severity,
  cwes, affected_products, reference_urls,
  published, last_modified, raw_json
)
```

JOIN both tables on `cve_id`.

**Example:** `sql="SELECT cve_id, vendor_project FROM kev_vulnerabilities LIMIT 5"`

---

## Connect from Claude Desktop

Claude Desktop only supports stdio servers natively, so a bridge is needed
for HTTP MCP servers. Use `mcp-remote` (no separate install required — `npx`
fetches it on first run).

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(Windows: `%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "kev-nvd-rag": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://app-chainlit-rag-dev.azurewebsites.net/mcp",
        "--header",
        "X-API-Key:YOUR_MCP_API_KEY"
      ]
    }
  }
}
```

Note the `:` (no space) between the header name and value — this is required
by `mcp-remote`'s argument parser.

Restart Claude Desktop. The `retrieve` and `query` tools will appear in the
tool picker.

---

## Connect from a Pydantic AI Agent

```python
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerHTTP

mcp_server = MCPServerHTTP(
    url="https://app-chainlit-rag-dev.azurewebsites.net/mcp",
    headers={"X-API-Key": "YOUR_MCP_API_KEY"},
)

agent = Agent(
    "anthropic:claude-haiku-4-5-20251001",
    mcp_servers=[mcp_server],
)

async with agent.run_mcp_servers():
    result = await agent.run("Which KEV vulnerabilities were added in 2024?")
    print(result.output)
```

---

## Key Management

**Generate a new key:**

```bash
openssl rand -hex 32
```

**Store in Key Vault (requires `az login` + Key Vault Secrets Officer role):**

```bash
az keyvault secret set \
  --vault-name kv-chainlit-rag-dev \
  --name mcp-api-key \
  --value "YOUR_NEW_KEY"
```

**Rotate:** Set a new value with the command above, then restart the App
Service. The old key is immediately invalid once the process restarts and
picks up the new Key Vault reference.

**Local development:** Add `MCP_API_KEY=your-key` to `.env`. If the variable
is absent, the server starts with a warning and the `/mcp` route operates
without auth enforcement (never deploy this way).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `HTTP 401` | Missing or wrong `X-API-Key` | Verify the key matches what is in Key Vault |
| `HTTP 405 Method Not Allowed` | Sending `GET` instead of `POST` | Use `POST` with a JSON-RPC body and correct `Accept` header |
| `not valid MCP server configurations` in Claude Desktop | Used `"type": "http"` directly — Desktop only supports stdio | Use `mcp-remote` via `npx` as shown above |
| `Not Acceptable` error in JSON-RPC response | Missing `Accept` header | Add `Accept: application/json, text/event-stream` |
| Tool returns `"Error: MCP context not initialised."` | App started but lifespan did not complete | Check App Service logs for DB connection errors at startup |
| Tool returns `"Error: Only SELECT statements are permitted."` | Non-SELECT SQL passed to `query` | Use only `SELECT` statements |
| Key Vault reference shows `Failed` in app settings | Secret name mismatch or MI lacks access | Verify secret name is `mcp-api-key` and managed identity has `Key Vault Secrets User` role |
| Connection timeout after initial connect | Azure App Service idle timeout on long-running requests | Streamable HTTP is stateless per-request and should not be affected; check for slow queries |
