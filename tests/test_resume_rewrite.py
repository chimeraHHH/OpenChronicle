from __future__ import annotations

import copy

import pytest

from openchronicle.resume_rescue.models import build_exact_artifact
from openchronicle.resume_rescue.rewrite import (
    ResumeRewriteValidationError,
    rewrite_output_digest,
    validate_rewrite_artifact_for_egress,
    validate_rewrite_model_output,
)


def _fact(fact_id: str, text: str) -> dict[str, object]:
    return {
        "id": fact_id,
        "section": "experience",
        "text": text,
        "confidentiality": "private",
        "ownership_scope": "individual",
        "provenance": [
            {
                "kind": "manual_reviewed",
                "reviewed_at": "2026-08-09T08:00:00+08:00",
            }
        ],
    }


def _artifact() -> dict[str, object]:
    facts = [
        _fact(
            "fact-latency",
            "Built Python APIs at Acme Labs and reduced p95 latency by 40% in 2024.",
        ),
        _fact("fact-budget", "Managed a $2M budget and delivered 3 projects."),
        _fact("fact-cjk", "构建Python服务，延迟降低40%。"),
        _fact("fact-rtl", "طورت خدمات Python وخفضت زمن الاستجابة بنسبة 40%."),
    ]
    opportunity_text = (
        "Improve service reliability. Own delivery planning. "
        "需要可靠的Python服务经验。 خبرة في خدمات Python الموثوقة."
    )
    return build_exact_artifact(
        profile={
            "schema_version": 1,
            "profile_id": "rewrite-profile",
            "display_name": "Ada Example",
            "locale": "en-US",
            "facts": facts,
            "conflicts": [],
        },
        profile_version=1,
        profile_digest_value="a" * 64,
        opportunity={
            "schema_version": 1,
            "employer": "Target Labs",
            "title": "Reliability Engineer",
            "source_url": "",
            "source_text": opportunity_text,
            "priorities": [],
            "locale": "en-US",
            "captured_at": "2026-08-09T09:00:00+08:00",
        },
        opportunity_id="rewrite-opportunity",
        opportunity_digest_value="b" * 64,
        request={
            "schema_version": 1,
            "sections": [
                {
                    "kind": "experience",
                    "fact_ids": [
                        "fact-latency",
                        "fact-budget",
                        "fact-cjk",
                        "fact-rtl",
                    ],
                }
            ],
            "requirements": [
                {
                    "id": "req-reliability",
                    "text": "Improve service reliability.",
                    "fact_ids": ["fact-latency"],
                },
                {
                    "id": "req-planning",
                    "text": "Own delivery planning.",
                    "fact_ids": ["fact-budget"],
                },
                {
                    "id": "req-cjk",
                    "text": "需要可靠的Python服务经验。",
                    "fact_ids": ["fact-cjk"],
                },
                {
                    "id": "req-rtl",
                    "text": "خبرة في خدمات Python الموثوقة.",
                    "fact_ids": ["fact-rtl"],
                },
            ],
        },
    )


def _proposal(
    *,
    fact_id: str = "fact-latency",
    original: str = "Built Python APIs at Acme Labs and reduced p95 latency by 40% in 2024.",
    proposed: str = "Reduced p95 latency by 40% in 2024 at Acme Labs; built Python APIs.",
    requirement_ids: list[str] | None = None,
    evidence_fragments: list[str] | None = None,
) -> dict[str, object]:
    return {
        "proposal_id": f"proposal-{fact_id}",
        "operation": "replace_text",
        "section": "experience",
        "fact_id": fact_id,
        "original_text": original,
        "proposed_text": proposed,
        "rationale": "Emphasizes the mapped result without adding a claim.",
        "requirement_ids": requirement_ids or ["req-reliability"],
        "evidence_fragments": evidence_fragments or ["reduced p95 latency by 40%"],
    }


def _output(*proposals: dict[str, object]) -> dict[str, object]:
    return {"schema_version": 1, "proposals": list(proposals)}


def _raises(
    output: dict[str, object], code: str, *, artifact: dict[str, object] | None = None
) -> None:
    with pytest.raises(ResumeRewriteValidationError) as captured:
        validate_rewrite_model_output(output, artifact=artifact or _artifact())
    assert captured.value.code == code


def test_accepts_exactly_bound_rewrite_and_stable_digest() -> None:
    artifact = _artifact()
    output = _output(_proposal())

    normalized = validate_rewrite_model_output(output, artifact=artifact)

    assert normalized == output
    assert rewrite_output_digest(normalized) == rewrite_output_digest(copy.deepcopy(normalized))
    assert validate_rewrite_artifact_for_egress(artifact) == artifact


def test_empty_proposals_are_an_explicit_abstention() -> None:
    assert validate_rewrite_model_output(_output(), artifact=_artifact()) == _output()


def test_accepts_cjk_and_rtl_reordering_without_new_claim_atoms() -> None:
    cjk = _proposal(
        fact_id="fact-cjk",
        original="构建Python服务，延迟降低40%。",
        proposed="延迟降低40%；构建Python服务。",
        requirement_ids=["req-cjk"],
        evidence_fragments=["延迟降低40%"],
    )
    rtl = _proposal(
        fact_id="fact-rtl",
        original="طورت خدمات Python وخفضت زمن الاستجابة بنسبة 40%.",
        proposed="وخفضت زمن الاستجابة بنسبة 40%؛ طورت خدمات Python.",
        requirement_ids=["req-rtl"],
        evidence_fragments=["خفضت زمن الاستجابة بنسبة 40%"],
    )
    normalized = validate_rewrite_model_output(_output(cjk, rtl), artifact=_artifact())
    assert [item["fact_id"] for item in normalized["proposals"]] == ["fact-cjk", "fact-rtl"]


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value.update({"extra": True}), "invalid_output"),
        (lambda value: value.update({"schema_version": 2}), "invalid_output"),
        (lambda value: value["proposals"][0].update({"extra": True}), "invalid_output"),
        (
            lambda value: value["proposals"][0].update({"operation": "append"}),
            "invalid_output",
        ),
        (
            lambda value: value["proposals"][0].update({"section": "skill"}),
            "source_mismatch",
        ),
        (
            lambda value: value["proposals"][0].update({"fact_id": "unknown-fact"}),
            "source_mismatch",
        ),
        (
            lambda value: value["proposals"][0].update({"original_text": "changed"}),
            "source_mismatch",
        ),
        (
            lambda value: value["proposals"][0].update({"requirement_ids": ["req-planning"]}),
            "source_mismatch",
        ),
        (
            lambda value: value["proposals"][0].update(
                {"evidence_fragments": ["not an exact excerpt"]}
            ),
            "unsupported_claim",
        ),
        (
            lambda value: value["proposals"][0].update(
                {"proposed_text": value["proposals"][0]["original_text"]}
            ),
            "invalid_output",
        ),
    ],
)
def test_rejects_open_schema_and_broken_bindings(mutation, code: str) -> None:
    output = _output(_proposal())
    mutation(output)
    _raises(output, code)


@pytest.mark.parametrize(
    "proposed",
    [
        "Reduced p95 latency by 45% in 2024 at Acme Labs; built Python APIs.",
        "Reduced p95 latency by 40% in 2025 at Acme Labs; built Python APIs.",
        "Reduced p95 latency by 40% at Acme Labs; built Python APIs.",
        "Reduced p95 latency by 40% in 2024 at Beta Corp; built Python APIs.",
        "Reduced p95 latency by 40% in 2024 at Acme Labs; built Kubernetes APIs.",
        "Reduced p95 latency by 40% in 2024 at Acme Labs; ada@example.com.",
        "Reduced p95 latency by 40% in 2024 at https://example.test.",
    ],
)
def test_rejects_changed_removed_or_new_claim_atoms(proposed: str) -> None:
    _raises(_output(_proposal(proposed=proposed)), "unsupported_claim")


def test_rejects_changed_currency_atom() -> None:
    proposal = _proposal(
        fact_id="fact-budget",
        original="Managed a $2M budget and delivered 3 projects.",
        proposed="Delivered 3 projects and managed a $3M budget.",
        requirement_ids=["req-planning"],
        evidence_fragments=["delivered 3 projects"],
    )
    _raises(_output(proposal), "unsupported_claim")


def test_rejects_credential_echo_and_outcome_claim() -> None:
    credential = _proposal(
        proposed=(
            "Reduced p95 latency by 40% in 2024 at Acme Labs; "
            "built Python APIs with sk-abcdefgh1234."
        )
    )
    _raises(_output(credential), "secret_echo")

    outcome = _proposal()
    outcome["rationale"] = "Guaranteed to pass ATS and get an interview."
    _raises(_output(outcome), "unsupported_claim")


def test_rejects_secretlike_source_before_egress() -> None:
    artifact = _artifact()
    artifact["sections"][0]["items"][0]["text"] += " api_key=secret-value"
    with pytest.raises(ResumeRewriteValidationError) as captured:
        validate_rewrite_artifact_for_egress(artifact)
    assert captured.value.code in {"source_mismatch", "secret_echo"}


def test_rejects_duplicate_proposal_or_fact_target() -> None:
    first = _proposal()
    duplicate_id = _proposal(
        fact_id="fact-budget",
        original="Managed a $2M budget and delivered 3 projects.",
        proposed="Delivered 3 projects and managed a $2M budget.",
        requirement_ids=["req-planning"],
        evidence_fragments=["delivered 3 projects"],
    )
    duplicate_id["proposal_id"] = first["proposal_id"]
    _raises(_output(first, duplicate_id), "invalid_output")

    same_fact = copy.deepcopy(first)
    same_fact["proposal_id"] = "proposal-second"
    _raises(_output(first, same_fact), "invalid_output")
