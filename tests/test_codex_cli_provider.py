from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openchronicle.writer import llm as llm_mod


def _fake_codex_run(payload: dict, captured: dict):
    def run(command, *, input, stdout, stderr, check):  # noqa: A002, ARG001
        captured["command"] = list(command)
        captured["prompt"] = input.decode("utf-8")
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    return run


def test_codex_cli_completion_is_ephemeral_tool_disabled_and_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    monkeypatch.setattr(llm_mod.shutil, "which", lambda name: "/opt/codex" if name == "codex" else None)
    monkeypatch.setattr(
        llm_mod.subprocess,
        "run",
        _fake_codex_run({"content": '{"entries":[]}'}, captured),
    )

    response = llm_mod._codex_cli_completion(
        {
            "model": "gpt-5.6-luna",
            "messages": [
                {"role": "system", "content": "Normalize activity."},
                {"role": "user", "content": "Window contents"},
            ],
            "reasoning_effort": "none",
            "json_mode": True,
        }
    )

    command = captured["command"]
    assert command[:2] == ["/opt/codex", "exec"]
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "skills.include_instructions=false" in command
    for feature in llm_mod._CODEX_DISABLED_FEATURES:
        index = command.index(feature)
        assert command[index - 1] == "--disable"
    assert command[-1] == "-"
    assert "Normalize activity." in captured["prompt"]
    assert "valid JSON object" in captured["prompt"]
    assert response["choices"][0]["message"]["content"] == '{"entries":[]}'
    assert response["choices"][0]["message"]["tool_calls"] == []


def test_codex_cli_completion_returns_custom_tool_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    monkeypatch.setattr(llm_mod.shutil, "which", lambda _name: "/opt/codex")
    monkeypatch.setattr(
        llm_mod.subprocess,
        "run",
        _fake_codex_run(
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "search_memory",
                        "arguments": '{"query":"editor"}',
                    }
                ],
            },
            captured,
        ),
    )

    response = llm_mod._codex_cli_completion(
        {
            "model": "gpt-5.6-sol",
            "messages": [{"role": "user", "content": "Find the editor preference"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "search_memory",
                        "description": "Search durable memory",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                }
            ],
            "reasoning_effort": "low",
            "json_mode": False,
        }
    )

    call = response["choices"][0]["message"]["tool_calls"][0]
    assert response["choices"][0]["finish_reason"] == "tool_calls"
    assert call["function"]["name"] == "search_memory"
    assert json.loads(call["function"]["arguments"]) == {"query": "editor"}
    schema_path = Path(
        captured["command"][captured["command"].index("--output-schema") + 1]
    )
    # The temporary directory is gone after the call; the prompt still proves
    # the host, not Codex, is responsible for executing custom tools.
    assert not schema_path.exists()
    assert "Do not execute custom tools yourself" in captured["prompt"]


def test_codex_cli_rejects_unknown_tool_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_mod.shutil, "which", lambda _name: "/opt/codex")
    monkeypatch.setattr(
        llm_mod.subprocess,
        "run",
        _fake_codex_run(
            {
                "content": "",
                "tool_calls": [
                    {"id": "call-1", "name": "shell", "arguments": "{}"}
                ],
            },
            {},
        ),
    )

    with pytest.raises(RuntimeError, match="invalid tool calls"):
        llm_mod._codex_cli_completion(
            {
                "model": "gpt-5.6-sol",
                "messages": [{"role": "user", "content": "Act"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "commit", "parameters": {}},
                    }
                ],
            }
        )
