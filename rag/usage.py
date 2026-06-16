"""Per-user daily usage tracking for rate limiting.

Backed by the ``user_usage`` table (see ``rag.database.SCHEMA_SQL``). All
functions take the existing asyncpg pool from ``rag.database.get_pool()``.
"""

import asyncpg

# Single atomic statement: insert today's row or bump the existing one. The
# UNIQUE (user_identifier, query_date) constraint makes ON CONFLICT fire, so two
# concurrent requests can't both pass a separate check before either increments.
_INCREMENT_SQL = """
INSERT INTO user_usage (user_identifier, query_date, query_count, input_tokens, output_tokens)
VALUES ($1, CURRENT_DATE, 1, $2, $3)
ON CONFLICT (user_identifier, query_date) DO UPDATE SET
    query_count   = user_usage.query_count   + 1,
    input_tokens  = user_usage.input_tokens  + EXCLUDED.input_tokens,
    output_tokens = user_usage.output_tokens + EXCLUDED.output_tokens
RETURNING query_count
"""


async def check_and_increment(
    pool: asyncpg.Pool,
    user_id: str,
    limit: int,
    input_tokens: int,
    output_tokens: int,
) -> tuple[bool, int]:
    """Atomically record one query's usage and report whether it was within limit.

    Returns ``(allowed, new_count)`` where ``allowed`` is ``new_count <= limit``.
    This is the authoritative gate; callers may run a cheap pre-check first to
    avoid spending an LLM call on an already-blocked user, but this upsert is what
    actually counts.
    """
    new_count = await pool.fetchval(_INCREMENT_SQL, user_id, input_tokens, output_tokens)
    return new_count <= limit, new_count
