mod bridge;
mod commands;
mod error;
mod selection_shortcut;
mod tray;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
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
            commands::edit_candidate,
            commands::approve_candidate,
            commands::reject_candidate,
            commands::preview_forget_candidate,
            commands::forget_candidate,
            commands::get_daily_wrap,
            commands::transition_suggestion,
            commands::get_prompt_rescue,
            commands::queue_prompt_rescue,
            commands::edit_prompt_rescue,
            commands::retry_prompt_rescue,
            commands::delete_prompt_rescue,
            commands::trace_provenance,
            commands::resolve_evidence,
            commands::set_capture_paused,
        ])
        .run(tauri::generate_context!())
        .expect("failed to run the OpenChronicle desktop shell");
}
