# Official MemOps adjacent stability v1 — 2026-08-24

Three clean `codex_cli:gpt-5.6-sol` runs bind OpenChronicle commit
`4b866f6eb8aafa36d26935940d993c37b854b0c5`, the pinned four-file official
MemOps adjacent smoke, reasoning effort `none`, and action capability `none`.
The aggregate report is
`reports/vida-memory-decisions-memops-stability-2026-08-24.json` with SHA-256
`5783f6dd4d294df9980920b3f2b7928514dac9115500e06cd534e2a61f38c506`.

The frozen v1 stability gate **failed**. All three underlying smoke gates
passed and their scalar metrics were identical:

| Metric | All three runs |
|---|---:|
| Operation recall | 1.000 |
| Operation precision | 0.964 |
| Operation F1 | 0.982 |
| False operations | 1 update |
| Exact provenance support | 0.963 |
| Provider / parse failures | 0 |

However, only one of four cases produced the exact same complete predicted
operation JSON in all three runs. The exact case-decision agreement rate was
0.250, below the frozen 0.500 gate, and all three whole-run signatures were
different.

Inspection shows three distinct layers of stability:

- operation type/target/order was identical for every case and run (the same
  structural SHA-256 `24ff43af…`);
- exact type/target/evidence sets were identical for B02, C26, and C29, but the
  reflect evidence set varied in E12;
- free-text `new_value`/`old_value` paraphrases varied in B02, C29, and E12.

Therefore the failed v1 gate mixes structural memory decisions, provenance-set
selection, and harmless surface paraphrase into one byte-level signature. The
failure is retained as evidence. A follow-up contract should report and gate
these layers separately; it must not rewrite this v1 result as a pass.

As before, this is a fixed official-data adapter smoke, not the full MemOps
question/distractor/judge pipeline or a leaderboard result.
