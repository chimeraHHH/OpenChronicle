# Work Resumption signal brief

Date: 2026-08-09

This is an algorithm-first optimization brief, not a paper-ready novelty claim.
The formal baseline registry is unavailable in the current tool session, so the
locally verified comparator remains `trusted_with_caveats` and unregistered.

## Objective contract

Real target: raise Work Resumption opportunity precision without losing current
evidence coverage, no-action safety, or the two positive cases in
`work-resumption-opportunity-v1`.

Trusted proxies are precision, recall, evidence coverage, unsupported-claim
rate, duplicate count, invalid interruptions per simulated user-day, and p95
decision latency from the existing metric contract.

False progress includes lexical “done” rules fitted to one fixture, app-name
blacklists, silently relabeling failures, delaying every card until recall
collapses, or optimizing accept rate while suggestion quality declines.

Hard constraints:

- screen-derived text remains untrusted data;
- no tools or action capability enter Stage 2;
- current capture policy and exact provenance remain mandatory;
- timing must use a suspend-aware monotonic clock sampled with the activity
  event, not a later unrelated sample; and
- the current dataset and metric definitions stay fixed during the first
  falsification run.

## Current board

The incumbent is an opt-in, deterministic gap detector composed with policy,
score, budget, cooldown, expiry, provenance, and artifact gates. The latest
three-repeat pilot reports Kernel precision 0.50, recall 1.00, evidence coverage
1.00, zero unsupported claims, zero semantic duplicates, and p95 decision
latency 1.137 ms on the reference host.

The decisive result is the empty-context ablation: requiring verified text on
both sides removed exactly one false positive and preserved both positives.
The active blockers are an already-resolved task and an active conversation.
Keyword completion detection and broad application suppression are stale routes
to ignore because neither has a defensible generalization contract.

## Related-work update

Reused coverage:

- [Need Help? Designing Proactive AI Assistants for Programming](https://arxiv.org/abs/2410.04596)
  separates suggestion relevance, feedback, timing, preview, dismissal, and
  manual invocation. Its implementation pauses suggestion timing during user
  interaction, resumes after input stops, and does not display a generated
  suggestion if the user becomes active again.
- [Vellum Assistant](https://github.com/vellum-ai/vellum-assistant) publicly
  describes periodic proactive checks that do not interrupt an active
  conversation. This is product/repository evidence, not an independent
  benchmark result.

New direct and adjacent evidence:

- Iqbal and Bailey,
  [Effects of Intelligent Notification Management on Users and Their Tasks](https://doi.org/10.1145/1357054.1357070),
  found that deferring notifications to detected task breakpoints reduced
  frustration and reaction time relative to immediate delivery, while content
  relevance affected which breakpoint was appropriate.
- Mozannar et al.,
  [When to Show a Suggestion?](https://ojs.aaai.org/index.php/AAAI/article/view/28878),
  use a utility-oriented conditional display cascade and human feedback to
  withhold likely rejected suggestions. Their ablation makes latent user state
  important, and they report a failure mode where acceptance-only reward can
  reduce suggestion quality.
- Trafton et al.,
  [Preparing to Resume an Interrupted Task](https://gregtrafton.com/papers/preparing.to.resume.pdf),
  show that prospective goal encoding during an interruption lag can reduce
  resumption time. This supports explicit user-authored resumption cues, but it
  does not solve unplanned gaps by itself.

Still missing: a representative, independently labeled OpenChronicle field set
for same-task continuity, resolution, and interruptibility. The 16 engineering
fixtures are sufficient to falsify unsafe rules, not to train or validate a
semantic ranker.

## Candidate frontier

### A. Capture breakpoint gate — selected immediate route

Maintain a privacy-filtered latest-activity signal from persisted capture
events. A candidate may be prepared, but it is displayed only after a short
quiescent interval; any new activity invalidates the pending display decision.
The timer uses the shared suspend-aware runtime clock sampled in the same event
envelope as the capture timestamp.

Why now: it directly targets the active-conversation failure and implements the
roadmap's missing real-time timing path. It is an infrastructure/mechanism
change, not a fixture-specific content rule.

Strongest objection: capture events may be too coarse to approximate typing or
task breakpoints. If event cadence cannot distinguish the existing positive
return from the active-conversation trace without excessive delay, abandon this
proxy rather than tuning around the fixture.

Cheapest falsification: add timestamped persisted-activity traces to the fixed
fixture set, preserve all content/labels/metrics, and compare the same kernel
with and without the gate. Success requires removing the active-conversation
false positive without losing either positive or exceeding the latency gate.

Anti-win: precision rises only because the positive trace was weakened,
notifications arrive outside the activation window, suspend/wake creates a
false quiet interval, or wall/monotonic samples come from different moments.

### B. Evidence continuity and resolution ranker — deferred

Use a typed, no-tool assessor over exact previous/current evidence to output
`same_task`, `resolved`, `uncertain`, and a calibrated display utility. Current
policy and evidence checks run before any model input; uncertain results abstain.
Accept/dismiss feedback is one signal, never the sole objective.

Why not now: the current fixtures are too small and synthetic to validate the
latent state highlighted by Mozannar et al. A ranker could memorize labels or
turn prompt injection into a display decision without a larger adversarial set.

Cheapest future falsification: frozen provider stubs for schema/safety plus an
independently labeled local corpus for accuracy and calibration. Abandon if it
cannot beat the deterministic kernel at the same precision/evidence gates.

### C. Explicit park/resume cue — retained complementary route

Let the user explicitly park a task with a reviewed next step before an
interruption, creating a high-confidence resumption cue inspired by the
interruption-lag evidence. This is highly inspectable and local.

Why it did not win: it cannot help with unplanned interruptions and therefore
does not replace proactive Work Resumption. It remains a useful reactive
fallback and a source of future clean labels.

## Selected idea and handoff

Selected direction: a same-sample, monotonic Capture Breakpoint Gate, followed
later by a separately evaluated semantic continuity ranker.

Core hypothesis: a real-time quiescence gate can suppress active-conversation
interruptions while preserving the current clean-return opportunities and all
hard safety metrics.

Prerequisite: repair the known capture/session clock boundary so a persisted
activity event carries its wall timestamp and monotonic tick from one sampling
point. Building the gate on the current post-persistence resample would create a
suspend/wake false-quiet bug.

Minimal validation:

1. same-sample clock envelope and suspend/wake regression;
2. active/quiet timestamped fixture traces without changing labels;
3. gate/no-gate comparison under the existing metric contract;
4. full suggestion, daemon, privacy, and clock regression suites.

Abandonment condition: if capture activity cannot separate active conversation
from a useful return without losing either positive or delaying beyond the
activation window, stop this route and require a richer explicit input signal.

Bounded-result update: the production clock envelope and breakpoint gate are
implemented. A separate 12-case synthetic trace sweep found that 20 seconds
improves display precision from 0.333 to 0.750 but defers one of four positives;
30 seconds reaches 1.00 precision only by reducing snapshot recall to 0.50 and
adding up to 18 seconds of delay on the positive set. Because the fixture labels
are engineering hypotheses and the timing distributions overlap, this route is
retained as a safety constraint but stopped as the sole quality optimizer.

Next stage: an explicit park/resume cue, plus broader external scouting for a
separately evaluated continuity/resolution signal. The remaining canonical
active-conversation and already-resolved false positives stay visible and are
not claimed solved.

Feedback instrumentation update: the desktop now asks for one structured
dismissal reason (`not_relevant`, `wrong_timing`, `already_resolved`,
`too_vague`, or `other`) and records `helpful` on acknowledgement. A local,
content-free summary groups the latest 1,000 valid terminal outcomes. This
implements the measurement prerequisite highlighted by the related work, but
does not use acceptance as a quality label or close the need for an
independently labeled field set.
