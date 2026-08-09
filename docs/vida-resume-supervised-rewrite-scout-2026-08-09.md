# Résumé Rescue supervised-rewrite scout

Date: 2026-08-09 (Asia/Shanghai)

This note freezes the evidence and clean-room product boundary for the next
Résumé Rescue slice. It supplements, rather than replaces, the source,
projection, import, render, and native-export contracts already recorded in
`vida-resume-rescue-scout-2026-08-09.md`.

## Current public Vida anchor

Vida's current public site redirects to the Viskey & Vida desktop page and
describes a work companion that understands work context, learns how the user
works, works where the user already works, and improves with interaction. It
also says users can view, edit, or delete memory, pause collection, exclude
apps, and control what Vida can see. “Polish Resume” is one of several public
entry points rather than the whole product.

The public [SOTA use-case page](https://vida.app/sotacases/) marks **Resume
Rescue** as achieved and defines it as turning “raw experience, documents, and
context” into a production-grade résumé tailored for a real opportunity. This
is the parity anchor. It does not establish an ATS score, autonomous
application submission, permission to invent claims, or a particular model or
implementation.

## Commercial product evidence

The current market converges on a useful interaction model but not on a
trustworthy factual guarantee.

| Product evidence | Observed interaction | Boundary for OpenChronicle |
| --- | --- | --- |
| [Huntr AI Resume Tailor](https://huntr.co/product/resume-tailor) and its [May 2026 base/tailored-resume guide](https://help.huntr.co/en/articles/13005373-building-your-tailored-resume) | Starts from a base résumé and saved job, shows rewritten bullets with an explanation, supports individual accept/edit/ignore, live preview, undo, and job-specific versions. It also offers apply-all, match scores, autofill, and application tracking. | Adopt base→opportunity→proposal review and per-role versions. Do not adopt apply-all, score optimization, autofill, or submission authority. Vendor interview-rate claims are not acceptance evidence. |
| [Enhancv tailoring](https://enhancv.com/features/tailor-resume-to-job-description/) | Paste a job description, receive highlighted improvements, edit them, and save multiple tailored versions. The product says most suggestions are not applied automatically. | Adopt explicit proposals and preserved master content. Do not inherit its ATS or outcome claims. |
| [Rezi AI Keyword Targeting](https://www.rezi.ai/rezi-docs/ai-keyword-targeting-explained) | Separates a stored résumé from design, identifies missing job keywords, asks whether a keyword is relevant, and lets the user generate or edit a bullet. | A job term is a requirement, not evidence that the user has the skill. Missing-keyword display may inform a gap ledger only. |
| [Kickresume tailoring](https://www.kickresume.com/en/resume-tailoring/) | Rewrites the whole résumé against a job description and claims it uses only supplied information and makes nothing up, while the same page describes the result as about 80% accurate. | The accuracy caveat is disconfirming evidence for trusting a whole-document rewrite. Every proposal needs a local verifier and human review. |
| [Teal resume builder](https://www.tealhq.com/tools/resume-builder) | Keeps reusable experience, creates multiple résumé versions, attaches a job description, and offers keyword matching and AI edits. | Adopt reusable reviewed facts and immutable opportunity-specific artifacts. Do not treat a match score as factual proof. |
| [Simplify builder guide](https://help.simplify.jobs/en/help/articles/8040103-building-and-tailoring-your-resume-on-simplify) | Supports job-specific versions, whole-resume and per-bullet rewrites, missing-keyword insertion, chat, and optional propagation of edits back to a profile. | Do not mutate the reviewed master profile from a job-specific rewrite. Promotion into master facts is a separate reviewed source-ingress operation. |
| [Jobscan](https://www.jobscan.co/) | Emphasizes ATS-specific scores, keyword gaps, AI optimization, and auto-apply as part of a broader job-search system. | Use only transparent requirement coverage. Proprietary ATS emulation, auto-apply, and interview-outcome claims are outside Stage 2. |
| [Rezi](https://www.rezi.ai/), [Simplify](https://simplify.jobs/resume-builder), and [Enhancv](https://enhancv.com/) | All combine job targeting with a score or optimization feedback. | Broad convergence makes scores useful as a comparison signal, not as a correctness or parity gate. OpenChronicle keeps support and target coverage separate. |

## Pinned repository evidence

Repository metadata and default-branch commits were read from GitHub on the
date above. Links below are commit-pinned so later work does not silently
inherit a moving implementation.

| Repository pin | License | Evidence and limitation |
| --- | --- | --- |
| [Resume Matcher `116f9cc`](https://github.com/srbhr/Resume-Matcher/tree/116f9cc3b00e1ac91734a6c2679bf41ea64a0edc) | Apache-2.0 | Its [diff design](https://github.com/srbhr/Resume-Matcher/blob/116f9cc3b00e1ac91734a6c2679bf41ea64a0edc/docs/superpowers/specs/2026-03-23-diff-based-improvement-design.md) correctly identifies full-document regeneration as a hallucination surface and uses path/original-text checks. The production verifier makes invented metrics informational warnings, and its skill planner may accept a skill merely because the job description names it. OpenChronicle raises those cases to hard rejection and does not copy code. |
| [Reactive Resume `c292968`](https://github.com/AmruthPillai/Reactive-Resume/tree/c292968314390090514e3abae5d09281f8fcef39) | MIT | Its [builder AI guide](https://github.com/AmruthPillai/Reactive-Resume/blob/c292968314390090514e3abae5d09281f8fcef39/docs/guides/using-ai-in-the-builder.mdx) documents before/after and raw JSON Patch review, individual accept/reject, stale-patch failure, and the risk of inaccurate wording. Its [history guide](https://github.com/AmruthPillai/Reactive-Resume/blob/c292968314390090514e3abae5d09281f8fcef39/docs/guides/undoing-changes-and-version-history.mdx) records undo plus non-destructive snapshots around AI edits. Apply-all is intentionally not adopted in v1. |
| [resuml `e214d08`](https://github.com/phoinixi/resuml/tree/e214d088804b8fe8b0dacba9e03c89dcedb17e63) | ISC | Browser-local YAML, job comparison, and iterative tailoring are useful structured-data references. Direct agent/MCP editing toward a score is an explicit negative boundary. |
| [JobSync `06d994b`](https://github.com/Gsync/jobsync/tree/06d994b248b73ea57395254f5e3ccce582388ec5) | MIT | A self-hosted résumé review and job-match comparator. Its job discovery, chat, and tracking breadth is outside the focused prepared-artifact slice. |
| [OpenResume `4f8255a`](https://github.com/xitanggg/open-resume/tree/4f8255a2c763479837f69f1dccf2a3338730cd79) | AGPL-3.0 | Local-browser structured edit, live preview, PDF import, and parser-view behavior remain useful. It is behavior-only evidence; no AGPL source is reused. |
| [CVAurum `e9f8993`](https://github.com/akhil-dara/CVAurum/tree/e9f8993487f784c90ce2872e2de5599a1ce7dbe4) | MIT | Emerging browser-local implementation with IndexedDB, direct editing, undo/redo, deterministic checks, and PDF/DOCX/JSON export. Its ATS-vendor simulations and score remain project claims, not independent truth. |
| [ApplyPilot `e77ec11`](https://github.com/ibarrajo/ApplyPilot/tree/e77ec117fa5a9fdbbb1879ace8c780a2ca6378e5) | AGPL-3.0 | “Discover, score, tailor, and auto-apply” is the clearest counterexample. External-provider plaintext and autonomous submission are not allowed in this stage. |

## Selected product contract

The next slice is a **supervised rewrite proposal set**, not an editable chat,
an ATS optimizer, or a final application agent.

1. The user explicitly starts from one current exact projection and sees the
   configured model and local/remote-or-unknown provider location before any
   egress.
2. Only the selected reviewed facts, exact mapped requirement excerpts, and a
   fixed style instruction are sent. The full OpenChronicle history, excluded
   facts, conflicts, unrelated profile facts, paths, credentials, and prior
   private captures are absent.
3. The provider receives ordinary messages with JSON mode and **no tools**.
   Job descriptions and fact text are delimited as untrusted data.
4. The model returns only a closed proposal schema. Each proposal binds one
   selected `fact_id`, its exact original text, zero or more already-mapped
   requirement IDs, a proposed replacement, a rationale, and exact evidence
   fragments copied from that fact.
5. Local validation rejects unknown fields, duplicate IDs, unknown or
   unselected facts, stale bindings, original-text mismatch, unmapped
   requirements, missing evidence fragments, new/changed numbers, dates,
   money, percentages, emails, URLs, credential-like strings, or identity
   atoms. A job keyword never counts as evidence.
6. A surviving proposal is still a suggestion. The desktop shows before,
   after, rationale, requirement targets, fact provenance, and verifier result.
   v1 has individual accept/reject only; no apply-all.
7. Accepting one proposal uses a proposal-level digest/CAS boundary. It creates
   a new immutable opportunity-specific projection version and never mutates
   the master profile. A user-edited alternative is labeled user-authored and
   requires an explicit truth confirmation.
8. Restoring an earlier version is non-destructive: the older content becomes
   a new current projection while all prior versions remain auditable.
9. No operation can upload a résumé, fill a form, contact a recruiter, submit
   an application, or claim an ATS/hiring outcome. `action_capability` remains
   `none`.

## Why the first model pass is deliberately narrow

The deterministic exact projection already performs factual selection and
manual requirement mapping. The first model pass therefore improves ordering
and phrasing of one selected fact at a time. It cannot add a new bullet, skill,
metric, title, employer, date, certification, outcome, or requirement mapping.

This is narrower than several competitors and narrower than Resume Matcher.
That is intentional: current public evidence shows that review UX is mature,
while factual guarantees remain claims or warnings. A later experiment may
add an explicitly confirmed “new user fact” flow, but that would be source
ingress, not silent model tailoring.

## Frozen evaluation decisions

Hard gates:

- zero provider or artifact tool calls;
- zero excluded-history egress;
- zero admitted unknown fields;
- zero admitted unknown/unselected facts or unmapped requirements;
- zero admitted new protected claim atoms;
- zero stale proposal decisions;
- zero unreviewed or bulk-applied model edits;
- zero master-profile mutation; and
- zero upload, submit, send, autofill, or account authority.

Reported separately:

- proposal acceptance yield after deterministic verification;
- human factual-accuracy judgment;
- human preference over the exact baseline;
- relevant-fact emphasis and requirement-target usefulness;
- abstention rate; and
- provider latency and failure rate.

Human preference cannot override a failed safety gate, and target coverage
cannot be reported as factual support or an ATS outcome.

## Next implementation anchor

Implement the frozen contract in this order:

1. closed proposal/result validators and protected-atom verifier;
2. immutable proposal-set storage with current-source validation;
3. explicit no-tool provider job using the existing isolated provider runtime;
4. proposal-level accept/reject CAS and derived projection versions;
5. desktop before/after/provenance review without apply-all; and
6. deterministic adversarial evaluator followed by a separately disclosed
   real-provider quality run.

