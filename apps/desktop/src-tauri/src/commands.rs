use crate::bridge::{self, Operation, MAX_REQUEST_BYTES};
use crate::error::DesktopError;
use rfd::{MessageButtons, MessageDialog, MessageDialogResult, MessageLevel};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashSet;
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
const MAX_SUGGESTION_ITEMS: usize = 50;
const MAX_PROMPT_RESCUE_ITEMS: usize = 50;
const MAX_PROMPT_RESCUE_INPUT_CHARS: usize = 20_000;
const MAX_PROMPT_RESCUE_OUTPUT_CHARS: usize = 30_000;
const MAX_PROMPT_RESCUE_CONTEXT_CHARS: usize = 500;
const MAX_PROMPT_RESCUE_CONSTRAINTS: usize = 20;
const MAX_REPLY_RESCUE_ITEMS: usize = 50;
const MAX_REPLY_RESCUE_INPUT_CHARS: usize = 50_000;
const MAX_REPLY_RESCUE_OUTPUT_CHARS: usize = 30_000;
const MAX_REPLY_RESCUE_FIELD_CHARS: usize = 1_000;
const MAX_REPLY_RESCUE_PARTICIPANTS: usize = 50;
const MAX_REPLY_RESCUE_DIRECTIONS: usize = 20;
const MAX_RESUME_ITEMS: usize = 50;
const MAX_RESUME_FACTS: usize = 2_000;
const MAX_RESUME_CONFLICTS: usize = 500;
const MAX_RESUME_REQUIREMENTS: usize = 200;
const MAX_RESUME_PRIORITIES: usize = 50;
const MAX_RESUME_FACT_TEXT_CHARS: usize = 8_000;
const MAX_RESUME_OPPORTUNITY_CHARS: usize = 50_000;
const MAX_RESUME_REQUIREMENT_CHARS: usize = 5_000;
const MAX_RESUME_PRIORITY_CHARS: usize = 2_000;
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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub suggestion_limit: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub prompt_rescue_limit: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reply_rescue_limit: Option<usize>,
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

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct SuggestionTransitionRequest {
    pub suggestion_id: String,
    pub expected_version: u64,
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PromptRescueGetRequest {
    pub job_id: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PromptRescueQueueRequest {
    pub rough_prompt: String,
    pub target: String,
    pub audience: String,
    pub constraints: Vec<String>,
    pub desired_format: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PromptRescueEditRequest {
    pub job_id: String,
    pub expected_version: u64,
    pub improved_prompt: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct PromptRescueCasRequest {
    pub job_id: String,
    pub expected_version: u64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ReplyRescueGetRequest {
    pub job_id: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ReplyRescueQueueRequest {
    pub conversation_text: String,
    pub participants: Vec<String>,
    pub intended_recipients: Vec<String>,
    pub reply_mode: String,
    pub goal: String,
    pub tone: String,
    pub style_instructions: Vec<String>,
    pub commitments: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ReplyRescueEditRequest {
    pub job_id: String,
    pub expected_version: u64,
    pub reply_body: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ReplyRescueCasRequest {
    pub job_id: String,
    pub expected_version: u64,
}

#[derive(Debug, Default, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub(crate) struct ResumeRescueStateRequest {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub profile_limit: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub opportunity_limit: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub projection_limit: Option<usize>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum ResumeProvenanceRequest {
    ManualReviewed {
        reviewed_at: String,
    },
    DocumentExcerpt {
        reviewed_at: String,
        source_id: String,
        source_digest: String,
        page: u64,
        section: String,
        start: u64,
        end: u64,
        extraction_method: String,
    },
    ReviewedMemory {
        reviewed_at: String,
        memory_id: String,
        memory_path: String,
        memory_digest: String,
    },
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeFactRequest {
    pub id: String,
    pub section: String,
    pub text: String,
    pub confidentiality: String,
    pub ownership_scope: String,
    pub provenance: Vec<ResumeProvenanceRequest>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeConflictRequest {
    pub id: String,
    pub fact_ids: Vec<String>,
    pub description: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeSaveProfileRequest {
    pub profile_id: String,
    pub display_name: String,
    pub locale: String,
    pub facts: Vec<ResumeFactRequest>,
    pub conflicts: Vec<ResumeConflictRequest>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expected_version: Option<u64>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeSaveOpportunityRequest {
    pub employer: String,
    pub title: String,
    pub source_text: String,
    pub source_url: String,
    pub priorities: Vec<String>,
    pub locale: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub captured_at: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeReplaceOpportunityRequest {
    pub opportunity_id: String,
    pub expected_digest: String,
    pub employer: String,
    pub title: String,
    pub source_text: String,
    pub source_url: String,
    pub priorities: Vec<String>,
    pub locale: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub captured_at: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeProjectionSectionRequest {
    pub kind: String,
    pub fact_ids: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeRequirementRequest {
    pub id: String,
    pub text: String,
    pub fact_ids: Vec<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeComposeExactRequest {
    pub profile_id: String,
    pub opportunity_id: String,
    pub sections: Vec<ResumeProjectionSectionRequest>,
    pub requirements: Vec<ResumeRequirementRequest>,
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
pub async fn transition_suggestion(
    request: SuggestionTransitionRequest,
) -> Result<Value, DesktopError> {
    validate_candidate_id(&request.suggestion_id)?;
    if !matches!(request.status.as_str(), "viewed" | "accepted" | "dismissed") {
        return Err(DesktopError::invalid_request(
            "The suggestion transition is unavailable.",
        ));
    }
    if let Some(reason) = &request.reason {
        validate_multiline_text(reason, MAX_REASON_CHARS, true)?;
    }
    invoke(Operation::SuggestionTransition, &request).await
}

#[tauri::command]
pub async fn get_prompt_rescue(request: PromptRescueGetRequest) -> Result<Value, DesktopError> {
    validate_prompt_rescue_job_id(&request.job_id)?;
    invoke(Operation::PromptRescueGet, &request).await
}

#[tauri::command]
pub async fn queue_prompt_rescue(request: PromptRescueQueueRequest) -> Result<Value, DesktopError> {
    validate_prompt_rescue_queue(&request)?;
    invoke(Operation::PromptRescueQueue, &request).await
}

#[tauri::command]
pub async fn edit_prompt_rescue(request: PromptRescueEditRequest) -> Result<Value, DesktopError> {
    validate_prompt_rescue_job_id(&request.job_id)?;
    validate_prompt_rescue_version(request.expected_version)?;
    validate_multiline_text(
        &request.improved_prompt,
        MAX_PROMPT_RESCUE_OUTPUT_CHARS,
        false,
    )?;
    invoke(Operation::PromptRescueEdit, &request).await
}

#[tauri::command]
pub async fn retry_prompt_rescue(request: PromptRescueCasRequest) -> Result<Value, DesktopError> {
    validate_prompt_rescue_cas(&request)?;
    invoke(Operation::PromptRescueRetry, &request).await
}

#[tauri::command]
pub async fn delete_prompt_rescue(
    app: AppHandle,
    request: PromptRescueCasRequest,
) -> Result<Value, DesktopError> {
    validate_prompt_rescue_cas(&request)?;
    tauri::async_runtime::spawn_blocking(move || delete_prompt_rescue_blocking(&app, request))
        .await
        .map_err(|_| {
            DesktopError::new(
                "BRIDGE_UNAVAILABLE",
                "The Prompt Rescue deletion worker stopped unexpectedly.",
            )
        })?
}

#[tauri::command]
pub async fn get_reply_rescue(request: ReplyRescueGetRequest) -> Result<Value, DesktopError> {
    validate_reply_rescue_job_id(&request.job_id)?;
    invoke(Operation::ReplyRescueGet, &request).await
}

#[tauri::command]
pub async fn queue_reply_rescue(request: ReplyRescueQueueRequest) -> Result<Value, DesktopError> {
    validate_reply_rescue_queue(&request)?;
    invoke(Operation::ReplyRescueQueue, &request).await
}

#[tauri::command]
pub async fn edit_reply_rescue(request: ReplyRescueEditRequest) -> Result<Value, DesktopError> {
    validate_reply_rescue_job_id(&request.job_id)?;
    validate_reply_rescue_version(request.expected_version)?;
    validate_multiline_text(&request.reply_body, MAX_REPLY_RESCUE_OUTPUT_CHARS, false)?;
    invoke(Operation::ReplyRescueEdit, &request).await
}

#[tauri::command]
pub async fn retry_reply_rescue(request: ReplyRescueCasRequest) -> Result<Value, DesktopError> {
    validate_reply_rescue_cas(&request)?;
    invoke(Operation::ReplyRescueRetry, &request).await
}

#[tauri::command]
pub async fn delete_reply_rescue(
    app: AppHandle,
    request: ReplyRescueCasRequest,
) -> Result<Value, DesktopError> {
    validate_reply_rescue_cas(&request)?;
    tauri::async_runtime::spawn_blocking(move || delete_reply_rescue_blocking(&app, request))
        .await
        .map_err(|_| {
            DesktopError::new(
                "BRIDGE_UNAVAILABLE",
                "The Reply Rescue deletion worker stopped unexpectedly.",
            )
        })?
}

#[tauri::command]
pub async fn get_resume_rescue_state(
    request: ResumeRescueStateRequest,
) -> Result<Value, DesktopError> {
    validate_resume_state(&request)?;
    invoke(Operation::ResumeRescueState, &request).await
}

#[tauri::command]
pub async fn save_resume_rescue_profile(
    request: ResumeSaveProfileRequest,
) -> Result<Value, DesktopError> {
    validate_resume_profile(&request)?;
    invoke(Operation::ResumeRescueSaveProfile, &request).await
}

#[tauri::command]
pub async fn save_resume_rescue_opportunity(
    request: ResumeSaveOpportunityRequest,
) -> Result<Value, DesktopError> {
    validate_resume_opportunity(
        &request.employer,
        &request.title,
        &request.source_text,
        &request.source_url,
        &request.priorities,
        &request.locale,
        request.captured_at.as_deref(),
    )?;
    invoke(Operation::ResumeRescueSaveOpportunity, &request).await
}

#[tauri::command]
pub async fn replace_resume_rescue_opportunity(
    request: ResumeReplaceOpportunityRequest,
) -> Result<Value, DesktopError> {
    validate_resume_identifier(&request.opportunity_id)?;
    validate_digest(&request.expected_digest)?;
    validate_resume_opportunity(
        &request.employer,
        &request.title,
        &request.source_text,
        &request.source_url,
        &request.priorities,
        &request.locale,
        request.captured_at.as_deref(),
    )?;
    invoke(Operation::ResumeRescueReplaceOpportunity, &request).await
}

#[tauri::command]
pub async fn compose_resume_rescue_exact(
    request: ResumeComposeExactRequest,
) -> Result<Value, DesktopError> {
    validate_resume_compose(&request)?;
    invoke(Operation::ResumeRescueComposeExact, &request).await
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

fn delete_prompt_rescue_blocking(
    app: &AppHandle,
    request: PromptRescueCasRequest,
) -> Result<Value, DesktopError> {
    let mut dialog = MessageDialog::new()
        .set_description(
            "This permanently deletes the local rough input and prepared prompt. It cannot be undone, and it does not change text in any other app.",
        )
        .set_title("Delete this Prompt Rescue job?")
        .set_level(MessageLevel::Warning)
        .set_buttons(MessageButtons::OkCancelCustom(
            "Delete Permanently".to_owned(),
            "Cancel".to_owned(),
        ));
    if let Some(window) = app.get_webview_window("main") {
        dialog = dialog.set_parent(&window);
    }
    let confirmed = match dialog.show() {
        MessageDialogResult::Ok | MessageDialogResult::Yes => true,
        MessageDialogResult::Custom(label) => label == "Delete Permanently",
        _ => false,
    };
    if !confirmed {
        return Err(DesktopError::new(
            "USER_CANCELLED",
            "Prompt Rescue deletion was cancelled.",
        ));
    }
    let params = checked_value(&request)?;
    bridge::call_blocking(Operation::PromptRescueDelete, params)
}

fn delete_reply_rescue_blocking(
    app: &AppHandle,
    request: ReplyRescueCasRequest,
) -> Result<Value, DesktopError> {
    let mut dialog = MessageDialog::new()
        .set_description(
            "This permanently deletes the local conversation source and prepared reply. It cannot be undone, and it does not change or send anything in another app.",
        )
        .set_title("Delete this Reply Rescue job?")
        .set_level(MessageLevel::Warning)
        .set_buttons(MessageButtons::OkCancelCustom(
            "Delete Permanently".to_owned(),
            "Cancel".to_owned(),
        ));
    if let Some(window) = app.get_webview_window("main") {
        dialog = dialog.set_parent(&window);
    }
    let confirmed = match dialog.show() {
        MessageDialogResult::Ok | MessageDialogResult::Yes => true,
        MessageDialogResult::Custom(label) => label == "Delete Permanently",
        _ => false,
    };
    if !confirmed {
        return Err(DesktopError::new(
            "USER_CANCELLED",
            "Reply Rescue deletion was cancelled.",
        ));
    }
    let params = checked_value(&request)?;
    bridge::call_blocking(Operation::ReplyRescueDelete, params)
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
        || request
            .suggestion_limit
            .is_some_and(|limit| limit > MAX_SUGGESTION_ITEMS)
        || request
            .prompt_rescue_limit
            .is_some_and(|limit| limit > MAX_PROMPT_RESCUE_ITEMS)
        || request
            .reply_rescue_limit
            .is_some_and(|limit| limit > MAX_REPLY_RESCUE_ITEMS)
    {
        return Err(DesktopError::invalid_request(
            "A snapshot limit exceeds the allowed maximum.",
        ));
    }
    Ok(())
}

fn validate_prompt_rescue_queue(request: &PromptRescueQueueRequest) -> Result<(), DesktopError> {
    validate_multiline_text(&request.rough_prompt, MAX_PROMPT_RESCUE_INPUT_CHARS, false)?;
    for value in [&request.target, &request.audience, &request.desired_format] {
        validate_multiline_text(value, MAX_PROMPT_RESCUE_CONTEXT_CHARS, true)?;
    }
    if request.constraints.len() > MAX_PROMPT_RESCUE_CONSTRAINTS {
        return Err(DesktopError::invalid_request(
            "The Prompt Rescue request has too many constraints.",
        ));
    }
    for constraint in &request.constraints {
        validate_multiline_text(constraint, MAX_PROMPT_RESCUE_CONTEXT_CHARS, false)?;
    }
    let declared_chars = request.rough_prompt.chars().count()
        + request.target.chars().count()
        + request.audience.chars().count()
        + request.desired_format.chars().count()
        + request
            .constraints
            .iter()
            .map(|value| value.chars().count())
            .sum::<usize>();
    if declared_chars > MAX_PROMPT_RESCUE_INPUT_CHARS {
        return Err(DesktopError::invalid_request(
            "The Prompt Rescue input exceeds the allowed size.",
        ));
    }
    Ok(())
}

fn validate_prompt_rescue_cas(request: &PromptRescueCasRequest) -> Result<(), DesktopError> {
    validate_prompt_rescue_job_id(&request.job_id)?;
    validate_prompt_rescue_version(request.expected_version)
}

fn validate_prompt_rescue_job_id(value: &str) -> Result<(), DesktopError> {
    validate_bounded_text(value, MAX_CANDIDATE_ID_CHARS, false)
}

fn validate_prompt_rescue_version(value: u64) -> Result<(), DesktopError> {
    if value == 0 || value > 2_147_483_647 {
        return Err(DesktopError::invalid_request(
            "The Prompt Rescue version is invalid.",
        ));
    }
    Ok(())
}

fn validate_reply_rescue_queue(request: &ReplyRescueQueueRequest) -> Result<(), DesktopError> {
    validate_multiline_text(
        &request.conversation_text,
        MAX_REPLY_RESCUE_INPUT_CHARS,
        false,
    )?;
    for value in [&request.goal, &request.tone] {
        validate_multiline_text(value, MAX_REPLY_RESCUE_FIELD_CHARS, true)?;
    }
    if !matches!(
        request.reply_mode.as_str(),
        "reply" | "reply_all" | "unspecified"
    ) {
        return Err(DesktopError::invalid_request(
            "The Reply Rescue reply mode is invalid.",
        ));
    }
    let groups = [
        (
            &request.participants,
            MAX_REPLY_RESCUE_PARTICIPANTS,
            "participants",
        ),
        (
            &request.intended_recipients,
            MAX_REPLY_RESCUE_PARTICIPANTS,
            "recipients",
        ),
        (
            &request.style_instructions,
            MAX_REPLY_RESCUE_DIRECTIONS,
            "style instructions",
        ),
        (
            &request.commitments,
            MAX_REPLY_RESCUE_DIRECTIONS,
            "commitments",
        ),
    ];
    for (values, maximum, _label) in groups {
        if values.len() > maximum {
            return Err(DesktopError::invalid_request(
                "The Reply Rescue request has too many list values.",
            ));
        }
        for value in values {
            validate_multiline_text(value, MAX_REPLY_RESCUE_FIELD_CHARS, false)?;
        }
    }
    let declared_chars = request.conversation_text.chars().count()
        + request.goal.chars().count()
        + request.tone.chars().count()
        + request
            .participants
            .iter()
            .chain(&request.intended_recipients)
            .chain(&request.style_instructions)
            .chain(&request.commitments)
            .map(|value| value.chars().count())
            .sum::<usize>();
    if declared_chars > MAX_REPLY_RESCUE_INPUT_CHARS {
        return Err(DesktopError::invalid_request(
            "The Reply Rescue input exceeds the allowed size.",
        ));
    }
    Ok(())
}

fn validate_reply_rescue_cas(request: &ReplyRescueCasRequest) -> Result<(), DesktopError> {
    validate_reply_rescue_job_id(&request.job_id)?;
    validate_reply_rescue_version(request.expected_version)
}

fn validate_reply_rescue_job_id(value: &str) -> Result<(), DesktopError> {
    validate_bounded_text(value, MAX_CANDIDATE_ID_CHARS, false)
}

fn validate_reply_rescue_version(value: u64) -> Result<(), DesktopError> {
    if value == 0 || value > 2_147_483_647 {
        return Err(DesktopError::invalid_request(
            "The Reply Rescue version is invalid.",
        ));
    }
    Ok(())
}

fn validate_resume_state(request: &ResumeRescueStateRequest) -> Result<(), DesktopError> {
    if [
        request.profile_limit,
        request.opportunity_limit,
        request.projection_limit,
    ]
    .into_iter()
    .flatten()
    .any(|limit| limit == 0 || limit > MAX_RESUME_ITEMS)
    {
        return Err(DesktopError::invalid_request(
            "A Résumé Rescue state limit is invalid.",
        ));
    }
    Ok(())
}

fn validate_resume_profile(request: &ResumeSaveProfileRequest) -> Result<(), DesktopError> {
    validate_resume_identifier(&request.profile_id)?;
    validate_multiline_text(&request.display_name, 512, false)?;
    validate_bounded_text(&request.locale, 64, true)?;
    if request
        .expected_version
        .is_some_and(|version| version == 0 || version > 2_147_483_647)
    {
        return Err(DesktopError::invalid_request(
            "The Résumé Rescue profile version is invalid.",
        ));
    }
    if request.facts.len() > MAX_RESUME_FACTS || request.conflicts.len() > MAX_RESUME_CONFLICTS {
        return Err(DesktopError::invalid_request(
            "The Résumé Rescue profile has too many records.",
        ));
    }
    let mut fact_ids = HashSet::new();
    for fact in &request.facts {
        validate_resume_identifier(&fact.id)?;
        if !fact_ids.insert(fact.id.as_str()) || !valid_resume_section(&fact.section) {
            return Err(DesktopError::invalid_request(
                "A Résumé Rescue fact identity or section is invalid.",
            ));
        }
        validate_multiline_text(&fact.text, MAX_RESUME_FACT_TEXT_CHARS, false)?;
        if !matches!(
            fact.confidentiality.as_str(),
            "public" | "private" | "confidential"
        ) || !matches!(
            fact.ownership_scope.as_str(),
            "individual" | "shared" | "organization" | "unspecified"
        ) || fact.provenance.is_empty()
            || fact.provenance.len() > 20
        {
            return Err(DesktopError::invalid_request(
                "A Résumé Rescue fact policy or provenance is invalid.",
            ));
        }
        for source in &fact.provenance {
            validate_resume_provenance(source)?;
        }
    }
    let mut conflict_ids = HashSet::new();
    for conflict in &request.conflicts {
        validate_resume_identifier(&conflict.id)?;
        validate_multiline_text(&conflict.description, 2_000, false)?;
        let mut members = HashSet::new();
        if !conflict_ids.insert(conflict.id.as_str())
            || conflict.fact_ids.len() < 2
            || conflict.fact_ids.len() > 50
        {
            return Err(DesktopError::invalid_request(
                "A Résumé Rescue conflict is invalid.",
            ));
        }
        for fact_id in &conflict.fact_ids {
            validate_resume_identifier(fact_id)?;
            if !fact_ids.contains(fact_id.as_str()) || !members.insert(fact_id.as_str()) {
                return Err(DesktopError::invalid_request(
                    "A Résumé Rescue conflict references an invalid fact.",
                ));
            }
        }
    }
    Ok(())
}

fn validate_resume_provenance(source: &ResumeProvenanceRequest) -> Result<(), DesktopError> {
    match source {
        ResumeProvenanceRequest::ManualReviewed { reviewed_at } => {
            validate_bounded_text(reviewed_at, 100, false)
        }
        ResumeProvenanceRequest::DocumentExcerpt {
            reviewed_at,
            source_id,
            source_digest,
            page,
            section,
            start,
            end,
            extraction_method,
        } => {
            validate_bounded_text(reviewed_at, 100, false)?;
            validate_resume_identifier(source_id)?;
            validate_resume_digest(source_digest)?;
            validate_multiline_text(section, 512, true)?;
            validate_bounded_text(extraction_method, 128, false)?;
            if *page > 100_000 || *start >= *end || *end > 10_000_000 {
                return Err(DesktopError::invalid_request(
                    "A Résumé Rescue document span is invalid.",
                ));
            }
            Ok(())
        }
        ResumeProvenanceRequest::ReviewedMemory {
            reviewed_at,
            memory_id,
            memory_path,
            memory_digest,
        } => {
            validate_bounded_text(reviewed_at, 100, false)?;
            validate_resume_identifier(memory_id)?;
            validate_multiline_text(memory_path, 1_024, false)?;
            validate_resume_digest(memory_digest)
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn validate_resume_opportunity(
    employer: &str,
    title: &str,
    source_text: &str,
    source_url: &str,
    priorities: &[String],
    locale: &str,
    captured_at: Option<&str>,
) -> Result<(), DesktopError> {
    validate_multiline_text(employer, 512, false)?;
    validate_multiline_text(title, 512, false)?;
    validate_multiline_text(source_text, MAX_RESUME_OPPORTUNITY_CHARS, false)?;
    validate_bounded_text(source_url, 4_096, true)?;
    validate_bounded_text(locale, 64, true)?;
    if priorities.len() > MAX_RESUME_PRIORITIES {
        return Err(DesktopError::invalid_request(
            "The Résumé Rescue opportunity has too many priorities.",
        ));
    }
    for priority in priorities {
        validate_multiline_text(priority, MAX_RESUME_PRIORITY_CHARS, false)?;
    }
    if let Some(timestamp) = captured_at {
        validate_bounded_text(timestamp, 100, false)?;
    }
    Ok(())
}

fn validate_resume_compose(request: &ResumeComposeExactRequest) -> Result<(), DesktopError> {
    validate_resume_identifier(&request.profile_id)?;
    validate_resume_identifier(&request.opportunity_id)?;
    if request.sections.len() > 8 || request.requirements.len() > MAX_RESUME_REQUIREMENTS {
        return Err(DesktopError::invalid_request(
            "The Résumé Rescue projection has too many records.",
        ));
    }
    let mut sections = HashSet::new();
    let mut selected = HashSet::new();
    for section in &request.sections {
        if !valid_resume_section(&section.kind)
            || !sections.insert(section.kind.as_str())
            || section.fact_ids.len() > MAX_RESUME_FACTS
        {
            return Err(DesktopError::invalid_request(
                "A Résumé Rescue projection section is invalid.",
            ));
        }
        for fact_id in &section.fact_ids {
            validate_resume_identifier(fact_id)?;
            if !selected.insert(fact_id.as_str()) {
                return Err(DesktopError::invalid_request(
                    "A Résumé Rescue fact may only be selected once.",
                ));
            }
        }
    }
    let mut requirements = HashSet::new();
    for requirement in &request.requirements {
        validate_resume_identifier(&requirement.id)?;
        validate_multiline_text(&requirement.text, MAX_RESUME_REQUIREMENT_CHARS, false)?;
        if !requirements.insert(requirement.id.as_str()) || requirement.fact_ids.len() > 50 {
            return Err(DesktopError::invalid_request(
                "A Résumé Rescue requirement is invalid.",
            ));
        }
        let mut mapped = HashSet::new();
        for fact_id in &requirement.fact_ids {
            validate_resume_identifier(fact_id)?;
            if !selected.contains(fact_id.as_str()) || !mapped.insert(fact_id.as_str()) {
                return Err(DesktopError::invalid_request(
                    "A Résumé Rescue requirement maps an invalid fact.",
                ));
            }
        }
    }
    Ok(())
}

fn valid_resume_section(value: &str) -> bool {
    matches!(
        value,
        "summary"
            | "experience"
            | "education"
            | "skill"
            | "project"
            | "certification"
            | "language"
            | "other"
    )
}

fn validate_resume_identifier(value: &str) -> Result<(), DesktopError> {
    let mut bytes = value.bytes();
    if value.len() > 128
        || !bytes
            .next()
            .is_some_and(|byte| byte.is_ascii_alphanumeric())
        || !bytes
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
    {
        return Err(DesktopError::invalid_request(
            "A Résumé Rescue identifier is invalid.",
        ));
    }
    Ok(())
}

fn validate_resume_digest(value: &str) -> Result<(), DesktopError> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
    {
        return Err(DesktopError::invalid_request(
            "A Résumé Rescue digest is invalid.",
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
            suggestion_limit: Some(MAX_SUGGESTION_ITEMS),
            prompt_rescue_limit: Some(MAX_PROMPT_RESCUE_ITEMS),
            reply_rescue_limit: Some(MAX_REPLY_RESCUE_ITEMS),
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
    fn prompt_rescue_queue_is_bounded_and_preserves_multiline_input() {
        let request = PromptRescueQueueRequest {
            rough_prompt: "Draft a plan\nwith exact checks.".to_owned(),
            target: "Engineering".to_owned(),
            audience: "Reviewers".to_owned(),
            constraints: vec!["Use supplied facts only".to_owned()],
            desired_format: "Markdown".to_owned(),
        };
        assert!(validate_prompt_rescue_queue(&request).is_ok());

        let oversized = PromptRescueQueueRequest {
            rough_prompt: "x".repeat(MAX_PROMPT_RESCUE_INPUT_CHARS),
            target: "extra".to_owned(),
            ..request
        };
        assert!(validate_prompt_rescue_queue(&oversized).is_err());
    }

    #[test]
    fn prompt_rescue_edit_and_cas_reject_unsupported_values() {
        assert!(validate_prompt_rescue_version(1).is_ok());
        assert!(validate_prompt_rescue_version(0).is_err());
        assert!(validate_prompt_rescue_version(2_147_483_648).is_err());
        assert!(validate_multiline_text("ready\nfor review", 30_000, false).is_ok());
        assert!(validate_multiline_text("hidden\0value", 30_000, false).is_err());
    }

    #[test]
    fn reply_rescue_queue_and_cas_are_bounded() {
        let request = ReplyRescueQueueRequest {
            conversation_text: "Ana: Can you meet Tuesday at 10?".to_owned(),
            participants: vec!["Ana".to_owned(), "Me".to_owned()],
            intended_recipients: vec!["Ana".to_owned()],
            reply_mode: "reply".to_owned(),
            goal: "Confirm the time".to_owned(),
            tone: "Warm".to_owned(),
            style_instructions: vec!["Use a greeting".to_owned()],
            commitments: vec!["Tuesday at 10 works".to_owned()],
        };
        assert!(validate_reply_rescue_queue(&request).is_ok());
        let unsupported_mode = ReplyRescueQueueRequest {
            reply_mode: "send_all".to_owned(),
            ..request
        };
        assert!(validate_reply_rescue_queue(&unsupported_mode).is_err());
        assert!(validate_reply_rescue_version(1).is_ok());
        assert!(validate_reply_rescue_version(0).is_err());
    }

    #[test]
    fn resume_profile_requires_closed_reviewed_sources_and_consistent_conflicts() {
        let fact = ResumeFactRequest {
            id: "fact-api".to_owned(),
            section: "experience".to_owned(),
            text: "Reduced API p95 latency by 40%.".to_owned(),
            confidentiality: "private".to_owned(),
            ownership_scope: "shared".to_owned(),
            provenance: vec![ResumeProvenanceRequest::ManualReviewed {
                reviewed_at: "2026-08-09T00:00:00Z".to_owned(),
            }],
        };
        let valid = ResumeSaveProfileRequest {
            profile_id: "primary-profile".to_owned(),
            display_name: "Ada Example".to_owned(),
            locale: "en-US".to_owned(),
            facts: vec![fact],
            conflicts: vec![],
            expected_version: None,
        };
        assert!(validate_resume_profile(&valid).is_ok());

        let conflicting = ResumeSaveProfileRequest {
            conflicts: vec![ResumeConflictRequest {
                id: "conflict-1".to_owned(),
                fact_ids: vec!["fact-api".to_owned(), "missing".to_owned()],
                description: "Review the competing claims.".to_owned(),
            }],
            ..valid
        };
        assert!(validate_resume_profile(&conflicting).is_err());
    }

    #[test]
    fn resume_exact_projection_rejects_duplicate_and_unselected_mappings() {
        let valid = ResumeComposeExactRequest {
            profile_id: "primary-profile".to_owned(),
            opportunity_id: "opportunity-1".to_owned(),
            sections: vec![ResumeProjectionSectionRequest {
                kind: "experience".to_owned(),
                fact_ids: vec!["fact-api".to_owned()],
            }],
            requirements: vec![ResumeRequirementRequest {
                id: "req-latency".to_owned(),
                text: "Improve service latency.".to_owned(),
                fact_ids: vec!["fact-api".to_owned()],
            }],
        };
        assert!(validate_resume_compose(&valid).is_ok());

        let invalid = ResumeComposeExactRequest {
            requirements: vec![ResumeRequirementRequest {
                id: "req-latency".to_owned(),
                text: "Improve service latency.".to_owned(),
                fact_ids: vec!["fact-missing".to_owned()],
            }],
            ..valid
        };
        assert!(validate_resume_compose(&invalid).is_err());
    }

    #[test]
    fn resume_state_and_opportunity_bounds_match_the_python_bridge() {
        assert!(validate_resume_state(&ResumeRescueStateRequest {
            profile_limit: Some(50),
            opportunity_limit: Some(50),
            projection_limit: Some(50),
        })
        .is_ok());
        assert!(validate_resume_state(&ResumeRescueStateRequest {
            profile_limit: Some(0),
            ..ResumeRescueStateRequest::default()
        })
        .is_err());
        assert!(validate_resume_opportunity(
            "Example Labs",
            "Reliability Engineer",
            "Improve service latency.",
            "https://example.test/jobs/123",
            &["Prefer measured evidence.".to_owned()],
            "en-US",
            Some("2026-08-09T01:00:00Z"),
        )
        .is_ok());
        assert!(validate_resume_opportunity(
            "Example Labs",
            "Reliability Engineer",
            "Improve service latency.\0hidden",
            "",
            &[],
            "",
            None,
        )
        .is_err());
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
