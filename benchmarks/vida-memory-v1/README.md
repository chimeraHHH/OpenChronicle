# Local long-term memory retrieval baseline v1

This package runs the production Markdown/SQLite write path, production
supersede lifecycle, and production FTS5 search against an isolated temporary
OpenChronicle root. It never reads or mutates the user's normal memory.

Run:

```bash
uv run python scripts/run_vida_memory_eval.py
```

The first frozen split covers exact retrieval, semantic paraphrase,
Chinese-to-English retrieval, knowledge updates, historical opt-in, entity
isolation, provenance identity, and abstention when no memory exists. The
current `bm25` variant is a pre-hybrid baseline: semantic failures are expected
and must remain visible rather than being hidden by an LLM judge.

This benchmark does not yet claim LongMemEval compatibility. A later adapter
may add the upstream task/version metadata, but native replay cases remain the
product gate because OpenChronicle starts from captured activity rather than
chat transcripts.
