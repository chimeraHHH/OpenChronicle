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

## Decision

Retain the gate as a production safety/timing constraint. Do **not** claim that
it fixes the active-conversation false positive until a frozen trace extension
with independently reviewed activity timestamps tests gate-on versus gate-off.
The other remaining false positive, already-resolved work, is untouched.

Next bounded experiment: add a trace fixture extension without changing the v1
labels, then compare quiescence thresholds for precision, recall, delay, and
invalid interruptions. If no threshold separates useful returns from active
conversation, abandon timing-only optimization and require an explicit cue or
a separately validated continuity/resolution assessor.
