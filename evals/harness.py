"""Programmatic invocation of rag_agent for eval scoring.

Walks the agent's message history to extract the strings returned by the
`retrieve` tool — Ragas needs them as `retrieved_contexts`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.messages import ToolReturnPart

from rag.agent import Deps, rag_agent

_RETRIEVE_PREFIX = "Retrieved context:\n\n"
_RETRIEVE_SEP = "\n\n---\n\n"


@dataclass
class EvalResult:
    question: str
    answer: str
    contexts: list[str]
    tools_used: list[str]
    raw_messages: list[Any] = field(default_factory=list)


def _extract_contexts_and_tools(messages: list[Any]) -> tuple[list[str], list[str]]:
    contexts: list[str] = []
    tools_used: list[str] = []
    for msg in messages:
        for part in getattr(msg, "parts", []) or []:
            if not isinstance(part, ToolReturnPart):
                continue
            tools_used.append(part.tool_name)
            if part.tool_name != "retrieve":
                continue
            content = part.content if isinstance(part.content, str) else str(part.content)
            if content.startswith(_RETRIEVE_PREFIX):
                body = content[len(_RETRIEVE_PREFIX) :]
                contexts.extend(c for c in body.split(_RETRIEVE_SEP) if c.strip())
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
