# Fixed official MemOps adjacent smoke — 2026-08-23

The clean external-data run passes the deliberately bounded smoke gates. It
uses `codex_cli:gpt-5.6-sol` with reasoning effort `none`, binds OpenChronicle
commit `da13513ab4021339204dae4fa3f93c1a95aa5e26`, pins MemOps commit
`312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35`, and records `dirty=false`.
The full report is `reports/vida-memory-decisions-memops-2026-08-23.json`.

| Metric | Result | Smoke gate |
|---|---:|---:|
| Parse success | 1.000 | ≥ 1.000 |
| Operation precision | 0.931 | ≥ 0.700 |
| Operation recall | 1.000 | ≥ 0.700 |
| Operation F1 | 0.964 | primary, reported |
| Target-binding accuracy | 1.000 | ≥ 0.800 |
| Exact provenance support | 0.963 | ≥ 0.700 |
| Exact full-string value accuracy | 0.370 | diagnostic only |

The four official adjacent evidence files contain 27 confirmed gold operations:
23 remember, two update, one forget, and one reflect. The model recovered all
27 and added two unsupported updates: an elaboration of Marco's ring-shopping
plan and a later detail about Elaine's relationship status. Forget and reflect
were both detected. One operation cited a non-minimal evidence set.

Before the clean run, two prompt-development pilots exposed a useful failure
sequence. A final-state prompt missed both update transitions and achieved
operation F1 0.941 with provenance 0.625. An overcorrected full-trace prompt
recovered updates but produced nine spurious updates and fell to F1 0.825. The
frozen prompt now requires full chronological traces while defining update as
an explicit replacement rather than confirmation, elaboration, consequence,
or reuse. A final dirty pilot reached F1 1.000; the clean run's two false
updates show that decision stability still needs repeated-run measurement.

This is not a MemOps leaderboard result. It uses four adjacent evidence files,
not the long distractor injection, six question types, downstream answering,
official judge, or full dataset. The exact full-string value diagnostic is
intentionally not gated because the model commonly produces faithful
paraphrases. The upstream conversations remain external; only commit, file
names, and SHA-256 values are stored here.
