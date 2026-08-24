# Local long-term memory retrieval baseline v1

This package runs the production Markdown/SQLite write path, production
supersede lifecycle, and production FTS5 search against an isolated temporary
OpenChronicle root. It never reads or mutates the user's normal memory.

Run:

```bash
uv run python scripts/run_vida_memory_eval.py
```

Run the local multilingual hybrid candidate (after installing the optional
dependency):

```bash
uv sync --extra semantic-memory
uv run python scripts/run_vida_memory_eval.py --hybrid
```

The first frozen split covers exact retrieval, semantic paraphrase,
Chinese-to-English retrieval, knowledge updates, historical opt-in, entity
isolation, provenance identity, and abstention when no memory exists. The
`production_fts5_bm25` is the fixed pre-hybrid baseline: semantic failures are
expected and remain visible rather than being hidden by an LLM judge.
`production_hybrid_rrf` runs the production local vector projection and
BM25/vector fusion against the identical fixtures and gates.

This benchmark does not yet claim LongMemEval compatibility. A later adapter
may add the upstream task/version metadata, but native replay cases remain the
product gate because OpenChronicle starts from captured activity rather than
chat transcripts.
