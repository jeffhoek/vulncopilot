# ETL Stats Persistence & Dashboard Plan

## Overview

The scheduled ETL job ([`scripts/run_etl.py`](../scripts/run_etl.py)) already produces structured
per-loader results and emails a SUCCESS/FAILED summary via Azure Communication Services. This plan
**persists that same data to a new `etl_runs` table** and **surfaces a scrollable run-history panel
in the Chainlit app** so the email is no longer the only record of ETL health.

Nothing new needs to be extracted — `run_pipeline()` already returns exactly the data the email
renders. The two halves are independently shippable:

| PR | Scope | Risk |
|---|---|---|
| **1 — Persist runs** | New `etl_runs` table + best-effort write from the ETL job + RBAC grants | Low (additive, backend only) |
| **2 — Display panel** | Read recent runs and render a scrollable history panel in Chainlit | Moderate (front-end component is the only real work) |

PR 1 ships value on its own (queryable run history via SQL). PR 2 builds on it and is read-only.

---

## PR 1 — Persist ETL runs

### Step 1 — Database schema

**File:** [`rag/database.py`](../rag/database.py)

Add to `SCHEMA_SQL` (follow the existing idempotent `CREATE TABLE IF NOT EXISTS` pattern, applied
on startup only when `settings.db_init_schema` is set — i.e. by the admin/ETL connection):

```sql
CREATE TABLE IF NOT EXISTS etl_runs (
    id            SERIAL PRIMARY KEY,
    run_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        VARCHAR(10) NOT NULL,        -- SUCCESS | FAILED
    total_elapsed NUMERIC(8,2) NOT NULL,       -- seconds
    results       JSONB        NOT NULL        -- per-loader list: label, ok, elapsed, summary, metrics, error
);
CREATE INDEX IF NOT EXISTS etl_runs_run_at_idx ON etl_runs (run_at DESC);
```

The per-loader `results` list maps cleanly to a single `JSONB` column (the schema already uses JSONB
for `nvd_vulnerabilities.raw_json`). Keeping the full list as JSONB avoids a second child table while
preserving every field the email shows. `status` is derivable but stored denormalized so the panel /
SQL filters (`WHERE status = 'FAILED'`) stay index-friendly without unpacking JSON.

Applied automatically on next admin/ETL startup, or manually:
```bash
psql -h localhost -U postgresuser -d mydb -c "CREATE TABLE IF NOT EXISTS etl_runs (...)"
```

### Step 2 — RBAC grants

**File:** [`docs/supabase-readonly-role.md`](../docs/supabase-readonly-role.md)

The project splits DB roles (`app_etl` = SELECT/INSERT/UPDATE, `app_readonly` = SELECT only). The new
table needs grants added to the documented grant blocks — **easy to miss, and the failure is a silent
runtime permission error**:

```sql
GRANT INSERT ON etl_runs TO app_etl;
GRANT USAGE, SELECT ON SEQUENCE etl_runs_id_seq TO app_etl;  -- SERIAL needs the sequence grant
GRANT SELECT ON etl_runs TO app_readonly;
```

If the doc uses `ALTER DEFAULT PRIVILEGES`, confirm whether new tables are covered automatically; if
so, only the explicit one-time grant for the already-created table is needed.

### Step 3 — Write the run

**File:** [`scripts/run_etl.py`](../scripts/run_etl.py)

Add a `record_run(results, total_elapsed)` helper and call it in `main()` right after
`build_email()`, alongside `send_email()`. Mirror the **best-effort contract** of `send_email()`:
wrap in try/except and never let a DB error change the process exit code — the ETL outcome must
remain authoritative.

```python
def record_run(results: list[dict], total_elapsed: float) -> None:
    """Persist the run to etl_runs (best-effort; never masks the ETL result)."""
    if not os.getenv("ETL_DB_RECORD", "").lower() == "true" and not settings...:
        ...  # decide gating, see open question below
    status = "SUCCESS" if all(r["ok"] for r in results) else "FAILED"
    try:
        # asyncpg one-shot connect (run_etl is sync; loaders use asyncio.run per step)
        async def _insert():
            conn = await asyncpg.connect(dsn=settings.get_database_dsn())
            try:
                await conn.execute(
                    "INSERT INTO etl_runs (status, total_elapsed, results) VALUES ($1, $2, $3)",
                    status, round(total_elapsed, 2), json.dumps(results),
                )
            finally:
                await conn.close()
        asyncio.run(_insert())
        print("ETL run recorded to etl_runs.")
    except Exception as exc:
        print(f"WARNING: failed to record ETL run: {exc}")
```

Notes:
- `metrics` dicts inside `results` are already JSON-serializable (`dict[str, int]`), so
  `json.dumps(results)` works directly. Pass the JSON string to a `JSONB` column, or register a JSON
  codec — confirm which asyncpg expects.
- The ETL job must connect with a role that has INSERT (`app_etl` or admin), not `app_readonly`.

### Step 4 — Tests

**File:** `tests/` (follow existing ETL test patterns)

- `record_run` builds the correct row from a sample `results` list (status SUCCESS vs FAILED).
- A raised DB error is swallowed and does not propagate / change exit code (best-effort contract).
- Optional: a round-trip integration test against a local Postgres if the suite already has one.

---

## PR 2 — Display panel in Chainlit

### Step 5 — Read recent runs

**File:** `rag/etl_stats.py` (new) or extend `rag/database.py`

```python
async def get_recent_runs(pool, limit: int = 50) -> list[dict]:
    rows = await pool.fetch(
        "SELECT run_at, status, total_elapsed, results "
        "FROM etl_runs ORDER BY run_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]
```

`app_readonly` already has SELECT, so the runtime app reads this with no privilege changes.

### Step 6 — Render the panel

**Chainlit 2.10.0 options (recommended → least-fit):**

1. **`cl.CustomElement`** (recommended) — a JSX component in `public/elements/` (e.g.
   `EtlHistory.jsx`) handed the `get_recent_runs()` payload. Renders a real scrollable history table /
   status badges, styled to match the app. Most flexible; the only meaningful work in this PR.
2. **`cl.ElementSidebar`** (`ElementSidebar.set_elements([...])`) — docks the element in a persistent
   right-hand panel rather than inline in the conversation. Best if the panel should always be visible.
3. **`cl.Dataframe`** — drop a pandas table of recent runs into a message. Fastest to build, least
   layout control. Good fallback / first iteration.

**Skip the iframe approach.** Chainlit has no native generic iframe element, and the data already
lives in our DB — embedding an `<iframe>` to a separate self-hosted stats page is strictly more work
for a worse result than rendering a CustomElement directly.

**Trigger** (pick one, see open questions):
- An action button (extend the existing `_quick_query_actions()` pattern in [`app.py`](../app.py)), or
- Auto-show on `@cl.on_chat_start`, or
- A docked `ElementSidebar` populated on chat start.

### Step 7 — Tests

- `get_recent_runs` returns rows newest-first and respects `limit`.
- Render path handles empty history (no runs yet) gracefully.

---

## Open questions (decide before building)

1. **Visibility / access.** A public-access change recently merged (PR #28). If the app is or becomes
   anonymous-facing, should ETL operational stats be visible to everyone, or gated to the admin login
   (`APP_USERNAME` in [`app.py`](../app.py))? This is the biggest scope driver.
2. **Panel vs. on-demand.** Always-on `ElementSidebar` vs. an action button that shows history on
   request. Drives the Step 6 approach more than anything else.
3. **Write gating.** Should `record_run` be unconditional, or gated by an env flag (like email is
   gated by `ACS_*`/`ETL_EMAIL_TO`)? Local runs without DB write access shouldn't error — best-effort
   already covers this, but an explicit `ETL_DB_RECORD` flag makes intent clear.
4. **Retention.** Unbounded `etl_runs` growth is slow (twice-daily = ~730 rows/yr, negligible), but
   decide if a retention window / `LIMIT` on display is enough or a periodic prune is wanted.

---

## Effort estimate

- **PR 1:** ~1–2 hours, mostly mechanical (schema + insert + grants + tests).
- **PR 2:** ~half a day to a day, almost entirely in the front-end component; the query is trivial.

Overall **low risk, moderate effort** — the backend is a layup; the only real work and the only
unknowns are in the Chainlit rendering.
