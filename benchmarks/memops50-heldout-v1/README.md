# MemOps-50 unseen longitudinal retrieval tier

This tier freezes a second 50-pair sample from the pinned
[MemOps](https://github.com/MemTensor/MemOps/tree/312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35)
revision. It is held out from the first retrieval baseline: all 50 source files
are disjoint from `memops50-lifecycle-v1`, while operation, difficulty, topic,
and evaluation-type quotas are identical.

The selection was fixed before evaluating retrieval changes. An exact
SciPy/HiGHS integer optimization over 1,756 eligible pairs from 353 remaining
source files selected one pair per file using seed `memops50-heldout-v1`. The
objective optimum was unique; its complete integer value and the second-best
value are recorded in `json/manifest.json`.

Rebuild and verify the manifest:

```bash
uv run python scripts/build_memops50_heldout_manifest.py \
  --memops-root /path/to/MemOps
```

Run the frozen production retrieval baseline:

```bash
uv run python scripts/run_memops50_retrieval.py \
  --memops-root /path/to/MemOps \
  --manifest benchmarks/memops50-heldout-v1/json/manifest.json \
  --exclusion benchmarks/memops50-lifecycle-v1/json/selected_pairs.json \
  --output /tmp/memops50-heldout-retrieval.json
```

The evaluator indexes dialogue text only, maps Stage 2 evidence coordinates to
the exact Stage 4 carriers, and calls OpenChronicle's production SQLite
FTS5/BM25 search. This remains a dataset-native conversation-segment proxy,
not an end-to-end answer-quality measurement.
