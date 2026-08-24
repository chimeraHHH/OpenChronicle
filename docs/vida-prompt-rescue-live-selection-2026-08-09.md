# Prompt Rescue live macOS selection acceptance — 2026-08-09

## Scope

This note records only the native exact-selection boundary for Prompt Rescue.
It does not claim that the broader macOS AX/privacy audit, the real-application
compatibility matrix, Prompt Rescue model quality, or Stage 2 is complete.

The run used an unlocked interactive `arm64` Mac on macOS 26.5.2 (build 25F84).
The real OpenChronicle daemon was stopped. The signed AppKit fixture and all
OpenChronicle sinks used by the audit were isolated from the user's normal data
root; the temporary root was removed after the run.

## Production path exercised

The audit compiled the checked-in `mac-ax-selection.swift` helper, opened the
signed two-window AppKit fixture, and called the production
`capture_selection()` adapter. The adapter first invoked the helper's
metadata-only mode, applied the current privacy policy, then invoked selected
text mode and compared the returned app, bundle, PID, and window title with the
preflight receipt. The helper also fenced the focused element, selected range,
and selected text across its own before/after reads.

Four required checks passed:

| Check | Result | Evidence retained |
|---|---|---|
| Exact ordinary selection | Pass | One closed receipt; selected text equaled the random fixture marker, bundle/window/range matched |
| Empty selection | Pass | Stable `no_selection` denial |
| Selected secure field | Pass | Stable `secure_field` denial; no secure value retained |
| Excluded bundle preflight | Pass | Stable `privacy_denied`; the only native invocation was `--frontmost-window-metadata` |

The report retained no plaintext markers or raw AX payloads and passed the
independent verifier in `--allow-incomplete` mode. Its local SHA-256 was
`a565c3bf66cf7264c84d43f3bf6e1b8d12745af6eefb892767dda8dea8b2c6e2`.
The report itself remains ignored because it is a machine-local audit artifact.

## Real-application compatibility matrix

The same production adapter was then exercised against disposable content in
installed applications. Only result booleans, closed identities, roles, and
ranges were retained.

| Application | Source | Result | Closed receipt |
|---|---|---|---|
| TextEdit | Dedicated test document | Pass | `com.apple.TextEdit`, `AXTextArea`, ASCII `17+33`; Unicode/emoji `7+25` |
| Notes | New note containing only a public test marker | Pass | `com.apple.Notes`, `AXTextArea`, `0+33` |
| Safari | Repository-local textarea fixture, no network | Pass | `com.apple.Safari`, `AXTextArea`, Unicode/emoji `0+46` |
| VS Code | Not installed on the acceptance machine | Unavailable | No result claimed |

Safari initially exposed its file URL address field as the focused
`AXTextField`; the adapter did not confuse that 116-unit selection with page
content. After the local textarea was explicitly focused, its exact selection
passed. This is expected focus binding, not a fallback.

TextEdit auto-saved the dedicated test document and Notes auto-saved the test
note. They contain only the public matrix markers, but were not deleted without
the user's explicit cleanup approval.

## Exact commands

```bash
uv run openchronicle status
uv run python scripts/run_macos_ax_privacy_audit.py \
  --acknowledge-live-ax \
  --acknowledge-production-capture-paused \
  --report tests/live/macos_ax_privacy/reports/prompt-rescue-selection-2026-08-09.json
uv run python scripts/verify_macos_ax_privacy_report.py \
  --allow-incomplete \
  tests/live/macos_ax_privacy/reports/prompt-rescue-selection-2026-08-09.json
```

## Boundaries and next gate

The same report was globally `incomplete`: three exact-window screenshot color
checks failed and the later timeline block was not produced, so downstream
prompt/sink and rapid-focus checks were skipped. Those failures are not hidden
or counted as selection passes; they remain separate Stage 0 audit work.

Prompt Rescue's next source-binding gate is VS Code compatibility on a machine
where it is installed plus a real-application focus-change stress case. The
prepared-prompt quality gate still requires a reachable, explicitly configured
provider; no model score is synthesized from the native acceptance run.
