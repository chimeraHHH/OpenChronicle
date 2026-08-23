# Vida event retrieval comparison v1

This frozen native development fixture compares four retrieval-unit shapes on
the same six cases with the same local SQLite FTS5/BM25 ranker. Every variant
uses an implicit-AND query first and a local OR/BM25 retry only on zero hits:

- individual minute observations;
- whole session text;
- reducer sub-task events;
- reducer sub-task events with one previous/next hop.

The comparison measures exact evidence-anchor recall, forbidden-anchor noise,
empty hits, and returned context size. It isolates unit shape; production event
parsing, canonical revalidation, adjacency, rebuild, and deletion behavior are
covered separately by `tests/test_activity_events.py`.

Run:

```bash
uv run python scripts/run_vida_event_retrieval.py
```

This small first-party regression is not a public benchmark result. It is a
gate against promoting model-driven topic segmentation without evidence that
the deterministic reducer sub-task boundary is insufficient.
