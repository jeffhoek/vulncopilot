"""Pytest fixtures for the offline eval harness.

Mirrors tests/conftest.py:52 (db_pool/seeded_pool) but reads the eval DB
from EVAL_DATABASE_URL and loads evals/fixtures/eval_db_seed.sql instead
of inline INSERTs.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

# Resolve API keys for local runs: prefer the existing env var, fall back
# to .env, then a placeholder so plain `pytest --collect-only` succeeds.
# `dotenv_values` reads .env without touching os.environ, which lets us
# fill in *empty* env vars (override=False alone won't replace empties).
from dotenv import dotenv_values  # noqa: E402

_dotenv = dotenv_values(".env")
for _k, _placeholder in [
    ("OPENAI_API_KEY", "sk-test-placeholder"),
    ("ANTHROPIC_API_KEY", "sk-ant-test-placeholder"),
]:
    if not os.environ.get(_k):
        os.environ[_k] = _dotenv.get(_k) or _placeholder

import asyncpg
import pytest
import yaml
from openai import AsyncOpenAI
from pgvector.asyncpg import register_vector

from config import settings
from evals.harness import EvalResult, run_query
from evals.scoring import GoldenEntry, score_all
from rag.agent import Deps
from rag.database import SCHEMA_SQL
from rag.vector_store import PgVectorStore

EVAL_DIR = Path(__file__).parent
SEED_SQL_PATH = EVAL_DIR / "fixtures" / "eval_db_seed.sql"
DATASET_PATH = EVAL_DIR / "dataset.yaml"
RESULTS_PATH = EVAL_DIR / "results.json"


async def _init_connection(conn: asyncpg.Connection) -> None:
    await register_vector(conn)


def load_dataset() -> list[GoldenEntry]:
    raw = yaml.safe_load(DATASET_PATH.read_text())
    return [
        GoldenEntry(
            id=entry["id"],
            query=entry["query"],
            ground_truth=entry.get("ground_truth", ""),
            intent=entry.get("intent", ""),
            expected_tool=entry.get("expected_tool", ""),
            expected_cve_ids=entry.get("expected_cve_ids"),
            notes=entry.get("notes", ""),
        )
        for entry in raw
    ]


@pytest.fixture(scope="session")
async def eval_pool() -> AsyncIterator[asyncpg.Pool]:
    dsn = os.environ.get("EVAL_DATABASE_URL")
    if not dsn:
        pytest.skip("EVAL_DATABASE_URL is not set; skipping eval suite")
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5, init=_init_connection)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture(scope="session")
async def eval_seeded_pool(eval_pool: asyncpg.Pool) -> asyncpg.Pool:
    if not SEED_SQL_PATH.exists():
        pytest.skip(f"{SEED_SQL_PATH} not found; run `uv run python -m evals.fixtures.build_seed` first")
    seed_sql = SEED_SQL_PATH.read_text()
    async with eval_pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
        await conn.execute(seed_sql)
    return eval_pool


@pytest.fixture(scope="session")
async def eval_deps(eval_seeded_pool: asyncpg.Pool) -> Deps:
    return Deps(
        openai_client=AsyncOpenAI(api_key=settings.openai_api_key),
        vector_store=PgVectorStore(eval_seeded_pool),
    )


def _write_results(
    rows: list[tuple[GoldenEntry, EvalResult]],
    scores: dict[str, dict[str, float]],
) -> None:
    payload = {
        entry.id: {
            "query": entry.query,
            "answer": result.answer,
            "tools_used": result.tools_used,
            "context_count": len(result.contexts),
            "scores": scores.get(entry.id, {}),
        }
        for entry, result in rows
    }
    RESULTS_PATH.write_text(json.dumps(payload, indent=2, default=str))


@pytest.fixture(scope="session")
async def all_scores(eval_deps: Deps) -> dict[str, dict[str, float]]:
    """Run every dataset entry through the agent, score in one batch, write results.json.

    The agent is invoked once per dataset row across the whole pytest session,
    then Ragas scores them in a single batch. results.json is written even if
    scoring fails partway, so a failed run still surfaces answers + tools_used.
    """
    entries = load_dataset()
    rows: list[tuple[GoldenEntry, EvalResult]] = []
    scores: dict[str, dict[str, float]] = {}
    try:
        for entry in entries:
            result = await run_query(entry.query, eval_deps)
            rows.append((entry, result))
        scores = score_all(rows)
    finally:
        _write_results(rows, scores)
    return scores
