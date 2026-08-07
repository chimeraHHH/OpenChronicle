use crate::bridge::{self, Operation, MAX_REQUEST_BYTES};
use crate::error::DesktopError;
use rfd::{MessageButtons, MessageDialog, MessageDialogResult, MessageLevel};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::{AppHandle, Manager};

const MAX_CANDIDATE_ID_CHARS: usize = 128;
const MAX_REFERENCE_ID_CHARS: usize = 512;
const MAX_PATH_CHARS: usize = 1_024;
const MAX_CONTENT_CHARS: usize = 20_000;
const MAX_TAGS: usize = 100;
const MAX_TAG_CHARS: usize = 100;
const MAX_REASON_CHARS: usize = 1_000;
const MAX_TIMELINE_ITEMS: usize = 24;
const MAX_CANDIDATE_ITEMS: usize = 100;
const MAX_WRAP_ITEMS: usize = 30;
const MAX_PROVENANCE_DEPTH: u8 = 8;

#[derive(Debug, Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct SnapshotRequest {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timeline_limit: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub candidate_limit: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub wrap_limit: Option<usize>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct CandidateRequest {
    pub candidate_id: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct EditCandidateRequest {
    pub candidate_id: String,
    pub expected_version: u64,
    pub content: String,
    pub tags: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ReviewCandidateRequest {
    pub candidate_id: String,
    pub expected_version: u64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct RejectCandidateRequest {
    pub candidate_id: String,
    pub expected_version: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ForgetCandidateRequest {
    pub candidate_id: String,
    pub expected_version: u64,
    pub plan_digest: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct DailyWrapRequest {
    pub local_date: String,
    pub timezone: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub scope: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct TraceProvenanceRequest {
    pub kind: String,
    pub artifact_id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max_depth: Option<u8>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResolveEvidenceRequest {
    pub kind: String,
    pub id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub path: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub timestamp: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content_hash: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct CapturePauseRequest {
    pub expected_state: bool,
    pub paused: bool,
}

#[derive(Debug, Deserialize)]
struct ForgetPreview {
    candidate_id: String,
    expected_version: u64,
    plan_digest: String,
    counts: ForgetCounts,
}

#[derive(Debug, Deserialize)]
struct ForgetCounts {
    candidates: u64,
    memory_files: u64,
    memory_entries: u64,
    daily_wraps: u64,
}

#[tauri::command]
pub async fn get_snapshot(request: SnapshotRequest) -> Result<Value, DesktopError> {
    validate_snapshot(&request)?;
    invoke(Operation::Snapshot, &request).await
}

#[tauri::command]
pub async fn get_candidate(request: CandidateRequest) -> Result<Value, DesktopError> {
    validate_candidate_id(&request.candidate_id)?;
    invoke(Operation::CandidateGet, &request).await
}

#[tauri::command]
pub async fn edit_candidate(request: EditCandidateRequest) -> Result<Value, DesktopError> {
    validate_edit_candidate(&request)?;
    invoke(Operation::CandidateEdit, &request).await
}

#[tauri::command]
pub async fn approve_candidate(request: ReviewCandidateRequest) -> Result<Value, DesktopError> {
    validate_candidate_id(&request.candidate_id)?;
    invoke(Operation::CandidateApprove, &request).await
}

#[tauri::command]
pub async fn reject_candidate(request: RejectCandidateRequest) -> Result<Value, DesktopError> {
    validate_candidate_id(&request.candidate_id)?;
    if let Some(reason) = &request.reason {
        validate_multiline_text(reason, MAX_REASON_CHARS, true)?;
    }
    invoke(Operation::CandidateReject, &request).await
}

#[tauri::command]
pub async fn preview_forget_candidate(
    request: ReviewCandidateRequest,
) -> Result<Value, DesktopError> {
    validate_candidate_id(&request.candidate_id)?;
    invoke(Operation::CandidateForgetPreview, &request).await
}

#[tauri::command]
pub async fn forget_candidate(
    app: AppHandle,
    request: ForgetCandidateRequest,
) -> Result<Value, DesktopError> {
    validate_forget_candidate(&request)?;
    tauri::async_runtime::spawn_blocking(move || forget_candidate_blocking(&app, request))
        .await
        .map_err(|_| {
            DesktopError::new(
                "BRIDGE_UNAVAILABLE",
                "The permanent-forget worker stopped unexpectedly.",
            )
        })?
}

#[tauri::command]
pub async fn get_daily_wrap(request: DailyWrapRequest) -> Result<Value, DesktopError> {
    validate_daily_wrap(&request)?;
    invoke(Operation::WrapGet, &request).await
}

#[tauri::command]
pub async fn trace_provenance(request: TraceProvenanceRequest) -> Result<Value, DesktopError> {
    validate_kind(&request.kind)?;
    validate_reference_id(&request.artifact_id)?;
    validate_optional_path(request.path.as_deref())?;
    if request
        .max_depth
        .is_some_and(|depth| depth > MAX_PROVENANCE_DEPTH)
    {
        return Err(DesktopError::invalid_request(
            "The provenance depth exceeds the allowed limit.",
        ));
    }
    invoke(Operation::ProvenanceTrace, &request).await
}

#[tauri::command]
pub async fn resolve_evidence(request: ResolveEvidenceRequest) -> Result<Value, DesktopError> {
    validate_kind(&request.kind)?;
    validate_reference_id(&request.id)?;
    validate_optional_path(request.path.as_deref())?;
    if let Some(timestamp) = &request.timestamp {
        validate_bounded_text(timestamp, 100, false)?;
    }
    if let Some(content_hash) = &request.content_hash {
        validate_digest(content_hash)?;
    }
    invoke(Operation::EvidenceResolve, &request).await
}

#[tauri::command]
pub async fn set_capture_paused(request: CapturePauseRequest) -> Result<Value, DesktopError> {
    if request.expected_state == request.paused {
        return Err(DesktopError::invalid_request(
            "The requested capture state is already expected.",
        ));
    }
    invoke(Operation::CaptureSetPaused, &request).await
}

async fn invoke<T: Serialize + ?Sized>(
    operation: Operation,
    request: &T,
) -> Result<Value, DesktopError> {
    let params = checked_value(request)?;
    bridge::call(operation, params).await
}

fn forget_candidate_blocking(
    app: &AppHandle,
    request: ForgetCandidateRequest,
) -> Result<Value, DesktopError> {
    let preview_params = serde_json::json!({
        "candidate_id": request.candidate_id,
        "expected_version": request.expected_version,
    });
    let preview_value = bridge::call_blocking(Operation::CandidateForgetPreview, preview_params)?;
    let preview: ForgetPreview = serde_json::from_value(preview_value).map_err(|_| {
        DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The desktop bridge returned an invalid forget preview.",
        )
    })?;

    if preview.candidate_id != request.candidate_id
        || preview.expected_version != request.expected_version
        || !constant_time_equal(
            preview.plan_digest.as_bytes(),
            request.plan_digest.as_bytes(),
        )
    {
        return Err(DesktopError::new(
            "STALE_PURGE_PLAN",
            "The forget preview changed. Review the updated impact before continuing.",
        ));
    }
    validate_digest(&preview.plan_digest).map_err(|_| {
        DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The desktop bridge returned an invalid forget preview.",
        )
    })?;

    let message = forget_confirmation_message(&preview.counts);
    let mut dialog = MessageDialog::new()
        .set_description(message)
        .set_title("Permanently forget this memory?")
        .set_level(MessageLevel::Warning)
        .set_buttons(MessageButtons::OkCancelCustom(
            "Permanently Forget".to_owned(),
            "Cancel".to_owned(),
        ));
    if let Some(window) = app.get_webview_window("main") {
        dialog = dialog.set_parent(&window);
    }
    let confirmed = match dialog.show() {
        MessageDialogResult::Ok | MessageDialogResult::Yes => true,
        MessageDialogResult::Custom(label) => label == "Permanently Forget",
        _ => false,
    };
    if !confirmed {
        return Err(DesktopError::new(
            "USER_CANCELLED",
            "Permanent forget was cancelled.",
        ));
    }

    let commit_params = serde_json::json!({
        "candidate_id": request.candidate_id,
        "expected_version": request.expected_version,
        "plan_digest": preview.plan_digest,
    });
    bridge::call_blocking(Operation::CandidateForgetCommit, commit_params)
}

fn checked_value<T: Serialize + ?Sized>(request: &T) -> Result<Value, DesktopError> {
    let encoded = serde_json::to_vec(request)
        .map_err(|_| DesktopError::invalid_request("The request could not be encoded."))?;
    if encoded.len() > MAX_REQUEST_BYTES {
        return Err(DesktopError::new(
            "REQUEST_TOO_LARGE",
            "The request exceeds the allowed size.",
        ));
    }
    serde_json::from_slice(&encoded)
        .map_err(|_| DesktopError::invalid_request("The request could not be encoded."))
}

fn forget_confirmation_message(counts: &ForgetCounts) -> String {
    format!(
        "This permanently changes local data:\n\nCandidate records removed: {}\nCandidate-created memory files cleaned or removed: {}\nMemory entries removed: {}\nDaily Wraps removed: {}\n\nThis cannot be undone. Upstream captures and external backups are not erased.",
        counts.candidates,
        counts.memory_files,
        counts.memory_entries,
        counts.daily_wraps
    )
}

fn validate_snapshot(request: &SnapshotRequest) -> Result<(), DesktopError> {
    if request
        .timeline_limit
        .is_some_and(|limit| limit > MAX_TIMELINE_ITEMS)
        || request
            .candidate_limit
            .is_some_and(|limit| limit > MAX_CANDIDATE_ITEMS)
        || request
            .wrap_limit
            .is_some_and(|limit| limit > MAX_WRAP_ITEMS)
    {
        return Err(DesktopError::invalid_request(
            "A snapshot limit exceeds the allowed maximum.",
        ));
    }
    Ok(())
}

fn validate_edit_candidate(request: &EditCandidateRequest) -> Result<(), DesktopError> {
    validate_candidate_id(&request.candidate_id)?;
    validate_multiline_text(&request.content, MAX_CONTENT_CHARS, false)?;
    if request.tags.len() > MAX_TAGS {
        return Err(DesktopError::invalid_request(
            "The candidate has too many tags.",
        ));
    }
    for tag in &request.tags {
        validate_bounded_text(tag, MAX_TAG_CHARS, false)?;
    }
    Ok(())
}

fn validate_forget_candidate(request: &ForgetCandidateRequest) -> Result<(), DesktopError> {
    validate_candidate_id(&request.candidate_id)?;
    validate_digest(&request.plan_digest)
}

fn validate_daily_wrap(request: &DailyWrapRequest) -> Result<(), DesktopError> {
    let date = request.local_date.as_bytes();
    if date.len() != 10
        || date[4] != b'-'
        || date[7] != b'-'
        || date
            .iter()
            .enumerate()
            .any(|(index, byte)| index != 4 && index != 7 && !byte.is_ascii_digit())
    {
        return Err(DesktopError::invalid_request(
            "The Daily Wrap date must use YYYY-MM-DD.",
        ));
    }
    if request.timezone.is_empty()
        || request.timezone.chars().count() > 100
        || request.timezone.starts_with('/')
        || request.timezone.contains("..")
        || !request
            .timezone
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'/' | b'_' | b'-' | b'+'))
    {
        return Err(DesktopError::invalid_request(
            "The Daily Wrap timezone is invalid.",
        ));
    }
    if let Some(scope) = &request.scope {
        validate_bounded_text(scope, 100, false)?;
    }
    Ok(())
}

fn validate_kind(kind: &str) -> Result<(), DesktopError> {
    if kind.is_empty()
        || kind.len() > 64
        || !kind
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
    {
        return Err(DesktopError::invalid_request(
            "The evidence kind is invalid.",
        ));
    }
    Ok(())
}

fn validate_candidate_id(value: &str) -> Result<(), DesktopError> {
    validate_bounded_text(value, MAX_CANDIDATE_ID_CHARS, false)
}

fn validate_reference_id(value: &str) -> Result<(), DesktopError> {
    validate_bounded_text(value, MAX_REFERENCE_ID_CHARS, false)
}

fn validate_optional_path(path: Option<&str>) -> Result<(), DesktopError> {
    if let Some(path) = path {
        // This is an opaque provenance selector forwarded to the fixed bridge
        // operation. Rust never opens it and the WebView has no filesystem API.
        validate_bounded_text(path, MAX_PATH_CHARS, true)?;
    }
    Ok(())
}

fn validate_bounded_text(
    value: &str,
    maximum_chars: usize,
    allow_empty: bool,
) -> Result<(), DesktopError> {
    if (!allow_empty && value.is_empty())
        || value.chars().count() > maximum_chars
        || value.chars().any(char::is_control)
    {
        return Err(DesktopError::invalid_request(
            "A request field is empty, too long, or contains control characters.",
        ));
    }
    Ok(())
}

fn validate_multiline_text(
    value: &str,
    maximum_chars: usize,
    allow_empty: bool,
) -> Result<(), DesktopError> {
    if (!allow_empty && value.is_empty())
        || value.chars().count() > maximum_chars
        || value
            .chars()
            .any(|character| character.is_control() && !matches!(character, '\n' | '\r' | '\t'))
    {
        return Err(DesktopError::invalid_request(
            "A text field is empty, too long, or contains unsupported control characters.",
        ));
    }
    Ok(())
}

fn validate_digest(value: &str) -> Result<(), DesktopError> {
    if value.len() != 64 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(DesktopError::invalid_request(
            "The request digest is invalid.",
        ));
    }
    Ok(())
}

fn constant_time_equal(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right)
        .fold(0_u8, |difference, (a, b)| difference | (a ^ b))
        == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn snapshot_limits_are_bounded() {
        let valid = SnapshotRequest {
            timeline_limit: Some(MAX_TIMELINE_ITEMS),
            candidate_limit: Some(MAX_CANDIDATE_ITEMS),
            wrap_limit: Some(MAX_WRAP_ITEMS),
        };
        assert!(validate_snapshot(&valid).is_ok());

        let invalid = SnapshotRequest {
            timeline_limit: Some(MAX_TIMELINE_ITEMS + 1),
            ..SnapshotRequest::default()
        };
        assert_eq!(
            validate_snapshot(&invalid).expect_err("must reject").code,
            "INVALID_REQUEST"
        );
    }

    #[test]
    fn candidate_edit_is_bounded() {
        let request = EditCandidateRequest {
            candidate_id: "mc-1".to_owned(),
            expected_version: 2,
            content: "A grounded fact".to_owned(),
            tags: vec!["project".to_owned()],
        };
        assert!(validate_edit_candidate(&request).is_ok());

        let oversized = EditCandidateRequest {
            content: "x".repeat(MAX_CONTENT_CHARS + 1),
            ..request
        };
        assert!(validate_edit_candidate(&oversized).is_err());
    }

    #[test]
    fn candidate_edit_accepts_multiline_markdown_but_rejects_nul() {
        let multiline = EditCandidateRequest {
            candidate_id: "mc-1".to_owned(),
            expected_version: 2,
            content: "First paragraph\n\n- item\n\tcontinuation\r\n".to_owned(),
            tags: vec!["project".to_owned()],
        };
        assert!(validate_edit_candidate(&multiline).is_ok());

        let with_nul = EditCandidateRequest {
            content: "trusted\0hidden".to_owned(),
            ..multiline
        };
        assert!(validate_edit_candidate(&with_nul).is_err());
    }

    #[test]
    fn candidate_edit_limits_match_the_python_bridge_contract() {
        let request = EditCandidateRequest {
            candidate_id: "候".repeat(MAX_CANDIDATE_ID_CHARS),
            expected_version: 2,
            content: "界".repeat(MAX_CONTENT_CHARS),
            tags: vec!["标".repeat(MAX_TAG_CHARS); MAX_TAGS],
        };
        assert!(validate_edit_candidate(&request).is_ok());

        let too_many_tags = EditCandidateRequest {
            tags: vec!["tag".to_owned(); MAX_TAGS + 1],
            ..request
        };
        assert!(validate_edit_candidate(&too_many_tags).is_err());
    }

    #[test]
    fn forget_digest_is_exact_hex() {
        assert!(validate_digest(&"a".repeat(64)).is_ok());
        assert!(validate_digest(&"g".repeat(64)).is_err());
        assert!(validate_digest(&"a".repeat(63)).is_err());
    }

    #[test]
    fn digest_comparison_checks_every_byte() {
        assert!(constant_time_equal(b"abcd", b"abcd"));
        assert!(!constant_time_equal(b"abcd", b"abce"));
        assert!(!constant_time_equal(b"abcd", b"abc"));
    }

    #[test]
    fn daily_wrap_rejects_path_shaped_timezone_escape() {
        let valid = DailyWrapRequest {
            local_date: "2026-08-08".to_owned(),
            timezone: "Asia/Shanghai".to_owned(),
            scope: None,
        };
        assert!(validate_daily_wrap(&valid).is_ok());

        let invalid = DailyWrapRequest {
            timezone: "../../tmp".to_owned(),
            ..valid
        };
        assert!(validate_daily_wrap(&invalid).is_err());
    }

    #[test]
    fn request_size_is_measured_as_utf8_json() {
        let request = serde_json::json!({"content": "界".repeat(30_000)});
        assert_eq!(
            checked_value(&request)
                .expect_err("UTF-8 request must exceed limit")
                .code,
            "REQUEST_TOO_LARGE"
        );
    }

    #[test]
    fn optional_bridge_fields_are_omitted_instead_of_encoded_as_null() {
        let snapshot = checked_value(&SnapshotRequest::default()).expect("snapshot JSON");
        assert_eq!(snapshot, serde_json::json!({}));

        let reject = checked_value(&RejectCandidateRequest {
            candidate_id: "mc-1".to_owned(),
            expected_version: 2,
            reason: None,
        })
        .expect("reject JSON");
        assert_eq!(
            reject,
            serde_json::json!({"candidate_id": "mc-1", "expected_version": 2})
        );

        let wrap = checked_value(&DailyWrapRequest {
            local_date: "2026-08-08".to_owned(),
            timezone: "Asia/Shanghai".to_owned(),
            scope: None,
        })
        .expect("wrap JSON");
        assert_eq!(
            wrap,
            serde_json::json!({"local_date": "2026-08-08", "timezone": "Asia/Shanghai"})
        );

        let trace = checked_value(&TraceProvenanceRequest {
            kind: "memory_candidate".to_owned(),
            artifact_id: "mc-1".to_owned(),
            path: None,
            max_depth: None,
        })
        .expect("provenance JSON");
        assert_eq!(
            trace,
            serde_json::json!({"kind": "memory_candidate", "artifact_id": "mc-1"})
        );

        let evidence = checked_value(&ResolveEvidenceRequest {
            kind: "observation".to_owned(),
            id: "obs-1".to_owned(),
            path: None,
            timestamp: None,
            content_hash: None,
        })
        .expect("evidence JSON");
        assert_eq!(
            evidence,
            serde_json::json!({"kind": "observation", "id": "obs-1"})
        );
    }

    #[test]
    fn native_forget_confirmation_requires_the_memory_file_count() {
        let complete = serde_json::json!({
            "candidate_id": "mc-1",
            "expected_version": 2,
            "plan_digest": "a".repeat(64),
            "counts": {
                "candidates": 1,
                "memory_files": 1,
                "memory_entries": 1,
                "daily_wraps": 1
            }
        });
        assert!(serde_json::from_value::<ForgetPreview>(complete.clone()).is_ok());

        let mut missing = complete;
        missing
            .get_mut("counts")
            .and_then(Value::as_object_mut)
            .expect("counts object")
            .remove("memory_files");
        assert!(serde_json::from_value::<ForgetPreview>(missing).is_err());
    }

    #[test]
    fn native_forget_confirmation_describes_files_as_cleaned_or_removed() {
        let message = forget_confirmation_message(&ForgetCounts {
            candidates: 1,
            memory_files: 2,
            memory_entries: 3,
            daily_wraps: 4,
        });

        assert!(message.starts_with("This permanently changes local data:"));
        assert!(message.contains("Candidate records removed: 1"));
        assert!(message.contains("Candidate-created memory files cleaned or removed: 2"));
        assert!(message.contains("Memory entries removed: 3"));
        assert!(message.contains("Daily Wraps removed: 4"));
        assert!(!message.starts_with("This permanently removes:"));
    }
}
