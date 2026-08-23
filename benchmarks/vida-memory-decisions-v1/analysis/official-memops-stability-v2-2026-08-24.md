# Official MemOps adjacent stability v2 — 2026-08-24

This follow-up re-aggregates the same three inert model runs used by the failed
v1 gate. The source runs bind OpenChronicle commit
`4b866f6eb8aafa36d26935940d993c37b854b0c5`; the clean v2 aggregator binds
commit `c3518fadc0783261e282c44d149a58b2ddc5f6bc`. The report is
`reports/vida-memory-decisions-memops-stability-v2-2026-08-24.json` with
SHA-256 `ef38ead89416e0d5c7803a49ec05f89413ffb5b6548d8778031e1db1c0506cfd`.

The v2 gate passes while preserving the v1 failure:

| Stability layer | Exact case agreement | Gate |
|---|---:|---:|
| Operation type + target + chronological order | 1.000 | ≥ 1.000 |
| Operation type + target + exact evidence set | 0.750 | ≥ 0.750 |
| Complete generated operation JSON | 0.250 | diagnostic only |

All three runs also passed the original smoke gate with operation recall 1.000,
precision 0.964, F1 0.982, provenance 0.963, one false update, and zero
provider/parse failures. Operation F1 population standard deviation was 0.

B02 remember, C26 update chain, and C29 forget had identical operation and
evidence structures. E12 reflect retained the same operation types, targets,
and order but selected different supersets/subsets of supporting evidence; its
generated reflection and several fact values were also paraphrased. This makes
reflect evidence calibration a concrete remaining target. The exact-value
diagnostic is not promoted into a semantic-equivalence claim because v2 uses no
judge model.

This remains the same fixed four-file official-data adapter smoke, not the full
MemOps pipeline or leaderboard.
