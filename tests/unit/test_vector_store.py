from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.vector_store import PgVectorStore


class _AcquireCtx:
    """Minimal async context manager that yields a mock connection."""

    def __init__(self, conn: AsyncMock) -> None:
        self._conn = conn

    async def __aenter__(self) -> AsyncMock:
        return self._conn

    async def __aexit__(self, *_) -> None:
        pass


@pytest.fixture
def mock_pool():
    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value = _AcquireCtx(conn)
    return pool, conn


async def test_search_returns_content_in_rank_order(mock_pool):
    pool, conn = mock_pool
    conn.fetch = AsyncMock(return_value=[{"content": "first"}, {"content": "second"}])
    result = await PgVectorStore(pool).search([0.1] * 1536)
    assert result == ["first", "second"]


async def test_search_returns_empty_list_when_no_rows(mock_pool):
    pool, conn = mock_pool
    conn.fetch = AsyncMock(return_value=[])
    result = await PgVectorStore(pool).search([0.1] * 1536)
    assert result == []


async def test_get_document_count_returns_integer(mock_pool):
    pool, conn = mock_pool
    conn.fetchval = AsyncMock(return_value=42)
    result = await PgVectorStore(pool).get_document_count()
    assert result == 42


async def test_search_passes_top_k_to_query(mock_pool):
    pool, conn = mock_pool
    conn.fetch = AsyncMock(return_value=[])
    await PgVectorStore(pool).search([0.1] * 1536, top_k=7)
    # Third positional arg to conn.fetch is the top_k limit value
    assert conn.fetch.call_args.args[2] == 7
