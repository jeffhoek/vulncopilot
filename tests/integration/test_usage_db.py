"""Integration tests for check_and_increment against a real database.

Requires TEST_DATABASE_URL env var. Fails loudly if absent. Each test uses a
unique user_identifier so today's rows never collide across tests.
"""

import uuid

import pytest

from rag.usage import check_and_increment, get_usage_stats


@pytest.fixture
def user_id() -> str:
    return f"github:test-{uuid.uuid4()}"


async def test_first_increment_creates_row_with_count_one(seeded_pool, user_id):
    allowed, count = await check_and_increment(seeded_pool, user_id, limit=20, input_tokens=10, output_tokens=5)

    assert allowed is True
    assert count == 1

    row = await seeded_pool.fetchrow(
        "SELECT query_count, input_tokens, output_tokens FROM user_usage "
        "WHERE user_identifier = $1 AND query_date = CURRENT_DATE",
        user_id,
    )
    assert row["query_count"] == 1
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 5


async def test_repeated_increments_accumulate_counts_and_tokens(seeded_pool, user_id):
    for _ in range(3):
        await check_and_increment(seeded_pool, user_id, limit=20, input_tokens=100, output_tokens=40)

    row = await seeded_pool.fetchrow(
        "SELECT query_count, input_tokens, output_tokens FROM user_usage "
        "WHERE user_identifier = $1 AND query_date = CURRENT_DATE",
        user_id,
    )
    assert row["query_count"] == 3
    assert row["input_tokens"] == 300
    assert row["output_tokens"] == 120


async def test_allowed_is_true_up_to_limit_then_false(seeded_pool, user_id):
    limit = 3
    results = [await check_and_increment(seeded_pool, user_id, limit, 1, 1) for _ in range(4)]

    # counts 1,2,3 are within the limit; the 4th crosses it.
    assert [allowed for allowed, _ in results] == [True, True, True, False]
    assert [count for _, count in results] == [1, 2, 3, 4]


async def test_separate_users_tracked_independently(seeded_pool):
    a = f"github:test-{uuid.uuid4()}"
    b = f"github:test-{uuid.uuid4()}"

    await check_and_increment(seeded_pool, a, limit=20, input_tokens=1, output_tokens=1)
    await check_and_increment(seeded_pool, a, limit=20, input_tokens=1, output_tokens=1)
    allowed_b, count_b = await check_and_increment(seeded_pool, b, limit=20, input_tokens=1, output_tokens=1)

    assert (allowed_b, count_b) == (True, 1)


# -- get_usage_stats --------------------------------------------------------


async def test_get_usage_stats_aggregates_counts_tokens_and_cost(seeded_pool, user_id):
    # 2 queries today, 1M input + 500K output tokens total.
    await check_and_increment(seeded_pool, user_id, limit=99, input_tokens=600_000, output_tokens=300_000)
    await check_and_increment(seeded_pool, user_id, limit=99, input_tokens=400_000, output_tokens=200_000)

    stats = await get_usage_stats(seeded_pool, input_cost_per_million=0.80, output_cost_per_million=4.00)
    row = next(r for r in stats if r["user_identifier"] == user_id)

    assert row["queries_today"] == 2
    assert row["queries_7d"] == 2
    assert row["queries_30d"] == 2
    assert row["input_tokens"] == 1_000_000
    assert row["output_tokens"] == 500_000
    # 1.0M input * $0.80 + 0.5M output * $4.00 = 0.80 + 2.00
    assert row["est_cost"] == pytest.approx(2.80)


async def test_get_usage_stats_excludes_users_with_no_rows(seeded_pool):
    absent = f"github:test-{uuid.uuid4()}"
    stats = await get_usage_stats(seeded_pool, input_cost_per_million=0.80, output_cost_per_million=4.00)

    assert all(r["user_identifier"] != absent for r in stats)
