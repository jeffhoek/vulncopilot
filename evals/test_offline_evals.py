"""Parametrized pytest entrypoint for offline RAG evals.

PR 3 wires hard floors via `check_thresholds()`. NaN scores are skipped
(judge noise, not regression). Per-entry scores live in
`evals/results.json`; failure messages include the metric and value, so
the suite stays quiet on success.
"""

from __future__ import annotations

import math

import pytest

from evals.conftest import load_dataset
from evals.scoring import METRIC_NAMES, GoldenEntry, check_thresholds


@pytest.mark.parametrize("entry", load_dataset(), ids=lambda e: e.id)
async def test_eval_entry(
    entry: GoldenEntry,
    all_scores: dict[str, dict[str, float]],
) -> None:
    scores = all_scores.get(entry.id, {})

    for name in METRIC_NAMES:
        value = scores.get(name)
        assert value is not None, f"missing score {name} for {entry.id}"
        if not math.isnan(value):
            assert 0.0 <= value <= 1.0, f"{name}={value} out of [0,1] for {entry.id}"

    failures = check_thresholds(entry.id, scores)
    assert not failures, "\n".join(
        f"{f.entry_id}: {f.metric}={f.value:.3f} below floor {f.floor:.2f}" for f in failures
    )
