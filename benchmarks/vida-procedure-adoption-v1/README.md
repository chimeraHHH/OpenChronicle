# Explicit-adoption procedural-memory evaluation v1

This frozen native development split tests whether one explicit **I used this**
record is enough to stage a reusable text procedure. It compares:

- `any_adoption`, which treats every adopted artifact as procedural evidence;
- a configured no-tool model that emits only an inert qualification decision
  and, when justified, a text-only workflow/checklist/template proposal.

All ten inputs are positive-use records. Only three artifacts explicitly encode
a reusable trigger and structure. The negatives cover one-off prompts, specific
replies and commitments, manual edits, external-action instructions, prompt
injection, secrets, and a placeholder reply without declared reuse intent.

Run the configured classifier provider (normally `codex_cli:gpt-5.6-sol`):

```bash
uv run python scripts/run_vida_procedure_adoption.py \
  --output reports/vida-procedure-adoption.json
```

The runner never stages a candidate or changes memory. Its result can justify
designing a review-only production adapter, but cannot by itself authorize
single-adoption promotion. This is a small first-party development regression,
not a public benchmark or evidence of actual external use.

The first clean configured-model result and its limitations are recorded in
[analysis/gpt-5.6-sol-2026-08-24.md](analysis/gpt-5.6-sol-2026-08-24.md).
