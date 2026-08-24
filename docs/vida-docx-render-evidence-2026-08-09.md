# Résumé Rescue DOCX interoperability evidence

Evidence date: **2026-08-09**. This acceptance run uses committed synthetic
fixtures only. It does not contain a private résumé, make an ATS claim, or
claim identical pagination across Microsoft Word and LibreOffice.

## Pinned run

- Production renderer: `openchronicle-classic-v1`, renderer v1,
  `python-docx` 1.2.0.
- Independent renderer: LibreOffice
  `26.2.3.2 70e089b17412e4cb7773e41413306b17a2328c34`.
- Inspectors: Poppler `pdfinfo`, `pdftotext`, and `pdftoppm` 26.04.0.
- Fixture digest:
  `3d93931bb857f48e0524f332f2cd1b917ae6d790f4b5f056a6e90f8004a9d158`.
- Native-export manifest digest:
  `c22ce7f3ce36a245a916024d3674e99b02d52037d5bc9dea87bb3a24a65c1d92`.
- Local ignored report:
  `scratch/vida-docx-render-lo262-20260809-6/report.json`.

All four cases passed the automated A4, bounded-artifact, deterministic DOCX,
ordered-text, page-count, PDF-parse, and raster-completeness checks. The
deterministic DOCX hashes were:

| Case | Pages | DOCX SHA-256 | PNG SHA-256 |
|---|---:|---|---|
| hostile markup | 1 | `6c30aa8338af8c8dc50ee0517dbc1e381fca2f3ba89ee9086ce7254c803d7f14` | `9e156c127d52059ed52e6b17aa55c134e5628cf61eeef95c361e4cd9e54a9e8e` |
| CJK/Latin/RTL | 1 | `c0c3be40cf662fc1de2aeb79d88530bfc9346d0f8fe4ee1d99275480def868e9` | `3ec64b3cb1c4b0b1b5ebce8440663ff2e2e8247910eb46cbd37ed275f979b6cd` |
| long unbroken token | 1 | `73d5c1703f889d6b049e86d7ee7af6a3a6e3e95c2d695269e94d72097c02c2b2` | `111fb4d25a4641699ff8033cc9933bcc502977383ae8848995d0ba07bc1b8128` |
| automatic multipage | 2 | `6219ac02c54f0d1aff9119d8639c6fe99a35242b053cb0fc11847f7c90fe94b9` | page 1 `d30bc7586b5dd09e134c215f9dad738e161ca244187853ded18e4a915ce80ca8`; page 2 `7b34ca2c16906454a49fc3cfa9eeccd7a68f5a66e590841b5e43babcef5027d5` |

## Visual decision

Every generated PNG was inspected at original resolution. The decision is
**accepted for the supported development-host DOCX boundary**: no clipping,
overlap, missing glyphs, missing bullets, broken long-token flow, bad margins,
or orphaned multipage section heading was observed.

The first audit exposed missing bullets on pure-CJK list items because the
default python-docx template used a Symbol private-use glyph. Production now
normalizes the OOXML numbering definitions to a standard Unicode bullet with
explicit fonts; the accepted run is after that correction. The remaining
visible spacing around some CJK punctuation reflects host font/layout fallback
and is not claimed to be cross-suite pixel parity.

This evidence clears DOCX generation and native-save integration on the pinned
development host. Cross-platform Microsoft Word testing and bundled-font
policy remain separate release-package gates.
