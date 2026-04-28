# Data Loading

This guide covers populating the PostgreSQL/pgvector database with CISA KEV and NIST NVD vulnerability data. These steps apply regardless of where the database is hosted (local container, Timescale Cloud, RDS, etc.).

## Prerequisites

- PostgreSQL with pgvector extension enabled
- `.env` configured with `DATABASE_URL` (or `PG_*` vars) and `OPENAI_API_KEY`
- Dependencies installed (`uv sync`)

Verify pgvector is available:

```bash
psql "$DATABASE_URL" -c "SELECT extname FROM pg_extension WHERE extname = 'vector';"
```

If not present:

```bash
psql "$DATABASE_URL" -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

## Schema

Schema creation runs automatically on app startup. To create it manually:

```bash
uv run python -c "from rag.database import init_db; import asyncio; asyncio.run(init_db())"
```

## ETL Scripts

There are three ETL scripts, each targeting a different scope:

| Script | Scope | Records | Use case |
|---|---|---|---|
| `scripts/load_kev.py` | CISA KEV catalog | ~1,500 | Always run first — KEV is the primary dataset |
| `scripts/load_nvd.py` | NVD data for KEV CVEs only | ~1,500 | Enriches KEV entries with CVSS scores, severity, affected products |
| `scripts/load_nvd_full.py` | Entire NVD database | ~280,000 | Full NVD corpus for broader vulnerability research |
| `scripts/scrape_references.py` | NVD reference URLs | ~5,000–8,000 | Scrapes linked advisories and write-ups for richer semantic search |

### 1. Load CISA KEV data

Fetches the CISA KEV catalog and generates OpenAI embeddings:

```bash
uv run python scripts/load_kev.py
```

### 2. Load NVD enrichment (KEV-scoped)

Fetches NVD data only for CVE IDs already in the `kev_vulnerabilities` table:

```bash
uv run python scripts/load_nvd.py
```

**Rate limits:**
- Without API key: 5 requests/30s (~5 min for full load)
- With API key: 50 requests/30s (~30 sec for full load)

The script is incremental — it skips CVEs already loaded, so re-runs only fetch new entries.

### 3. Load full NVD database (optional)

Fetches the entire NVD (~280k CVEs) via paginated bulk API calls. This is a large dataset requiring ~3.5-5.5 GB of storage (see [plans/postgres-hosting-options.md](../plans/postgres-hosting-options.md) for sizing details).

```bash
# Full load — fetches all CVEs, generates embeddings
uv run python scripts/load_nvd_full.py

# Incremental sync — fetches only CVEs published or modified since last run
uv run python scripts/load_nvd_full.py --incremental

# Override start date — use after an interrupted incremental run
uv run python scripts/load_nvd_full.py --incremental --since 2026-04-14

# Data only, skip embedding generation (faster initial load)
uv run python scripts/load_nvd_full.py --skip-embeddings

# Backfill embeddings for records loaded without them
uv run python scripts/load_nvd_full.py --backfill-embeddings

# Test with a limited number of pages
uv run python scripts/load_nvd_full.py --limit 3
```

**Features:**
- Paginated bulk fetching (2,000 CVEs per page)
- Checkpoint/resume — interrupted full loads pick up where they left off
- Two-phase incremental sync: new CVEs (by publish date) first, then modified CVEs — ensures newly published vulnerabilities aren't buried behind routine metadata updates
- Staging table upserts (`INSERT ... ON CONFLICT`) for idempotent loads
- Retry logic for both NVD API and database connections

**Recovering from an interrupted incremental sync:**

If you kill an incremental run mid-way, the `MAX(last_modified)` high-water mark in the DB will have advanced, causing the next run to skip unprocessed records. Use `--since` to force the original start date:

```bash
uv run python scripts/load_nvd_full.py --incremental --since 2026-04-14
```

Already-processed records will upsert harmlessly.

**Recommended workflow for the full NVD load:**

1. Load data without embeddings (fast, ~30 min with API key):
   ```bash
   uv run python scripts/load_nvd_full.py --skip-embeddings
   ```

2. Backfill embeddings separately (can be interrupted and resumed):
   ```bash
   caffeinate -i uv run python scripts/load_nvd_full.py --backfill-embeddings
   ```

3. Keep up to date with incremental syncs (Phase 2 can take several hours — use `caffeinate -i`):
   ```bash
   caffeinate -i uv run python scripts/load_nvd_full.py --incremental
   ```

**HNSW index and large incremental syncs:**

NVD modifies thousands of CVEs per week for routine metadata refreshes (CVSS rescoring, CPE updates, etc.), so large incremental windows can involve 20k–80k upserts. Maintaining the HNSW vector index on every batch causes significant Disk IO — on constrained hosting (e.g. Supabase Micro) this can make each 2,000-row batch take several minutes.

For large syncs (roughly monthly or after a long gap), drop the index before running and rebuild it afterward. **Upgrade to Medium compute before the rebuild** — Micro cannot allocate enough shared memory for a usable `maintenance_work_mem` setting, making the build extremely slow. Downgrade back to Micro when done.

```sql
-- Before ETL (run in Supabase SQL editor or psql)
DROP INDEX IF EXISTS nvd_embedding_idx;
```

```bash
caffeinate -i uv run python scripts/load_nvd_full.py --incremental
```

```bash
# After ETL — rebuild with 1GB maintenance_work_mem (max usable on Medium)
caffeinate -i time psql "$DATABASE_URL" -c "SET statement_timeout = 0; SET maintenance_work_mem = '1GB'; CREATE INDEX nvd_embedding_idx ON nvd_vulnerabilities USING hnsw (embedding vector_cosine_ops);"
```

Monitor progress in the Supabase SQL editor:

```sql
SELECT phase, tuples_done, tuples_total,
       round(tuples_done::numeric / nullif(tuples_total, 0) * 100, 1) AS pct_done
FROM pg_stat_progress_create_index
WHERE relid = 'nvd_vulnerabilities'::regclass;
```

The row disappears when the build completes. On Medium with 1GB `maintenance_work_mem`, expect ~60 minutes for ~346k rows at 1536 dimensions. The chatbot's semantic search is unavailable during this window but the app remains up.

For smaller weekly syncs the index overhead is usually tolerable — skip the drop/rebuild unless upsert batches start taking several minutes.

### 4. Scrape NVD reference URLs

Scrapes the reference URLs stored in `nvd_vulnerabilities.reference_urls`, extracts main-text
content, summarizes long pages with Claude Haiku, generates embeddings, and stores results in
the `cve_references` table. Requires NVD data to be loaded first (step 2 or 3).

```bash
# Initial scrape — processes all unscraped (cve_id, url) pairs
uv run python scripts/scrape_references.py

# Single CVE — useful for testing or targeted refresh
uv run python scripts/scrape_references.py --cve CVE-2021-44228

# Re-scrape stale entries — URLs not fetched in the last 30 days (skips known-dead links)
uv run python scripts/scrape_references.py --refresh
```

**What gets skipped:**

Low-signal domains are skipped immediately without fetching (NVD self-refs, MITRE CVE pages,
social media). Pages blocked by `robots.txt`, HTTP errors, and pages with no extractable text
are recorded with a `skip_reason` so re-runs don't re-attempt them.

**Summarization:**

Pages longer than ~8,000 characters are summarized with Claude Haiku before embedding to keep
chunk quality high and stay within the embedding model's token limit. Requires
`ANTHROPIC_API_KEY` in `.env`.

**Scale estimate:**

KEV-scoped NVD (~1,500 CVEs, up to 10 URLs each) produces roughly 5,000–8,000 fetchable URLs
after denylist filtering. With per-domain rate limiting and HTTP timeouts, expect 2–4 hours
for an initial full scrape. Use `--cve` to test a single CVE first.

**Interrupted runs:**

Unlike `load_nvd_full.py`, the reference scraper has no checkpoint file — there is no mid-batch
resume. However, a restart is safe: already-processed URLs are skipped by the `NOT EXISTS`
guard in the query, so only unprocessed pairs will be re-attempted.

## NVD API Key

All NVD scripts benefit from an API key, which increases the rate limit from 5 to 50 requests per 30 seconds. Set `NVD_API_KEY` in `.env`. Request a free key at https://nvd.nist.gov/developers/request-an-api-key.

## Verification

```bash
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM kev_vulnerabilities;"
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM nvd_vulnerabilities;"
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM nvd_vulnerabilities WHERE embedding IS NULL;"
```

For reference scraping:

```bash
# Overall counts
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM cve_references;"
psql "$DATABASE_URL" -c "SELECT COUNT(*) FROM cve_references WHERE embedding IS NOT NULL;"

# Breakdown by outcome
psql "$DATABASE_URL" -c "
  SELECT
    COALESCE(skip_reason, 'scraped') AS outcome,
    COUNT(*) AS count
  FROM cve_references
  GROUP BY outcome
  ORDER BY count DESC;"

# Sample of successfully scraped rows
psql "$DATABASE_URL" -c "
  SELECT cve_id, domain, http_status, length(content) AS content_len, scraped_at
  FROM cve_references
  WHERE embedding IS NOT NULL
  ORDER BY scraped_at DESC
  LIMIT 10;"
```

## Refreshing Data

**CISA KEV + NVD enrichment** (re-run to pick up new entries):

```bash
uv run python scripts/load_kev.py
uv run python scripts/load_nvd.py
```

**Full NVD** (incremental sync):

```bash
# Weekly — index overhead is usually fine
caffeinate -i uv run python scripts/load_nvd_full.py --incremental

# Monthly / large gap — upgrade to Medium compute, drop HNSW index first, rebuild after (see above)
```

**Reference URLs** (re-scrape pages older than 30 days, skip dead links):

```bash
uv run python scripts/scrape_references.py --refresh
```

No app restart is needed — data is queried live from the database.
