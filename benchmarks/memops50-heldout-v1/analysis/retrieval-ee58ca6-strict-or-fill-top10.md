# Strict-underfill OR-completion diagnostic

This is a clean post-baseline diagnostic on the already-observed held-out tier.
It is not a second blind held-out result. The production change fixes a general
retrieval defect: a single strict-AND hit previously prevented all relaxed-OR
candidates from being considered, even when the caller requested more results.
Strict hits now remain first and a deduplicated OR stream completes only the
underfilled page.

## Reproducibility

```text
OpenChronicle commit  ee58ca65df12a4d50ec8584546e815388de37dfd
repository dirty      false
MemOps commit          312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35
result SHA-256         47e9217309a4d06762d4cdcfd0cd608b3851a114ff667d075e79a146768a535b
ranker                 production activity SQLite FTS5/BM25
query mode             strict first, deduplicated OR completion
top-k                  10
adjacent expansion     0
model calls            0
```

## Paired result

| Longitudinal metric | Frozen baseline | Strict/OR fill | Delta |
|---|---:|---:|---:|
| Provenance segment recall, macro | 0.963333 | 0.983333 | +0.020000 |
| Provenance turn recall, macro | 0.970000 | 0.990000 | +0.020000 |
| Complete-case recall | 0.940000 | 0.960000 | +0.020000 |
| Mean reciprocal rank | 0.833333 | 0.840000 | +0.006667 |
| Injected-distractor contamination | 0.350305 | 0.356000 | +0.005695 |
| Injected distractor at rank one | 0.080000 | 0.080000 | 0.000000 |
| Returned non-gold share | 0.780041 | 0.782000 | +0.001959 |
| Mean context characters | 79,677.40 | 80,979.58 | +1,302.18 |

Forty-nine cases used relaxed OR after zero strict hits. One case used a strict
hit followed by OR completion. That completion recovered the only gold carrier
for `A07_remember.json / p4_candidate_disambiguation`, removing one complete
case failure. The other two misses remain multi-carrier Reflect/TargetBinding
cases.

The development tier is unchanged because all 50 development queries already
had zero strict hits and therefore used the relaxed stream. The overall gate
still fails only on injected-distractor contamination (`0.356 > 0.20`). The
change is promoted as a recall correctness fix, not a context-purity solution.
This observed held-out tier must no longer choose ranking parameters; a future
ranking or distillation candidate needs a new source-disjoint validation tier.
