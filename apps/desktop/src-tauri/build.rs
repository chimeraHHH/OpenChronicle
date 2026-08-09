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
    "get_reply_rescue",
    "queue_reply_rescue",
    "edit_reply_rescue",
    "retry_reply_rescue",
    "delete_reply_rescue",
    "get_resume_rescue_state",
    "save_resume_rescue_profile",
    "save_resume_rescue_opportunity",
    "replace_resume_rescue_opportunity",
    "compose_resume_rescue_exact",
    "get_resume_rescue_preview",
    "export_resume_rescue_html",
    "open_resume_rescue_json",
    "admit_resume_rescue_json",
    "get_resume_rescue_json_export",
    "export_resume_rescue_json",
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
