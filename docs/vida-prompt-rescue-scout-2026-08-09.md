# Prompt Rescue clean-room scout and implementation contract

Research date: **2026-08-09**. This is a point-in-time clean-room design record.
Public product statements are treated as claims; repositories and papers are
references, not permission to transplant code or UX assets.

## Public parity anchor

Vida's public site says Prompt Rescue upgrades rough questions into
“production-grade prompts before AI sees them.” It does not publish a source
schema, prompt, evaluator, model, or submission boundary. OpenChronicle therefore
targets the observable behavior—explicit rough input becomes a reviewable
improved prompt—without claiming knowledge of Vida's private implementation.

Source: [Vida public product page](https://web-prod.vida.app/).

## Interaction and source-binding evidence

| Reference | Publicly demonstrated mechanism | OpenChronicle consequence |
|---|---|---|
| [Apple Writing Tools](https://support.apple.com/en-ca/guide/mac-help/mchldcd6c260/mac) | The user selects text, sees a rewrite, can compare with the original, retry, undo, revert, copy, or finish. Read-only sources produce a dialog rather than silent replacement. | Show source and proposal side by side. First release supports edit/copy only and never replaces text in another app. |
| [VS Code inline chat](https://code.visualstudio.com/docs/chat/inline-chat) | An explicit code selection scopes the request; the result appears as a diff with Keep/Undo. The active editor and potentially other workspace files can become context. | Source scope must be visible and closed. Extra project context is opt-in and separately listed, never silently inherited from ambient memory. |
| [Raycast AI Commands](https://manual.raycast.com/ai/ai-commands) | Commands generally consume the active selection or focused field, then expose copy, paste, or continued refinement. Its Quick Fix can replace text directly. | Reuse explicit invocation and preview/copy ergonomics. Reject automatic replacement for the Stage 2 slice. |
| [ChatGPT Work with Apps on macOS](https://help.openai.com/en/articles/10119604-work-with-apps-on-macos) | A visible banner identifies connected apps/content; selected text is prioritized and neighboring content may be included up to a limit. Included content becomes chat history. | Show exactly which source was imported and do not add neighboring text by default. Keep canonical input and prepared output local except for an explicitly enabled configured provider call. |
| [PowerToys Advanced Paste](https://learn.microsoft.com/en-ie/windows/powertoys/advanced-paste) and [source](https://github.com/microsoft/PowerToys) | Explicit clipboard activation can transform text; current versions support configured cloud or local models. | A manual-paste compatibility path is valid, but it is labeled `manual_paste`, not an exact application selection. |
| [Apple `NSPasteboard`](https://developer.apple.com/documentation/appkit/nspasteboard) | The general pasteboard is shared across apps and participates in Universal Clipboard. Its change count tracks ownership/content changes. | Clipboard bytes and change count can bind an imported snapshot, but clipboard provenance cannot prove the originating app/window. |
| [Apple `kAXSelectedTextAttribute`](https://developer.apple.com/documentation/applicationservices/kaxselectedtextattribute) | Editable accessibility text objects expose the current selected text; noncontiguous selections have a separate ranges attribute. | A later macOS adapter can capture one exact focused selection. It must reject empty, multiple, unsupported, secure, policy-excluded, or identity-racing selections. |

### macOS selection-adapter update

The native adapter design was checked against Apple's
[`AXUIElementCopyAttributeValue`](https://developer.apple.com/documentation/applicationservices/1462085-axuielementcopyattributevalue),
[`kAXFocusedApplicationAttribute`](https://developer.apple.com/documentation/applicationservices/kaxfocusedapplicationattribute),
[`kAXFocusedWindowAttribute`](https://developer.apple.com/documentation/applicationservices/kaxfocusedwindowattribute),
[`frontmostApplication`](https://developer.apple.com/documentation/appkit/nsworkspace/frontmostapplication),
and
[`AXIsProcessTrustedWithOptions`](https://developer.apple.com/documentation/applicationservices/1459186-axisprocesstrustedwithoptions)
contracts. The API can report unsupported/no-value attributes, and frontmost
state is time-varying, so a single successful text read is not a sufficient
binding.

[AXSwift](https://github.com/tmandry/AXSwift) and
[Hammerspoon's accessibility element model](https://github.com/Hammerspoon/hammerspoon)
were reviewed as public implementation references. They reinforce explicit AX
error handling, element validity checks, and focused app/element traversal;
their automation actions are deliberately outside this read-only adapter.
The implemented probe therefore reads only `AXSelectedText` plus its range,
checks the secure-field ancestor chain, and rechecks frontmost app, focused
window, focused element, range, and text before emitting a receipt. It has no
`AXValue`/clipboard fallback and no AX write API. A global shortcut is required
for product integration because focusing an ordinary OpenChronicle button would
destroy the external focus being bound.

## Repository and evaluation evidence

| Reference | Mechanism worth reusing | Boundary |
|---|---|---|
| [Promptfoo](https://github.com/promptfoo/promptfoo) | Versioned prompt/model comparisons, local eval execution, CI, and injection/privacy red teaming | Reference its evaluation shape; do not add its permissive code-execution features to the product path. |
| [PromptSource](https://github.com/bigscience-workshop/promptsource) and [API model](https://github.com/bigscience-workshop/promptsource/blob/main/API_DOCUMENTATION.md) | A prompt is a template plus metadata, stable ID, input application, truncation, and declared metrics | Give every rescue template, input snapshot, provider configuration, and output a stable digest. |
| [ChainForge](https://github.com/ianarawjo/ChainForge) | Compare prompt permutations, models, parameters, and scoring functions over a fixed input table | Evaluate the rescue prompt on a frozen case matrix instead of accepting one appealing example. |
| [GitHub Models prompt files](https://docs.github.com/en/github-models/use-github-models/storing-prompts-in-github-repositories) | Repository-stored `.prompt.yml` configurations make prompt changes reviewable | Keep the OpenChronicle system template versioned in the repository, never only in code or provider UI. |
| [NVIDIA NeMo Guardrails](https://github.com/NVIDIA-NeMo/Guardrails) | Separate input/output validation rails, request IDs, and fail-closed configuration validation | Use deterministic local schema/policy checks before and after the model. Do not delegate action authority to a guardrail model. |

### Evaluation-neighborhood update

The frozen evaluator was additionally checked against four active primary
repositories before implementation:

| Reference | Mechanism retained | Deliberate exclusion |
|---|---|---|
| [NVIDIA garak](https://github.com/NVIDIA/garak) | Keep adversarial probes separate from detectors/generators and report the prompts that cause hits. | Do not load its broad plugin/model execution surface into the desktop product. |
| [OpenAI Evals](https://github.com/openai/evals) | Version the dataset and custom task-specific evaluation rather than relying on a generic score. | Do not require an OpenAI key or upload private user prompts for the committed offline gate. |
| [UK AI Security Institute Inspect](https://github.com/UKGovernmentBEIS/inspect_ai) | Separate dataset/task execution from scoring and retain reproducible run metadata. | Tool-use and model-graded scorers are outside this no-action deterministic safety gate. |
| [DeepEval](https://github.com/confident-ai/deepeval) | Treat LLM behavior as regression tests; retain explicit JSON-correctness and prompt-alignment dimensions. | LLM-as-judge results may be exploratory evidence, never the sole formal safety verdict. |

These references reinforce the chosen split: a frozen synthetic case file, a
separate metric contract, complete provider corpora with model/template
identity, and deterministic local scoring. They do not change the product
capability boundary.

## Selected product contract

Prompt Rescue is explicitly initiated. It is not a proactive ambient suggestion
and does not consume the timeline by default.

```text
explicit input -> immutable local source snapshot -> deterministic preflight
              -> no-tool model job -> strict JSON validation
              -> durable prepared artifact -> side-by-side review/edit/copy
```

The first cross-platform source is a trusted desktop form in which the user
pastes or types the rough prompt. It is labeled `manual_paste`; the UI never
claims an application/window origin. The macOS exact-selection adapter is a
separate acceptance gate before the workflow can be described as fully bound to
an external selection.

Every immutable input snapshot binds:

- schema version, stable ID, creation time, and source kind;
- exact UTF-8 input digest and bounded text;
- capture privacy-policy digest;
- optional user-stated target, audience, constraints, and desired output form;
- no ambient neighboring text unless the user explicitly adds and previews it.

Every prepared artifact binds the input snapshot plus:

- prompt-template version and digest;
- configured provider/model identity;
- improved prompt, explicit assumptions, missing-context questions, and a
  bounded change summary;
- generation status and sanitized failure state;
- `action_capability: none`.

The model receives no tools. Screen-derived or pasted content is delimited as
untrusted source material; text inside it cannot grant capabilities, alter the
output schema, or waive policy. Strict parsing rejects unknown fields,
oversized values, malformed JSON, empty improvements, and attempts to encode a
submission/tool call.

## Runtime and privacy contract

- Disabled by default on upgrade.
- Creating a request is fast and local. A supervised daemon job owns the
  provider call because the desktop bridge has a five-second hard deadline.
- A configured local provider may run without network egress. A cloud-backed
  provider is an explicit configuration choice and the UI must show its model
  identity before the user queues the input.
- Failure, timeout, cancellation, malformed output, or provider unavailability
  becomes a visible failed job, never a fabricated deterministic “improvement.”
- The input and result stay in the local 0600 database. Delete removes both and
  invalidates provenance edges; exports are not added in this slice.
- Copy is an explicit user action. Automatic paste, Enter/Return synthesis,
  accessibility writes, API submission, and tool execution are absent.

## Evaluation contract

The frozen Prompt Rescue fixture must include:

- rough but complete prompts whose intent and constraints must be preserved;
- ambiguous prompts that should ask questions rather than invent requirements;
- empty/whitespace, oversized, invalid Unicode/NUL, and duplicate input;
- selected text containing prompt injection, fake system messages, JSON/schema
  escape attempts, secrets, and instructions to call tools or submit itself;
- explicit target/audience/format constraints and conflicting context;
- provider timeout, unavailable, malformed JSON, unknown fields, and replay;
- source deletion/change before generation and before review; and
- proof that no UI/backend/native path can submit or replace external text.

Primary quality metrics are intent preservation, constraint preservation,
unsupported-assumption rate, schema-valid rate, evidence/source coverage,
duplicate-job rate, failure transparency, and human preference versus the raw
input. Safety gates remain zero tool calls, zero automatic submissions, zero
excluded-data egress, and zero successful source-text instruction overrides.

## Rejected shortcuts

- Treating clipboard content as proof of a specific app/window source.
- Automatically reading neighboring windows or long-term memory “for quality.”
- Replacing selected text in place before review.
- Calling a model synchronously through the five-second desktop bridge.
- Scoring improvement with the same one-shot model that generated it.
- Shipping a generic “make this prompt better” prompt without frozen fixtures,
  version metadata, and adversarial cases.

## Implementation order

1. **Implemented:** durable input/job/artifact state machine and strict
   template/output schema.
2. **Implemented:** manual-paste desktop form, provider disclosure, status,
   side-by-side preview, edit, copy, retry, and native-confirmed delete; no
   submit/paste command.
3. **Implemented:** frozen model-stub/adversarial evaluation and an explicit
   configured-provider runner that reuses the production template and strict
   output validator. A real provider corpus remains unreported because no
   enabled reachable provider was available in the local acceptance
   environment; no score is synthesized.
4. **Implemented, pending live acceptance:** macOS exact focused-selection
   adapter with secure-field/policy/identity fencing, durable binding, and a
   global shortcut that captures before focusing the review app. The live TCC
   app matrix is still required.

Only after step 4 passes may the roadmap's “explicit selection binding” item be
marked complete.
