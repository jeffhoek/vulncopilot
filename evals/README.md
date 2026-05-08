# Offline RAG Evals

A regression suite for the `rag_agent`. Runs every entry in
[`dataset.yaml`](dataset.yaml) through the agent against a frozen
Postgres+pgvector fixture, scores the answers with [Ragas](https://docs.ragas.io),
and writes results to `evals/results.json`.

The suite ships 15 golden questions (one per production action button)
scored on three Ragas metrics: `faithfulness`, `context_recall`, and
`answer_correctness`. Status by PR per [`plans/eval-framework.md`](../plans/eval-framework.md):

| | PR 1 | PR 2 | PR 3 |
|---|---|---|---|
| Dataset entries | 5 | **15 ✓** | 15 |
| Metrics | faithfulness | **+ context_recall, answer_correctness ✓** | same |
| Baselines | — | — | ≥3 `run-*.json` snapshots committed |
| Thresholds | advisory | **advisory ✓** | hard floors (`mean − 1σ`) |

The GitHub Actions workflow originally scoped as PR 3 is **deferred** —
revisited once the feature is merged to main. See
[`plans/eval-framework.md`](../plans/eval-framework.md) → "CI workflow —
deferred".

## Pieces

| File | What it does |
|---|---|
| [`dataset.yaml`](dataset.yaml) | Golden questions: query, intent, hand-authored ground truth |
| [`harness.py`](harness.py) | `run_query()` — invokes `rag_agent.run()`, extracts `retrieve` contexts and tool order from `result.all_messages()` |
| [`scoring.py`](scoring.py) | One-shot Ragas batch scorer; Claude judge via `EVAL_JUDGE_MODEL` |
| [`conftest.py`](conftest.py) | Pytest fixtures: `eval_pool` (asyncpg), `eval_seeded_pool` (loads JSONL), `eval_deps`, session-scoped `all_scores` that writes `results.json` |
| [`test_offline_evals.py`](test_offline_evals.py) | Parametrized over `dataset.yaml`; advisory mode (no thresholds yet) |
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

# 5. (Optional) Snapshot this run as a baseline for PR 3 thresholds.
#    See "Baselines" below.
```

If `EVAL_DATABASE_URL` is unset, the suite skips cleanly — useful as a guardrail
so a default `pytest` from repo root never accidentally hits a real DB.

## Baselines

PR 3's hard floors (`mean − 1σ` per metric) are derived from a small set of
clean local runs committed under `evals/baselines/`. After a clean run:

```bash
mkdir -p evals/baselines
cp evals/results.json evals/baselines/run-$(date +%Y%m%d-%H%M%S).json
git add evals/baselines/
```

- **Why commit each run:** the floors and their derivation must be auditable
  and reproducible. Anyone re-running the threshold script should land on
  the same numbers.
- **Why the full JSON, not a flattened summary:** when one entry drags a
  metric down, you'll want per-row detail to see *which* — without
  re-running the full suite.
- **Gitignore:** `evals/results.json` (the working file) stays gitignored;
  only the timestamped snapshots in `evals/baselines/` are tracked.

Aim for ≥3 snapshots from independent runs before deriving thresholds —
the LLM judge introduces enough noise that a single run isn't a stable
baseline.

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

The 15 dataset entries map 1:1 to the production action buttons in
`config.py` / `.env.example`. **When a button text changes, the matching
dataset entry's `query` field must change too** — otherwise the eval drifts
away from real user behavior. Concrete example: PR 2 renamed the
`"Anthropic Claude"` button to `"Anthropic Claude vulns"` (the bare entity
name was triggering the agent's intro response instead of vuln data); the
`anthropic_claude` dataset entry's `query` was updated in lockstep, and
the rename also touched `infra/modules/app-service.bicep` and
`k8s/configmap.yaml`.

## Interpreting scores

The harness folds output from **both** tools into `retrieved_contexts`:

- `retrieve` (semantic search) → each chunk becomes a context entry.
- `query` (SQL) → the formatted result table becomes a single context entry.

That means every metric below grades uniformly across SQL-driven and
semantic-search-driven answers.

### `faithfulness`

Of the claims in the agent's `response`, what fraction are supported by
`retrieved_contexts`?

- **High (>0.7):** answer is grounded in tool output.
- **Low (<0.4):** the agent makes claims the tool returns don't support —
  possible hallucination, or the agent volunteering external knowledge
  (URLs, examples) the data didn't include.
- **0.0 with `context_count: 0`:** the agent answered without calling any
  data tool (or every call returned empty / errored). Cross-check
  `tools_used` and `answer` in `results.json`.

### `context_recall`

Of the claims in `reference` (the `ground_truth` field in
[`dataset.yaml`](dataset.yaml)), what fraction appear in
`retrieved_contexts`? Catches **retrieval gaps** — the SQL query missed a
filter, the semantic search returned irrelevant chunks, the tool got
called with the wrong args.

- **High (>0.7):** the right data made it back from the tools.
- **Low (<0.4):** ground truth references CVEs / fields the tool calls
  never surfaced. Check `tools_used` and the SQL the agent generated.

Note: Ragas's `context_recall` reads `reference` (i.e. `ground_truth`),
**not** the `expected_cve_ids` field. That field is a human-maintained
recall hint useful for debugging or a future custom metric — populate it
where it's natural; don't contort it.

### `answer_correctness`

How factually + semantically close is `response` to `reference`? Doesn't
depend on `contexts`. Catches "the agent had the right data but reasoned
poorly" — wrong CVE attributed to the wrong product, severity flipped,
date wrong.

- **High (>0.7):** answer matches ground truth in substance.
- **Low (<0.4):** material disagreement with the hand-authored ground
  truth. Read both side by side before assuming the agent is wrong — the
  ground truth itself can be stale once the seed is regenerated.

### Tool path

The `tools_used` field in `results.json` shows the agent's actual tool
sequence (e.g. `["retrieve", "query"]`). The `expected_tool` field on
each dataset entry is advisory — no assertion in PR 2 — but a mismatch
is the first thing to look at when a row scores poorly.

## Known footguns

- **`pytest --collect-only` does not require API keys**, but a real run
  does — `rag.agent` is imported lazily inside fixtures so the agent
  isn't constructed until you actually need it.
- The seed file is the source of truth. `eval_db_seed.jsonl` includes
  1536-dim embeddings, so it's a few MB — that's expected.
- The judge LLM (`EVAL_JUDGE_MODEL`, default `claude-haiku-4-5`)
  occasionally produces noisy scores. `answer_correctness` tends to be
  the noisiest of the three because it combines factual and semantic
  similarity into a single number; if a single row's score fluctuates
  run-to-run, the judge is the likely culprit before the agent.
