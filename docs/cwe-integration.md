# CWE Integration

## Overview

Common Weakness Enumeration (CWE) is a MITRE-maintained taxonomy of software and hardware weakness types. NVD assigns one or more CWE IDs to each CVE to classify the underlying weakness that was exploited.

Both `kev_vulnerabilities` and `nvd_vulnerabilities` store CWE IDs in a `cwes TEXT[]` column (e.g., `{CWE-79, CWE-89}`). Without names or descriptions those IDs are opaque. The `cwe_definitions` table resolves them to human-readable names and descriptions, enabling queries like:

- "Which weakness types appear most often in actively exploited vulnerabilities?"
- "Show me all KEV entries classified as injection weaknesses"
- "What is CWE-416 and which CVEs in our database are affected?"

## Data Source

MITRE publishes the full CWE list as a downloadable CSV:

**URL:** `https://cwe.mitre.org/data/csv/1000.csv.zip` (Research Concepts view, ~900 weaknesses)

This file is versioned with each CWE release (typically 2–3 times per year). Re-running `load_cwe.py` at any time will pull the latest version and upsert any changes.

## Database Schema

```sql
TABLE: cwe_definitions (
  cwe_id      VARCHAR(20) PRIMARY KEY,  -- e.g., 'CWE-79'
  name        TEXT NOT NULL,            -- 'Improper Neutralization of Input During Web Page Generation'
  abstraction VARCHAR(20),              -- Pillar | Class | Base | Variant | Compound
  description TEXT,                     -- short description from MITRE
  url         TEXT                      -- https://cwe.mitre.org/data/definitions/79.html
)
```

No `embedding` column — this is a join/lookup table only. It is joined to vulnerability tables on `cwe_id = ANY(cwes)`.

## Loading CWE Definitions

```bash
uv run python scripts/load_cwe.py
```

This script:
1. Downloads `1000.csv.zip` from `cwe.mitre.org`
2. Parses CWE-ID, Name, Weakness Abstraction, and Description
3. Upserts all rows into `cwe_definitions` (idempotent — safe to re-run)

No API key or authentication required. Expected output:

```
Downloading CWE definitions from https://cwe.mitre.org/data/csv/1000.csv.zip...
Loaded 933 CWE definitions.
```

## Example Queries

### Resolve CWE IDs to names for a specific CVE

```sql
SELECT n.cve_id, c.cwe_id, c.name, c.abstraction
FROM nvd_vulnerabilities n
JOIN cwe_definitions c ON c.cwe_id = ANY(n.cwes)
WHERE n.cve_id = 'CVE-2021-44228';
```

### Top weakness types in the KEV catalog

```sql
SELECT c.cwe_id, c.name, COUNT(*) AS cve_count
FROM kev_vulnerabilities k
JOIN cwe_definitions c ON c.cwe_id = ANY(k.cwes)
GROUP BY c.cwe_id, c.name
ORDER BY cve_count DESC
LIMIT 10;
```

### Top weakness types in the KEV catalog — cross-referenced with NVD severity

```sql
SELECT c.name, COUNT(*) AS count, AVG(n.cvss_v31_score) AS avg_cvss
FROM kev_vulnerabilities k
JOIN nvd_vulnerabilities n ON n.cve_id = k.cve_id
JOIN cwe_definitions c ON c.cwe_id = ANY(n.cwes)
WHERE n.cvss_v31_score IS NOT NULL
GROUP BY c.cwe_id, c.name
ORDER BY count DESC
LIMIT 10;
```

### All Base-level weaknesses related to injection

```sql
SELECT cwe_id, name, description
FROM cwe_definitions
WHERE abstraction = 'Base'
  AND (name ILIKE '%injection%' OR description ILIKE '%injection%');
```

### Chatbot natural language examples

These questions use the `query` tool with a JOIN on `cwe_definitions`:

- "Which CWE weakness types appear most often in KEV entries?"
- "What is CWE-416 and which actively exploited CVEs are affected?"
- "How many CRITICAL severity CVEs in our database involve memory corruption weaknesses?"
- "Show me the top 5 weakness categories by average CVSS score"
- "Which vendors have the most vulnerabilities classified as injection weaknesses?"
