# Reviewed-memory Prompt Rescue A/B — passing constraint-fidelity run

This rerun kept the frozen six cases, metric contract, provider
`codex_cli:gpt-5.6-sol`, and reasoning effort `none`. The only production change
from the retained failed baseline was a general Prompt Rescue instruction to
carry legitimate current constraints into the improved prompt with auditable
wording, especially negation, quantities, and scope.

The machine-readable report is
`reports/vida-prompt-memory-2026-08-24.json`, SHA-256
`daff3ffe4fb4a248c6ff61683d41bab0fd163d5efd6a4ce03fce62fc6b1d9b82`.

| Variant | Current anchors | Applicable memory anchors | Forbidden hits | Action boundary | Parse success |
|---|---:|---:|---:|---:|---:|
| No reviewed memory | 1.000 | 0.111 | 0.000 | 1.000 | 1.000 |
| Reviewed memory | 1.000 | 0.889 | 0.000 | 1.000 | 1.000 |

Reviewed memory adds 0.778 applicable-procedure anchor coverage while retaining
every exact current constraint and introducing no stale, unrelated,
prompt-injection, or action-bearing forbidden text. All unchanged v1 checks
pass.

This is a small first-party development result, not a held-out or public
quality claim. Production store retrieval and supersede/forget behavior are
tested separately. The result authorizes retaining the Prompt Rescue pilot; it
does not authorize automatic procedure promotion, Reply Rescue expansion, or
computer use.
