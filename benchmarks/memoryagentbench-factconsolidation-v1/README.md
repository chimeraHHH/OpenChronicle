# MemoryAgentBench FactConsolidation SH 6K v1

This benchmark runs the public MemoryAgentBench
`Conflict_Resolution / factconsolidation_sh_6k` sample through
OpenChronicle's production review-first memory lifecycle. It is a pure-text,
deterministic conflict-resolution gate: 455 ordered statements update 294 fact
slots, followed by all 100 frozen questions.

The adapter intentionally does not install MemoryAgentBench's vendored agent
runtime and does not redistribute its dataset. Download the pinned Parquet file
from the [fixed Hugging Face revision](https://huggingface.co/datasets/ai-hyz/MemoryAgentBench/blob/7ea066982b140a19337e17e60d45d4076e042faf/data/Conflict_Resolution-00000-of-00001.parquet),
then run:

```bash
uv run --with pyarrow==21.0.0 \
  python scripts/run_memoryagentbench_factconsolidation.py \
  --parquet /absolute/path/to/Conflict_Resolution-00000-of-00001.parquet \
  --output /tmp/mab-factconsolidation-report.json
```

The loader rejects any file whose SHA-256 differs from the frozen manifest.
`pyarrow` is an evaluation-only transient dependency and is not added to the
product runtime.

## Variants and boundary

- `no_memory`: always returns an empty answer.
- `bm25_current_only`: retrieves only current approved benchmark entries, then
  requires the retrieved fact to match the requested predicate and subject.
- `typed_current_fact`: addresses the canonical current subject slot directly.

The fixture auto-approves every gold statement only inside a disposable local
root. Each statement still passes through production evidence, candidate,
approval, Markdown publication, supersede, FTS, and current-fact code paths.
The metric copies MemoryAgentBench's normalized substring exact match.

This is an official-data deterministic OpenChronicle adapter, not an official
MemoryAgentBench leaderboard run: it replaces the upstream reader/agent with
explicit diagnostic variants. Raw predictions and per-query outcomes remain in
the generated JSON for audit.

The first frozen result is recorded in
[analysis/openchronicle-2026-08-24.md](analysis/openchronicle-2026-08-24.md).
