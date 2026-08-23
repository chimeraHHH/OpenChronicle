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

The first clean production-lifecycle result is recorded in
[analysis/openchronicle-4bb887f.md](analysis/openchronicle-4bb887f.md). Its raw,
machine-readable report is
[`results/openchronicle-4bb887f.json`](results/openchronicle-4bb887f.json).
