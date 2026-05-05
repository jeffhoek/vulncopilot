"""Ragas wrapper for the offline eval harness.

PR 1 ships `faithfulness` only; PR 2 will add `context_recall` and
`answer_correctness`. The judge LLM is configured via EVAL_JUDGE_MODEL
(default claude-haiku-4-5) and reuses ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ragas import evaluate
from ragas.dataset_schema import EvaluationDataset, SingleTurnSample
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import faithfulness

from evals.harness import EvalResult


@dataclass
class GoldenEntry:
    id: str
    query: str
    ground_truth: str
    intent: str = ""
    expected_tool: str = ""
    expected_cve_ids: list[str] | None = None
    notes: str = ""


METRICS = [faithfulness]
METRIC_NAMES = [m.name for m in METRICS]


def _build_judge():
    """Wrap a Claude chat model as a Ragas LLM."""
    from langchain_anthropic import ChatAnthropic

    model = os.environ.get("EVAL_JUDGE_MODEL", "claude-haiku-4-5")
    chat = ChatAnthropic(model=model, temperature=0)
    return LangchainLLMWrapper(chat)


def score_all(
    rows: list[tuple[GoldenEntry, EvalResult]],
) -> dict[str, dict[str, float]]:
    """Score every (entry, result) pair in a single Ragas batch call."""
    if not rows:
        return {}

    samples = [
        SingleTurnSample(
            user_input=entry.query,
            response=result.answer,
            retrieved_contexts=result.contexts or [""],
            reference=entry.ground_truth,
        )
        for entry, result in rows
    ]
    dataset = EvaluationDataset(samples=samples)
    judge = _build_judge()
    report = evaluate(dataset, metrics=METRICS, llm=judge)

    df = report.to_pandas()
    out: dict[str, dict[str, float]] = {}
    for i, (entry, _) in enumerate(rows):
        out[entry.id] = {
            name: (float(df[name].iloc[i]) if name in df.columns else float("nan")) for name in METRIC_NAMES
        }
    return out
