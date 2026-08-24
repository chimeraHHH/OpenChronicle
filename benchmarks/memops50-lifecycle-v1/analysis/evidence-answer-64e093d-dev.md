# Evidence-distillation development baseline at `64e093d`

This is the first complete development-only run of the frozen exact-turn
pipeline. It is not a held-out or validation result. The source-disjoint third
validation tier was not retrieved, distilled, answered, or judged.

## Reproduction

```bash
uv run python scripts/run_memops50_evidence_answer.py \
  --memops-root /tmp/openchronicle-memops \
  --manifest benchmarks/memops50-lifecycle-v1/json/manifest.json \
  --contract benchmarks/memops50-lifecycle-v1/json/evidence_answer_metric_contract.json \
  --output /tmp/memops50-evidence-answer-dev-64e093d.json \
  --quiet
```

The clean repository commit and pushed upstream were both
`64e093d1663975bdf76615661f72ae50ff50a334`. The report contains 50 logical
pairs and 100 setting-specific rows. Its SHA-256 is
`07faa45ef915840973c17a2ad9f2a935d77e11ccab08794ef2b9ce9090d947b3`.

## Result

| Metric | Adjacent | Longitudinal |
|---|---:|---:|
| Pipeline-valid rate | 0.960000 | 0.960000 |
| Candidate gold-turn recall @20 | 1.000000 | 1.000000 |
| Candidate complete-case recall @20 | 1.000000 | 1.000000 |
| Selector oracle efficiency | 0.636000 | 0.683667 |
| Selected gold-turn recall | 0.635333 | 0.678714 |
| Selected evidence precision | 0.632530 | 0.609890 |
| Budgeted-complete selection rate | 0.380000 | 0.360000 |
| Answer accuracy | 0.820000 | 0.760000 |
| Fully faithful answer rate | 0.960000 | 0.820000 |
| Citation entailment precision | 0.950000 | 0.917355 |
| Harmful-extra rate | 0.080000 | 0.060000 |
| Selected context reduction | 0.875751 | 0.988966 |

The longitudinal answer-accuracy drop is `0.06`; the fully-faithful-answer
drop is `0.14`. The longitudinal operation accuracies are Remember `0.60`,
Forget `0.80`, Update `1.00`, Reflect `0.50`, and TrajectoryOps `0.90`.

The run failed the aggregate gate. Adjacent had one distiller parse/contract
failure and one faithfulness-judge failure. Longitudinal had two
faithfulness-judge failures. No parse retry, provider fallback, best-of-N, or
rule answer was used.

## Diagnosis

Retrieval is not the development bottleneck: every gold turn was present in
the top-20 candidate pool in both settings. The loss occurs after recall.

- Two cases require seven unique gold turns, already exceeding the frozen
  five-turn ceiling. Other incomplete answers selected only two or three of the
  available gold turns and then omitted requested subparts or abstained.
- The selector prompt asks for the *smallest* sufficient set. In this benchmark
  that instruction underweights exhaustive lifecycle boundaries, retained
  neighbors, and multi-part operation traces.
- Reflect errors repeatedly fold tentative plans into confirmed preferences or
  omit explicit boundary/counterexample evidence. This is an answer-lifecycle
  error, not a missing-candidate error.
- The faithfulness parser has deliberate internal-consistency constraints, but
  the exact consistency table was not shown to the model. Two settings failed
  the same case and one additional longitudinal case failed at this interface.
- The max-five selector still chose injected distractor turns in the long
  setting (`0.076923` of selected turns), so merely increasing the budget is
  insufficient; the selector must prefer complete question coverage without
  filling unused budget.

## One permitted development calibration

The next contract revision may make one evidence-backed calibration before any
secondary qualification rerun:

1. raise the hard evidence ceiling from five to seven, the observed maximum;
   an eighth slot has no development evidence and would add noise risk;
2. replace “smallest set” with “cover every requested subpart and lifecycle
   boundary, then stop,” explicitly including retained neighbors,
   tentative/retracted branches, and counterexamples;
3. require the answer to cover every supported subpart, avoid whole-answer
   abstention when selected evidence supports a partial answer, and never turn
   a future/tentative plan into a confirmed state or preference;
4. state the exact faithfulness output-consistency table already enforced by
   the parser;
5. retain the same ranker, top-20 candidate pool, model identity, two settings,
   isolated correctness judge, applicability gates, and blind-validation
   ledger. Do not add graph retrieval, hosted storage, computer use, retries,
   or a fallback answer.

The development rerun must be archived even if it still fails. Only after that
single calibration is frozen and pushed may the already-observed secondary
qualification tier be run once. The third validation tier remains untouched.
