# Desktop sidecar packaging scout — 2026-08-09

This note records the public implementation references and the selected
clean-room release boundary for OpenChronicle's Python desktop bridge.

## Current references

| Reference | Observed boundary | OpenChronicle decision |
| --- | --- | --- |
| [Tauri 2 external binaries](https://v2.tauri.app/develop/sidecar/) | `bundle.externalBin` embeds a sidecar whose build input is named with the target triple; the shell/command permission system is optional when Rust itself owns process creation. | Use a target-suffixed build input and the existing Rust-owned fixed-path spawn. Do not grant WebView shell/process permissions. |
| [PyInstaller 6.22 usage](https://pyinstaller.org/en/stable/usage.html) and [macOS multi-arch notes](https://pyinstaller.org/en/stable/feature-notes.html#macos-multi-arch-support) | Builds are OS-specific; current-architecture macOS builds are thin by default; a real universal2 build requires a universal2 Python and universal dependencies. macOS signing and UPX have explicit constraints. | Pin 6.22.0, build thin arm64/x86_64 sidecars independently, disable UPX, verify each Mach-O suffix, and never merge one-file outputs with `lipo`. |
| [OpenAdapt Desktop](https://github.com/OpenAdaptAI/openadapt-desktop) and its [public design](https://github.com/OpenAdaptAI/openadapt-desktop/blob/main/DESIGN.md) | Public architecture uses a Tauri cockpit, bundled Python engine, and stdio/localhost IPC; its current README calls most E2E tests mocked and describes the product as beta. | Reuse only the architectural comparison. Keep OpenChronicle's one-request strict JSON line, environment clearing, response caps, timeouts, and no runtime upload/update authority. |

PyInstaller is not a cross-compiler. A macOS arm64 development build therefore
proves only the local arm64 artifact. Intel macOS, Developer ID signing,
notarization, Gatekeeper, clean-machine install/launch/uninstall, and protocol
fault injection remain independent release gates.

## Selected build boundary

1. `uv` installs the exact `desktop-bundle` dependency group.
2. `scripts/build_desktop_sidecar.py` builds only an allowlisted target triple.
3. The one-file bridge is named
   `openchronicle-desktop-bridge-<target-triple>` for Tauri ingestion.
4. A build fails unless `lipo -archs` matches the suffix and an isolated
   `resume_rescue.state` protocol-v14 request returns the closed schema.
5. The build emits binary SHA-256, byte size, tool version, target, and smoke
   result in a local manifest.
6. Tauri embeds the binary only through the explicit bundle config. Runtime
   code still resolves an adjacent fixed executable, clears the environment,
   and never searches `PATH` in release builds.
7. Frozen child workers cannot use `sys.executable -m` or `-c`, because that
   executable is the bridge bootloader rather than a general Python command.
   Document extraction and provider calls therefore re-enter the same binary
   through exact allowlisted worker arguments; unknown or extra arguments are
   rejected before any bridge request is read.
8. Local evidence preserves PyInstaller's sidecar signature, then ad-hoc signs
   only the native shell and outer app so nested-code integrity is testable.
   A release must instead pass the same Developer ID into PyInstaller and Tauri
   before either signs; one-file embedded libraries cannot be re-signed later.
9. Finalization re-runs the adjacent bundled sidecar, launches the actual app
   for five seconds with an isolated root, requires its config and SQLite state,
   and emits a second local manifest for the complete `.app`.
10. The packaged bridge must also complete reviewed JSON Resume ingress, exact
   composition/preview, JSON/DOCX/PDF export, and DOCX extraction from isolated
   synthetic inputs. Import success in the development environment is not
   accepted as packaged-dependency evidence.
11. The current external Chrome/Poppler PDF path is deliberately reported as
    `blocked_unbundled_engine` by development finalization. Only that exact
    fail-closed code is recorded as a known gap; it cannot make the release
    gate pass, and no other bridge failure is downgraded.

No packaged binary, signature, notarization ticket, or clean-machine result is
committed to Git. Source and deterministic packaging tests are reviewable;
release artifacts belong in an authenticated release pipeline.
