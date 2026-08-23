# Reviewed long-term memory lifecycle baseline v1

This deterministic benchmark executes gold `remember`, `update`, `forget`, and
`reflect` operations through OpenChronicle's production review-first services in
an isolated temporary root. It checks current state, immutable history, exact
claim support, stale-value filtering, complete forget, and unrelated-memory
retention.

Run:

```bash
uv run python scripts/run_vida_memory_operations.py
```

The trace vocabulary and metrics are informed by
[MemOps](https://github.com/MemTensor/MemOps), pinned during design at
`312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35`. No upstream code or generated
dataset is copied. This native v1 split executes already-known gold operations;
it does not yet measure whether a model correctly infers those operations from
noisy conversation or activity history. The `reflect` case verifies two-source
support and state behavior, not the semantic quality of the inferred sentence.

The first clean result and its interpretation boundary are recorded in
[analysis/reviewed-lifecycle-2026-08-23.md](analysis/reviewed-lifecycle-2026-08-23.md).
