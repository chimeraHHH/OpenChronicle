# Supervised rewrite deterministic boundary gate (development)

Date: 2026-08-09  
Repository commit: `ff0ac99e665416fcb3855c2bc0889ed17c4a74f6`  
Branch: `agent/vida-integration`  
Worktree during run: clean

## Reproduction

```console
uv run python -m openchronicle.evaluation.resume_rewrite \
  --dataset benchmarks/vida-resume-rescue-v1/rewrite/cases.json \
  --contract benchmarks/vida-resume-rescue-v1/rewrite/metric_contract.json \
  --output /tmp/openchronicle-resume-rewrite-ff0ac99.json \
  --quiet
```

Report SHA-256: `f5476eecc94d5d6b0d2f6c3d62defd81fc2f5b20f979464b9d58d5a382fdb36c`  
Dataset SHA-256: `abaf891fda30070b5ac73ca9881259cdc2925b7ee449806d67bea1533a0ae8cb`  
Metric-contract SHA-256: `1ac27941629b2671a5161cbe02546da3b9668c0baeac2d060a6fcd5e140ae07d`  
Production template SHA-256: `3f6044810dfbcfe74083099aea434d4c97291690c1cf83ec034dcd99e4135edf`

## Result

The formal development gate passed all 40 frozen cases. The run exercised the
production model-output validator, minimized provider-input serializer,
disclosed no-tool provider call, durable generation/review CAS, and closed
desktop protocol.

| Metric | Result |
| --- | ---: |
| Case pass rate | 1.000 |
| Schema/action pass rate | 1.000 |
| Source-binding pass rate | 1.000 |
| Protected-atom rejection recall | 1.000 |
| Prompt-injection rejection rate | 1.000 |
| Stale-decision rejection rate | 1.000 |
| Verified proposal yield | 1.000 |
| Abstention handling rate | 1.000 |
| Injected provider failure rate | 0.500 |

The provider failure value is expected: one of the two provider-family cases
injects a timeout while the other injects malformed JSON. Both fail closed.

## Scope and limitations

This is synthetic adversarial development evidence, not a Vida benchmark. It
contains no Vida prompt/output, private user résumé, or hiring labels. Human
factual accuracy, preference over the exact baseline, and target usefulness
remain `not_run`. The result therefore supports only the closed safety and
consistency boundaries above; it does not support ATS, interview, offer, or
feature-equivalence claims.
