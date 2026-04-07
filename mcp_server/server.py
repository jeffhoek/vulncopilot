import logging
import re
import secrets
from dataclasses import dataclass

import asyncpg
from fastmcp import FastMCP
from openai import AsyncOpenAI
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from config import settings
from rag.embeddings import generate_embedding
from rag.vector_store import PgVectorStore

logger = logging.getLogger(__name__)

MAX_QUERY_ROWS = 100

mcp = FastMCP("kev-nvd-rag")


@dataclass
class McpContext:
    pool: asyncpg.Pool
    openai_client: AsyncOpenAI
    vector_store: PgVectorStore


_mcp_context: McpContext | None = None


def set_mcp_context(pool: asyncpg.Pool, openai_client: AsyncOpenAI) -> None:
    """Called from app.py lifespan to inject the shared pool and client."""
    global _mcp_context
    _mcp_context = McpContext(
        pool=pool,
        openai_client=openai_client,
        vector_store=PgVectorStore(pool),
    )


@mcp.tool
async def retrieve(query: str) -> str:
    """Retrieve relevant context from the KEV/NVD knowledge base using semantic search.

    Args:
        query: Natural language search query (e.g. "log4j remote code execution").

    Returns:
        Relevant document excerpts from the knowledge base.
    """
    if _mcp_context is None:
        return "Error: MCP context not initialised."

    query_embedding = await generate_embedding(_mcp_context.openai_client, query)
    results = await _mcp_context.vector_store.search(query_embedding, top_k=settings.top_k)

    if not results:
        return "No relevant context found."

    context = "\n\n---\n\n".join(results)
    return f"Retrieved context:\n\n{context}"


@mcp.tool
async def query(sql: str) -> str:
    """Execute a read-only SQL SELECT query against the KEV/NVD database.

    Args:
        sql: A SELECT statement against kev_vulnerabilities or nvd_vulnerabilities.

    Returns:
        Query results as a formatted table, or an error message.
    """
    if _mcp_context is None:
        return "Error: MCP context not initialised."

    if not sql.strip().upper().startswith("SELECT"):
        return "Error: Only SELECT statements are permitted."

    limit_match = re.search(r"\bLIMIT\s+(\d+)\b", sql, re.IGNORECASE)
    if limit_match:
        if int(limit_match.group(1)) > MAX_QUERY_ROWS:
            sql = sql[: limit_match.start(1)] + str(MAX_QUERY_ROWS) + sql[limit_match.end(1) :]
    else:
        sql = sql.rstrip().rstrip(";") + f" LIMIT {MAX_QUERY_ROWS}"

    try:
        async with _mcp_context.pool.acquire() as conn:
            rows = await conn.fetch(sql)
    except asyncpg.PostgresError as e:
        return f"Query error: {e}"
    except Exception:
        logger.exception("Unexpected error in MCP query tool")
        return "Internal error executing query."

    if not rows:
        return "No results found."

    headers = list(rows[0].keys())
    lines = [" | ".join(headers)]
    lines.append("-" * len(lines[0]))
    for row in rows:
        lines.append(" | ".join(str(v) for v in row.values()))
    lines.append(f"\n{len(rows)} row(s) returned.")
    return "\n".join(lines)


class McpRouterMiddleware:
    """
    Starlette middleware that intercepts /mcp* requests before Chainlit's router,
    enforces API key auth, and delegates to FastMCP's ASGI app.
    All other requests pass through untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        if settings.mcp_api_key is None:
            logger.warning(
                "MCP_API_KEY is not set — /mcp endpoint is UNAUTHENTICATED. "
                "Set MCP_API_KEY in .env or Key Vault before deploying."
            )
        # stateless_http=True avoids needing to manage FastMCP's session lifespan.
        self._mcp_asgi: ASGIApp = mcp.http_app(transport="streamable-http", stateless_http=True)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if not (path == "/mcp" or path.startswith("/mcp/")):
            await self.app(scope, receive, send)
            return

        # Auth check
        if settings.mcp_api_key is not None:
            headers = dict(scope.get("headers", []))
            api_key = headers.get(b"x-api-key", b"").decode()
            if not secrets.compare_digest(api_key, settings.mcp_api_key):
                response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return

        await self._mcp_asgi(scope, receive, send)
