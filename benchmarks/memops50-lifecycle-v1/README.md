# MemOps-50 reviewed-memory lifecycle tier

This benchmark freezes a balanced external diagnostic tier from
[MemOps](https://github.com/MemTensor/MemOps/tree/312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35)
without copying its generated conversations into this repository.

The tier contains 50 logical question pairs from 50 different source files.
Each pair has both `adjacent_operation` and `longitudinal_operation` question
specifications, so an answer method will eventually produce 100 rows. The
selection contains 10 scenarios each for Remember, Forget, Update, Reflect, and
TrajectoryOps; difficulty is split 25 medium / 25 hard, with fixed topic-family
and evaluation-type quotas.

## Reproduce the manifest

Clone and check out the pinned upstream revision:

```bash
git clone https://github.com/MemTensor/MemOps.git /path/to/MemOps
git -C /path/to/MemOps checkout 312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35
```

Verify the committed manifest:

```bash
uv run python scripts/build_memops50_manifest.py \
  --memops-root /path/to/MemOps
```

Use `--write` only when deliberately rebuilding the manifest from the same
pinned inputs. The verifier checks:

- upstream Git commit and MIT license bytes;
- the official runner, evaluator, and requirements hashes;
- all 403 Stage 2 and all 403 Stage 4 JSON file hashes;
- 50 unique `(source_file, question_pair_id)` identities;
- exact adjacent/longitudinal parity after removing only
  `evaluation_setting`;
- every question-spec and selected raw-file hash;
- operation, difficulty, topic-family, and evaluation-type quotas.

The selected pair list uses canonical sorted-key compact UTF-8 JSON with digest
`f1a2bd273122b990508504650082d8c5909d2805272ee28e10eeb53589f5b9c0`.
It is a fixed diagnostic subset, not a statistically unbiased replacement for
the complete MemOps benchmark. The upstream generated data and code are MIT
licensed; MemOps also attributes the longitudinal carrier conversations to
UltraChat.

## Production lifecycle oracle

Run the deterministic storage/lifecycle tier:

```bash
uv run python scripts/run_memops50_lifecycle.py \
  --memops-root /path/to/MemOps \
  --output /tmp/memops50-lifecycle.json
```

The runner takes the 281 `confirmed` Stage 2 operations as the decisions a user
has approved, excludes 30 `tentative` operations, and executes the confirmed
operations through OpenChronicle's production `MemoryService` in an isolated
temporary root. Every remember, update, reflect, and forget uses the normal
proposal/approval, typed-subject, supersession, provenance, and purge paths.

It reports:

- operation and per-operation checkpoint success;
- final current-state accuracy;
- stale-value rate after updates;
- current and historical leakage after forget;
- over-forget rate for unrelated current facts;
- durable provenance support;
- TrajectoryOps checkpoint accuracy.

This is explicitly a **gold-operation oracle**. It measures whether reviewed
memory can faithfully store and execute already-approved lifecycle decisions.
It does not measure whether the classifier inferred those operations from the
conversation, whether retrieval found the right evidence among longitudinal
distractors, or whether an answer model produced a correct response. Those
remain separate tiers so a model failure cannot be mislabeled as a storage
failure.

The original clean production-lifecycle result is recorded in
[analysis/openchronicle-4bb887f.md](analysis/openchronicle-4bb887f.md). After the
adapter's evidence contract was aligned with the classifier trigger semantics,
the clean regression result remained perfect; see
[analysis/openchronicle-3a70dfa-trigger-provenance.md](analysis/openchronicle-3a70dfa-trigger-provenance.md)
and its
[`machine-readable report`](results/openchronicle-3a70dfa-trigger-provenance.json).

## Operation-inference tier

The same 50 verified Stage 2 files are projected into the existing inert
memory-decision evaluator by `json/decision_manifest.json`. With the configured
classifier set to `codex_cli:gpt-5.6-sol`, run:

```bash
uv run python scripts/run_vida_memory_decisions.py \
  --memops-root /path/to/MemOps \
  --dataset benchmarks/memops50-lifecycle-v1/json/decision_manifest.json \
  --contract benchmarks/memops50-lifecycle-v1/json/decision_metric_contract.json \
  --output /tmp/memops50-decisions.json
```

This tier emits text-only JSON decisions and cannot publish or delete memory.
It evaluates 274 state-changing confirmed operations: 219 remember, 33 update,
10 forget, and 12 reflect. Thirty tentative operations are excluded. Seven
confirmed statements that only reaffirm the already-current value after a
tentative branch are also treated as evidence-only confirmations rather than
fake state changes. This normalization is specific to OpenChronicle's
state-changing decision contract and is not presented as the official MemOps
operation score.

The first clean Sol run and its scorer audit are recorded in
[analysis/decisions-c941895-gpt-5.6-sol-v1.md](analysis/decisions-c941895-gpt-5.6-sol-v1.md).
The raw report is
[`results/decisions-c941895-gpt-5.6-sol.json`](results/decisions-c941895-gpt-5.6-sol.json).
The deterministic corrected-provenance rescore is recorded in
[analysis/decisions-c941895-rescored-3a70dfa.md](analysis/decisions-c941895-rescored-3a70dfa.md)
with its
[`machine-readable report`](results/decisions-c941895-rescored-3a70dfa.json).
The rescore made zero model calls and preserves the original response hashes,
sizes, and latencies.

## Longitudinal evidence-retrieval tier

Run the frozen Stage 2 / Stage 4 paired retrieval diagnostic:

```bash
uv run python scripts/run_memops50_retrieval.py \
  --memops-root /path/to/MemOps \
  --output /tmp/memops50-retrieval.json
```

The verifier maps each Stage 2 provenance coordinate to its exact Stage 4
carrier using the pinned injection metadata, then proves that the complete
inserted dialogue slice is byte-for-byte equal. It also verifies these fixed
structural counts before retrieval starts:

```text
adjacent segments                 150
longitudinal segments            2500
longitudinal evidence carriers    150
longitudinal distractor segments  285
gold provenance items             165
unique gold turns                 164
gold segments                     110
```

Each source conversation segment is serialized as one isolated synthetic
activity event, containing only its dialogue text in original order. The query
is only the selected question. Answers, rubrics, gold state, provenance quotes,
target metadata, evidence flags, and distractor metadata are never indexed.
The evaluator then calls the production activity SQLite FTS5/BM25 search
primitive with the pre-registered `top_k=10`, strict-AND followed by relaxed-OR
only after zero hits, and no adjacent-event expansion.

This unit is a **dataset-native conversation-segment proxy**, not a claim that
MemOps segments equal OpenChronicle reducer events. Production events are
usually narrower semantic sub-tasks, and the writer's full tool can optionally
expand adjacent events. This tier deliberately holds both features out so it
can measure the lexical ranker against the external distractor corpus without
silently introducing a layout shortcut.

Primary metrics separately report macro and micro gold-turn/segment recall,
complete-case recall, first-gold MRR, injected-distractor contamination and
top-one rate, context size, query-mode counts, and adjacent-to-longitudinal
degradation. Turn recall is carrier-weighted—retrieving a carrier recovers all
gold turns within it—so it is not presented as independent turn-level ranking.
Injected distractors are an upstream label and are not necessarily relevant to
the selected question; their contamination rate is a context-purity diagnostic,
not a query-target error rate.

The first clean pre-registered result is recorded in
[analysis/retrieval-9f4a437-bm25-top10.md](analysis/retrieval-9f4a437-bm25-top10.md),
with its
[`machine-readable report`](results/retrieval-9f4a437-bm25-top10.json). It
passes nine of ten substantive retrieval checks but intentionally remains a
failed gate because injected-distractor contamination exceeds the frozen limit.
