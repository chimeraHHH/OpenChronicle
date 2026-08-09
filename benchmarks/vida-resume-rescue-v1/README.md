# OC-Vida Résumé Rescue v1

This frozen development benchmark evaluates OpenChronicle's clean-room Résumé
Rescue source and exact-projection contract. It is not a Vida benchmark and
contains no Vida prompts, outputs, implementation details, or claimed Vida
scores.

The 18 synthetic cases contain eight accepted and ten rejected inputs. They
cover evidence-backed selection, domain-evidence abstention, job-description
injection, confidential and shared-ownership warnings, multilingual text,
sparse profiles, unresolved conflicts, unknown or duplicated facts, wrong
sections, unselected mappings, non-excerpt requirements, closed-schema fields,
malformed provenance, credential-bearing URLs, and NUL text.

The committed grader separates factual/safety gates from targeting quality.
`candidate_supported` deliberately means only that the user mapped a displayed
fact to a requirement; `manual_mapping_unverified` prevents the deterministic
grader from pretending it has proved semantic entailment. Hiring fitness,
qualification, human preference, proprietary ATS behavior, and real-world
outcomes all remain outside this fixture.

Two deterministic variants run without a model:

- `base_profile` retains every non-conflicted reviewed fact and marks every job
  requirement as missing. It is a safe, untailored comparator.
- `deterministic_exact_projection` runs the production source validators and
  exact artifact builder twice, checking byte-equivalent semantic output.

```bash
uv run python -m openchronicle.evaluation.resume_rescue \
  --dataset benchmarks/vida-resume-rescue-v1/fixtures/cases.json \
  --contract benchmarks/vida-resume-rescue-v1/json/metric_contract.json \
  --output scratch/vida-resume-rescue-report.json
```

Generated reports belong under ignored `scratch/` until run provenance and the
fixture contract are reviewed. Later no-tool tailoring providers must add a
separately recorded corpus and human claim-level review; they cannot inherit
the exact projection's factual pass by similarity alone.

## Pinned render acceptance

The separate render suite passes each production preview through the exact
Chrome and Poppler versions frozen in `render/manifest.json`. It prints every
case twice, then checks A4 geometry, page-count bounds, embedded fonts, ordered
text preservation, in-page text boxes, bounded artifacts, and repeatable text,
layout, and per-page raster output. Its fixtures cover hostile markup,
multilingual and right-to-left text, a long unbroken token, and automatic
three-page flow.

```bash
uv run python -m openchronicle.evaluation.resume_render \
  --output-dir tmp/pdfs/resume-render-chrome151
```

The output directory must be new. A successful automated report deliberately
uses `release_gate_status=pending_visual_review`; inspect every generated PNG
for clipping, overlap, missing glyphs, spacing defects, and bad page
transitions before recording a local evidence decision. PDF bytes are not a
determinism gate because engine metadata can change while extracted text,
layout, and pixels remain identical. This audit neither enables product PDF
export nor proves fidelity on another browser, OS, or font installation.

## Reviewed document ingress

The `document-extraction/` contract freezes the next PDF/DOCX source boundary
before implementation. Its 20 synthetic cases cover page/bounding-box and
OOXML part/block provenance, uncertain reading order, image-only PDFs,
encryption, corruption, byte/page/ZIP expansion limits, external
relationships, active content, hostile instructions, digest tampering, and
profile CAS races. Candidates are review-only and initially unselected; the
contract grants no model, OCR, network, tool, upload, or application authority.
