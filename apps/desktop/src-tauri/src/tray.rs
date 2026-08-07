use crate::bridge::{self, Operation};
use crate::error::DesktopError;
use rfd::{MessageButtons, MessageDialog, MessageLevel};
use serde_json::{json, Value};
use tauri::image::Image;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Emitter, Manager};

const MAIN_WINDOW: &str = "main";
const OPEN_ITEM: &str = "open";
const PAUSE_ITEM: &str = "pause-capture";
const PERMISSIONS_ITEM: &str = "permissions";
const QUIT_ITEM: &str = "quit";

pub(crate) fn install(app: &tauri::App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, OPEN_ITEM, "Open OpenChronicle", true, None::<&str>)?;
    let pause = MenuItem::with_id(
        app,
        PAUSE_ITEM,
        "Pause or Resume Capture",
        true,
        None::<&str>,
    )?;
    let permissions = MenuItem::with_id(
        app,
        PERMISSIONS_ITEM,
        "Permissions & Privacy…",
        true,
        None::<&str>,
    )?;
    let quit = MenuItem::with_id(app, QUIT_ITEM, "Quit OpenChronicle", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &pause, &permissions, &quit])?;

    TrayIconBuilder::new()
        .icon(tray_icon())
        .icon_as_template(true)
        .tooltip("OpenChronicle")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id().as_ref() {
            OPEN_ITEM => show_main_window(app),
            PAUSE_ITEM => {
                let app = app.clone();
                tauri::async_runtime::spawn_blocking(move || {
                    if let Err(error) = toggle_capture(&app) {
                        show_native_error(&app, error);
                    }
                });
            }
            PERMISSIONS_ITEM => {
                show_main_window(app);
                if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
                    let _ = window.emit("desktop:navigate", "permissions");
                }
            }
            QUIT_ITEM => app.exit(0),
            _ => {}
        })
        .build(app)?;
    Ok(())
}

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn toggle_capture(app: &tauri::AppHandle) -> Result<(), DesktopError> {
    let snapshot = bridge::call_blocking(
        Operation::Snapshot,
        json!({
            "timeline_limit": 0,
            "candidate_limit": 0,
            "wrap_limit": 0
        }),
    )?;
    let paused = capture_paused(&snapshot).ok_or_else(|| {
        DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The local snapshot omitted the capture state.",
        )
    })?;
    bridge::call_blocking(
        Operation::CaptureSetPaused,
        json!({"expected_state": paused, "paused": !paused}),
    )?;

    let action = if paused { "resumed" } else { "paused" };
    show_message(
        app,
        "OpenChronicle capture",
        format!("New capture is now {action}."),
        MessageLevel::Info,
    );
    Ok(())
}

fn capture_paused(snapshot: &Value) -> Option<bool> {
    snapshot
        .get("capture")
        .and_then(|capture| capture.get("paused"))
        .and_then(Value::as_bool)
}

fn show_native_error(app: &tauri::AppHandle, error: DesktopError) {
    show_message(
        app,
        "OpenChronicle could not change capture",
        error.message,
        MessageLevel::Error,
    );
}

fn show_message(app: &tauri::AppHandle, title: &str, description: String, level: MessageLevel) {
    let mut dialog = MessageDialog::new()
        .set_description(description)
        .set_title(title)
        .set_level(level)
        .set_buttons(MessageButtons::Ok);
    if let Some(window) = app.get_webview_window(MAIN_WINDOW) {
        dialog = dialog.set_parent(&window);
    }
    let _ = dialog.show();
}

fn tray_icon() -> Image<'static> {
    const WIDTH: u32 = 18;
    const HEIGHT: u32 = 18;
    let mut rgba = vec![0_u8; (WIDTH * HEIGHT * 4) as usize];

    for y in 2..16 {
        for x in 2..16 {
            let centered_x = i32::try_from(x).unwrap_or_default() - 9;
            let centered_y = i32::try_from(y).unwrap_or_default() - 9;
            let radius_squared = centered_x * centered_x + centered_y * centered_y;
            let ring = (25..=49).contains(&radius_squared);
            let hand = (x == 9 && (5..=10).contains(&y)) || (y == 10 && (9..=13).contains(&x));
            if ring || hand {
                let offset = ((y * WIDTH + x) * 4) as usize;
                rgba[offset] = 0;
                rgba[offset + 1] = 0;
                rgba[offset + 2] = 0;
                rgba[offset + 3] = 255;
            }
        }
    }

    Image::new_owned(rgba, WIDTH, HEIGHT)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tray_uses_only_the_documented_items() {
        assert_eq!(
            [OPEN_ITEM, PAUSE_ITEM, PERMISSIONS_ITEM, QUIT_ITEM],
            ["open", "pause-capture", "permissions", "quit"]
        );
    }

    #[test]
    fn capture_pause_state_is_read_from_the_fixed_snapshot_field() {
        assert_eq!(
            capture_paused(&json!({"capture": {"paused": true}})),
            Some(true)
        );
        assert_eq!(capture_paused(&json!({"capture_paused": true})), None);
    }

    #[test]
    fn generated_tray_icon_has_the_expected_shape() {
        let icon = tray_icon();
        assert_eq!(icon.width(), 18);
        assert_eq!(icon.height(), 18);
        assert!(icon.rgba().iter().any(|channel| *channel != 0));
    }
}
