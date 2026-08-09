# Vida ecosystem scout and baseline shortlist

Research date: **2026-08-09**. This is a clean-room scouting record for the
Vida-like roadmap. It uses public product pages, papers, and public source
repositories. Vendor claims are treated as claims, not benchmark results.

## Question and stop condition

The question was not "which app should OpenChronicle copy?" It was:

> Which externally demonstrated mechanisms and evaluation assets reduce the
> risk of building a local-first proactive desktop assistant from intuition?

The scout stops at a decision-ready shortlist: one reference set for local
context, one for proactive interaction, one for governed action, and one for
evaluation. It does not authorize importing third-party code or changing the
Memory Plane / Action Plane boundary.

## Vida parity anchor

Vida's current public site still names five completed workflows: Reply Rescue,
Prompt Rescue, Resume Rescue, Workspace Cleanup, and Daily Wrap. Its
[privacy policy](https://vida.app/privacy/) describes authorized access to app
names, window titles, visible text, selected content, controls, and workflow
signals, plus possible server and third-party-model processing. The parity
target is therefore the observable product loop, not Vida's private
implementation or cloud architecture:

```text
observe -> understand -> detect an opportunity -> suggest -> prepare
        -> obtain approval when needed -> act -> verify
```

OpenChronicle deliberately keeps local canonical evidence, inspectable memory,
and stricter action boundaries even when that differs from Vida.

## Repository evidence

| Reference | Publicly demonstrated mechanism | OpenChronicle decision |
|---|---|---|
| [screenpipe](https://github.com/screenpipe/screenpipe) | Local continuous screen/audio memory, searchable history, event-driven capture, scheduled "pipes", and app/window/content permissions | Reference its context API, permission vocabulary, and scheduled-agent ergonomics. Do not replace the AX-first observation model or import capture code. |
| [ActivityWatch](https://github.com/ActivityWatch/activitywatch) | Cross-platform, privacy-focused activity watchers, local event storage, timeline UI, AFK handling, and notification modules | Use as an overhead, retention, watcher-health, and timeline-navigation comparison. Its activity events are not sufficient evidence for Vida-like text workflows. |
| [Windrecorder](https://github.com/yuka-friends/Windrecorder) | Local changed-scene recording, OCR search, and app/title/URL skip rules | Reference scene-change and exclusion fixtures. Do not make pixels the default source when structured accessibility data exists. |
| [OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt) and [OpenAdapt Desktop](https://github.com/OpenAdaptAI/openadapt-desktop) | Modular capture/privacy/evaluation packages; a Tauri cockpit plus version-bound Python sidecar; record-review-run lifecycle; native install/launch/uninstall CI | Reuse the architectural tests: typed bridge, exact sidecar version, review gate, and packaged lifecycle matrix. Keep OpenChronicle's own schemas and permission policy. |
| [UI-TARS Desktop](https://github.com/bytedance/UI-TARS-desktop), [UFO](https://github.com/microsoft/UFO) | Screenshot/UI-grounded computer use, application agents, and desktop orchestration | Watch as Action Plane implementation references only. They are not a reason to grant screen content authority or ship autonomous mutation in Stage 2. |
| [GAIA](https://github.com/heygaia/gaia) | Scheduled/event-driven proactive workflows whose results land in a notification surface for approve/edit/dismiss | Adopt the durable notification/review state pattern, not its connector breadth or hosted architecture. |
| [Vellum Assistant](https://github.com/vellum-ai/vellum-assistant) | Structured source-attributed memory, hourly self-checks, active-conversation suppression, and type-specific staleness | Reference quiet-state suppression, staleness, and source attribution. Hourly polling is a comparison baseline, not the target detector architecture. |
| [Letta](https://github.com/letta-ai/letta), [Mem0](https://github.com/mem0ai/mem0) | Stateful-agent and memory-layer APIs with published evaluation paths | Compare retrieval and update behavior. Do not replace canonical Markdown with an opaque memory service. |
| [LongMemEval](https://github.com/xiaowu0162/longmemeval) and [LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2) | Longitudinal extraction, update, temporal reasoning, abstention, workflow knowledge, environment gotchas, answer accuracy, and query-latency evaluation | Build an adapter after the native fixture suite. Preserve its task/version metadata and report latency with accuracy. |
| [OSWorld-V2](https://github.com/xlang-ai/OSWorld-V2) | Version-pinned real-desktop task manifests, environments, observations, actions, and evaluators | Reserve a small pinned subset for Stage 3. Stage 2 has no action executor and must not claim OSWorld competence. |

### License and clean-room boundary

Repository visibility is not permission to transplant an architecture. The
shortlist contains MIT, MPL-2.0, Apache-style, and source-available projects
with different terms. Before any code reuse, record the exact upstream commit,
license, copied files, notices, and dependency impact. Until that review exists,
all entries above are design or evaluation references only.

## Product evidence

| Product | Useful public evidence | Consequence |
|---|---|---|
| [Microsoft Recall](https://learn.microsoft.com/en-us/windows/client-management/manage-recall) | Snapshot timeline/search, pause/delete, app/site filtering, and administrative data-loss-prevention controls | Pause, source filtering, deletion, and policy status must be first-class UX, not hidden configuration. |
| [Pieces Long-Term Memory](https://pieces.app/features/long-term-memory/ai-memory-assistant) | On-device workflow memory with source/time controls and MCP access | Consumer memory needs source and time visibility plus granular deletion, beyond candidate approval. |
| [Limitless](https://www.limitless.ai/privacy) | Pause/delete/export and configurable raw-audio retention, but a cloud account and consent obligations remain | Separate raw-evidence retention from derived memory retention and expose both. Do not infer that capture consent is solved by a toggle. |
| [Raycast AI Extensions](https://manual.raycast.com/ai/ai-extensions) | Tools are scoped by extension and require approval by default; a user may persist approval per exact tool | Confirms capability-scoped tools and default approval. OpenChronicle additionally binds approval to exact parameters and observed state. |
| [Notion AI](https://www.notion.com/help/category/notion-ai) | Workspace search, meeting notes, agent, and connected knowledge in one product surface | Reference unified retrieval UX, not its cloud workspace as a storage model. |
| [Granola](https://docs.granola.ai/help-center/getting-started/granola-101) | Meeting capture, notes, chat, folders, and post-meeting follow-up | Adjacent Daily Wrap/Reply preparation reference; not a continuous desktop-memory baseline. |

## Research evidence on interruption

[Need Help? Designing Proactive AI Assistants for Programming](https://arxiv.org/abs/2410.04596)
tests mixed-initiative suggestions in a shared work context. Its central warning
is as important as its positive result: relevance alone is insufficient;
presentation, timing, persistence, preview, dismissal, and user invocation
shape whether proactive help is useful. The OpenChronicle evaluation must
therefore score interruption cost and control, not just generated-answer
quality.

## Baseline resolution

### Selected now

1. **Reactive control:** identical ContextService and generator, but suggestions
   are created only after explicit user invocation.
2. **Heuristic proactive control:** deterministic opportunity rules, cooldown,
   and no model ranking. This reveals whether an LLM ranker improves precision.
3. **OpenChronicle Suggestion Kernel:** typed evidence, deterministic eligibility,
   rank/dedupe/budget, durable state, and prepared artifacts without side
   effects.
4. **Longitudinal memory adapter:** LongMemEval-V2 small tier after local privacy
   fixtures pass.
5. **Action evaluation:** a pinned OSWorld-V2 subset only after Stage 3 approval,
   verification, and undo exist.

### Rejected as the product anchor

- Replacing accessibility observations with unconditional screenshots: weaker
  semantic identity and a larger privacy surface.
- Replacing inspectable Markdown with a hosted or opaque memory service: breaks
  the local canonical-data decision.
- Starting with broad connectors or desktop control: measures reach before
  suggestion trust and violates the planned autonomy ladder.
- Treating repository stars, vendor "SOTA" labels, or demos as quality evidence.

### Watchlist, not a dependency

UI-TARS, UFO, screenpipe pipes, GAIA workflows, and Raycast tools remain moving
references. Their current behavior must be rechecked before an implementation
decision; no Stage 2 acceptance criterion depends on their availability.

## Directional decision

The next product anchor remains a **side-effect-free Suggestion Kernel**, after
the known Stage 0 bounded-history fix. Prompt Rescue and Work Resumption are the
first vertical slices because they can demonstrate the proactive loop without
external mutation. Reply Rescue and Résumé Rescue follow once target binding
and user-selected source scopes exist. Workspace Cleanup stays behind the
reversible Action Plane.

The executable acceptance rules are in
[vida-parity-evaluation-contract.md](vida-parity-evaluation-contract.md).
