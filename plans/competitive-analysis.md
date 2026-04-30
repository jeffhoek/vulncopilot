# Competitive Analysis

## Positioning

There is a spectrum of tools that overlap with pieces of what this project does, but nothing that assembles the full combination in a single open-source, self-hosted repository. The honest verdict: this project occupies a niche that is not well served anywhere.

## Competitive Landscape

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

Beyond OAuth and reference scraping, planned enhancements include:

- **EPSS scores** — integrate Exploit Prediction Scoring System likelihood-of-exploitation signals alongside CVSS for better triage prioritization
- **Hybrid BM25 + vector search** — combine pgvector similarity with PostgreSQL `tsvector` full-text search for improved keyword matching on CVE IDs and vendor names
- **Multi-agent architecture** — specialized agents for triage, reporting, and trend analysis orchestrated by a router that delegates based on query intent
- **SBOM / CPE matching** — cross-reference a software bill of materials against KEV to identify exposure in a specific environment
- **Charting tool** — generate vulnerability trend graphs and severity breakdowns directly in the chat interface
- **Alerting** — subscribe to notifications when new KEV entries match specific vendor, product, or severity criteria

See [future-enhancements.md](future-enhancements.md) for the full list.
