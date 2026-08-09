# Reviewed document-extraction contract

This frozen development contract covers local PDF/DOCX ingress into Résumé
Rescue. It contains synthetic documents only and does not claim Vida, ATS,
OCR, or semantic résumé-parser parity.

The production result must be review-only: exact extracted blocks, source and
candidate digests, explicit locators, omissions, warnings, and no action
capability. Candidates start unselected. Admission must re-extract the original
bytes, bind the expected review digest, and append only the selected facts under
profile CAS. The raw document is not persisted in the profile store.

PDF page and bounding-box evidence is required when text exists. A DOCX is a
ZIP package rather than a paginated artifact, so the v1 locator uses `page=0`
plus part/block coordinates and must not invent page numbers. Image-only PDFs
fail closed with an OCR-required warning until a separate offline OCR contract
is accepted.

The cases in `cases.json` freeze admission, warning, locator, and action
expectations. Binary fixtures will be generated from reviewed deterministic
builders so no private résumé enters Git history. Independent Poppler checks
are required for generated PDF fixtures, following the repository's PDF
render-and-inspect workflow.
