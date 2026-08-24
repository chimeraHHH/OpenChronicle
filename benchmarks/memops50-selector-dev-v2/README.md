# MemOps-50 selector development tier v2

This is a fourth, non-validation MemOps-50 slice for development of the local
evidence-coverage selector. Its 50 source files are disjoint from the original
development, held-out, and reserved validation tiers. The four slices therefore
use 200 distinct source files.

The reserved validation tier contributes only its frozen source-file IDs to the
exclusion set. This builder does not read validation questions, Stage 2
conversations, Stage 4 histories, gold provenance, manifest items, or retrieval
statistics. Excluded source IDs are checked before any upstream JSON file is
opened.

## Frozen selection

The selection seed is `memops50-selector-dev-v2`. After excluding 150 earlier
source IDs, 1,256 first-occurrence question-pair candidates remain in 253 source
files. SciPy 1.17.1 / HiGHS 1.12.0 selects at most one pair per source under the
same exact quotas as the earlier tiers:

- 10 each of Remember, Forget, Update, Reflect, and TrajectoryOps;
- 25 medium and 25 hard;
- topic families A/B/C/D/E/F = 9/9/8/8/8/8;
- evaluation types OperationTrace/TargetBinding/StateTransition/
  CandidateDisambiguation/OperationApplication/StateTrajectory =
  10/10/8/9/11/2.

For candidate `i`, the objective coefficient is the unsigned integer value of
`sha256(seed + NUL + source_file + NUL + question_pair_id)`. The exact optimum
is:

```text
195936203683711679311810569030144406157310332749992783216497488840492521689260
```

Re-solving with the no-good cut `sum(x[i] for i in selected) <= 49` gives:

```text
196406442688359200038718775623938066558473325142265044846795081734960173666635
```

The strict gap proves that the optimum is unique. Selected pairs are stored in
ascending rank-hash order. Their canonical digest is
`25f1972376448bac77e05b8e23b432dd64ca63a15609b9dd0d209aa9f7f3809c`.

## Rebuild and verify

Use the same pinned upstream checkout as the other MemOps tiers:

```bash
uv run python scripts/build_memops50_selector_dev_v2_manifest.py \
  --memops-root /path/to/MemOps
```

`--write` deliberately rebuilds `json/manifest.json`. Verification reads the
253 eligible Stage 2 files to reconstruct candidate identities and only the 50
newly selected Stage 4 files for final parity and byte hashes. It never opens
the 150 excluded Stage 2 or Stage 4 files.

For retrieval adapters, pass the source-only exclusion artifact:

```bash
uv run python scripts/run_memops50_retrieval.py \
  --memops-root /path/to/MemOps \
  --manifest benchmarks/memops50-selector-dev-v2/json/manifest.json \
  --exclusion benchmarks/memops50-selector-dev-v2/json/source_exclusions.json
```

This command is documentation, not a recorded result. No retrieval, LECS, or
model run is part of constructing this data slice.

## Development boundary

Once scored, this tier is observable development data. It may tune selector
architecture and selector hyperparameters only. A selector may receive the
question, candidate dialogue turns, and production retrieval order. It must not
receive expected answers, rubrics, gold state, gold provenance, operation or
difficulty labels, or distractor labels. Gold provenance remains scorer-only.

Results from this tier must not be used to tune answer, faithfulness, or
correctness prompts, and the tier must not be relabeled as held-out. The third
validation tier remains reserved and unchanged.
