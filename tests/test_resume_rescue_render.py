from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

from openchronicle import config as config_mod
from openchronicle.resume_rescue import ResumeRescueConflict, ResumeRescueService
from openchronicle.resume_rescue.render import RENDERER_VERSION, TEMPLATE_ID
from openchronicle.store import fts


class _ResumeTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._capture: str | None = None
        self.names: list[str] = []
        self.sections: list[str] = []
        self.facts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = dict(attrs).get("class", "") or ""
        if tag == "h1" and "resume-name" in classes:
            self._capture = "name"
        elif tag == "h2" and "resume-section-title" in classes:
            self._capture = "section"
        elif tag == "li" and "resume-item" in classes:
            self._capture = "fact"

    def handle_endtag(self, tag: str) -> None:
        if tag in {"h1", "h2", "li"}:
            self._capture = None

    def handle_data(self, data: str) -> None:
        if self._capture == "name":
            self.names.append(data)
        elif self._capture == "section":
            self.sections.append(data)
        elif self._capture == "fact":
            self.facts.append(data)


def _cfg() -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.resume_rescue.enabled = True
    return cfg


def _profile_fact(text: str) -> dict[str, object]:
    return {
        "id": "fact-hostile",
        "section": "experience",
        "text": text,
        "confidentiality": "private",
        "ownership_scope": "shared",
        "provenance": [
            {"kind": "manual_reviewed", "reviewed_at": "2026-08-09T08:00:00+08:00"}
        ],
    }


def test_resume_preview_is_deterministic_escaped_no_network_and_parse_ordered(
    ac_root: Path,
) -> None:
    hostile = '</li><script>alert("resume")</script> Built APIs.\nSecond line \u202etxt.exe'
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, _cfg())
        profile, _ = service.save_profile(
            profile_id="primary-profile",
            display_name='Ada <img src="https://evil.test/x">',
            locale="en-US",
            facts=[
                _profile_fact(hostile),
                {
                    **_profile_fact("Built production services in Python."),
                    "id": "fact-python",
                    "section": "skill",
                    "confidentiality": "public",
                    "ownership_scope": "individual",
                },
            ],
        )
        opportunity, _ = service.save_opportunity(
            employer="Example Labs",
            title="Reliability Engineer",
            source_text="Build reliable APIs.",
            captured_at="2026-08-09T12:30:00+08:00",
        )
        projection, _ = service.compose_exact(
            profile_id=profile.profile_id,
            opportunity_id=opportunity.id,
            sections=[
                {"kind": "experience", "fact_ids": ["fact-hostile"]},
                {"kind": "skill", "fact_ids": ["fact-python"]},
            ],
        )

        first = service.preview(projection.id)
        second = service.preview(projection.id)

    assert first == second
    assert first.renderer_version == RENDERER_VERSION == 1
    assert first.template_id == TEMPLATE_ID == "openchronicle-classic-v1"
    assert first.projection_id == projection.id
    assert first.artifact_digest == projection.artifact_digest
    assert len(first.document_digest) == 64
    assert first.to_dict()["action_capability"] == "none"
    assert "<script" not in first.html.lower()
    assert "<img" not in first.html.lower()
    assert "<form" not in first.html.lower()
    assert 'src="https://evil.test' not in first.html
    assert "&lt;script&gt;" in first.html
    assert "default-src 'none'" in first.html
    assert "data-fact-id=\"fact-hostile\"" in first.html

    parser = _ResumeTextParser()
    parser.feed(first.html)
    assert parser.names == ['Ada <img src="https://evil.test/x">']
    assert parser.sections == ["Experience", "Skills"]
    assert parser.facts == [hostile, "Built production services in Python."]
    assert first.plain_text == (
        'Ada <img src="https://evil.test/x">\n\n'
        "EXPERIENCE\n"
        f"- {hostile}\n\n"
        "SKILLS\n"
        "- Built production services in Python.\n"
    )


def test_resume_preview_refuses_stale_projection(ac_root: Path) -> None:
    with fts.cursor() as conn:
        service = ResumeRescueService(conn, _cfg())
        profile, _ = service.save_profile(
            profile_id="primary-profile",
            display_name="Ada Example",
            facts=[_profile_fact("Built reliable APIs.")],
        )
        opportunity, _ = service.save_opportunity(
            employer="Example Labs",
            title="Engineer",
            source_text="Build reliable APIs.",
        )
        projection, _ = service.compose_exact(
            profile_id=profile.profile_id,
            opportunity_id=opportunity.id,
            sections=[{"kind": "experience", "fact_ids": ["fact-hostile"]}],
        )
        service.save_profile(
            profile_id=profile.profile_id,
            display_name="Ada Example",
            facts=[_profile_fact("Built reviewed APIs.")],
            expected_version=profile.version,
        )

        with pytest.raises(ResumeRescueConflict, match="projection changed"):
            service.preview(projection.id)
