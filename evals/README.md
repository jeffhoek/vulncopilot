# Offline RAG Evals

A regression suite for the `rag_agent`. Runs every entry in
[`dataset.yaml`](dataset.yaml) through the agent against a frozen
Postgres+pgvector fixture, scores the answers with [Ragas](https://docs.ragas.io),
and writes results to `evals/results.json`.

This is **PR 1** of the rollout in [`plans/eval-framework.md`](../plans/eval-framework.md):
5 golden questions, `faithfulness` only, no thresholds. Later PRs add the
remaining 10 questions, `context_recall` + `answer_correctness`, the CI
workflow, and hard score floors.

## Pieces

| File | What it does |
|---|---|
| [`dataset.yaml`](dataset.yaml) | Golden questions: query, intent, hand-authored ground truth |
| [`harness.py`](harness.py) | `run_query()` — invokes `rag_agent.run()`, extracts `retrieve` contexts and tool order from `result.all_messages()` |
| [`scoring.py`](scoring.py) | One-shot Ragas batch scorer; Claude judge via `EVAL_JUDGE_MODEL` |
| [`conftest.py`](conftest.py) | Pytest fixtures: `eval_pool` (asyncpg), `eval_seeded_pool` (loads JSONL), `eval_deps`, session-scoped `all_scores` that writes `results.json` |
| [`test_offline_evals.py`](test_offline_evals.py) | Parametrized over `dataset.yaml`; advisory mode in PR 1 |
| [`fixtures/build_seed.py`](fixtures/build_seed.py) | One-time generator that pulls real rows from a populated DB |
| [`fixtures/eval_db_seed.jsonl`](fixtures/eval_db_seed.jsonl) | Frozen snapshot — committed, source of truth |

## Running locally

```bash
# 1. Make sure ANTHROPIC_API_KEY and OPENAI_API_KEY are exported.
#    Easiest: `set -a; source .env; set +a` (or use direnv).

# 2. Spin up a clean Postgres+pgvector. Any port works; just match EVAL_DATABASE_URL.
podman run -d --name evals-pg -e POSTGRES_PASSWORD=postgres \
  -p 55433:5432 docker.io/pgvector/pgvector:pg16

# 3. Run the suite. The fixture seeds the DB on first use.
EVAL_DATABASE_URL="postgresql://postgres:postgres@localhost:55433/postgres" \
  uv run pytest evals/ -v

# 4. Inspect results.
cat evals/results.json | jq
```

If `EVAL_DATABASE_URL` is unset, the suite skips cleanly — useful as a guardrail
so a default `pytest` from repo root never accidentally hits a real DB.

## Adding a golden question

1. Add an entry to `dataset.yaml` (id, query, intent, expected_tool, ground_truth).
   Hand-write the `ground_truth` from data you can see in
   [`fixtures/eval_db_seed.jsonl`](fixtures/eval_db_seed.jsonl) — *not* by
   running the current agent. That would lock in current behavior as truth.
2. If the answer needs data not present in the seed:
   - Add the relevant CVE-IDs / CWE-IDs to `SEED_CVE_IDS` / `SEED_CWES` in
     [`fixtures/build_seed.py`](fixtures/build_seed.py).
   - Regenerate: `uv run python -m evals.fixtures.build_seed`
     (reads `PG_DATABASE_URL` from your `.env`, writes
     `evals/fixtures/eval_db_seed.jsonl`).
   - Commit the new JSONL.
3. Re-run the suite to confirm scores look reasonable.

## Action buttons ↔ dataset coupling

The 5 PR-1 entries are picked from the 15 production action buttons in
`config.py`. **When a button text changes, the matching dataset entry's
`query` field must change too** — otherwise the eval drifts away from real
user behavior. Example: if `"Anthropic Claude"` is updated to
`"Anthropic Claude vulns"` (because the bare name doesn't trigger a useful
agent response today), update the corresponding entry in `dataset.yaml`
when that button lands in the dataset (currently slated for PR 2).

## Interpreting scores (PR 1)

PR 1 ships **`faithfulness` only**. Ragas defines it as: of the claims in
the agent's answer, what fraction are supported by the retrieved contexts?

The harness folds output from **both** tools into `retrieved_contexts`:

- `retrieve` (semantic search) → each chunk becomes a context entry.
- `query` (SQL) → the formatted result table becomes a single context entry.

This means faithfulness grades every answer uniformly — whether the agent
reasoned over embedded documents or over a SQL result set, "did the answer
stick to what the tool actually returned?" is a meaningful question.

- **High score (>0.7):** answer is grounded in tool output.
- **Low score (<0.4):** the agent's answer makes claims the tool returns
  don't support — possible hallucination, or a metric/judge artifact.
- **0.0 with `context_count: 0`:** the agent answered without calling any
  data tool (or every tool call returned empty / errored). Cross-check
  with `tools_used` and `answer` in `results.json`.

The `tools_used` field in `results.json` shows the agent's tool path —
useful when diagnosing why a question went one way or the other.

## Known footguns

- **`pytest --collect-only` does not require API keys**, but a real run
  does — `rag.agent` is imported lazily inside fixtures so the agent
  isn't constructed until you actually need it.
- The seed file is the source of truth. `eval_db_seed.jsonl` includes
  1536-dim embeddings, so it's a few MB — that's expected.
- The judge LLM (`EVAL_JUDGE_MODEL`, default `claude-haiku-4-5`)
  occasionally produces noisy scores; if a single row's faithfulness
  fluctuates run-to-run, the judge is the likely culprit before the
  agent.
