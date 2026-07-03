import logging
from dataclasses import dataclass

import asyncpg
from openai import AsyncOpenAI
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.anthropic import AnthropicModelSettings

from config import settings
from rag.embeddings import generate_embedding
from rag.sql_utils import apply_row_limit, format_query_results, validate_sql
from rag.vector_store import PgVectorStore


@dataclass
class Deps:
    openai_client: AsyncOpenAI
    vector_store: PgVectorStore


rag_agent = Agent(
    settings.llm_model,
    deps_type=Deps,
    system_prompt=settings.system_prompt,
    model_settings=(AnthropicModelSettings(anthropic_effort=settings.llm_effort) if settings.llm_effort else None),
)


@rag_agent.tool
async def query(ctx: RunContext[Deps], sql: str) -> str:
    """Execute a read-only SQL SELECT query against the database.

    Args:
        sql: A SELECT statement to run against the database.

    Returns:
        Query results as a formatted table, or an error message.
    """
    error = validate_sql(sql)
    if error:
        return error

    sql = apply_row_limit(sql)

    try:
        async with ctx.deps.vector_store.pool.acquire() as conn:
            rows = await conn.fetch(sql)
    except asyncpg.PostgresError as e:
        return f"Query error: {e}"
    except Exception:
        logging.exception("Unexpected error in query tool")
        return "Internal error executing query."

    if not rows:
        return "No results found."

    return format_query_results(rows)


@rag_agent.tool
async def retrieve(ctx: RunContext[Deps], query: str) -> str:
    """Retrieve relevant context from the knowledge base.

    Args:
        query: The search query to find relevant documents.

    Returns:
        Relevant context from the knowledge base.
    """
    query_embedding = await generate_embedding(ctx.deps.openai_client, query)
    results = await ctx.deps.vector_store.search(query_embedding, top_k=settings.top_k)

    if not results:
        return "No relevant context found."

    context = "\n\n---\n\n".join(results)
    return f"Retrieved context:\n\n{context}"
