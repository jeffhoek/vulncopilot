"""Integration tests for check_and_increment against a real database.

Requires TEST_DATABASE_URL env var. Fails loudly if absent. Each test uses a
unique user_identifier so today's rows never collide across tests.
"""

import uuid

import pytest

from rag.usage import check_and_increment


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
