# Timeline

The timeline is the mandatory **verbatim-preserving normalizer** between raw captures and the S2 reducer. Always-on — there is no toggle.

- Raw AX trees run 200–400 KB for Electron apps. Passing a session's raw captures directly to the reducer would blow out the prompt budget.
- But downstream stages depend on knowing *what the user actually typed* — a TODO, a message draft, a search query, a URL, a window title. For normal captures, timeline is careful not to throw that content away: it strips UI chrome and collapses duplicates while keeping authored text, URLs, titles, and proper nouns verbatim. Active URL policy deliberately supplies only the approved explicit URL and identity metadata; it clears all page/focused/title content before timeline.
- The real compression happens one stage later in the session reducer, which consumes a batch of timeline blocks per flush.

## Wall-clock alignment

Windows are aligned to the daemon generation's logical wall clock: with the
default `window_minutes = 1`, that's `[10:00, 10:01)`, `[10:01, 10:02)`, …,
**not** rolling from an arbitrary capture. The logical clock takes one real
IANA-local wall anchor at daemon startup and advances it from a suspend-aware
monotonic source. The same clock supplies capture persistence, session
boundaries, and timeline `now`, so a later manual/NTP jump cannot put one
pipeline two hours ahead of another. A restart takes a new wall anchor and
relies on durable replay to reconcile generations. This means:

- A running daemon does not re-anchor its grid after a host wall-clock jump.
- Restart replay retains the durable grid anchor and does not overlap existing blocks.
- Blocks have a natural UNIQUE key `(start_time, end_time)` → production is idempotent.
- A human looking at the timeline can reason about "the 10:15 block" without mental arithmetic.

The production clock retains the host's IANA timezone rules. Window iteration and
lookback arithmetic use elapsed UTC instants, so spring-forward gaps are skipped
and the two fall-back folds remain distinct. SQLite enforces window uniqueness on
the two absolute instants, not their offset-dependent ISO spellings; migration
quarantines any older semantic duplicate. If a new daemon generation starts
after the host wall clock was moved backwards (or a standalone tick observes
the same rollback), the producer rewinds its future empty-window proof to the
safe retained-evidence boundary while retaining the durable `processed_from`
grid anchor. Existing blocks therefore remain idempotent without creating an
overlapping, newly re-anchored window.

For the default one-minute window, timezone offset changes stay on familiar wall
minute labels. A custom window that does not divide an offset transition remains
on its original elapsed-time grid after the transition; its displayed wall labels
may shift instead of introducing a short, long, or overlapping block.

The first producer tick records a durable window-duration epoch. After that,
changing `window_minutes` is fail-closed: the producer does no more work until
`openchronicle clean timeline` explicitly removes blocks, coverage, receipts,
and the old epoch. OpenChronicle never mixes two window durations in one
receipt ledger.

Why 1-minute blocks? Two reasons:

1. The timeline prompt is now a verbatim-preserving normalizer, not a summarizer. Short windows carry few captures each, so each authored text snippet can round-trip through the prompt without being dropped or paraphrased.
2. The reducer's flush tick already does 5-min-scale work every `session.flush_minutes`, so there's no need for the timeline itself to be 5 min wide. A 5-min flush now consumes ~5 timeline blocks.

## Production cadence

`timeline/tick.py::run_forever` fires every 60s inside the daemon. Each tick:

1. Snapshots retained capture paths and assigns them to windows using their exact persisted timestamps.
2. Loads the durable inspected range `[processed_from, processed_through)`.
3. Iterates closed windows from that boundary (or the earliest retained capture, pending session, or cold-lookback seed on first run) up to the current window floor.
4. For each window without a current block, calls `produce_block_for_window`.
5. Re-reads the complete physical window under the capture lock and commits
   the block, provenance, sparse root outcome, complete child manifest, and
   watermark advance as one SQLite publication. Empty and policy-excluded
   outcomes use the same final recheck and transaction without a block.
6. Cleans raw JSON only as a complete receipted window, after the valid upper
   bound and retention/size rules permit it. Screenshot stripping remains a
   per-file operation because screenshot bytes are outside the semantic hash.

Only closed windows are produced. The current half-formed window sits as "trailing captures" in the buffer until it closes.

## Coverage, receipts, and late evidence

The upper watermark alone proves only what the producer could see when it
inspected a window. It does not prove that an older-timestamped capture could
not arrive later. The receipt ledger therefore has two levels:

- `timeline_window_receipts` is a sparse, per-window root. It binds the full
  capture-set digest, capture-policy digest, typed outcome (`block` or
  `policy_excluded`), block projection/source identity when applicable, and raw
  lifecycle state. Proven-empty minutes are represented by contiguous coverage
  and do not create one row per minute.
- `timeline_capture_receipts` is the complete child manifest while raw state is
  `live` or `retiring`. Every row binds exact path, observation ID, source hash,
  capture time, and semantic window. Child rows are removed only after the
  entire old manifest is absent and the root becomes `retired`.

The v2 source hash covers every persisted observation field except screenshot
bytes and the storage-only `screenshot_stripped` marker. Schema, identity,
privacy, AX, text, URL, and otherwise unknown-field changes therefore invalidate
the receipt, while normal screenshot-only retention does not invalidate a block.
The policy digest covers every capture policy field used by delayed egress. A
policy change invalidates non-retired outcomes and forces a policy-aware replay;
it cannot silently reuse a block or `policy_excluded` decision produced under a
different policy. Receipt mode is enabled before replay on an upgraded database,
and receipt/outcome publication is atomic with the matching watermark advance.
Missing, unreadable, changed, overlapping, or unverifiable receipt state keeps
the raw JSON rather than guessing that timeline consumed it.

Raw cleanup is a small durable state machine:

1. `live`: every manifest member must still be a regular canonical file with
   the exact receipted semantic identity, and the root/outcome must be current.
2. `retiring`: in one transaction cleanup tombstones the complete window,
   removes its FTS rows, and records this state before attempting any unlink.
   A crash or partial unlink is therefore safely resumable from the durable
   manifest, including at startup before a new producer boundary exists.
3. `retired`: only after no old manifest member remains does cleanup mark the
   root retired, delete its child rows, and release their tombstones. A reused
   filename with different semantic identity is new evidence, not completion
   of the old unlink.

Age retention and size eviction are all-or-none for a window: one old member
cannot delete its younger siblings, and an unexpected late path in the same
window prevents retirement. Screenshot stripping is the deliberate exception;
it may update one JSON independently because those bytes are not model input and
do not change the v2 digest.

When an unreceipted capture appears behind `processed_through`, the producer
rewinds to the earliest affected window and invalidates in-flight reducer
publication. If an existing block has no durable downstream/session progress,
timeline can re-materialize it from the expanded immutable source set, replace
the old block under a generation fence, receipt the late capture, and advance
again. If the block has already been consumed by reducer/session progress,
classifier delivery, Daily Wrap, or another provenance-linked artifact,
replacement would make those durable claims false. The producer therefore
fails closed: it leaves the old block unchanged, preserves the raw late capture
without a receipt, and keeps the watermark before the affected window. A
durable replay range remembers that the window was previously inspected, so a
restart cannot misclassify this stalled replay as an ordinary virgin frontier.

That stopped state is intentionally visible and safe, but it is **not automatic
convergence**. A scoped downstream cascade replay/repair mechanism must first
invalidate and rebuild every dependent artifact before the block can be
replaced. That mechanism has not yet been implemented or acceptance-tested.

Likewise, a row with an invalid or mismatched timeline projection is a durable
coverage gap, not an empty-window proof. It is quarantined before provider
egress; the tick neither issues a receipt nor advances the watermark past it.
Cleanup therefore retains the corresponding raw evidence until explicit repair
or reset establishes valid coverage.

## The aggregator LLM call

`timeline/aggregator.py` reads each capture's available S1 fields
(`focused_element`, `visible_text`, `url`, `window_meta`) — **not** the raw AX
tree. URL-metadata-only observations intentionally omit `focused_element`. This
keeps the prompt tractable:

- Historical captures without a typed `visible_text` projection contribute
  metadata only; timeline never reconstructs text from raw `ax_tree`. When a
  current title exclusion is active, a legacy app-wide tree must also prove it
  contains exactly one matching window or the observation is quarantined.

- `visible_text` is a pre-rendered, length-capped markdown view of the AX tree (capped at 10 KB per capture by S1, then capped at 4 KB per capture by the timeline prompt).
- `focused_element` carries the user's current cursor / input context (role, title, value, editable flag). When `is_editable=true` and `value_length > 0`, the value is the user's own typed content — the prompt treats this as the highest-priority signal to preserve verbatim.
- `url` is an S1 convenience field emitted only for recognized-browser bundles;
  timeline never reparses raw AX. This is separate from the privacy boundary:
  active URL policy rejects unsupported bundles before AX, requires one
  explicit stable-ID browser address plus a complete full-tree deny scan, and
  compares two ephemeral snapshots. A successful schema-v5/policy-v3
  `url_metadata_only` capture reaches timeline with only app/bundle/PID/window
  ID/bounds and the approved URL; raw AX and focused content are absent, and
  `visible_text` and titles are empty. The two reads mitigate navigation races
  but are not an atomic browser transaction. Treat this URL as an approved
  editable address-control value, not proof that navigation committed or that
  the document was visited. The deterministic formatter labels it
  `MAY BE UNCOMMITTED`, and the timeline prompt forbids claims that the user
  visited, read, or navigated to that URL without separate evidence.

Up to `_MAX_EVENTS_PER_WINDOW = 30` events per window (rarely hit at 1-min granularity). The prompt (`prompts/timeline_block.md`) commands the model to emit a JSON array of normalized activity records, one per distinct conversation / context / tab / file. Each record follows this shape:

```
[<app name>] <context (title/URL/file)>: <what happened>. <Authored text verbatim, in quotes, if any>. Involving: <people/topics/files named in this conversation>.
```

Examples:

```
[Notes] Shopping list: user drafted a list, latest version "milk, eggs, flour, butter".
[Google Chrome] ACME Q3 roadmap (https://docs.example/roadmap): read the document; noted priorities with Owner Alice and Deadline Oct 14. Involving: Alice, ACME Q3 roadmap.
[Cursor] openchronicle/timeline/aggregator.py: editing the _stem_to_dt parser. Involving: openchronicle/timeline/aggregator.py, _stem_to_dt.
```

Explicit rules (see the prompt for the full list):

- **Verbatim preservation.** Authored text from editable inputs must be carried into the entry in quotes, not paraphrased. URLs, window titles, file paths, and proper nouns must be verbatim.
- **Anti-hallucination.** Topics and people seen in one conversation must not be cross-attributed to another conversation in the same app (multiple tabs, multiple chats).
- **Authorship guard.** Typing into a search box / address bar is not chat participation.
- **De-duplication.** Collapse consecutive identical passive reads; keep the longest version of an in-progress draft.

On any failure (JSON parse, LLM timeout, empty), the code falls back to a heuristic entry built from `window_meta.app_name` counts. Never silently drops a window.

## Schema

```sql
CREATE TABLE timeline_blocks (
  id TEXT PRIMARY KEY,
  start_time TEXT NOT NULL,
  end_time TEXT NOT NULL,
  timezone TEXT NOT NULL DEFAULT '',
  entries TEXT NOT NULL,         -- JSON array of strings (one record per conversation/context)
  apps_used TEXT NOT NULL,       -- JSON array of app names
  capture_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  source_digest TEXT NOT NULL DEFAULT '',
  projection_digest TEXT NOT NULL DEFAULT '',
  UNIQUE(start_time, end_time)
);
```

Stored in the same `index.db` as the FTS tables. Not FTS-indexed — the reducer queries them directly by time range for each session / flush.

The same database also stores the sparse `timeline_window_receipts`, their
temporary `timeline_capture_receipts` child manifests, and the single
`timeline_window_receipt_epoch`. Canonical UTC text is retained for inspection,
while integer UTC-microsecond start/end keys enforce semantic uniqueness across
equivalent timezone spellings. A malformed or overlapping root is a coverage
gap, never an implicit empty outcome.

`source_digest` binds the complete, order-independent set of direct observation
references exactly once when the block and provenance are published.
`projection_digest` binds that source closure plus every persisted field in the
row (identity, window, timezone, entries, apps, exact count type, and creation
time). Existing databases receive both bindings once during schema migration
when their source edges are available. Afterward, a blank, malformed, rebound,
or mismatched digest is never repaired implicitly: Context, MCP, reducers,
classifiers, and Daily Wrap all quarantine the block. A damaged row occupying a
unique window is skipped before provider egress and recorded as a coverage gap;
it is not repeatedly regenerated into the same uniqueness collision. Timeline
block duration is bounded to one day (the producer clamps configured windows to
that ceiling), which also bounds indexed day/session candidate scans. This keeps
SQLite a fail-closed derived projection instead of allowing an out-of-band row
or provenance-edge edit to become model or API input.

The one-time observation-digest v2 migration is deliberately narrow. It runs
under the capture-store lock and upgrades a timeline block only when its old
projection and complete source binding were already current and every retained
observation is a regular canonical file whose path, ID, timestamp, and v1/v2
hash agree. The block upgrades all observation edges atomically or not at all.
Receipts are never auto-upgraded: a legacy receipt retains raw JSON and forces
a replay that writes a v2 receipt only after the current window is proven.
Legacy v1 references embedded in other
artifacts are not granted a permanent compatibility bypass: if they cannot be
rebuilt as v2, readers quarantine them. This trades some upgrade availability
for the guarantee that a schema/privacy mutation cannot hide behind the old
narrow digest.

## CLI

```bash
openchronicle timeline tick         # synchronous: build all closed windows now
openchronicle timeline list -n 24   # last 24 blocks, oldest → newest
```

Production is idempotent — manual ticks are always safe.

`openchronicle clean captures` preserves a valid window proof by completing the
same `live → retiring → retired` protocol. If the root or manifest is already
invalid, explicit raw deletion instead removes that proof and clears producer
coverage/replay state so no watermark can claim the missing bytes were
inspected. `openchronicle clean timeline` refuses to run while any root is
`retiring`, because dropping that manifest would make a partial unlink
unrecoverable.

## Interaction with the S2 reducer

Both the flush tick and the terminal reducer first use a bounded, indexed
`julianday(start_time)` candidate band, then verify each projection/source
binding and apply the exact UTC intersection in Python:

```sql
SELECT * FROM timeline_blocks
 WHERE julianday(start_time) > julianday(:start_bound) - 2
   AND julianday(start_time) < julianday(:end_bound) + 2
   AND julianday(end_time) > julianday(:start_bound) - 2
```

Where `:start_bound` is `flush_end` (or `session.start` on the first flush) and `:end_bound` is `now` (flush) or `session.end` (terminal). All overlapping blocks are fed to the reducer LLM along with the window's wall-clock range. The reducer emits per-window-range sub_tasks like `[13:25-13:30, Cursor] edited tick.py; "fixed _stem_to_dt for negative offsets"; involving openchronicle/timeline/aggregator.py`.

## Tuning

- Timeline runs every 60s even with no captures; the LLM call is skipped when the window has zero events.
- If your `timeline` model is slow (>30s per call), that's your bottleneck — consider a faster model for this stage. Since the prompt is now bigger (1-min window but more verbatim content), a mid-tier model may be worth it; a too-weak model will start summarizing instead of normalizing.
- `window_minutes` can be tuned before the receipt epoch is activated. After a
  producer tick, change it only by explicitly cleaning timeline first. A larger
  window cuts LLM calls per hour but risks the model over-summarizing a crowded
  window; a smaller window costs more calls but keeps fidelity high.
- Drop all timeline data with `openchronicle clean timeline`. The aggregator will re-produce blocks from whatever captures are still in the buffer on the next tick.
