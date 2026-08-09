# Desktop release gate

Debug builds may resolve only an explicit absolute
`OPENCHRONICLE_DESKTOP_BRIDGE` executable or the repository
`.venv/bin/openchronicle-desktop-bridge`. Release builds resolve only fixed app
installation locations and never search `PATH`.

`npm run tauri:bundle` builds a PyInstaller 6.22.0 one-file bridge for the
current allowlisted macOS architecture, verifies the Mach-O architecture,
smoke-tests protocol v14 in an isolated data root, writes a SHA-256 manifest,
and passes the target-suffixed binary to Tauri through
`tauri.bundle.conf.json`. Generated binaries and manifests are ignored.
The finalizer checks both Mach-O files, preserves the sidecar hash in local
mode, verifies the complete signature tree, re-runs the bundled sidecar, and
holds the actual app open for five seconds against a new isolated data root.
It also drives the packaged bridge through reviewed JSON Resume admission,
exact composition/preview, JSON/DOCX/PDF export, and DOCX extraction. Until the
PDF engine is self-contained, the development manifest records its exact
`EXPORT_UNAVAILABLE` result as `blocked_unbundled_engine`; every other bridge
error remains fatal and `release_gate_passed` remains false.

This development bundle is still not a release artifact. Its final local
verification preserves PyInstaller's sidecar signature and ad-hoc signs only
the native shell and outer `.app`; this is not a distribution identity.
Distribution remains
blocked until both macOS architectures pass clean-machine install, launch,
protocol fault, uninstall, Developer ID signing, notarization, and Gatekeeper
verification. The sidecar must be rebuilt on each target OS/architecture; it is
never downloaded at app runtime.

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
