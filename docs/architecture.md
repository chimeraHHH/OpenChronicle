# Architecture

OpenChronicle is a single daemon that ingests capture events, compresses them through a deterministic funnel, and classifies the result into a review inbox. Explicit local approval is the only path from a candidate into durable Markdown memory.

```mermaid
flowchart LR
    W[mac-ax-watcher<br/>Swift binary]

    subgraph capture [Capture Layer]
        direction TB
        S0["<b>S0</b> event_dispatcher<br/>dedup · debounce · min-gap"]
        S1["<b>S1</b> s1_parser<br/>focused_element · visible_text · url"]
        BUF[(capture-buffer/*.json)]
        S0 --> S1 --> BUF
    end

    subgraph compress [Compression Layer]
        direction TB
        TL["Timeline aggregator · LLM<br/>1-min normalized blocks<br/>verbatim-preserving"]
        BLOCKS[(timeline_blocks)]
        SM["Session manager<br/>3-rule cutter<br/>active → ended"]
        S2["<b>S2</b> session_reducer · LLM<br/>async thread"]
        TL --> BLOCKS
        BLOCKS -- read window --> S2
        SM -. trigger: flush 5m / on_session_end .-> S2
    end

    subgraph memory [Memory Layer]
        direction TB
        ED[(event-YYYY-MM-DD.md)]
        CLF["Classifier · LLM<br/>tool-call loop · 30 m tick + terminal"]
        CAND[(memory_candidates<br/>review inbox)]
        REVIEW["Trusted local review<br/>edit · approve · reject · forget"]
        MF[(user- · project- · tool- ·<br/>topic- · person- · org-*.md)]
        WRAP[(Daily Wrap<br/>opt-in · revisioned)]
        CMP["Compact · LLM<br/>on-demand"]
        ED --> CLF --> CAND --> REVIEW --> MF
        ED --> WRAP
        BLOCKS --> WRAP
        MF -. read / rewrite .-> CMP
        CMP -. supersede .-> MF
    end

    subgraph query [Query Layer]
        direction TB
        FTS[(SQLite FTS5<br/>entries_fts · captures_fts)]
        MCP["MCP server<br/>127.0.0.1:8742/mcp"]
        AG[Tool-capable agents<br/>Claude Code · Desktop · Cursor · Codex · …]
        FTS --> MCP --> AG
    end

    W --> S0
    BUF -. pre_capture_hook<br/>(post-write · skipped on content-dedup) .-> SM
    BUF --> TL
    S2 --> ED
    BLOCKS -. grounding .-> CLF
    MF --> FTS
    ED --> FTS
    BUF -. indexed .-> FTS
```

## Runtime sequence

A typical 5-minute flush window, showing how one AX event propagates through to durable memory:

```mermaid
sequenceDiagram
    participant W as mac-ax-watcher
    participant S0 as S0 dispatcher
    participant S1 as S1 parser
    participant BUF as capture-buffer
    participant SM as Session mgr
    participant TL as Timeline tick
    participant R as S2 reducer
    participant CLF as Classifier
    participant DB as SQLite + memory/
    participant MCP as MCP / agent

    W->>S0: AX event
    S0->>S0: debounce / dedup / min-gap
    S0->>S1: schedule capture runner (threaded)
    S1->>BUF: write enriched {iso}.json
    Note right of BUF: content-fingerprint dedup<br/>drops consecutive duplicates
    BUF->>SM: pre_capture_hook → on_event<br/>(post-write · skipped on content-dedup)

    Note over TL,BUF: timeline tick · every 60 s
    TL->>BUF: scan closed 1-min windows
    TL->>DB: LLM → insert timeline_blocks

    Note over SM,R: flush tick · every 5 min
    SM->>R: reduce(flush_end → now)
    R->>DB: read blocks · LLM · append [flush] entry
    R->>SM: advance flush_end

    Note over CLF,DB: classifier tick · every 30 min
    CLF->>DB: read event-daily (tagged sid:)<br/>+ timeline_blocks in window (grounding)
    CLF->>DB: LLM tool-call loop → stage grounded candidates
    CLF->>SM: advance classified_end

    Note over CLF,DB: later, explicit trusted local review
    DB->>DB: revalidate evidence → deterministic Markdown write

    Note over SM,CLF: on_session_end<br/>(idle / soft-cut / timeout / shutdown / 23:55)
    SM->>R: terminal reduce (full trailing range)
    R->>DB: final entry
    R-->>CLF: on_done callback
    CLF->>DB: classify trailing window

    Note over MCP,DB: any time
    MCP->>DB: FTS search / list / read
    DB-->>MCP: results
```

## Tasks in the daemon

Defined in `src/openchronicle/daemon.py`.

| Task | Purpose |
|---|---|
| `capture` | Consumes `mac-ax-watcher` events, debounces, writes enriched JSON captures (incl. S1 fields) to `~/.openchronicle/capture-buffer/`. Heartbeat catches quiet periods. Also calls `SessionManager.on_event` on every capture so the session cutter sees the same signal. |
| `timeline` | Every 60s scans closed wall-clock windows (default 1 min), runs the `timeline` LLM stage for populated windows, and records the inspected interval `[processed_from, processed_through)` (bucket-end proof `(processed_from, processed_through]`) across populated and proven-empty windows. A cold start backfills retained captures/pending sessions in bounded pages. Cleans buffer files only behind the valid upper bound. |
| `session` | Every `session.tick_seconds` (default 30), calls `SessionManager.check_cuts()` so idle-gap and timeout cuts fire even when the dispatcher is quiet. |
| `flush` | Every `session.flush_minutes` (default 5, clamped to 5-min floor), runs the reducer incrementally over the active session's newly closed timeline blocks (~5 of them at defaults) and appends `[flush]`-tagged partial entries to today's event-daily. |
| `classifier-tick` | Every `classifier.interval_minutes` (default 30, min 5), runs the classifier over any event-daily entries appended since the session's `classified_end` bookmark. Silent no-op when no new entries have landed. |
| `pending-reducer` | Every 60s retries durable `ended`/due-`failed` rows. A terminal callback that beats the timeline producer remains `ended` until its final bucket lies inside the producer's durable coverage range. |
| `daily-safety-net` | Once per local day at `reducer.daily_tick_hour:minute` (default 23:55), force-ends the currently-open session and reduces every stranded `ended`/`failed` session row — the "we survived a crash or midnight rollover" safety net. |
| `daily-wrap` | Opt-in worker (disabled by default). After the configured post-midnight time, synthesizes the previous IANA-local day, retries/rechecks within the late-data grace window, and revises one canonical grounded wrap. Provider calls run on cancellable dedicated daemon threads; shutdown revokes the matching lease without waiting for a stuck provider. |
| `mcp` | Hosts the Reader MCP server inside the daemon. Exponential backoff on crash. |

The session cutter itself doesn't have a dedicated task — it runs inline on every persisted, non-duplicate watcher or heartbeat capture via the `pre_capture_hook` wired in `daemon.py`. Session-end callbacks spawn the reducer on a daemon thread; if the final timeline bucket is still pending, the durable row stays queued for `pending-reducer`. A successful terminal write then fires the classifier over the trailing window. Each session's progress on both stages is bookkept on its sessions row: `flush_end` for the reducer, `classified_end` for the classifier.

The daemon also holds a private singleton file lease for its entire lifetime.
CLI `status`/`stop` trust `.pid` only while that lease is held, so a PID reused
after `SIGKILL` cannot cause an unrelated process to be reported or signalled.
Before optional workers start, every normal daemon launch resumes any authorized
memory purge tombstones. This recovery is independent of Daily Wrap enablement.

`--capture-only` is a strict no-model ingestion/debug mode: it disables the
timeline, reducer/flush, classifier, and MCP paths. Capture, session bookkeeping,
and the daily safety-net still run so session rows land on disk.

## The session boundary

Three rules (ported verbatim from Einsia-Partner), all enforced in `session/manager.py`:

1. **Hard cut.** No capture-worthy events for `session.gap_minutes` (default 5) → close the session at the last event's timestamp.
2. **Soft cut.** A single unrelated app is focused for `session.soft_cut_minutes` (default 3) unless ≥2 distinct apps were focused in the preceding 2 minutes (frequent-switching defuses the rule).
3. **Timeout.** A session older than `session.max_session_hours` (default 2) is force-cut regardless.

Force-end is also called on graceful daemon shutdown and on the 23:55 safety net.
After a hard crash, startup recovery closes orphaned active rows at a safe
inferred boundary, preferring persisted timeline evidence without crossing the
restart, the next session, or the configured maximum duration.

## On-disk state

```
~/.openchronicle/
├── config.toml               # single source of truth for runtime config
├── .pid                      # daemon PID; absence ⇒ stopped
├── .paused                   # sentinel — capture skips while present
├── index.db                  # SQLite WAL; projections, provenance, candidates, wraps
├── capture-buffer/           # S1-enriched {iso8601}.json captures
├── memory/
│   ├── index.md              # auto-generated overview
│   ├── event-YYYY-MM-DD.md   # one file per day, one entry per reduced session
│   ├── user-*.md             # identity, preferences (durable)
│   └── project-*.md / tool-*.md / topic-*.md / person-*.md / org-*.md
└── logs/
    ├── capture.log           # watcher events, dedup, writes
    ├── timeline.log          # window scan, block production, buffer cleanup
    ├── session.log           # cut decisions, session-end events
    ├── writer.log            # reducer + classifier runs, tool calls
    ├── compact.log           # compact rounds with preservation ratios
    ├── daily-wrap.log        # scheduled wrap attempts and outcomes
    └── daemon.log            # lifecycle + MCP server
```

SQLite is opened with WAL mode — the MCP reader and the writer paths coexist without blocking.

## Code layout

```
src/openchronicle/
├── cli.py                    # Typer entry point
├── daemon.py                 # Async task orchestration
├── config.py                 # TOML loader, per-stage ModelConfig inheritance
├── paths.py                  # ~/.openchronicle/* paths
├── logger.py                 # Rotating file sinks per component
├── capture/
│   ├── watcher.py            # Spawns mac-ax-watcher, parses JSONL
│   ├── event_dispatcher.py   # Debounce / dedup / min-gap
│   ├── ax_capture.py         # One-shot mac-ax-helper invocation
│   ├── ax_models.py          # ax_tree_to_markdown, prune helpers
│   ├── s1_parser.py          # Enriches captures with focused_element / visible_text / url
│   ├── screenshot.py         # mss + PIL → base64 JPEG
│   ├── window_meta.py        # foreground app / title / bundle_id
│   └── scheduler.py          # Capture loop + buffer cleanup
├── timeline/
│   ├── store.py              # timeline_blocks schema + CRUD
│   ├── aggregator.py         # Captures-in-window → LLM → entries list
│   └── tick.py               # Every-minute scan for closed windows
├── session/
│   ├── store.py              # sessions table + retry bookkeeping
│   ├── manager.py            # 3-rule session cutter
│   └── tick.py               # Daemon wiring: check_cuts loop + daily safety net
├── writer/
│   ├── agent.py              # CLI entry: catch up pending sessions + classify
│   ├── session_reducer.py    # S2: session → event-YYYY-MM-DD.md entry
│   ├── classifier.py         # Proposes grounded durable facts for review
│   ├── tools.py              # classifier read/search/propose/commit boundary
│   ├── compact.py            # Per-file compaction with fact-preservation check
│   └── llm.py                # litellm wrapper; per-stage config
├── provenance/               # Typed evidence refs and rebuildable edge graph
├── memory_candidates/        # Review-inbox rows and purge tombstones
├── daily_wrap/               # Canonical job store, service, scheduler
├── services/                 # Context assembly and trusted memory mutations
├── store/
│   ├── fts.py                # SQLite FTS5 schema, search, cursor context manager
│   ├── files.py              # Markdown + YAML frontmatter IO
│   ├── entries.py            # Entry format, supersede logic, rebuild_index
│   └── index_md.py           # Rebuild memory/index.md from the files table
├── mcp/
│   ├── server.py             # FastMCP server + tool definitions
│   └── captures.py           # Read-side helpers for raw capture buffer + captures_fts
└── prompts/
    ├── timeline_block.md     # short-window normalizer (verbatim-preserving)
    ├── session_reduce.md     # S2 reducer
    ├── classifier.md         # Durable-fact extraction
    ├── compact.md            # Compaction
    └── schema.md             # Full memory spec — also returned by MCP get_schema
```

## Why this shape

- **Compression first, review before durable memory.** S1 → Timeline → S2 is a deterministic funnel with bounded prompt size at each step. The classifier can only stage evidence-linked candidates; a trusted local approval revalidates source hashes before materializing Markdown.
- **Session as the natural unit.** A "session" — a bounded chunk of focused work — is what humans remember. Cutting on idle / app-switch / timeout produces event-daily entries with accurate time ranges, which solves the v1 problem of long sessions being under-reported after the first append.
- **Periodic classifier, bookmarked.** The classifier fires on a 30-min interval during each active session, then attempts one last trailing-window pass at session end. Each successful pass advances `classified_end`, reducing repeat work while long sessions remain open. Crash scheduling is best-effort and has both duplicate-work and missed-terminal-pass windows, so classifier tools deduplicate against existing memory and the bookmark is not an exactly-once contract.
- **Daily event files.** `event-YYYY-MM-DD.md` sorts alphabetically by day. Weekly files from v1 are left untouched — they stay searchable via FTS.
- **One process, many tasks.** Avoids IPC overhead and keeps `index.db` single-writer in practice. SQLite WAL gives the MCP reader what it needs.
- **MCP inside the daemon.** External MCP clients get a stable localhost URL instead of spawning a fresh stdio subprocess per session.
