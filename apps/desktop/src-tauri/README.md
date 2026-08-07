# Desktop release gate

This source tree deliberately does **not** bundle the
`openchronicle-desktop-bridge` sidecar yet. Debug builds may resolve only an
explicit absolute `OPENCHRONICLE_DESKTOP_BRIDGE` executable or the repository
`.venv/bin/openchronicle-desktop-bridge`. Release builds resolve only fixed app
installation locations and never search `PATH`.

Shipping a desktop bundle is blocked until a self-contained bridge is built for
every target architecture, added to the bundle, signed with the app, and tested
for missing, crashed, oversized, malformed, and timed-out responses. A source
build or unsigned `.app` is not a release artifact.

The WebView capability intentionally contains no shell, filesystem, HTTP,
dialog, process, SQL, asset-protocol, or arbitrary URL permission. Native Rust
uses the platform dialog library directly and owns sidecar selection, tray
actions, and permanent-forget confirmation; no dialog plugin is registered.

Development uses the Node.js version pinned in `../.nvmrc`. Its CSP permits
Vite's inline development styles only in `devCsp`; the production CSP retains
`style-src 'self'` and ships no JavaScript source maps.
Native development uses the Rust version pinned in `rust-toolchain.toml`; CI
also requires `Cargo.lock` with `--locked` so dependency resolution cannot
silently change the tested toolchain floor.
