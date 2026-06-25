"""Programmatic invocation of rag_agent for eval scoring.

Walks the agent's message history to extract the strings returned by every
tool call — Ragas needs them as `retrieved_contexts` to grade faithfulness
and context_recall. Both `retrieve` (semantic search chunks) and `query`
(formatted SQL result tables) feed into the same contexts list, since
either is fair game for the agent to ground its answer in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.messages import ToolReturnPart

from rag.agent import Deps, rag_agent

# Markers in the `retrieve` tool's formatted output (see rag/agent.py:74).
_RETRIEVE_PREFIX = "Retrieved context:\n\n"
_RETRIEVE_SEP = "\n\n---\n\n"
_RETRIEVE_EMPTY = "No relevant context found."

# Markers in the `query` tool's output (see rag/agent.py:47-53).
_QUERY_EMPTY = "No results found."
_QUERY_ERROR_PREFIXES = ("Query error:", "Internal error executing query.")


@dataclass
class EvalResult:
    question: str
    answer: str
    contexts: list[str]
    tools_used: list[str]
    raw_messages: list[Any] = field(default_factory=list)


def _extract_contexts_and_tools(messages: list[Any]) -> tuple[list[str], list[str]]:
    """Pull tool-return strings into Ragas-shaped contexts.

    - `retrieve`: split the chunk-joined output back into individual chunks.
    - `query`: keep the formatted result table as a single context entry.
    - Empty results and error returns are skipped (no real evidence to ground in).
    """
    contexts: list[str] = []
    tools_used: list[str] = []
    for msg in messages:
        for part in getattr(msg, "parts", []) or []:
            if not isinstance(part, ToolReturnPart):
                continue
            tools_used.append(part.tool_name)
            content = part.content if isinstance(part.content, str) else str(part.content)
            if part.tool_name == "retrieve":
                if content == _RETRIEVE_EMPTY:
                    continue
                if content.startswith(_RETRIEVE_PREFIX):
                    body = content[len(_RETRIEVE_PREFIX) :]
                    contexts.extend(c for c in body.split(_RETRIEVE_SEP) if c.strip())
            elif part.tool_name == "query":
                if content == _QUERY_EMPTY:
                    continue
                if any(content.startswith(p) for p in _QUERY_ERROR_PREFIXES):
                    continue
                contexts.append(content)
    return contexts, tools_used


async def run_query(question: str, deps: Deps) -> EvalResult:
    result = await rag_agent.run(question, deps=deps, message_history=[])
    messages = result.all_messages()
    contexts, tools_used = _extract_contexts_and_tools(messages)
    return EvalResult(
        question=question,
        answer=result.output,
        contexts=contexts,
        tools_used=tools_used,
        raw_messages=messages,
    )
