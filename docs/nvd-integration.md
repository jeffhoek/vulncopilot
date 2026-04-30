# NVD Integration

## Overview

The chatbot cross-references two vulnerability datasets:

- **CISA KEV** (~1,500 records) — actively exploited vulnerabilities with remediation deadlines and ransomware campaign tracking
- **NIST NVD** (~1,500 records, scoped to KEV CVEs) — severity scores (CVSS), affected product versions (CPE), weakness classifications (CWE), and detailed descriptions

Both tables share `cve_id` as a key, enabling JOINs for cross-referenced analysis.

## ETL Scripts

There are two NVD loading strategies — see [data-loading.md](data-loading.md) for the full guide:

| Script | Scope | Records |
|---|---|---|
| `scripts/load_nvd.py` | NVD data for KEV CVEs only | ~1,500 |
| `scripts/load_nvd_full.py` | Entire NVD database | ~280,000 |

Both scripts share parsing and extraction logic via `scripts/nvd_utils.py`.

**Quick start** (KEV-scoped):

```bash
uv run python scripts/load_kev.py       # Load KEV first
uv run python scripts/load_nvd.py       # Enrich with NVD data
uv run chainlit run app.py              # Start the chatbot
```

**Full NVD** (for broader vulnerability research):

```bash
uv run python scripts/load_kev.py
uv run python scripts/load_nvd_full.py  # ~280k CVEs, supports --incremental, --skip-embeddings, --backfill-embeddings
uv run chainlit run app.py
```

See [plans/postgres-hosting-options.md](../plans/postgres-hosting-options.md) for storage sizing with the full NVD dataset.

## Database Schema

```sql
-- CISA Known Exploited Vulnerabilities
TABLE: kev_vulnerabilities (
  cve_id, vendor_project, product, vulnerability_name,
  short_description, required_action, notes,
  date_added, due_date, known_ransomware_campaign_use, cwes
)

-- NIST National Vulnerability Database
TABLE: nvd_vulnerabilities (
  cve_id, description,
  cvss_v31_score, cvss_v31_severity, cvss_v31_vector,
  cvss_v2_score, cvss_v2_severity,
  cwes, affected_products, reference_urls,
  published, last_modified
)
```

Both tables also have `content` (text for display) and `embedding` (vector for semantic search).

## Database Backup
Take a `pg_dump` backup using the following:
```
podman exec chainlit-pydanticai-rag-pg-pgvector-1 pg_dump -U postgresuser inventory > backup.sql
```

## Example Queries

### Semantic search (retrieve tool)

These questions use vector similarity search across both datasets:

- "Tell me about Log4j vulnerabilities"
- "What vulnerabilities involve remote code execution?"
- "Describe vulnerabilities related to buffer overflow in network services"
- "What are the most dangerous deserialization vulnerabilities?"
- "Find vulnerabilities related to authentication bypass"

### Structured queries — KEV only

These use SQL against the `kev_vulnerabilities` table:

- "How many CVEs have known ransomware campaigns?"
- "Which 5 vendors have the most KEV entries?"
- "List all KEV entries added in the last 30 days"
- "What products from Microsoft are in the KEV catalog?"
- "How many vulnerabilities were added to KEV in 2026?"

### Structured queries — NVD only

These use SQL against the `nvd_vulnerabilities` table:

- "How many CVEs have a CVSS score of 10.0?"
- "What is the average CVSS score across all vulnerabilities?"
- "List CVEs with CRITICAL severity published in 2026"
- "Which CWEs appear most frequently?"
- "Show the distribution of CVSS severity levels"

### CWE weakness queries — JOIN with cwe_definitions

These join `nvd_vulnerabilities` or `kev_vulnerabilities` with `cwe_definitions` to resolve IDs to names:

- "Which weakness types appear most often in KEV entries?"
- "What is CWE-79 and which CVEs in our database are affected?"
- "Show the top 10 weakness categories by average CVSS score"
- "How many actively exploited vulnerabilities involve injection weaknesses?"
- "Which vendors have the most CVEs classified as memory corruption weaknesses?"

### Cross-referenced queries — JOIN

These combine both tables using `cve_id` as the join key:

- "Which actively exploited CVEs have CRITICAL CVSS severity?"
- "What is the average CVSS score of KEV entries with ransomware campaigns?"
- "Show me KEV entries that have a CVSS score above 9.0, sorted by date added"
- "Which vendors have the most critical-severity actively exploited vulnerabilities?"
- "List CVEs that are both in KEV and have affected Apache products according to NVD"
- "Compare the average CVSS score of ransomware-linked vs non-ransomware KEV entries"
- "What are the top 10 most severe actively exploited vulnerabilities?"
- "Which KEV entries have the widest range of affected products?"

### Hybrid queries (semantic + SQL)

These may use both the retrieve tool and SQL:

- "Describe the most critical Apache vulnerabilities that are actively exploited"
- "What do the highest-severity ransomware-linked vulnerabilities have in common?"
- "Explain the impact of the most recent CRITICAL severity KEV entries"
