# CLAUDE.md

A RAG chatbot built with Pydantic AI and Chainlit that indexes CISA KEV and NIST NVD vulnerability data into PostgreSQL with pgvector embeddings, enabling natural language queries about security vulnerabilities via semantic search and direct SQL tools.

## Development Commands

```bash
# Run the main script
uv run python main.py

# Add a dependency
uv add <package-name>

# Sync dependencies
uv sync
```

## Requirements

- Python 3.12+
- uv package manager
