# LongMemEval-V2 adapter

OpenChronicle includes a text-only backend for the official
[LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2) harness. It is an
external longitudinal evaluation adapter, not a dependency of the desktop
product and not a claim that the benchmark has already passed.

## Pinned evidence

| Component | Pinned identity |
|---|---|
| Official harness | `xiaowu0162/LongMemEval-V2@2cc8c540bdb87fe6761629b585e727e1c4704520` |
| Hugging Face dataset | `xiaowu0162/longmemeval-v2@f152293e235517d504809563c833d7190b8c713b` |
| License | Apache-2.0 |
| Public task surface | 451 questions, five memory abilities, web and enterprise domains, small and medium tiers |

The adapter does not copy upstream implementation code. The wrapper registers
an OpenChronicle backend at runtime and delegates question privacy, prompt
construction, reader calls, scoring, aggregation, and output formats to the
pinned official harness.

## What the adapter measures

For each upstream haystack, the backend receives only full trajectory objects.
It converts each state into a local text record containing trajectory identity,
goal, outcome, environment, URL, action, action-referenced accessibility
lines, agent observation, and the bounded accessibility tree. It then uses the
same local FastEmbed projection and BM25/vector RRF implementation as product
memory search. Retrieved states expand to a configurable neighboring-state
slice before being returned to the fixed reader.

The query method sees only question text and the optional question-image path
defined by the upstream `Memory` interface. It never receives question ID,
type, gold answer, evaluator configuration, or raw question record. The backend
ignores query images and returns text context only; the upstream reader may
still receive a question image directly. This is intentional because
OpenChronicle's product memory is text-only and does not include computer-use.

Each backend instance uses a private temporary database. It never opens or
changes `~/.openchronicle`, and an enabled embedding failure aborts the run
instead of silently becoming BM25-only. Saved benchmark memory contains the
isolated database plus a version/model/config manifest.

## Reproduction

Create the official Python 3.11 environment, pin the checkout, and install this
repository with the local semantic extra:

```bash
git clone https://github.com/xiaowu0162/LongMemEval-V2.git
git -C LongMemEval-V2 checkout 2cc8c540bdb87fe6761629b585e727e1c4704520
conda env create -f LongMemEval-V2/environment.yml
conda activate lme-v2-release
pip install -e '/absolute/path/to/OpenChronicle[semantic-memory]'
```

Download the exact public snapshot and run the official preparation and
validation steps:

```bash
python LongMemEval-V2/data/download_data.py \
  --revision f152293e235517d504809563c833d7190b8c713b \
  --data-root /absolute/path/to/longmemeval-v2-data
python LongMemEval-V2/data/prepare_data.py \
  --data-root /absolute/path/to/longmemeval-v2-data --mode symlink
python LongMemEval-V2/data/validate_data.py \
  --data-root /absolute/path/to/longmemeval-v2-data --tier small
```

The public trajectory JSONL is about 1.20 GB before screenshot bundles. Start
with one question, but expect the official harness to load the trajectory file:

```bash
python /absolute/path/to/OpenChronicle/scripts/run_longmemeval_v2.py \
  --upstream-root /absolute/path/to/LongMemEval-V2 \
  --data-root /absolute/path/to/longmemeval-v2-data \
  --domain web \
  --tier small \
  --method openchronicle \
  --limit 1 \
  --output-dir runs/openchronicle_web_small_smoke \
  --reader-model Qwen/Qwen3.5-9B \
  --reader-base-url http://localhost:8023/v1
```

A bounded preflight streams only the first public trajectory, indexes its real
101-state shape, runs retrieval, and emits no state content:

```bash
uv run --extra semantic-memory python scripts/run_longmemeval_v2_smoke.py
```

Remove `--limit 1`, then run both `web` and `enterprise` for a publishable tier
result. LongMemEval-V2's LLM-scored abstention/gotcha cases also require its
documented evaluator credentials. Preserve `aggregated_metrics.json`,
per-question outputs, run arguments, repository commit, dataset revision,
FastEmbed model/version, and host latency data together.

## Current interpretation boundary

The adapter is now executable and contract-tested against realistic trajectory
shape, save/load, blind-query behavior, and production local retrieval. A full
small-tier score is still pending the 1.20 GB dataset and fixed reader/evaluator
runtime. Until that clean run exists, the passing native v1 fixture remains the
only quantitative OpenChronicle memory claim.

The clean bounded smoke artifact is
`reports/longmemeval-v2-real-trajectory-smoke-2026-08-23.json`: commit
`872397a089a9a26245e5cac5aca0d1f9cd9215d0`, `dirty=false`, 101 real states,
1.442 s local incremental indexing, 9.607 ms retrieval, three text contexts,
and preserved source identity. Network download time is recorded separately.

The current vector stage performs exact local cosine scoring. Do not add ANN
infrastructure on intuition: use the official small-tier query latency and
memory-size evidence to decide whether exact scan is actually the next
bottleneck. Retrieval error analysis should separately label missed state,
wrong trajectory, stale/dynamic state, workflow ordering, gotcha, premise, and
appropriate abstention failures.
