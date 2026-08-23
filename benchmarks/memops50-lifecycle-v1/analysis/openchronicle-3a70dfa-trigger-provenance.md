# MemOps-50 reviewed lifecycle: trigger-provenance regression

## Verdict

The corrected evidence adapter leaves the production lifecycle result
unchanged: all 11 gates pass on clean OpenChronicle commit
`3a70dfa470c1cc059c874608dda4e99ef768d480`.

For remember, update, and forget, durable provenance is now checked against the
exact turn that triggered the operation. Reflect continues to require its full
set of independent supporting turns. This matches the operation-inference
contract instead of assigning old values or later confirmations to a
state-changing trigger.

## Reproduction identity

- MemOps commit:
  `312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35`
- manifest SHA-256:
  `5f362fea11b74097cba3fa3f4ed3d135ffd3329aa3232308671bdfc6b71e1c83`
- metric-contract SHA-256:
  `43731c99d8083a043c393c1d48cebcb2dc7cc53c44d20dc053aabfb9fc2f973b`
- raw report SHA-256:
  `d8e25e7df7faee04e9716cfc345ba05fa07ce4c08b267b64bc5c81759fcebe01`
- repository state at evaluation start: clean, zero status lines
- action capability: isolated temporary memory only

## Metrics

| Metric | Result |
|---|---:|
| Scenario pass rate | 1.000 |
| Operation success rate | 1.000 |
| Per-operation checkpoint accuracy | 1.000 |
| Final-state accuracy | 1.000 |
| Stale-value rate after update | 0.000 |
| Forget current-state leakage | 0.000 |
| Forget historical leakage | 0.000 |
| Over-forget rate | 0.000 |
| Durable provenance support | 1.000 |
| TrajectoryOps checkpoint accuracy | 1.000 |

The result covers 281 confirmed operations in 50 scenarios: 219 remember, 40
update, 10 forget, and 12 reflect. Thirty tentative operations remain excluded.
It confirms that the provenance correction did not regress reviewed storage,
supersession, purge, or trajectory behavior.
