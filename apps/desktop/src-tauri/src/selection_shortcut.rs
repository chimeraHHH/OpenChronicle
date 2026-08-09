use crate::bridge::{self, Operation};
use crate::tray;
use rfd::MessageLevel;
use serde_json::json;
use std::sync::atomic::{AtomicBool, Ordering};
use tauri::{Emitter, Manager};
use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutEvent, ShortcutState};

pub(crate) const PROMPT_RESCUE_SHORTCUT: &str = "CommandOrControl+Shift+Space";
pub(crate) const REPLY_RESCUE_SHORTCUT: &str = "CommandOrControl+Shift+R";
static SELECTION_CAPTURE_ACTIVE: AtomicBool = AtomicBool::new(false);

struct CaptureGuard;

impl Drop for CaptureGuard {
    fn drop(&mut self) {
        SELECTION_CAPTURE_ACTIVE.store(false, Ordering::Release);
    }
}

pub(crate) fn install(app: &tauri::AppHandle) {
    install_one(
        app,
        PROMPT_RESCUE_SHORTCUT,
        Operation::PromptRescueQueueSelection,
        "prompt-rescue",
        "Prompt Rescue could not import the selection",
    );
    install_one(
        app,
        REPLY_RESCUE_SHORTCUT,
        Operation::ReplyRescueQueueSelection,
        "reply-rescue",
        "Reply Rescue could not import the selection",
    );
}

fn install_one(
    app: &tauri::AppHandle,
    shortcut: &'static str,
    operation: Operation,
    page: &'static str,
    failure_title: &'static str,
) {
    if let Err(error) =
        app.global_shortcut()
            .on_shortcut(shortcut, move |app, _shortcut, event: ShortcutEvent| {
                if event.state != ShortcutState::Pressed
                    || SELECTION_CAPTURE_ACTIVE.swap(true, Ordering::AcqRel)
                {
                    return;
                }
                let app = app.clone();
                tauri::async_runtime::spawn_blocking(move || {
                    let _guard = CaptureGuard;
                    let outcome = bridge::call_blocking(operation, json!({})).map(|_| ());
                    let ui_app = app.clone();
                    let _ = app.run_on_main_thread(move || match outcome {
                        Ok(()) => {
                            tray::show_main_window(&ui_app);
                            if let Some(window) = ui_app.get_webview_window("main") {
                                let _ = window.emit("desktop:navigate", page);
                                let _ = window.emit("desktop:refresh", ());
                            }
                        }
                        Err(error) => tray::show_message(
                            &ui_app,
                            failure_title,
                            error.message,
                            MessageLevel::Error,
                        ),
                    });
                });
            })
    {
        // Shortcut registration conflicts must not prevent the privacy/review
        // app from opening. No selection content or credentials are logged.
        eprintln!("Selection shortcut unavailable: {error}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn selection_shortcut_requires_command_and_never_implies_submit() {
        assert!(PROMPT_RESCUE_SHORTCUT.contains("CommandOrControl"));
        assert!(PROMPT_RESCUE_SHORTCUT.contains("Shift"));
        assert!(!PROMPT_RESCUE_SHORTCUT
            .to_ascii_lowercase()
            .contains("enter"));
        assert!(REPLY_RESCUE_SHORTCUT.contains("CommandOrControl"));
        assert!(REPLY_RESCUE_SHORTCUT.contains("Shift"));
        assert!(!REPLY_RESCUE_SHORTCUT.to_ascii_lowercase().contains("enter"));
        assert_ne!(PROMPT_RESCUE_SHORTCUT, REPLY_RESCUE_SHORTCUT);
    }
}
