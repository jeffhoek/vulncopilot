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


# Per-user aggregate for the /admin dashboard. Query counts are windowed
# (today / last 7 / last 30 days, each window inclusive of today); token totals
# are all-time so the estimated cost reflects everything the user has spent.
_STATS_SQL = """
SELECT
    user_identifier,
    COALESCE(SUM(query_count) FILTER (WHERE query_date = CURRENT_DATE), 0)                      AS queries_today,
    COALESCE(SUM(query_count) FILTER (WHERE query_date >= CURRENT_DATE - INTERVAL '6 days'), 0) AS queries_7d,
    COALESCE(SUM(query_count) FILTER (WHERE query_date >= CURRENT_DATE - INTERVAL '29 days'), 0) AS queries_30d,
    COALESCE(SUM(input_tokens), 0)  AS input_tokens,
    COALESCE(SUM(output_tokens), 0) AS output_tokens
FROM user_usage
GROUP BY user_identifier
ORDER BY queries_30d DESC, user_identifier
"""


async def get_usage_stats(
    pool: asyncpg.Pool,
    input_cost_per_million: float,
    output_cost_per_million: float,
) -> list[dict]:
    """Per-user usage aggregate for the admin dashboard.

    Returns one dict per user with windowed query counts (today / 7-day / 30-day),
    all-time token totals, and an estimated USD cost computed from the supplied
    per-million token prices (kept out of this module so ``Settings`` is the single
    source of truth).
    """
    rows = await pool.fetch(_STATS_SQL)
    stats: list[dict] = []
    for r in rows:
        input_tokens = r["input_tokens"]
        output_tokens = r["output_tokens"]
        est_cost = (
            input_tokens / 1_000_000 * input_cost_per_million + output_tokens / 1_000_000 * output_cost_per_million
        )
        stats.append(
            {
                "user_identifier": r["user_identifier"],
                "queries_today": r["queries_today"],
                "queries_7d": r["queries_7d"],
                "queries_30d": r["queries_30d"],
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "est_cost": est_cost,
            }
        )
    return stats
