# Configuration

Runtime config lives at `~/.openchronicle/config.toml` (or `$OPENCHRONICLE_ROOT/config.toml`). It's created with sensible defaults the first time you run `openchronicle status`.

View the resolved config any time with:

```bash
openchronicle config
```

## `[models.*]` — LLM per stage

Every LLM stage goes through [litellm](https://github.com/BerriAI/litellm), so anything litellm speaks will work: OpenAI, Anthropic, Azure, Bedrock, Gemini, Mistral, Ollama, DeepSeek, any OpenAI-compatible gateway…

```toml
[models.default]
model = "gpt-5.4-nano"
api_key_env = "OPENAI_API_KEY"
# base_url = "https://your-gateway/v1"
# api_key  = "sk-..."        # overrides api_key_env if set
# timeout_seconds = 120       # per-attempt provider I/O timeout; max 1800
# num_retries = 2             # transient failures only; max 5; 3 total attempts

[models.timeline]     # short-window normalizer — runs constantly, keep cheap but not weak
# inherits from default

[models.reducer]      # session → event-daily entry
# Consider a stronger model:
# model = "claude-haiku-4-5"
# api_key_env = "ANTHROPIC_API_KEY"

[models.classifier]   # durable-fact extraction via tool calls
# Accuracy-sensitive; a weak model here poisons dedup.

[models.daily_wrap]   # grounded day synthesis; JSON-only and no tools
# Accuracy-sensitive; unsupported items are rejected by deterministic validation.

[models.prompt_rescue] # explicit rough-prompt preparation; JSON-only and no tools
# This model receives only the text and constraints the user reviews and queues.

[models.compact]      # file compaction — accuracy matters
# e.g. same as classifier
```

Each stage section **inherits every field** from `[models.default]` and overrides only what it sets. If you want a single model everywhere, set `[models.default]` and leave the rest empty.

`timeout_seconds` is passed to LiteLLM as the provider transport timeout and is
also enforced by a parent-owned outer deadline. Every non-mock attempt runs in
a fresh process group; its byte-bounded JSON request travels over stdin (never
argv), and its response must be one complete byte-bounded JSON envelope. If the
SDK hangs past the configured timeout plus a one-second scheduling grace, the
parent sends `TERM`, escalates to `KILL`, and reaps the worker before returning.
At most eight provider workers may be in flight.

OpenChronicle owns the retry loop and clears request-level and process-global
LiteLLM retries, so attempt count is deterministic. Provider failures that
arrive before the outer deadline are retried only for 408/409/429,
connection/timeout failures, and HTTP 5xx. A hard outer timeout, worker-protocol
failure, or local capacity rejection is not retried. Killing the local worker
cannot prove that a remote service stopped processing an already-sent request,
so provider calls and billing are not exactly-once; local publication remains
replay-safe and separately fenced.

Stage → purpose:

| Stage | Runs | What it does |
|---|---|---|
| `timeline` | every 60s while captures exist | Normalizes a short (default 1-min) capture window into a list of activity records with authored text preserved verbatim. |
| `reducer` | active-session flushes + session end + due retry/safety net | Turns a session's timeline blocks into time-ranged event-daily entries. |
| `classifier` | periodic active-session passes + terminal catch-up | Reads event-daily entries + context and stages evidence-linked candidates; it cannot write Markdown. |
| `daily_wrap` | post-midnight or explicit CLI run | Produces a bounded, evidence-backed JSON review with no tools. |
| `prompt_rescue` | explicit, opt-in queued jobs | Rewrites reviewed manual input into a bounded prepared artifact; it cannot use tools, paste, or submit. |
| `compact` | after commits that flag files | Rewrites a fat file; rejects if >5% noun-phrase loss. |

### Fully local with Ollama

OpenChronicle has no hard dependency on a cloud provider — any model litellm can reach works, including a local [Ollama](https://ollama.com/) server. Minimum config:

```toml
[models.default]
model = "ollama/llama3.1:8b"            # any model you've pulled; prefix with ollama/ or ollama_chat/
base_url = "http://localhost:11434"
api_key_env = ""                        # leave blank — Ollama needs no key
```

Tiered assignment is usually worth the trouble — timeline fires every minute, classifier is accuracy-sensitive:

```toml
[models.timeline]
model = "ollama/qwen2.5:7b"             # cheap-but-not-weak; runs constantly

[models.reducer]
model = "ollama/qwen2.5:14b"            # compresses a whole session — precision matters

[models.classifier]
model = "ollama/qwen2.5:14b"            # tool-calling; weak models here poison dedup

[models.daily_wrap]
model = "ollama/qwen2.5:14b"            # strict grounded JSON synthesis

[models.compact]
model = "ollama/qwen2.5:14b"            # match classifier or stronger
```

Things to check before trusting a local setup:

- **Tool-calling support is required for the classifier.** It can read/search and call `propose_memory_candidate`; approval is a separate trusted local operation. `qwen2.5`, `llama3.1`, `mistral-nemo` and `command-r` are typical tool-capable choices; small variants are often unreliable.
- **JSON mode is required for `timeline` and `reducer`.** They pass `response_format={"type":"json_object"}`, which litellm forwards to Ollama as `format: "json"`. If the model ignores it and returns prose, both stages will log parse errors — pick a bigger model.
- **Context window.** Timeline blocks are 1-min, reducer flushes consume ~5 blocks, a 2-hour session can stack ~24 blocks. Set Ollama's `num_ctx` to ≥ 16 k for `timeline`, ≥ 32 k for `reducer` / `classifier`. Tiny defaults (2–4 k) will silently truncate.
- **Leave `api_key_env` empty.** If you keep the default `"OPENAI_API_KEY"` and don't have one exported, litellm complains even though Ollama wouldn't use it.

## `[capture]`

```toml
[capture]
event_driven = true                  # consume mac-ax-watcher events
heartbeat_minutes = 10               # periodic capture even when nothing happens (0 disables entirely)
debounce_seconds = 3.0               # AXValueChanged bursts collapse to one capture
min_capture_gap_seconds = 2.0        # hard floor between consecutive captures, regardless of event reason
dedup_interval_seconds = 1.0         # same-event-type dedup window
same_window_dedup_seconds = 5.0      # non-focus-change events in the same bundle+window are dropped if within this gap
buffer_retention_hours = 168         # 7 days; stale absorbed captures past this are deleted
screenshot_retention_hours = 24      # normal captures only; URL-policy captures never have screenshots
buffer_max_mb = 2000                 # best-effort target over absorbed files (0 disables)
allowed_bundle_ids = []              # non-empty = capture only these bundle IDs
excluded_bundle_ids = []             # exact, case-insensitive
excluded_app_names = []              # exact, case-insensitive
excluded_window_title_patterns = []  # substring, case-insensitive
allowed_url_patterns = []             # supported-browser stable-ID URL literal allowlist
excluded_url_patterns = []            # exclusions win; successful URL policy stores metadata only
deny_unknown_windows = true           # policy default; exact focused identity is always required
include_screenshot = false            # opt-in exact-CGWindowID JPEG; unused downstream today
screenshot_max_width = 1920
screenshot_jpeg_quality = 80
ax_depth = 100                       # Electron apps need deep trees; 8 only reaches chrome
ax_timeout_seconds = 3
```

Tuning notes:

- **`allowed_bundle_ids`.** Leave empty for compatibility, or set a strict
  allowlist for the safest deployment. Unknown/empty bundle IDs are denied
  whenever this list is non-empty. All exclusion rules still take precedence.
- **Exclusions.** Bundle IDs and app names use case-insensitive exact matching;
  window-title patterns use case-insensitive substring matching. These checks
  run before AX collection, screenshots, persistence, indexing, and model use.
- **URL rules.** Despite the `patterns` name, both lists contain bounded
  literals, never regular expressions. A bare hostname matches the exact host
  or a DNS-label subdomain. A full HTTP(S) URL is canonicalized and matches
  only at the start of the observed URL on a component boundary; it cannot be
  smuggled through an unrelated query or fragment. Allow rules accept only
  those two forms. Exclusion rules additionally accept non-host literals with
  case-insensitive substring semantics. Each list accepts at
  most 128 items, each item at most 512 characters, and observed URLs at most
  4,096 characters. Scheme and host case, IDNA hosts, default ports, and
  percent-escape case are normalized; credentials, whitespace/control
  characters, malformed percent escapes, and malformed URLs are rejected.
  Exclusions win. A non-empty allowlist must match the one trusted address, and
  every URL-like value found elsewhere in the tree must also pass. Missing,
  invalid, incomplete, or ambiguous evidence fails closed.
- **URL-policy scope.** With either rule list active, only the known Safari,
  Chrome/Edge/Brave/Opera, Firefox, and Arc bundle/family adapters are eligible.
  Unknown browsers and ordinary apps are rejected before AX; use bundle/title
  policy without URL rules for them. A family adapter must find exactly one
  explicit HTTP(S) address in an editable browser-chrome control by exact
  stable `identifier`/`domIdentifier`. The exact-label fallback used by normal
  S1 extraction cannot authorize policy. A separate bounded full-tree scan is
  an additional deny surface, never address evidence: page content and URL
  decoys cannot grant capture. Unsupported URI schemes, malformed fields,
  resource overages, and ambiguous controls deny the observation.
- **URL-policy completeness and persistence.** The helper must return a
  versioned receipt proving an unpruned focused-window tree at the effective
  `ax_depth`. Two snapshots must retain identical exact-window identity and
  address/full-scan evidence, including provenance and source paths. This is a
  race mitigation, not an atomic browser transaction. On success, schema v5 /
  policy v3 `url_metadata_only` persistence keeps app, bundle, PID,
  `CGWindowID`, bounds, and the approved explicit URL; raw AX, focused content,
  AX metadata, and screenshots are absent, and visible text/titles are empty.
  Scheme-less candidates are conservatively evaluated as both HTTP and HTTPS,
  remain `null` in S1, and are ultimately rejected because they cannot supply
  explicit durable URL evidence. Malformed configuration denies every app
  before AX. The retained URL is an editable address-control value, not proof
  that navigation committed or that the document was loaded; a typed-but-not-
  submitted value can therefore yield a URL-only activity record.
- **`deny_unknown_windows`.** Defaults to `true`. If macOS active-window
  policy metadata cannot provide a bundle ID, it cannot bypass a rule. The
  capture scheduler is stricter regardless of this compatibility knob: every
  observation always requires an unambiguous exact `WindowMeta` containing
  app, bundle, title, PID, `CGWindowID`, and valid bounds.
- **`include_screenshot`.** Defaults to `false`. Screenshots are not consumed by
  the current memory stages. When enabled, the native helper captures only the
  verified `CGWindowID` through CoreGraphics and rechecks the complete identity
  before and after pixels/encoding; Python validates the returned JPEG and
  identity. There is no full-display, monitor, bounds-crop, or `mss` fallback.
  Missing Screen Recording permission or any helper/image/identity failure
  drops the entire observation rather than retaining AX-only content.
  Screenshots additionally require both URL rule lists to be empty.
- **Desktop privacy view.** The Stage 1 shell displays these effective rules and
  retention values read-only. It intentionally does not rewrite `config.toml`;
  safe editing still requires a comment-preserving, allowlisted, etag-bound
  settings service. Pause/resume is the only immediate privacy mutation.
- **`ax_depth`.** Native Cocoa apps are often fine at 20. Electron apps (Claude
  Desktop, VS Code, Slack, Notion) put user content past layer 20 — stay at 100
  unless you're CPU-constrained. When URL policy is active, reaching this depth
  is a fail-closed observation error rather than silent truncation.
- **`debounce_seconds`.** Lower = more captures during typing; higher = fewer near-duplicates.
- **`same_window_dedup_seconds`.** When the user types for a long time in the same document, this is the knob that decides how frequently you re-capture the same (bundle, window) pair. Focus changes always bypass this.
- **`heartbeat_minutes`.** Periodic capture as a safety net. `0` disables it completely (watcher-only). Values `>0` are clamped to a 60s floor.
- **`buffer_retention_hours`.** Whole-JSON deletion cutoff. Default 7 days lets
  `read_recent_capture` reach back that far. Deletion is receipt-gated and
  all-or-none per timeline window: all members must be behind the producer
  boundary, old enough, and exactly match a current complete manifest. Missing,
  changed, late, or unreceipted files retain the whole window.
- **`screenshot_retention_hours`.** After this many hours the screenshot field
  is stripped (rest of the JSON stays). This is intentionally per-file rather
  than whole-window: screenshots are not used by timeline/reducer/classifier and
  are excluded from the semantic receipt digest. Setting this much lower than
  `buffer_retention_hours` makes long retention cheap. `0` or very large values
  keep screenshots for the full window.
- **`buffer_max_mb`.** Best-effort size target in MB. When exceeded, cleanup
  evicts the oldest eligible complete windows toward the target, but never
  splits a window or removes unprocessed/unverifiable captures. Capture-only
  mode or a stalled timeline can therefore exceed it. Set to `0` to disable
  size-based cleanup.

## `[timeline]`

```toml
[timeline]
window_minutes = 1                # wall-clock aligned (:00/:01/:02/...); effective range 1..1440
cold_lookback_minutes = 30        # default seed when no older retained/pending evidence exists
recent_context_blocks = 720       # ~12h of 1-min blocks; consulted by tooling
```

Timeline is always-on and acts as a **verbatim-preserving normalizer** — it de-duplicates snapshots and strips UI chrome but preserves the user's typed text, URLs, titles, and proper nouns unchanged. Real compression happens in the reducer.

`window_minutes` is bound to a durable epoch on the first producer tick, even
when that tick finds no capture. Changing it afterward does **not** create
mixed-size future blocks: production fails closed until you run
`openchronicle clean timeline`, which removes timeline blocks, coverage,
receipts, and the old epoch. The effective value is clamped to `1..1440`;
longer rows are quarantined. The default 1-min size pairs with the reducer's
flush tick (default 5-min) so each flush consumes ~5 blocks. A larger timeline
window cuts LLM calls per hour but risks the model sliding from normalization
into summarization.

`cold_lookback_minutes` is not a data-loss cutoff. On a fresh/legacy state the
producer seeds from the earliest of this default horizon, any valid retained
capture, and any durable pending reducer window. Catch-up is capped per tick
and resumes from its persisted upper bound, so a long outage is recovered over
multiple ticks without blocking the daemon indefinitely.

## `[session]`

```toml
[session]
gap_minutes = 5                 # hard cut: idle > 5 min ends the session
soft_cut_minutes = 3            # soft cut: single unrelated app > 3 min
max_session_hours = 2           # forced cut at 2h
tick_seconds = 30               # check_cuts() interval
flush_minutes = 5               # incremental reducer tick inside an active session (min 5)
```

See [session.md](session.md) for what each rule means and how to tune it.

**Flush ticks.** While a session is still active, every `flush_minutes` the reducer wakes up and compresses any new closed timeline blocks into a partial entry in today's `event-YYYY-MM-DD.md`. This makes long sessions visible in near-real-time instead of waiting for the final cut. Minimum effective value is 5 (clamped) to keep LLM cost bounded — at the default 1-min timeline window, a 5-min flush consumes ~5 blocks. The classifier runs on its own separate cadence (see `[classifier] interval_minutes` below) and does not fire per flush.

## `[reducer]`

```toml
[reducer]
enabled = true                   # session/flush/pending reducer + classifier pipeline
daily_tick_hour = 23             # local-time hour for the daily safety-net tick
daily_tick_minute = 55
```

Setting `enabled = false` disables both the S2 reducer and the classifier. Sessions still close and persist to the `sessions` table, but no event-daily entries or classifier candidates land — useful for capture-only debugging.

## `[classifier]`

```toml
[classifier]
interval_minutes = 30           # durable-fact extraction cadence inside active sessions (min 5)
retry_seconds = 60              # durable delivery base backoff (effective 1..3600)
lease_seconds = 300             # minimum claim/renewal lease (effective min 30)
```

While a session is active, `interval_minutes` controls how much new reducer
coverage must accumulate between `classified_end` (or session start) and the
durable `flush_end` before a periodic delivery is requested. Terminal reduction
stores a separate exact-entry request, or a typed zero-block proof, for the
trailing/final range. Only a
durable commit-or-skip receipt followed by atomic finalization advances
`classified_end`. Stable job/run keys plus proposal slots make local effects
replay-safe even when a provider call repeats. Proposals remain pending until
explicit local review; classifier tools cannot mutate Markdown.

`interval_minutes` values below 5 are clamped to 5. Pair it with
`session.flush_minutes`: the reducer normally materializes event entries more
frequently than the classifier requests them.

`retry_seconds` is the base delay for an unreceipted failed delivery. The
worker applies exponential backoff capped at 3600 seconds; the configured base
is clamped to 1–3600. The same value also influences how often the daemon looks
for new, expired, committed, or due work, but that polling sleep is separately
clamped to 5–60 seconds. It does not configure the LLM client's internal retry
policy.

`lease_seconds` is the minimum SQLite ownership lease used to claim and renew a
classifier job. The effective lease is at least 30 seconds and is automatically
raised to cover the classifier provider's complete call budget plus a 60-second
margin. The worker renews immediately before and after each provider call. An
effective value above 21600 seconds (six hours) fails closed rather than running
without a bounded fence. Lease expiry does not cancel an in-flight provider;
it prevents that stale worker from persisting proposals or a receipt after a
replacement worker claims a new token.

## `[writer]`

```toml
[writer]
soft_limit_tokens = 20000        # compact trigger on any single file above this
hard_limit_tokens = 50000        # emergency ceiling
dedup_window_hours = 24          # dedup search horizon before appending
cold_start_conservative_hours = 0 # 0 = off
max_tool_iterations = 12         # classifier tool-call loop hard cap
```

The old per-capture trigger knobs are gone — the writer is driven by session boundaries now. See [writer.md](writer.md) for the full trigger model.

## `[memory]`

```toml
[memory]
auto_dormant_days = 30           # files untouched this long are marked dormant in the index
```

Dormant files don't show in `list_memories` by default. Pass `include_dormant=true` from the MCP client to see them. They're never deleted automatically.

## `[daily_wrap]`

```toml
[daily_wrap]
enabled = false           # opt in: scheduled synthesis may call a remote model
timezone = ""            # empty = infer system IANA zone; or e.g. "Asia/Shanghai"
hour = 0
minute = 5
retry_seconds = 300       # 30..3600; failure retry + late-evidence recheck cadence
late_data_grace_hours = 6 # 0..24; revise yesterday's wrap during this window
lease_seconds = 300       # 30..21600 minimum; raised to cover the model call budget
```

Scheduled Daily Wrap is disabled by default so an upgrade cannot silently add a
new model call. Once enabled, the daemon synthesizes the previous local day at
this time, catches up once after a late startup, retries failures within the
grace window, and rechecks for late evidence at `retry_seconds` intervals. The
same day, timezone, and input digest are returned from cache; changed evidence
creates a new revision on the same canonical row. See
[stage1-memory-daily-wrap.md](stage1-memory-daily-wrap.md).

## `[prompt_rescue]`

```toml
[prompt_rescue]
enabled = false          # opt in; the configured model receives reviewed user text
poll_seconds = 5        # 1..300; durable queued-job cadence
lease_seconds = 300     # 30..21600 minimum; raised to the model call budget
max_input_chars = 20000 # bound over the complete declared input
max_output_chars = 30000
```

Prompt Rescue starts disabled and accepts only an explicitly reviewed
`manual_paste` source in the first slice. The daemon claims jobs through a
durable lease, calls `[models.prompt_rescue]` with JSON mode and no tools, and
stores a closed prepared-artifact schema. A ready artifact can be reviewed,
edited, copied, retried after a visible sanitized failure, or permanently
deleted. It has no capability to paste into another app or submit on the
user's behalf. The current slice does not claim that pasted text is bound to
an external macOS selection. On macOS, `Command-Shift-Space` instead invokes a
read-only Accessibility adapter while the source app still owns focus; a
successful job records the exact app/window/element/range receipt and is
labeled `macos_selection`. Secure, empty, multiple, changing, excluded, and
URL-policy-unverifiable selections are rejected without clipboard fallback.
The frozen Prompt Rescue evaluator never invokes this model unless
`--run-configured-provider` is supplied and this workflow is enabled. That
explicit runner records a complete raw corpus under the caller-selected path;
keep it under `scratch/` until its contents and provider cost are reviewed.

## `[reply_rescue]`

```toml
[reply_rescue]
enabled = false          # opt in; the configured model receives reviewed conversation text
poll_seconds = 5         # 1..300; durable queued-job cadence
lease_seconds = 300      # 30..21600 minimum; raised to the model call budget
max_input_chars = 50000  # bound over conversation plus all declared directions
max_output_chars = 30000 # prepared reply plus review ledger
```

Reply Rescue starts disabled and its first source is explicitly labeled
`manual_conversation` with `manual_unverified` identity assurance. A pasted
excerpt cannot prove an account, thread, sender, or recipient. The user must
declare participants, intended recipients, reply/reply-all mode, goal, tone,
reviewed style instructions, and commitments. The supervised daemon calls
`[models.reply_rescue]` in JSON mode with no tools and stores a strict no-action
artifact containing the reply, question/warning lists, and a review claim
ledger. The desktop can review, edit, copy, retry, or permanently delete it.
There is no mailbox/OAuth access, provider draft creation, paste, or send
command. Editing clears the generated claim/answered-question ledger so the old
model analysis cannot appear to support newly edited text.

## `[resume_rescue]`

```toml
[resume_rescue]
enabled = false
max_profile_chars = 500000
max_opportunity_chars = 200000
```

Résumé Rescue starts disabled. Its first local slice stores immutable versions
of explicitly reviewed career facts and content-addressed opportunity snapshots.
Facts carry stable IDs, closed provenance, confidentiality, ownership scope,
and unresolved conflict groups. Updating a profile creates a new version
instead of overwriting a source used by an earlier projection. Opportunity
updates preserve immutable snapshots in a digest-CAS supersession chain. The
first deterministic projection can only select exact reviewed fact text; its
job-requirement mappings are explicitly unverified until reviewed. This slice
has no document upload, model call, ATS promise, application form access, or
submission capability.

## `[search]`

```toml
[search]
default_top_k = 5
filter_superseded_by_default = true
```

Both apply to MCP `search` calls. Superseded entries are still searchable with `include_superseded=true`.

## `[mcp]`

```toml
[mcp]
auto_start = true                 # run an always-on MCP server inside the daemon
transport = "streamable-http"     # "streamable-http" | "sse" (deprecated) | "stdio"
host = "127.0.0.1"                # keep localhost-only
port = 8742
```

- `streamable-http` — default. Served at `http://<host>:<port>/mcp`.
- `sse` — legacy. Still works but deprecated.
- `stdio` — don't set this in the daemon config; stdio is for per-client spawns via `openchronicle mcp`.

## Environment overrides

- `OPENCHRONICLE_ROOT=/some/path` — move `~/.openchronicle/` entirely. Good for tests, throwaway envs, or separating work and personal memory.
- `OPENAI_API_KEY` (or whichever `api_key_env` you set) — picked up at runtime.

## Validating changes

The daemon reads config once on startup. After editing `config.toml`:

```bash
openchronicle stop && openchronicle start
openchronicle status
```

`status` prints the resolved model for each stage **and probes each stage's provider** with a tiny round-trip (`max_tokens=4`, ~5s timeout). Each row shows one of:

- `gpt-5.4-nano   ✓ 234 ms` — provider answered.
- `claude-haiku-4-5   ✗ AuthenticationError: …` — provider rejected the request. Typos in `model`, missing `api_key_env`, wrong `base_url`, or expired keys all show up here on the first `status` call instead of silently failing inside the writer hours later.

Probes for stages that share an identical `(model, base_url, api_key)` are deduplicated, so the common case (one model for all stages) makes one network call. They run in parallel and the whole status command stays under ~5s even if one provider is slow. Provider I/O runs outside the capture-store lock; after probes finish, `status` takes only a short cleanup/capture fence to rebuild, authorize, and serialize its final local snapshot.

To skip the network round-trip — e.g. on a flight, in CI, or just to inspect the resolved config — set the mock env var:

```bash
OPENCHRONICLE_LLM_MOCK=1 openchronicle status
# rows show: ✓ mocked
```
