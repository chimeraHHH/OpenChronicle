# Native résumé export scout and acceptance decision

Research date: **2026-08-09**. This record covers PDF and DOCX generation for
the clean-room Résumé Rescue implementation. It is not a Vida implementation
disclosure or an ATS/hiring-effectiveness claim.

## Current product and repository evidence

| Reference | Pinned state | Observed mechanism | OpenChronicle decision |
|---|---|---|---|
| [Reactive Resume](https://github.com/AmruthPillai/Reactive-Resume/tree/ba1f46995067ac834ab90c64855393731625c4c6) | `ba1f469`, MIT; current main inspected 2026-08-09 | Its PDF package uses `@react-pdf/renderer` 4.6 and its preview path uses PDF.js. Its separate DOCX package uses `docx` 9.7.1. | Adopt the shared semantic document-tree invariant and format-specific renderers. Do not copy templates or move file-write authority into the WebView. |
| [ReportLab](https://github.com/MrBitBucket/reportlab-mirror/tree/5e0a36e3face0f8ecd1ed03cc4791a84981bd8c5) | `5e0a36e`, PyPI 5.0.0, BSD-3-Clause | Direct Python PDF generation with bounded flowables and explicit font registration. | Comparator for a future self-contained renderer. It is not selected for v1 because portable CJK/RTL shaping and bundled-font parity are not yet demonstrated. |
| [WeasyPrint](https://github.com/Kozea/WeasyPrint/tree/32873118e8a70ceef87643775c247384d64798ea) | `3287311`, v69.0, BSD-3-Clause | HTML/CSS paged-media renderer with Pango/font fallback. | Strong fidelity comparator, but native Cairo/Pango packaging is broader than the current desktop sidecar. |
| [Typst](https://github.com/typst/typst/releases/tag/v0.15.1) | v0.15.1, Apache-2.0 | Reproducible document language and Rust PDF renderer. | Promising self-contained follow-up; adopting a second template language before the exact projection contract is stable would create avoidable drift. |
| [python-docx](https://github.com/python-openxml/python-docx/tree/e45454602b53e8e572b179ccf1c91093ec9f4ed7) | v1.2.0, MIT | OOXML document construction with explicit section, paragraph, style, and core-property APIs. | Selected DOCX primitive. Repackage its OPC ZIP deterministically and audit the result as untrusted OOXML before release. |
| [dolanmiu/docx](https://github.com/dolanmiu/docx/tree/4934d310c724520ad9d3e7e6d5d47430664ea9f7) | `4934d31`, v9.7.1, MIT | JavaScript DOCX construction; used by current Reactive Resume. | Behavior comparator. Keeping construction in the trusted Python sidecar avoids exposing arbitrary document bytes or filesystem writes to the WebView. |
| [Chrome Headless](https://developer.chrome.com/docs/chromium/headless) and [DevTools `Page.printToPDF`](https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-printToPDF) | Chrome 151.0.7922.77 accepted locally | Prints the already accepted production HTML/CSS tree with browser font fallback and A4 paged media. | Selected development PDF engine because the exact binary already passed the frozen multilingual raster/text/geometry audit. Product export must verify its version and SHA-256 and remain unavailable on an unpinned engine. Shipping remains blocked until the engine is bundled or equivalently replaced. |

Reactive Resume's current architecture is important evidence against converting
one exported format into another: it generates PDF and DOCX separately from
structured résumé data. OpenChronicle likewise uses one closed semantic tree,
then renders HTML preview, PDF, DOCX, plain text, and export ledgers from that
tree. No renderer may query the master profile independently.

## Selected v1 boundary

The WebView supplies only a projection ID and the digest of the preview it just
reviewed. The Python service re-fetches the current projection and profile,
rebuilds the semantic tree, and rejects any digest change. It returns bounded
bytes plus their SHA-256 in a closed response. Rust validates the identity,
format, size, magic, and digest, opens a native save dialog, and creates a new
private file without overwrite authority.

PDF development export uses only the exact accepted Chrome 151 binary. The
renderer runs in a private temporary directory with networking, extensions,
background services, and external assets disabled; success still drains the
owned process group. The exported PDF must pass the same Poppler text, A4,
font, geometry, and raster checks as the existing preview audit. This enables a
reviewed local development workflow, not a distributable release claim.

DOCX export uses python-docx to construct the same name/section/item tree. It
sets A4 dimensions and explicit margins, removes external/action relationships,
sets stable core properties, and canonicalizes ZIP member order, timestamps,
permissions, and compression. Admission-style OOXML limits are reapplied to
the generated package. LibreOffice conversion is an independent QA tool, not a
runtime dependency or fidelity oracle.

## Deferred, explicit limits

- PDF remains fail-closed when the pinned engine is absent or changed. A signed
  app cannot ship until that engine is bundled or replaced by an equivalently
  audited self-contained renderer.
- DOCX pagination varies by Word/LibreOffice/fonts. OpenChronicle claims exact
  text and safe OOXML structure, not identical page breaks across office suites.
- PDF accessibility tags, editable templates, cover letters, contact-layout
  inference, and real-résumé corpus evaluation are separate milestones.
- Neither format adds upload, application, submission, email, or browser
  automation capability.
