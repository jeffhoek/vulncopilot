# Test Plan

A targeted test suite for the KEV/NVD RAG chatbot. Goal: cover every
decision point that could fail silently or produce wrong behaviour,
without padding for coverage metrics or making feature development
onerous. Evals (Ragas / autoevals, LLM-as-judge) are a separate future
phase — see [future-enhancements.md](future-enhancements.md#evaluation-framework)
— but fixture design here is deliberately forward-compatible with them.

## Guiding principles

- Test behaviour, not implementation. Assert what comes out, not which
  internal calls were made.
- Mock at the I/O boundary only. asyncpg pool and OpenAI client are
  mocked for unit tests; the real DB is used for integration tests.
- No testing of trivial wiring. Pydantic Settings validation, Chainlit
  decorator registration, and `__init__.py` re-exports are not tested.
- Skip `app.py` and `config.py` entirely for now. Chainlit lifecycle
  hooks are framework-dependent and not meaningfully unit-testable.
- ETL scripts (`scripts/`) are out of scope for this phase.
- CI coverage gate: **65%** — a byproduct of the tests below, not a
  target to pad. Enforced with `--fail-under=65` in pytest config.

## Test types

| Type | What it covers | DB required |
|---|---|---|
| Unit | Pure functions, mock I/O boundaries | No |
| Contract | MCP auth middleware (ASGI-level) | No |
| Integration | Real asyncpg pool against a test DB | Yes |

No E2E (Chainlit UI) or property-based tests in this phase.

## Tools and configuration

- **pytest** with **pytest-asyncio** (`asyncio_mode = "auto"`)
- **pytest-cov** for coverage reporting
- **pytest-mock** (or stdlib `unittest.mock`) for mocking
- **httpx** `ASGITransport` for ASGI-level contract tests (no live server)

`pyproject.toml` additions:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
testpaths = ["tests"]

[tool.coverage.run]
source = ["rag", "mcp_server"]
omit = ["scripts/*", "app.py", "config.py"]

[tool.coverage.report]
fail_under = 65
```

Dev dependencies to add (`httpx` is already a prod dependency — do not add it again):

```
pytest
pytest-asyncio
pytest-cov
pytest-mock
```

## Directory layout

```
tests/
├── conftest.py              # shared fixtures (pool, openai client, test DB)
├── unit/
│   ├── test_sql_utils.py    # validate_sql, apply_row_limit, format_query_results
│   ├── test_embeddings.py   # generate_embedding, generate_embeddings_batch
│   └── test_vector_store.py # PgVectorStore (mocked pool)
├── contract/
│   └── test_mcp_middleware.py  # McpRouterMiddleware auth enforcement
└── integration/
    ├── test_vector_store_db.py  # PgVectorStore against real test DB
    └── test_mcp_tools_db.py     # MCP query + retrieve end-to-end
```

`tests/evals/` will be added in the evaluation framework phase. Fixtures
in `conftest.py` are designed to be reused there without restructuring.

## Shared fixtures (`conftest.py`)

```python
# Real asyncpg pool for integration tests — scoped to the session for speed.
# Requires TEST_DATABASE_URL env var pointing at a local postgres with pgvector.
@pytest.fixture(scope="session")
async def db_pool() -> asyncpg.Pool: ...

# Seeded pool: runs schema init and inserts a small fixed golden dataset.
# Used by both integration tests and (later) evals.
@pytest.fixture(scope="session")
async def seeded_pool(db_pool) -> asyncpg.Pool: ...

# Mock OpenAI client that returns a deterministic unit embedding vector.
@pytest.fixture
def mock_openai() -> AsyncMock: ...

# Patch settings.embedding_model so embedding unit tests don't require
# EMBEDDING_MODEL to be set in the environment.
@pytest.fixture(autouse=False)
def mock_settings(monkeypatch): monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small") ...

# PgVectorStore wrapping the real seeded pool.
@pytest.fixture(scope="session")
async def vector_store(seeded_pool) -> PgVectorStore: ...
```

Integration tests require `TEST_DATABASE_URL`. They are **not** skipped
automatically when the variable is absent — fail loudly in CI so the gap
is visible. Local developers without a DB run `pytest tests/unit
tests/contract` to skip integration.

## Unit tests — `rag/sql_utils.py`

~10 tests covering all branches.

### `validate_sql`

| # | Input | Expected return |
|---|---|---|
| 1 | `"SELECT * FROM t"` | `None` (valid) |
| 2 | `"select * from t"` (lowercase) | `None` (valid) |
| 3 | `"  SELECT * FROM t"` (leading whitespace) | `None` (valid) |
| 4 | `"DROP TABLE t"` | error string |
| 5 | `"INSERT INTO t VALUES (1)"` | error string |
| 6 | `""` (empty string) | error string |

### `apply_row_limit`

| # | Input SQL | max_rows | Expected behaviour |
|---|---|---|---|
| 7 | `"SELECT * FROM t"` | 100 | appends `LIMIT 100` |
| 8 | `"SELECT * FROM t;"` (trailing semicolon) | 100 | strips `;`, appends `LIMIT 100` |
| 9 | `"SELECT * FROM t LIMIT 10"` | 100 | unchanged (10 ≤ 100) |
| 10 | `"SELECT * FROM t LIMIT 500"` | 100 | rewrites to `LIMIT 100` |
| 11 | `"SELECT * FROM t LIMIT 500"` | 200 | rewrites to `LIMIT 200` (custom max) |

### `format_query_results`

| # | Scenario | Assertion |
|---|---|---|
| 12 | 2-row result | output contains both values, header row, separator, row count line |
| 13 | cell value exceeds `max_cell_chars` | value is truncated with `…` |
| 14 | total output exceeds `max_output_chars` | output ends with truncation notice |
| 15 | total output within `max_output_chars` | no truncation notice present |

## Unit tests — `rag/embeddings.py`

~3 tests. Mock `openai_client.embeddings.create`. Apply the `mock_settings` fixture so `settings.embedding_model` resolves without a live env var.

| # | Scenario | Assertion |
|---|---|---|
| 16 | `generate_embedding` happy path | returns list of floats matching mock response |
| 17 | `generate_embeddings_batch` with 3 texts | calls API once, returns 3 vectors |
| 18 | `generate_embeddings_batch` with empty list | returns empty list without calling API |

## Unit tests — `rag/vector_store.py`

~4 tests. Mock `pool.acquire()` context manager.

| # | Scenario | Assertion |
|---|---|---|
| 19 | `search` returns rows | returns list of content strings in rank order |
| 20 | `search` returns no rows | returns empty list |
| 21 | `get_document_count` happy path | returns integer from mock row |
| 22 | `search` passes `top_k` to query | SQL contains the expected limit value |

## Contract tests — `mcp_server/server.py` (McpRouterMiddleware)

~5 tests. Use `httpx.ASGITransport` to drive the middleware directly —
no live server, no DB. `set_mcp_context` is **not** called so
`_mcp_context` remains `None`; these tests only exercise the auth layer.

Note: `McpRouterMiddleware.__init__` calls `mcp.http_app(...)` (FastMCP
initialization). Verify in implementation whether FastMCP handles this
cleanly in test contexts; if it has side effects, mock `mcp.http_app`
in the fixture setup.

| # | Scenario | Expected HTTP status |
|---|---|---|
| 23 | `GET /mcp` with correct `X-Api-Key` | passes through to MCP app (not 401) |
| 24 | `GET /mcp` with wrong `X-Api-Key` | 401 |
| 25 | `GET /mcp` with no `X-Api-Key` header | 401 |
| 26 | `GET /other` path — no auth header | passes through (non-MCP path) |
| 27 | `MCP_API_KEY` is `None` (unset) | all `/mcp` requests pass through unauthenticated |

## Integration tests — `vector_store` against real DB

~4 tests. Require `TEST_DATABASE_URL`. Use the `seeded_pool` fixture.

| # | Scenario | Assertion |
|---|---|---|
| 28 | `search` with a known embedding | returns non-empty list; top result is semantically close |
| 29 | `search` top_k is respected | result list length ≤ top_k |
| 30 | `get_document_count` | returns count matching seeded row count |
| 31 | `search` against empty table | returns empty list without raising — use `db_pool` directly with a `TRUNCATE` before this test, **not** `seeded_pool` (which always has rows) |

## Unit tests — MCP tool error paths (`mcp_server/server.py`)

~4 tests. Mock the pool/context; do **not** require a real DB. These
cover the silent-failure branches the guiding principles call out.

| # | Scenario | Assertion |
|---|---|---|
| 36 | `query` called before `set_mcp_context` (`_mcp_context is None`) | returns the expected "context not initialised" error string |
| 37 | `retrieve` called before `set_mcp_context` | returns the expected "context not initialised" error string |
| 38 | `query` — pool raises `asyncpg.PostgresError` | returns `"Error: Database error executing query."` |
| 39 | `query` — pool raises an unexpected `Exception` | returns `"Error: Internal error executing query."` |

## Integration tests — MCP tools end-to-end

~4 tests. Call the MCP tool functions directly (not via HTTP) after
calling `set_mcp_context` with the `seeded_pool`. Validates the full
path from tool function → SQL or embedding → DB → formatted output.

| # | Scenario | Assertion |
|---|---|---|
| 40 | `query` valid SELECT against seeded data | returns formatted table with expected rows |
| 41 | `query` non-SELECT statement | returns permission error string |
| 42 | `query` with no results | returns `"No results found."` |
| 43 | `retrieve` with a relevant query string | returns non-empty context string |

## Caller contract — `format_query_results` with empty input

`format_query_results([])` raises `IndexError` (accesses `rows[0].keys()`).
This is intentional — the `query` tool filters empty results before
calling it (see `server.py` lines 94-95). There is no test for the
crash itself; test #42 covers the caller-side guard. This contract is
documented here rather than tested to make the responsibility explicit.

## What is explicitly not tested

- `app.py` — Chainlit lifecycle hooks (framework-dependent)
- `config.py` — Pydantic Settings field assignment (framework-validated)
- `rag/database.py` schema init — DDL correctness is validated implicitly
  by integration tests that use the schema
- ETL scripts (`scripts/`) — deferred to a later phase
- Chainlit tool registration on `rag_agent` — framework wiring, not
  application logic

## Forward compatibility with evals

When the evaluation framework phase begins:

1. Add `tests/evals/conftest.py` that imports `seeded_pool`,
   `vector_store`, and `mock_openai` from `tests/conftest.py`.
2. Add a `golden_dataset` fixture (JSONL file of question/expected-answer
   pairs checked in to `tests/evals/fixtures/`).
3. Eval tests import Ragas / autoevals scorers; regular tests never do.
4. Evals run under a separate pytest mark (`-m evals`) and are excluded
   from the default CI run due to LLM cost — they run on a nightly
   schedule or on-demand.

No restructuring of existing fixtures is required.

## Implementation order

1. Install dev dependencies and add pytest config to `pyproject.toml`
2. Write `tests/conftest.py` (fixtures only, no tests)
3. `tests/unit/test_sql_utils.py` — highest value, zero setup
4. `tests/unit/test_embeddings.py`
5. `tests/unit/test_vector_store.py`
6. `tests/contract/test_mcp_middleware.py`
7. Set up local test DB; validate `seeded_pool` fixture
8. `tests/integration/test_vector_store_db.py`
9. `tests/integration/test_mcp_tools_db.py`
10. Enable coverage gate in CI — add `--cov --cov-fail-under=65` to the
    pytest step in `azure-pipelines.yml`
