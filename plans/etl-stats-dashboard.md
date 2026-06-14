# ETL Stats Persistence & Dashboard Plan

## Overview

The scheduled ETL job ([`scripts/run_etl.py`](../scripts/run_etl.py)) already produces structured
per-loader results and emails a SUCCESS/FAILED summary via Azure Communication Services. This plan
**persists that same data to a new `etl_runs` table** and **surfaces an always-on, public run-history
panel** so the email is no longer the only record of ETL health — and so anyone (logged in or not)
can see that the data is freshly updated.

Nothing new needs to be extracted — `run_pipeline()` already returns exactly the data the email
renders. The two halves are independently shippable:

| PR | Scope | Risk |
|---|---|---|
| **1 — Persist runs** | New `etl_runs` table + unconditional best-effort write from the ETL job + RBAC grants | Low (additive, backend only) |
| **2 — Public stats page** | A public FastAPI route serving an always-on, scrollable run-history page | Moderate (small HTML page; the query is trivial) |

PR 1 ships value on its own (queryable run history via SQL). PR 2 builds on it and is read-only.

See **Decisions** at the end for the resolved design choices that shape this plan (public visibility,
always-on panel, no write flag, no retention job yet).

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

The write is **unconditional** (no env flag). Unlike email — which is gated by `ACS_*`/`ETL_EMAIL_TO`
because those credentials are *absent* on local runs — the DB DSN is always present and write-capable
wherever the ETL runs (the loaders write CVE data). So there's no graceful-skip gap to cover, and
best-effort `try/except` already absorbs any unexpected error. (Resolved open question #3.)

```python
def record_run(results: list[dict], total_elapsed: float) -> None:
    """Persist the run to etl_runs (best-effort; never masks the ETL result)."""
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

## PR 2 — Public stats page

**Design driver:** stats must be visible to **everyone, including logged-out visitors** (Decision 1).
The app gates the whole Chainlit UI behind `password_auth_callback` ([`app.py`](../app.py)), so a
Chainlit element (`CustomElement` / `ElementSidebar`) — which only renders inside an authenticated
session — **cannot** serve anonymous visitors. The fix is a **public FastAPI route** mounted on the
same app object Chainlit already exposes (`from chainlit.server import app as fastapi_app`, already
imported in [`app.py`](../app.py)). This route bypasses Chainlit's login and serves an always-on
stats page. (This is the same mount-a-FastAPI-route pattern the public-access-plan uses for `/admin`,
but **public** — no auth dependency.)

### Step 5 — Read recent runs

**File:** `rag/etl_stats.py` (new) or extend `rag/database.py`

```python
async def get_recent_runs(pool, limit: int = 50) -> list[dict]:
    rows = await pool.fetch(
        "SELECT run_at, status, total_elapsed, results "
        "FROM etl_runs ORDER BY run_at DESC LIMIT $1", limit)
    return [dict(r) for r in rows]
```

`app_readonly` already has SELECT, so the runtime app reads this with no privilege changes. The
`LIMIT` keeps the page responsive regardless of table size (Decision 4 — no prune job needed), and the
`etl_runs_run_at_idx` index from Step 1 keeps the ordered scan fast.

### Step 6 — Public route + always-on page

**File:** [`app.py`](../app.py) (or a small `routes/etl_stats.py` mounted from `app.py`)

```python
from chainlit.server import app as fastapi_app  # already imported

@fastapi_app.get("/etl-stats", response_class=HTMLResponse)
async def etl_stats_page() -> str:
    runs = await get_recent_runs(get_pool(), limit=50)
    return render_etl_stats_html(runs)  # Jinja2 template, auto-escaped
```

- **Always-on, scrollable history.** The page renders the run list newest-first in a scrollable table
  (status badge, timestamp, total duration, per-loader summary + counts). "Always on" = the page is a
  persistent, self-contained surface, not a click-to-open panel (Decision 2). Optionally add a small
  `<meta http-equiv="refresh">` or `fetch` poll so an open tab shows fresh data after each ETL run —
  reinforcing the "visibly fresh data" draw.
- **No iframe.** The page reads `get_recent_runs()` directly and renders server-side; there's no
  second service to embed. (The earlier blanket "render inside Chainlit" advice is superseded by the
  public requirement, but the anti-iframe point stands.)
- **Public-exposure hardening (important):** the per-loader `error` field is raw exception text
  (`f"{type(exc).__name__}: {exc}"`, [`run_etl.py`](../scripts/run_etl.py)) and can leak internal
  detail (paths, connection strings). On this **public** page, show status/counts/durations and
  **either omit the raw `error` string or show a sanitized/generic failure note** — never echo it
  verbatim. Use a template engine with autoescaping (Jinja2) to avoid HTML injection from any stored
  text.
- **Optional logged-in surface.** If an in-chat panel is also wanted later for authenticated users, a
  `cl.ElementSidebar` reading the same `get_recent_runs()` can be added without an iframe — but it is
  **not** required for this plan and does not satisfy the logged-out requirement on its own.

### Step 7 — Tests

- `get_recent_runs` returns rows newest-first and respects `limit`.
- The route returns 200 with empty history (no runs yet) — page renders a graceful "no runs" state.
- The rendered page does **not** contain raw `error` strings (public-exposure hardening regression
  guard); stored text is HTML-escaped.

---

## Decisions

1. **Visibility — public, including logged-out.** Stats are visible to everyone; "visibly fresh data
   is the draw." This forces a public FastAPI route rather than a Chainlit element (see PR 2 intro),
   and requires the public-exposure hardening in Step 6 (sanitize/omit raw `error` text, autoescape).
2. **Always-on, not on-demand.** The stats live on a persistent, self-contained page rather than
   behind a button or a click-to-open panel.
3. **No write flag.** `record_run` is unconditional best-effort — local ETL always has a write-capable
   DSN, so there's no skip gap to gate (see Step 3).
4. **No retention job yet.** Growth is negligible (~730 rows/yr) and the display `LIMIT` keeps the page
   responsive independent of table size. Revisit only if a long-horizon view is wanted later.

---

## Effort estimate

- **PR 1:** ~1–2 hours, mostly mechanical (schema + insert + grants + tests).
- **PR 2:** ~half a day, almost entirely in the public route + HTML template; the query is trivial.

Overall **low risk, moderate effort** — the backend is a layup; the only real work is the public stats
page and its exposure hardening.
