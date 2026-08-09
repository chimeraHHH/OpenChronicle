# OC-Vida Prompt Rescue v1

This frozen development benchmark evaluates OpenChronicle's clean-room Prompt
Rescue contract. It is not a Vida benchmark and contains no Vida prompts,
outputs, private implementation details, or claimed Vida scores.

The 17 synthetic cases cover complete and ambiguous requests, explicit
target/audience/format constraints, Chinese text, prompt/schema injection,
secret-bearing input, requested external action, conflicting constraints, and
deterministically invalid inputs. The committed metric contract separates hard
safety gates from quality gates.

The default run evaluates a reproducible `raw_input` comparator. It deliberately
does no model call and normally fails the improvement and adversarial gates.
Provider results must be supplied as an explicit, complete corpus whose dataset,
model identity, provider location, template version/digest, case IDs, output,
and latency are recorded. A provider corpus is never silently synthesized or
graded by the same model under test.

```bash
uv run python -m openchronicle.evaluation.prompt_rescue \
  --dataset benchmarks/vida-prompt-rescue-v1/fixtures/cases.json \
  --contract benchmarks/vida-prompt-rescue-v1/json/metric_contract.json \
  --output scratch/vida-prompt-rescue-report.json
```

Add one or more `--corpus path/to/provider-results.json` arguments to compare
complete pre-recorded provider runs. Raw provider corpora and generated reports
belong under ignored `scratch/` until their provenance, cost, and run conditions
are reviewed for publication.
