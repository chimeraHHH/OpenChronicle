# Evidence-answer qualification (`1d84d4a`)

## Run identity

- Purpose: the single pre-registered qualification of the frozen
  seven-turn coverage-first candidate after its calibrated development run
- Repository commit: `1d84d4a11a7f747a6fc8a2ac026b6f20c5753193`
- Repository state: clean and equal to its pushed upstream
- Dataset: 50 logical pairs from 50 source files disjoint from development,
  evaluated in adjacent and longitudinal settings for 100 rows
- Dataset manifest SHA-256:
  `26404cbf01b75479bc2076d7426d91eab37d0f512e1ced3cf1f055c989c1f378`
- Contract SHA-256:
  `1cb08172a5cc2cf484ef1c61495a986f2563b512c934e9777bc8ea5f703917ab`
- Provider: tool-disabled `codex_cli:gpt-5.6-sol`, reasoning effort `none`
- Raw report:
  `benchmarks/memops50-heldout-v1/results/evidence-answer-1d84d4a-qualification.json`
- Raw report SHA-256:
  `9732e4391360dda6e99566094a3aa4b0a7eb71465e79017cc94b34b3e71ef11b`
- Gate verdict: **failed**; 17/31 checks passed

The report is the only evidence-answer run of this candidate on this tier. No
retry, fallback, best-of-N, rule answer, prompt change, threshold change, or
rerun was used. The source-disjoint third validation tier was not read or run.

## Qualification metrics

| Metric | Adjacent | Longitudinal | Longitudinal gate |
|---|---:|---:|---:|
| Pipeline valid rate | 0.98 | 0.98 | 1.00 |
| Candidate gold-turn recall | 1.000 | 0.995 | 0.98 |
| Candidate complete case recall | 1.00 | 0.98 | 0.94 |
| Selected gold-turn recall | 0.817 | 0.804 | 0.85 |
| Selected gold-segment recall | 0.853 | 0.840 | 0.90 |
| Selector oracle efficiency | 0.817 | 0.809 | 0.90 |
| Budgeted-complete selection | 0.58 | 0.58 | 0.80 |
| Selected evidence precision | 0.465 | 0.413 | 0.60 |
| Mean selected turn count | 5.68 | 6.30 | diagnostic |
| Injected distractor turn share | 0.000 | 0.130 | 0.05 max |
| Injected distractor case rate | 0.00 | 0.44 | diagnostic |
| Answer accuracy | 0.84 | 0.76 | 0.86 |
| Fully faithful answer rate | 0.84 | 0.84 | 0.90 |
| Citation entailment precision | 0.969 | 0.974 | 0.95 |
| Citation completeness | 0.949 | 0.951 | 0.90 |
| Harmful extra rate | 0.02 | 0.04 | 0.00 max |
| Forget leakage rate | 0.00 | 0.143 | 0.00 max |
| Reflect precision | 0.70 | 0.70 | 0.90 |
| Reflect recall | 0.286 | 0.143 | 0.90 |
| Context reduction ratio | 0.795 | 0.981 | 0.90 |
| Unexpected empty selection | 0.02 | 0.02 | 0.00 max |

Longitudinal operation accuracy was Remember 0.80, Forget 0.80, Update 0.80,
Reflect 0.70, and TrajectoryOps 0.70. The 0.08 adjacent-to-longitudinal
accuracy drop passed its 0.10 ceiling, but both absolute answer quality and the
selection gates failed.

## Cross-source replication

The qualification reproduces the calibrated development failure instead of
rescuing it:

- 24/50 adjacent and 34/50 longitudinal cases selected all seven turns.
- Longitudinal selected-evidence precision is again 0.413, effectively the
  same as calibrated development (0.413).
- Longitudinal injected-distractor share is 0.130, worse than calibrated
  development (0.114), and distractors appear in 22/50 cases.
- Longitudinal answer accuracy remains 0.76, exactly matching both development
  runs. Coverage growth therefore does not improve the primary outcome.
- Faithfulness is 0.84 in both settings. Citation-level precision remains high,
  showing that individual cited claims can be entailed while the whole answer
  still has missing, harmful, or lifecycle-incorrect content.
- One adjacent and one longitudinal row had a distillation schema failure.
  The longitudinal failure was a Forget case; a separate longitudinal Forget
  answer also leaked information under the correctness rubric.

## Frozen decision

Reject `memops50-evidence-distill-answer-v1` with the seven-turn
coverage-first generative selector. Do not lower its gates, change its prompt,
rerun qualification, or run it on `memops50-validation-v1`. The qualification
confirms that the failure is architectural: generation-based one-shot turn
selection trades recall for uncontrolled evidence accumulation and cannot keep
the required context purity.

The next experiment must have a new architecture identity and new contract.
The evidence-backed minimal candidate is a local extractive exact-turn
cross-encoder selector that replaces only the distillation call while keeping
the production top-20 retrieval, cited answerer, faithfulness judge, and
correctness judge fixed. It must use a new source-disjoint non-validation
development slice and a native, pre-registered score boundary; it must not tune
against this qualification report or consume the third validation tier.
