# Résumé Rescue clean-room scout and implementation contract

Research date: **2026-08-09**. This is a point-in-time clean-room design record.
“Résumé Rescue” here means a job-application résumé workflow, not interruption
recovery. Vendor claims and open-source repositories are reference evidence,
not proof of hiring outcomes or permission to transplant implementations.

## Public parity anchor

Vida says Resume Rescue turns raw experience, documents, and context into a
production-grade résumé tailored to an opportunity. It does not publish a data
model, evidence policy, renderer, matching metric, or submission boundary.
OpenChronicle targets a user-started, job-targeted, reviewable résumé while
making factual support inspectable and never submitting an application.

Source: [Vida public product page](https://web-prod.vida.app/).

## Product and standards evidence

| Reference | Publicly demonstrated mechanism | OpenChronicle consequence |
|---|---|---|
| [Europass CV](https://europass.europa.eu/en/create-europass-cv) and [profile versus CV](https://europass.europa.eu/en/what-difference-between-europass-profile-and-cv) | A broad reusable profile holds experience; users select relevant facts to create multiple tailored CVs. | Store one evidence-backed master profile, then create immutable job-specific projections. Do not overwrite the source profile during tailoring. |
| [Teal tailoring guide](https://help.tealhq.com/en/articles/14435726-how-to-tailor-your-resume-for-a-specific-job) and [builder guide](https://help.tealhq.com/en/articles/14435724-how-to-build-your-resume-in-teal) | A comprehensive base résumé supplies bullets that are selected and adapted to a saved job description. | Preserve the job snapshot and selected fact IDs. Treat match scores and effectiveness as vendor claims, not outcome evidence. |
| [JSON Resume schema](https://github.com/jsonresume/jsonresume.org/tree/master/packages/schema) | The maintained MIT-licensed standard represents basics, work, education, skills, projects, and other sections in one JSON document, with validation and semantic versioning. The old `resume-schema` repository is archived and points to this monorepo. | Use a small versioned internal schema with an explicit JSON Resume export mapping later. Do not bind the database directly to an evolving permissive external schema. |

## Repository evidence

| Reference | Mechanism worth studying | Deliberate exclusion |
|---|---|---|
| [Reactive Resume](https://github.com/amruthpillai/reactive-resume) | MIT; structured editing, real-time preview, JSON Resume import, multiple export formats, self-hosting, and client-side PDF generation. | Its large web/auth/server stack is not needed for the local first slice. Layout UX is a reference; its data model is not factual provenance. |
| [OpenResume](https://github.com/xitanggg/open-resume) | Local-browser builder, real-time PDF preview, PDF parsing, and a parser-readability check demonstrate a useful import/render/parse loop. | It is AGPL-3.0 and therefore a behavior-only reference here. Its ATS and hiring-success statements are vendor claims, not independent validation. No source code is copied. |
| [RenderCV](https://github.com/rendercv/rendercv) | MIT; schema-driven YAML/JSON input, strict validation, reproducible/version-controlled documents, and typography-oriented rendering. | Its renderer/dependency stack is not adopted before a packaging and license review. A deterministic internal HTML preview is the smaller initial step. |
| [CVAurum](https://github.com/akhil-dara/cvaurum) | MIT; a small, emerging browser-local builder using IndexedDB, Zod-validated imports, JSON Resume round trips, a dedicated native-print route, and separate preview/parser-text views. | Its local structured edit/preview/import boundary is useful. The repository is new and small, and its ATS, parser-vendor emulation, score, and export-fidelity statements are project claims rather than independent quality evidence. |
| [Resume Matcher docs](https://github.com/srbhr/Resume-Matcher-Docs) | Apache-2.0 documentation for parsing résumés and job descriptions and comparing keywords/key terms/embeddings. | Match scores are exploratory relevance signals only. They cannot justify unsupported claims, keyword stuffing, or an “ATS pass” promise. |
| [CareerProof](https://github.com/wyl000bdml-sys/CareerProof) | MIT; an early template/agent-skill repository separates a multi-source evidence vault, confidentiality and ownership metadata, direct/transferable/unsupported matches, and missing evidence. | At the research date it has three commits and one star, so it is a mechanism prompt, not a mature baseline. OpenChronicle uses independently designed schemas and tests; no skill or prompt text is transplanted. |

## Research update: one document tree must drive preview and export

The 2026 [Reactive Resume v5.1 release](https://github.com/AmruthPillai/Reactive-Resume/releases/tag/v5.1.0)
moved PDF creation into the browser with `@react-pdf/renderer` and renders the
live preview through PDF.js from the same document tree. The stated reason is
to remove preview-versus-download drift. OpenChronicle adopts the invariant,
not that dependency stack: one deterministic renderer produces the preview
HTML, plain-text extraction mirror, digest, and later export input.

The old [JSON Resume `resume-cli` repository](https://github.com/jsonresume/resume-cli)
was archived in June 2026 and active development moved into the
[`jsonresume.org` monorepo](https://github.com/jsonresume/jsonresume.org/tree/master/packages/cli).
It still demonstrates schema validation followed by themed HTML/PDF export,
but the move is another reason to keep OpenChronicle's internal renderer and
schema version pinned rather than executing third-party themes as trusted code.

The [W3C CSS Paged Media Level 3 draft](https://www.w3.org/TR/css-page-3/)
defines page boxes, size, orientation, and margins, but remains a Working Draft
and leaves physical sheet handling to user agents. The first renderer therefore
uses a fixed, no-network HTML/CSS template and records its version. PDF is not
called deterministic until a pinned engine passes text-order, overflow,
pagination, and byte/document-diff acceptance on macOS packaging.

Chrome's maintained [Headless mode documentation](https://developer.chrome.com/docs/chromium/headless)
documents `--print-to-pdf` and the flag that removes date, URL, and page-number
headers and footers. The browser's
[DevTools `Page.printToPDF` contract](https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-printToPDF)
also exposes CSS page-size preference and print-background controls. These are
engine mechanisms, not fidelity guarantees. OpenChronicle therefore freezes an
exact Chrome and Poppler toolchain in a separate render manifest and requires
two-pass text, geometry, raster, and manual PNG review. The first macOS run is
recorded in [the Chrome 151 render evidence](vida-resume-render-evidence-2026-08-09.md).
It passed that local configuration, while intentionally leaving product PDF
export closed until the engine is packaged behind the native digest-bound
export boundary.

For local export, the current [Tauri 2 dialog API](https://v2.tauri.app/reference/javascript/dialog/#save)
explicitly recommends a dedicated native command when security matters instead
of exposing a general WebView file-write scope. OpenChronicle follows that
boundary: the WebView supplies only a projection ID and expected digest; Rust
re-fetches and validates the current renderer output, opens the native save
dialog, and creates a new `.html` file without overwrite authority.

## Research update: interoperability must expose loss

The canonical JSON Resume
[`schema.json` at commit `272929d`](https://github.com/jsonresume/jsonresume.org/blob/272929d51b450dbd5a0d242af24c60252904f405/packages/schema/schema.json)
is JSON Schema draft-07, identifies itself as version `v1.0.0`, and permits
additional properties at the root and nested objects. OpenChronicle therefore
accepts extension fields, but lists their JSON Pointer paths and digests their
unmapped values instead of trusting or silently discarding them.

Reactive Resume v5.2.5's current
[JSON Resume importer](https://github.com/AmruthPillai/Reactive-Resume/blob/88a19619daf5fd0fc09c73f5b19fa23860dbd230/packages/import/src/json-resume.tsx)
also uses loose objects. Its conversion filters work entries without company or
position, education without institution, projects without name, and several
other incomplete items. Its
[import guide](https://github.com/AmruthPillai/Reactive-Resume/blob/88a19619daf5fd0fc09c73f5b19fa23860dbd230/docs/guides/importing-resumes.mdx)
correctly warns users to review every imported section and says a native
Reactive Resume backup preserves more than JSON Resume. OpenChronicle makes
that loss machine-visible before admission rather than relying on a generic
post-import warning.

RenderCV v2.8 accepts YAML, JSON, and JSON5 through its own strongly validated
[input model](https://docs.rendercv.com/user_guide/yaml_input_structure/); that
does not make its schema JSON Resume-compatible. OpenResume's current AGPL tree
contains a behaviorally useful
[PDF parser](https://github.com/xitanggg/open-resume/tree/4f8255a2c763479837f69f1dccf2a3338730cd79/src/app/lib/parse-resume-from-pdf),
but no JSON Resume import path was found. These boundaries rule out pretending
that “JSON input” means round-trip interoperability.

The selected mapping consequently has two separate operations. Import produces
unreviewed exact-field or deterministic-composite candidates, source-field
bindings, unknown-field paths, and omission reasons; an explicit admission
creates new `json_resume_field` provenance. Export starts from one selected
projection rather than the master profile, uses only narrow standard mappings,
and emits an explicit loss ledger plus an untrusted namespaced extension for
exact OpenChronicle round trips. Other tools may ignore that extension, and the
export says so.

That boundary is now implemented through the desktop protocol. The native
picker accepts a bounded regular UTF-8 `.json` file without disclosing its path
to the WebView; every candidate starts unselected, exposes its exact source
fields, and requires an explicit fact ID, section, confidentiality, and
ownership decision. Admission reparses the original JSON and checks both the
review digest and target-profile CAS. Export is projection-scoped, shows the
loss ledger before enabling save, re-fetches the current document in Rust, and
creates only a new private digest-matched file. The implementation does not
claim lossless interoperability with tools that ignore the namespaced
extension.

## Research update: provenance helps only inside its evidence boundary

[Career-Aware Resume Tailoring via Multi-Source RAG with Provenance Tracking](https://arxiv.org/abs/2605.05257)
reports a 2026 pilot on one candidate and nine job descriptions. Retrieval over a
longitudinal career vault improved the paper's ATS-style score for six
domain-aligned descriptions, but reduced it for two descriptions whose required
domain evidence was absent. The paper explicitly says that these scores do not
reproduce proprietary ATS implementations and that provenance accuracy and
longitudinal utility were not quantitatively evaluated.

This is useful boundary evidence, not a product-effectiveness result. Résumé
Rescue therefore:

- abstains and displays `missing_evidence` when the admitted fact set does not
  support a requirement, rather than retrieving a superficially similar fact;
- keeps requirement coverage distinct from factual support and never reports a
  proprietary-ATS or hiring-probability claim;
- exposes provenance in the review interface instead of keeping it as hidden
  generation metadata; and
- requires multi-profile, adversarial, human-annotated evaluation before any
  claim about tailoring quality beyond the frozen fixture.

## Research update: evaluation signals are not factual proof

[ResumeFlow](https://arxiv.org/abs/2402.06221) proposes token-overlap and
embedding-similarity measures for job alignment and content preservation. The
paper itself describes low preservation plus high job alignment as a warning
that hallucination may have occurred; similarity does not establish
claim-level entailment. OpenChronicle may report relevance separately, but its
factual gate compares every rendered claim with its admitted fact IDs and
exact source revision.

A 2025 [NAACL observational study](https://aclanthology.org/2025.findings-naacl.270/)
compared zero-shot GPT-4 and human ratings over 736 real résumés and found only
minor correlation. Résumé Rescue therefore does not use an LLM match score as
a substitute for human preference, qualification, or hiring judgment.

The maintained [JSON Resume schema documentation](https://jsonresume.org/docs/013-schema-definitions)
also notes that the canonical package permits additional properties. That is
appropriate for an interchange ecosystem, but not for a security boundary.
OpenChronicle keeps a closed internal schema and will implement JSON Resume as
an explicit import/export mapping with unknown-field review, never as its
authorization model.

## Selected source model

Résumé Rescue has two independently versioned source sets.

The **master profile** is user-controlled structured data. Each fact and bullet
has a stable ID, exact value, review status, and one or more provenance entries:

- `manual_reviewed`: the user explicitly entered and approved the fact;
- `document_excerpt`: bounded source ID, content digest, page/section/span, and
  extraction method; or
- `reviewed_memory`: a specific long-term memory entry with provenance and the
  user's explicit admission into the résumé profile.

No capture, timeline inference, generic memory search, or model output becomes
a master fact automatically. Extracted candidates remain unverified until the
user reviews them. Conflicting dates, titles, employers, degrees, skills, and
metrics stay as visible conflicts; the system does not guess a winner.

The **opportunity snapshot** contains:

- stable ID, employer, title, source URL when present, capture time, exact text,
  and content digest;
- user-stated priorities and locale/language;
- extracted responsibilities, requirements, preferences, and keywords with
  source spans; and
- schema/template/extractor versions plus privacy-policy digest.

Job descriptions are ephemeral web content. Every tailored artifact binds the
immutable snapshot, never a live URL alone.

## Selected projection and artifact contract

```text
reviewed master facts + immutable opportunity snapshot
        -> deterministic eligibility/conflict preflight
        -> no-tool tailoring job -> strict structured validation
        -> claim-to-evidence verification
        -> deterministic preview -> review/export
```

A tailored résumé is a projection, not a new truth store. It may select,
reorder, shorten, and rephrase supported facts. Every visible claim retains the
master fact IDs and provenance supporting it. Any proposed claim that cannot be
entailed by admitted facts is rejected or shown as `missing_evidence`; it never
enters the rendered résumé silently.

The first artifact contains:

- schema/template/renderer and provider/model/location identities;
- master-profile version/digest and opportunity snapshot/digest;
- ordered sections and bullets with stable IDs;
- claim-to-evidence links and transformation kind;
- job requirement coverage separate from factual support;
- conflicts, missing evidence, unanswered questions, and excluded facts;
- deterministic document/preview digest; and
- `action_capability: none`.

The initial export target is structured JSON plus a deterministic HTML preview.
PDF/DOCX export follows only after render/parse/layout evaluation. Application
upload, form filling, account access, and submission are absent.

## Privacy and lifecycle

- The workflow is disabled by default on upgrade and starts only by an explicit
  user action.
- Contact details, employment history, education, compensation, citizenship,
  disability, and demographic data are sensitive. Source selection and cloud
  provider disclosure occur before egress.
- A local provider may operate on-device. Cloud use is opt-in and receives only
  the selected opportunity plus selected reviewed facts, not the entire
  OpenChronicle history.
- Changing a master fact, its review state/provenance, the job snapshot, source
  policy, template, or renderer invalidates the tailored artifact.
- Delete removes the job snapshot, projections, edits, renders, and provenance
  edges. Deleting a master fact invalidates every dependent projection.

## Frozen evaluation contract

The versioned fixture must cover:

- conflicting employment/education dates, overlapping roles, aliases, and
  current-role end dates;
- achievements with and without quantitative evidence;
- skills merely named in a job description but absent from the master profile;
- degree, certification, clearance, language, location, and work-authorization
  requirements with missing evidence;
- prompt injection and false instructions inside job descriptions/documents;
- sensitive or legally risky attributes that should remain excluded;
- stale/changed job descriptions, changed master facts, deleted evidence, and
  extraction/page-span failures;
- multilingual and Unicode content, long URLs, sparse profiles, duplicates,
  malformed dates, and oversized documents;
- model timeout/unavailability/malformed output/unknown fields/replay; and
  every attempted upload, application submission, or tool action.

Quality metrics are supported-claim rate, fact preservation, conflict recall,
job-requirement coverage, relevant-fact selection, unsupported-claim rate,
human preference, parse round-trip, text extraction order, layout overflow,
render determinism, and document diff stability. Hard gates are zero fabricated
facts, zero silent conflict resolution, zero excluded-data egress, and zero
external mutations. Keyword coverage is reported separately and never treated
as proof of ATS or hiring success.

## Rejected shortcuts

- Asking a model to rewrite an uploaded résumé without per-claim provenance.
- Treating a job description as evidence that the user has a listed skill.
- Inventing metrics, titles, dates, certifications, tools, or outcomes to fill
  gaps.
- Optimizing a single opaque “ATS score” or keyword density.
- Mutating the master profile while generating one job-specific version.
- Starting with form filling, account login, upload, or application submission.
- Copying AGPL implementation code into OpenChronicle.

## Implementation order

1. Define reviewed master-fact, provenance, opportunity-snapshot, projection,
   and claim-ledger schemas plus invalidation rules.
2. Add deterministic fixtures and an adversarial factual-support evaluator.
3. Add local structured profile/job composition and review; generation remains
   a supervised no-tool job.
4. Add deterministic HTML preview and render/parse/layout acceptance. The
   Chrome 151 macOS acceptance is complete; cross-platform packaging remains a
   separate export prerequisite.
5. Add JSON Resume import/export mapping, then reviewed PDF/DOCX extraction.
   The JSON Resume desktop slice is complete; document extraction is next.
6. Consider PDF/DOCX export after packaging evaluation. Application submission
   remains outside Stage 2.
