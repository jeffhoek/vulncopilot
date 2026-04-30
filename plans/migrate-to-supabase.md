# Migrate from Timescale Cloud to Supabase

Migration runbook for moving the pgvector-backed KEV/NVD database from Timescale Cloud to Supabase Pro.

## Context

| | Timescale Cloud | Supabase Pro |
|---|---|---|
| Monthly cost | ~$30 | $25 |
| Storage included | 25 GB | 8 GB |
| Storage overage | N/A | $0.125/GB |
| pgvector | ✅ | ✅ |
| Backups | Continuous | Daily, 7-day retention |
| PITR | ✅ | ❌ (Pro) |

Current DB size: **~7.29 GiB** — within the 8 GB limit, but close. See [Storage headroom](#storage-headroom) below before migrating.

## Prerequisites

- `pg_dump` / `pg_restore` installed locally (`brew install libpq`)
- Access to Timescale Cloud connection string
- Supabase project provisioned at [supabase.com](https://supabase.com)
- Supabase connection strings from **Project Settings → Database**

## Storage headroom

At 7.29 GiB with an 8 GB cap, you have ~730 MB of headroom. NVD grows by roughly 20,000–30,000 CVEs/year; at ~8 KB/row (embedding + text + indexes amortized) that's ~160–240 MB/year of growth. Overage is $0.125/GB/month — modest cost.

**Optional: reclaim ~0.5–1.2 GB before migrating** by dropping the `raw_json` column (all useful fields are already extracted into dedicated columns; raw JSON can be re-fetched from NVD if ever needed):

```sql
ALTER TABLE vulnerabilities DROP COLUMN IF EXISTS raw_json;
```

Run this on Timescale Cloud *before* dumping, or on Supabase *after* restoring, to reduce ongoing storage cost.

## Step 1 — Provision Supabase project

1. Create a new project at [supabase.com/dashboard](https://supabase.com/dashboard)
2. Choose the region closest to your app deployment
3. Note the two connection strings from **Settings → Database**:
   - **Direct connection** — use for migrations and `pg_restore`
   - **Transaction pooler (port 6543)** — use for the app at runtime

## Step 2 — Enable pgvector on Supabase

Connect to your Supabase project via the SQL editor or `psql` and run:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Verify:

```sql
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
```

## Step 3 — Dump from Timescale Cloud

Use the direct connection string from Timescale Cloud. The `-Fc` flag produces a compressed custom-format archive.

```bash
pg_dump \
  --no-owner \
  --no-acl \
  -Fc \
  "postgresql://<user>:<password>@<host>.tsdb.cloud.timescale.com:5432/<dbname>?sslmode=require" \
  -f timescale_backup.dump
```

> **Note:** `--no-owner` and `--no-acl` prevent Timescale-specific roles from being baked into the dump, which would cause restore errors on Supabase.

Check dump size:

```bash
ls -lh timescale_backup.dump
```

## Step 4 — Restore to Supabase

Use the **direct connection** string (not the pooler) for `pg_restore`. Supabase's direct connection uses port `5432`.

```bash
pg_restore \
  --no-owner \
  --no-acl \
  -d "postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres" \
  timescale_backup.dump
```

The HNSW index rebuild will be the slowest part — expect several minutes for 250k vectors. Monitor progress:

```sql
SELECT phase, blocks_done, blocks_total
FROM pg_stat_progress_create_index;
```

## Step 5 — Verify the restore

```sql
-- Row counts
SELECT COUNT(*) FROM nvd_vulnerabilities;
SELECT COUNT(*) FROM kev_vulnerabilities;

-- Spot-check a known CVE
SELECT cve_id, description, published
FROM nvd_vulnerabilities
WHERE cve_id = 'CVE-2021-44228';

-- Confirm vector index exists
SELECT indexname, indexdef
FROM pg_indexes
WHERE tablename = 'nvd_vulnerabilities'
  AND indexdef ILIKE '%hnsw%';

-- Confirm pgvector works
SELECT cve_id, embedding <=> embedding AS self_distance
FROM nvd_vulnerabilities
LIMIT 1;
```

## Step 6 — Update app configuration

Update `.env` to use Supabase. Use the **transaction pooler** (port 6543) for the running app:

```dotenv
# Remove or comment out individual PG_* vars
# PG_HOST=...
# PG_PORT=...
# PG_USER=...
# PG_PASSWORD=...
# PG_DATABASE=...

# Add Supabase connection URL (transaction pooler for app runtime)
PG_DATABASE_URL=postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require
```

> See `.env.example` — the app reads `PG_DATABASE_URL` when set, falling back to individual `PG_*` vars.

## Step 7 — Smoke test

```bash
uv run chainlit run app.py
```

Run these queries in the chatbot to exercise the full RAG pipeline:

- "What vulnerabilities affect Apache Log4j?"
- "Show recent KEV entries"
- "What ransomware campaigns are tracked?"

If using the MCP server, restart it and confirm it connects — see [docs/mcp-server.md](mcp-server.md).

## Step 8 — Cut over and decommission

Once satisfied with Supabase:

1. Cancel Timescale Cloud subscription from the Timescale dashboard
2. Delete the local `timescale_backup.dump` (or archive it to cold storage)
3. Update any deployment configs (Azure App Service, GCP Cloud Run, EKS) with the new `PG_DATABASE_URL` — see deployment docs

## Rollback

If something goes wrong before decommissioning Timescale:

1. Revert `.env` to the original Timescale `PG_DATABASE_URL` or `PG_*` vars
2. Restart the app — no data was modified on Timescale during this migration

## Connection string reference

| Use case | Port | Which string |
|---|---|---|
| `pg_restore` | 5432 | Direct connection |
| App runtime (`PG_DATABASE_URL`) | 6543 | Transaction pooler |
| psql / ad-hoc queries | 5432 | Direct connection |

Supabase connection strings are available under **Project Settings → Database → Connection string**.
