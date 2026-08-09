# Work Resumption opportunity baseline v1

This package compares the reactive control, the production deterministic gap
assessor, and the production Suggestion Kernel on `OC-Vida-Fixtures-v1`.

Run:

```bash
uv run python scripts/run_vida_suggestion_eval.py
```

The runner creates an isolated OpenChronicle root and real SQLite/provenance
graph for every case and latency repetition. It never uses the user's normal
OpenChronicle root. The JSON result records the repository commit, dirty state,
host, exact dataset digest, per-case decisions, confusion counts, metrics, and
gate verdicts.

The fixture labels are engineering hypotheses, not ground truth from users.
`negative-already-resolved` and `negative-active-conversation` are deliberately
included to expose where a gap detector needs richer opportunity signals. A
passing test suite proves evaluator determinism; it does not turn a failing
quality gate into a pass.

The first bounded follow-up is recorded in
[analysis/empty-context-ablation.md](analysis/empty-context-ablation.md). The
full Kernel remains below the precision gate after that ablation.
