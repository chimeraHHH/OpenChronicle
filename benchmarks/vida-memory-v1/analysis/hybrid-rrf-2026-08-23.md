# Local hybrid memory retrieval result — 2026-08-23

The clean hybrid run passes every frozen v1 gate. It uses commit
`d73d074b2cc3d716962d82db65bd632b11a0fd97`, records `dirty=false`, and is
stored in `reports/vida-memory-hybrid-2026-08-23.json`.

| Metric | BM25 baseline | Hybrid RRF | Delta |
|---|---:|---:|---:|
| Case pass rate | 0.571429 | 1.000000 | +0.428571 |
| Recall@K | 0.500000 | 1.000000 | +0.500000 |
| Mean reciprocal rank | 0.500000 | 1.000000 | +0.500000 |
| Semantic/cross-language Recall@K | 0.000000 | 1.000000 | +1.000000 |
| Forbidden-hit rate | 0.000000 | 0.000000 | unchanged |
| Abstention accuracy | 1.000000 | 1.000000 | unchanged |
| Source-identity coverage | 1.000000 | 1.000000 | unchanged |
| Query latency p95 | 0.179417 ms | 3.039917 ms | +2.860500 ms |

Cold construction of the six-entry vector projection took 32.580709 ms. The
projection reuses unchanged vectors on later searches and remains disposable:
canonical Markdown is still the only durable source of truth.

The hybrid result closes the frozen BM25 gaps for semantic paraphrase,
Chinese-to-English retrieval, and historical opt-in while preserving current
knowledge, entity isolation, source identity, and abstention. The entity result
depends on a lexical-scope rule: when BM25 finds an entity/file anchor, vector
expansion cannot import a similarly worded fact from another Markdown file.
With no lexical hit, global semantic recall remains available.

This is a small deterministic engineering gate, not a population-level quality
claim. Cosine scoring is currently an exact scan of the local projection. A
larger corpus benchmark must measure scaling before an ANN index is justified;
LongMemEval-V2 remains the next external longitudinal adapter.
