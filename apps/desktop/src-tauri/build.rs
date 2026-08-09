const COMMANDS: &[&str] = &[
    "get_snapshot",
    "get_candidate",
    "edit_candidate",
    "approve_candidate",
    "reject_candidate",
    "preview_forget_candidate",
    "forget_candidate",
    "get_daily_wrap",
    "transition_suggestion",
    "get_prompt_rescue",
    "queue_prompt_rescue",
    "edit_prompt_rescue",
    "retry_prompt_rescue",
    "delete_prompt_rescue",
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
