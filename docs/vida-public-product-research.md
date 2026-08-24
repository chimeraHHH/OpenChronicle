# Vida public-product research and clean-room gap map

Research date: **2026-08-08**. This note records only information observable on
Vida's official public websites. It does not rely on a logged-in account,
downloaded binaries, reverse engineering, private APIs, or non-public product
materials. Marketing claims are recorded as vendor claims, not independent
benchmark results.

This document is a point-in-time input to OpenChronicle's clean-room roadmap.
Vida may change after the research date, so re-check the linked primary sources
before treating product availability, pricing, or policy language as current.

## Publicly observable product surface

| Area | Direct public evidence | Planning consequence |
|---|---|---|
| Web entry | The anonymous [Vida web app](https://vida.app/) exposes a chat-like home, Recent, Standard mode, and shortcuts for slides, résumé polishing, sheets, and industry research. Sending asks the user to sign in. | Vida has a unified assistant surface; OpenChronicle currently supplies memory to external agents rather than owning this surface. |
| Named completed cases | Vida labels Reply Rescue, Prompt Rescue, Resume Rescue, Workspace Cleanup, and Daily Wrap as “SOTA Achieved” on its [official cases page](https://vida.app/sotacases/). This is a vendor status label. | These five names define the public-parity workflow set, but do not specify an implementation or prove quality. |
| Named work in progress | Investment Research, Market Research, Product Research, Deck Builder, and Sheet Builder are labelled “Under Conquest” on the same page. | A home-page shortcut is not sufficient evidence that a workflow is complete. Do not make these Stage 2 acceptance targets. |
| Resume Rescue | Vida describes it as turning experience, documents, and context into a job-targeted résumé. | It is **not** interruption recovery. OpenChronicle must model résumé preparation and work resumption as separate workflows. |
| Proactivity | The official [marketing site](https://web-prod.vida.app/) names proactive context learning, proactive task execution, taste calibration, cross-app context stitching, opportunity detection, proposed actions, missing-information collection, and deliverables. | The largest product gap is the assistant loop above the memory plane: detect, rank, suggest, prepare, approve, execute, and verify. |
| Memory controls | The marketing site says an About Me panel can view, edit, or delete generated memory, with examples such as writing preferences and customer priority. | Candidate review alone is not feature parity. A consumer-facing view of published memory and its sources is still needed. |
| Action levels | The marketing FAQ says Vida may suggest, prepare, or act based on the task and settings and asks for confirmation on critical steps. The [terms](https://vida.app/terms/) use weaker language: the product may request confirmation for local files, sensitive information, or material consequences. | OpenChronicle keeps deterministic local policy as authoritative. Marketing text is not a sufficient safety contract. |
| Platforms | The [download page](https://vida.app/download/) lists macOS 13+ and Windows 10/11, while anonymous download options were still loading during this review. | The public page does not prove both binaries are currently downloadable. OpenChronicle remains macOS-only until a separately tested Windows port exists. |
| Account and commercial layer | The [login page](https://vida.app/login/) connects the account to a cloud identity. The [current pricing page](https://vida.app/pricing/) shows a free Basic tier and credit-backed Standard chat, while Pro, model details, and credit packs are unavailable in the anonymous state. | Cloud identity, hosted models, credits, and billing are not required for the local-first clean-room core and must not be inferred from stale pages. |

## Data flow supported by the legal text

Vida's [privacy policy](https://vida.app/privacy/) was last updated 2026-05-20
when checked. It supports the following high-level flow:

```text
account data, instructions, selected/uploaded content
                         +
authorized desktop context
(app/window names, visible text, controls and workflow signals)
                         |
                         v
                  Vida web/desktop
                         |
                         v
        Vida servers and third-party AI models
                         |
                         +--> cloud, authentication, analytics,
                         |    support, monitoring and integrations
                         v
 responses, summaries, drafts, recommendations, plans or device operations
                         |
                         v
 conversation/task history, preferences and execution/error logs may remain
```

The policy says authorized desktop context, instructions, selected or uploaded
content, and conversation history may be sent to third-party model providers.
It also allows cloud infrastructure, authentication, analytics, support, error
monitoring, and user-enabled integrations, and says processing may occur across
borders. On this evidence Vida is a hybrid local-desktop/cloud product; the
public text does not support describing it as fully local.

## Privacy promises and boundaries

Direct public promises include:

- the product will not access desktop context without user authorization;
- users may pause it or revoke operating-system permissions;
- the marketing site says users can exclude applications and review, edit, or
  delete memory;
- personal information is not sold;
- private content is not used to train general-purpose models without explicit
  consent, subject to the policy's anonymized/de-identified exception;
- transmission encryption and, where appropriate, storage encryption are used;
- error logs are typically retained no more than 90 days; and
- applicable access, correction, erasure, portability, objection, restriction,
  and consent-withdrawal rights are described.

Important unknowns remain. The public material does not name all model
providers or processing regions, promise end-to-end encryption, publish a fixed
desktop-context retention period, describe deletion propagation to every
provider copy, provide a deletion SLA, or cite an independent security audit.
Deleting a generated About Me memory also does not, by itself, prove deletion
of the source desktop context, conversation history, execution logs, error
logs, or provider-held copies. These are evidence gaps, not claims that the
product lacks internal controls.

## OpenChronicle gap map

| Vida public capability | OpenChronicle evidence today | Remaining clean-room work |
|---|---|---|
| Live cross-app context | Event-driven macOS AX capture, exact window identity, privacy firewall, timeline, and sessions | Complete unlocked real-app/TCC acceptance, packaging, and later platform work. |
| Long-term preferences and context | Inspectable Markdown, SQLite/FTS, typed candidates, provenance, review, and permanent-forget paths | Add one published-memory/About Me experience with safe edit, supersede, source inspection, export, and deletion. |
| Daily Wrap | Canonical local-day/timezone wrap, evidence references, revisioning, late-data coverage, CLI/MCP/desktop read | Complete signed desktop validation and improve the consumer experience; scheduling remains opt-in. |
| Prompt Rescue | Durable manual-paste jobs, strict no-tool prepared artifacts, provider disclosure, side-by-side desktop review/edit/copy, retry, and hard delete | Add the exact macOS focused-selection adapter and frozen adversarial/provider evaluation; never submit automatically. |
| Reply Rescue | No workflow | Detect reply context, use reviewed style memory, prepare a target-bound draft, and never send in the first pilot. |
| Résumé Rescue | No workflow; the old roadmap reused the name for interruption recovery | Build an explicitly user-started, evidence-backed résumé draft from selected experience/documents and reviewed memory. Never submit an application. |
| Work resumption | Timeline, sessions, Daily Wrap, and current-context retrieval provide inputs | Keep this OpenChronicle-specific workflow under a distinct name; prepare a last-state/next-step card. |
| Workspace Cleanup | Cleanup exists only for OpenChronicle-owned stores | Add scoped workspace inventory, previewable typed moves/renames, conflicts, exact approval, postconditions, and undo in the Action Plane. |
| Proactive assistant loop | Capture/classification/wrap workers are proactive data processing | Add real-time detectors, ranking, dedupe, cooldown, quiet hours, interruption budget, feedback, and suggestion state/UI. |
| Suggest / prepare / act | No Action Plane; desktop explicitly has no external actions | Add typed intents and artifacts first, then deterministic policy, bound approvals, capability broker, verification, audit, and undo. |
| Unified chat and task surface | Read-only MCP lets external agents query local memory | Decide later whether to own a chat/task shell; it is not required for the first trustworthy suggestion slice. |
| Accounts, sync, billing | Model-agnostic local-first configuration | Treat these as optional product choices, not parity requirements for the local-first architecture. |

## Product and architecture decisions

1. Preserve the local-first Memory Plane and separate Action Plane. Vida's
   public cloud data flow is evidence about its product, not a requirement to
   reproduce that architecture.
2. Treat screen-derived text as untrusted evidence. It may support a suggestion
   but may never grant a capability, weaken policy, or act as approval.
3. Implement the assistant loop before broad connectors: opportunity detection,
   durable suggestion state, evidence, ranking, trusted presentation, feedback,
   and prepared artifacts.
4. Keep public-parity names precise. “Résumé Rescue” means job-application
   résumé preparation. “Work Resumption” means reconstructing interrupted work.
5. Do not present existing OpenChronicle store cleanup as Workspace Cleanup.
   The latter operates on user-selected workspace files and belongs behind the
   future reversible Action Plane.
6. Prefer OpenChronicle's stronger auditable properties—local canonical data,
   source hashes, current-policy checks, deletion tombstones, and explicit
   approval—to undocumented parity guesses.

## Recommended implementation order

1. Finish Stage 0 process-level reliability and live macOS acceptance.
2. Finish the signed native memory/privacy/source-drawer shell.
3. Add a side-effect-free suggestion kernel and evaluation fixtures.
4. Ship Prompt Rescue and Work Resumption as the first Prepare workflows.
5. Add Reply Rescue and Résumé Rescue with explicit user initiation and target
   binding.
6. Add the reversible Action Plane, then implement Workspace Cleanup as its
   first scoped Act workflow.

This sequence reproduces the publicly observable product loop without copying
Vida's code, assets, brand, or private implementation.
