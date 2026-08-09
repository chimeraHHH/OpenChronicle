# Live macOS AX/privacy audit

This is an **opt-in local audit**, not a normal pytest or GitHub Actions job. It
opens a signed AppKit fixture with two real windows and sends random canaries
through OpenChronicle's production AX provider, privacy policy, capture
scheduler, FTS writer, timeline reducer, session reducer, and classifier prompt
assembly.

The retained report contains only SHA-256 digests, counts, booleans, and stable
reason codes. Raw AX JSON, prompt text, canary values, temporary capture JSON,
logs, SQLite data, timeline rows, and session Markdown are deleted before the
report is written.

## Safety preflight

1. Run on a local macOS desktop session (not over a headless CI runner).
2. Pause or stop any real OpenChronicle daemon first. Otherwise that separate
   process could capture the deliberately sensitive fixture window into the
   user's real `~/.openchronicle` store.

   ```bash
   uv run openchronicle pause
   # or: uv run openchronicle stop
   ```

3. Close unrelated sensitive windows. The audit helper targets the fixture by
   bundle ID, but minimizing ambient data is still good live-test hygiene.
4. Do not add debug printing or redirect the production helper's raw stdout.
   The runner intentionally inspects raw AX and prompt values only in memory.

## Run

From the repository root:

```bash
uv run python scripts/run_macos_ax_privacy_audit.py \
  --acknowledge-live-ax \
  --acknowledge-production-capture-paused \
  --report tests/live/macos_ax_privacy/reports/audit.json
```

On the first run, macOS may ask for Accessibility permission. Grant the invoking
terminal/helper under **System Settings → Privacy & Security → Accessibility**,
then run the command again. A denial is itself tested for fail-closed behavior,
but the report remains `incomplete` until the real two-window AX checks run.

Exit codes:

- `0`: every manifest requirement passed;
- `2`: a prerequisite was unavailable or an audit assertion failed; a redacted
  diagnostic report was still written;
- argument/setup errors use the normal nonzero argparse exit.

Verify the retained report independently:

```bash
uv run python scripts/verify_macos_ax_privacy_report.py \
  tests/live/macos_ax_privacy/reports/audit.json
```

For a permission-denied diagnostic report, `--allow-incomplete` verifies the
schema and redaction invariants without claiming full live coverage.

## What the fixture exercises

- two simultaneously visible AppKit windows;
- an ordinary text field plus allowed and forbidden URL-like states in the
  allowed-title window;
- an `NSSecureTextField`, excluded title marker, and excluded URL-like marker in
  the denied window;
- Prompt Rescue's production exact-selection adapter against an ordinary full
  selection, an empty selection, and a selected `NSSecureTextField`;
- the metadata-only Prompt Rescue privacy preflight, proving an excluded bundle
  is denied before the helper's selected-text mode is invoked;
- focused-window-only helper output versus explicit all-window output;
- the native complete-tree receipt required by active URL policy;
- 48 rapid focus changes across both windows and all four controls;
- synthetic helper exit `2` (permission denial) and `SIGKILL` (crash) through
  the production `MacAXHelperProvider` and capture scheduler; both helpers put
  a forbidden canary on stderr before failing;
- exact-window screenshot capture through production `screenshot.grab`, with
  only pixel counts and identity booleans retained (never JPEG/base64); the
  public window is green and its visible sibling is magenta;
- forbidden-marker scans of capture JSON, rotating logs, capture FTS,
  `timeline_blocks`, session rows/Markdown/FTS, and the exact in-memory
  timeline/session/classifier model messages.

The runner requires the allowed controls to appear only in real, ephemeral AX
output. It requires the secure value to be absent and `[REDACTED]` to be present
in the ephemeral all-window result. The excluded title must be denied before
AX. For URL checks, the runner temporarily registers the fixture bundle,
family, and exact `oc-live-public-url-field` stable identifier; there is no
label fallback. It first supplies an explicit forbidden HTTP(S) address and
observes the production parser-to-`evaluate_url_candidate` denial, then restores
all adapter state in `finally`.

The allowed production-scheduler path takes two receipted AX snapshots and
persists schema-v5/policy-v3 `url_metadata_only`. The retained capture/FTS path
contains only app/bundle/PID/window ID/bounds and the explicit approved URL:
`ax_tree`, `ax_metadata`, `focused_element`, and `screenshot` are absent, while
visible text and titles are empty. Normal-field and page content must not enter
any durable/model sink. The two snapshots are a race mitigation, not proof of
atomic browser capture; every forbidden sink scan must still report zero hits.

The explicit pixel probe is separate from the downstream capture: it calls
`screenshot.grab(target=active_meta, ...)`, requires a non-empty JPEG plus
`same_capture_target`, decodes it only in memory, and checks aspect ratio, five
center/edge samples, and a downsampled color histogram. A valid result must
contain a substantial amount of the public green canary and effectively none
of the sibling magenta canary. This prevents a whole-screen JPEG paired with
forged window metadata from passing. The runner then drops the image and
base64 immediately. Screenshots remain disabled in every capture sent to
JSON/FTS/timeline/session/model sinks, and each persisted capture is checked
for absence of a `screenshot` field.

The `--app-name ... --focused-window-only` helper calls prove the two-window
fixture shape, complete-tree receipt, and secure-field redaction only. The
production scheduler check is separate: it calls `capture_frontmost`, requires
the helper's exact window identity, and verifies persisted PID/window ID/bounds
against the pre-capture identity while requiring the durable title to be empty.

## Normal-CI checks

The only tests run in ordinary CI are pure scanner/manifest tests; they neither
launch the fixture nor request Accessibility permission:

```bash
uv run pytest -q tests/test_live_ax_privacy_audit.py
uv run ruff check scripts/live_ax_privacy.py \
  scripts/run_macos_ax_privacy_audit.py \
  scripts/verify_macos_ax_privacy_report.py \
  tests/test_live_ax_privacy_audit.py
```

The Swift fixture can be compile-checked without launching it:

```bash
mkdir -p tests/live/macos_ax_privacy/.build/typecheck
swiftc tests/live/macos_ax_privacy/LiveAXFixture.swift \
  -o tests/live/macos_ax_privacy/.build/typecheck/LiveAXFixture \
  -O -swift-version 5 -framework AppKit -framework ApplicationServices
codesign --force --sign - \
  tests/live/macos_ax_privacy/.build/typecheck/LiveAXFixture
```

## Known boundaries

- multi-display geometry, minimized/off-screen windows, sheets/popovers, and
  cross-Space screenshot behavior are not covered by this two-window fixture;
- the audit calls the scheduler directly, so watcher reconnect/backoff and
  Input Monitoring/CGEventTap behavior remain separate tests;
- the fixture is a deliberately narrow browser test double. It proves the
  stable-ID parser-to-policy handoff for a nested AX text field, but does not
  cover real Safari/Chrome address-bar roles, DOM/WebArea behavior, localization,
  or browser updates;
- an editable address-control value is not proof that browser navigation
  committed. The audit verifies metadata-only persistence and sink isolation,
  not correspondence between the typed URL and a loaded document;
- revoking Accessibility permission during an already-running helper call is
  not automated;
- real Notes, TextEdit, Safari, and VS Code selection behavior remains a
  separate manual compatibility matrix; this fixture proves the AppKit/TCC
  boundary without touching user documents;
- a complete run still requires an interactive macOS session whose TCC and
  Automation permissions allow both AX access and active-window metadata; a
  locked/loginwindow session correctly produces an `incomplete` report.
