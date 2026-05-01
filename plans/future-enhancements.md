# Future Enhancements

Potential improvements and feature additions for the vulnerability research
chatbot.

## Data & Coverage

### Automated ETL Scheduling

Run KEV and NVD data loaders on a recurring schedule (e.g., daily cron) so
the database stays current without manual intervention.

### Additional Data Sources

Ingest supplementary vulnerability intelligence such as:

- **EPSS scores** — Exploit Prediction Scoring System for likelihood of
  exploitation
- **GitHub Security Advisories** — coverage for open-source dependencies
- **Exploit-DB** — proof-of-concept exploit availability
- **Vendor-specific advisories** — Microsoft, Cisco, Adobe, etc.
- **STIG / IAVA compliance data** — DISA IAVA mandatory-remediation orders and STIG check
  findings, cross-referenced with KEV/NVD CVEs and the CWE taxonomy.
  See [stig-iava-integration.md](stig-iava-integration.md) for the full plan.

### Reference URL Content Scraping

NVD stores up to ten `reference_urls` per CVE pointing to vendor advisories, patch notes,
proof-of-concept write-ups, and security blog posts. Scrape those pages and store the
extracted text in a dedicated `cve_references` table (columns: `url`, `cve_id`, `title`,
`scraped_text`, `embedding`). Index the embeddings with pgvector so RAG retrieval can
surface relevant reference snippets alongside core CVE data. Considerations:

- Filter low-value URLs at ingest time (dead links, paywalled pages, NVD self-referential
  links, social media)
- Summarize long pages with an LLM call before embedding to keep chunk quality high
- Respect `robots.txt` and rate-limit scraping to avoid being blocked by vendor sites
- Re-scrape on a schedule so content stays current as advisories are updated

### Software Inventory Matching

Allow users to upload a Software Bill of Materials (SBOM) or CPE list and
cross-reference it against known exploited vulnerabilities to identify
exposure.

## Agent Capabilities

### Multi-Agent Architecture

Introduce specialized agents for different tasks (triage, reporting, trend
analysis) orchestrated by a router agent that delegates based on query
intent.

### Charting & Visualization Tool

Give the agent a tool to generate charts and graphs, such as vulnerabilities
by severity over time, top affected vendors, or ransomware campaign trends.

### Export Tool

Generate downloadable PDF or CSV reports from query results for sharing
with stakeholders who don't use the chatbot directly.

### Alerting & Notifications

Subscribe to alerts when new KEV entries match specific criteria such as
vendor, product, or severity threshold.

## RAG & Search Quality

### Reranking

Add a cross-encoder reranker after initial vector retrieval to improve
result relevance, especially for ambiguous or broad queries.

### Hybrid Search with BM25

Combine vector similarity with PostgreSQL full-text search (`tsvector`) to
improve keyword matching alongside semantic understanding.

### Query Routing

Automatically determine whether a user question is best served by semantic
search, direct SQL, or a combination of both.

### Evaluation Framework

Build a test suite of question/answer pairs to systematically measure and
track retrieval quality and agent accuracy over time.

## UX & Integration

### Persistent Conversation History

Store chat history in the database so users can resume previous
conversations across sessions.

### Role-Based Access Control

Implement permission levels such as read-only analyst, power user, and
admin (who can trigger data loads or manage configuration).

### Slack & Teams Integration

Expose the chatbot in messaging platforms so analysts can query
vulnerability data without leaving their primary communication tool.

### REST API Endpoint

Provide a programmatic API for other security tools (SIEM, SOAR, ticketing
systems) to query the vulnerability knowledge base.

### Streaming Citations

Display source CVE IDs inline as the agent responds, linked directly to
NVD and KEV detail pages for verification.

## Ops & Observability

### Usage Analytics Dashboard

Track the most common queries and usage patterns to guide data expansion
and prompt improvements.

### Cost Tracking

Monitor LLM token usage and embedding API costs on a per-query basis to
manage operational expenses.

### User Feedback Loop

Let users rate responses (thumbs up/down) to build a signal for prompt
tuning and retrieval optimization.
