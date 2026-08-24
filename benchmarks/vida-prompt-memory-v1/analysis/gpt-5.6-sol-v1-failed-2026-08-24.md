# Reviewed-memory Prompt Rescue A/B — failed v1 baseline

The first configured run used `codex_cli:gpt-5.6-sol` with reasoning effort
`none`. Its machine-readable report is
`reports/vida-prompt-memory-v1-failed-2026-08-24.json`, SHA-256
`0f72b6000bec54d1e8299e5d4089e1e706782572cf83174496600bbe684a0a7a`.

| Variant | Current anchors | Applicable memory anchors | Forbidden hits | Action boundary |
|---|---:|---:|---:|---:|
| No reviewed memory | 0.556 | 0.333 | 0.000 | 1.000 |
| Reviewed memory | 0.778 | 1.000 | 0.000 | 1.000 |

Reviewed memory added 0.667 applicable-procedure anchor coverage and introduced
no stale, unrelated, injection, or action-bearing forbidden term. The full v1
gate nevertheless failed because its exact current-anchor threshold is 0.9.

The missed anchors expose a real auditability weakness rather than a stale
memory failure. The model respected all three constraints semantically but
paraphrased them:

- `Do not use bullet points` became `without bullet points`;
- `Prepare text only` was `using text only` in the no-memory variant;
- `Do not add topics` became `do not introduce, infer, or add ... topics`.

The frozen gate and failed report are retained unchanged. The next experiment
must change the production Prompt Rescue instruction to carry explicit user
constraints into the improved prompt with auditable wording, then rerun the
same fixture. The metric contract will not be weakened after observing this
result. Reply Rescue remains out of scope until this gate passes.
