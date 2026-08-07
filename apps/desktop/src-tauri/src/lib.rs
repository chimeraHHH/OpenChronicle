mod bridge;
mod commands;
mod error;
mod tray;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            tray::install(app)?;
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
            commands::trace_provenance,
            commands::resolve_evidence,
            commands::set_capture_paused,
        ])
        .run(tauri::generate_context!())
        .expect("failed to run the OpenChronicle desktop shell");
}
