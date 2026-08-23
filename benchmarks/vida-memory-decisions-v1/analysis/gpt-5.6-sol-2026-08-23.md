# Model memory-decision result — 2026-08-23

The clean native development run passes every frozen v1 gate. It uses
`codex_cli:gpt-5.6-sol` with reasoning effort `none`, binds commit
`7d8eea069929d51d158fd276eeb438d763e35617`, records `dirty=false`, and is
stored in `reports/vida-memory-decisions-2026-08-23.json`.

| Metric | Result | Gate |
|---|---:|---:|
| Parse success | 1.00 | ≥ 1.00 |
| Operation precision | 1.00 | ≥ 0.80 |
| Operation recall | 1.00 | ≥ 0.80 |
| Operation F1 | 1.00 | primary, reported |
| Target-binding accuracy | 1.00 | ≥ 0.80 |
| Value accuracy | 1.00 | ≥ 0.80 |
| Provenance support | 1.00 | ≥ 0.80 |
| No-operation accuracy | 1.00 | ≥ 1.00 |

All eight cases passed: six gold operations were detected with no false
positive or false negative, and all provider, parse, detection, binding, value,
and provenance failure counts were zero. Per-case wall time ranged from about
8.0 to 9.5 seconds.

The first dirty pilot already achieved perfect operation detection, binding,
evidence, and abstention, but exact whole-sentence value comparison marked
three valid paraphrases wrong. Before the clean run, the contract was changed
to frozen necessary phrase anchors for generated new values; current/old values
and evidence identities remain exact. The final anchors were not changed after
the clean run began.

This is a small synthetic product-native development result. Candidate target
ids are supplied to the model, and the evaluator does not execute predicted
operations. It does not establish performance on official MemOps, real user
activity, long distractor histories, or automatic forgetting. A fixed public
MemOps tier is still required.
