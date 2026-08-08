# Troubleshooting

Work from symptoms to cause. Each section links to the relevant log file under `~/.openchronicle/logs/`.

## Daemon won't start

Symptom: `openchronicle start` returns `Already running (pid N)` but the process is dead.

Check:

```bash
ps -p $(cat ~/.openchronicle/.pid) || rm ~/.openchronicle/.pid
openchronicle start
```

A stale PID file is the typical cause; `stop` removes it cleanly, crashes don't.

Symptom: foreground start immediately exits without error.

```bash
openchronicle start --foreground
```

Read the console output. Common culprits:

- `OSError: [Errno 48] Address already in use` → another process holds port 8742. `lsof -i :8742` to find it.
- Missing `OPENAI_API_KEY` → set it or put `api_key = "..."` in `[models.default]`.
- `mac-ax-helper` / `mac-ax-watcher` binary missing → run `bash resources/build-mac-ax-helper.sh && bash resources/build-mac-ax-watcher.sh`.

## `capture-once` writes no observation

Capture now fails closed: no partial or metadata-only JSON is written when the
exact focused-window, AX, URL-policy, or optional screenshot checks cannot all
be completed. Start with the privacy-safe reason codes in the capture log:

```bash
openchronicle capture-once
tail -50 ~/.openchronicle/logs/capture.log
```

Common causes:

- **Accessibility permission is missing.** Open System Settings → Privacy &
  Security → Accessibility and enable the terminal/app that launches
  OpenChronicle. Restart it after changing TCC permission.
- **The focused window cannot be joined unambiguously.** A valid observation
  requires app, bundle ID, title, PID, `CGWindowID`, and bounded geometry to
  agree between AX and CoreGraphics. Transient menus, minimized/off-screen
  windows, a focus change during collection, or two ambiguous sibling windows
  are deliberately dropped; focus a normal document window and retry.
- **The AX helper is missing or stale.** Rebuild it with
  `bash resources/build-mac-ax-helper.sh`, then retry.
- **A browser URL rule denied the observation.** Inspect the resolved
  `[capture]` config with `openchronicle config`. If either URL list is active,
  only a supported browser bundle/family adapter is eligible; unknown browsers
  and normal apps are denied before AX. Policy requires exactly one explicit
  HTTP(S) address from an exact stable AX identifier. Missing identifiers,
  label-only controls, scheme-less addresses, unsupported schemes, and
  ambiguous controls are denied. The full-tree URL scan is an additional deny
  surface, so an excluded link or conservative dotted/path-like token anywhere
  in AX also rejects the capture. Both AX reads need valid complete-tree
  receipts and identical address/full-scan evidence. Use bundle/title policy
  without URL rules for non-browser or unsupported-browser capture.
- **Screenshots and URL rules are both enabled.** macOS cannot atomically bind
  an AX address value to the pixels of the same `CGWindowID`. OpenChronicle
  therefore drops every such observation before pixel collection. Disable
  screenshots or scope the app with bundle/title rules instead.
- **Screenshots are enabled without Screen Recording access.** When
  `include_screenshot = true`, grant the invoking app under System Settings →
  Privacy & Security → Screen Recording. Permission, helper, JPEG, or identity
  failure drops the whole observation; there is no AX-only, full-screen, or
  `mss` fallback. Keep screenshots disabled if pixels are not required.

If a successful URL-policy capture looks empty by design, check its
`privacy.content_mode`. Schema-v5/policy-v3 `url_metadata_only` retains only
app/bundle/PID/window ID/bounds plus the approved explicit URL. It omits raw AX,
focused content, AX metadata, and screenshots, and clears title/visible text.
The two AX reads reduce navigation races but do not make browser capture atomic.

The helper's stderr is intentionally not copied into capture logs because it
can contain application metadata. Log messages report only stable failure
classes/statuses.

If captures exist but an Electron tree lacks content, **`ax_depth` may be too
shallow**. See [capture.md](capture.md#ax-depth-the-1-footgun).

## Run the opt-in live AX/privacy audit

For a real two-window/secure-field/privacy-sink check, pause or stop the
production daemon and run:

```bash
uv run python scripts/run_macos_ax_privacy_audit.py \
  --acknowledge-live-ax \
  --acknowledge-production-capture-paused \
  --report tests/live/macos_ax_privacy/reports/audit.json

uv run python scripts/verify_macos_ax_privacy_report.py \
  tests/live/macos_ax_privacy/reports/audit.json
```

See [`tests/live/macos_ax_privacy/README.md`](../tests/live/macos_ax_privacy/README.md)
for TCC prerequisites, redacted artifact guarantees, exact-window pixel probe,
and limitations. Screenshot bytes are checked only in memory and never enter
the retained report or downstream sink fixtures.

## Desktop review shell cannot reach the local bridge

Symptom: the Tauri shell reports that the desktop bridge is missing,
unavailable, malformed, too large, or timed out.

For a repository debug build, install the project into its local environment
and confirm the fixed entry point exists:

```bash
uv sync --all-extras
test -x .venv/bin/openchronicle-desktop-bridge
printf '%s\n' '{"version":2,"operation":"snapshot","params":{"timeline_limit":0,"candidate_limit":0,"wrap_limit":0}}' \
  | .venv/bin/openchronicle-desktop-bridge
```

The v2 bridge must emit exactly one JSON response line and no captured content
on stderr, including when its privacy fence cannot be acquired or released.
Release builds do not search shell `PATH` and do not honor the debug
override; the executable must be shipped beside the app or at a documented
fixed install path. The current source slice deliberately does not bundle that
sidecar, so an unsigned source build is not a release artifact.

If pause/review reports a version conflict, refresh before retrying. Do not
bypass it: the conflict is the compare-and-set fence preventing a stale window
from overwriting a newer proposal or capture state. A changed permanent-forget
preview must be reviewed again because its transitive deletion closure changed.
If the desktop reports that the purge closure is unverifiable, repair or remove
the affected local Markdown entry with the damaged provenance frame, rebuild the
index, and request a fresh preview; the safe behavior is to retain data rather
than claim that an incomplete deletion succeeded.

## No event-daily entries appearing

Entries land in `~/.openchronicle/memory/event-YYYY-MM-DD.md` at session boundaries. Silence usually means one of three things.

### 1. No sessions are closing

Check `session.log`:

```bash
tail -30 ~/.openchronicle/logs/session.log
```

If there's a single `session started` line but no `session ended`, the cutter thinks you're still in one session. Normal during continuous work. Force a boundary for debugging by pausing briefly (`openchronicle pause`, wait > `session.gap_minutes`, `resume`).

### 2. Timeline is empty for the session's window

Check `timeline.log`:

```bash
tail -30 ~/.openchronicle/logs/timeline.log
```

If you see window scans but no production, it's usually one of:

- **No captures in the window.** The timeline skips empty windows. Confirm captures exist: `ls ~/.openchronicle/capture-buffer/ | wc -l`.
- **LLM call failing.** Look for `timeline aggregator failed`. Check `[models.timeline]` config.

The reducer handles empty timelines gracefully — it marks the session `reduced` with no entry. If *all* your sessions end up with empty timelines, the aggregator is the root cause.

### 3. Reducer is failing

Check `writer.log`:

```bash
tail -50 ~/.openchronicle/logs/writer.log
```

Look for `reducer failed (retry N/5)` lines. After 5 failed attempts the reducer writes a heuristic entry tagged `heuristic` and marks the session `reduced` — you should *never* see a permanently-stuck session.

Force a catch-up pass:

```bash
openchronicle writer run
```

This runs the same code path the daily 23:55 cron uses.

## Classifier never proposes durable facts

This is often correct behavior — the classifier's default action is an empty commit. It should only propose a fact that would still matter in six months.

Signs it's misbehaving rather than doing its job:

- `classifier ended without commit at iter N` in `writer.log` — the model bailed without calling `commit`. Usually means the stage model is too weak to follow the tool-call protocol. Try a stronger `[models.classifier]`.
- `event-daily is reducer-owned and cannot receive candidates` — the classifier tried to target an `event-*` file. This is always rejected. If every session triggers it, the classifier prompt isn't landing; check that `classifier.md` exists under `src/openchronicle/prompts/`.
- Repeated runs show one candidate with a replay-mismatch error — this is the idempotency guard preserving the first durable proposal when a provider retry changes wording. Review that original candidate instead of expecting a second card.
- A candidate will not appear in Markdown until `openchronicle memory approve <id>` succeeds. Approval rechecks every cited source and rejects missing or changed evidence.

## Scheduled Daily Wrap is not running

Scheduled synthesis is deliberately disabled on fresh installs and upgrades.
Set `enabled = true` under `[daily_wrap]`, restart the daemon, and inspect
`~/.openchronicle/logs/daily-wrap.log`. Use `openchronicle daily-wrap run
--date YYYY-MM-DD --timezone Area/City` for an explicit one-off run. A failed
refresh keeps the last successful wrap visible and records the failed attempt;
it does not replace good output with an error.
When `--timezone` is omitted, the CLI uses `[daily_wrap].timezone` and then the
system IANA zone, in the same order as the scheduler.

## Timeline blocks not appearing

Check `timeline.log`:

```bash
tail -30 ~/.openchronicle/logs/timeline.log
```

If you see window scans but no production, the aggregator window is empty. The fallback heuristic still produces *something*, so total silence means the tick itself isn't firing.

Force a scan:

```bash
openchronicle timeline tick
openchronicle timeline list -n 5
```

## MCP client can't connect

Symptom: Claude Code / Cursor reports the server unreachable.

1. Is the daemon running?

   ```bash
   openchronicle status
   curl -s http://127.0.0.1:8742/mcp -XPOST -H 'Content-Type: application/json' -d '{}' | head -5
   ```

2. Is `mcp.auto_start = true` and `mcp.transport` ∈ {`sse`, `streamable-http`}?

   ```bash
   openchronicle config | grep -A3 '\[mcp\]'
   ```

3. Did `install claude-code` actually add the entry?

   ```bash
   claude mcp list | grep openchronicle
   ```

If `mcp.auto_start = false`, the daemon intentionally won't host a server; use stdio instead.

## ChatGPT Desktop can't see the server

Symptom: you pasted `http://127.0.0.1:8742/mcp` into ChatGPT's Create Connector dialog and got "could not reach server" or "invalid URL."

ChatGPT's MCP client runs in OpenAI's cloud, not on your Mac — **localhost is unreachable from its side**. You need a public HTTPS URL via a tunnel (ngrok / Cloudflare Tunnel). See [mcp.md → ChatGPT Desktop](mcp.md#chatgpt-desktop) for the full setup, including the data-egress trade-offs. No amount of local config can make this work directly; the tunnel is mandatory. If you want a ChatGPT-style client that speaks to the local endpoint, Codex CLI works natively.

## MCP client connects but doesn't use the memory

Symptom: Claude Code / Cursor / ChatGPT is attached, but when you ask *"when is my interview?"* it says "I don't know" instead of calling `search` or `list_memories`.

Two levers:

1. **Restart the client.** MCP `instructions` and tool descriptions are only re-read on reconnect. After updating OpenChronicle, restart the client session.
2. **Nudge once.** Tell the client explicitly: *"check openchronicle for my interview time."* A single prompt usually anchors subsequent turns.

## MCP client answers from compressed memory without drilling into raw captures

Symptom: ask *"what code did I write in main.py at 14:30?"* and the agent paraphrases from the event-daily sub_task (`"edited main.py"`) instead of reading the actual code.

Cause: the agent isn't reaching for the raw-capture layer. Either it stopped at `search` / `read_memory` (compressed), or its session pre-dates the drill-down surface.

Fixes:

1. **Check the reducer is emitting breadcrumbs.** Every sub_task should end with ` — raw: read_recent_capture(at="HH:MM", app_name="…")`. Open today's `event-YYYY-MM-DD.md` and verify. If a line has no breadcrumb, the reducer's output didn't match the canonical `[HH:MM-HH:MM, <app>]` prefix — check `logs/writer.log` for the reduced entry text.
2. **Try `search_captures` directly.** Ask the agent *"search captures for <keyword>"* or *"what's in current_context"*. If those work, the FTS index is healthy and the issue is tool-selection, not retrieval.
3. **Rebuild the captures index if it's empty or out of date.** Run `openchronicle rebuild-captures-index`. Compare `SELECT COUNT(*) FROM captures` against `ls ~/.openchronicle/capture-buffer | wc -l` — they should match modulo one active capture.
4. **Restart the client** after updating OpenChronicle — server-level `instructions` (which teach the two-layer model) are only read on reconnect.

## Long session got chopped in half

Symptom: a real 3-hour focused-work session produced two event-daily entries with a mid-session boundary.

Cause: `session.max_session_hours` (default 2) force-cut it. Raise to 4 in `[session]` if this is routine for you. Keep it finite — a runaway session is worse than a clean split.

## Session cuts every few minutes during real work

Symptom: event-daily has many short entries for what was clearly one focused stretch.

Two likely causes:

1. **`session.soft_cut_minutes` too aggressive.** You're single-apping for >3 min (say, a long read in the browser). Raise to 5–10.
2. **`session.gap_minutes` too short.** Idle stretches during thinking are ending sessions. Raise to 8–10.

The frequent-switching exception (≥2 distinct apps in the last 2 min) already defuses the soft cut for multi-app work — if you're still seeing cuts, one of the above two is the knob.

## High CPU / disk from capture

Symptom: laptop fan spinning during capture activity.

Tuning levers, in order:

1. `same_window_dedup_seconds = 15.0` (up from 5.0) — cuts re-capture rate during long typing in a single document.
2. `debounce_seconds = 5.0` (up from 3.0) — batches more keystroke events.
3. `min_capture_gap_seconds = 5.0` — hard-limits capture rate.
4. `include_screenshot = false` — screenshots are the heaviest single cost per capture.
5. `ax_depth = 50` — if you don't need deep Electron content.

Restart the daemon after any `[capture]` change.

## FTS search returns nothing but files exist

Cause: the index drifted from disk (manual edits without `rebuild-index`, power loss mid-write, bug).

Fix:

```bash
openchronicle rebuild-index
```

Rewrites `entries`, `files`, and `entries_fts` from the Markdown on disk. Idempotent.

## Resetting

Start from scratch without reinstalling:

```bash
openchronicle stop
openchronicle clean all -y        # keeps config.toml
openchronicle start
```

Full nuke including config:

```bash
openchronicle stop
rm -rf ~/.openchronicle
openchronicle start                # recreates config.toml with defaults
```

## Getting more signal

```bash
tail -F ~/.openchronicle/logs/*.log
```

All sinks in one terminal. When in doubt, run `start --foreground` and keep this tail open in another pane.
