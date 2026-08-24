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

- `no_memory`: always returns an empty answer. This is a sanity check, not a
  competitive baseline.
- `bm25_top1_current_only`: returns the first authorized current BM25 hit. It
  receives no parsed predicate or subject and therefore cannot use a slot
  oracle.
- `bm25_top20_plus_slot_oracle`: searches 20 current hits, then uses the exact
  parsed predicate and subject to select one. It is reported separately because
  this reranking information is not available to ordinary retrieval.
- `typed_slot_oracle`: parses the question into the benchmark's exact typed
  subject slot and addresses that slot directly. It is a schema and lifecycle
  diagnostic, not a natural-language retrieval score.

The fixture auto-approves every gold statement only inside a disposable local
root. Each statement still passes through production evidence, candidate,
approval, Markdown publication, supersede, FTS, and current-fact code paths.
The metric preserves MemoryAgentBench's normalized substring exact match, and
adds independent stale-value and contradiction checks. A prediction such as
`Paris and Lyon` can therefore be substring-correct while still failing the
contradiction-free metric.

The ingest gate separately records the real current-entry count, unique typed
slot count, and duplicate current slot count. Duplicate slots are never hidden
by dictionary overwrite and are not addressable by the typed oracle.

This is an official-data deterministic OpenChronicle adapter, not an official
MemoryAgentBench leaderboard run: it replaces the upstream reader/agent with
explicit diagnostic variants. Raw predictions and per-query outcomes remain in
the generated JSON for audit.

The metric contract is byte-bound by SHA-256 in both the adapter and manifest.
Formal runs also require a clean Git worktree, and the report records the exact
commit, manifest hash, contract hash, environment, per-query predictions, and
all gate checks.

The first frozen result is recorded in
[analysis/openchronicle-2026-08-24.md](analysis/openchronicle-2026-08-24.md).
Its complete machine-readable report is stored in
[`results/openchronicle-93fb60f.json`](results/openchronicle-93fb60f.json).
The preceding `bb0cf50` report is retained beside it as an audit trail for the
metric-edge correction.
