mod bridge;
mod commands;
mod error;
mod selection_shortcut;
mod tray;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(commands::ResumeDocumentVault::default())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .setup(|app| {
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            tray::install(app)?;
            selection_shortcut::install(app.handle());
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() == "main" {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_snapshot,
            commands::get_candidate,
            commands::export_published_memory,
            commands::correct_published_memory,
            commands::preview_forget_published_memory,
            commands::forget_published_memory,
            commands::edit_candidate,
            commands::approve_candidate,
            commands::reject_candidate,
            commands::preview_forget_candidate,
            commands::forget_candidate,
            commands::get_daily_wrap,
            commands::transition_suggestion,
            commands::create_resume_cue,
            commands::transition_resume_cue,
            commands::get_prompt_rescue,
            commands::queue_prompt_rescue,
            commands::edit_prompt_rescue,
            commands::retry_prompt_rescue,
            commands::delete_prompt_rescue,
            commands::get_reply_rescue,
            commands::queue_reply_rescue,
            commands::edit_reply_rescue,
            commands::retry_reply_rescue,
            commands::delete_reply_rescue,
            commands::get_resume_rescue_state,
            commands::queue_resume_rescue_rewrite,
            commands::retry_resume_rescue_rewrite,
            commands::delete_resume_rescue_rewrite,
            commands::decide_resume_rescue_rewrite,
            commands::restore_resume_rescue_rewrite,
            commands::get_resume_rescue_rewrite_preview,
            commands::get_resume_rescue_rewrite_pdf_preview,
            commands::get_resume_rescue_rewrite_json_export,
            commands::export_resume_rescue_rewrite_json,
            commands::export_resume_rescue_rewrite_docx,
            commands::export_resume_rescue_rewrite_pdf,
            commands::save_resume_rescue_profile,
            commands::save_resume_rescue_opportunity,
            commands::replace_resume_rescue_opportunity,
            commands::compose_resume_rescue_exact,
            commands::get_resume_rescue_preview,
            commands::get_resume_rescue_pdf_preview,
            commands::export_resume_rescue_html,
            commands::open_resume_rescue_json,
            commands::admit_resume_rescue_json,
            commands::open_resume_rescue_document,
            commands::admit_resume_rescue_document,
            commands::discard_resume_rescue_document,
            commands::get_resume_rescue_json_export,
            commands::export_resume_rescue_json,
            commands::export_resume_rescue_docx,
            commands::export_resume_rescue_pdf,
            commands::trace_provenance,
            commands::resolve_evidence,
            commands::set_capture_paused,
        ])
        .run(tauri::generate_context!())
        .expect("failed to run the OpenChronicle desktop shell");
}
