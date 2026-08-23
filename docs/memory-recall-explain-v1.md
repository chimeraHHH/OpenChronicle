# Memory recall explanation v1

OpenChronicle can inspect why the existing local durable-memory ranker returned
an entry:

```bash
openchronicle memory explain-recall "editor preference" --top-k 5
openchronicle memory explain-recall "editor preference" --top-k 5 --json
```

This is a diagnostic view, not a new ranker. It uses the same non-event memory
scope, BM25 implementation, optional local embedding model, reciprocal-rank
fusion parameters, canonical Markdown authorization, tombstones, and current
fact policy as normal memory recall.

For BM25-only search, each result includes its ordinal BM25 rank and raw FTS5
score. With semantic memory enabled, each result includes whichever channels
actually found it:

- `bm25_rank`;
- `vector_rank` and `vector_similarity`;
- final `rrf_score` and output `position`.

The report intentionally omits entry body, tags, evidence text, and the raw
query. It returns only a SHA-256 query digest and character count so two local
runs can be compared without creating a second plaintext query record. The
command does not persist its report, call an LLM, add a table, change ranking,
or feed usage back into future rank. Normal first-run initialization can still
create the standard config, log, and SQLite schema files. Hybrid search may
synchronize its rebuildable local embedding projection, exactly as product
search already does.

Semantic-backend failure is explicit. Once semantic memory is enabled, an
unavailable FastEmbed installation or model produces `hybrid_unavailable` and
an empty result set; there is no hidden BM25 fallback.

This view is intended to pair with `openchronicle memory usefulness --json`:
recall explanation answers why an exact revision entered context, while the
usefulness report shows whether a conditioned text product was later adopted.
Neither signal changes memory automatically.
