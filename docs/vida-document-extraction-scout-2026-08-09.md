# Résumé document extraction scout — 2026-08-09

This is a clean-room source and architecture survey for the next Résumé Rescue
milestone. It records behavior, versions, licenses, and boundaries observed in
public repositories. It is not a claim that any project is compatible with
Vida, nor an instruction to copy implementation code.

## Product boundary

The first document-ingress slice converts a user-selected PDF or DOCX into
untrusted, initially-unselected review candidates. Every candidate must retain
exact extracted text, a digest binding to the original bytes, and the strongest
location the format can support. Nothing enters the master profile until the
user selects it and supplies a fact ID, section, confidentiality, and ownership
scope. The source document is not an instruction stream, and extraction has no
network, tool, upload, application, or model authority.

This milestone does not promise semantic résumé parsing, OCR, PDF layout
reconstruction, DOCX pagination, ATS compatibility, or lossless round trips.
Those are separate claims and require separate evidence.

## Current repository survey

| Project | Pinned public state | What was inspected | Decision for this slice |
| --- | --- | --- | --- |
| [Microsoft MarkItDown](https://github.com/microsoft/markitdown/tree/fd239d5d2be43d9b68329730206b9312c7d5a388) | `fd239d5`, v0.1.7, MIT | The PDF extra uses `pdfminer.six` plus `pdfplumber`; DOCX uses Mammoth and an HTML-to-Markdown pass. The converter result exposes generated text rather than OpenChronicle's candidate-level source bindings. | Useful behavior and fixture reference. Do not add the whole general-purpose converter merely to reach its lower-level dependencies. |
| [Docling](https://github.com/docling-project/docling/tree/8050c42be2b179504445cb8f3c75655e27cbb662) | `8050c42`, v2.118.1, MIT | PDF/DOCX backends, structured document ground truth, page/layout pipelines, and OCR-oriented model integration. | Valuable later quality comparator. Too broad for the first bounded local ingress and would make model/runtime packaging part of a source-review milestone. |
| [xberg](https://github.com/xberg-io/xberg/tree/cbbdabaea6e4d1ba2dba0374b8f3c7a5b9d79547) | `cbbdaba`, v1.0.14, MIT | Rust features separate PDF, Office, OCR, and model/network integrations; the tree contains geometry, reading-order, two-column, password, and DOCX order tests. | Promising native/offline comparator and possible later packaged backend. Its fast-moving, broad Rust surface is not adopted before a frozen OpenChronicle corpus proves a concrete advantage. |
| [Unstructured](https://github.com/Unstructured-IO/unstructured/tree/114a1d511df49e8680e9608b14ee85dbd2c480dd) | `114a1d5`, v0.25.2, Apache-2.0 | General partitioning, OCR, NLP, and ETL-oriented document processing. | Behavior reference only; the dependency and capability surface exceeds reviewed résumé ingress. |
| [pdfplumber](https://github.com/jsvine/pdfplumber/tree/4c64b92d5caccd71c645e98e0fabb0c4dba7ff45) | `4c64b92`, v0.11.10, MIT | Page and word extraction exposes page number and `x0`, `x1`, `top`, `bottom`, and document-top geometry. Its documentation warns that text flow does not always equal logical order. | Selected PDF primitive for an isolated prototype because it can preserve page/bounding-box evidence. It must run behind byte, page, output, and time limits. |
| [pdfminer.six](https://github.com/pdfminer/pdfminer.six) | current public repository inspected 2026-08-09, MIT | Lower-level PDF text extraction used by pdfplumber and MarkItDown. | Transitive PDF primitive, not a semantic résumé parser. |
| [pypdf](https://github.com/py-pdf/pypdf) | v6.15.0 current release inspected 2026-08-09 | Pure-Python PDF manipulation and extraction. | Kept as an alternative comparator; pdfplumber exposes the word geometry required by the first evidence contract more directly. |
| [python-docx](https://github.com/python-openxml/python-docx/tree/e45454602b53e8e572b179ccf1c91093ec9f4ed7) | `e454546`, MIT | Paragraph/table object model and document editing API. | Behavior reference. The ingress boundary still needs explicit ZIP member, expansion, relationship, part, and XML limits, so a narrow bounded OOXML reader is easier to audit than admitting the full package as the security boundary. |
| [Mammoth](https://github.com/mwilliamson/python-mammoth) | current public repository inspected 2026-08-09, BSD-2-Clause | DOCX-to-HTML conversion used by MarkItDown. | Useful formatting comparator, but HTML conversion is not candidate provenance and does not replace explicit package limits. |
| [OpenResume](https://github.com/xitanggg/open-resume/tree/4f8255a2c763479837f69f1dccf2a3338730cd79) | `4f8255a`, AGPL-3.0 | Browser-side PDF parsing and resume-parser behavior. | Behavior-only reference. No source code is copied into the MIT project. |
| [Resume Matcher](https://github.com/srbhr/Resume-Matcher/tree/116f9cc3b00e1ac91734a6c2679bf41ea64a0edc) | `116f9cc`, v1.2.0, Apache-2.0 | Its parser writes bytes to a temporary file, uses MarkItDown for PDF/DOCX-to-Markdown, then asks an LLM for structured resume JSON and patches some dates from the raw Markdown. | Product-flow comparator. The LLM step cannot establish exact factual provenance and belongs only in the later supervised no-tool generation stage. |
| [Reactive Resume](https://github.com/AmruthPillai/Reactive-Resume/tree/88a19619daf5fd0fc09c73f5b19fa23860dbd230) | `88a1961`, v5.2.5, MIT | Structured editing/import and export review behavior. | Existing interoperability and UI reference; it is not a PDF/DOCX evidence extractor. |
| [CVAurum](https://github.com/akhil-dara/CVAurum/tree/e9f8993487f784c90ce2872e2de5599a1ce7dbe4) | `e9f8993`, MIT | Browser-local editing, preview/parser-text separation, and PDF/Word/JSON export claims. | UX comparator only; project claims are not independent extraction evidence. |
| [Marker](https://github.com/datalab-to/marker/tree/e1a6226adfaab4cd573cfa96e12d60905ee38036) and [OpenDataLoader PDF](https://github.com/opendataloader-project/opendataloader-pdf/tree/bec7c7c07944b810ea5c6542fc6e26a0288454d5) | `e1a6226` / `bec7c7c`, Apache-2.0 | High-fidelity PDF-to-structured-text/OCR-oriented systems. | Later scanned/complex-layout comparators, not first-slice dependencies. |

AGPL/GPL parsers and old, unmaintained resume-parser repositories were excluded
from implementation reuse. Star counts and self-reported ATS accuracy are not
quality evidence.

## Selected architecture

1. Rust owns the native file picker and reads only a regular, non-symlink file
   selected for this operation. It returns neither the path nor general
   filesystem authority to the WebView.
2. A bounded extraction worker receives bytes, detected kind, and a generated
   source ID. It has no provider credentials and no network or action API.
3. PDF extraction emits page-local text blocks with page number and bounding
   box evidence. It does not flatten uncertain multi-column reading order into
   a false exact-order claim.
4. DOCX extraction validates the ZIP central directory before reading parts,
   ignores external relationships, rejects active/macro-bearing content, and
   emits document-order paragraph/table blocks. `page=0` means pagination is
   unavailable for OOXML; the part and block location remains explicit.
5. Review output binds source SHA-256, format, extractor version, candidate
   digests, exact text, locators, omissions, and warnings. Every checkbox starts
   clear.
6. Admission reparses the original bytes and compares the review digest before
   appending `document_excerpt` facts under profile CAS. Raw source bytes are
   not copied into the profile store.

The first implementation may fail closed with `ocr_required` for image-only
PDFs. Adding OCR requires a separately pinned offline model/runtime, language
coverage tests, and an explicit lower-assurance label.

## Frozen risks and evaluation targets

- malformed, encrypted, oversized, too-many-page, empty, and image-only PDFs;
- single-column, multi-column, hostile-instruction, Unicode, RTL, long-token,
  duplicate-line, header/footer, table, and rotated-text PDFs;
- malformed OOXML, excessive member count, high expansion ratio, oversized
  uncompressed archive, macro or embedded-object parts, external relationships,
  headers/footers, nested tables, Unicode/RTL, and hostile-instruction DOCX;
- timeout, worker crash, truncated output, unknown result fields, source/review
  tampering, duplicate selections, fact-ID collisions, and profile CAS races;
- zero network requests, zero model/tool calls, zero silent auto-admission, and
  zero raw document retention in the profile store.

The committed manifest under
`benchmarks/vida-resume-rescue-v1/document-extraction/` is the normative local
development contract. Real résumés cannot enter the repository fixture corpus
without consent and redaction.
