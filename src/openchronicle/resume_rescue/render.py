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


@dataclass(frozen=True, slots=True)
class ResumeDocumentItem:
    fact_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ResumeDocumentSection:
    kind: str
    label: str
    items: tuple[ResumeDocumentItem, ...]


@dataclass(frozen=True, slots=True)
class ResumeDocumentTree:
    schema_version: int
    projection_id: str
    artifact_digest: str
    created_at: str
    display_name: str
    locale: str
    sections: tuple[ResumeDocumentSection, ...]

    def plain_text(self) -> str:
        lines = [self.display_name]
        for section in self.sections:
            lines.extend(["", section.label.upper()])
            lines.extend(f"- {item.text}" for item in section.items)
        return "\n".join(lines) + "\n"

    def digest_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "projection_id": self.projection_id,
            "artifact_digest": self.artifact_digest,
            "created_at": self.created_at,
            "display_name": self.display_name,
            "locale": self.locale,
            "sections": [
                {
                    "kind": section.kind,
                    "label": section.label,
                    "items": [
                        {"fact_id": item.fact_id, "text": item.text} for item in section.items
                    ],
                }
                for section in self.sections
            ],
        }


def build_document_tree(
    *, profile: ProfileVersion, projection: ResumeProjection
) -> ResumeDocumentTree:
    """Build the single closed tree consumed by every résumé renderer."""

    normalized_profile = validate_profile(profile.profile)
    artifact = validate_artifact(projection.artifact)
    if (
        profile.profile_id != projection.profile_id
        or profile.version != projection.profile_version
        or profile.digest != projection.profile_digest
        or artifact["profile_binding"]
        != {"id": profile.profile_id, "version": profile.version, "digest": profile.digest}
    ):
        raise ValueError("resume document profile binding differs")
    return ResumeDocumentTree(
        schema_version=1,
        projection_id=projection.id,
        artifact_digest=projection.artifact_digest,
        created_at=projection.created_at,
        display_name=normalized_profile["display_name"],
        locale=normalized_profile["locale"] or "en",
        sections=tuple(
            ResumeDocumentSection(
                kind=section["kind"],
                label=SECTION_LABELS[section["kind"]],
                items=tuple(
                    ResumeDocumentItem(fact_id=item["fact_id"], text=item["text"])
                    for item in section["items"]
                ),
            )
            for section in artifact["sections"]
        ),
    )


def render_preview(*, profile: ProfileVersion, projection: ResumeProjection) -> ResumePreview:
    """Render one current projection without templates, scripts, or external assets."""

    return render_preview_tree(build_document_tree(profile=profile, projection=projection))


def render_preview_tree(tree: ResumeDocumentTree) -> ResumePreview:
    """Render HTML and plain text from an already validated semantic tree."""

    if not isinstance(tree, ResumeDocumentTree) or tree.schema_version != 1:
        raise ValueError("resume document tree is invalid")
    section_html: list[str] = []
    for section in tree.sections:
        items_html = []
        for item in section.items:
            fact_id = html.escape(item.fact_id, quote=True)
            fact_text = html.escape(item.text, quote=True)
            items_html.append(
                f'        <li class="resume-item" data-fact-id="{fact_id}">{fact_text}</li>'
            )
        section_html.extend(
            [
                f'    <section class="resume-section" data-section="{section.kind}">',
                f'      <h2 class="resume-section-title">{section.label}</h2>',
                '      <ul class="resume-items">',
                *items_html,
                "      </ul>",
                "    </section>",
            ]
        )

    escaped_name = html.escape(tree.display_name, quote=True)
    escaped_locale = html.escape(tree.locale, quote=True)
    escaped_projection_id = html.escape(tree.projection_id, quote=True)
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
    plain_text = tree.plain_text()
    document_digest = canonical_digest(
        {
            "schema": "resume-preview-v1",
            "projection_id": tree.projection_id,
            "artifact_digest": tree.artifact_digest,
            "renderer_version": RENDERER_VERSION,
            "template_id": TEMPLATE_ID,
            "html": document,
            "plain_text": plain_text,
        }
    )
    return ResumePreview(
        projection_id=tree.projection_id,
        artifact_digest=tree.artifact_digest,
        renderer_version=RENDERER_VERSION,
        template_id=TEMPLATE_ID,
        html=document,
        plain_text=plain_text,
        document_digest=document_digest,
    )
