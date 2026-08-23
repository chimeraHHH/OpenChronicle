# Explicit park/resume cue scout

Date: 2026-08-23

## Decision

The next Work Resumption slice will add one explicit, user-authored parked-task
cue. It is a complementary high-confidence input, not a replacement for the
existing gap detector and not a semantic claim that later activity belongs to
the same task.

The first slice will:

- let the user park one current task with a short label and an exact next step;
- store the cue locally with `parked`, `resumed`, or `dismissed` state and
  compare-and-swap versioning;
- keep cue content immutable after parking; correction is dismiss-and-recreate;
- surface it only after a later verified activity gap whose current block
  started after the cue was parked;
- bind the cue and the two timeline blocks as exact suggestion evidence;
- leave generic Work Resumption behavior unchanged when no cue is eligible;
- require an explicit user transition to mark the cue resumed or dismissed;
  acknowledging a suggestion will not close it; and
- use no model, network request, tool, or action capability.

Only one cue may be `parked` in this first slice. This matches the primary-task
interruption evidence and avoids pretending that the product can semantically
match several parked tasks to later screen activity. Multiple concurrent cues,
editing, complete cue-history deletion, and cue-derived quality claims remain
separate follow-ups.

## Paper evidence

- Trafton et al.,
  [Preparing to resume an interrupted task](https://doi.org/10.1016/S1071-5819(03)00023-5),
  found that prospective goal encoding during the interruption lag can reduce
  resumption cost. The product analogue is a short future-facing next step
  written before leaving, not a retrospective model guess.
- Altmann and Trafton,
  [Task Interruption: Resumption Lag and the Role of Cues](https://escholarship.org/uc/item/18b4r661),
  report that cues available before interruption facilitate performance after
  it. This supports preserving the user's exact cue rather than rewriting it.
- Parnin and DeLine,
  [Evaluating Cues for Resuming Interrupted Programming Tasks](https://www.microsoft.com/en-us/research/publication/evaluating-cues-for-resuming-interrupted-programming-tasks/),
  surveyed 371 programmers and found heavy reliance on notes across media,
  then compared automated chronological/aggregate cues with note-taking. This
  supports an explicit note-like cue as a real baseline rather than treating
  generated summaries as the default.
- Iqbal and Horvitz,
  [Disruption and Recovery of Computing Tasks](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/11/CHI_2007_Iqbal_Horvitz-1.pdf),
  studied suspension and resumption in field computing and examined behavior
  before suspension that appeared to prepare for later recovery. This supports
  capturing intent at the suspension boundary.
- Weber et al.,
  [AR Cue Reliability for Interrupted Task Resumption](https://doi.org/10.1145/3706598.3713685),
  found shorter resumption lags with cues but also changes in user strategy when
  cue reliability varied. The applicable lesson is to show only an exact
  user-authored cue and never synthesize a confident next step from uncertain
  activity continuity.

These studies do not establish that an OpenChronicle card improves real-world
outcomes. They justify the mechanism and its evaluation targets: cue recall,
wrong-task displays, resumption lag, resumption errors, and user dismissal
reasons.

## Repository and competitor evidence

Source inspection used pinned revisions so implementation details can be
rechecked:

- [Super Productivity at `bf3fd0b`](https://github.com/super-productivity/super-productivity/tree/bf3fd0b004b15a2fe884cfdb4ef7a5d0694a0443)
  keeps a `currentTaskId` and `lastCurrentTaskId`, while tasks carry notes,
  attachments, reminders, and time history. The reusable idea is a small
  explicit current-task state plus attached context; its full task-manager
  surface is out of scope.
- [Tacks at `d515678`](https://github.com/srmccray/tacks/tree/d515678a6bedb95777e67fa87da7d5bca3f0817f)
  separates mutable working notes from append-only comments and provides a
  bounded `prime` view of in-progress and ready tasks. The reusable idea is to
  keep current resumption state small and queryable instead of replaying all
  history.
- [`td` at `f2fa240`](https://github.com/rosgoo/td/tree/f2fa2408db34a4bd739bf1654490d89296188104)
  stores a task plan path, session identity, working directory, and last-opened
  time, then restores bounded plan context when resuming. The reusable idea is
  explicit durable continuity metadata; launching or controlling another agent
  session is out of scope.
- [ActivityWatch at `ce58382`](https://github.com/ActivityWatch/activitywatch/tree/ce5838296004605c9b8edcecad128e64e2718e31)
  uses local timestamped events, heartbeats, buckets, and timeline views. The
  reusable idea is to retain the existing local activity boundary as timing
  evidence, while keeping the user-authored cue as a separate intent source.

The scan also reviewed local-first task/note projects including
[Task Coach](https://github.com/taskcoach/taskcoach),
[Stacks](https://github.com/stacks-task-manager/stacks), and
[desk.md](https://github.com/v1lling/desk.md). They reinforce attaching notes,
files, and history to explicit tasks, but do not provide evidence that an
automatic same-task classifier is reliable enough for this slice.

## Data and protocol contract

The proposed local row is:

```text
resume_cue = {
  id,
  status: parked | resumed | dismissed,
  task_label,
  next_step,
  created_at,
  updated_at,
  version,
  projection_digest
}
```

The projection digest covers all fields. A suggestion references the exact
parked projection through `EvidenceRef(kind="resume_cue", id=...,`
`content_hash=projection_digest)`. A terminal transition changes the digest,
so an outstanding cue-assisted suggestion becomes ineligible on the next
snapshot rather than continuing to display stale text.

The cue-assisted artifact extends Work Resumption with a closed, versioned
`parked_cue` object containing only the exact label, exact next step, parked
time, and an explicit `user_authored: true` marker. Timeline excerpts remain
marked untrusted. `action_capability` stays `none`.

## Falsification and acceptance gates

The slice is accepted only if tests show:

1. generic Work Resumption output is byte-for-byte unchanged without a cue;
2. a cue parked before the current block produces one cue-assisted proposal;
3. a cue parked after that block starts is not retroactively attached;
4. a changed, terminal, missing, or corrupt cue cannot authorize a card;
5. cue creation and terminal transitions enforce compare-and-swap semantics;
6. the desktop rejects unknown cue/artifact fields and mismatched mutation
   identities;
7. no cue operation invokes a model, network, tool, clipboard, or filesystem
   write outside the existing local SQLite store; and
8. all existing suggestion, provenance, desktop, Rust, and build suites remain
   green.

The slice is instrumentation-ready, not quality-complete. Field evaluation must
separately label whether the reminder was for the intended task and whether it
reduced recovery effort; suggestion acknowledgement alone is not that label.

## Implementation update

The first slice is implemented on 2026-08-23. The local store, cue evidence
resolver, Work Resumption artifact v2, snapshot, protocol-v22 Python/Rust
bridge, desktop form/review, structured terminal transitions, and regression
tests follow the frozen contract above. Generic v1 Work Resumption artifacts
remain unchanged when no eligible cue exists. Representative field evaluation,
multiple concurrent cues, editing, and complete cue-history deletion remain
open and are not claimed by this implementation milestone.
