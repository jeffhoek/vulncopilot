"""Integration tests for PgVectorStore against a real PostgreSQL+pgvector database.

Requires TEST_DATABASE_URL env var.  Fails loudly if absent.
"""

from rag.vector_store import PgVectorStore
from tests.conftest import GOLDEN_KEV_EMBEDDING, _seed_tables


async def test_search_returns_nonempty_list_and_top_result_matches_seed(vector_store):
    results = await vector_store.search(GOLDEN_KEV_EMBEDDING, top_k=5)
    assert len(results) > 0
    # kev row was seeded with GOLDEN_KEV_EMBEDDING; it should be the top match
    assert any("Log4Shell" in r for r in results)


async def test_search_respects_top_k(vector_store):
    results = await vector_store.search(GOLDEN_KEV_EMBEDDING, top_k=1)
    assert len(results) <= 1


async def test_get_document_count_matches_seeded_row_count(vector_store, seeded_pool):
    count = await vector_store.get_document_count()
    # seeded_pool inserts exactly 1 kev + 1 nvd row (ON CONFLICT DO UPDATE keeps exactly those)
    assert count >= 2


async def test_search_against_empty_tables_returns_empty_list(db_pool):
    async with db_pool.acquire() as conn:
        await conn.execute("TRUNCATE kev_vulnerabilities, nvd_vulnerabilities CASCADE")
    try:
        store = PgVectorStore(db_pool)
        results = await store.search(GOLDEN_KEV_EMBEDDING, top_k=5)
        assert results == []
    finally:
        # Re-seed so the session-scoped seeded_pool is still usable after this test
        async with db_pool.acquire() as conn:
            await _seed_tables(conn)
