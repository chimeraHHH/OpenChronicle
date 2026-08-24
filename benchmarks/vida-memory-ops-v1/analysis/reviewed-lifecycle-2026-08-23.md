# Reviewed memory lifecycle result — 2026-08-23

The clean native lifecycle run passes every frozen v1 gate. It uses commit
`624faa9758156cb4b8b500322a0a0d763ff7d999`, records `dirty=false`, and is
stored in `reports/vida-memory-operations-2026-08-23.json`.

| Metric | Result | Gate |
|---|---:|---:|
| Operation success rate | 1.00 | ≥ 1.00 |
| Checkpoint pass rate | 1.00 | ≥ 1.00 |
| Stale-value rate | 0.00 | ≤ 0.00 |
| Forget leakage rate | 0.00 | ≤ 0.00 |
| Over-forget rate | 0.00 | ≤ 0.00 |
| Provenance-support rate | 1.00 | ≥ 1.00 |
| Trajectory-order accuracy | 1.00 | ≥ 1.00 |

The three traces execute seven gold operations and inspect five intermediate
states through production services. They establish that:

- a reviewed update makes the new value current while retaining the old value
  only in immutable history;
- forgetting the current update deletes the complete two-version lineage;
- forgetting one preference does not delete an unrelated preference;
- a reviewed inferred pattern can preserve two exact independent claim sources.

This is a deterministic engineering result. It does not measure whether the
classifier chooses the correct operation from noisy evidence, whether the
inferred pattern is semantically justified, or OpenChronicle's score on the
public MemOps corpus. Those remain the next evaluation layer.
