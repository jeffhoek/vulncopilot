# Competitive Analysis

## Positioning

There is a spectrum of tools that overlap with pieces of what this project does, but nothing that assembles the full combination in a single open-source, self-hosted repository. The honest verdict: this project occupies a niche that is not well served anywhere.

## Competitive Landscape

### Open-Source MCP Servers for CVE Data

The closest neighbors on GitHub are pure MCP servers that fan out HTTPS calls
to many APIs and let the LLM correlate — no persistent store, no UI.

- **[mukul975/cve-mcp-server](https://github.com/mukul975/cve-mcp-server)** —
  27 tools across 21 APIs (NVD, EPSS, KEV, OSV, GHSA, MITRE ATT&CK, Shodan,
  GreyNoise, VirusTotal, AbuseIPDB, MalwareBazaar, ThreatFox, URLScan, OTX,
  Ransomwhere, CIRCL PDNS). Composite risk score over CVSS + EPSS + KEV +
  exploit-evidence with explanation. SQLite cache + audit log. Active.
- **[badchars/cve-mcp](https://github.com/badchars/cve-mcp)** — 23 tools over
  NVD, EPSS, KEV, GHSA, OSV. Same fan-out shape.
- **[jgamblin/CVE-MCP](https://github.com/jgamblin/CVE-MCP)** and
  **[rinadelph/CVE-MCP](https://github.com/rinadelph/CVE-MCP)** — narrower
  CVE-only MCP servers.

What they do well: data-source breadth, composite scoring with explanation,
SSRF guards on lookup tools. What they lack: persistent vector store (every
query re-fetches from APIs and pays rate limits), conversational UI, hybrid
SQL+RAG querying, deploy story beyond `pip install`.

### Threat-Intel Dashboards

These poll feeds and present aggregated views — SQL-shaped, no agent.

- **[infinri/A.S.E](https://github.com/infinri/A.S.E)** — polls KEV, NVD,
  GHSA, OSV, Packagist; filters against `composer.lock`; Slack-alerts only
  P0/P1. The inventory-matching + alert-fatigue framing is correct.
- **[jly-engineer/threat_intelligence_app](https://github.com/jly-engineer/threat_intelligence_app)** —
  MSP-flavored: monitored-software inventory + KEV/NVD/EPSS matching + daily
  PDF digest.
- **[moke-cloud/threat-radar](https://github.com/moke-cloud/threat-radar)**,
  **[fir3storm/sec-ticker](https://github.com/fir3storm/sec-ticker)** —
  dashboard variants over KEV/NVD/RSS feeds.

What they do well: software inventory matching, daily digest generation,
alert-quality discipline (only ship P0/P1). What they lack: any conversational
or semantic-search layer; no CWE join; no MCP exposure.

### Agentic Triage

- **[dguilliams3/mcp-agentic-security-escalation](https://github.com/dguilliams3/mcp-agentic-security-escalation)** —
  closest in architecture: LangChain ReAct agent + FAISS over KEV/NVD +
  SQLite for incident risk assessments + tool isolation in a separate MCP
  server. Different problem (incident correlation, not interactive Q&A) but
  validates the agent-with-isolated-tools shape.

### Commercial Vulnerability Platforms

**Tenable, Rapid7, Qualys, Snyk**

These platforms aggregate KEV and NVD data with dashboarded analytics, but they are scanners first. They do not support conversational, multi-signal queries like "rank overdue KEV vulnerabilities by CVSS score for my vendor list." They are CRUD UIs, not analysts. Beyond that, they are expensive SaaS products with no self-hosted option and no programmatic composability outside their own integrations.

### NVIDIA Agent Morpheus

The closest in spirit. Morpheus combines RAG and AI agents in an event-driven workflow that connects to multiple vulnerability databases and threat intelligence sources to assess CVE exploitability. But it is a batch pipeline built for container scanning — not a conversational chatbot. It is also heavily tied to the NVIDIA stack and is enterprise-grade in complexity. The blueprint is open source on GitHub, but it is not remotely "clone and run."

### IntellBot (2024)

An LLM-based security chatbot described in a 2024 academic paper that gathers information from diverse data sources to create a knowledge base covering vulnerabilities, recent attacks, and emerging threats. Conceptually close, but it exists only as a research paper with no deployable project. It is LangChain-based, targets a broader scope than KEV/NVD specifically, and uses no pgvector or SQL hybrid architecture.

### CVEdetails.com / CVEfind / CVEfeed

These sites aggregate CVE, KEV, and EPSS data with filtering, alerting, and APIs. But they are search-and-filter UIs: no semantic search, no conversational agent, no hybrid RAG+SQL query capability.

### Generic RAG + pgvector Tutorials

There are many "build a RAG chatbot with pgvector" guides online, but none that target KEV/NVD as the domain or implement the hybrid semantic+SQL dual-tool agent pattern this project uses.

## Differentiators

The combination of capabilities assembled here cannot be found in any single open-source repository:

| Capability | This project | Alternatives |
|---|---|---|
| KEV + NVD as a joint, JOINable dataset | Yes | Separate browsable databases on government sites |
| Hybrid agent tooling: semantic `retrieve` + direct SQL `query` | Yes | LangChain RAG projects typically use retrieval only |
| MITRE CWE weakness taxonomy integration | Yes | No other conversational tool resolves CWE IDs and enables weakness-level analytics |
| MCP server exposing tools to external agents | Yes | Unique in the vulnerability intelligence space |
| PydanticAI + Chainlit stack | Yes | Most RAG security tools use LangChain |
| Full multi-cloud deployment story | Yes — Docker Compose, EKS, Cloud Run, Azure App Service | Typically left as an exercise for the reader |

### The MCP Server Angle

The MCP server is the most forward-looking differentiator. No tool in the vulnerability intelligence space currently exposes its data layer as an MCP server, which means this project can be composed into larger agentic workflows — an AI coding assistant, a SOAR platform, or a custom orchestration pipeline can call `retrieve` and `query` directly without going through the Chainlit UI. As the MCP ecosystem grows, that composability becomes increasingly valuable.

### CWE Weakness Taxonomy

The `cwe_definitions` table resolves MITRE weakness taxonomy IDs to human-readable names and descriptions, enabling queries that no other conversational tool supports:

- "Which weakness types appear most often in actively exploited vulnerabilities?"
- "How many CRITICAL CVEs in our database involve memory corruption?"
- "Which vendors have the most vulnerabilities classified as injection weaknesses?"

Cross-referencing weakness taxonomy with KEV exploitation status and CVSS scores in a single natural-language query is a capability that the raw NVD site, commercial scanners, and existing open-source projects do not offer.

## What's Coming Next

### OAuth Authentication

Currently the app uses username/password authentication via Chainlit's built-in login. OAuth support is in development, enabling SSO integration with existing identity providers. This makes team deployment practical without managing per-user local credentials, and is a prerequisite for role-based access control.

### Reference URL Scraping: The Third Agent Tool

NVD stores up to ten reference URLs per CVE pointing to vendor advisories, patch notes, proof-of-concept write-ups, and security blog posts. Scraping those pages and indexing their content with pgvector embeddings would unlock a natural third agent tool alongside the existing two:

| Tool | What it answers |
|---|---|
| `retrieve` | Semantic search over CVE descriptions and metadata |
| `query` | Structured SQL over KEV, NVD, and CWE tables |
| `fetch_references` *(coming)* | Full context from vendor advisories, patch notes, and PoC write-ups |

This is the capability that turns the project from a database lookup tool into something that genuinely feels like having an analyst. Answering "what does the Ivanti patch actually require me to do?" or "is there a public exploit for this CVE?" from scraped reference content is not possible with NVD data alone — and is not something any competitor currently offers in a conversational interface.

## Roadmap

Beyond OAuth, the high-priority items informed by the comparables above are:

- **EPSS scores** *(now high priority)* — load FIRST.org's daily exploit
  prediction feed into Postgres alongside KEV and NVD. Fills the gap between
  CVSS impact and KEV confirmation with a near-term exploitation likelihood.
  Prerequisite for the composite risk score and EPSS-weighted retrieval.
- **Composite risk score tool** — a third agent tool returning a single
  explainable 0–100 score blending CVSS, EPSS, KEV status, ransomware use,
  and CWE class. Also exposed as a SQL view so the existing `query` tool can
  `ORDER BY risk_score DESC`. Every MCP-server competitor computes this with
  live API fan-out per CVE; pre-joining in Postgres ranks the whole dataset
  in milliseconds.
- **Software inventory matching** — let users paste a `composer.lock`,
  `package-lock.json`, `requirements.txt`, or SBOM and join it against
  KEV/NVD on CPE/PURL. Turns the project from "ask about CVEs" into "tell me
  what's wrong with my stack." Pairs with the risk score (rank what to patch
  first) and alerting (notify only on inventory matches).
- **Reference URL scraping** — see the Differentiators section above for the
  full motivation; covered in detail in `future-enhancements.md`.
- **Hybrid BM25 + vector search** — combine pgvector similarity with
  PostgreSQL `tsvector` full-text search for keyword matching on CVE IDs and
  vendor names.
- **Charting tool** — generate vulnerability trend graphs and severity
  breakdowns directly in the chat interface.
- **Alerting** — subscribe to notifications when new KEV entries match
  specific vendor, product, or severity criteria — filtered through user
  inventory once that lands.

See [future-enhancements.md](future-enhancements.md) for the full list.
