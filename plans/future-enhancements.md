# Future Enhancements

Potential improvements and feature additions for the vulnerability research
chatbot, organized by production priority. High-priority items signal that the
system is designed to operate reliably, securely, and measurably — not just
demo-able. Medium items are either actively in flight or deliver meaningful
engineering value once the core is solid. Nice-to-have items expand reach and
polish but are not the difference between a toy and a product.

## High Priority — Production-Readiness

### Role-Based Access Control *(plan: PR #28)*

Implement permission levels such as read-only analyst, power user, and admin
(who can trigger data loads or manage configuration), backed by OAuth so access
is tied to real identities rather than shared credentials.

### Alerting & Notifications

Subscribe to alerts when new KEV entries match specific criteria such as vendor,
product, or severity threshold.

### Evaluation Framework

Build a test suite of question/answer pairs to systematically measure and track
retrieval quality and agent accuracy over time. Approach in two phases:

- **Offline evals in-repo**: unit-test-style assertions using
  [Ragas](https://ragas.io/) or [autoevals](https://github.com/brainlid/autoevals)
  measuring context recall, answer correctness, and faithfulness via
  LLM-as-judge. Run in CI against a fixed golden dataset.
- **Online evals via Logfire**: once the offline baseline is established,
  sample production queries and score grounding and domain relevance directly
  in Logfire to catch regressions in live traffic.

### Automated ETL Scheduling

Run KEV and NVD data loaders on a recurring schedule (e.g., daily cron) so the
database stays current without manual intervention. Surface a data freshness
indicator in the UI and API responses (e.g., "KEV last synced: 4 hours ago") so
users can trust the currency of results without checking logs.

### Persistent Conversation History

Store chat history in the database so users can resume previous conversations
across sessions.

### Cost Tracking

Monitor LLM token usage and embedding API costs on a per-query basis to manage
operational expenses.

### User Feedback Loop

Let users rate responses (thumbs up/down) to build a signal for prompt tuning
and retrieval optimization.

## Medium Priority — In-Flight or High-Value

### OWASP Top 10 (2025) Integration

Ingest the OWASP Top 10:2025 web app risk categories as a curated taxonomy
layer that bridges existing CWE data to practitioner-facing remediation
guidance. Each of the 10 categories has rich prose (description, "How to
Prevent," example scenarios) and an OWASP-published list of mapped CWEs,
which lets the agent answer category-framed questions and surface mitigation
content the current schema lacks.

Key points:

- **Schema**: `owasp_top10_categories` (id, name, description, prevention,
  examples, url, list_type, embedding) + `owasp_cwe_mapping` (owasp_id,
  cwe_id). Mirrors KEV/NVD shape (embedded content) more than CWE
  (JOIN-only) because the prose is high-value for retrieval.
- **Sourcing**: pull from https://owasp.org/Top10/2025/ — 10 stable,
  well-structured pages, each with a "List of Mapped CWEs" section.
  Either (a) hand-curate a JSON in `data/` (simplest given 10 rows that
  change every ~3 years) or (b) parse the canonical markdown sources from
  the [OWASP/Top10 GitHub repo](https://github.com/OWASP/Top10) (structured,
  version-controlled, easy to re-run on new releases). Both are realistic;
  unlike the deprioritized reference-URL effort, this is 10 known cooperative
  pages, not thousands of unknown ones.
- **Bridge query path**: `OWASP category → CWE → CVE (NVD) → KEV (exploited?)`
  via JOINs on existing `cwes TEXT[]` columns.
- **Tool surface**: extend `retrieve()` to include OWASP rows alongside KEV/NVD
  so semantic queries naturally pull in prevention guidance. Update system
  prompt with example JOIN patterns.
- **Mapping precision**: start with OWASP's official CWE mappings (~248 CWEs
  total). Resist transitively expanding via CWE parent/child relationships in
  v1 — adds recall but editorializes past OWASP's framing.
- **Future extension**: same schema accommodates the OWASP Top 10 for LLM
  Applications (2025) via a `list_type` column. Worth adding once the web
  list pattern is proven; the LLM list is standalone (no meaningful CVE
  bridge) but is self-applicable to this RAG app and timely.

Example queries this unlocks:

- "How many actively exploited (KEV) CVEs fall under Broken Access Control?"
- "Which OWASP 2025 category has the most KEV entries in the last 90 days?"
- "For CVE-2024-XXXX, what does OWASP recommend for prevention?"
- "Summarize KEV trends grouped by OWASP category."
- "What is Software Supply Chain Failures and which recent CVEs are examples?"

This is the curated alternative to the broad reference URL scraping effort
below — same goal (surface remediation context next to CVE data) at a
fraction of the operational cost.

### STIG / IAVA Compliance Data *(plan: PR #48)*

Ingest DISA IAVA mandatory-remediation orders and STIG check findings,
cross-referenced with KEV/NVD CVEs and the CWE taxonomy.
See [stig-iava-integration.md](stig-iava-integration.md) for the full plan.

### Hybrid Search with BM25

Combine vector similarity with PostgreSQL full-text search (`tsvector`) to
improve keyword matching alongside semantic understanding.

### Reranking

Add a cross-encoder reranker after initial vector retrieval to improve result
relevance, especially for ambiguous or broad queries.

### Query Routing

Automatically determine whether a user question is best served by semantic
search, direct SQL, or a combination of both.

### Retrieval Scoring Beyond Vector Similarity

Weight retrieval results using domain-specific signals in addition to cosine
similarity, so that higher-priority vulnerabilities surface first regardless of
query phrasing:

- **KEV status** — known-exploited CVEs rank above non-KEV results at equal
  similarity
- **EPSS score** — once ingested (see Additional Data Sources), use exploitation
  likelihood as a retrieval weight
- **Recency** — `date_added` to KEV or NVD publish date as a decay factor
- **User feedback** — upvoted CVEs gain a small boost (ties into User Feedback
  Loop)
- **Top-K tuning** — as more datasets are added (STIG, EPSS, GitHub advisories),
  increase top-K and rely on Reranking to maintain precision

Implementable as a weighted scoring expression in pgvector alongside the
existing similarity query.

### REST API Endpoint

Provide a programmatic API for other security tools (SIEM, SOAR, ticketing
systems) to query the vulnerability knowledge base.

### Semantic Caching

Cache embeddings and responses for near-duplicate queries so repeated or similar
questions skip the LLM call entirely. Reduces latency and token costs
meaningfully in multi-user deployments, and complements Cost Tracking by
lowering the baseline spend.

## Nice-to-Have — Cool Features

### Reference URL Content Scraping *(deprioritized; PR #45 stalled)*

NVD stores up to ten `reference_urls` per CVE pointing to vendor advisories,
patch notes, proof-of-concept write-ups, and security blog posts. The original
intent was to scrape those pages into a dedicated `cve_references` table with
embeddings so RAG retrieval could surface remediation snippets alongside core
CVE data.

Deprioritized because scraping thousands of unknown pages turned out to be a
high-cost, low-signal effort — redirects, paywalls, dead links, robots.txt
constraints, and inconsistent content quality made the value uncertain. The
OWASP Top 10 (2025) integration above covers the core remediation-guidance
need at a tiny fraction of the operational complexity. Revisit only if a
specific question class proves OWASP + STIG/IAVA isn't enough.

If revisited, considerations:

- Filter low-value URLs at ingest time (dead links, paywalled pages, NVD
  self-referential links, social media)
- Summarize long pages with an LLM call before embedding to keep chunk quality
  high
- Respect `robots.txt` and rate-limit scraping to avoid being blocked by vendor
  sites
- Re-scrape on a schedule so content stays current as advisories are updated

### Multi-Agent Architecture

Introduce specialized agents for different tasks (triage, reporting, trend
analysis) orchestrated by a router agent that delegates based on query intent.
Only justified if Query Routing proves insufficient — smart dispatch within a
single agent should be the first attempt.

### Charting & Visualization Tool

Give the agent a tool to generate charts and graphs, such as vulnerabilities by
severity over time, top affected vendors, or ransomware campaign trends.

### Export Tool

Generate downloadable PDF or CSV reports from query results for sharing with
stakeholders who don't use the chatbot directly.

### Slack & Teams Integration

Expose the chatbot in messaging platforms so analysts can query vulnerability
data without leaving their primary communication tool. Downstream of the REST
API — build the API first and Slack/Teams become thin wrappers around it.

### Streaming Citations

Display source CVE IDs inline as the agent responds, linked directly to NVD and
KEV detail pages for verification.

### Software Inventory Matching

Allow users to upload a Software Bill of Materials (SBOM) or CPE list and
cross-reference it against known exploited vulnerabilities to identify exposure.

### Additional Data Sources

Ingest supplementary vulnerability intelligence such as:

- **EPSS scores** — Exploit Prediction Scoring System for likelihood of
  exploitation
- **GitHub Security Advisories** — coverage for open-source dependencies
- **Exploit-DB** — proof-of-concept exploit availability
- **Vendor-specific advisories** — Microsoft, Cisco, Adobe, etc.
