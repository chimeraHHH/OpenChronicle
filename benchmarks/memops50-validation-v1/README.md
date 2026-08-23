# MemOps-50 source-disjoint validation tier

This directory freezes the third balanced 50-pair sample from the pinned
[MemOps](https://github.com/MemTensor/MemOps/tree/312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35)
revision. Every selected source file is disjoint from both
`memops50-lifecycle-v1` and `memops50-heldout-v1`, so the 150 cases across the
three tiers use 150 distinct source files.

The tier was frozen after the earlier held-out result had become an error
analysis set, but before defining or running the downstream evidence-selection
and answer-quality evaluation. It must remain unevaluated until that contract,
reader, prompts, metrics, and promotion rule are committed. Dataset structure
verification is allowed; retrieval or answer scores are not.

The selection uses seed `memops50-validation-v1` with the same exact operation,
difficulty, topic-family, and evaluation-type quotas as the first two tiers.
After excluding their 100 source files, the optimizer considered 1,506 logical
pairs from 303 files and selected at most one pair per file. The optimum is
unique; its exact integer objective and second-best objective are recorded in
`json/manifest.json`.

Candidate construction first reads each Stage 2 `answer` array and keeps the
first occurrence of each unique `question_pair_id`; full Stage 2/Stage 4 item
verification runs only after selection. This preserves the established
selection semantics despite an unrelated adjacent/longitudinal question drift
in an unselected upstream candidate.

Rebuild and verify the manifest without evaluating retrieval:

```bash
uv run python scripts/build_memops50_validation_manifest.py \
  --memops-root /path/to/MemOps
```

After the downstream contract is frozen, the retrieval adapter can accept both
source exclusions:

```bash
uv run python scripts/run_memops50_retrieval.py \
  --memops-root /path/to/MemOps \
  --manifest benchmarks/memops50-validation-v1/json/manifest.json \
  --exclusion benchmarks/memops50-lifecycle-v1/json/selected_pairs.json \
  --exclusion benchmarks/memops50-heldout-v1/json/selected_pairs.json
```

Do not run that command merely to inspect the frozen tier. The first scored run
is reserved for the pre-registered downstream validation.
