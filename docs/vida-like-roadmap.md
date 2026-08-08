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

The point-in-time official-source research and capability gap map live in
[vida-public-product-research.md](vida-public-product-research.md). Public
marketing claims are inputs, not independent quality evidence or permission to
copy Vida's code, assets, brand, or private implementation.

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

The public-parity pilot targets Vida's five publicly named workflows. An
OpenChronicle-specific work-resumption slice is kept separately because Vida's
current public “Resume Rescue” means job-application résumé preparation, not
reconstructing interrupted work.

| Workflow | Clean-room behavior | Initial autonomy |
|---|---|---|
| Daily Wrap | Evidence-backed summary of completed, open, and blocked work | Suggest |
| Prompt Rescue | Prepare a stronger prompt from selected text and project context | Prepare; never submit |
| Reply Rescue | Prepare an evidence-backed reply draft in the user's style | Prepare; never send |
| Résumé Rescue | Prepare a job-targeted résumé from user-selected experience, documents, and reviewed memory | Prepare; never submit an application |
| Work Resumption | Reconstruct the last state and next step after an interruption | Suggest / Prepare |
| Workspace Cleanup | Preview a scoped file move/rename plan with conflicts and undo | Prepare, then approved Act |

Sending, publishing, permanent deletion, payment, unrestricted shell, and
credential access are outside the first pilot.

## Delivery stages

### Stage 0 — trustworthy observation and runtime

- Preserve deep-copied watcher snapshots for debounce, then project them to
  exact identity-only triggers before persistence or session hooks.
- Give every observation a unique, round-trippable ID and timestamp.
- Persist private captures atomically with restrictive permissions.
- Recover active sessions left by crashes.
- Put finite timeout and retry bounds on every model call.
- Supervise daemon workers instead of treating an early exit as clean shutdown.
- Add a fail-closed capture privacy pipeline: validate configuration first,
  apply app/bundle/title policy before AX, and, when URL rules are active,
  reject unsupported bundles before AX. A known browser family must expose one
  explicit address under an exact stable identifier; a bounded full-tree scan
  is an additional deny surface before persistence or model use.
- Bind AX and opt-in screenshots to one exact focused-window identity (app,
  bundle, title, PID, `CGWindowID`, and bounds), with no display fallback.
- Default screenshots off and add retention/redaction controls.

Exit criteria include zero excluded-data leakage across every sink, no capture
overwrite under same-time bursts, no stranded sessions after kill/restart, no
duplicate reduction, and a seven-day soak without data corruption.

Live AX privacy and exact-window capture are tracked in
[#3](https://github.com/chimeraHHH/OpenChronicle/issues/3). The current working
branch implements normal schema-v4/policy-v2 observations and schema-v5/
policy-v3 `url_metadata_only` observations, literal browser URL allow/exclude
rules, secure-field helper redaction, exact `WindowMeta` fencing around AX, and
CoreGraphics capture of one verified `CGWindowID` with no full-screen/`mss`
fallback. URL policy is deliberately narrower: it accepts only known browser
family adapters with one explicit stable-ID address, requires verified complete
trees and matching evidence from two ephemeral snapshots, uses the full tree
only as an additional deny surface, and persists no raw AX/focused/page/title
content or pixels. The double read mitigates navigation races but is not atomic.
The branch also includes an opt-in, redaction-safe live macOS AX/privacy
protocol. This is implementation status only, not a completion or merge claim:
#3 remains open until automated/remote review is green and the interactive
macOS acceptance run is recorded. The live protocol includes an in-memory
exact-window pixel probe with public/sibling color canaries, but the current
machine was locked (`loginwindow`) during its latest run; a full unlocked pass
is still required before issue closure.

Classifier delivery is tracked separately in
[#4](https://github.com/chimeraHHH/OpenChronicle/issues/4). The current branch
implements a durable outbox/state machine, deterministic delivery/producer
keys, lease-fenced proposal/commit transactions, typed committed receipts,
post-receipt bookmark finalization, exact-entry/typed-empty terminal recovery,
reducer cleanup-generation fencing, status counts, and fault tests across the
main crash/concurrency boundaries. The reviewed local tree passes the full
Python suite on both supported Python versions plus the desktop frontend and
native-core gates; an independent red-team pass found no remaining reproducible
P0/P1. This is still not an issue-closure claim: #4 should close only after the
Draft PR's remote CI/review confirms the same tree and the stack is merged. The
guarantee covers replay-safe local candidate delivery; it does not promise
exactly-once provider invocation and does not convert delivery commit into user
approval.

Process-level runtime validation is tracked in
[#2](https://github.com/chimeraHHH/OpenChronicle/issues/2). The current runtime
branch adds authenticated test-only `SIGKILL` boundaries, fresh-process daemon
restart/PID tests, deterministic capture/Markdown/index reconciliation,
a shared suspend-aware daemon-generation clock for capture/session/timeline,
exact persisted capture timestamps, wake-before-refresh session cuts,
receipt-gated capture cleanup, a parent-owned provider process deadline, and a
closed-schema 10,000-capture/soak harness. Late captures can re-materialize an
unconsumed timeline block; a consumed or invalid block instead preserves raw
evidence and stalls the watermark. This is fail-closed safety, not automatic
convergence, because downstream cascade replay is not implemented or validated.
Local 10,000-capture replay evidence passes, but #2 remains open: no qualifying
24-hour run has been recorded, the synthetic storage worker does not measure
the production daemon queue, and downstream-level cascade recovery has not
been demonstrated. An unsigned report proves only internal consistency;
trusted runner logs and the reported artifact digest are still required to
establish its origin.

Stage 0 remains open after #4 as well: its broader privacy, supervision,
model-call, crash-recovery, and seven-day-soak exit criteria must be evaluated
as a set. No item above should be read as a claim that Stage 0 is complete.

### Stage 1 — memory product and Daily Wrap

- [x] Shared typed `ContextService` and `MemoryService`, with read-only MCP adapters.
- [x] Evidence references from observation through activity and durable memory.
- [x] Review-first classifier, candidate conflicts/editing/approval, and crash-resumable purge.
- [x] One canonical, revisioned, evidence-backed Daily Wrap per local day and timezone.
- [x] CLI review inbox, source tracing, Daily Wrap generation/read commands, and daemon worker.
- [ ] Native desktop shell for permissions, pause/exclusions, source drawer, and review inbox.
  The capability-scoped Tauri/React source slice and one-shot Python bridge are
  implemented on the desktop-shell branch. This stays open until the bridge is
  bundled self-contained and a signed/notarized macOS build passes TCC,
  accessibility, pause-latency, and adversarial source-drawer validation.
- [ ] Provenance-aware compaction and deterministic supersede proposals.

The implemented backend contract, threat boundaries, and known limitations are
documented in [stage1-memory-daily-wrap.md](stage1-memory-daily-wrap.md). Stage 1
is not complete until the native review/source-drawer shell is built and its
privacy UX is validated on a signed macOS build.

### Stage 2 — Suggest and Prepare

- Privacy-filtered real-time path independent of the minute timeline.
- Opportunity detectors, ranking, dedupe, cooldown, quiet hours, and daily budget.
- Prompt Rescue, Reply Rescue, Résumé Rescue, and Work Resumption without side
  effects.
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
| Timeline and session | SQLite | Daily event Markdown |
| Classifier delivery job/receipt | SQLite `classifier_jobs` outbox | Status/log summaries |
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
2. Runtime/session/model-call reliability under #2; the current Draft remains
   incomplete until a qualifying 24-hour soak, real production-daemon queue
   evidence, and downstream cascade-replay acceptance are recorded.
3. Observation identity, event fidelity, atomic private persistence.
4. Capture policy, redaction, exact-window identity, and retention. **#3 is in
   progress; implementation and an AX/privacy audit protocol exist, but live
   pixel validation, remote review, and merge remain.**
5. Provenance spine and memory candidates. **Implemented on the Stage 1 branch.**
6. Daily Wrap vertical slice. **Implemented on the Stage 1 branch.**
7. Native review inbox, permissions shell, and source drawer. **Source slice
   implemented; signed macOS release validation remains.**

Generic planning, connectors, and action execution intentionally start only
after the earlier gates pass.
