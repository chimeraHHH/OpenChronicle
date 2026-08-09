from __future__ import annotations

import copy
import json

import pytest

from openchronicle import config as config_mod
from openchronicle.resume_rescue.models import build_exact_artifact
from openchronicle.resume_rescue.rewrite import ResumeRewriteValidationError
from openchronicle.resume_rescue.rewrite_generation import (
    ResumeRewriteEgressDenied,
    build_rewrite_provider_input,
    generate_rewrite_output,
    provider_summary,
    rewrite_provider_input_digest,
    validate_rewrite_config,
    validate_rewrite_provider_input,
)


def _artifact() -> dict[str, object]:
    return build_exact_artifact(
        profile={
            "schema_version": 1,
            "profile_id": "rewrite-profile",
            "display_name": "Ada Example",
            "locale": "en-US",
            "facts": [
                {
                    "id": "fact-latency",
                    "section": "experience",
                    "text": (
                        "Built Python APIs at Acme Labs and reduced p95 latency by 40% in 2024."
                    ),
                    "confidentiality": "private",
                    "ownership_scope": "individual",
                    "provenance": [
                        {
                            "kind": "manual_reviewed",
                            "reviewed_at": "2026-08-09T08:00:00+08:00",
                        }
                    ],
                }
            ],
            "conflicts": [],
        },
        profile_version=1,
        profile_digest_value="a" * 64,
        opportunity={
            "schema_version": 1,
            "employer": "Target Labs",
            "title": "Reliability Engineer",
            "source_url": "",
            "source_text": "Improve service reliability.",
            "priorities": [],
            "locale": "en-US",
            "captured_at": "2026-08-09T09:00:00+08:00",
        },
        opportunity_id="rewrite-opportunity",
        opportunity_digest_value="b" * 64,
        request={
            "schema_version": 1,
            "sections": [{"kind": "experience", "fact_ids": ["fact-latency"]}],
            "requirements": [
                {
                    "id": "req-reliability",
                    "text": "Improve service reliability.",
                    "fact_ids": ["fact-latency"],
                }
            ],
        },
    )


def _proposal(
    *,
    proposed: str = "Reduced p95 latency by 40% in 2024 at Acme Labs; built Python APIs.",
) -> dict[str, object]:
    return {
        "proposal_id": "proposal-fact-latency",
        "operation": "replace_text",
        "section": "experience",
        "fact_id": "fact-latency",
        "original_text": ("Built Python APIs at Acme Labs and reduced p95 latency by 40% in 2024."),
        "proposed_text": proposed,
        "rationale": "Emphasizes the mapped result without adding a claim.",
        "requirement_ids": ["req-reliability"],
        "evidence_fragments": ["reduced p95 latency by 40%"],
    }


def _output(*proposals: dict[str, object]) -> dict[str, object]:
    return {"schema_version": 1, "proposals": list(proposals)}


class _Response:
    def __init__(self, value: object, *, tool_calls: list[object] | None = None) -> None:
        self.choices = [
            type(
                "Choice",
                (),
                {
                    "message": type(
                        "Message",
                        (),
                        {"content": json.dumps(value), "tool_calls": tool_calls},
                    )()
                },
            )
        ]


def _cfg(*, model: str = "ollama/test", base_url: str | None = None) -> config_mod.Config:
    cfg = config_mod.Config()
    cfg.resume_rescue.enabled = True
    cfg.resume_rescue.rewrite_enabled = True
    cfg.models["resume_rescue"] = config_mod.ModelConfig(model=model, base_url=base_url)
    return cfg


def test_provider_input_contains_only_selected_fact_text_and_mapped_requirements() -> None:
    artifact = _artifact()
    payload = build_rewrite_provider_input(artifact)

    assert set(payload) == {
        "schema_version",
        "workflow",
        "action_capability",
        "data_trust",
        "facts",
    }
    assert payload["action_capability"] == "none"
    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in (
        "profile_binding",
        "opportunity_binding",
        "excluded_fact_ids",
        "conflicts",
        "missing_evidence",
        "warnings",
        "provenance",
        "confidentiality",
        "ownership_scope",
        "Target Labs",
    ):
        assert forbidden not in serialized
    latency = next(item for item in payload["facts"] if item["fact_id"] == "fact-latency")
    assert latency["requirements"] == [
        {"requirement_id": "req-reliability", "text": "Improve service reliability."}
    ]
    assert validate_rewrite_provider_input(payload) == payload
    assert rewrite_provider_input_digest(payload) == rewrite_provider_input_digest(
        copy.deepcopy(payload)
    )


def test_one_requirement_may_bind_multiple_selected_facts() -> None:
    artifact = _artifact()
    second = copy.deepcopy(artifact["sections"][0]["items"][0])
    second["fact_id"] = "fact-latency-secondary"
    artifact["sections"][0]["items"].append(second)
    artifact["requirement_coverage"][0]["fact_ids"].append("fact-latency-secondary")

    payload = build_rewrite_provider_input(artifact)

    assert [fact["requirements"][0]["requirement_id"] for fact in payload["facts"]] == [
        "req-reliability",
        "req-reliability",
    ]
    assert validate_rewrite_provider_input(payload) == payload


def test_provider_summary_classifies_local_and_remote_or_unknown() -> None:
    assert provider_summary(_cfg()) == {"model": "ollama/test", "location": "local"}
    assert provider_summary(_cfg(model="openai/test")) == {
        "model": "openai/test",
        "location": "remote_or_unknown",
    }
    assert (
        provider_summary(_cfg(model="custom/test", base_url="http://127.0.0.1:8000"))["location"]
        == "local"
    )


def test_remote_provider_requires_current_disclosure_and_explicit_authorization() -> None:
    cfg = _cfg(model="openai/test")
    calls: list[dict[str, object]] = []

    def caller(*args, **kwargs):
        calls.append(kwargs)
        return _Response(_output(_proposal()))

    with pytest.raises(ResumeRewriteEgressDenied, match="not authorized"):
        generate_rewrite_output(
            cfg,
            artifact=_artifact(),
            expected_model_identity="openai/test",
            expected_provider_location="remote_or_unknown",
            remote_egress_authorized=False,
            llm_caller=caller,
        )
    with pytest.raises(ResumeRewriteEgressDenied, match="disclosure changed"):
        generate_rewrite_output(
            cfg,
            artifact=_artifact(),
            expected_model_identity="openai/old",
            expected_provider_location="remote_or_unknown",
            remote_egress_authorized=True,
            llm_caller=caller,
        )
    assert calls == []


def test_generation_calls_json_provider_without_tools_and_validates_output() -> None:
    cfg = _cfg()
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def caller(*args, **kwargs):
        calls.append((args, kwargs))
        return _Response(_output(_proposal()))

    result = generate_rewrite_output(
        cfg,
        artifact=_artifact(),
        expected_model_identity="ollama/test",
        expected_provider_location="local",
        remote_egress_authorized=False,
        llm_caller=caller,
    )

    assert result == _output(_proposal())
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[1] == "resume_rescue"
    assert kwargs["tools"] is None
    assert kwargs["json_mode"] is True
    messages = kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert "You have no tools" in messages[0]["content"]
    assert json.loads(messages[1]["content"]) == build_rewrite_provider_input(_artifact())


def test_generation_rejects_provider_tool_call_even_with_valid_json_text() -> None:
    cfg = _cfg()
    tool_call = type(
        "ToolCall",
        (),
        {
            "id": "rewrite-tool-call",
            "function": type(
                "Function",
                (),
                {"name": "submit_resume", "arguments": "{}"},
            )(),
        },
    )()

    with pytest.raises(ResumeRewriteValidationError, match="tool call") as error:
        generate_rewrite_output(
            cfg,
            artifact=_artifact(),
            expected_model_identity="ollama/test",
            expected_provider_location="local",
            remote_egress_authorized=False,
            llm_caller=lambda *_args, **_kwargs: _Response(
                _output(_proposal()), tool_calls=[tool_call]
            ),
        )

    assert error.value.code == "invalid_output"


@pytest.mark.parametrize(
    "response",
    [
        "not-json",
        json.dumps({"schema_version": 1, "proposals": [], "extra": True}),
        json.dumps(
            _output(
                _proposal(
                    proposed=("Reduced p95 latency by 99% in 2024 at Acme Labs; built Python APIs.")
                )
            )
        ),
    ],
)
def test_generation_rejects_malformed_open_or_unsupported_output(response: str) -> None:
    class Response:
        choices = [type("Choice", (), {"message": type("Message", (), {"content": response})()})]

    with pytest.raises(ResumeRewriteValidationError):
        generate_rewrite_output(
            _cfg(),
            artifact=_artifact(),
            expected_model_identity="ollama/test",
            expected_provider_location="local",
            remote_egress_authorized=False,
            llm_caller=lambda *args, **kwargs: Response(),
        )


def test_source_credentials_fail_before_provider_call() -> None:
    artifact = _artifact()
    artifact["sections"][0]["items"][0]["text"] += " password=secret-value"
    called = False

    def caller(*args, **kwargs):
        nonlocal called
        called = True
        return _Response(_output())

    with pytest.raises(ResumeRewriteValidationError):
        generate_rewrite_output(
            _cfg(),
            artifact=artifact,
            expected_model_identity="ollama/test",
            expected_provider_location="local",
            remote_egress_authorized=False,
            llm_caller=caller,
        )
    assert called is False


def test_rewrite_config_is_closed_and_bounded() -> None:
    cfg = _cfg()
    validate_rewrite_config(cfg)

    cfg.resume_rescue.rewrite_enabled = 1  # type: ignore[assignment]
    with pytest.raises(ValueError, match="must be a boolean"):
        validate_rewrite_config(cfg)

    cfg = _cfg()
    cfg.resume_rescue.rewrite_max_input_chars = 200_001
    with pytest.raises(ValueError, match="rewrite_max_input_chars"):
        validate_rewrite_config(cfg)
