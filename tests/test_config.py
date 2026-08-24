from pathlib import Path

from openchronicle import config


def test_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = config.load(tmp_path / "missing.toml")
    assert cfg.capture.interval_minutes == 10
    assert cfg.capture.include_screenshot is False
    assert cfg.capture.deny_unknown_windows is True
    assert cfg.capture.allowed_bundle_ids == []
    assert cfg.capture.excluded_bundle_ids == []
    assert cfg.capture.allowed_url_patterns == []
    assert cfg.capture.excluded_url_patterns == []
    assert cfg.session.gap_minutes == 5
    assert cfg.reducer.enabled is True
    assert cfg.resume_rescue.enabled is False
    default = cfg.model_for("reducer")
    assert default.provider == "litellm"
    assert default.model == "gpt-5.6-sol"
    assert default.reasoning_effort == "none"
    assert default.timeout_seconds is None
    assert default.num_retries is None


def test_stage_override_merges(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[models.default]
provider = "codex_cli"
model = "gpt-5.4-nano"
reasoning_effort = "low"
api_key_env = "OPENAI_API_KEY"

[models.classifier]
provider = "litellm"
model = "claude-haiku-4-5"
api_key_env = "ANTHROPIC_API_KEY"
"""
    )
    cfg = config.load(path)
    default = cfg.model_for("default")
    classifier = cfg.model_for("classifier")
    assert default.provider == "codex_cli"
    assert default.model == "gpt-5.4-nano"
    assert default.reasoning_effort == "low"
    assert default.api_key_env == "OPENAI_API_KEY"
    assert classifier.provider == "litellm"
    assert classifier.model == "claude-haiku-4-5"
    assert classifier.reasoning_effort == "low"
    assert classifier.api_key_env == "ANTHROPIC_API_KEY"


def test_url_privacy_literals_load_from_capture_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[capture]
allowed_url_patterns = ["example.com"]
excluded_url_patterns = ["/private/"]
"""
    )

    cfg = config.load(path)

    assert cfg.capture.allowed_url_patterns == ["example.com"]
    assert cfg.capture.excluded_url_patterns == ["/private/"]


def test_llm_reliability_settings_inherit_and_override(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[models.default]
timeout_seconds = 90
num_retries = 1

[models.reducer]
timeout_seconds = 240

[models.timeline]
num_retries = 0
"""
    )

    cfg = config.load(path)

    assert cfg.model_for("classifier").timeout_seconds == 90
    assert cfg.model_for("classifier").num_retries == 1
    assert cfg.model_for("reducer").timeout_seconds == 240
    assert cfg.model_for("reducer").num_retries == 1
    assert cfg.model_for("timeline").timeout_seconds == 90
    assert cfg.model_for("timeline").num_retries == 0


def test_write_default_creates_file(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    assert config.write_default_if_missing(p)
    assert p.exists()
    assert "[models.default]" in p.read_text()
    assert 'provider = "codex_cli"' in p.read_text()
    assert 'model = "gpt-5.6-sol"' in p.read_text()
    assert '[models.timeline]\nmodel = "gpt-5.6-luna"' in p.read_text()
    assert "# timeout_seconds = 120" in p.read_text()
    assert "# num_retries = 2" in p.read_text()
    assert "include_screenshot = false" in p.read_text()
    assert "allowed_url_patterns = []" in p.read_text()
    assert "excluded_url_patterns = []" in p.read_text()
    # idempotent
    assert not config.write_default_if_missing(p)


def test_api_key_precedence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ENV_KEY", "from-env")
    cfg = config.ModelConfig(api_key="direct", api_key_env="ENV_KEY")
    assert config.resolve_api_key(cfg) == "direct"
    cfg2 = config.ModelConfig(api_key="", api_key_env="ENV_KEY")
    assert config.resolve_api_key(cfg2) == "from-env"
