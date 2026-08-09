# Native PDF/DOCX export contract

This contract freezes the product boundary before native export is enabled.
Both formats start from the same current, reviewed semantic document tree. The
WebView never supplies document bytes or a filesystem path, and Rust alone
creates a new private file after re-fetch, digest, format, and size validation.

PDF acceptance extends the existing Chrome/Poppler render suite with product
bridge and native-save checks. DOCX acceptance requires a deterministic,
bounded, macro-free OPC ZIP; exact ordered text; A4 section geometry; no
external relationships; successful parsing by python-docx; and an independent
LibreOffice-to-PDF text/render inspection on the supported development host.

Generated artifacts belong under ignored temporary directories. No private
résumé or installed-font file enters Git history.

Run the pinned DOCX interoperability audit on the supported development host:

```bash
uv run python -m openchronicle.evaluation.resume_docx_render \
  --output-dir scratch/vida-docx-render-lo262 \
  --soffice /opt/homebrew/bin/soffice
```

The automated result intentionally remains `pending_visual_review`; inspect
every generated PNG before recording an evidence decision. PDF byte hashes may
vary because LibreOffice writes conversion metadata, while DOCX bytes and
rendered page pixels are the repeatability gates.
