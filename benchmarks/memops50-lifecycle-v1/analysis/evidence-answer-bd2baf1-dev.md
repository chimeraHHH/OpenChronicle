# Evidence-answer calibrated development run (`bd2baf1`)

## Run identity

- Repository commit: `bd2baf1dc97f7cac3c1014c5898cfd8aaa7768a6`
- Repository state: clean, with zero ignored artifact status lines
- Dataset: 50 logical pairs, evaluated once in adjacent and longitudinal
  settings for 100 rows
- Upstream MemOps commit: `312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35`
- Provider: tool-disabled `codex_cli:gpt-5.6-sol`, reasoning effort `none`
- Raw report:
  `benchmarks/memops50-lifecycle-v1/results/evidence-answer-bd2baf1-dev.json`
- Raw report SHA-256:
  `95404ac44b964eda0b306f73a6a34e218929313f6719fe38d360d4ef11462f82`
- Contract changes relative to the first development run: evidence budget
  five to seven, coverage-first distillation, atomic/boundary-aware answering,
  an explicit faithfulness truth table, and the semantically corrected
  unexpected-empty-selection gate
- Gate verdict: **failed**

This is the one permitted prompt calibration on the development tier. The
result is archived regardless of failure. No retry, fallback, best-of-N, rule
answer, or second prompt calibration was used. The source-disjoint third
validation tier was not read or run.

## Metric comparison

| Metric | Adjacent first | Adjacent calibrated | Longitudinal first | Longitudinal calibrated |
|---|---:|---:|---:|---:|
| Pipeline valid rate | 0.96 | 0.98 | 0.96 | 1.00 |
| Candidate gold-turn recall | 1.000 | 1.000 | 1.000 | 1.000 |
| Selected gold-turn recall | 0.635 | 0.791 | 0.679 | 0.822 |
| Selected gold-segment recall | 0.690 | 0.860 | 0.720 | 0.873 |
| Budgeted-complete selection | 0.38 | 0.56 | 0.36 | 0.60 |
| Selected evidence precision | 0.633 | 0.440 | 0.610 | 0.413 |
| Mean selected turn count | 3.32 | 5.68 | 3.64 | 6.34 |
| Injected distractor turn share | 0.000 | 0.000 | 0.077 | 0.114 |
| Injected distractor case rate | 0.00 | 0.00 | 0.20 | 0.50 |
| Answer accuracy | 0.82 | 0.76 | 0.76 | 0.76 |
| Fully faithful answer rate | 0.96 | 0.86 | 0.82 | 0.90 |
| Citation entailment precision | 0.950 | 0.963 | 0.917 | 0.959 |
| Citation completeness | 1.000 | 0.963 | 0.944 | 0.973 |
| Harmful extra rate | 0.08 | 0.06 | 0.06 | 0.08 |
| Reflect precision | 0.50 | 0.70 | 0.40 | 0.70 |
| Reflect recall | 0.556 | 0.778 | 0.444 | 0.667 |
| Context reduction ratio | 0.876 | 0.780 | 0.989 | 0.980 |
| Unexpected empty selection | derived 0.02 | 0.02 | derived 0.00 | 0.00 |

The exact faithfulness truth table removed faithfulness schema failures. The
only invalid calibrated row was the adjacent `C11_remember:p1_operation_trace`
distillation response. It had seven candidate gold turns but produced an
invalid distiller shape. The legitimate negative Remember question remained a
correct empty selection and no longer failed the corrected gate.

## Diagnosis

The calibration improved evidence coverage but overcorrected into evidence
accumulation:

- 23/50 adjacent cases and 32/50 longitudinal cases selected the full seven
  turns. The budget became the common stopping condition rather than a rare
  allowance for the two development cases with more than five gold turns.
- Adjacent selection grew from 166 to 284 turns, but gold selections grew only
  from 105 to 125. Longitudinal selection grew from 182 to 317 turns, while
  gold selections grew only from 111 to 131; injected distractors grew from 14
  to 36 turns across 10 to 25 cases.
- Longitudinal selected gold-turn recall increased by 0.143, while precision
  fell by 0.197. Selected distractors reached 0.114 of turns and appeared in
  half of the longitudinal cases.
- Adjacent context reduction fell below its 0.90 gate to 0.780 even though
  adjacent candidates contain only three operation-local segments.
- Paired correctness did not support promotion. Adjacent had two previously
  wrong cases become correct but five previously correct cases become wrong.
  Longitudinal had five improvements and five regressions, leaving accuracy
  unchanged.
- The larger selection produced 10 adjacent and 13 longitudinal gains in
  budgeted completeness, but this did not translate into answer accuracy.
- Faithfulness improved longitudinally but regressed adjacent. Mean
  per-case faithfulness latency rose from about 28.6 s to 37.4 s adjacent and
  from 29.0 s to 44.6 s longitudinal because the answerer emitted more parts.

Operation accuracy after calibration was:

| Operation | Adjacent | Longitudinal |
|---|---:|---:|
| Remember | 0.80 | 0.80 |
| Forget | 0.80 | 0.90 |
| Update | 0.70 | 0.80 |
| Reflect | 0.70 | 0.60 |
| TrajectoryOps | 0.80 | 0.70 |

All leakage, over-forget, and stale-value error gates stayed at zero, but
answer accuracy, per-operation accuracy, selection completeness/recall,
selection precision/noise, harmful extras, reflection precision/recall, and
adjacent context reduction still failed one or more frozen gates.

## Decision

Do not accept the seven-turn coverage-first calibration as the active answer
path. It proves that missing candidate recall was not the problem and that a
single selector prompt cannot jointly optimize exhaustive lifecycle coverage
and noise control. Do not lower gates and do not perform another development
prompt calibration.

The previously frozen procedure still requires exactly one run on the
already-observed, source-disjoint qualification tier after this report is
archived and pushed. That run must use the exact `bd2baf1` implementation,
contract SHA-256
`1cb08172a5cc2cf484ef1c61495a986f2563b512c934e9777bc8ea5f703917ab`,
model, and gates. It is a directional cross-source confirmation, not a way to
rescue the failed development gates. Its result cannot authorize more prompt
tuning. The unobserved third validation tier remains untouched.

The next candidate, if pursued, must change the evidence-selection mechanism
rather than add prompt wording or slots. It must remain local and auditable,
preserve exact leaf-turn citations and lifecycle operations, use no computer
use or hosted memory, and be tested as a separately identified architecture
against the already archived development evidence before any third-tier run.
