# backfill_embeddings resilience & speed changes

All changes below have been applied to `scripts/load_nvd_full.py`.

**Status:** Blocked — Timescale Cloud DB is in read-only mode after exceeding
free tier limits. Upgraded to base plan, waiting on support to re-enable writes.

## Applied changes

### 1. BACKFILL_BATCH_SIZE = 1000
Separate constant from `EMBEDDING_BATCH_SIZE` (500). 2000 was tried first but
hit OpenAI's 300k token-per-request limit with CVE content lengths.

### 2. Retry OpenAI embedding calls
3 attempts with exponential backoff (1s, 2s, 4s), catching broad `Exception`.

### 3. Bulk UPDATE with unnest
Replaced per-row `executemany` with single `conn.execute` using
`unnest($1::varchar[], $2::vector[])`. Uses `pgvector.vector.Vector` objects
(not numpy arrays) for asyncpg serialization.

### 4. caffeinate reminder
Printed at start of `backfill_embeddings()`.

### 5. Expanded DB retry logic
- Added `ReadOnlySQLTransactionError` to `DB_RETRY_EXCEPTIONS`
- Added `BACKFILL_MAX_RETRIES = 5` (separate from `MAX_RETRIES = 3`)
- Exponential backoff on DB retries: 5s, 10s, 20s, 40s, 80s

## Once DB is writable again

```bash
caffeinate -i uv run python scripts/load_nvd_full.py --backfill-embeddings
```

Verify progress:
```sql
SELECT COUNT(*) FROM nvd_vulnerabilities WHERE embedding IS NULL;
```
