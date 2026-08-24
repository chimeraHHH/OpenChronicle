# Empty-context abstention analysis

Date: 2026-08-09

## Parent result and question

The parent pilot compared reactive invocation, the production deterministic
assessor, and the full Suggestion Kernel on the fixed 16-case
`work-resumption-opportunity-v1` split. The full kernel initially emitted three
false positives: empty context, an already-resolved task, and an active
conversation.

This bounded analysis asked one question: should Work Resumption ever emit when
either side of the interruption has no verified timeline text?

## Slice contract

- Intervention: require at least one non-blank verified timeline entry on both
  sides of an otherwise eligible gap.
- Fixed conditions: fixture file, labels, variant definitions, score, policy,
  budget, cooldown, latency method, and Stage 2 metric gates.
- Execution envelope: local arm64 macOS host, CPython 3.11.15, isolated temporary
  roots, real SQLite/provenance/capture projections, three latency repetitions.
- Stop condition: rerun the unchanged metric contract once and stop whether the
  result improves, regresses, or is null.

## Result

| Variant | Precision | Recall | False positives | Evidence coverage | p95 decision latency |
|---|---:|---:|---:|---:|---:|
| Reactive | 1.00 | 0.50 | 0 | 1.00 | 1.320 ms |
| Heuristic | 0.20 | 1.00 | 8 | 0.90 | 0.012 ms |
| Kernel | 0.50 | 1.00 | 2 | 1.00 | 1.137 ms |

The intervention removed exactly the empty-context false positive without
changing either positive case. It improved Kernel precision from 0.40 to 0.50,
but the 0.85 precision gate still fails. The result narrows the product claim:
the Kernel enforces policy, budget, cooldown, evidence, and minimum-content
gates, but it does not yet know whether a task is resolved or whether the user
is in an active conversation.

The local raw report is `scratch/vida-suggestions-pilot.json` with SHA-256
`7a292a47561ea545d6e982f0c7f3be0c34c44bac1b41c811c750e84f8011c6e9`.
It records base commit `60e1642cd89572cba2c4f785680e3bb103d92096`,
`dirty=true`, and 37 status lines. It is therefore pilot evidence only and must
not be promoted as a clean accepted result.

## Comparability and route decision

Comparability is preserved: only the production assessor's minimum-content
condition changed; the dataset and metric contract did not. The same condition
necessarily affects both the heuristic comparator and the full kernel because
the latter composes the former.

This one-slice campaign is complete. No lexical “completed” detector or
app-name blacklist will be added merely to fit two fixtures. The next route is
an explicit product idea/experiment for continuity and active-conversation
signals, informed by external references and independently labeled data. Until
then, Work Resumption remains below its precision gate.

Formal baseline registration is also still blocked because the current tool
session has no `artifact.confirm_baseline` capability and the worktree is
dirty. The local reactive comparator is reproducible and
`trusted_with_caveats`, not formally accepted.
