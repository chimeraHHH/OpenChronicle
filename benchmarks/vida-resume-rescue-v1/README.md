# OC-Vida Résumé Rescue v1

This frozen development benchmark evaluates OpenChronicle's clean-room Résumé
Rescue source and exact-projection contract. It is not a Vida benchmark and
contains no Vida prompts, outputs, implementation details, or claimed Vida
scores.

The 18 synthetic cases contain eight accepted and ten rejected inputs. They
cover evidence-backed selection, domain-evidence abstention, job-description
injection, confidential and shared-ownership warnings, multilingual text,
sparse profiles, unresolved conflicts, unknown or duplicated facts, wrong
sections, unselected mappings, non-excerpt requirements, closed-schema fields,
malformed provenance, credential-bearing URLs, and NUL text.

The committed grader separates factual/safety gates from targeting quality.
`candidate_supported` deliberately means only that the user mapped a displayed
fact to a requirement; `manual_mapping_unverified` prevents the deterministic
grader from pretending it has proved semantic entailment. Hiring fitness,
qualification, human preference, proprietary ATS behavior, and real-world
outcomes all remain outside this fixture.

Two deterministic variants run without a model:

- `base_profile` retains every non-conflicted reviewed fact and marks every job
  requirement as missing. It is a safe, untailored comparator.
- `deterministic_exact_projection` runs the production source validators and
  exact artifact builder twice, checking byte-equivalent semantic output.

```bash
uv run python -m openchronicle.evaluation.resume_rescue \
  --dataset benchmarks/vida-resume-rescue-v1/fixtures/cases.json \
  --contract benchmarks/vida-resume-rescue-v1/json/metric_contract.json \
  --output scratch/vida-resume-rescue-report.json
```

Generated reports belong under ignored `scratch/` until run provenance and the
fixture contract are reviewed. Later no-tool tailoring providers must add a
separately recorded corpus and human claim-level review; they cannot inherit
the exact projection's factual pass by similarity alone.
