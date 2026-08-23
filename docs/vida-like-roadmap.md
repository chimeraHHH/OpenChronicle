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

The broader repository/competitor search, selected comparison baselines, and
rejected directions live in
[vida-ecosystem-scout-2026-08-09.md](vida-ecosystem-scout-2026-08-09.md).
Every product slice must also satisfy the executable gates in
[vida-parity-evaluation-contract.md](vida-parity-evaluation-contract.md); a demo
or vendor parity claim is not an acceptance result.
The Memory Plane's pinned external longitudinal adapter is documented in
[longmemeval-v2-adapter.md](longmemeval-v2-adapter.md); implementation and a
real-trajectory retrieval smoke do not close the pending clean small-tier
LongMemEval-V2 score.
The selected follow-up for Work Resumption timing is recorded in
[vida-work-resumption-signal-brief.md](vida-work-resumption-signal-brief.md).
The clean-room interaction, source-binding, runtime, and evaluation contract for
the next workflow lives in
[vida-prompt-rescue-scout-2026-08-09.md](vida-prompt-rescue-scout-2026-08-09.md).

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
- [x] Provenance-aware leaf compaction with exact evidence-frame preservation
  and byte-frozen bodies for entries cited by downstream memory.
- [x] Deterministic reviewed supersede proposals with target revision binding,
  approval-time revalidation, provenance-linked history, idempotent publication,
  and previous-value restoration when the reviewed replacement is purged.
- [x] Authorized historical activity-evidence search for classifier-side
  cross-session pattern confirmation, kept separate from accepted-memory search
  and from the durable semantic index.
- [x] Published Memory / About Me desktop slice for authorized current facts,
  local filtering, scopes, exact provenance opening, native JSON/Markdown
  export, and revision-bound direct correction that preserves superseded
  history without a model call.
- [x] Two-phase Published Memory forget rooted at the selected current entry:
  resolve its oldest fact version, preview the full revision/proposal/file/wrap
  closure, revalidate a digest before native confirmation, and remove the whole
  chain so an older value cannot reappear.
- [x] Separate explicitly cited claim support from the full classifier input-flow
  closure while keeping both revision-bound; review UX shows the focused source
  set and privacy/purge semantics retain the complete dependency closure.
- [x] Typed current-fact projection for new reviewed memories: canonical global
  subject slots, user-asserted/observed/inferred basis, optional valid time,
  Markdown-authoritative round trips, supersede slot preservation, expired or
  scheduled recall filtering, and Review/Published Memory visibility.
- [x] Native lifecycle-operation memory evaluation: seven gold
  remember/update/forget/reflect operations, five state checkpoints,
  stale-value rate, forget leakage, over-forget, provenance support, and
  trajectory order through production review services.
- [x] Inert model-decision lifecycle evaluation with operation
  precision/recall/F1 plus provider/parse/detection/value/provenance failure
  labels. It scores remember/update/forget/reflect without executing any
  prediction; forget remains user-confirmed product behavior. The first clean
  eight-case `gpt-5.6-sol` native run passed every frozen gate, but remains a
  development regression rather than a public benchmark claim. The fresh
  external survey selects MemOps as the closest public contract; see
  [local-long-term-memory-research-2026-08-23.md](local-long-term-memory-research-2026-08-23.md).
- [x] Explicit MCP `search(as_of=...)` historical snapshot over recorded and
  typed valid time, kept separate from ordinary current-only recall.
- [x] On-demand Published Memory revision-history inspection, bound to the
  selected current fact and kept out of the default current-only snapshot.
- [ ] `as_of` desktop query UX; the explicit MCP historical query is already
  implemented.
- [ ] Rebuildable event/topic segmentation plus adjacent-event retrieval,
  gated by a same-case comparison against minute and session retrieval units.
- [ ] Reviewed procedural memory derived only from repeated/adopted text
  workflows, templates, or checklists. It remains generation context and never
  executes computer actions.

The implemented backend contract, threat boundaries, and known limitations are
documented in [stage1-memory-daily-wrap.md](stage1-memory-daily-wrap.md). Stage 1
is not complete until the native review/source-drawer shell is built and its
privacy UX is validated on a signed macOS build.

### Stage 2 — Suggest and Prepare

- [x] Durable, evidence-bound Suggestion Kernel with an explicit lifecycle,
  projection digest, idempotent emission, compare-and-swap review transitions,
  current-policy checks, cooldown, quiet hours, score threshold, daily budget,
  and expiry.
- [x] First Work Resumption slice: a deterministic activity-gap detector,
  strict no-action artifact contract, opt-in daemon worker, bounded desktop
  inbox, inert evidence text, source drawer, dismiss, and acknowledge-only.
  The production worker now also uses a same-sample, suspend-aware capture
  breakpoint gate and invalidates a prepared card after any newer durable
  capture. This is a timing safety milestone, not proof that the active-
  conversation quality failure is solved.
- [x] Explicit park/resume cue: one immutable user-authored task label and next
  step, local projection digest, single-active-cue constraint, CAS
  resumed/dismissed transitions, strict protocol-v22 desktop review, and exact
  cue-plus-timeline suggestion evidence. It uses no model or network and does
  not infer that later activity belongs to the parked task. The research and
  falsification contract are recorded in
  [vida-park-resume-scout-2026-08-23.md](vida-park-resume-scout-2026-08-23.md).
- [ ] Privacy-filtered real-time detector path independent of the minute
  timeline. The first Work Resumption slice intentionally consumes verified
  timeline blocks and therefore does not satisfy this item.
- [ ] Run the versioned proactive fixture set and publish the reactive versus
  heuristic versus full-kernel quality/latency/interruption report. The local
  v1 evaluator and metric contract now exist under
  `benchmarks/vida-suggestions-v1`; the first dirty pilot improved Kernel
  precision from 0.40 to 0.50 after an empty-context abstention, but still
  fails the 0.85 gate and is not a publishable clean result. The bounded
  breakpoint-gate development result is recorded in
  `benchmarks/vida-suggestions-v1/analysis/capture-breakpoint-gate-dev.md`;
  the unchanged canonical decisions confirm comparability. A separate 12-case
  synthetic trace sweep shows the 20-second default improves the timing
  tradeoff but cannot reach the precision target without deferring useful
  moments, so timing-only optimization is stopped.
- [ ] Prompt Rescue prepared-artifact slice with explicit selection binding and
  no automatic submission. The external scout and implementation contract are
  complete. Its durable, lease-fenced backend now accepts an honestly labeled
  manual-paste source, records model/provider disclosure and provenance, calls
  a strict JSON/no-tool generator, and supports bounded retry, CAS edit, and
  hard delete. Protocol v5 now provides provider disclosure, manual-paste
  composition, asynchronous status, side-by-side review/edit, explicit copy,
  retry, and native-confirmed delete in the desktop shell. A global
  `Command-Shift-Space` path now captures a double-fenced `AXSelectedText`
  receipt before focusing OpenChronicle, persists its app/window/element/range
  binding, and never falls back to clipboard or whole-field text. A live signed
  AppKit/TCC fixture now passes exact, empty, secure-field, and metadata-first
  policy-denial checks; the scoped record is
  [vida-prompt-rescue-live-selection-2026-08-09.md](vida-prompt-rescue-live-selection-2026-08-09.md).
  TextEdit, Notes, and a local Safari textarea now pass exact-range acceptance.
  VS Code was not installed on the acceptance machine; its compatibility, a
  real-app focus-change stress case, and a reachable-provider quality report
  remain before this checkbox can close.
  The frozen 17-case adversarial dataset, deterministic grader, and raw-input
  negative-control report now exist under `benchmarks/vida-prompt-rescue-v1`.
  Its explicit provider runner shares the production template, no-tool call,
  strict output validator, model/location disclosure, and closed failure codes.
  A reachable enabled provider is still required to publish a real comparison;
  the next selection gate is the real-application compatibility matrix.
- [ ] Reply Rescue and Résumé Rescue with exact target/source binding and no
  send/submit capability. Clean-room competitor, standards, repository,
  source-binding, artifact, and frozen-evaluation contracts are now recorded in
  [vida-reply-rescue-scout-2026-08-09.md](vida-reply-rescue-scout-2026-08-09.md)
  and
  [vida-resume-rescue-scout-2026-08-09.md](vida-resume-rescue-scout-2026-08-09.md).
  Implementation starts with Reply Rescue's honestly labeled
  `manual_conversation` prepared artifact. That first slice is now implemented:
  a disabled-by-default supervised backend, immutable source/provenance digest,
  strict no-tool artifact, lease/CAS lifecycle, sanitized failures, native-
  confirmed delete, and desktop compose/review/edit/copy UI all preserve
  `manual_unverified` identity assurance. Manual edits clear the generated
  claim/answered-question ledger. The frozen 17-case adversarial evaluator now
  separates usefulness from schema/action, secret-echo, quoted-injection,
  recipient-warning, unresolved-context, and claim-ledger gates under
  `benchmarks/vida-reply-rescue-v1`. A distinct `Command-Shift-R` path now
  reuses Prompt Rescue's double-fenced exact AX selection receipt; it labels the
  result `selected_excerpt_unverified`, leaves recipients and reply mode
  unspecified, and does not claim mail-thread identity. Connector identities,
  live selection compatibility evidence, and provider quality evidence remain
  open. Résumé Rescue now has its first local source/projection foundation:
  immutable reviewed-profile versions, content-addressed opportunity revisions
  with digest-CAS supersession, closed fact/provenance/conflict schemas, and a
  deterministic exact-text projection whose requirement mappings remain
  explicitly `manual_mapping_unverified`. Profile or opportunity changes make
  prior projections unavailable. The frozen 18-case source/projection
  evaluator now separates exact factual preservation, provenance/conflict and
  action/injection gates from selection, missing-evidence, candidate-mapping,
  warning, and exclusion quality under `benchmarks/vida-resume-rescue-v1`.
  The exact projection passes the frozen local gate; the safe untailored
  base-profile comparator preserves facts but fails targeting quality. The
  protocol-v9 desktop slice now provides capability-scoped Rust commands,
  closed WebView decoders, profile/opportunity version review, exact fact and
  exact requirement selection, manual mapping, missing-evidence/conflict and
  provenance/confidentiality/ownership ledgers, and immutable projection
  review. A fixed A4 HTML renderer now derives an escaped, no-network document,
  parser-order plain-text mirror, and digest from that same current projection;
  the desktop renders it in an empty-permission sandbox and rejects active
  content at its protocol boundary. A frozen Chrome 151 + Poppler 26.04 macOS
  suite now passes hostile markup, Unicode/RTL, unbroken-token, and automatic
  three-page fixtures through two independent renders; text order, page boxes,
  font embedding, in-bounds glyph boxes, layout, and page pixels pass, followed
  by manual review of all six PNG pages. The report explicitly does not claim
  PDF-byte determinism or portability to another engine/OS/font set.
  Reviewed PDF/DOCX extraction is now complete through bounded isolated
  parsers, exact candidate provenance, explicit selection, review-digest
  replay, and profile CAS. Supervised no-tool rewriting remains open; its
  current competitor/repository scout and frozen 40-case safety contract are
  recorded in
  [vida-resume-supervised-rewrite-scout-2026-08-09.md](vida-resume-supervised-rewrite-scout-2026-08-09.md).
  Drafts and all send/submit authority remain later, separate capabilities.
  HTML export now uses a dedicated native command: it
  re-fetches the current document, digest-CAS checks the WebView request, opens
  the system save dialog, creates only a new private `.html` file, and never
  exposes general filesystem write or overwrite authority. A shared semantic
  document tree now drives preview and native export. Digest-bound DOCX passes
  pinned LibreOffice/Poppler visual QA, and guarded PDF passes the frozen
  Chrome 151/Poppler production fixture on the audited development host.
  Bundling or equivalently pinning the engine and fonts for distribution
  remains open.
  The protocol-v10 JSON Resume slice now pins the canonical v1 schema,
  generates initially-unselected exact/composite import candidates, ledgers
  contact, reference, URL, unknown, and unmapped fields, and exports only the
  selected projection with standard-mapping losses made explicit. A native
  picker keeps the source path out of the WebView, admission reparses the raw
  JSON and fences its review digest plus profile CAS, and the desktop requires
  explicit candidate selection before admission. Export requires a visible
  loss-ledger review and uses a digest-bound native command that creates only a
  new private `.json` file without overwrite authority. Raw import bytes are
  not copied into the profile store. The reviewed PDF/DOCX ingress slice now
  applies the same initially-unselected review and profile-CAS boundary with
  page/bounding-box or OOXML part/block provenance.
- [ ] Feedback aggregation and longitudinal proactive-quality evaluation. The
  first local slice now records a structured helpful outcome or one of five
  dismissal reasons, aggregates the latest 1,000 projection-valid terminal
  outcomes without suggestion content, and shows counts plus the acceptance
  rate in the desktop. This is instrumentation, not evidence that suggestion
  quality improved; representative longitudinal data and the frozen quality
  report remain open.

The current kernel is a local preparation plane, not an Action Plane. Its only
implemented proactive workflow is Work Resumption; Prompt Rescue and Reply
Rescue are explicitly initiated preparation workflows. Their backends reject
unknown artifact fields and any `action_capability` other than `none`.
Suggestions are disabled
by default, disappear when their evidence or policy authority changes, and a
desktop “accept” transition records acknowledgement only. This is an
implementation milestone, not a Stage 2 or Vida-parity completion claim.

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
8. Side-effect-free Suggestion Kernel and Work Resumption. **The first local,
   opt-in vertical slice and explicit user-authored park/resume cue are
   implemented and cross-stack tested; proactive fixture baselines, the
   independent real-time path, packaged UX acceptance, and the remaining Vida
   workflows are still open.**

Generic planning, connectors, and action execution intentionally start only
after the earlier gates pass.
