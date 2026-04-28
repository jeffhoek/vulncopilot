# Reference URL Content Scraping — Implementation Plan

## Context

NVD stores up to ten `reference_urls` per CVE, already extracted and saved as a `TEXT[]` column in `nvd_vulnerabilities`. These URLs point to vendor advisories, patch notes, PoC write-ups, and security blog posts — high-signal content that is currently invisible to the RAG pipeline. Scraping and embedding this content would let the agent surface advisory details, patch instructions, and exploit context that never appear in the structured NVD fields.

The primary challenge is not the scraping itself but **URL quality discrimination**: NVD reference lists are noisy, mixing dead links, NVD self-references, MITRE CVE pages (minimal new signal), vendor advisories, GitHub issues, and the occasional PoC. The filtering logic and summarization cost management are where most of the implementation complexity lives.

---

## Critical Files to Read Before Implementing

| File | Purpose |
|------|---------|
| `rag/database.py` | `SCHEMA_SQL` constant — add `cve_references` DDL here |
| `rag/vector_store.py` | `PgVectorStore.search()` — extend UNION query to include `cve_references` |
| `rag/embeddings.py` | `generate_embedding()` / `generate_embeddings_batch()` — reuse as-is |
| `config.py` | `settings`, system prompt string — extend prompt to mention `cve_references` |
| `scripts/load_nvd.py` | Pattern to follow for script structure (httpx, asyncpg, batch embed, upsert) |
| `scripts/nvd_utils.py` | `extract_reference_urls()` — source of the URLs to scrape |

---

## Step 1 — Write the Plan Doc to the Repo

Write this plan to `docs/reference-url-scraping.md` verbatim so it is available in fresh sessions. No other file is created in this step.

---

## Step 2 — Schema: Add `cve_references` Table

Add to `SCHEMA_SQL` in `rag/database.py` (append after the existing table definitions):

```sql
CREATE TABLE IF NOT EXISTS cve_references (
    id              SERIAL PRIMARY KEY,
    url             TEXT NOT NULL,
    cve_id          VARCHAR(20) NOT NULL,
    domain          TEXT,
    title           TEXT,
    scraped_text    TEXT,
    summary         TEXT,
    content         TEXT,
    embedding       vector(1536),
    http_status     INTEGER,
    scraped_at      TIMESTAMPTZ,
    content_hash    TEXT,
    skip_reason     TEXT,
    UNIQUE (url, cve_id)
);

CREATE INDEX IF NOT EXISTS cve_references_embedding_idx
    ON cve_references
    USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS cve_references_cve_id_idx
    ON cve_references (cve_id);

CREATE INDEX IF NOT EXISTS cve_references_scraped_at_idx
    ON cve_references (scraped_at);
```

Column notes:
- `scraped_text`: raw extracted text from the page
- `summary`: LLM-generated summary (only populated for long pages)
- `content`: what gets embedded — either `scraped_text` (short) or `summary` (long)
- `http_status`: enables the re-scrape scheduler to skip known-dead URLs (status ≥ 400)
- `content_hash`: SHA-256 of `scraped_text`; skip re-embedding if unchanged on refresh
- `skip_reason`: populated when URL is skipped — e.g. `"denylist:nvd_self_ref"`, `"robots_txt"`, `"http_404"`
- `UNIQUE (url, cve_id)`: same URL can appear under multiple CVEs; store once per pair

---

## Step 3 — New Script: `scripts/scrape_references.py`

Pattern mirrors `scripts/load_nvd.py`: async, httpx, asyncpg, batch embed, upsert.

### 3a. New dependencies

```bash
uv add trafilatura robotexclusionrulesparser
```

- **`trafilatura`**: extracts main article text from HTML (strips nav, ads, boilerplate). Best-in-class for this use case.
- **`robotexclusionrulesparser`**: full robots.txt parser with TTL caching (stdlib `urllib.robotparser` works but has no caching).

### 3b. URL filtering — domain allowlist / denylist

```python
# Immediate skip — no fetch, record skip_reason
DENYLIST_PATTERNS = [
    "nvd.nist.gov",          # NVD self-refs
    "web.nvd.nist.gov",
    "cve.mitre.org",         # MITRE CVE pages (duplicates NVD description)
    "twitter.com",
    "x.com",
    "facebook.com",
    "youtube.com",
    "linkedin.com",
    "t.co",
]

# High-signal domains — scrape + embed (also scrape unknown domains)
HIGH_SIGNAL_DOMAINS = [
    "github.com",                 # security advisories, issues, PRs
    "exploit-db.com",
    "www.exploit-db.com",
    "packetstormsecurity.com",
    "seclists.org",
    "kb.cert.org",
    "www.kb.cert.org",
    "cert.org",
    "us-cert.cisa.gov",
    "www.cisa.gov",
    "rapid7.com",
    "tenable.com",
    "snyk.io",
    "huntr.dev",
    "security.gentoo.org",
    "ubuntu.com",
    "access.redhat.com",
    "bugzilla.redhat.com",
    "lists.debian.org",
    "www.debian.org",
    "security.freebsd.org",
    "jvn.jp",
    "jvndb.jvn.jp",
    "support.apple.com",
    "security.cisco.com",
    "tools.cisco.com",
    "chromereleases.googleblog.com",
    "android.googlesource.com",
    "source.android.com",
    "msrc.microsoft.com",        # conservative rate limit (see §3d)
]
```

Strategy:
1. If URL domain matches denylist → insert row with `skip_reason`, no fetch
2. Otherwise → fetch and scrape (high-signal and unknown domains are treated equally; skip_reason stays NULL)

### 3c. Per-domain rate limiting

Use a `defaultdict` of `asyncio.Semaphore` keyed by registered domain (eTLD+1), with per-domain concurrency caps and inter-request delays:

```python
import asyncio
from collections import defaultdict

DOMAIN_CONCURRENCY = {
    "microsoft.com": 1,     # MSRC is aggressive; single-threaded
    "cisco.com": 1,
    "apple.com": 1,
    "default": 3,
}

DOMAIN_DELAY_SECONDS = {
    "microsoft.com": 3.0,
    "cisco.com": 2.0,
    "default": 1.0,
}
```

Acquire the domain semaphore before each fetch; sleep `DOMAIN_DELAY_SECONDS[domain]` after release.

### 3d. Robots.txt enforcement

```python
from robotexclusionrulesparser import RobotExclusionRulesParser

_robots_cache: dict[str, RobotExclusionRulesParser] = {}

async def is_allowed(client: httpx.AsyncClient, url: str) -> bool:
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in _robots_cache:
        try:
            r = await client.get(f"{origin}/robots.txt", timeout=10)
            parser = RobotExclusionRulesParser()
            parser.parse(r.text)
        except Exception:
            parser = RobotExclusionRulesParser()  # allow on error
        _robots_cache[origin] = parser
    return _robots_cache[origin].is_allowed("*", url)
```

### 3e. Content extraction

```python
import trafilatura

def extract_text(html: str) -> tuple[str, str]:
    """Returns (title, body_text)."""
    doc = trafilatura.extract(html, include_comments=False, include_tables=True)
    title = trafilatura.extract_metadata(html).title or ""
    return title, doc or ""
```

### 3f. Two-tier summarization

Threshold: **8,000 characters** of extracted text (~2,000 tokens).

- **Short** (< 8,000 chars): `content = scraped_text` — embed directly
- **Long** (≥ 8,000 chars): call `claude-haiku-4-5-20251001` to summarize; `content = summary`

Haiku summarization prompt (keep it tight):

```
Summarize the following security advisory or vulnerability write-up in 3-5 sentences.
Focus on: what is vulnerable, how it can be exploited, what the impact is, and how to remediate.
Do not include boilerplate or disclaimers.

{scraped_text[:12000]}
```

Model: `claude-haiku-4-5-20251001` (already in `config.py` as `settings.llm_model`).
Use `anthropic.AsyncAnthropic` (already in deps via `anthropic>=0.78.0`).

### 3g. Content hash

```python
import hashlib

def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]
```

### 3h. Upsert SQL

```sql
INSERT INTO cve_references (
    url, cve_id, domain, title, scraped_text, summary, content,
    embedding, http_status, scraped_at, content_hash, skip_reason
) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,NOW(),$10,$11)
ON CONFLICT (url, cve_id) DO UPDATE SET
    title         = EXCLUDED.title,
    scraped_text  = EXCLUDED.scraped_text,
    summary       = EXCLUDED.summary,
    content       = EXCLUDED.content,
    embedding     = EXCLUDED.embedding,
    http_status   = EXCLUDED.http_status,
    scraped_at    = EXCLUDED.scraped_at,
    content_hash  = EXCLUDED.content_hash,
    skip_reason   = EXCLUDED.skip_reason
```

### 3i. Script CLI modes

```bash
uv run python scripts/scrape_references.py             # scrape all unprocessed URLs
uv run python scripts/scrape_references.py --refresh   # re-scrape stale (> 30 days) or changed
uv run python scripts/scrape_references.py --cve CVE-2024-XXXX  # single CVE
```

Refresh logic:
- Skip rows where `http_status >= 400` (dead links)
- Re-scrape where `scraped_at < NOW() - INTERVAL '30 days'`
- After fetch, compare `content_hash`; if unchanged, update `scraped_at` only (no re-embed)

### 3j. Main pipeline flow

```
1. Query nvd_vulnerabilities for (cve_id, reference_urls) — LEFT JOIN cve_references
   to find unprocessed (cve_id, url) pairs
2. For each URL:
   a. Check denylist → if match, upsert skip_reason row, continue
   b. Check robots.txt via cached parser
   c. Acquire domain semaphore
   d. GET url with httpx (timeout=30, follow_redirects=True, max_redirects=5)
   e. Record http_status
   f. If status != 200: upsert with status + skip_reason="http_{status}", continue
   g. Extract text via trafilatura
   h. If text empty: upsert with skip_reason="no_content", continue
   i. Compute content_hash
   j. If long: summarize with Haiku → set summary, content=summary
      If short: content=scraped_text
   k. Batch accumulate for embedding (flush every 100)
3. Generate embeddings in batches of 500 (reuse generate_embeddings_batch from rag/embeddings.py)
4. Upsert batch to cve_references
```

---

## Step 4 — Extend Vector Search

In `rag/vector_store.py`, extend `PgVectorStore.search()` to include `cve_references`:

Current query unions `kev_vulnerabilities` and `nvd_vulnerabilities`. Add a third leg:

```sql
SELECT content, embedding <=> $1 AS distance
FROM cve_references
WHERE embedding IS NOT NULL
ORDER BY distance
LIMIT $2
```

Full three-way UNION (replace the existing query body):

```sql
(
    SELECT content, embedding <=> $1 AS distance
    FROM kev_vulnerabilities
    WHERE embedding IS NOT NULL
    ORDER BY distance LIMIT $2
)
UNION ALL
(
    SELECT content, embedding <=> $1 AS distance
    FROM nvd_vulnerabilities
    WHERE embedding IS NOT NULL
    ORDER BY distance LIMIT $2
)
UNION ALL
(
    SELECT content, embedding <=> $1 AS distance
    FROM cve_references
    WHERE embedding IS NOT NULL
    ORDER BY distance LIMIT $2
)
ORDER BY distance LIMIT $2
```

---

## Step 5 — Update System Prompt

In `config.py`, extend the system prompt to mention the new table. Add after the existing table descriptions:

```
- cve_references: Scraped content from NVD reference URLs (vendor advisories, PoC write-ups,
  patch notes). Columns: url, cve_id, domain, title, scraped_text, summary, content, http_status,
  scraped_at. Use retrieve() to surface relevant advisories, or query() to look up references
  for a specific CVE (e.g. WHERE cve_id = 'CVE-2024-XXXX' AND http_status = 200).
```

---

## Step 6 — MCP Server

No changes needed. The MCP server's `retrieve` tool calls `vector_store.search()`, which will automatically include `cve_references` results after Step 4.

---

## Implementation Order

1. Write this doc to `docs/reference-url-scraping.md`
2. `uv add trafilatura robotexclusionrulesparser`
3. Edit `rag/database.py` — add `cve_references` DDL to `SCHEMA_SQL`
4. Create `scripts/scrape_references.py`
5. Edit `rag/vector_store.py` — extend UNION query
6. Edit `config.py` — extend system prompt
7. Run `uv run python scripts/scrape_references.py` against a small subset (e.g. `--cve CVE-2021-44228`) to verify end-to-end

---

## Verification

### Schema
```bash
psql -h localhost -U postgresuser -d mydb -c "\d cve_references"
```

### Single-CVE smoke test
```bash
uv run python scripts/scrape_references.py --cve CVE-2021-44228
psql -h localhost -U postgresuser -d mydb -c \
  "SELECT url, domain, http_status, skip_reason, length(content), scraped_at
   FROM cve_references WHERE cve_id = 'CVE-2021-44228';"
```

### Embedding check
```bash
psql -h localhost -U postgresuser -d mydb -c \
  "SELECT COUNT(*) FROM cve_references WHERE embedding IS NOT NULL;"
```

### RAG retrieval test (via chatbot or MCP)
Ask: *"What do vendor advisories say about Log4Shell exploitation?"*
Expect: results from `cve_references` content mixed with core CVE rows.

### SQL query test (via agent)
Ask: *"Show me all reference URLs for CVE-2021-44228 that were successfully scraped."*
Expected SQL: `SELECT url, title, domain FROM cve_references WHERE cve_id='CVE-2021-44228' AND http_status=200;`

---

## Open Questions / Decisions Already Made

| Decision | Choice | Reason |
|----------|--------|--------|
| Content extractor | `trafilatura` | Best-in-class main-content extraction; handles most security sites |
| Robots.txt parser | `robotexclusionrulesparser` | Caching + full spec compliance vs. stdlib |
| Summarization model | `claude-haiku-4-5-20251001` | Cheapest Anthropic model; already in deps |
| Summarization threshold | 8,000 chars (~2k tokens) | Keeps direct embeds within `text-embedding-3-small` 8,191 token limit |
| Unknown domains | Scrape + embed (same as allowlist) | Conservative filtering via denylist is sufficient; unknown domains often contain signal |
| Microsoft MSRC | Scrape but rate-limit to 1 concurrent, 3s delay | MSRC is aggressive; back off hard |
| Re-scrape cadence | 30 days | Advisories are updated infrequently; weekly would add unnecessary cost |
| Embedding dimension | 1536 (unchanged) | Matches existing HNSW indexes |
