# STIG / IAVA Integration Plan

## Context

This project indexes CISA KEV and NIST NVD data to answer natural-language questions about
vulnerability exploitability and severity. A natural extension is DISA compliance data:

- **IAVAs** (Information Assurance Vulnerability Alerts) are DISA's mandatory-remediation orders
  that map directly to CVE IDs. Many KEV entries are also covered by IAVAs, but no single tool
  currently lets an analyst query "which of our unpatched CVEs carry mandatory DoD remediation
  orders?" in a conversational interface.
- **STIG findings** embed CVE and CWE references inside security configuration checks, enabling
  cross-references like "which STIG checks are affected by memory-corruption CVEs that are
  actively exploited?"
- **CCI → NIST 800-53 control mapping** enables RMF-level queries: "which security controls
  cover Log4Shell?"

All three data sources are publicly available from DISA and NIST without registration.

---

## Phased Approach

### Phase 1 — IAVA Data (MVP, highest value)

**Why first:** IAVA → CVE is a direct join on `cve_id`, so it immediately enriches the existing
`kev_vulnerabilities` and `nvd_vulnerabilities` tables with no schema changes to them.

#### New table: `iava_entries`

Add to `rag/database.py` `SCHEMA_SQL` (follow existing `CREATE TABLE IF NOT EXISTS` pattern):

```sql
CREATE TABLE IF NOT EXISTS iava_entries (
    iava_id        VARCHAR(20)  PRIMARY KEY,   -- e.g. "2024-A-0001"
    iava_type      VARCHAR(10)  NOT NULL,       -- IAVA | IAVB | IAVT
    title          TEXT         NOT NULL,
    severity       VARCHAR(20),                 -- Critical | High | Medium
    release_date   DATE,
    superseded_by  VARCHAR(20),                 -- nullable IAVA ID
    cve_ids        TEXT[]       NOT NULL,       -- array of CVE IDs
    description    TEXT,
    content        TEXT         NOT NULL,       -- text used for embedding
    embedding      vector(1536)
);
CREATE INDEX IF NOT EXISTS iava_embedding_idx
    ON iava_entries USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS iava_cve_ids_idx ON iava_entries USING gin (cve_ids);
```

The GIN index on `cve_ids` enables fast `cve_ids @> ARRAY['CVE-XXXX-YYYY']` lookups.

#### New loader: `scripts/load_iava.py`

Data source: DISA public IAVA listing at `https://www.ia.mil/Bulletins/`

The page is HTML; scrape with `lxml` or `BeautifulSoup`, or upgrade to the DISA Cyber Exchange
XML export if a machine-readable endpoint is found. Follow the `load_kev.py` async skeleton:

1. `fetch_iava_list()` → list of dicts with `iava_id`, `title`, `type`, `release_date`, CVE IDs
2. `build_content(iava)` → concatenate IAVA ID, type, title, severity, CVEs, description
3. `generate_embeddings_batch()` — reuse `rag/embeddings.py`
4. `upsert_records()` — `ON CONFLICT (iava_id) DO UPDATE`

CLI flags to match existing loaders:
- No flags → full reload
- `--since YYYY-MM-DD` → only IAVAs released after that date
- `--skip-embeddings` → data only

#### Update vector search

In `rag/vector_store.py` `PgVectorStore.search()`, add `iava_entries` to the `UNION ALL`:

```sql
SELECT content, embedding <=> $1 AS distance FROM iava_entries
```

This gives the `retrieve` tool semantic access to IAVA content alongside KEV/NVD.

#### New agent/MCP tool: `lookup_compliance`

Add to `rag/agent.py` and mirror in `mcp_server/server.py`:

```python
@rag_agent.tool
async def lookup_compliance(ctx: RunContext[Deps], cve_id: str) -> str:
    """Return compliance obligations for a CVE: IAVA mandatory-remediation orders and
    (once Phase 2 is complete) related STIG findings.

    Args:
        cve_id: CVE identifier, e.g. 'CVE-2021-44228'.
    """
```

Implementation: single SQL query joining `iava_entries` (via `cve_ids @> ARRAY[$1]`),
`kev_vulnerabilities`, and `nvd_vulnerabilities` on `cve_id`. Returns a formatted summary of
IAVA ID, type, severity, release date, and KEV due date if present.

Update `config.py` `system_prompt` to document when to use `lookup_compliance` vs `retrieve`
vs `query`.

---

### Phase 2 — STIG Findings

**Complexity note:** STIGs are distributed as individual XCCDF ZIP files per product/version
(e.g., "Windows Server 2022 STIG"). There is no single bulk download of all STIGs. The loader
must accept a directory of downloaded ZIP files from `https://public.cyber.mil/stigs/downloads/`.

#### New table: `stig_findings`

```sql
CREATE TABLE IF NOT EXISTS stig_findings (
    stig_finding_id  VARCHAR(50)  PRIMARY KEY,  -- e.g. "V-220903"
    rule_id          VARCHAR(100),               -- e.g. "SV-220903r991589_rule"
    stig_name        TEXT         NOT NULL,      -- e.g. "Windows Server 2022 STIG"
    stig_version     VARCHAR(20),
    severity         VARCHAR(10)  NOT NULL,      -- CAT I | CAT II | CAT III
    title            TEXT         NOT NULL,
    discussion       TEXT,
    check_content    TEXT,
    fix_text         TEXT,
    cve_ids          TEXT[],
    cci_ids          TEXT[],
    cwe_ids          TEXT[],
    content          TEXT         NOT NULL,
    embedding        vector(1536)
);
CREATE INDEX IF NOT EXISTS stig_embedding_idx
    ON stig_findings USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS stig_cve_ids_idx ON stig_findings USING gin (cve_ids);
CREATE INDEX IF NOT EXISTS stig_cwe_ids_idx ON stig_findings USING gin (cwe_ids);
```

#### New loader: `scripts/load_stig.py`

Input: directory path containing downloaded STIG ZIP files from `public.cyber.mil`.

Parse XCCDF XML with `xml.etree.ElementTree` or `lxml`. Each `<Group>` element contains:
- `<Rule id="...">` → `rule_id`
- `<title>` → finding title
- `<description>` → discussion
- `<check>/<check-content>` → check content
- `<fixtext>` → fix text
- `<ident system="http://cve.mitre.org">CVE-...</ident>` → CVE references
- `<ident system="http://iase.disa.mil/cci">CCI-...</ident>` → CCI references
- `severity` attribute on `<Rule>` → CAT I / CAT II / CAT III

CLI usage:
```
uv run python scripts/load_stig.py --stig-dir ./data/stigs/
uv run python scripts/load_stig.py --stig-dir ./data/stigs/ --skip-embeddings
```

Use the bulk temp-table upsert pattern from `scripts/load_nvd_full.py:257-269` since STIG
libraries can be large (tens of thousands of findings across all STIGs).

Add `stig_findings` to the `UNION ALL` in `PgVectorStore.search()` and extend
`lookup_compliance` to also join `stig_findings` on `cve_ids`.

---

### Phase 3 — CCI / NIST 800-53 Control Mapping

**Why:** Closes the RMF loop: CVE → IAVA → STIG finding → CCI → NIST 800-53 control.
Enables "which security controls cover this CVE?" queries.

Data source: `https://public.cyber.mil/stigs/cci/` — XML download, no auth required.

#### New table: `cci_control_map`

```sql
CREATE TABLE IF NOT EXISTS cci_control_map (
    cci_id       VARCHAR(20)  PRIMARY KEY,   -- e.g. "CCI-000001"
    control_id   VARCHAR(20),                -- e.g. "AC-2"
    control_name TEXT,
    definition   TEXT
);
```

No embedding needed — reference lookup table, same pattern as `cwe_definitions`.

#### New loader: `scripts/load_cci.py`

Single HTTP download of the CCI XML, parse with `xml.etree.ElementTree`, upsert with
per-row `ON CONFLICT (cci_id) DO UPDATE` (same pattern as `scripts/load_cwe.py`).

---

## Files to Create / Modify

| Action | File |
|---|---|
| Create | `scripts/load_iava.py` |
| Create | `scripts/load_stig.py` |
| Create | `scripts/load_cci.py` |
| Modify | `rag/database.py` — add 3 new `CREATE TABLE` blocks + indexes to `SCHEMA_SQL` |
| Modify | `rag/vector_store.py` — add `iava_entries` and `stig_findings` to `UNION ALL` |
| Modify | `rag/agent.py` — add `lookup_compliance` tool |
| Modify | `mcp_server/server.py` — mirror `lookup_compliance` |
| Modify | `config.py` — update `system_prompt` to document new tool |
| Modify | `docs/data-loading.md` — add IAVA/STIG/CCI loader instructions |

---

## Verification

1. **Schema** — run `uv run chainlit run app.py` (triggers `init_db()`); confirm new tables
   with `\dt` and `\d iava_entries` in `psql`.

2. **IAVA loader** — `uv run python scripts/load_iava.py`; spot-check with:
   ```sql
   SELECT iava_id, title, cve_ids FROM iava_entries LIMIT 5;
   ```

3. **Cross-reference query** — in `psql`:
   ```sql
   SELECT i.iava_id, i.severity, k.due_date
   FROM iava_entries i
   JOIN kev_vulnerabilities k ON k.cve_id = ANY(i.cve_ids)
   LIMIT 10;
   ```

4. **Semantic search** — ask the chatbot "Are there mandatory DoD remediation orders for any
   actively exploited vulnerabilities?" — `retrieve` should surface IAVA content.

5. **`lookup_compliance` tool** — ask "What are the compliance obligations for
   CVE-2021-44228?" — the agent should call `lookup_compliance` and return IAVA details +
   KEV due date.

6. **MCP** — call `retrieve` via the MCP server to confirm IAVA embeddings appear in results.
