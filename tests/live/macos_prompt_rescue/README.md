# macOS Prompt Rescue application fixtures

`selection_fixture.html` is a network-free Safari fixture for the manual
Prompt Rescue compatibility matrix. It contains only a public test marker and
selects a bounded substring in a textarea. The production selection helper
must bind Safari, the fixture window, the focused textarea, and the exact
UTF-16 range; an address-bar selection is a different source and must not be
accepted as the textarea marker.

The AppKit/TCC, empty-selection, secure-field, and policy-preflight checks are
automated by the broader opt-in runner documented in
`../macos_ax_privacy/README.md`. This directory does not automate TextEdit,
Notes, or VS Code because doing so can create application-managed documents or
depend on locally installed software. Their point-in-time results and cleanup
boundary are recorded in
`../../../docs/vida-prompt-rescue-live-selection-2026-08-09.md`.
