# Reviewed-memory Prompt Rescue A/B evaluation v1

This frozen native development split checks the first complete long-term-memory
product loop:

```text
reviewed procedure -> relevant future request -> improved text artifact
```

It sends the same six current requests through two variants:

- `no_memory`, with an empty reviewed-memory context;
- `reviewed_memory`, with the frozen procedure context.

The cases test relevant procedure application, current-request precedence,
irrelevant-memory rejection, prompt injection inside memory, explicit empty
context, and the no-action boundary. The gate requires reviewed memory to add
at least 0.5 applicable-anchor coverage without reducing current-request
coverage or introducing forbidden stale/irrelevant/action text.

Run the configured Prompt Rescue provider:

```bash
uv run python scripts/run_vida_prompt_memory.py \
  --output reports/vida-prompt-memory.json
```

This benchmark does not retrieve from a real store; production retrieval,
revision binding, supersede/forget invalidation, and race revalidation are
covered by `tests/test_prompt_rescue.py`. Keeping the A/B text-effect gate
separate makes failures attributable. It is a small first-party development
fixture, not a public benchmark or a claim of Vida parity.

The first configured `gpt-5.6-sol` run improved applicable-memory coverage but
failed the unchanged current-constraint anchor gate. The retained report and
diagnosis are in
[analysis/gpt-5.6-sol-v1-failed-2026-08-24.md](analysis/gpt-5.6-sol-v1-failed-2026-08-24.md).
