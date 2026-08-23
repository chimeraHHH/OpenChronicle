# Stage 1 trusted desktop shell

The desktop shell is a clean-room, local control and review surface for the
OpenChronicle Memory Plane. It is not a chat surface and it does not contain an
Action Plane. Captured text is always displayed as untrusted evidence; it never
becomes an instruction source for the WebView, Rust core, or Python backend.

## Process boundary

```text
React/Vite WebView
    | fixed, typed invoke commands only
    v
Tauri Rust core + native tray/confirmation
    | one bounded JSON request on stdin, one bounded response on stdout
    v
openchronicle-desktop-bridge
    | shared Config / SQLite / MemoryService / provenance locks
    v
OpenChronicle local stores
```

The WebView has no shell, filesystem, SQL, HTTP, MCP, Keychain, or arbitrary
URL capability. Rust selects the bridge executable and operation; the frontend
cannot supply an executable, working directory, data root, operation name, or
free-form argument vector. Each request runs in a short-lived process so the UI
continues to work when the capture daemon is stopped and a crashed review call
does not leave a durable write endpoint behind.

The production app must bundle a self-contained bridge sidecar for each target
architecture. Development may use an explicit absolute
`OPENCHRONICLE_DESKTOP_BRIDGE` path or the repository virtual environment.
Depending on a GUI user's shell `PATH`, Python, or `uv` is not a release path.

## Exposed product operations

The bridge protocol is versioned and allowlisted. Protocol **v18** cumulatively
includes immutable Daily Wrap reads, side-effect-free suggestions, Prompt and
Reply Rescue review, exact-selection receipts, reviewed résumé source/import
flows, deterministic preview/export, supervised résumé proposal review, a
bounded typed current-fact snapshot, and explicit local current-memory export.
Older requests or
responses fail closed as unsupported protocol envelopes. The shell exposes
only:

- a local-only, model-ping-free status snapshot;
- bounded recent timeline, review-inbox, current published-memory, and Daily
  Wrap summaries;
- user-triggered JSON or Markdown export of the authorized current-memory view
  to a newly created local file; no model or network call is involved;
- one candidate read/edit/approve/reject operation with optimistic version
  checking;
- two-phase permanent forget with a version- and closure-bound plan digest;
- exact Daily Wrap reads;
- bounded Work Resumption cards with acknowledgement/dismissal only;
- Prompt Rescue manual-input and global-shortcut exact-selection queue,
  read/edit/retry/delete operations, and explicit clipboard copy in the
  WebView;
- Reply Rescue manual-conversation and distinct global-shortcut exact-selection
  queues, read/edit/retry/delete operations, visible unverified conversation-
  identity/recipient warnings, and explicit clipboard copy;
- Résumé Rescue reviewed profile versions, immutable opportunity snapshots,
  unchecked PDF/DOCX/JSON imports, exact fact selection and requirement
  ledgers, deterministic preview, and loss-explicit JSON/DOCX/PDF export;
- separately opted-in résumé wording jobs with disclosed model location,
  proposal-level accept/reject CAS, non-destructive version restore, and
  reviewed-derivative preview/JSON/DOCX/PDF export;
- bounded provenance tracing and exact, policy-aware evidence resolution;
- compare-and-set pause/resume for **new capture**.

It deliberately does not expose generic process execution, arbitrary files or
paths, arbitrary SQL, provider/model probing, Daily Wrap generation, bulk
approval, connectors, target-app paste/replacement, message sending, prompt
submission, or any other external effect.

## Dangerous-action semantics

- **Pause new capture** stops later observations. Already captured local work
  may still be reduced, classified, or included in a configured model call.
- **Save changes** edits only a candidate and does not write durable memory.
- **Save reviewed memory** materializes one reviewed local Markdown entry. It
  performs no external action.
- **Reject proposal** retains review history and source evidence; it is not
  deletion.
- **Copy reviewed prompt** writes only the reviewed prepared text to the shared
  clipboard. OpenChronicle does not claim where it will be pasted and has no
  command to paste or submit it.
- **Import selected text** is initiated with `Command-Shift-Space` while the
  external app still owns focus. The native probe reads only a stable
  `AXSelectedText` range, rejects secure/multiple/empty/policy-excluded/racing
  sources, queues the receipt, and only then focuses OpenChronicle. It never
  reads `AXValue` or the clipboard as a fallback.
- **Delete Prompt Rescue job** uses a native confirmation and version-bound
  delete to remove the local rough input, artifact, and provenance edge. It
  does not alter clipboard contents or another application.
- **Copy reviewed reply** writes only the reviewed reply body to the shared
  clipboard. It cannot create a provider draft, paste, address, or send it.
- **Delete Reply Rescue job** uses a native confirmation and version-bound
  delete to remove the local conversation, artifact, and provenance edge. It
  does not alter any mailbox, provider draft, clipboard content, or external
  application.
- **Accept résumé wording** applies exactly one digest-bound, locally validated
  proposal to a new opportunity-specific version. It never modifies the
  reviewed master profile, and there is no accept-all command.
- **Restore résumé wording** copies a prior reviewed artifact into a new current
  immutable version; prior decisions remain in the audit history.
- **Save résumé export** asks for a native destination, creates a new private
  file, and refuses overwrite or symlink replacement. It does not upload,
  autofill, apply, submit, or send the résumé.
- **Permanently forget** can remove the candidate, accepted/derived memory
  entries, and affected Daily Wraps. An unchanged candidate-created Markdown
  container is deleted when empty; if it contains surviving canonical entries,
  candidate-derived owner metadata, description, and tags are sanitized while
  the surviving entries remain. It does not delete pre-existing or
  user-modified files, and does not promise to erase upstream raw captures,
  APFS snapshots, backups, provider logs, or copies made elsewhere.

Forget is previewed under the provenance/review fence. The preview binds the
candidate version and canonical transitive deletion closure to a digest. Rust
re-fetches the preview, presents a native confirmation sheet, and commits only
if the digest is unchanged. A concurrent edit or new derivative invalidates the
preview instead of silently broadening the deletion. Candidate-created files
carry an ownership marker bound to their stable initial metadata; automatic
cleanup fails closed if the marker, stable metadata, or symlink status changed;
verified files with surviving content are sanitized instead of deleted. If any
canonical entry has a damaged provenance frame, OpenChronicle cannot prove the
transitive closure and blocks the preview until that local record is repaired.

## Source drawer

Evidence resolution is by exact reference, never nearest timestamp. Reads
recheck tombstones, current capture policy, canonical content hashes, valid
Markdown provenance, and transitive dependencies while holding the same locks
as cleanup/rebuild. Missing, changed, excluded, expired, or purging sources are
reported as such and are never replaced with nearby content.

The drawer returns bounded text and metadata only. Screenshots, full AX trees,
URLs, HTML, and rendered Markdown are excluded, and common credential patterns
are redacted before display. That redaction is defense in depth rather than a
claim that arbitrary captured prose can be classified perfectly as secret.
React uses ordinary text nodes plus bidi isolation; no evidence string can
navigate, invoke Tauri, or create a link.

## Published Memory / About Me

The Memory page is an inspect-only projection of current, authorized entries
from active non-`event-*` Markdown files. It supports local text filtering and
About Me/project scopes, and opens the exact entry's source lineage in the
existing drawer. Superseded entries and reducer-owned session logs are excluded
from the current view; the canonical Markdown history remains intact. New facts
and corrections continue through the Review Inbox.

## Tauri security profile

- one local `main` WebView and one capability;
- custom commands declared in the build manifest and split into read/review
  permission sets;
- native tray owns open, pause/resume, the permissions-view entry, and menu-app
  quit;
- strict production CSP with no remote assets; no global Tauri object, asset
  protocol, devtools, shell plugin capability, or broad core defaults;
- the isolation pattern applies an additional command and request-size filter;
- closing the window hides it; quitting the menu app does not claim to stop or
  delete the OpenChronicle daemon or its data.

The design follows the official Tauri guidance for
[capabilities](https://v2.tauri.app/security/capabilities/),
[permissions](https://v2.tauri.app/security/permissions/),
[CSP](https://v2.tauri.app/security/csp/), the
[isolation pattern](https://v2.tauri.app/concept/inter-process-communication/isolation/),
[Rust commands](https://v2.tauri.app/develop/calling-rust/), and the
[system tray](https://v2.tauri.app/learn/system-tray/).

## Current product boundary

Daily Wrap is read-only in the first shell slice. The scheduler retains
`running/succeeded/failed` internally, but protocol v5 exposes only an
authorized published revision (`status="succeeded"`) and its `ready/partial`
coverage state. Neither field is user acceptance. The UI therefore does not
present fake Accept/Edit/Ignore actions. A later revision-bound review overlay
is required before those controls can exist.

Capture exclusions and other TOML settings are also read-only in this slice.
Safe editing requires an atomic, comment-preserving, allowlisted settings
service with a config etag and an explicit daemon-restart result. Pause is the
only immediate privacy mutation.

## Verification

The release gate includes:

```bash
uv run pytest -q
uv run ruff check .
cd apps/desktop && nvm use && npm ci
cd apps/desktop && npm test
cd apps/desktop && npm run build
cd apps/desktop && npm run tauri:dev
cd apps/desktop/src-tauri && cargo test --locked --all-targets
cd apps/desktop/src-tauri && cargo clippy --locked --all-targets -- -D warnings
cd apps/desktop/src-tauri && cargo check --locked --release
```

In addition, the packaged macOS app must be tested for first-run Accessibility
behavior, denied permission, pause latency, window close/hide, native forget
confirmation, sidecar absence/crash/timeout, offline operation, VoiceOver,
reduced motion, high contrast, and 200% text scaling. Signing, notarization,
self-contained sidecar production, and TCC tests on supported macOS versions
remain release gates rather than claims made by a source-tree build.
