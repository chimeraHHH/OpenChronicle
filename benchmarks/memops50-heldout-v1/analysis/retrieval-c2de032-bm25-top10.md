# Unseen MemOps-50 production BM25 baseline

This report records the first evaluation of the frozen, source-disjoint
MemOps-50 held-out tier. The selection and its expected structure were
committed before this run. No retrieval setting was changed after observing
the held-out cases.

## Reproducibility

```text
OpenChronicle commit  c2de032ddf2bf13a1627e1534874fd407badbcdb
repository dirty      false
MemOps commit          312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35
manifest SHA-256       26404cbf01b75479bc2076d7426d91eab37d0f512e1ced3cf1f055c989c1f378
contract SHA-256       7f649fd2506eab4f7d825b83006fafca1d628c72245179b003b7bced1a899e4a
result SHA-256         79f08ec81e17bade2b52973b34a62e5d1e1ba15ca8542038f2e79c3daddb4b15
ranker                 production activity SQLite FTS5/BM25
top-k                  10
adjacent expansion     0
model calls            0
```

## Results

| Metric | Adjacent | Longitudinal |
|---|---:|---:|
| Provenance segment recall, macro | 0.980000 | 0.963333 |
| Provenance segment recall, micro | 0.990991 | 0.972973 |
| Provenance turn recall, macro | 0.980000 | 0.970000 |
| Provenance turn recall, micro | 0.993939 | 0.981818 |
| Complete-case recall | 0.980000 | 0.940000 |
| Mean reciprocal rank | 0.833333 | 0.833333 |
| Injected-distractor contamination | 0.000000 | 0.350305 |
| Injected distractor at rank one | 0.000000 | 0.080000 |
| Mean returned context characters | 12,854.54 | 79,677.40 |

Forty-nine of 50 queries required the production relaxed-OR fallback; one used
strict AND. The longitudinal tier missed at least one carrier in three cases:

- `B02_reflect.json / p3_state_transition`: one of two carriers;
- `A07_remember.json / p4_candidate_disambiguation`: its only carrier;
- `A07_reflect.json / p2_target_binding`: two of three carriers.

## Gate decision

Ten of eleven checks passed, including the clean-repository gate. The overall
gate failed only because longitudinal injected-distractor contamination was
`0.350305`, above the pre-registered maximum `0.20`. The development tier had
the same single failure (`0.372` contamination), while its recall and complete
case metrics were also high. This source-disjoint replication supports a narrow
conclusion: the lexical retriever generally finds the evidence, but top-10
context contains too many upstream-labelled distractors and is very large.

No production ranker change is promoted from this result alone. The next
experiment must be fixed against the development tier and evaluated once on a
newly isolated validation split, or must measure downstream answer use so a
lower distractor count is not mistaken for better product utility.
