"""TOML config loader with defaults and per-stage LLM resolution."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths


@dataclass
class ModelConfig:
    model: str = "gpt-5.4-nano"
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    max_tokens: int | None = None
    # ``None`` preserves compatibility with existing config files while the
    # LLM wrapper applies its bounded, safe defaults.
    timeout_seconds: float | None = None
    num_retries: int | None = None


@dataclass
class CaptureConfig:
    # Event-driven capture knobs
    event_driven: bool = True  # consume mac-ax-watcher events
    heartbeat_minutes: int = 10  # periodic capture even without events
    debounce_seconds: float = 3.0  # for AXValueChanged bursts
    min_capture_gap_seconds: float = 2.0  # between consecutive captures
    dedup_interval_seconds: float = 1.0  # per-event-type dedup window
    same_window_dedup_seconds: float = (
        5.0  # skip repeat non-focus capture in same window within this window
    )
    # Legacy timer knob (kept for back-compat; also treated as a floor on heartbeat)
    interval_minutes: int = 10
    # Tiered buffer retention:
    #   * whole JSON is deleted once older than `buffer_retention_hours`
    #     AND already absorbed by a timeline block
    #   * screenshot (base64) is stripped from JSONs older than
    #     `screenshot_retention_hours` — it's 77% of the bytes and nothing
    #     downstream currently consumes it
    #   * `buffer_max_mb` is a best-effort target; when exceeded the oldest
    #     already-absorbed files are deleted first, but unprocessed data is
    #     never evicted (0 disables size-based cleanup)
    buffer_retention_hours: int = 168
    screenshot_retention_hours: int = 24
    buffer_max_mb: int = 2000
    # Privacy gate evaluated from active-window metadata before AX/screenshot.
    # A non-empty allowlist restricts capture to those bundle identifiers.
    allowed_bundle_ids: list[str] = field(default_factory=list)
    excluded_bundle_ids: list[str] = field(default_factory=list)
    excluded_app_names: list[str] = field(default_factory=list)
    excluded_window_title_patterns: list[str] = field(default_factory=list)
    # URL config and supported-browser eligibility run before AX; candidate
    # evaluation runs on the ephemeral structured tree. Despite the ``patterns``
    # name these are bounded literals, never regular expressions: bare host
    # names match that host and its subdomains and full URLs use a component-
    # boundary prefix. Allow rules accept only those two safe forms; exclusion
    # rules additionally accept conservative case-insensitive substrings. Empty
    # lists preserve capture defaults. Once either list is non-empty, only a
    # supported browser family is eligible: policy requires one explicit
    # HTTP(S) address from an exact stable AX identifier, and scans the full AX
    # tree as an additional deny surface. Unknown/non-browser bundles fail
    # before AX. Successful observations retain URL/identity metadata only.
    allowed_url_patterns: list[str] = field(default_factory=list)
    excluded_url_patterns: list[str] = field(default_factory=list)
    deny_unknown_windows: bool = True
    # Screenshots duplicate substantially more context than structured AX and
    # are not consumed by the current memory pipeline, so opt in explicitly.
    include_screenshot: bool = False
    screenshot_max_width: int = 1920
    screenshot_jpeg_quality: int = 80
    ax_depth: int = 100
    ax_timeout_seconds: int = 3


@dataclass
class TimelineConfig:
    # Wall-clock window length for each aggregator block. 1-min blocks
    # keep timeline entries close to verbatim — the reducer (which runs
    # every flush_minutes ≥5m) is the stage that does real compression.
    window_minutes: int = 1
    cold_lookback_minutes: int = 30
    # Wall-clock horizon of blocks kept warm for tooling / context.
    # 720 × 1-min ≈ 12h.
    recent_context_blocks: int = 720


@dataclass
class WriterConfig:
    soft_limit_tokens: int = 20_000
    hard_limit_tokens: int = 50_000
    dedup_window_hours: int = 24
    cold_start_conservative_hours: int = 0
    max_tool_iterations: int = 12


@dataclass
class SessionConfig:
    # Hard cut: no capture-worthy events for this many minutes
    gap_minutes: int = 5
    # Soft cut: single unrelated app focused for this many minutes
    soft_cut_minutes: int = 3
    # Forced cut once a session crosses this many hours
    max_session_hours: int = 2
    # Wall-clock interval between check_cuts() ticks
    tick_seconds: int = 30
    # Incremental flush inside an active session: every flush_minutes, the
    # reducer runs over any newly-closed timeline blocks since the last flush
    # and appends a partial entry to event-YYYY-MM-DD.md. The terminal
    # reduce at session-end covers only the trailing window since the last
    # flush. Minimum effective interval is 5 minutes — anything smaller is
    # clamped up, to keep LLM cost bounded. (Timeline blocks themselves are
    # 1-min wide, so a 5-min flush consumes ~5 blocks.)
    flush_minutes: int = 5


@dataclass
class ReducerConfig:
    # Enable S2 session reduction (on session end + daily safety net)
    enabled: bool = True
    # Local wall-clock time for the daily safety-net tick. 23:55 gives the
    # current open session a chance to close on its own but still catches
    # anything unfinished before the date rolls over.
    daily_tick_hour: int = 23
    daily_tick_minute: int = 55


@dataclass
class ClassifierConfig:
    # How often to fire the classifier inside an active session. The
    # terminal classifier still runs at session end over any trailing
    # window that this tick hasn't covered. Clamped to >= 5 minutes.
    interval_minutes: int = 30
    # Durable outbox retry poll and minimum worker lease. The worker renews
    # before/after every provider call and raises the lease to one full call
    # budget, so a healthy multi-tool round remains fenced without duplicates.
    retry_seconds: int = 60
    lease_seconds: int = 300


@dataclass
class MemoryConfig:
    auto_dormant_days: int = 30


@dataclass
class DailyWrapConfig:
    # Opt-in on upgrades: enabling this schedules remote synthesis.
    enabled: bool = False
    timezone: str = ""  # empty = infer the system IANA timezone
    # The daemon scheduler is introduced separately; these values define the
    # intended post-midnight local schedule and are already available to it.
    hour: int = 0
    minute: int = 5
    retry_seconds: int = 300
    late_data_grace_hours: int = 6
    # Minimum lease. The service raises it to cover the configured provider's
    # full timeout/retry budget so two workers cannot duplicate remote calls.
    lease_seconds: int = 300


@dataclass
class SuggestionConfig:
    # Proactivity is opt-in even though Stage 2 cannot execute side effects.
    enabled: bool = False
    scan_seconds: int = 60
    daily_budget: int = 3
    cooldown_minutes: int = 240
    quiet_hours_enabled: bool = True
    quiet_hours_start: int = 22
    quiet_hours_end: int = 8
    min_score: float = 0.75
    expiry_minutes: int = 120
    work_resumption_min_gap_minutes: int = 15
    work_resumption_max_gap_hours: int = 12
    work_resumption_activation_minutes: int = 10
    work_resumption_settle_seconds: int = 20


@dataclass
class PromptRescueConfig:
    # Explicitly enabled because a configured remote model receives user text.
    enabled: bool = False
    poll_seconds: int = 5
    lease_seconds: int = 300
    max_input_chars: int = 20_000
    max_output_chars: int = 30_000


@dataclass
class ReplyRescueConfig:
    # Explicitly enabled because a configured remote model receives conversation text.
    enabled: bool = False
    poll_seconds: int = 5
    lease_seconds: int = 300
    max_input_chars: int = 50_000
    max_output_chars: int = 30_000


@dataclass
class SearchConfig:
    default_top_k: int = 5
    filter_superseded_by_default: bool = True


@dataclass
class MCPConfig:
    auto_start: bool = True  # run an in-daemon MCP server
    transport: str = (
        "streamable-http"  # "streamable-http" | "sse" (deprecated 2026-04-01) | "stdio"
    )
    host: str = "127.0.0.1"
    port: int = 8742


@dataclass
class Config:
    models: dict[str, ModelConfig] = field(default_factory=dict)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    timeline: TimelineConfig = field(default_factory=TimelineConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    reducer: ReducerConfig = field(default_factory=ReducerConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    writer: WriterConfig = field(default_factory=WriterConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    daily_wrap: DailyWrapConfig = field(default_factory=DailyWrapConfig)
    suggestions: SuggestionConfig = field(default_factory=SuggestionConfig)
    prompt_rescue: PromptRescueConfig = field(default_factory=PromptRescueConfig)
    reply_rescue: ReplyRescueConfig = field(default_factory=ReplyRescueConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)

    def model_for(self, stage: str) -> ModelConfig:
        """Return stage config (already inherited from default at build time)."""
        return self.models.get(stage) or self.models.get("default") or ModelConfig()


def resolve_api_key(cfg: ModelConfig) -> str | None:
    if cfg.api_key:
        return cfg.api_key
    if cfg.api_key_env:
        return os.environ.get(cfg.api_key_env)
    return None


def _as_dict(section: Any) -> dict:
    return section if isinstance(section, dict) else {}


def _build_models(raw: dict) -> dict[str, ModelConfig]:
    # Build default first so stage sections can inherit only its explicitly-set values.
    default_data = _as_dict(raw.get("default", {}))
    default_allowed = {
        k: v for k, v in default_data.items() if k in ModelConfig.__dataclass_fields__
    }
    default = ModelConfig(**default_allowed)
    models = {"default": default}
    for name, section in raw.items():
        if name == "default":
            continue
        data = _as_dict(section)
        allowed = {k: v for k, v in data.items() if k in ModelConfig.__dataclass_fields__}
        models[name] = ModelConfig(**{**default.__dict__, **allowed})
    return models


def _build_dataclass(cls, raw: dict):
    allowed = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
    return cls(**allowed)


def load(path: Path | None = None) -> Config:
    path = path or paths.config_file()
    raw: dict = {}
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)

    return Config(
        models=_build_models(_as_dict(raw.get("models"))),
        capture=_build_dataclass(CaptureConfig, _as_dict(raw.get("capture"))),
        timeline=_build_dataclass(TimelineConfig, _as_dict(raw.get("timeline"))),
        session=_build_dataclass(SessionConfig, _as_dict(raw.get("session"))),
        reducer=_build_dataclass(ReducerConfig, _as_dict(raw.get("reducer"))),
        classifier=_build_dataclass(ClassifierConfig, _as_dict(raw.get("classifier"))),
        writer=_build_dataclass(WriterConfig, _as_dict(raw.get("writer"))),
        memory=_build_dataclass(MemoryConfig, _as_dict(raw.get("memory"))),
        daily_wrap=_build_dataclass(DailyWrapConfig, _as_dict(raw.get("daily_wrap"))),
        suggestions=_build_dataclass(SuggestionConfig, _as_dict(raw.get("suggestions"))),
        prompt_rescue=_build_dataclass(
            PromptRescueConfig,
            _as_dict(raw.get("prompt_rescue")),
        ),
        reply_rescue=_build_dataclass(
            ReplyRescueConfig,
            _as_dict(raw.get("reply_rescue")),
        ),
        search=_build_dataclass(SearchConfig, _as_dict(raw.get("search"))),
        mcp=_build_dataclass(MCPConfig, _as_dict(raw.get("mcp"))),
    )


DEFAULT_CONFIG_TEMPLATE = """# OpenChronicle configuration
# All LLM stages go through litellm. Each stage inherits from [models.default].

[models.default]
model = "gpt-5.4-nano"
api_key_env = "OPENAI_API_KEY"
# base_url = ""
# api_key = ""          # overrides api_key_env if set
# timeout_seconds = 120  # per-attempt provider I/O timeout; max 1800
# num_retries = 2        # transient failures only; max 5 (3 total attempts)

[models.compact]
# Accuracy-sensitive — match or exceed the default.

[models.timeline]
# 1-minute activity normalisation (verbatim-preserving). The reducer,
# which runs every flush_minutes ≥ 5m, is the stage that does real
# compression — timeline only cleans up, de-duplicates, and separates
# independent conversations. A small model is fine: the prompt is short
# and the output is a bounded JSON list.

[models.reducer]
# Session-level S2 reduce-from-blocks. Prompt is short (blocks are already
# compressed) but output quality matters — consider a stronger model here.

[models.classifier]
# Extracts classifiable long-term facts from event-daily entries into an
# evidence-linked local review inbox. It cannot write Markdown directly.
# Accuracy-sensitive — pick a capable model.

[models.daily_wrap]
# Evidence-backed end-of-day synthesis. This stage has no tools and receives
# only bounded, policy-filtered activity excerpts.

[models.prompt_rescue]
# Explicit rough-prompt preparation. This stage has no tools. Enabling the
# workflow may send exactly the reviewed manual input to this configured model.

[models.reply_rescue]
# Explicit reply preparation. This stage has no tools and cannot send. Enabling
# it may send exactly the reviewed conversation source to this configured model.

[capture]
event_driven = true           # capture on window/app/typing events via mac-ax-watcher
heartbeat_minutes = 10        # periodic capture even when nothing happens
debounce_seconds = 3.0        # for AXValueChanged bursts
min_capture_gap_seconds = 2.0 # minimum gap between consecutive captures
dedup_interval_seconds = 1.0  # per-event-type dedup window
same_window_dedup_seconds = 5.0  # don't re-capture the same bundle+window unless 5s have passed (or it's a focus change)
buffer_retention_hours = 168           # 7 days; stale absorbed captures past this are deleted
screenshot_retention_hours = 24        # normal captures: strip screenshot after 24h; URL-policy captures never have one
buffer_max_mb = 2000                   # best-effort target over absorbed files (0 to disable)
allowed_bundle_ids = []                # non-empty = capture only these bundle IDs
excluded_bundle_ids = []               # exact, case-insensitive
excluded_app_names = []                # exact, case-insensitive
excluded_window_title_patterns = []    # substring, case-insensitive
allowed_url_patterns = []              # known-browser stable-ID URL literals; non-empty requires a match (not regex/glob)
excluded_url_patterns = []             # exclusions win; active URL policy persists URL/identity metadata only
deny_unknown_windows = true            # fail closed when active app identity is unavailable
include_screenshot = false             # opt in only without URL policy; URL-policy captures never include pixels
screenshot_max_width = 1920
screenshot_jpeg_quality = 80
ax_depth = 100                # Electron apps (Claude Desktop, VS Code, Slack) have deep DOM; 8 only reaches the chrome
ax_timeout_seconds = 3

[timeline]
window_minutes = 1             # length of each aggregator block (verbatim-preserving normalizer)
cold_lookback_minutes = 30
recent_context_blocks = 720    # ~12h of 1-min blocks

[writer]
soft_limit_tokens = 20000
hard_limit_tokens = 50000
dedup_window_hours = 24
cold_start_conservative_hours = 0

[session]
gap_minutes = 5            # hard cut: idle > 5 min ends the session
soft_cut_minutes = 3       # soft cut: single unrelated app > 3 min
max_session_hours = 2      # forced cut at 2h
tick_seconds = 30          # check_cuts() interval
flush_minutes = 5          # incremental reduce tick inside active sessions (min 5)

[reducer]
enabled = true             # run S2 reducer on session end + daily safety net
daily_tick_hour = 23       # local-time hour for the daily safety-net tick
daily_tick_minute = 55     # local-time minute for the daily safety-net tick

[classifier]
interval_minutes = 30      # durable-fact extraction cadence inside active sessions (min 5)
retry_seconds = 60         # durable failed/expired delivery poll (1..3600)
lease_seconds = 300        # minimum lease; auto-raised and renewed per provider call

[memory]
auto_dormant_days = 30

[daily_wrap]
enabled = false                  # opt in: scheduled runs may call a remote model
timezone = ""                  # e.g. "Asia/Shanghai"; empty = infer system zone
hour = 0                       # intended local post-midnight generation time
minute = 5
retry_seconds = 300            # failed-run retry and late-data recheck cadence
late_data_grace_hours = 6      # revise yesterday's wrap during this window
lease_seconds = 300            # minimum lease; auto-raised to provider call budget

[suggestions]
enabled = false                        # opt in to proactive cards; Stage 2 never acts
scan_seconds = 60                      # local detector cadence
daily_budget = 3                       # unsolicited cards per local day
cooldown_minutes = 240                 # same semantic opportunity cooldown
quiet_hours_enabled = true
quiet_hours_start = 22                 # local hour, inclusive
quiet_hours_end = 8                    # local hour, exclusive
min_score = 0.75
expiry_minutes = 120
work_resumption_min_gap_minutes = 15
work_resumption_max_gap_hours = 12
work_resumption_activation_minutes = 10
work_resumption_settle_seconds = 20       # quiet time after the latest persisted capture

[prompt_rescue]
enabled = false                 # explicit opt-in; configured remote models receive user text
poll_seconds = 5                # durable queued-job cadence (1..300)
lease_seconds = 300             # minimum lease; auto-raised to provider call budget
max_input_chars = 20000         # rough prompt plus declared context remains bounded
max_output_chars = 30000        # improved prompt bound

[reply_rescue]
enabled = false                 # explicit opt-in; generated artifacts cannot send
poll_seconds = 5                # durable queued-job cadence (1..300)
lease_seconds = 300             # minimum lease; auto-raised to provider call budget
max_input_chars = 50000         # reviewed conversation plus directions remains bounded
max_output_chars = 30000        # prepared reply plus review ledger bound

[search]
default_top_k = 5
filter_superseded_by_default = true

[mcp]
auto_start = true                 # run an always-on MCP server inside the daemon
transport = "streamable-http"     # "streamable-http" | "sse" (deprecated 2026-04-01) | "stdio"
host = "127.0.0.1"                # bind address; keep localhost-only by default
port = 8742
"""


def write_default_if_missing(path: Path | None = None) -> bool:
    if path is None:
        paths.ensure_dirs()
        path = paths.config_file()
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        path.chmod(0o600)
        return False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another process won the create race; preserve its configuration.
        path.chmod(0o600)
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(DEFAULT_CONFIG_TEMPLATE)
        handle.flush()
        os.fsync(handle.fileno())
    return True
