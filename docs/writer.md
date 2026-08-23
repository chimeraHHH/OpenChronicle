# Writer

The writer is two LLM stages wired behind session boundaries:

1. **S2 reducer** (`writer/session_reducer.py`) — writes incremental `[flush]` entries during an active session and a final entry when it closes, both to `event-YYYY-MM-DD.md`.
2. **Classifier** (`writer/classifier.py`) — consumes durable periodic and terminal delivery jobs, scans the referenced event-daily evidence for durable facts, and proposes reviewable memory candidates. Only an explicit local approval writes a candidate to `user-/project-/tool-/topic-/person-/org-*.md`.

Both the reducer and the classifier are periodic during long sessions. The reducer flushes every `session.flush_minutes` so event-daily surfaces activity in near-real-time; the classifier requests coverage every `classifier.interval_minutes` (default 30, min 5) so durable facts can reach the local review inbox without waiting for the session to close. `flush_end` proves reducer materialization, while `classified_end` advances only after a durable classifier commit receipt is finalized. Reducer entry materialization is lock-serialized and replay-idempotent. Classifier work is serialized per session in the SQLite `classifier_jobs` outbox; stable delivery/run keys and proposal slots make crash replay reuse persisted candidates instead of silently replacing content the user may already have reviewed. Session boundaries come from `session/manager.py` (see [session.md](session.md)).

## Triggers

| Trigger | What fires |
|---|---|
| Flush tick (every `session.flush_minutes`, min 5) | `flush_active_session` runs the reducer with `is_final=False` over new closed blocks since `flush_end`. Appends a `[flush]`-tagged entry to today's event-daily. The classifier does **not** fire. |
| Classifier delivery loop | Polls every 5–60 seconds. Once an active session has at least `classifier.interval_minutes` of new, durably flushed coverage, it enqueues/coalesces a periodic job through the exact `flush_end` boundary, then drains due jobs. |
| `SessionManager.on_session_end` callback | `reduce_session_async` spawns a daemon thread with `is_final=True`, covering whatever was not flushed yet. The reducer stores either the exact final entry ID/path or a typed zero-block proof as terminal intent. The callback merely prompts a recovery pass; startup/periodic recovery recreates the request if the callback is lost. |
| Pending reducer tick (every 60s) | Retries `ended` rows after the timeline producer watermark reaches their final wall-clock bucket, and due `failed` rows after backoff. Per-session locks make overlap with the immediate callback safe. |
| Daily 23:55 safety-net cron | `reduce_all_pending` picks up any `ended`/`failed` session rows whose async work didn't finish (e.g. daemon crashed mid-reduce). Also force-ends the currently-open session so day boundaries are clean. |
| Daily Wrap worker (opt-in; 00:05 local time by default) | Builds the previous local day's evidence snapshot and creates or revises one canonical, read-only Daily Wrap. It is disabled by default and independent of the 23:55 reducer safety net. |
| `openchronicle writer run` (CLI) | Reduces pending sessions, recovers terminal requests, and drains due classifier jobs — useful manually after pulling new code or after a crash. |

## Stage 1 — S2 reducer

For each session that ended, `reduce_session`:

1. Reads `timeline_blocks` in `[flush_end or session.start, session.end)` from SQLite. Flushes select only fully closed blocks; a terminal pass may include the wall-clock block straddling the exact session end.
2. Before terminal finalization, requires the final bucket-end boundary to lie inside the durable timeline proof range `(processed_from, processed_through]` (or have an already-materialized straddling block). The reducer snapshots that range before reading blocks, preventing a newer watermark from certifying an older block read. If the bucket is still pending, the session remains `ended` for the 60s retry tick. Only a proven-empty range is marked `reduced` as a no-op.
3. Renders the blocks into `prompts/session_reduce.md` and calls the `reducer` LLM stage with `json_mode=True`.
4. Parses `{summary: str, sub_tasks: [str]}`. Each sub_task must look like `[HH:MM-HH:MM, <app>] <action>, involving <...>`.
5. Appends one entry to `event-YYYY-MM-DD.md` (the date of `session.start`). Entry header: `**Session <sid>** (HH:MM–HH:MM)` for terminal reduces, or `**Session <sid> [flush]** (HH:MM–HH:MM)` for flush passes. Every new reducer entry carries `sid:<sid>` and an `oc-window-end:<encoded instant>` coverage tag; flush entries additionally carry `flush`.
6. A flush advances `flush_end`. A terminal reduce instead sets `status=reduced` and records `classifier_terminal_pending`, the exact deterministic terminal entry ID, and its authoritative event-daily path. If durable timeline coverage proves that the terminal range contains zero blocks, it records a typed `classifier_terminal_noop` proof instead of inventing an entry. Crash replay reuses an existing Markdown entry, repairs its projection, and preserves the same terminal intent.

The reducer snapshots a content-generation fence before reading evidence. Entry
repair/publication and the matching session bookmark or terminal intent are
performed under the review-operation lock only if that generation is still
current. Explicit timeline/memory cleanup bumps the generation first, so an
in-flight pre-clean model result cannot republish deleted evidence or advance a
post-clean bookmark.

### Retry + heuristic fallback

If the LLM call fails or returns unparseable JSON:

- **Retry queue.** Backoff schedule `5 / 15 / 30 / 60 / 120` minutes (verbatim from Einsia). The session row moves to `status=failed` with `next_retry_at` set; the daily safety-net picks it up. (Flush failures don't schedule retries — the next flush covers a bigger window, and the terminal reduce is authoritative.)
- **Exhausted retries.** A heuristic entry is written (one sub_task per distinct app, tagged `heuristic`), and the row is marked `reduced`. A session is never silently lost.

### Event-daily file ownership

Session entries in event-daily files are owned by the reducer. The classifier has no Markdown mutation tools at all. The separate Daily Wrap worker stores its canonical output in SQLite; it does not rewrite reducer-owned entries.

## Stage 2 — Classifier

There are two durable request kinds, executed by the same
`classifier_delivery` worker and `classifier.classify_window` core:

- **Periodic** — the delivery loop may request
  `[classified_end or session_start, flush_end]` only when the session's
  durable `flush_end` proves that coverage and the unclassified span has
  reached `classifier.interval_minutes`. Unclaimed requests coalesce. A claim
  freezes `window_end`; a later request extends `requested_end`, and successful
  finalization creates a contiguous follow-up rather than widening an
  in-flight evidence snapshot. Focus entries must carry `sid:<session_id>` and
  an `oc-window-end` boundary in `(window_start, window_end]`.
- **Terminal** — terminal reduction durably records that classification is
  owed, including the exact final reducer entry ID and authoritative file path.
  Recovery scans reduced sessions and enqueues this job even when the reducer
  callback or process was lost. The exact terminal entry is included by ID
  regardless of its display timestamp or trailing-window boundary; a missing
  or changed ID fails closed. A missing ID alone never authorizes a skip. An
  empty terminal job is allowed only when the reducer persisted a zero-block
  proof and either there is no flush prefix or the classifier cursor is at or
  beyond `flush_end`. Its only valid result is the typed
  `EMPTY_TERMINAL_SKIP` (`skipped_reason="proven_empty_terminal"`) receipt with
  no summary or mutation IDs.

Both paths assemble the same prompt inputs:

1. The focus entries proved by the request kind: periodic `sid:<session_id>` entries with coverage boundaries in the frozen window, or the terminal entry with the exact persisted ID.
2. The timeline blocks covering the window — verbatim-preserving activity slices so the classifier can ground any durable fact against raw evidence.
3. The preceding day's trailing entries as dedup context on the first classifier delivery for a session.
4. The memory-file index filtered to exclude `event-*` files.

Both paths then run a bounded, review-first tool-call loop over `writer/tools.py`:

| Tool | Purpose |
|---|---|
| `read_memory(path, tail_n?)` | Fetch a durable (non-`event-*`) memory file's frontmatter + last 1–20 entries (default 10). |
| `search_memory(query, top_k?, include_superseded?)` | BM25 search over durable, current, non-tombstoned memory only; `top_k` is 1–20. |
| `propose_memory_candidate(kind, path, content, tags, evidence_tokens, confidence?, conflict_key?)` | Persist a pending candidate whose evidence tokens must have been authorized by the current prompt or an actual read/search result. It does not mutate Markdown. |
| `commit(summary)` | End a model-driven round. Called exactly once; the proven-empty terminal path does not call the provider or this tool. |

Iteration cap: `writer.max_tool_iterations = 12`.

The prompt is biased toward **doing nothing**: default action is an empty `commit` if no durable signal is present. Raw activity ("used Cursor for 2h", "played Slay the Spire") is explicitly *not* classifiable — that's already captured in the event-daily entry. Pending candidates are listed, edited, approved, rejected, or purged through the explicit local CLI/service boundary; the read-only MCP surface cannot approve them.

### Durable delivery state machine

`classifier_jobs` is a per-session SQLite outbox. A partial unique index allows
only one active (`pending`, `running`, `failed`, or `committed`) delivery for a
session, and the job ID deterministically binds the session plus the periodic
window start or exact terminal entry identity.

```text
pending -> running                 claim(token, expiry)
running -> failed -> running       unreceipted error, then due retry
expired running -> running         reclaim with a fresh token
running -> committed               persist typed receipt
committed -> succeeded             atomic bookmark finalization
succeeded -> new pending job       contiguous periodic follow-up, if requested
committed -> succeeded             restart recovery; no model call
```

A claim assigns a fresh lease token. A live lease prevents a second worker
from claiming the job; expiry permits recovery with a new token and fences the
old worker. The worker renews the same token immediately before and after each
provider call. Proposal mutations and commit publication assert the matching,
unexpired token inside their SQLite transactions, so an expired worker cannot
land candidates or a receipt after replacement.

Each attempt binds an `input_digest` over the session, authoritative file,
exact window, and sorted evidence IDs/hashes, plus a producer run key derived
from the job ID and digest. An unchanged retry reproduces both and replays the
same candidate slots. If valid evidence changed between an uncommitted failed
attempt and its retry, the same frozen job/window atomically rebinds to the new
digest and a new run key; pending candidates from the superseded turn become
explicit conflicts instead of being reused as current output. Before each
proposal mutation and again inside the commit transaction, the classifier
re-reads the focus entries and timeline evidence, checks provenance/source
liveness and pending-purge state, and recomputes the digest. A change during a
live attempt, or missing/edited/superseded/purged evidence, still fails closed
rather than committing against a stale prompt snapshot.

The explicit `commit` tool persists a validated receipt before returning from
the tool loop. Its JSON shape is:

```text
committed: bool
summary: str
written_ids: list[str]
created_paths: list[str]
candidate_ids: list[str]
skipped_reason: str
```

`candidate_ids` is reconstructed from SQLite by the bound producer run key,
not trusted from model/tool memory. A normal receipt must be a commit with no
skip reason. The only non-commit receipt is the proven-empty terminal form
described above; arbitrary skip strings, mixed commit/skip receipts, or empty
receipts fail validation. The receipt is byte-bounded and stored in the
`committed` state. Finalization then advances `sessions.classified_end` and
marks the job `succeeded` in one SQLite transaction. A terminal finalization
clears only the still-matching exact terminal intent. A periodic finalization
also creates any contiguous follow-up requested while its window was frozen.
If the process dies after receipt persistence but before finalization, recovery
finalizes `committed` directly without another model call.

This is a delivery guarantee, not an exactly-once provider-call guarantee. A
process can die during a provider call before any receipt exists, so a later
worker may call the model again; deterministic keys, source revalidation, and
lease-fenced mutations make that replay safe. Likewise, `committed` means the
classifier delivery ended durably. It does **not** mean a memory candidate was
approved: trusted local review remains the only path to durable Markdown.

## Approval and provenance

Every approved entry embeds exactly one final-line `oc-provenance` JSON comment in canonical Markdown. Malformed, duplicate, or embedded markers fail closed instead of truncating user text. Candidate bodies that contain a canonical entry heading or provenance marker are rejected before approval, preventing quoted screen content from creating a second record. The SQLite provenance graph is a query projection and can be rebuilt from valid comments.

Proposal creation revalidates evidence and atomically commits the candidate, conflict classification, and source edges under an immediate SQLite transaction. Approval uses a deterministic entry ID, revision compare-and-swap, current-evidence hash checks, and exact replay validation. A re-entrant cross-process review-operation lock serializes proposal, edit, approval, rejection, purge, provenance-bearing append/supersede, and full provenance rebuild operations. Every dependent append rechecks its source inside that fence, so it either precedes a purge and enters the captured closure or follows it and fails closed. Supersede replacements cite the post-strike source entry, and rebuild resolves embedded memory dependencies to a fixed point rather than filename order. Purge combines SQLite edges with valid Markdown frames before atomically writing content-free tombstones; normal daemon startup always resumes an interrupted forget operation.

Compaction accepts only non-empty files whose entries are all explicit
`oc-origin:manual-v1` roots with unique canonical IDs and no provenance.
Automation-origin, unmarked legacy, invalid-origin, and provenance-bearing files
fail closed before provider egress. Accepted output must preserve every entry's
ID, timestamp, order, origin marker, and provenance-free status. Local
frontmatter remains authoritative, and stale-snapshot writeback is rejected.
Provenance-preserving compaction and deterministic supersede approval remain
later-stage work; see [Stage 1 memory and Daily Wrap](stage1-memory-daily-wrap.md).

## Sessions table

```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,             -- sess_<12hex>
  start_time TEXT NOT NULL,
  end_time TEXT,
  status TEXT NOT NULL,            -- active | ended | reduced | failed
  flush_end TEXT,                  -- reducer bookmark: upper bound of last reduced window
  classified_end TEXT,             -- finalized classifier coverage boundary
  classifier_terminal_pending INTEGER NOT NULL DEFAULT 0,
  classifier_terminal_entry_id TEXT NOT NULL DEFAULT '',
  classifier_terminal_path TEXT NOT NULL DEFAULT '',
  classifier_terminal_noop INTEGER NOT NULL DEFAULT 0,
  retry_count INTEGER NOT NULL DEFAULT 0,
  next_retry_at TEXT,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  owner_pid INTEGER,                -- daemon that opened an active row
  owner_token TEXT                  -- per-process instance identity
);
```

Lives in `index.db` alongside `entries`, `files`, `timeline_blocks`, and the
`classifier_jobs` outbox. The reducer uses it to bookkeep retries; the
safety-net cron uses `status IN ('ended','failed')` to find anything still owed
reduction. The classifier recovery scan treats a reduced row with terminal
intent, or a classifier bookmark visibly behind the session end, as still owed
delivery. Existing databases gain the new columns in place. A legacy active
row with no owner identity is treated as an orphan and safely ended during the
next startup recovery pass.

## Per-stage model picks

Defaults inherit from `[models.default]`. Override in `config.toml`:

- **`[models.reducer]`** — prompt is short (timeline blocks are already compressed), but output precision matters (time ranges, per-app attribution). The default is `codex_cli:gpt-5.6-sol`.
- **`[models.classifier]`** — accuracy-sensitive. The classifier decides what becomes long-term memory; a weak model here means either missed facts or poisoned dedup. The default is `codex_cli:gpt-5.6-sol`; Codex only returns declarative tool requests, which the bounded local loop validates and executes.
- **`[models.daily_wrap]`** — grounds a one-day progress/open/blocked digest against bounded, policy-filtered evidence. It may inherit the default model or use a stronger summarizer.
- **`[models.timeline]`** — runs every minute of activity as a verbatim-preserving normalizer. It defaults to `codex_cli:gpt-5.6-luna`; a direct LiteLLM/API Luna override avoids Codex CLI's fixed startup context in high-volume deployments.
- **`[models.compact]`** — runs only when files fatten. Match reducer or stronger.

## Logs

```
~/.openchronicle/logs/writer.log    # reducer + classifier tool-call loops
~/.openchronicle/logs/session.log   # flush/recovery ticks + delivery outcomes/errors
~/.openchronicle/logs/compact.log   # compact rounds + preservation ratios
~/.openchronicle/logs/daily-wrap.log # scheduled Daily Wrap attempts and outcomes
```

A flush (every 5 min) produces one reducer line in `writer.log` plus a
"flushed" line in `session.log`. Classifier delivery logs a receipted skip,
candidate count, no-write commit, or failure in `session.log`; the classifier's
tool-call trail lands in `writer.log`. `openchronicle status` summarizes active
outbox counts (`pending`, `running`, `failed`, and `committed`) plus
`terminal-owed` session intents. `succeeded` history remains queryable in
SQLite but is omitted from the compact status line.
