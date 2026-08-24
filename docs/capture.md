# Capture

Capture is the only layer that touches the outside world. It produces one JSON file per observation into `~/.openchronicle/capture-buffer/`; nothing above it ever talks to macOS directly.

## Two signal sources

**`mac-ax-watcher`** (primary, event-driven). A vendored Swift binary that subscribes to AX notifications across all running apps: window focus, value changes (typing), title changes, app activation. It emits one JSON object per event on stdout. The Python side reads that stream line-by-line in `capture/watcher.py` → `capture/event_dispatcher.py`.

**Heartbeat timer** (fallback). Every `heartbeat_minutes` (default 10), the scheduler fires a capture even if no event arrived — so long idle periods leave a trail. Set `heartbeat_minutes = 0` to disable entirely (watcher-only); values `>0` are clamped to a 60-second floor.

Both funnel into `capture_once` in `capture/scheduler.py`, which runs:

1. Validate the URL-policy shape and bounds. Malformed URL policy denies every
   app before either native helper is asked for content. When either URL list
   is active, only a known browser bundle with a family-specific address
   adapter is supported; unknown browsers and ordinary apps are denied before
   AX collection.
2. `window_meta.active_window()` asks `mac-ax-helper` to join AX's focused
   window to one unambiguous CoreGraphics window. The resulting `WindowMeta`
   contains app name, title, bundle ID, PID, `window_id` (`CGWindowID`), and
   global bounds. Missing or ambiguous identity fails closed.
3. Apply app/bundle/title allow/exclude policy and bind a queued watcher event
   to the currently focused PID/bundle/title. The event is only a wake-up
   signal: its AX `details` are discarded and only the verified event type plus
   exact current-window identity can enter an observation or session hook.
4. `ax_capture.capture_frontmost(focused_window_only=True)` captures only that
   focused AX window. The native helper fences the AX traversal with the exact
   identity before and after collection; Python then requires exactly one
   frontmost app, one focused window, and the same complete `WindowMeta`. When
   URL policy is active, `--require-complete-tree` turns any configured-depth
   pruning into a failed observation, and Python requires a matching versioned
   completeness receipt from the helper.
5. `s1_parser.enrich()` extracts `focused_element`, `visible_text`, and `url`
   in memory. With URL policy active, a family adapter must find exactly one
   browser-chrome address control by exact stable AX identifier; the ordinary
   S1 label fallback is not trusted at this boundary. Every URL-like value in a
   separate bounded full-tree scan must also pass policy, but page content can
   only deny and can never supply the required address evidence. The scheduler
   repeats the complete AX capture and requires the same exact window, address
   value/provenance/source path, and full-tree evidence. This double-read
   reduces ordinary navigation races; it is not atomic and does not claim to
   eliminate every race.
6. If `include_screenshot = true`, re-check the complete focused-window
   identity, capture exactly its `CGWindowID`, and check identity again inside
   the helper and in the scheduler. There is no display/full-screen or `mss`
   fallback. Screenshot permission, helper, image-validation, or identity
   failure drops the whole observation rather than persisting an AX-only one.
   Because macOS cannot atomically bind an AX address to CGWindow pixels,
   screenshots are disabled whenever URL rules are active.
7. Before persistence, a successful URL-policy capture is projected to the
   `url_metadata_only` profile: keep only app/bundle/PID/window ID/bounds and an
   explicitly observed, approved HTTP(S) URL; omit raw AX, focused content, AX
   metadata, and screenshots; clear visible text and titles. Other successful
   captures retain the normal schema-v4/policy-v2 S1 payload. URL metadata uses
   schema v5 and policy v3. Atomically write the private (`0600`) JSON, then
   update recoverable FTS.

The filename preserves timestamp fractions and timezone, then appends a random observation ID so same-millisecond events cannot overwrite each other. Example: `2026-04-21T17-07-32.123p08-00_obs_0123456789abcdef.json`. Legacy timestamp-only filenames remain readable.

The same capture scheduler also invokes `SessionManager.on_event` (wired as a `pre_capture_hook` in `daemon.py`), so the session cutter sees every capture-worthy event without a separate subscription path.

## Debounce / dedup / gap

Four time-based knobs throttle the event firehose (`capture/event_dispatcher.py`):

| Knob | Default | What it does |
|---|---|---|
| `debounce_seconds` | 3.0 | `AXValueChanged` events within this window collapse — only the last triggers a capture. Prevents one-capture-per-keystroke during typing. |
| `dedup_interval_seconds` | 1.0 | Same `(event_type, app)` pair within this window is dropped outright. |
| `min_capture_gap_seconds` | 2.0 | Hard floor between consecutive `capture_once` calls, regardless of event reason. |
| `same_window_dedup_seconds` | 5.0 | Non-focus-change events in the same `(bundle_id, title)` pair collapse within this window. Focus changes always bypass it. |

Tune these if you see `capture.log` flooded; the defaults produce a few hundred captures per work-day, comfortably under the buffer retention.

### Content dedup (no time window)

On top of the time-based knobs, the scheduler compares each built capture against the previous one by a content fingerprint (`hash(bundle + title + focused_element.value + visible_text + url)`, in `capture/scheduler.py`). If the fingerprint matches, the capture is **not** written and the session manager's `pre_capture_hook` is **not** fired.

This catches the case the time knobs can't: a screen that doesn't change (lock screen overnight, a paused video, an idle IDE) keeps generating AX events with the same content indefinitely. Without content-dedup those would both fill the buffer and keep the current session from ever idling out. Timestamps, triggers, and screenshots are excluded from the fingerprint so only meaningful changes count.

## AX depth — the #1 footgun

AX Trees for native Cocoa apps are shallow (5–15 layers). Electron apps (Claude Desktop, VS Code, Slack, Notion) nest user content 20–60 layers deep under chrome.

**Default `ax_depth = 100`** was chosen after diagnosing silent capture misses: a 90-second Claude Desktop conversation about an interview at 18:00 was producing captures where "18:00" appeared at character 5639 of the tree — past any reasonable prune limit. At depth 8, the tree contained only window chrome and sidebar headers; at depth 100, the full conversation was there.

If you're running on limited hardware and only care about native apps, lowering
to 30 can be reasonable when URL policy is disabled. With URL rules active,
the scheduler passes `--require-complete-tree`: if `ax_depth` would prune even
one encountered node, the helper exits nonzero and the observation is dropped
instead of treating a partial URL scan as complete. A successful helper also
emits a versioned, content-free receipt covering `tree_complete`,
`focused_window_only`, effective depth, and the resource-limit version; Python
checks and strips that transport receipt before policy processing. Don't go
below 20 for ordinary capture quality.

Diagnostic:

```bash
./resources/mac-ax-helper --app-name Claude --depth 30 --raw | wc -c
# vs.
./resources/mac-ax-helper --app-name Claude --depth 100 --raw | wc -c
```

A 10×+ ratio means there's content past depth 30 you'd miss.

## What's in a capture file

With URL policy disabled, a normal S1 capture can contain AX-derived content:

```json
{
  "timestamp": "2026-04-21T17:07:32.123+08:00",
  "schema_version": 4,
  "observation_id": "obs_0123456789abcdef...",
  "trigger": {
    "event_type": "UserTextInput",
    "app_name": "Claude",
    "bundle_id": "com.anthropic.claudefordesktop",
    "pid": 1234,
    "window_id": 5678,
    "window_title": "New conversation — Claude"
  },
  "window_meta": {
    "app_name": "Claude",
    "bundle_id": "com.anthropic.claudefordesktop",
    "title": "New conversation — Claude",
    "pid": 1234,
    "window_id": 5678,
    "bounds": { "x": 0.0, "y": 25.0, "width": 1440.0, "height": 875.0 }
  },
  "privacy": { "decision": "allowed", "policy_version": 2 },
  "focused_element": {
    "role": "AXTextArea",
    "title": "Message composer",
    "value": "I have an interview at 18:00",
    "is_editable": true,
    "value_length": 30
  },
  "visible_text": "### New conversation — Claude\n...",
  "url": null,
  "ax_tree": { ... pruned tree with roles, titles, values ... },
  "ax_metadata": {
    "mode": "frontmost",
    "platform": "macos",
    "focused_window_only": true
  },
  "screenshot": {
    "capture_mode": "exact_window_v1",
    "image_base64": "/9j/4AAQSkZJRgABAQ...",
    "mime_type": "image/jpeg",
    "width": 1920,
    "height": 1200,
    "window_meta": {
      "schema_version": 1,
      "app_name": "Claude",
      "bundle_id": "com.anthropic.claudefordesktop",
      "title": "New conversation — Claude",
      "pid": 1234,
      "window_id": 5678,
      "bounds": { "x": 0.0, "y": 25.0, "width": 1440.0, "height": 875.0 }
    }
  }
}
```

With URL policy active, the durable shape is deliberately smaller:

```json
{
  "timestamp": "2026-04-21T17:07:32.123+08:00",
  "schema_version": 5,
  "observation_id": "obs_0123456789abcdef...",
  "trigger": {
    "event_type": "AXValueChanged",
    "app_name": "Safari",
    "bundle_id": "com.apple.Safari",
    "pid": 1234,
    "window_id": 5678,
    "window_title": ""
  },
  "window_meta": {
    "app_name": "Safari",
    "bundle_id": "com.apple.Safari",
    "title": "",
    "pid": 1234,
    "window_id": 5678,
    "bounds": { "x": 0.0, "y": 25.0, "width": 1440.0, "height": 875.0 }
  },
  "privacy": {
    "decision": "allowed",
    "policy_version": 3,
    "content_mode": "url_metadata_only"
  },
  "visible_text": "",
  "url": "https://allowed.example/path"
}
```

There is no `ax_tree`, `ax_metadata`, `focused_element`, or `screenshot` in the
URL-policy form. Its title and visible text are empty, and only an explicit,
stable-ID-derived, policy-approved HTTP(S) URL can reach the durable `url`
field. A scheme-less address is rejected before persistence.

Watcher frames may carry authored text or a stale address in `details`, so the
scheduler never persists that payload and never forwards it to
`SessionManager`. Every successful trigger is projected to a fixed,
identity-only shape (`event_type`, app, bundle, title, PID, and window ID); the
exact AX snapshot is the sole content source. Screenshot is omitted entirely
by default.

The v4/v5 `window_meta` is a capture fence, not just display metadata. PID and
`window_id` distinguish sibling windows that share a title; bounds and the
remaining fields must also match exactly at each Python boundary. The native
helper permits a small geometry tolerance only while joining AX coordinates to
CoreGraphics and rejects ambiguous joins instead of selecting a first window.

URL policy is available only for supported browser bundle/family adapters.
Unknown browsers and non-browser apps are denied before AX, because arbitrary
page or application text cannot prove that a value came from the active address
bar. The two rule lists contain bounded literals, never user-supplied regular
expressions:

- a bare hostname matches that host or one of its DNS-label subdomains;
- a full HTTP(S) URL is normalized and matched only at the start of the
  observed URL on a component boundary, so an allowed URL embedded in an
  unrelated query cannot grant access;
- allow rules accept only bare hostnames or full HTTP(S) URLs. Exclusion rules
  additionally accept non-host case-insensitive substring literals, such as
  `/private/`, so they can conservatively deny sensitive paths;
- each list is limited to 128 rules, each rule to 512 characters, and an
  observed URL to 4,096 characters;
- unreserved percent escapes are decoded before comparison and malformed
  escapes fail closed;
- URL exclusions run first, then a non-empty allowlist requires a match;
- the supported browser's family adapter must identify exactly one
  browser-chrome address control using an exact stable `identifier` or
  `domIdentifier`. The exact-label fallback remains available for ordinary S1
  extraction only and cannot authorize a URL-policy capture;
- every candidate found by the bounded full-tree scan must also pass. That scan
  is an extra deny surface only: a link, page text field, or convincing decoy
  inside `AXWebArea` cannot grant address evidence;
- unsupported URI schemes, malformed values, budget exhaustion, ambiguous
  controls, and incomplete scans fail closed;
- the helper must return a verified complete-tree receipt, and two snapshots
  must preserve exact window identity plus address/full-scan evidence,
  including provenance and AX source paths. The second snapshot is used only
  to build the metadata projection; no raw tree becomes durable.

With URL policy enabled, an unsupported bundle is denied before AX. Use
bundle/title rules without URL rules when ordinary non-browser capture or an
unsupported browser is required. Malformed configuration fails closed globally.
For a supported browser, URL denial happens after in-memory AX parsing but
before screenshots, JSON, FTS, hooks, timeline/session data, or model prompts.

AX may omit an address scheme. Candidate policy evaluates that evidence under
both HTTP and HTTPS interpretations, and either denial rejects the capture.
Active URL policy then rejects it even if both interpretations pass, because a
scheme-less value cannot become durable URL evidence. S1 leaves `url` as
`null` instead of inventing HTTPS. Bare-token detection in the full-tree deny
scan remains conservative and can interpret dotted filenames or strings such
as `feature/branch` as addresses.

Even a stable-ID address control is an editable AX value, not a browser commit
receipt. A user can type an allowed explicit URL over a forbidden page without
pressing Enter; two snapshots may then produce a schema-v5 URL-only observation
whose address value is stable while the loaded document is different. The
metadata projection prevents page/body/title leakage, but downstream consumers
must describe `url` as an approved address-control value, not proof that the
document was loaded or visited.

Before these scans, the scheduler strictly rebuilds the native helper's bounded
root/app/window/element schema. Unknown fields or types reject the observation,
and provider-supplied metadata is ignored. Secure text-field values are
redacted again at this Python boundary even though the bundled Swift helper
already redacts them. Even an allowed URL-policy capture then discards the raw
tree rather than relying on redaction as its durable boundary.

Opt-in screenshots are JPEGs captured by CoreGraphics from an array containing
only the verified `CGWindowID`. The helper checks Screen Recording access
without prompting, verifies the exact identity before capture, after pixel
capture, and after encoding, and returns the final identity with the image.
Python validates the JPEG dimensions/data and the scheduler performs its own
pre/post checks. No monitor image, bounds crop, primary-display capture, or
`mss` fallback exists. Any active URL policy prevents pixel capture because
same-window navigation cannot be atomically fenced by the available
AX/CoreGraphics APIs. The two AX reads are defense in depth, not a claim of
atomicity.

The event watcher is only a bounded wake-up source. Native identity strings
and event JSON are capped, Python reads the binary JSONL pipe with a 32 KiB
frame limit, and oversized frames are drained without logging or dispatching
their content. Watcher `details` never enter observations, hooks, FTS, or model
prompts.

Secure fields (password inputs) are replaced with `"[REDACTED]"` at the helper level — the Python side never sees them.

## S1 fields

Ported from Einsia-Partner's `s1_collector`. For normal captures these are what
downstream LLM stages consume; raw `ax_tree` remains local for future
vision/debugging paths. URL-policy captures are the exception: they use the
`url_metadata_only` durable profile and never retain the raw tree or focused
content.

- **`focused_element`** — `{role, title, value, is_editable, value_length}` for the currently focused AX element. This is the user's cursor context: what they're typing into, which sidebar row is selected, etc.
- **`visible_text`** — a length-capped markdown rendering of the AX tree (up to ~10 k chars). What the user is currently reading on screen.
- **`url`** — for a supported browser, the unique explicit HTTP(S) value from
  its family address-control adapter; `null` for non-browsers, ambiguous
  controls, unsupported values, and scheme-less addresses. Ordinary S1 may use
  an exact-label fallback when stable identifiers are absent; active URL policy
  never trusts that fallback.

For normal opt-in captures, screenshots live in JSON and may be returned by an
explicit `read_recent_capture(..., include_screenshot=true)` MCP request only
after current policy and exact-window attestation checks. They are **not**
passed to model prompts, timeline, reducer, or classifier stages. URL-policy
captures never collect or persist screenshots.

Current-policy reads re-evaluate retained observations. In particular, a
historical app-wide AX tree cannot satisfy an active window-title exclusion
unless it proves a single AX window matching the retained public identity.
Missing or sibling-window projections fail closed, and no model path falls
back to rendering a legacy raw AX tree.

The capture's authoritative `timestamp` is assigned immediately before its
atomic write while holding the collection lock. Timeline membership is
snapshotted under that same lock. A slow AX/screenshot collection therefore
cannot arrive later with an old timestamp after the producer has already
certified that wall-clock bucket as empty.

## Buffer hygiene — tiered retention

Captures are pruned by the timeline tick, not the writer. After each timeline scan, `capture_scheduler.cleanup_buffer` applies three passes (oldest-safe-first), all gated on "this file has already been absorbed by a closed timeline block" so un-absorbed trailing captures are never touched:

| Pass | Condition | Action |
|---|---|---|
| **Delete** | mtime older than `buffer_retention_hours` (default **168** = 7 days) | Whole JSON removed |
| **Strip screenshot** | mtime older than `screenshot_retention_hours` (default **24**) | Rewrite a normal capture without `screenshot`; AX/S1 fields stay. `url_metadata_only` captures never have pixels to strip. |
| **Evict by size** | Total buffer > `buffer_max_mb` (default **2000**, i.e. 2 GB; `0` disables) | Delete oldest absorbed files toward the target; never evict unprocessed captures |

Why tiered: the screenshot base64 is ~77% of each capture's bytes. It is not
consumed by model prompts, timeline, reducer, or classifier stages, though an
explicit MCP read can return a currently authorized, exact-window-attested
image. Stripping it at 24h drops each stale capture to ~20% of its original
size, which is what makes a 7-day window affordable. Typical steady-state
footprint is in the 100s of MB.

To wipe manually:

```bash
openchronicle clean captures
```

Atomic writes use private temporary files. If the process is killed before the
rename, the next daemon startup or cleanup pass removes the strictly matched
orphan temp while holding the capture-store lock; manual clean removes them too.

## Search index — `captures_fts`

Every successful capture write is also indexed into an FTS5 virtual table
(`captures_fts`, backed by a `captures` content table — see
`src/openchronicle/store/fts.py`). This powers MCP `search_captures` and
`current_context`. Normal captures expose AX-derived screen content; URL-policy
rows expose only approved address-control/identity metadata.

**Lifecycle.**

| Event | Effect on index |
|---|---|
| `_write_capture` (write-through) | Upsert one row into `captures` (`INSERT OR REPLACE` on the file stem). Triggers keep `captures_fts` in sync. |
| `cleanup_buffer` time-based delete | Each removed JSON file → `delete_capture(stem)` → trigger drops the FTS row. |
| `cleanup_buffer` size-based eviction | Same — each evicted file is also removed from FTS. |
| Screenshot strip | **Untouched.** Strip only removes normal-capture image bytes; indexed text is unchanged. URL-metadata-only rows already have empty title/text/focused columns. |
| `openchronicle rebuild-captures-index` | Backfill from `~/.openchronicle/capture-buffer/*.json`. Idempotent (`INSERT OR REPLACE`). Run once after upgrading onto a populated buffer, or any time the index drifts. |

**Indexed columns.** Only searchable text is in FTS: `app_name`,
`window_title`, `focused_value`, `visible_text`, `url`. Filterable metadata
(timestamp, bundle ID, focused role) lives on `captures`. For
`url_metadata_only`, title/focused/text are empty and only the approved explicit
URL plus app identity can be searched. Screenshots are never duplicated into
FTS.

**Tokenizer.** `unicode61 remove_diacritics 2` — case-insensitive, accent-folded, Unicode-aware. Same setup as the compressed-memory `entries` index.

If `captures_fts` falls out of sync (e.g. capture worker crashed mid-write, or the daemon was killed during cleanup), the index is recoverable in one shot:

```bash
openchronicle rebuild-captures-index
```

## Pause

```bash
openchronicle pause
```

Drops a `~/.openchronicle/.paused` sentinel. The watcher keeps streaming but `capture_once` short-circuits on sentinel presence. `resume` removes the sentinel.

## Smoke test

```bash
openchronicle capture-once
```

Writes one capture immediately, prints its path. Good for confirming Accessibility permission is granted and the helper compiled correctly.

## Opt-in live macOS privacy audit

The repository includes an interactive, local-only AX/privacy audit. Stop or
pause any production daemon first, then run from the repository root:

```bash
uv run python scripts/run_macos_ax_privacy_audit.py \
  --acknowledge-live-ax \
  --acknowledge-production-capture-paused \
  --report tests/live/macos_ax_privacy/reports/audit.json

uv run python scripts/verify_macos_ax_privacy_report.py \
  tests/live/macos_ax_privacy/reports/audit.json
```

The fixture exercises two real AppKit windows, ordinary and secure fields,
excluded title/URL canaries, rapid focus changes, helper denial/crash paths,
and scans durable/model sinks for forbidden markers. It also runs an in-memory
exact-window pixel probe with public/sibling color canaries; downstream test
captures keep screenshots disabled and no image/base64 is retained. Its report
contains only hashes, counts, booleans, and stable reason codes. Read
[`tests/live/macos_ax_privacy/README.md`](../tests/live/macos_ax_privacy/README.md)
before running it.
