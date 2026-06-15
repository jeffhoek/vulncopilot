# CISA KEV + NVD Vulnerability Research

Ask natural-language questions about known exploited vulnerabilities, CVSS scores, affected products, weakness types (CWE), and remediation timelines. A Pydantic AI agent answers using **semantic search** (pgvector embeddings) and **direct SQL** over the CISA KEV catalog and NIST NVD database — cross-referencing data that the source sites keep on separate pages.

## Example questions

- Which critical vulnerabilities are due for remediation this week?
- Show me CVEs affecting Microsoft products with CVSS score above 9
- Which vendors have the most known exploited vulnerabilities?
- Has CVE-2024-12345 been used in ransomware campaigns?
- What are the required actions for Apache vulnerabilities added in the last 30 days?
- How many days on average elapsed between NVD publication and CISA KEV addition?

## Data sources

- **[CISA KEV catalog](https://www.cisa.gov/known-exploited-vulnerabilities-catalog)** — ~1,500 known exploited vulnerabilities, with BOD 22-01 remediation due dates and ransomware-campaign flags
- **[NIST NVD](https://nvd.nist.gov/)** — CVSS scores, severity, and affected products, enriching each KEV entry
- **[MITRE CWE](https://cwe.mitre.org/)** — weakness taxonomy for resolving CWE identifiers to names

Data is refreshed by a scheduled ETL job — see the **[live ETL run history](/etl-stats)** to confirm freshness.

## Learn more

- **[GitHub repository](https://github.com/jeffhoek/chainlit-pydanticai-postgres)** — source, architecture, and full docs
- **[Why this vs. the NVD site?](https://github.com/jeffhoek/chainlit-pydanticai-postgres/blob/main/README.md#why-this-project)** — capability comparison
- **[MCP server](https://github.com/jeffhoek/chainlit-pydanticai-postgres/blob/main/docs/mcp-server.md)** — connect external agents to `retrieve` and `query`
- **[Data loading (ETL)](https://github.com/jeffhoek/chainlit-pydanticai-postgres/blob/main/docs/data-loading.md)** · **[NVD integration](https://github.com/jeffhoek/chainlit-pydanticai-postgres/blob/main/docs/nvd-integration.md)** · **[CWE integration](https://github.com/jeffhoek/chainlit-pydanticai-postgres/blob/main/docs/cwe-integration.md)**

---

*Built with [Pydantic AI](https://ai.pydantic.dev/) and [Chainlit](https://chainlit.io/), powered by Claude. Answers are generated from indexed public data and may contain errors — verify against the official CISA and NVD sources before acting.*
