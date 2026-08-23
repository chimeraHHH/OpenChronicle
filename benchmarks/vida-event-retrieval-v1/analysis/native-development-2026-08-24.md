# Native event retrieval comparison — 2026-08-24

The clean confirmation run binds OpenChronicle commit
`c903bb6d554b528881560846430f78baaaa2e6de`, records `dirty=false`, and passes
the frozen native-development gates. The machine-readable report is
`reports/vida-event-retrieval-2026-08-24.json` with SHA-256
`e30d4e87add919c0657f7b52a61b13d8d06695358132364dae86be24b0dd59f1`.

| Retrieval unit | Answerable | Anchor recall | Forbidden-anchor rate | Mean context chars |
|---|---:|---:|---:|---:|
| Minute | 0.333 | 0.500 | 0.000 | 58.3 |
| Whole session | 1.000 | 1.000 | 1.000 | 189.5 |
| Event | 0.667 | 0.800 | 0.000 | 73.0 |
| Event + one-hop adjacency | 1.000 | 1.000 | 0.286 | 139.8 |

On these six cases, one-hop event adjacency recovered every required evidence
anchor. Relative to minute units, anchor recall increased by 0.500. Relative
to whole-session recall, returned context was 0.738 times as large and the
forbidden-anchor rate fell by 0.714. Blind temporal adjacency still introduced
two nearby distractor anchors, so this result does not justify claiming
noise-free context.

The first development pass also exposed a zero-hit failure when an inflected
query term or a multi-term query crossed an event boundary. The frozen ranker
therefore runs the same implicit-AND FTS query for every variant and retries
once with local OR/BM25 only when the strict query returns no row. Production
activity results expose whether `strict_and` or
`relaxed_or_after_zero_hits` produced the match.

This is a small first-party development regression, not a held-out or public
benchmark. The gates and six cases were finalized after local development
pilots, then confirmed on the clean bound commit. It supports keeping the
deterministic reducer-subtask boundary as the current product default; it does
not establish that model-driven topic segmentation is unnecessary on broader
real or public activity traces.
