"""Pytest fixtures for the offline eval harness.

Reads the eval DB from EVAL_DATABASE_URL (mirrors tests/conftest.py:55's
TEST_DATABASE_URL convention) and seeds it from
evals/fixtures/eval_db_seed.jsonl. ANTHROPIC_API_KEY and OPENAI_API_KEY
must be exported in the environment before running pytest — source your
.env or pass them inline.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import numpy as np
import pytest
import yaml
from pgvector.asyncpg import register_vector

# `rag.agent` and `evals.harness`/`evals.scoring` (which import it) are
# loaded lazily inside fixture bodies so `pytest --collect-only` works
# without ANTHROPIC_API_KEY — pydantic-ai constructs the Anthropic provider
# at import time and refuses to load without a key.
from config import settings
from evals.scoring import GoldenEntry, score_all  # gated via TYPE_CHECKING on rag.agent
from rag.database import SCHEMA_SQL  # no agent dep
from rag.vector_store import PgVectorStore  # no agent dep

if False:  # keep type checkers happy without paying the import cost at collect-time
    from evals.harness import EvalResult

EVAL_DIR = Path(__file__).parent
SEED_PATH = EVAL_DIR / "fixtures" / "eval_db_seed.jsonl"
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


# Tables and columns we'll allow into the INSERT statement. Anything outside
# these allowlists is rejected before string interpolation, which neutralizes
# the S608 SQL-injection concern (the JSONL is locally generated, but the
# allowlist makes that guarantee explicit and audit-able).
_ALLOWED_TABLES = {"kev_vulnerabilities", "nvd_vulnerabilities", "cwe_definitions"}
_ALLOWED_COLUMNS = {
    "id",
    "cve_id",
    "vendor_project",
    "product",
    "vulnerability_name",
    "short_description",
    "required_action",
    "notes",
    "date_added",
    "due_date",
    "known_ransomware_campaign_use",
    "cwes",
    "content",
    "embedding",
    "description",
    "cvss_v31_score",
    "cvss_v31_severity",
    "cvss_v31_vector",
    "cvss_v2_score",
    "cvss_v2_severity",
    "affected_products",
    "reference_urls",
    "published",
    "last_modified",
    "raw_json",
    "cwe_id",
    "name",
    "abstraction",
    "url",
}
# Columns excluded from INSERTs (auto-assigned by Postgres).
_AUTO_COLUMNS = {"id"}
# Columns that need conversion from JSON-friendly types back to native types.
_VECTOR_COLUMNS = {"embedding"}
_JSONB_COLUMNS = {"raw_json"}


def _coerce(col: str, value):
    if value is None:
        return None
    if col in _VECTOR_COLUMNS:
        return np.array(value, dtype=np.float32)
    if col in _JSONB_COLUMNS:
        return json.dumps(value)
    return value


async def _seed_from_jsonl(conn: asyncpg.Connection, path: Path) -> None:
    """Insert every row in `path` into its tagged table. Skips the meta header."""
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("_meta"):
            continue
        table = payload.pop("table")
        if table not in _ALLOWED_TABLES:
            raise ValueError(f"unexpected table in seed JSONL: {table!r}")
        cols = [c for c in payload if c not in _AUTO_COLUMNS]
        for c in cols:
            if c not in _ALLOWED_COLUMNS:
                raise ValueError(f"unexpected column in seed JSONL: {c!r}")
        values = [_coerce(c, payload[c]) for c in cols]
        placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
        col_list = ", ".join(cols)
        # `table` and `col_list` validated against allowlists above; values
        # are bound via $N parameters. Safe to interpolate.
        sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"  # noqa: S608
        await conn.execute(sql, *values)


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
    if not SEED_PATH.exists():
        pytest.skip(f"{SEED_PATH} not found; run `uv run python -m evals.fixtures.build_seed` first")
    async with eval_pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
        await _seed_from_jsonl(conn, SEED_PATH)
    return eval_pool


@pytest.fixture(scope="session")
async def eval_deps(eval_seeded_pool: asyncpg.Pool):
    # Deferred so `pytest --collect-only` doesn't require ANTHROPIC_API_KEY.
    from openai import AsyncOpenAI

    from rag.agent import Deps

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
async def all_scores(eval_deps) -> dict[str, dict[str, float]]:
    """Run every dataset entry through the agent, score in one batch, write results.json.

    Session-scoped: the agent is invoked once per row, then Ragas scores them
    in a single batch. results.json is written even if scoring fails partway,
    so a failed run still surfaces answers + tools_used.
    """
    from evals.harness import run_query  # deferred import — see top of file

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
