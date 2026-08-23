from __future__ import annotations

from pathlib import Path

import pytest

from openchronicle.evaluation.longmemeval_v2 import (
    DATASET_REVISION,
    UPSTREAM_COMMIT,
    AdapterConfig,
    LongMemEvalV2Corpus,
    default_memory_config,
)


class FixtureEmbedder:
    model_id = "fixture:longmemeval-v2"

    def embed_documents(self, texts):
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.lower()
        if any(token in lowered for token in ("report", "save", "submission control")):
            return [1.0, 0.0, 0.0]
        if any(token in lowered for token in ("incident", "assignment", "ticket")):
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]


def _trajectory(
    trajectory_id: str,
    *,
    goal: str,
    tree: str,
    action: str,
) -> dict[str, object]:
    return {
        "id": trajectory_id,
        "domain": "enterprise",
        "environment": "fixture",
        "goal": goal,
        "outcome": "success",
        "start_url": "https://example.invalid/start",
        "states": [
            {
                "state_index": 0,
                "url": "https://example.invalid/page",
                "thought": "Locate the relevant control.",
                "action": action,
                "accessibility_tree": tree,
                "screenshot": "ignored.png",
            },
            {
                "state_index": 1,
                "url": "https://example.invalid/done",
                "thought": "The operation completed.",
                "action": None,
                "accessibility_tree": "[s1] status 'Complete'",
                "screenshot": "ignored-2.png",
            },
        ],
    }


def test_adapter_config_is_closed_and_versioned() -> None:
    config = AdapterConfig.from_mapping({})

    assert config.top_k == 6
    assert default_memory_config()["memory_type"] == "openchronicle"
    assert len(UPSTREAM_COMMIT) == 40
    assert len(DATASET_REVISION) == 40
    with pytest.raises(ValueError, match="unexpected"):
        AdapterConfig.from_mapping({"answer_gold": "leak"})


def test_corpus_retrieves_text_only_state_slices() -> None:
    corpus = LongMemEvalV2Corpus(
        AdapterConfig(top_k=2, candidate_k=4, min_similarity=0.5, slice_radius=1),
        embedder=FixtureEmbedder(),
    )
    try:
        corpus.insert(
            _trajectory(
                "report-flow",
                goal="Save the quarterly report.",
                tree="[b7] button 'Save report'\n[t2] textbox 'Report title'",
                action="click('b7')",
            )
        )
        corpus.insert(
            _trajectory(
                "incident-flow",
                goal="Assign an incident ticket.",
                tree="[b9] button 'Assign incident'",
                action="click('b9')",
            )
        )

        contexts = corpus.query("Where is the submission control?")
        metadata = corpus.metadata()
    finally:
        corpus.close()

    assert contexts
    assert all(item["type"] == "text" for item in contexts)
    assert "Save report" in contexts[0]["value"]
    assert "[b7] button 'Save report'" in contexts[0]["value"]
    assert "Assign incident" not in contexts[0]["value"]
    assert metadata["trajectory_count"] == 2
    assert metadata["entry_count"] == 4
    assert metadata["image_memory"] is False


def test_corpus_save_and_load_round_trip(tmp_path: Path) -> None:
    config = AdapterConfig(top_k=2, candidate_k=4, min_similarity=0.5)
    saved = tmp_path / "memory"
    first = LongMemEvalV2Corpus(config, embedder=FixtureEmbedder())
    second = LongMemEvalV2Corpus(config, embedder=FixtureEmbedder())
    try:
        first.insert(
            _trajectory(
                "report-flow",
                goal="Save the quarterly report.",
                tree="[b7] button 'Save report'",
                action="click('b7')",
            )
        )
        assert first.query("submission control")
        first.save(saved)

        second.load(saved)
        restored = second.query("submission control")
    finally:
        first.close()
        second.close()

    assert restored
    assert second.entry_count == 2
    assert second.trajectory_count == 1


def test_corpus_rejects_duplicate_trajectory() -> None:
    corpus = LongMemEvalV2Corpus(
        AdapterConfig(top_k=1, candidate_k=1),
        embedder=FixtureEmbedder(),
    )
    trajectory = _trajectory(
        "same",
        goal="Assign an incident ticket.",
        tree="[b9] button 'Assign incident'",
        action="click('b9')",
    )
    try:
        corpus.insert(trajectory)
        with pytest.raises(ValueError, match="duplicate trajectory"):
            corpus.insert(trajectory)
    finally:
        corpus.close()
