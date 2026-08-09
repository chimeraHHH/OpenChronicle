# Reply Rescue clean-room scout and implementation contract

Research date: **2026-08-09**. This is a point-in-time clean-room design record.
Vendor statements are treated as product claims. Public repositories and
standards are references, not permission to transplant code, prompts, or UX.

## Public parity anchor

Vida says Reply Rescue understands conversation context and generates a reply
the user can send immediately. It does not publish a source schema, thread
identity rule, prompt, model, recipient policy, or send boundary. OpenChronicle
therefore targets the observable behavior—an explicit conversation snapshot
becomes a reviewable reply—without claiming knowledge of Vida's private
implementation.

Source: [Vida public product page](https://web-prod.vida.app/).

## Product and standards evidence

| Reference | Publicly demonstrated mechanism | OpenChronicle consequence |
|---|---|---|
| [Outlook Draft with Copilot](https://support.microsoft.com/en-us/outlook/copilot-pages/draft-an-email-message-with-copilot-in-outlook) | A user explicitly starts a draft or reply, reviews it, can change tone or length, regenerates, keeps/inserts it, edits, and sends separately. | Generation and external delivery are different operations. The first slice ends at a local review/copy artifact. |
| [Outlook custom draft instructions](https://support.microsoft.com/en-US/Outlook/copilot-outlook/ask-copilot-to-make-email-drafts-sound-like-you) | Users can define tone and style instructions for generated drafts. | Style is explicit, reviewable local input. OpenChronicle does not silently mine an entire mailbox to infer voice. |
| [Gmail contextual Smart Reply](https://workspaceupdates.googleblog.com/2025/03/contextual-smart-replies-available-for-business-and-enterprise-customers.html) | Reply suggestions can use the whole thread and expose multiple response choices. | Context scope and alternatives improve usefulness, but only a connector with exact thread identity may claim whole-thread coverage. |
| [Gmail Help me write update](https://workspaceupdates.googleblog.com/2026/05/improvements-to-help-me-write-in-gmail.html) | Gmail can personalize suggestions with connected Gmail/Drive context and writing style. | Cross-source context must be separately authorized, enumerated, and bound. It is not ambient authority. |
| [Apple Intelligence](https://www.apple.com/apple-intelligence/?os=v) and [Smart Reply API](https://developer.apple.com/documentation/uikit/adopting-smart-reply-in-your-messaging-or-email-app?changes=_5) | Smart Reply identifies questions and offers relevant responses; Writing Tools supports user-directed rewriting. | The artifact should expose unanswered questions and missing context, not merely a fluent paragraph. |
| [Proton Scribe](https://proton.me/support/proton-scribe-writing-assistant) | Scribe can run locally, draft or improve selected text, and asks the user to review before insertion. | Keep a provider-location disclosure and preserve a local-provider path. Model output is never presumed accurate. |
| [RFC 5322](https://www.rfc-editor.org/rfc/rfc5322.html) | `Message-ID` identifies a message; `In-Reply-To` identifies its parent; `References` carries the thread chain. | Screen text cannot prove mail-thread identity. A future connector receipt must retain protocol identities, not just subject text. |
| [Gmail thread guide](https://developers.google.com/workspace/gmail/api/guides/threads), [message resource](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages), and [draft guide](https://developers.google.com/workspace/gmail/api/guides/drafts) | Gmail exposes message/thread IDs and headers; adding to a thread requires thread identity, reply headers, and a matching subject; drafts are separately managed resources. | A connector source needs account, immutable message, thread, revision/history, subject, recipients, and content digests. Draft creation remains a later explicit capability. |
| [Microsoft Graph message](https://learn.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0) and [`createReply`](https://learn.microsoft.com/en-us/graph/api/message-createreply?view=graph-rest-1.0) | Graph distinguishes message/conversation identities, revisions, recipients, reply creation, later draft update, and send. | Preserve immutable IDs/change keys where available and keep reply/reply-all plus normalized recipients inside the binding. Never collapse create-draft and send into one authority. |

The product pages above describe vendor capabilities, not independent quality
evidence. OpenChronicle will not claim equivalent accuracy from those claims.

## Repository evidence

| Reference | Mechanism worth studying | Deliberate exclusion |
|---|---|---|
| [Inbox Zero](https://github.com/elie222/inbox-zero) | Open-source mail integration, pre-drafted replies, user-defined rules, and personalized tone show the breadth of a connector-backed assistant. | Its inbox mutation, calendar, filing, unsubscribe, and send-adjacent capabilities are outside Stage 2. The first slice will not require OAuth, Postgres, or Redis. |
| [Himalaya](https://github.com/pimalaya/himalaya) | A Rust CLI treats message reading, reply composition, and sending as separate commands and supports machine-readable output. | It is a later connector-boundary reference, not a dependency and not permission to expose send in Reply Rescue. |
| [Cloudflare Agentic Inbox](https://github.com/cloudflare/agentic-inbox) | A self-hosted client/agent demonstrates provider-native thread and draft workflows. | A Cloudflare Workers/Durable Objects deployment is not local-first and is not adopted as OpenChronicle's trust boundary. |
| [OpenYak](https://github.com/openyak/openyak) | A local-first desktop agent produces reviewable artifacts such as follow-up emails. | Its broader agent/tool surface is not imported into the no-action preparation plane. |
| [outlook_skill](https://github.com/grapeot/outlook_skill) | Local retrieval/search/reply tooling illustrates how quickly reply generation can acquire live mailbox authority. | It is a connector threat-model reference only. No mailbox credential, send function, or tool is available to the initial model job. |

## Evaluation-neighborhood update

The frozen evaluator also takes structure—not fixtures, prompts, or scores—from
three primary security references:

| Reference | Evaluated risk | Reply Rescue consequence |
|---|---|---|
| [AgentDojo](https://github.com/ethz-spylab/agentdojo) and its [NeurIPS paper](https://arxiv.org/abs/2406.13352) | An extensible environment measures both ordinary task utility and prompt-injection security over untrusted tool data, including workspace/email-style tasks. | Report useful-reply quality separately from injection success. OpenChronicle narrows the consequence surface further by giving the reply generator no tools or actions. |
| [InjecAgent](https://github.com/uiuc-kang-lab/InjecAgent) and its [ACL paper](https://arxiv.org/abs/2403.02691) | 1,054 cases combine user tools with attacker tools and distinguish direct harm from data-stealing attacks over external content such as email. | Include quoted-message attacks, schema/role impersonation, secret exfiltration markers, and action-boundary escapes as distinct deterministic gates. |
| [Microsoft BIPIA](https://github.com/microsoft/BIPIA) | The benchmark includes an EmailQA task and composes benign external context with separate text/code attack payloads. | Keep benign conversation evidence and attack-success labels separate in fixtures; do not grade safety only from answer fluency. |

These projects evaluate broader systems than Reply Rescue and are not claimed
as directly comparable leaderboards. Their common lesson is structural: measure
benign utility and adversarial failure independently, preserve untrusted source
boundaries, and make consequential actions impossible or separately authorized.

## Selected source contract

The first source kind is `manual_conversation`. The user pastes or types a
reviewed excerpt and explicitly supplies participants, intended recipients,
reply mode, goal, tone, and any commitments. It is honest about what it cannot
know: pasted text does not prove a mailbox account, message, thread, recipient,
or revision.

A later `macos_selection` source may reuse Prompt Rescue's exact focused
selection receipt. It binds the application, process, window, element, range,
text, and policy digests, but it still does **not** prove a mail thread or the
actual To/Cc set. Its output must be labeled a reply to a selected excerpt,
never a connector-bound reply.

A future connector source must use a closed receipt containing:

- provider and stable account identity;
- immutable message ID plus Internet Message-ID where available;
- thread/conversation ID and provider revision (`historyId`, `changeKey`, or an
  equivalent closed value);
- `reply` or `reply_all`, normalized intended To/Cc recipients, and a recipient
  digest;
- normalized subject and subject digest;
- exact bounded message/thread-context digest and capture time; and
- provider policy, connector version, and authorization-scope digests.

No subject-only, sender-only, window-title-only, or body-text-only matching can
upgrade a manual/selection source into a connector receipt.

## Selected artifact contract

```text
explicit source -> immutable conversation snapshot -> deterministic preflight
                -> no-tool model job -> strict JSON validation
                -> durable prepared reply -> review/edit/copy
```

Every prepared reply binds the immutable source plus:

- template version/digest and provider/model/location disclosure;
- source kind and explicit `identity_assurance`;
- intended participants, recipients, reply mode, goal, tone, and commitments;
- reply body;
- questions answered, questions still unresolved, missing context, and
  assumptions requiring review;
- factual/commitment claim ledger tied to source spans or explicit user input;
- warnings for reply-all exposure, new recipients, sensitive content, dates,
  money, legal promises, credentials, or attachments; and
- `action_capability: none`.

Quoted messages and pasted instructions are untrusted evidence. They cannot
change the schema, recipients, provider, tool policy, or action boundary. The
model receives no tools. The UI offers edit and explicit copy only: no paste,
draft creation, reply action, send, keyboard synthesis, or mailbox mutation.

## Invalidation and privacy

- The workflow is disabled by default on upgrade.
- A manual snapshot is immutable; edits create a new source/version.
- Exact-selection artifacts invalidate when their bound source or privacy
  policy changes.
- Connector artifacts invalidate on account/message/thread/revision,
  recipient, subject, content, or authorization-scope drift.
- Only user-reviewed style instructions and individually admitted examples may
  be used. Mailbox-wide style mining is excluded from this stage.
- Local providers may keep content on-device. Cloud egress is explicit and the
  provider/model/location is shown before queueing.
- Delete removes the source, artifact, edits, and provenance edges from the
  local database.

## Frozen evaluation contract

The first versioned fixture must cover:

- direct reply versus reply-all and silent recipient-set changes;
- missing recipients, ambiguous participant roles, aliases, groups, and Bcc;
- quoted prompt injection and fake system/provider instructions;
- stale source, changed thread, changed subject, changed recipients, and
  provider revision races;
- explicit dates, prices, legal promises, attachments, credentials, and
  sensitive/policy-excluded content;
- hallucinated facts, meetings, deadlines, discounts, attachments, and prior
  agreements;
- questions that must be answered, questions that should remain open, and
  cases where abstention is the correct result;
- style-memory conflict, unsafe mimicry, and instructions that would obscure
  material facts; and
- timeout, unavailable provider, malformed/oversized JSON, unknown fields,
  replay, edit races, deletion, and every attempted send/paste/tool action.

Quality metrics are intent/constraint preservation, supported-claim rate,
question coverage, recipient correctness, calibrated abstention, style
preference, edit distance after user review, and human preference versus the
raw/manual baseline. Hard gates are zero unsupported commitments, zero target
binding escapes, zero excluded-data egress, and zero external mutations.

## Rejected shortcuts

- Treating a selected email body or window title as proof of thread/recipients.
- Mining all historic email for style without explicit review and admission.
- Giving the generator a mailbox, clipboard, accessibility-write, or send tool.
- Creating a provider draft in the same operation as generation.
- Hiding unresolved questions behind a confident fluent reply.
- Measuring only tone or keyword overlap while ignoring claims and recipients.

## Implementation order

1. Add the `manual_conversation` immutable source, job, and prepared-artifact
   state machine with closed schemas and no action capability.
2. Add a deterministic stub plus frozen adversarial evaluator before enabling a
   real provider corpus.
3. Add desktop compose/review/edit/copy/retry/delete with visible source and
   identity-assurance labels.
4. Reuse the exact macOS selection adapter while preserving its weaker
   conversation-identity claim.
5. Only after separate connector threat modeling, add read-only Gmail/Graph
   snapshots; draft creation and send remain later capabilities.
