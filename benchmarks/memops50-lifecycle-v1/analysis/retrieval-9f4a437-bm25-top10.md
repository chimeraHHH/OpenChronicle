# MemOps-50 Stage 4 activity retrieval: BM25 Top-10

## Verdict

The clean pre-registered gate fails on OpenChronicle commit
`9f4a4373d5b4da8e89c9c13e43983fb72eba41e8`: nine of ten substantive
retrieval checks pass, but injected-distractor contamination is `0.372`, above
the frozen maximum of `0.20`.

This is a context-purity failure, not a broad recall failure. Longitudinal gold
segment macro recall is `0.976667`, complete-case recall is `0.94`, and first
gold mean reciprocal rank is `0.865667`. The result therefore points toward a
smaller or staged context budget rather than a new memory store, automatic
decay, or automatic consolidation.

## Reproduction identity

- OpenChronicle commit:
  `9f4a4373d5b4da8e89c9c13e43983fb72eba41e8`
- repository state at evaluation start: clean, zero status lines
- MemOps commit:
  `312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35`
- manifest SHA-256:
  `5f362fea11b74097cba3fa3f4ed3d135ffd3329aa3232308671bdfc6b71e1c83`
- retrieval metric-contract SHA-256:
  `7f649fd2506eab4f7d825b83006fafca1d628c72245179b003b7bced1a899e4a`
- raw report SHA-256:
  `9a346ad4da43ca367f37fce6d27f1d6752db23facbdcaee6ca304a376d9aaa21`
- model calls: 0
- answer generation: disabled
- ranker: production activity SQLite FTS5/BM25
- unit: dataset-native conversation-segment proxy
- query mode: strict AND, relaxed OR only after zero results
- context budget: Top-10, no adjacent-event expansion

Before querying, the adapter verified 50 logical pairs, 150 Stage 2 segments,
2,500 Stage 4 segments, 150 exact evidence carriers, 285 injected-distractor
segments, 165 provenance items, 164 unique gold turns, and 110 gold segments.
It indexed only ordered dialogue text and queried only the selected question.

## Metrics

| Metric | Adjacent | Longitudinal | Change |
|---|---:|---:|---:|
| Gold segment recall, macro @10 | 1.000000 | 0.976667 | -0.023333 |
| Gold segment recall, micro @10 | 1.000000 | 0.972727 | -0.027273 |
| Gold turn recall, macro @10 | 1.000000 | 0.975333 | -0.024667 |
| Gold turn recall, micro @10 | 1.000000 | 0.975610 | -0.024390 |
| Complete-case recall @10 | 1.000000 | 0.940000 | -0.060000 |
| Mean reciprocal rank | 0.876667 | 0.865667 | -0.011000 |
| Empty-hit rate | 0.000000 | 0.000000 | 0.000000 |
| Injected-distractor contamination @10 | 0.000000 | 0.372000 | +0.372000 |
| Injected distractor at rank 1 | 0.000000 | 0.020000 | +0.020000 |
| Mean returned context characters | 13,082.14 | 79,019.16 | +65,937.02 |

All 50 queries used the production relaxed-OR fallback because strict AND
returned no segment. Every longitudinal case returned at least one injected
distractor, although only one of 50 ranked a distractor first. The upstream
label applies to the source scenario as a whole; it does not prove that every
distractor is relevant to the selected question, so `0.372` must not be
reported as a factual-answer error rate.

Three longitudinal cases did not retrieve every gold carrier:

| Source | Family | Retrieved gold carriers | Gold carriers |
|---|---|---:|---:|
| `A16_remember.json` | Remember | 1 | 2 |
| `F16_reflect.json` | Reflect | 2 | 3 |
| `E12_reflect.json` | Reflect | 2 | 3 |

The two reflect misses fit the expected multi-evidence difficulty: a smaller
budget must preserve diversity across independent support segments, not merely
truncate the list.

## Post-baseline budget diagnostic

After freezing the Top-10 result, a read-only K sweep on the same adapter found:

| K | Segment recall, macro | Complete cases | Distractor contamination | Mean context chars |
|---:|---:|---:|---:|---:|
| 3 | 0.816667 | 0.660000 | 0.126667 | 29,782.44 |
| 5 | 0.946667 | 0.880000 | 0.308000 | 44,409.92 |
| 10 | 0.976667 | 0.940000 | 0.372000 | 79,019.16 |
| 20 | 1.000000 | 1.000000 | 0.244000 | 148,497.04 |

This sweep is exploratory and does not replace or retroactively change the
pre-registered gate. Blindly changing Top-10 to Top-3 would satisfy purity but
would disproportionately damage Reflect (`0.666667` segment recall) and
TrajectoryOps (`0.800000`). The better product hypothesis is progressive
retrieval: begin with a small context, then expand or diversify only when the
question needs multiple independent evidence segments.

## Optimization boundary

No product ranking change is justified by this proxy alone. MemOps carrier
segments are much coarser than OpenChronicle's reducer-generated sub-task
events, and the full writer tool can request adjacent events. The next
evaluation should compare fixed Top-10 against a pre-registered progressive or
diversity-aware selector on held-out cases, then run answer generation under
the upstream rubric. Only a same-case improvement in recall, purity, and answer
quality should be promoted into the product path.
