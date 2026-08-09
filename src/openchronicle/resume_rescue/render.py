"""Deterministic, no-network HTML preview for reviewed résumé projections."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

from ..provenance.models import canonical_digest
from .models import validate_artifact, validate_profile
from .store import ProfileVersion, ResumeProjection

RENDERER_VERSION = 1
TEMPLATE_ID = "openchronicle-classic-v1"
SECTION_LABELS = {
    "summary": "Summary",
    "experience": "Experience",
    "education": "Education",
    "skill": "Skills",
    "project": "Projects",
    "certification": "Certifications",
    "language": "Languages",
    "other": "Additional Information",
}

_STYLE = """
@page { size: A4; margin: 16mm 17mm; }
* { box-sizing: border-box; }
html { color: #17201d; background: #eef2ef; font-family: Arial, Helvetica, sans-serif; }
body { margin: 0; }
.resume-page { width: 210mm; min-height: 297mm; padding: 16mm 17mm; margin: 0 auto; background: white; }
.resume-header { padding-bottom: 5mm; border-bottom: 0.6mm solid #176b5c; }
.resume-name { margin: 0; font-size: 25pt; line-height: 1.1; letter-spacing: -0.03em; }
.resume-section { margin-top: 6mm; break-inside: auto; }
.resume-section-title { margin: 0 0 2.5mm; color: #176b5c; font-size: 11pt; letter-spacing: 0.08em; text-transform: uppercase; }
.resume-items { padding-left: 5mm; margin: 0; }
.resume-item { margin: 0 0 2.2mm; font-size: 10.5pt; line-height: 1.42; white-space: pre-line; break-inside: avoid; overflow-wrap: anywhere; }
@media print {
  html { background: white; }
  .resume-page { width: auto; min-height: auto; padding: 0; }
}
""".strip()


@dataclass(frozen=True, slots=True)
class ResumePreview:
    projection_id: str
    artifact_digest: str
    renderer_version: int
    template_id: str
    html: str
    plain_text: str
    document_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "projection_id": self.projection_id,
            "artifact_digest": self.artifact_digest,
            "renderer_version": self.renderer_version,
            "template_id": self.template_id,
            "html": self.html,
            "plain_text": self.plain_text,
            "document_digest": self.document_digest,
            "action_capability": "none",
        }


def render_preview(
    *, profile: ProfileVersion, projection: ResumeProjection
) -> ResumePreview:
    """Render one current projection without templates, scripts, or external assets."""

    normalized_profile = validate_profile(profile.profile)
    artifact = validate_artifact(projection.artifact)
    if (
        profile.profile_id != projection.profile_id
        or profile.version != projection.profile_version
        or profile.digest != projection.profile_digest
        or artifact["profile_binding"]
        != {"id": profile.profile_id, "version": profile.version, "digest": profile.digest}
    ):
        raise ValueError("resume preview profile binding differs")

    display_name = normalized_profile["display_name"]
    locale = normalized_profile["locale"] or "en"
    section_html: list[str] = []
    plain_lines = [display_name]
    for section in artifact["sections"]:
        label = SECTION_LABELS[section["kind"]]
        plain_lines.extend(["", label.upper()])
        items_html = []
        for item in section["items"]:
            fact_id = html.escape(item["fact_id"], quote=True)
            fact_text = html.escape(item["text"], quote=True)
            items_html.append(
                f'        <li class="resume-item" data-fact-id="{fact_id}">{fact_text}</li>'
            )
            plain_lines.append(f'- {item["text"]}')
        section_html.extend(
            [
                f'    <section class="resume-section" data-section="{section["kind"]}">',
                f'      <h2 class="resume-section-title">{label}</h2>',
                '      <ul class="resume-items">',
                *items_html,
                "      </ul>",
                "    </section>",
            ]
        )

    escaped_name = html.escape(display_name, quote=True)
    escaped_locale = html.escape(locale, quote=True)
    escaped_projection_id = html.escape(projection.id, quote=True)
    document = "\n".join(
        [
            "<!doctype html>",
            f'<html lang="{escaped_locale}">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="referrer" content="no-referrer">',
            "  <meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; font-src 'none'; connect-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'\">",
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            f"  <title>{escaped_name} — Résumé</title>",
            "  <style>",
            _STYLE,
            "  </style>",
            "</head>",
            "<body>",
            f'  <main class="resume-page" data-projection-id="{escaped_projection_id}">',
            '    <header class="resume-header">',
            f'      <h1 class="resume-name">{escaped_name}</h1>',
            "    </header>",
            *section_html,
            "  </main>",
            "</body>",
            "</html>",
            "",
        ]
    )
    plain_text = "\n".join(plain_lines) + "\n"
    document_digest = canonical_digest(
        {
            "schema": "resume-preview-v1",
            "projection_id": projection.id,
            "artifact_digest": projection.artifact_digest,
            "renderer_version": RENDERER_VERSION,
            "template_id": TEMPLATE_ID,
            "html": document,
            "plain_text": plain_text,
        }
    )
    return ResumePreview(
        projection_id=projection.id,
        artifact_digest=projection.artifact_digest,
        renderer_version=RENDERER_VERSION,
        template_id=TEMPLATE_ID,
        html=document,
        plain_text=plain_text,
        document_digest=document_digest,
    )
