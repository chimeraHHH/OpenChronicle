# Explicit-adoption procedure gate — gpt-5.6-sol — 2026-08-24

The clean confirmation run binds OpenChronicle commit
`9b1f6d8c38e244d8f62019ce8ad4033972cc3c4d`, records `dirty=false`, and uses
`codex_cli:gpt-5.6-sol` with reasoning effort `none`. The machine-readable
report is `reports/vida-procedure-adoption-2026-08-24.json` with SHA-256
`d565789ec92d7a6decf304fabaaa82ab1cb9a3a82cc775e8d45f126558f07251`.

| Variant | Precision | Recall | Negative accuracy | Anchor support | Action boundary |
|---|---:|---:|---:|---:|---:|
| Any explicit adoption | 0.300 | 1.000 | 0.000 | 0.000 | 1.000 |
| Configured no-tool model | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |

The naive route promoted all three reusable artifacts but also all seven
negative artifacts. Those negatives include one-off prompts and replies,
specific commitments, an action-bearing prompt, prompt injection, a secret,
and a placeholder reply without explicit reuse intent. Therefore an **I used
this** record alone is not sufficient procedural-memory evidence.

The configured model correctly retained the three artifacts whose text itself
declared a reusable trigger and workflow/checklist/template, rejected all seven
negatives, preserved every required anchor, and emitted `action_capability:
none`. This passes the frozen gate for designing a review-only pilot. The run
did not stage candidates or change the production classifier.

This is one run on a small first-party development split with author-defined
labels, not a public benchmark or repeat-stability result. The user-confirmed
signal is not independent observation of external use. Before production use,
the screened proposal must still pass the existing procedure validator and
enter the ordinary review inbox; it must never auto-publish or execute actions.
