# Writer

The writer is two LLM stages wired behind session boundaries:

1. **S2 reducer** (`writer/session_reducer.py`) — writes incremental `[flush]` entries during an active session and a final entry when it closes, both to `event-YYYY-MM-DD.md`.
2. **Classifier** (`writer/classifier.py`) — runs on a timer during the active session, then one last trailing-window pass at the terminal reduce. Scans the newly-appended event-daily entries for durable facts and proposes reviewable memory candidates. Only an explicit local approval writes a candidate to `user-/project-/tool-/topic-/person-/org-*.md`.

Both the reducer and the classifier are periodic during long sessions. The reducer flushes every `session.flush_minutes` so event-daily surfaces activity in near-real-time; the classifier fires every `classifier.interval_minutes` (default 30, min 5) so durable facts can reach the local review inbox without waiting for the session to close. Each stage tracks progress on the sessions row (`flush_end` for the reducer, `classified_end` for the classifier). Reducer entry materialization is lock-serialized and replay-idempotent; a lightweight pending tick retries terminal callbacks that arrived before their timeline bucket. Classifier proposals use a stable run key and proposal slot, so a repeated run reuses the first persisted candidate instead of silently replacing content the user may already have reviewed. Session boundaries come from `session/manager.py` (see [session.md](session.md)).

## Triggers

| Trigger | What fires |
|---|---|
| Flush tick (every `session.flush_minutes`, min 5) | `flush_active_session` runs the reducer with `is_final=False` over new closed blocks since `flush_end`. Appends a `[flush]`-tagged entry to today's event-daily. The classifier does **not** fire. |
| Classifier tick (every `classifier.interval_minutes`, min 5) | For the currently-active session, proposes candidates from entries appended since `classified_end` (fallback: `session_start`). On a committed pass advances `classified_end`. Silent no-op when no new entries landed since last tick. |
| `SessionManager.on_session_end` callback | `reduce_session_async` spawns a daemon thread with `is_final=True`, covering whatever wasn't flushed yet. On success, its `on_done` callback invokes the classifier over the trailing window `[classified_end, now)` — whatever the 30-min tick hadn't reached yet. |
| Pending reducer tick (every 60s) | Retries `ended` rows after the timeline producer watermark reaches their final wall-clock bucket, and due `failed` rows after backoff. Per-session locks make overlap with the immediate callback safe. |
| Daily 23:55 safety-net cron | `reduce_all_pending` picks up any `ended`/`failed` session rows whose async work didn't finish (e.g. daemon crashed mid-reduce). Also force-ends the currently-open session so day boundaries are clean. |
| Daily Wrap worker (opt-in; 00:05 local time by default) | Builds the previous local day's evidence snapshot and creates or revises one canonical, read-only Daily Wrap. It is disabled by default and independent of the 23:55 reducer safety net. |
| `openchronicle writer run` (CLI) | Same as the safety net — useful manually after pulling new code or for recovery. |

## Stage 1 — S2 reducer

For each session that ended, `reduce_session`:

1. Reads `timeline_blocks` in `[flush_end or session.start, session.end)` from SQLite. Flushes select only fully closed blocks; a terminal pass may include the wall-clock block straddling the exact session end.
2. Before terminal finalization, requires the final bucket-end boundary to lie inside the durable timeline proof range `(processed_from, processed_through]` (or have an already-materialized straddling block). The reducer snapshots that range before reading blocks, preventing a newer watermark from certifying an older block read. If the bucket is still pending, the session remains `ended` for the 60s retry tick. Only a proven-empty range is marked `reduced` as a no-op.
3. Renders the blocks into `prompts/session_reduce.md` and calls the `reducer` LLM stage with `json_mode=True`.
4. Parses `{summary: str, sub_tasks: [str]}`. Each sub_task must look like `[HH:MM-HH:MM, <app>] <action>, involving <...>`.
5. Appends one entry to `event-YYYY-MM-DD.md` (the date of `session.start`). Entry header: `**Session <sid>** (HH:MM–HH:MM)` for terminal reduces, or `**Session <sid> [flush]** (HH:MM–HH:MM)` for flush passes. Flush entries carry a `flush` tag alongside `sid:<sid>` so they're easy to filter later.
6. A flush advances `flush_end`; a terminal reduce instead sets
   `status=reduced`. Terminal materialization uses a deterministic entry ID so
   crash replay reuses the existing Markdown entry and repairs its projection.

### Retry + heuristic fallback

If the LLM call fails or returns unparseable JSON:

- **Retry queue.** Backoff schedule `5 / 15 / 30 / 60 / 120` minutes (verbatim from Einsia). The session row moves to `status=failed` with `next_retry_at` set; the daily safety-net picks it up. (Flush failures don't schedule retries — the next flush covers a bigger window, and the terminal reduce is authoritative.)
- **Exhausted retries.** A heuristic entry is written (one sub_task per distinct app, tagged `heuristic`), and the row is marked `reduced`. A session is never silently lost.

### Event-daily file ownership

Session entries in event-daily files are owned by the reducer. The classifier has no Markdown mutation tools at all. The separate Daily Wrap worker stores its canonical output in SQLite; it does not rewrite reducer-owned entries.

## Stage 2 — Classifier

Two entry points, same core (`classifier.classify_window`):

- **Tick path** — `session/tick.run_classifier_tick` fires every `classifier.interval_minutes` (default 30). For the currently-active session, it classifies the window `[classified_end or session_start, now)` and, on a committed pass, advances `classified_end` so the next tick picks up where it left off.
- **Terminal path** — the reducer's `on_done` callback classifies the trailing window `[classified_end or session_start, now)` right after the final reduce lands. This covers whatever the tick didn't reach (sessions shorter than one interval, or the tail between the last tick and the session close).

Both paths assemble the same prompt inputs:

1. The event-daily entries tagged `sid:<session_id>` whose timestamps fall in the window — these are the focus entries.
2. The timeline blocks covering the window — verbatim-preserving activity slices so the classifier can ground any durable fact against raw evidence.
3. The preceding-day's trailing entries as dedup context (terminal path only — the tick runs inside the day so it doesn't need cross-day context).
4. The memory-file index filtered to exclude `event-*` files.

Both paths then run a bounded, review-first tool-call loop over `writer/tools.py`:

| Tool | Purpose |
|---|---|
| `read_memory(path, tail_n?)` | Fetch a durable (non-`event-*`) memory file's frontmatter + last 1–20 entries (default 10). |
| `search_memory(query, top_k?, include_superseded?)` | BM25 search over durable, current, non-tombstoned memory only; `top_k` is 1–20. |
| `propose_memory_candidate(kind, path, content, tags, evidence_tokens, confidence?, conflict_key?)` | Persist a pending candidate whose evidence tokens must have been authorized by the current prompt or an actual read/search result. It does not mutate Markdown. |
| `commit(summary)` | End the round. Always called exactly once. |

Iteration cap: `writer.max_tool_iterations = 12`.

The prompt is biased toward **doing nothing**: default action is an empty `commit` if no durable signal is present. Raw activity ("used Cursor for 2h", "played Slay the Spire") is explicitly *not* classifiable — that's already captured in the event-daily entry. Pending candidates are listed, edited, approved, rejected, or purged through the explicit local CLI/service boundary; the read-only MCP surface cannot approve them.

## Approval and provenance

Every approved entry embeds exactly one final-line `oc-provenance` JSON comment in canonical Markdown. Malformed, duplicate, or embedded markers fail closed instead of truncating user text. Candidate bodies that contain a canonical entry heading or provenance marker are rejected before approval, preventing quoted screen content from creating a second record. The SQLite provenance graph is a query projection and can be rebuilt from valid comments.

Proposal creation revalidates evidence and atomically commits the candidate, conflict classification, and source edges under an immediate SQLite transaction. Approval uses a deterministic entry ID, revision compare-and-swap, current-evidence hash checks, and exact replay validation. A re-entrant cross-process review-operation lock serializes proposal, edit, approval, rejection, purge, provenance-bearing append/supersede, and full provenance rebuild operations. Every dependent append rechecks its source inside that fence, so it either precedes a purge and enters the captured closure or follows it and fails closed. Supersede replacements cite the post-strike source entry, and rebuild resolves embedded memory dependencies to a fixed point rather than filename order. Purge combines SQLite edges with valid Markdown frames before atomically writing content-free tombstones; normal daemon startup always resumes an interrupted forget operation.

Compaction currently fails closed for files containing provenance-bearing entries. Provenance-preserving compaction and deterministic supersede approval remain later-stage work; see [Stage 1 memory and Daily Wrap](stage1-memory-daily-wrap.md).

## Sessions table

```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,             -- sess_<12hex>
  start_time TEXT NOT NULL,
  end_time TEXT,
  status TEXT NOT NULL,            -- active | ended | reduced | failed
  flush_end TEXT,                  -- reducer bookmark: upper bound of last reduced window
  classified_end TEXT,             -- classifier bookmark: upper bound of last classifier pass
  retry_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  owner_pid INTEGER,                -- daemon that opened an active row
  owner_token TEXT                  -- per-process instance identity
);
```

Lives in `index.db` alongside `entries` / `files` / `timeline_blocks`. The reducer uses it to bookkeep retries; the safety-net cron uses `status IN ('ended','failed')` to find anything still owed work. Existing databases gain nullable owner columns in place. A legacy active row with no owner identity is treated as an orphan and safely ended during the next startup recovery pass.

## Per-stage model picks

Defaults inherit from `[models.default]`. Override in `config.toml`:

- **`[models.reducer]`** — prompt is short (timeline blocks are already compressed), but output precision matters (time ranges, per-app attribution). A mid-tier model is usually the right trade-off.
- **`[models.classifier]`** — accuracy-sensitive. The classifier decides what becomes long-term memory; a weak model here means either missed facts or poisoned dedup.
- **`[models.daily_wrap]`** — grounds a one-day progress/open/blocked digest against bounded, policy-filtered evidence. It may inherit the default model or use a stronger summarizer.
- **`[models.timeline]`** — runs every minute of activity as a verbatim-preserving normalizer. Keep it cheap, but don't go too weak — a too-weak model will summarize instead of normalizing and drop authored text.
- **`[models.compact]`** — runs only when files fatten. Match reducer or stronger.

## Logs

```
~/.openchronicle/logs/writer.log    # reducer + classifier tool-call loops, commit summaries
~/.openchronicle/logs/session.log   # flush tick + classifier tick + terminal reduce callback lines
~/.openchronicle/logs/compact.log   # compact rounds + preservation ratios
~/.openchronicle/logs/daily-wrap.log # scheduled Daily Wrap attempts and outcomes
```

A flush (every 5 min) produces one reducer line in writer.log + a "flushed" line in session.log. A classifier tick (every 30 min) produces either a "skipped (no session entries in window)" line in session.log, or a candidate-summary + tool-call trail in writer.log. At session-end you'll see the terminal reducer followed by the terminal classifier callback in session.log; the classifier's own tool calls land in writer.log.
