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

### Reference URL Content Scraping *(PR #45 open — in progress)*

NVD stores up to ten `reference_urls` per CVE pointing to vendor advisories,
patch notes, proof-of-concept write-ups, and security blog posts. Scrape those
pages and store the extracted text in a dedicated `cve_references` table
(columns: `url`, `cve_id`, `title`, `scraped_text`, `embedding`). Index the
embeddings with pgvector so RAG retrieval can surface relevant reference
snippets alongside core CVE data. Considerations:

- Filter low-value URLs at ingest time (dead links, paywalled pages, NVD
  self-referential links, social media)
- Summarize long pages with an LLM call before embedding to keep chunk quality
  high
- Respect `robots.txt` and rate-limit scraping to avoid being blocked by vendor
  sites
- Re-scrape on a schedule so content stays current as advisories are updated

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
