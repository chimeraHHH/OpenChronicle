# Capture breakpoint gate — bounded development result

Date: 2026-08-09

Status: **development evidence only; not a registered or publishable main
experiment**. The canonical v1 opportunity set has no timestamped capture
activity traces, so this change is intentionally evaluated as an auxiliary
runtime constraint rather than used to relabel or remove its
`negative-active-conversation` case.

## Hypothesis

A Work Resumption card should be eligible only after the latest persisted
capture has been quiet for one configurable interval. The capture's wall time
and suspend-aware monotonic tick must come from the same clock sample. Any new
capture after card preparation must invalidate that card.

This mechanism follows the externally supported direction recorded in
`docs/vida-work-resumption-signal-brief.md`: proactive programming assistants
pause while the user is active, breakpoint-delayed notifications reduce
interruption cost, and suggestion-display policies need latent user state rather
than relevance alone.

## Implementation under test

- `CaptureActivityGate` accepts only the scheduler's private same-sample
  persisted-event envelope and fails closed before its first healthy sample,
  on missing/malformed values, or on a backwards monotonic tick.
- The production suggestion worker samples `MonotonicWallClock.sample()` once
  per scan and supplies both wall time and monotonic tick to Work Resumption.
- The daemon passes one gate instance to both the persisted-capture hook and the
  suggestion worker. Capture-only mode does not start this path.
- A suggestion stores the current SQLite `captures` AUTOINCREMENT generation.
  A later capture changes that durable generation, so list and transition paths
  expire the stale card even after a process restart.
- The default quiet interval is 20 seconds, bounded to 1–300 seconds.

## Falsification results

Targeted local verification passed 42 tests across the Suggestion Kernel,
three-baseline evaluator, clock pipeline, and daemon lifecycle. The auxiliary
cases establish:

- no activity sample: display denied;
- 19.999 seconds after the latest sample: denied;
- exactly 20 seconds: allowed;
- a new sample: denied again;
- a backwards tick: the gate becomes unhealthy and denies;
- the daemon's capture and suggestion paths share the same gate; and
- a newly indexed capture expires a previously prepared card.

The unchanged canonical dataset was then rerun with three latency repetitions.
Its decisions remained unchanged, as required:

| Variant | Precision | Recall | Evidence | Unsupported claims | Duplicates | p95 decision |
|---|---:|---:|---:|---:|---:|---:|
| Reactive | 1.00 | 0.50 | 1.00 | 0 | 0 | 1.213 ms |
| Heuristic | 0.20 | 1.00 | 0.90 | 0 | 0 | 0.013 ms |
| Kernel | 0.50 | 1.00 | 1.00 | 0 | 0 | 0.913 ms |

Raw ignored report:
`scratch/vida-suggestions-pilot-breakpoint.json`, SHA-256
`0ec91f7724543d687624f91b931a5c7ef50faf11e93320e71bb60be49a0e2d51`.
It records a dirty worktree and `blocked_unregistered`, so it cannot support a
release or parity claim.

## Timestamped trace extension

The follow-up `OC-Vida-Activity-Traces-v1` adds 12 synthetic timestamped states
without modifying the canonical opportunity fixture. It compares production
Work Resumption with no gate and 5/10/20/30/60-second quiet thresholds. Labels
remain engineering hypotheses, not independently collected field ground truth.

| Threshold | Precision | Snapshot recall | False positives | Deferred positives | Added-delay p95 |
|---|---:|---:|---:|---:|---:|
| No gate | 0.333 | 1.00 | 8 | 0 | 0 s |
| 5 s | 0.571 | 1.00 | 3 | 0 | 0 s |
| 10 s | 0.667 | 1.00 | 2 | 0 | 0 s |
| 20 s | 0.750 | 0.75 | 1 | 1 | 8 s |
| 30 s | 1.000 | 0.50 | 0 | 2 | 18 s |
| 60 s | 1.000 | 0.25 | 0 | 3 | 48 s |

Every variant retained 100% evidence coverage and remained well under the 250
ms decision-latency gate. Raw ignored report:
`scratch/vida-breakpoint-traces.json`, SHA-256
`17e760e306485b616bc0d4ac10aa7963ee4a20a14868a8d3caf4b17532bfe66f`.
It is explicitly `blocked_unregistered_auxiliary`.

The deliberately overlapping pair—helpful at 12 seconds, still thinking at 21
seconds—shows why threshold tuning alone cannot preserve all useful moments and
remove all interruptions. A 30-second threshold reaches the precision target
only by deferring half the positives at the evaluation snapshot.

## Decision

Retain the default 20-second gate as a production safety/timing constraint, but
stop the timing-only optimization route. It improves the synthetic tradeoff
without reaching the 0.85 precision target and preserving all positives. The
canonical active-conversation and already-resolved false positives therefore
remain open.

Next bounded direction: prefer an explicit park/resume cue for inspectable,
high-confidence labels while scouting a separately validated continuity and
resolution assessor. No model-based assessor should be promoted on this tiny
synthetic set.
