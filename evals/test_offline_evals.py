"""Parametrized pytest entrypoint for offline RAG evals.

PR 1 is advisory-only: scorers run, results recorded, no assertions. PR 4
will add `assert_thresholds()` once ≥3 baseline runs are available.
"""

from __future__ import annotations

import math

import pytest

from evals.conftest import load_dataset
from evals.scoring import METRIC_NAMES, GoldenEntry


@pytest.mark.parametrize("entry", load_dataset(), ids=lambda e: e.id)
async def test_eval_entry(
    entry: GoldenEntry,
    all_scores: dict[str, dict[str, float]],
) -> None:
    scores = all_scores.get(entry.id, {})
    # PR 1: log only, no thresholds. Surface the score in pytest -v output
    # so reviewers see it without opening results.json.
    summary = ", ".join(f"{name}={scores.get(name, float('nan')):.3f}" for name in METRIC_NAMES)
    print(f"\n{entry.id}: {summary}")

    # Sanity guard: scoring must produce a value (NaN allowed, but not missing key).
    for name in METRIC_NAMES:
        value = scores.get(name)
        assert value is not None, f"missing score {name} for {entry.id}"
        # math.isnan(None) raises — already guarded above.
        if not math.isnan(value):
            assert 0.0 <= value <= 1.0, f"{name}={value} out of [0,1] for {entry.id}"
