"""Unit tests for MCP tool error paths — no real DB required."""

from unittest.mock import AsyncMock, MagicMock

import asyncpg

import mcp_server.server as server_module
from mcp_server.server import McpContext
from rag.vector_store import PgVectorStore


class _AcquireCtx:
    def __init__(self, conn: AsyncMock) -> None:
        self._conn = conn

    async def __aenter__(self) -> AsyncMock:
        return self._conn

    async def __aexit__(self, *_) -> None:
        pass


def _mock_context_with_conn(conn: AsyncMock) -> McpContext:
    pool = MagicMock()
    pool.acquire.return_value = _AcquireCtx(conn)
    return McpContext(
        pool=pool,
        openai_client=AsyncMock(),
        vector_store=PgVectorStore(pool),
    )


async def test_query_before_set_mcp_context_returns_error(monkeypatch):
    monkeypatch.setattr(server_module, "_mcp_context", None)
    result = await server_module.query("SELECT 1")
    assert "not initialised" in result


async def test_retrieve_before_set_mcp_context_returns_error(monkeypatch):
    monkeypatch.setattr(server_module, "_mcp_context", None)
    result = await server_module.retrieve("anything")
    assert "not initialised" in result


async def test_query_postgres_error_returns_db_error_string(monkeypatch):
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=asyncpg.PostgresError("boom"))
    monkeypatch.setattr(server_module, "_mcp_context", _mock_context_with_conn(conn))
    result = await server_module.query("SELECT * FROM kev_vulnerabilities")
    assert result == "Error: Database error executing query."


async def test_query_unexpected_error_returns_internal_error_string(monkeypatch):
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=RuntimeError("unexpected"))
    monkeypatch.setattr(server_module, "_mcp_context", _mock_context_with_conn(conn))
    result = await server_module.query("SELECT * FROM kev_vulnerabilities")
    assert result == "Error: Internal error executing query."
