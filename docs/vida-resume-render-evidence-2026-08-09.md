# Résumé Rescue render evidence — Chrome 151 on macOS

Review date: **2026-08-09**. This is local development evidence for one exact
browser, PDF parser, OS, and installed-font configuration. It is not a Vida
measurement, a cross-platform guarantee, or authorization to expose product
PDF export.

## Reproduction identity

- Renderer: `openchronicle-classic-v1`, version `1`
- Browser: `Google Chrome 151.0.7922.77`
- Browser executable SHA-256:
  `696e4277196f9bc4d6655285f617efb068a84d4c7fc498948f10d8a4e03f9663`
- Poppler: `pdfinfo`, `pdftotext`, `pdftoppm`, and `pdffonts` version `26.04.0`
- Manifest digest:
  `17f1e7943f8224414299f5aa3c388a804833d74e42832339a7db01d3a06776c4`
- Fixture digest:
  `3d93931bb857f48e0524f332f2cd1b917ae6d790f4b5f056a6e90f8004a9d158`
- Generated report SHA-256:
  `5c693d208ce8febf417077d5bfe320d70a87830934ac1466a35e530113d09b43`

Command:

```bash
uv run python -m openchronicle.evaluation.resume_render \
  --output-dir scratch/vida-resume-render-product-pdf-20260809
```

The generated report returned `automated_status=passed` and intentionally
retained `release_gate_status=pending_visual_review`. All ten required checks
passed for all four cases: A4 page geometry, artifact bounds, embedded fonts,
in-page text boxes, ordered text completeness, expected page count, parseable
PDF, repeat layout, repeat pixels, and repeat text.

## Manual page review

Six PNG pages were rendered at 144 DPI and reviewed individually at original
resolution. The review found no clipping, overlap, black boxes, missing glyphs,
unreadable text, or broken page transitions. Headers, footers, and page numbers
are intentionally absent for this résumé template and were absent on every
page.

| Case | Pages | Visual result | Page PNG SHA-256 |
|---|---:|---|---|
| `single-page-hostile-markup` | 1 | Pass; markup remained visible inert text and the ASCII hyphen line break remained legible. | `6da1e8f4ffbb264b2a01f992081fe4be769c49eb25ff1a1796a4041b792dc070` |
| `unicode-and-directionality` | 1 | Pass; Chinese, Latin diacritics, Japanese, and Arabic were legible with consistent spacing. | `5fac858f7247d36f962e49920fa1c7e8a7745bd1cf774832f3fd6d0f35fcf135` |
| `long-unbroken-token` | 1 | Pass; the token wrapped inside the text column without clipping or margin overflow. | `b2bb37afdeee2b5c700312880b6dc13b8e760be76823da4d6fac3c22091ade41` |
| `automatic-multipage-flow` | 3 | Pass; items 18/19 and 39/40 separated cleanly across pages, item 42 remained complete, and Education followed without overlap. | `670acdb81c75676d2e5abba55dbe73d77ae58c0bbf54697d745ff0159f4497db`, `104caab9e4dbf1012693edd6ad552994fba29e60a00a88a9340075021bfebe2e`, `1176d88be1193f9461cdf412aa5ccf9280505afe373ba6b2357199683112a1cf` |

## Determinism and process findings

Every case was printed twice from identical production HTML. Normalized raw
text, ordered text coverage, page count, word geometry, and every page PNG were
identical. All four PDF byte streams differed, so this evidence makes no
PDF-byte determinism claim; browser metadata is outside the document-level
gate.

Chrome 151 on this macOS host acknowledged that the PDF was written but kept
its Headless application loop alive. The audit treats the bounded “bytes
written” acknowledgement plus PDF magic as output readiness, then sends TERM
and KILL to only the still-owned process group before reaping its leader. Both
runs of all four cases recorded `terminated_after_output_ready`; no renderer
process remained. The product PDF renderer and audit now share this owned-group
implementation. A separate regression proves that a TERM-resistant,
stdio-closed descendant is drained even after its direct leader exits cleanly.

## Boundary decision

The local Chrome 151 render/parse/layout gate is **accepted for the guarded
development-host product path**. Python re-fetches the current digest-bound
projection and validates the PDF with the pinned inspectors; Rust rechecks
identity, size, SHA-256, passive structure, and creates only a new private file.
The WebView receives no PDF bytes or filesystem path. The path fails closed
with `EXPORT_UNAVAILABLE` when any engine or inspector pin differs.

This is not yet a distributable PDF release claim. Packaging remains blocked
until the engine and fonts are bundled or equivalently replaced and packaged
app tests pass. Windows/Linux fidelity, accessibility tagging, additional
templates, and real résumé corpus evaluation remain separate gates.
