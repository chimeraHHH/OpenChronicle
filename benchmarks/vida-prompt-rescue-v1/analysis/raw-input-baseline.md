# Prompt Rescue raw-input baseline

Run date: **2026-08-09**  
Evaluation: `vida-prompt-rescue-v1`  
Dataset: `OC-Vida-Prompt-Rescue-v1` / `prompt-rescue-adversarial-dev-v1`

## Frozen inputs

- Dataset SHA-256:
  `c5178e68e7126d35c1063fa5696bd4d2fed6e0bd62f3fc0c6afa6f2a35643d4b`
- Metric-contract SHA-256:
  `bc7a6e9200ca9a4c6832d6b40830c2a6fac8d471a6fb11c252dcbf6870486905`
- Cases: 17 total; 12 expected accepted and 5 expected rejected.
- Comparator: `raw_input_no_model`; no provider or network call.
- The generated scratch report is intentionally untracked until a complete
  provider run is reviewed for provenance and cost.

## Result

| Metric | Raw input |
|---|---:|
| Case pass rate | 0.294118 |
| Admission accuracy | 1.000000 |
| Valid acceptance rate | 1.000000 |
| Invalid rejection rate | 1.000000 |
| Schema-valid rate | 1.000000 |
| Intent preservation | 0.666667 |
| Constraint preservation | 0.000000 |
| Missing-context behavior | 0.666667 |
| Material change | 0.000000 |
| Forbidden-output rate | 0.333333 |
| Secret-echo rate | 0.083333 |
| Injection-override rate | 0.250000 |
| Unsupported-assumption rate | 0.000000 |
| Action-capability violations | 0 |

The comparator fails the overall gate. Passing admission and schema checks is
not sufficient: copying raw text omits separately declared constraints and
missing-context questions, makes no material improvement, and repeats attack
or secret-bearing substrings. This is the intended negative control.

## Decision

- Retain the raw-input comparator as the reproducible lower bound.
- Do not claim Prompt Rescue quality or Vida parity from implementation tests.
- Next, record complete provider corpora against the unchanged case and metric
  files. Provider/template identity and latency are mandatory; results from
  different digests are not comparable.
- Keep deterministic safety gates authoritative. Any later blinded human or
  model-judge preference study is an additional quality signal, not a waiver.
