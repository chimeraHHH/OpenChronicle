use crate::error::DesktopError;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::env;
#[cfg(not(unix))]
use std::ffi::OsStr;
use std::fs;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

pub(crate) const MAX_REQUEST_BYTES: usize = 12 * 1024 * 1024;
const PROTOCOL_VERSION: u8 = 14;
// The largest allowlisted response is wrap.get: the Python bridge bounds five
// categories to 100 items each and each item to 20 bounded references. Even if
// every bounded character needs JSON's six-byte control-character escape, the
// current schema remains below 120 MB. Keep a strict 128 MiB ceiling so every
// schema-bounded snapshot/wrap response fits while a faulty sidecar still has a
// finite memory budget. Unbounded record closures still fail closed at the cap.
const MAX_RESPONSE_BYTES: usize = 128 * 1024 * 1024;
#[cfg(test)]
const MAX_JSON_ESCAPE_BYTES_PER_CHAR: usize = 6;
#[cfg(test)]
const MAX_WRAP_ITEMS: usize = 5 * 100;
#[cfg(test)]
const MAX_WRAP_REFERENCES_PER_ITEM: usize = 20;
#[cfg(test)]
const MAX_REFERENCE_CHARS: usize = 64 + 512 + 1_024 + 100 + 128;
#[cfg(test)]
const MAX_WRAP_ITEM_CHARS: usize =
    128 + 50 + 500 + 500 + MAX_WRAP_REFERENCES_PER_ITEM * MAX_REFERENCE_CHARS;
#[cfg(test)]
const MAX_WRAP_OTHER_CHARS: usize = 16_000;
#[cfg(test)]
const MAX_JSON_STRUCTURE_BYTES: usize = 2_000_000;
#[cfg(test)]
const MAX_DECLARED_BACKEND_RESPONSE_BYTES: usize =
    (MAX_WRAP_ITEMS * MAX_WRAP_ITEM_CHARS + MAX_WRAP_OTHER_CHARS) * MAX_JSON_ESCAPE_BYTES_PER_CHAR
        + MAX_JSON_STRUCTURE_BYTES;
const MAX_STDERR_BYTES: usize = 32_768;
const BRIDGE_TIMEOUT: Duration = Duration::from_secs(5);
const DOCUMENT_BRIDGE_TIMEOUT: Duration = Duration::from_secs(40);
const POLL_INTERVAL: Duration = Duration::from_millis(10);
#[cfg(debug_assertions)]
const BRIDGE_OVERRIDE: &str = "OPENCHRONICLE_DESKTOP_BRIDGE";
const BRIDGE_FILENAME: &str = "openchronicle-desktop-bridge";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Operation {
    Snapshot,
    CandidateGet,
    CandidateEdit,
    CandidateApprove,
    CandidateReject,
    CandidateForgetPreview,
    CandidateForgetCommit,
    WrapGet,
    SuggestionTransition,
    PromptRescueGet,
    PromptRescueQueue,
    PromptRescueQueueSelection,
    PromptRescueEdit,
    PromptRescueRetry,
    PromptRescueDelete,
    ReplyRescueGet,
    ReplyRescueQueue,
    ReplyRescueQueueSelection,
    ReplyRescueEdit,
    ReplyRescueRetry,
    ReplyRescueDelete,
    ResumeRescueState,
    ResumeRescueSaveProfile,
    ResumeRescueSaveOpportunity,
    ResumeRescueReplaceOpportunity,
    ResumeRescueComposeExact,
    ResumeRescuePreview,
    ResumeRescueReviewJson,
    ResumeRescueAdmitJson,
    ResumeRescueExportJson,
    ResumeRescueExportDocx,
    ResumeRescueExportPdf,
    ResumeRescueReviewDocument,
    ResumeRescueAdmitDocument,
    ResumeRescueQueueRewrite,
    ResumeRescueRetryRewrite,
    ResumeRescueDeleteRewrite,
    ResumeRescueDecideRewrite,
    ResumeRescueRestoreRewrite,
    ResumeRescuePreviewRewrite,
    ResumeRescueExportRewriteJson,
    ResumeRescueExportRewriteDocx,
    ResumeRescueExportRewritePdf,
    ProvenanceTrace,
    EvidenceResolve,
    CaptureSetPaused,
}

impl Operation {
    fn as_str(self) -> &'static str {
        match self {
            Self::Snapshot => "snapshot",
            Self::CandidateGet => "candidate.get",
            Self::CandidateEdit => "candidate.edit",
            Self::CandidateApprove => "candidate.approve",
            Self::CandidateReject => "candidate.reject",
            Self::CandidateForgetPreview => "candidate.forget_preview",
            Self::CandidateForgetCommit => "candidate.forget_commit",
            Self::WrapGet => "wrap.get",
            Self::SuggestionTransition => "suggestion.transition",
            Self::PromptRescueGet => "prompt_rescue.get",
            Self::PromptRescueQueue => "prompt_rescue.queue",
            Self::PromptRescueQueueSelection => "prompt_rescue.queue_selection",
            Self::PromptRescueEdit => "prompt_rescue.edit",
            Self::PromptRescueRetry => "prompt_rescue.retry",
            Self::PromptRescueDelete => "prompt_rescue.delete",
            Self::ReplyRescueGet => "reply_rescue.get",
            Self::ReplyRescueQueue => "reply_rescue.queue",
            Self::ReplyRescueQueueSelection => "reply_rescue.queue_selection",
            Self::ReplyRescueEdit => "reply_rescue.edit",
            Self::ReplyRescueRetry => "reply_rescue.retry",
            Self::ReplyRescueDelete => "reply_rescue.delete",
            Self::ResumeRescueState => "resume_rescue.state",
            Self::ResumeRescueSaveProfile => "resume_rescue.save_profile",
            Self::ResumeRescueSaveOpportunity => "resume_rescue.save_opportunity",
            Self::ResumeRescueReplaceOpportunity => "resume_rescue.replace_opportunity",
            Self::ResumeRescueComposeExact => "resume_rescue.compose_exact",
            Self::ResumeRescuePreview => "resume_rescue.preview",
            Self::ResumeRescueReviewJson => "resume_rescue.review_json",
            Self::ResumeRescueAdmitJson => "resume_rescue.admit_json",
            Self::ResumeRescueExportJson => "resume_rescue.export_json",
            Self::ResumeRescueExportDocx => "resume_rescue.export_docx",
            Self::ResumeRescueExportPdf => "resume_rescue.export_pdf",
            Self::ResumeRescueReviewDocument => "resume_rescue.review_document",
            Self::ResumeRescueAdmitDocument => "resume_rescue.admit_document",
            Self::ResumeRescueQueueRewrite => "resume_rescue.queue_rewrite",
            Self::ResumeRescueRetryRewrite => "resume_rescue.retry_rewrite",
            Self::ResumeRescueDeleteRewrite => "resume_rescue.delete_rewrite",
            Self::ResumeRescueDecideRewrite => "resume_rescue.decide_rewrite",
            Self::ResumeRescueRestoreRewrite => "resume_rescue.restore_rewrite",
            Self::ResumeRescuePreviewRewrite => "resume_rescue.preview_rewrite",
            Self::ResumeRescueExportRewriteJson => "resume_rescue.export_rewrite_json",
            Self::ResumeRescueExportRewriteDocx => "resume_rescue.export_rewrite_docx",
            Self::ResumeRescueExportRewritePdf => "resume_rescue.export_rewrite_pdf",
            Self::ProvenanceTrace => "provenance.trace",
            Self::EvidenceResolve => "evidence.resolve",
            Self::CaptureSetPaused => "capture.set_paused",
        }
    }

    fn timeout(self) -> Duration {
        match self {
            Self::ResumeRescueReviewDocument
            | Self::ResumeRescueAdmitDocument
            | Self::ResumeRescueExportDocx
            | Self::ResumeRescueExportPdf
            | Self::ResumeRescueExportRewriteDocx
            | Self::ResumeRescueExportRewritePdf => DOCUMENT_BRIDGE_TIMEOUT,
            _ => BRIDGE_TIMEOUT,
        }
    }
}

#[derive(Serialize)]
struct BridgeRequest<'a> {
    version: u8,
    operation: &'a str,
    params: Value,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct BridgeResponse {
    version: u8,
    ok: bool,
    #[serde(default)]
    result: Option<Value>,
    #[serde(default)]
    error: Option<BridgeServiceError>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct BridgeServiceError {
    code: String,
    // The bridge intentionally returns a generic message. Rust still maps the
    // code to its own stable copy so backend details never cross into WebView.
    #[allow(dead_code)]
    message: String,
}

struct CapturedOutput {
    bytes: Vec<u8>,
    exceeded: bool,
}

pub(crate) async fn call(operation: Operation, params: Value) -> Result<Value, DesktopError> {
    tauri::async_runtime::spawn_blocking(move || call_blocking(operation, params))
        .await
        .map_err(|_| {
            DesktopError::new(
                "BRIDGE_UNAVAILABLE",
                "The local desktop bridge worker stopped unexpectedly.",
            )
        })?
}

pub(crate) fn call_blocking(operation: Operation, params: Value) -> Result<Value, DesktopError> {
    let executable = resolve_bridge_path()?;
    call_blocking_with_path(operation, params, &executable)
}

fn call_blocking_with_path(
    operation: Operation,
    params: Value,
    executable: &Path,
) -> Result<Value, DesktopError> {
    let request = serde_json::to_vec(&BridgeRequest {
        version: PROTOCOL_VERSION,
        operation: operation.as_str(),
        params,
    })
    .map_err(|_| DesktopError::invalid_request("The request could not be encoded."))?;

    if request.len() > MAX_REQUEST_BYTES {
        return Err(DesktopError::new(
            "REQUEST_TOO_LARGE",
            "The request exceeds the allowed size.",
        ));
    }

    let working_directory = executable.parent().ok_or_else(|| {
        DesktopError::new(
            "BRIDGE_UNAVAILABLE",
            "The local desktop bridge is not available.",
        )
    })?;
    let mut command = Command::new(executable);
    command
        .current_dir(working_directory)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env_clear()
        .env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin");
    copy_allowed_environment(&mut command);

    let mut child = command.spawn().map_err(|_| {
        DesktopError::new(
            "BRIDGE_UNAVAILABLE",
            "The local desktop bridge could not be started.",
        )
    })?;

    let mut stdin = child.stdin.take().ok_or_else(|| {
        DesktopError::new(
            "BRIDGE_UNAVAILABLE",
            "The local desktop bridge could not accept a request.",
        )
    })?;
    let stdout = child.stdout.take().ok_or_else(|| {
        DesktopError::new(
            "BRIDGE_UNAVAILABLE",
            "The local desktop bridge could not return a response.",
        )
    })?;
    let stderr = child.stderr.take().ok_or_else(|| {
        DesktopError::new(
            "BRIDGE_UNAVAILABLE",
            "The local desktop bridge could not report its status.",
        )
    })?;

    let input_writer = thread::spawn(move || -> io::Result<()> {
        stdin.write_all(&request)?;
        stdin.write_all(b"\n")?;
        stdin.flush()
    });
    let output_reader = thread::spawn(move || read_capped(stdout, MAX_RESPONSE_BYTES));
    let error_reader = thread::spawn(move || read_capped(stderr, MAX_STDERR_BYTES));

    let deadline = Instant::now() + operation.timeout();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => thread::sleep(POLL_INTERVAL),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = input_writer.join();
                let _ = output_reader.join();
                let _ = error_reader.join();
                return Err(DesktopError::new(
                    "BRIDGE_TIMEOUT",
                    "The local operation exceeded the five-second time limit.",
                ));
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = input_writer.join();
                let _ = output_reader.join();
                let _ = error_reader.join();
                return Err(DesktopError::new(
                    "BRIDGE_UNAVAILABLE",
                    "The local desktop bridge stopped unexpectedly.",
                ));
            }
        }
    };

    input_writer
        .join()
        .map_err(|_| {
            DesktopError::new(
                "BRIDGE_UNAVAILABLE",
                "The local desktop bridge could not receive the request.",
            )
        })?
        .map_err(|_| {
            DesktopError::new(
                "BRIDGE_UNAVAILABLE",
                "The local desktop bridge could not receive the request.",
            )
        })?;
    let stdout = join_reader(output_reader)?;
    let stderr = join_reader(error_reader)?;

    if stdout.exceeded {
        return Err(DesktopError::new(
            "BRIDGE_RESPONSE_TOO_LARGE",
            "The local desktop bridge response exceeded the allowed size.",
        ));
    }
    if stderr.exceeded {
        return Err(DesktopError::new(
            "BRIDGE_STDERR_TOO_LARGE",
            "The local desktop bridge produced excessive diagnostic output.",
        ));
    }

    let response = parse_response(&stdout.bytes)?;
    if response.ok {
        if !status.success() {
            return Err(DesktopError::new(
                "BRIDGE_FAILED",
                "The local desktop bridge exited unsuccessfully.",
            ));
        }
        response.result.ok_or_else(|| {
            DesktopError::new(
                "BRIDGE_PROTOCOL_ERROR",
                "The local desktop bridge omitted its result.",
            )
        })
    } else {
        let service_error = response.error.ok_or_else(|| {
            DesktopError::new(
                "BRIDGE_PROTOCOL_ERROR",
                "The local desktop bridge omitted its error.",
            )
        })?;
        Err(DesktopError::from_bridge(&service_error.code))
    }
}

fn copy_allowed_environment(command: &mut Command) {
    const ALLOWED: &[&str] = &[
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "OPENCHRONICLE_ROOT",
        "__CF_USER_TEXT_ENCODING",
    ];
    for key in ALLOWED {
        if let Some(value) = env::var_os(key) {
            command.env(key, value);
        }
    }
}

fn join_reader(
    handle: thread::JoinHandle<io::Result<CapturedOutput>>,
) -> Result<CapturedOutput, DesktopError> {
    handle
        .join()
        .map_err(|_| {
            DesktopError::new(
                "BRIDGE_UNAVAILABLE",
                "The local desktop bridge output could not be read.",
            )
        })?
        .map_err(|_| {
            DesktopError::new(
                "BRIDGE_UNAVAILABLE",
                "The local desktop bridge output could not be read.",
            )
        })
}

fn read_capped(mut reader: impl Read, limit: usize) -> io::Result<CapturedOutput> {
    let mut bytes = Vec::with_capacity(limit.min(8_192));
    let mut buffer = [0_u8; 8_192];
    let mut exceeded = false;

    loop {
        let read = reader.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        let remaining = limit.saturating_sub(bytes.len());
        let keep = remaining.min(read);
        bytes.extend_from_slice(&buffer[..keep]);
        if keep < read {
            exceeded = true;
        }
    }

    Ok(CapturedOutput { bytes, exceeded })
}

fn parse_response(bytes: &[u8]) -> Result<BridgeResponse, DesktopError> {
    let text = std::str::from_utf8(bytes).map_err(|_| {
        DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The local desktop bridge returned non-UTF-8 output.",
        )
    })?;
    let line = text.strip_suffix('\n').unwrap_or(text);
    let line = line.strip_suffix('\r').unwrap_or(line);
    if line.is_empty() || line.contains(['\n', '\r']) {
        return Err(DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The local desktop bridge must return exactly one JSON line.",
        ));
    }
    let response: BridgeResponse = serde_json::from_str(line).map_err(|_| {
        DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The local desktop bridge returned malformed JSON.",
        )
    })?;
    let has_result = response.result.is_some();
    let has_error = response.error.is_some();
    if response.version != PROTOCOL_VERSION
        || response.ok != has_result
        || response.ok == has_error
        || has_result == has_error
    {
        return Err(DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The local desktop bridge returned an invalid envelope.",
        ));
    }
    Ok(response)
}

fn resolve_bridge_path() -> Result<PathBuf, DesktopError> {
    #[cfg(debug_assertions)]
    {
        if let Some(override_path) = env::var_os(BRIDGE_OVERRIDE) {
            return validate_override(Path::new(&override_path));
        }
    }

    for candidate in fixed_bridge_candidates() {
        if is_executable_file(&candidate) {
            return candidate.canonicalize().map_err(|_| {
                DesktopError::new(
                    "BRIDGE_UNAVAILABLE",
                    "The local desktop bridge path could not be resolved.",
                )
            });
        }
    }

    Err(DesktopError::new(
        "BRIDGE_NOT_FOUND",
        "The local desktop bridge is not installed.",
    ))
}

#[cfg(any(debug_assertions, test))]
fn validate_override(path: &Path) -> Result<PathBuf, DesktopError> {
    if !path.is_absolute() {
        return Err(DesktopError::new(
            "BRIDGE_OVERRIDE_INVALID",
            "The desktop bridge override must be an absolute executable path.",
        ));
    }
    if !is_executable_file(path) {
        return Err(DesktopError::new(
            "BRIDGE_OVERRIDE_INVALID",
            "The desktop bridge override must be an absolute executable path.",
        ));
    }
    path.canonicalize().map_err(|_| {
        DesktopError::new(
            "BRIDGE_OVERRIDE_INVALID",
            "The desktop bridge override could not be resolved.",
        )
    })
}

fn fixed_bridge_candidates() -> Vec<PathBuf> {
    let mut candidates = Vec::new();

    #[cfg(debug_assertions)]
    {
        let repository_root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../..")
            .join(".venv/bin")
            .join(BRIDGE_FILENAME);
        candidates.push(repository_root);
    }

    if let Ok(current_executable) = env::current_exe() {
        if let Some(directory) = current_executable.parent() {
            candidates.push(directory.join(BRIDGE_FILENAME));
        }
    }

    #[cfg(target_os = "macos")]
    {
        candidates.push(PathBuf::from(
            "/Applications/OpenChronicle.app/Contents/MacOS/openchronicle-desktop-bridge",
        ));
        candidates.push(PathBuf::from(
            "/Library/Application Support/OpenChronicle/bin/openchronicle-desktop-bridge",
        ));
    }

    candidates
}

fn is_executable_file(path: &Path) -> bool {
    let Ok(metadata) = fs::metadata(path) else {
        return false;
    };
    if !metadata.is_file() {
        return false;
    }

    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        metadata.permissions().mode() & 0o111 != 0
    }

    #[cfg(not(unix))]
    {
        path.extension()
            .and_then(OsStr::to_str)
            .is_some_and(|extension| extension.eq_ignore_ascii_case("exe"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::io::Cursor;

    #[test]
    fn operation_names_are_fixed() {
        assert_eq!(Operation::Snapshot.as_str(), "snapshot");
        assert_eq!(Operation::CandidateGet.as_str(), "candidate.get");
        assert_eq!(
            Operation::CandidateForgetCommit.as_str(),
            "candidate.forget_commit"
        );
        assert_eq!(Operation::CaptureSetPaused.as_str(), "capture.set_paused");
        assert_eq!(
            Operation::SuggestionTransition.as_str(),
            "suggestion.transition"
        );
        assert_eq!(Operation::PromptRescueGet.as_str(), "prompt_rescue.get");
        assert_eq!(Operation::PromptRescueQueue.as_str(), "prompt_rescue.queue");
        assert_eq!(
            Operation::PromptRescueQueueSelection.as_str(),
            "prompt_rescue.queue_selection"
        );
        assert_eq!(Operation::PromptRescueEdit.as_str(), "prompt_rescue.edit");
        assert_eq!(Operation::PromptRescueRetry.as_str(), "prompt_rescue.retry");
        assert_eq!(
            Operation::PromptRescueDelete.as_str(),
            "prompt_rescue.delete"
        );
        assert_eq!(Operation::ReplyRescueGet.as_str(), "reply_rescue.get");
        assert_eq!(Operation::ReplyRescueQueue.as_str(), "reply_rescue.queue");
        assert_eq!(
            Operation::ReplyRescueQueueSelection.as_str(),
            "reply_rescue.queue_selection"
        );
        assert_eq!(Operation::ReplyRescueEdit.as_str(), "reply_rescue.edit");
        assert_eq!(Operation::ReplyRescueRetry.as_str(), "reply_rescue.retry");
        assert_eq!(Operation::ReplyRescueDelete.as_str(), "reply_rescue.delete");
        assert_eq!(
            Operation::ResumeRescuePreview.as_str(),
            "resume_rescue.preview"
        );
        assert_eq!(
            Operation::ResumeRescueReviewJson.as_str(),
            "resume_rescue.review_json"
        );
        assert_eq!(
            Operation::ResumeRescueAdmitJson.as_str(),
            "resume_rescue.admit_json"
        );
        assert_eq!(
            Operation::ResumeRescueExportJson.as_str(),
            "resume_rescue.export_json"
        );
        assert_eq!(
            Operation::ResumeRescueExportDocx.as_str(),
            "resume_rescue.export_docx"
        );
        assert_eq!(
            Operation::ResumeRescueExportPdf.as_str(),
            "resume_rescue.export_pdf"
        );
        assert_eq!(
            Operation::ResumeRescueReviewDocument.as_str(),
            "resume_rescue.review_document"
        );
        assert_eq!(
            Operation::ResumeRescueAdmitDocument.as_str(),
            "resume_rescue.admit_document"
        );
        assert_eq!(
            Operation::ResumeRescueReviewDocument.timeout(),
            DOCUMENT_BRIDGE_TIMEOUT
        );
        assert_eq!(
            Operation::ResumeRescueAdmitDocument.timeout(),
            DOCUMENT_BRIDGE_TIMEOUT
        );
        assert_eq!(
            Operation::ResumeRescueExportRewriteJson.as_str(),
            "resume_rescue.export_rewrite_json"
        );
        assert_eq!(
            Operation::ResumeRescueExportRewriteDocx.as_str(),
            "resume_rescue.export_rewrite_docx"
        );
        assert_eq!(
            Operation::ResumeRescueExportRewritePdf.as_str(),
            "resume_rescue.export_rewrite_pdf"
        );
        assert_eq!(
            Operation::ResumeRescueExportRewriteDocx.timeout(),
            DOCUMENT_BRIDGE_TIMEOUT
        );
        assert_eq!(
            Operation::ResumeRescueExportRewritePdf.timeout(),
            DOCUMENT_BRIDGE_TIMEOUT
        );
        assert_eq!(Operation::ResumeRescueReviewJson.timeout(), BRIDGE_TIMEOUT);
    }

    #[test]
    fn capped_reader_drains_but_retains_only_the_limit() {
        let input = vec![7_u8; 257];
        let captured = read_capped(Cursor::new(input), 64).expect("read should succeed");
        assert_eq!(captured.bytes.len(), 64);
        assert!(captured.exceeded);
    }

    #[test]
    fn capped_reader_accepts_the_exact_boundary() {
        let input = vec![7_u8; 64];
        let captured = read_capped(Cursor::new(input), 64).expect("read should succeed");
        assert_eq!(captured.bytes.len(), 64);
        assert!(!captured.exceeded);
    }

    #[test]
    fn response_limit_covers_the_declared_backend_schema_with_headroom() {
        let response_limit = std::hint::black_box(MAX_RESPONSE_BYTES);
        let declared_maximum = std::hint::black_box(MAX_DECLARED_BACKEND_RESPONSE_BYTES);
        assert!(response_limit > declared_maximum);
        assert!(declared_maximum < 120_000_000);
        assert_eq!(response_limit, 134_217_728);
    }

    #[test]
    fn response_must_be_one_strict_versioned_line() {
        let response = parse_response(b"{\"version\":14,\"ok\":true,\"result\":{}}\n")
            .expect("valid response");
        assert!(response.ok);

        assert!(
            parse_response(b"{\"version\":14,\"ok\":true,\"result\":{}}\n{\"extra\":true}\n")
                .is_err()
        );
        assert!(parse_response(b"{\"version\":2,\"ok\":true,\"result\":{}}\n").is_err());
        assert!(
            parse_response(b"{\"version\":14,\"ok\":true,\"result\":{},\"unknown\":true}\n")
                .is_err()
        );
        assert!(parse_response(
            b"{\"version\":14,\"ok\":false,\"result\":{},\"error\":{\"code\":\"BUSY\",\"message\":\"busy\"}}\n"
        )
        .is_err());
    }

    #[test]
    fn backend_error_is_mapped_by_code_only() {
        let response = parse_response(
            b"{\"version\":14,\"ok\":false,\"error\":{\"code\":\"BUSY\",\"message\":\"secret detail\"}}\n",
        )
        .expect("valid error envelope");
        let error = DesktopError::from_bridge(&response.error.expect("error").code);
        assert_eq!(error.code, "BUSY");
        assert!(!error.message.contains("secret detail"));
    }

    #[test]
    fn relative_override_is_rejected_without_falling_back() {
        let error = validate_override(Path::new("openchronicle-desktop-bridge"))
            .expect_err("relative path must fail closed");
        assert_eq!(error.code, "BRIDGE_OVERRIDE_INVALID");
    }

    #[test]
    fn oversized_request_is_rejected_before_spawn() {
        let huge = json!({"value": "x".repeat(MAX_REQUEST_BYTES)});
        let error = call_blocking_with_path(Operation::Snapshot, huge, Path::new("/bin/cat"))
            .expect_err("oversized request must fail");
        assert_eq!(error.code, "REQUEST_TOO_LARGE");
    }

    #[cfg(unix)]
    #[test]
    fn executable_cannot_turn_request_into_a_different_operation() {
        let error = call_blocking_with_path(Operation::Snapshot, json!({}), Path::new("/bin/cat"))
            .expect_err("cat cannot produce a bridge response");
        assert_eq!(error.code, "BRIDGE_PROTOCOL_ERROR");
    }
}
