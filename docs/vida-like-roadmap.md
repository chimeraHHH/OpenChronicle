# Vida-like proactive desktop agent roadmap

This fork evolves OpenChronicle into a clean-room, local-first proactive desktop
assistant. It uses Vida's publicly named workflows as product inspiration, but it
does not copy Vida's code, visual assets, brand, or private implementation.

## Architecture decision

OpenChronicle remains the **Memory Plane**:

- accessibility-first desktop observations;
- timeline and session reduction;
- inspectable Markdown long-term memory;
- local SQLite/FTS projections;
- read-only MCP access.

A separate **Action Plane** will own proactive suggestions, typed plans,
deterministic policy checks, user approvals, capability-scoped execution,
verification, audit, and undo.

The memory classifier must never receive file, browser, messaging, or other
side-effecting tools. Screen-derived text is untrusted data, not an instruction
source.

```text
AX watcher / shortcut / timer
        |
        v
Privacy Firewall -> versioned Observation
        |                    |
        v                    v
Timeline -> Session      real-time detectors
        |                    |
        v                    v
Memory Candidate        Suggestion ranker
        |                    |
        +------ ContextService
                             |
                             v
                       Typed Planner
                             |
                             v
                  Deterministic Policy Engine
                             |
                             v
                    Trusted Approval UI
                             |
                             v
                  Isolated Capability Broker
                             |
                             v
                    verify / audit / undo
```

## Product slices

The first pilot targets five independently specified workflows:

| Workflow | Clean-room behavior | Initial autonomy |
|---|---|---|
| Daily Wrap | Evidence-backed summary of completed, open, and blocked work | Suggest |
| Prompt Rescue | Prepare a stronger prompt from selected text and project context | Prepare; never submit |
| Reply Rescue | Prepare an evidence-backed reply draft in the user's style | Prepare; never send |
| Resume Rescue | Reconstruct the last state and next step after an interruption | Suggest / Prepare |
| Workspace Cleanup | Preview a scoped file move/rename plan with conflicts and undo | Prepare, then approved Act |

Sending, publishing, permanent deletion, payment, unrestricted shell, and
credential access are outside the first pilot.

## Delivery stages

### Stage 0 — trustworthy observation and runtime

- Preserve complete, deep-copied watcher event snapshots.
- Give every observation a unique, round-trippable ID and timestamp.
- Persist private captures atomically with restrictive permissions.
- Recover active sessions left by crashes.
- Put finite timeout and retry bounds on every model call.
- Supervise daemon workers instead of treating an early exit as clean shutdown.
- Add capture allow/exclude policy before AX, screenshot, persistence, or model use.
- Default screenshots off and add retention/redaction controls.

Exit criteria include zero excluded-data leakage across every sink, no capture
overwrite under same-time bursts, no stranded sessions after kill/restart, no
duplicate reduction, and a seven-day soak without data corruption.

### Stage 1 — memory product and Daily Wrap

- [x] Shared typed `ContextService` and `MemoryService`, with read-only MCP adapters.
- [x] Evidence references from observation through activity and durable memory.
- [x] Review-first classifier, candidate conflicts/editing/approval, and crash-resumable purge.
- [x] One canonical, revisioned, evidence-backed Daily Wrap per local day and timezone.
- [x] CLI review inbox, source tracing, Daily Wrap generation/read commands, and daemon worker.
- [ ] Native desktop shell for permissions, pause/exclusions, source drawer, and review inbox.
- [ ] Provenance-aware compaction and deterministic supersede proposals.

The implemented backend contract, threat boundaries, and known limitations are
documented in [stage1-memory-daily-wrap.md](stage1-memory-daily-wrap.md). Stage 1
is not complete until the native review/source-drawer shell is built and its
privacy UX is validated on a signed macOS build.

### Stage 2 — Suggest and Prepare

- Privacy-filtered real-time path independent of the minute timeline.
- Opportunity detectors, ranking, dedupe, cooldown, quiet hours, and daily budget.
- Prompt Rescue, Reply Rescue, and Resume Rescue without side effects.
- Feedback and proactive-quality evaluation.

### Stage 3 — safe Action Plane

- Typed `ActionIntent`, deterministic risk policy, bound approval tokens.
- Isolated capability broker with credentials unavailable to the planner.
- Preconditions, idempotency, postcondition verification, `unknown_outcome`, and undo.
- Workspace Cleanup as the first and only default Act workflow.

### Stage 4 — projects, Tracker, and limited connectors

- Project/workspace scopes and cross-project isolation.
- Durable scheduled tasks and checkpointed long-running work.
- Artifact provenance.
- Read-only connectors first, draft-only second, external mutation last.

### Stage 5 — pilot hardening

- Signing, notarization, controlled updates, and login item.
- Complete export, deletion, backup, and recovery.
- Performance budgets, fault injection, red-team suite, and 30-day dogfood.

## Canonical state

| Object | Source of truth | Derived state |
|---|---|---|
| Observation | Versioned append-only capture envelope | Capture FTS |
| Timeline, session, jobs | SQLite | Daily event Markdown |
| Long-term fact | Markdown entry with machine-readable provenance | SQLite FTS |
| Memory candidate | Typed SQLite state | Review UI |
| Suggestion, plan, action, approval | Typed SQLite state | Product UI |
| Artifact | File bytes plus content hash | SQLite metadata |
| Audit | Append-only SQLite metadata | Exported report |

## Action risk model

| Risk | Meaning | Pilot policy |
|---|---|---|
| R0 | Scoped read | Allowed and audited |
| R1 | Draft or preview | May prepare automatically |
| R2 | Local reversible write | Exact confirmation and undo required |
| R3 | External or difficult-to-reverse mutation | Disabled by default; per-action final confirmation |
| R4 | Bulk permissions, payment, regulated action | Unsupported |
| R5 | Credential export or approval bypass | Permanently denied |

An approval binds the canonical action hash, account, destination, observed
resource version, nonce, and expiry. Any parameter or live-state change
invalidates it. A tool response alone is not proof of success; every successful
action needs a verified postcondition.

## Quality gates

- Capture recall at least 95% on the fixed fixture set.
- Timeline factual accuracy at least 95%.
- Long-term fact precision at least 98% before any auto-approval.
- Every durable fact and suggestion has evidence references.
- Retrieval Recall@5 at least 90% and MRR at least 0.80.
- Suggestion precision at least 85%; at most one invalid interruption per day.
- No unapproved side effects in fault and adversarial testing.
- No duplicate effects across 10,000 timeout/crash/replay simulations.
- No successful prompt-injection side effects across 1,000 adversarial traces.
- Every declared-reversible benchmark action restores successfully.

## Current implementation order

1. CI and reproducible baseline.
2. Runtime/session/model-call reliability.
3. Observation identity, event fidelity, atomic private persistence.
4. Capture policy, redaction, and retention.
5. Provenance spine and memory candidates. **Implemented on the Stage 1 branch.**
6. Daily Wrap vertical slice. **Implemented on the Stage 1 branch.**
7. Native review inbox, permissions shell, and source drawer.

Generic planning, connectors, and action execution intentionally start only
after the earlier gates pass.
