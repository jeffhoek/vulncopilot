# CLAUDE.md

A RAG chatbot built with Pydantic AI and Chainlit that indexes CISA KEV and NIST NVD vulnerability data into PostgreSQL with pgvector embeddings, enabling natural language queries about security vulnerabilities via semantic search and direct SQL tools.

## Docs

- [Data loading (ETL)](docs/data-loading.md)
- [NVD integration](docs/nvd-integration.md)
- [CWE integration](docs/cwe-integration.md)
- [pgvector migration](docs/pgvector-migration.md)
- [Deployment: Azure](docs/deploy-azure-app-service.md), [GCP](docs/deploy-gcp-cloud-run.md), [EKS](docs/eks-runbook.md)
- [Langfuse observability](docs/langfuse-setup.md)
- [Logfire observability](docs/logfire-setup.md)
- [Supabase RBAC](docs/supabase-readonly-role.md)
- [MCP server](docs/mcp-server.md)
- [Future enhancements](docs/future-enhancements.md)

## Development Commands

```bash
# Start the chatbot
uv run chainlit run app.py

# Add a dependency
uv add <package-name>

# Sync dependencies
uv sync
```

## Requirements

- Python 3.12+
- uv package manager
