const COMMANDS: &[&str] = &[
    "get_snapshot",
    "get_candidate",
    "edit_candidate",
    "approve_candidate",
    "reject_candidate",
    "preview_forget_candidate",
    "forget_candidate",
    "get_daily_wrap",
    "trace_provenance",
    "resolve_evidence",
    "set_capture_paused",
];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("failed to build the OpenChronicle desktop manifest");
}
