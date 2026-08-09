use crate::bridge::{self, Operation, MAX_REQUEST_BYTES};
use crate::error::DesktopError;
use base64::Engine;
use quick_xml::events::{BytesStart, Event};
use quick_xml::Reader as XmlReader;
use rfd::{FileDialog, MessageButtons, MessageDialog, MessageDialogResult, MessageLevel};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fs::OpenOptions;
use std::io::{Cursor, ErrorKind, Read, Write};
use std::path::{Component, Path};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Manager, State};
use uuid::Uuid;
use zip::CompressionMethod;

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
const MAX_JSON_RESUME_SOURCE_BYTES: usize = 500_000;
const MAX_JSON_RESUME_CANDIDATES: usize = 2_000;
const MAX_RESUME_DOCUMENT_BYTES: usize = 8 * 1024 * 1024;
const MAX_RESUME_DOCX_EXPORT_BYTES: usize = 4 * 1024 * 1024;
const MAX_RESUME_PDF_EXPORT_BYTES: usize = 10 * 1024 * 1024;
const MAX_RESUME_DOCUMENT_VAULT_ITEMS: usize = 4;
const MAX_RESUME_DOCUMENT_VAULT_BYTES: usize = 32 * 1024 * 1024;
const RESUME_DOCUMENT_VAULT_TTL: Duration = Duration::from_secs(30 * 60);
const MAX_PROVENANCE_DEPTH: u8 = 8;

#[derive(Clone, Default)]
pub(crate) struct ResumeDocumentVault {
    entries: Arc<Mutex<HashMap<String, ResumeDocumentVaultEntry>>>,
}

#[derive(Clone)]
struct ResumeDocumentVaultEntry {
    source: Vec<u8>,
    source_format: String,
    source_digest: String,
    review_digest: String,
    created_at: Instant,
    in_use: bool,
}

impl ResumeDocumentVault {
    fn insert(
        &self,
        source: Vec<u8>,
        source_format: String,
        source_digest: String,
        review_digest: String,
    ) -> Result<String, DesktopError> {
        let mut entries = self.entries.lock().map_err(|_| {
            DesktopError::new(
                "DOCUMENT_REVIEW_UNAVAILABLE",
                "The local document review vault is unavailable.",
            )
        })?;
        purge_expired_document_entries(&mut entries);
        let retained_bytes = entries
            .values()
            .map(|entry| entry.source.len())
            .sum::<usize>();
        if entries.len() >= MAX_RESUME_DOCUMENT_VAULT_ITEMS
            || retained_bytes.saturating_add(source.len()) > MAX_RESUME_DOCUMENT_VAULT_BYTES
        {
            return Err(DesktopError::new(
                "DOCUMENT_REVIEW_LIMIT",
                "Too many document reviews are open; finish one before opening another.",
            ));
        }
        let token = Uuid::new_v4().simple().to_string();
        entries.insert(
            token.clone(),
            ResumeDocumentVaultEntry {
                source,
                source_format,
                source_digest,
                review_digest,
                created_at: Instant::now(),
                in_use: false,
            },
        );
        Ok(token)
    }

    fn begin(
        &self,
        token: &str,
        expected_review_digest: &str,
    ) -> Result<ResumeDocumentVaultEntry, DesktopError> {
        let mut entries = self.entries.lock().map_err(|_| {
            DesktopError::new(
                "DOCUMENT_REVIEW_UNAVAILABLE",
                "The local document review vault is unavailable.",
            )
        })?;
        purge_expired_document_entries(&mut entries);
        let entry = entries.get_mut(token).ok_or_else(|| {
            DesktopError::new(
                "DOCUMENT_REVIEW_EXPIRED",
                "The document review expired; choose the source again.",
            )
        })?;
        if entry.in_use
            || !constant_time_equal(
                entry.review_digest.as_bytes(),
                expected_review_digest.as_bytes(),
            )
        {
            return Err(DesktopError::new(
                "DOCUMENT_REVIEW_CHANGED",
                "The document review changed; review the source again.",
            ));
        }
        entry.in_use = true;
        Ok(entry.clone())
    }

    fn finish(&self, token: &str, source_digest: &str, consumed: bool) {
        let Ok(mut entries) = self.entries.lock() else {
            return;
        };
        let matches = entries.get(token).is_some_and(|entry| {
            constant_time_equal(entry.source_digest.as_bytes(), source_digest.as_bytes())
        });
        if !matches {
            return;
        }
        if consumed {
            entries.remove(token);
        } else if let Some(entry) = entries.get_mut(token) {
            entry.in_use = false;
        }
    }

    fn discard(&self, token: &str, expected_review_digest: &str) -> Result<bool, DesktopError> {
        let mut entries = self.entries.lock().map_err(|_| {
            DesktopError::new(
                "DOCUMENT_REVIEW_UNAVAILABLE",
                "The local document review vault is unavailable.",
            )
        })?;
        purge_expired_document_entries(&mut entries);
        let Some(entry) = entries.get(token) else {
            return Ok(false);
        };
        if entry.in_use
            || !constant_time_equal(
                entry.review_digest.as_bytes(),
                expected_review_digest.as_bytes(),
            )
        {
            return Err(DesktopError::new(
                "DOCUMENT_REVIEW_CHANGED",
                "The document review changed; review the source again.",
            ));
        }
        entries.remove(token);
        Ok(true)
    }
}

fn purge_expired_document_entries(entries: &mut HashMap<String, ResumeDocumentVaultEntry>) {
    entries.retain(|_, entry| entry.created_at.elapsed() <= RESUME_DOCUMENT_VAULT_TTL);
}

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
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rewrite_limit: Option<usize>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rewrite_version_limit: Option<usize>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeRewriteQueueRequest {
    pub projection_id: String,
    pub expected_artifact_digest: String,
    pub expected_model_identity: String,
    pub expected_provider_location: String,
    pub remote_egress_authorized: bool,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeRewriteCasRequest {
    pub job_id: String,
    pub expected_version: u64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeRewriteDecisionRequest {
    pub job_id: String,
    pub proposal_id: String,
    pub expected_proposal_digest: String,
    pub expected_job_version: u64,
    pub expected_head_id: String,
    pub expected_artifact_digest: String,
    pub decision: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeRewriteRestoreRequest {
    pub target_version_id: String,
    pub expected_head_id: String,
    pub expected_artifact_digest: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeRewriteVersionRequest {
    pub version_id: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeRewriteExportJsonRequest {
    pub version_id: String,
    pub expected_document_digest: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeRewriteNativeExportRequest {
    pub version_id: String,
    pub expected_artifact_digest: String,
    pub expected_preview_document_digest: String,
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
    JsonResumeField {
        reviewed_at: String,
        source_id: String,
        source_digest: String,
        json_pointer: String,
        value_digest: String,
        mapping: String,
        upstream_schema_version: String,
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

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumePreviewRequest {
    pub projection_id: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeExportHtmlRequest {
    pub projection_id: String,
    pub expected_document_digest: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeJsonSelectionRequest {
    pub candidate_id: String,
    pub fact_id: String,
    pub section: String,
    pub confidentiality: String,
    pub ownership_scope: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeAdmitJsonRequest {
    pub source_text: String,
    pub expected_review_digest: String,
    pub profile_id: String,
    pub display_name: String,
    pub locale: String,
    pub selections: Vec<ResumeJsonSelectionRequest>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expected_version: Option<u64>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeAdmitDocumentRequest {
    pub review_token: String,
    pub expected_review_digest: String,
    pub profile_id: String,
    pub display_name: String,
    pub locale: String,
    pub selections: Vec<ResumeJsonSelectionRequest>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expected_version: Option<u64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeDiscardDocumentRequest {
    pub review_token: String,
    pub expected_review_digest: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeExportJsonRequest {
    pub projection_id: String,
    pub expected_document_digest: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeExportDocxRequest {
    pub projection_id: String,
    pub expected_artifact_digest: String,
    pub expected_preview_document_digest: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResumeExportPdfRequest {
    pub projection_id: String,
    pub expected_artifact_digest: String,
    pub expected_preview_document_digest: String,
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

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ResumePreviewResponse {
    preview: ResumePreviewPayload,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ResumePreviewPayload {
    schema_version: u64,
    projection_id: String,
    artifact_digest: String,
    renderer_version: u64,
    template_id: String,
    html: String,
    plain_text: String,
    document_digest: String,
    action_capability: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct JsonResumeUpstreamSchema {
    version: String,
    commit: String,
    url: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct JsonResumeSourceBinding {
    id: String,
    digest: String,
    byte_count: u64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct JsonResumeImportReview {
    schema_version: u64,
    format: String,
    upstream_schema: JsonResumeUpstreamSchema,
    source: JsonResumeSourceBinding,
    display_name_candidate: String,
    candidates: Vec<Value>,
    omissions: Vec<Value>,
    unknown_fields: Vec<String>,
    warnings: Vec<String>,
    action_capability: String,
    review_digest: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct JsonResumeReviewResponse {
    review: JsonResumeImportReview,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ResumeDocumentExtractorBinding {
    version: u64,
    method: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ResumeDocumentImportReview {
    schema_version: u64,
    format: String,
    extractor: ResumeDocumentExtractorBinding,
    source: JsonResumeSourceBinding,
    candidates: Vec<Value>,
    omissions: Vec<Value>,
    warnings: Vec<Value>,
    action_capability: String,
    review_digest: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ResumeDocumentReviewResponse {
    review: ResumeDocumentImportReview,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ResumeNativeExportPayload {
    schema_version: u64,
    projection_id: String,
    artifact_digest: String,
    preview_document_digest: String,
    renderer_version: u64,
    native_export_version: u64,
    template_id: String,
    format: String,
    media_type: String,
    extension: String,
    byte_count: u64,
    content_digest: String,
    action_capability: String,
    content_base64: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ResumeNativeExportResponse {
    export: ResumeNativeExportPayload,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct JsonResumeProjectionBinding {
    id: String,
    artifact_digest: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct JsonResumeProfileBinding {
    id: String,
    version: u64,
    digest: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct JsonResumeExportPayload {
    schema_version: u64,
    format: String,
    upstream_schema: JsonResumeUpstreamSchema,
    projection_binding: JsonResumeProjectionBinding,
    profile_binding: JsonResumeProfileBinding,
    document: Value,
    json_text: String,
    document_digest: String,
    interoperability_losses: Vec<Value>,
    warnings: Vec<String>,
    action_capability: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct JsonResumeExportResponse {
    export: JsonResumeExportPayload,
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
pub async fn queue_resume_rescue_rewrite(
    request: ResumeRewriteQueueRequest,
) -> Result<Value, DesktopError> {
    validate_resume_identifier(&request.projection_id)?;
    validate_resume_digest(&request.expected_artifact_digest)?;
    validate_multiline_text(&request.expected_model_identity, 256, false)?;
    if !matches!(
        request.expected_provider_location.as_str(),
        "local" | "remote_or_unknown"
    ) {
        return Err(DesktopError::invalid_request(
            "The Résumé Rescue provider location is invalid.",
        ));
    }
    invoke(Operation::ResumeRescueQueueRewrite, &request).await
}

#[tauri::command]
pub async fn retry_resume_rescue_rewrite(
    request: ResumeRewriteCasRequest,
) -> Result<Value, DesktopError> {
    validate_resume_rewrite_cas(&request)?;
    invoke(Operation::ResumeRescueRetryRewrite, &request).await
}

#[tauri::command]
pub async fn delete_resume_rescue_rewrite(
    request: ResumeRewriteCasRequest,
) -> Result<Value, DesktopError> {
    validate_resume_rewrite_cas(&request)?;
    invoke(Operation::ResumeRescueDeleteRewrite, &request).await
}

#[tauri::command]
pub async fn decide_resume_rescue_rewrite(
    request: ResumeRewriteDecisionRequest,
) -> Result<Value, DesktopError> {
    validate_resume_rewrite_decision(&request)?;
    invoke(Operation::ResumeRescueDecideRewrite, &request).await
}

#[tauri::command]
pub async fn restore_resume_rescue_rewrite(
    request: ResumeRewriteRestoreRequest,
) -> Result<Value, DesktopError> {
    validate_resume_identifier(&request.target_version_id)?;
    validate_resume_identifier(&request.expected_head_id)?;
    validate_resume_digest(&request.expected_artifact_digest)?;
    invoke(Operation::ResumeRescueRestoreRewrite, &request).await
}

#[tauri::command]
pub async fn get_resume_rescue_rewrite_preview(
    request: ResumeRewriteVersionRequest,
) -> Result<Value, DesktopError> {
    validate_resume_identifier(&request.version_id)?;
    invoke(Operation::ResumeRescuePreviewRewrite, &request).await
}

#[tauri::command]
pub async fn get_resume_rescue_rewrite_json_export(
    request: ResumeRewriteVersionRequest,
) -> Result<Value, DesktopError> {
    validate_resume_identifier(&request.version_id)?;
    invoke(Operation::ResumeRescueExportRewriteJson, &request).await
}

#[tauri::command]
pub async fn export_resume_rescue_rewrite_json(
    app: AppHandle,
    request: ResumeRewriteExportJsonRequest,
) -> Result<Value, DesktopError> {
    validate_resume_identifier(&request.version_id)?;
    validate_resume_digest(&request.expected_document_digest)?;
    let export_request = ResumeExportJsonRequest {
        projection_id: request.version_id,
        expected_document_digest: request.expected_document_digest,
    };
    tauri::async_runtime::spawn_blocking(move || {
        export_resume_rewrite_json_blocking(&app, export_request)
    })
    .await
    .map_err(|_| {
        DesktopError::new(
            "BRIDGE_UNAVAILABLE",
            "The reviewed résumé JSON export worker stopped unexpectedly.",
        )
    })?
}

#[tauri::command]
pub async fn export_resume_rescue_rewrite_docx(
    app: AppHandle,
    request: ResumeRewriteNativeExportRequest,
) -> Result<Value, DesktopError> {
    validate_resume_identifier(&request.version_id)?;
    validate_resume_digest(&request.expected_artifact_digest)?;
    validate_resume_digest(&request.expected_preview_document_digest)?;
    let export_request = ResumeExportDocxRequest {
        projection_id: request.version_id,
        expected_artifact_digest: request.expected_artifact_digest,
        expected_preview_document_digest: request.expected_preview_document_digest,
    };
    tauri::async_runtime::spawn_blocking(move || {
        export_resume_rewrite_docx_blocking(&app, export_request)
    })
    .await
    .map_err(|_| {
        DesktopError::new(
            "BRIDGE_UNAVAILABLE",
            "The reviewed résumé DOCX export worker stopped unexpectedly.",
        )
    })?
}

#[tauri::command]
pub async fn export_resume_rescue_rewrite_pdf(
    app: AppHandle,
    request: ResumeRewriteNativeExportRequest,
) -> Result<Value, DesktopError> {
    validate_resume_identifier(&request.version_id)?;
    validate_resume_digest(&request.expected_artifact_digest)?;
    validate_resume_digest(&request.expected_preview_document_digest)?;
    let export_request = ResumeExportPdfRequest {
        projection_id: request.version_id,
        expected_artifact_digest: request.expected_artifact_digest,
        expected_preview_document_digest: request.expected_preview_document_digest,
    };
    tauri::async_runtime::spawn_blocking(move || {
        export_resume_rewrite_pdf_blocking(&app, export_request)
    })
    .await
    .map_err(|_| {
        DesktopError::new(
            "BRIDGE_UNAVAILABLE",
            "The reviewed résumé PDF export worker stopped unexpectedly.",
        )
    })?
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
pub async fn get_resume_rescue_preview(
    request: ResumePreviewRequest,
) -> Result<Value, DesktopError> {
    validate_resume_identifier(&request.projection_id)?;
    invoke(Operation::ResumeRescuePreview, &request).await
}

#[tauri::command]
pub async fn export_resume_rescue_html(
    app: AppHandle,
    request: ResumeExportHtmlRequest,
) -> Result<Value, DesktopError> {
    validate_resume_identifier(&request.projection_id)?;
    validate_resume_digest(&request.expected_document_digest)?;
    tauri::async_runtime::spawn_blocking(move || export_resume_html_blocking(&app, request))
        .await
        .map_err(|_| {
            DesktopError::new(
                "BRIDGE_UNAVAILABLE",
                "The Résumé Rescue export worker stopped unexpectedly.",
            )
        })?
}

#[tauri::command]
pub async fn open_resume_rescue_json(app: AppHandle) -> Result<Value, DesktopError> {
    tauri::async_runtime::spawn_blocking(move || open_resume_json_blocking(&app))
        .await
        .map_err(|_| {
            DesktopError::new(
                "BRIDGE_UNAVAILABLE",
                "The JSON Resume review worker stopped unexpectedly.",
            )
        })?
}

#[tauri::command]
pub async fn admit_resume_rescue_json(
    request: ResumeAdmitJsonRequest,
) -> Result<Value, DesktopError> {
    validate_resume_json_admission(&request)?;
    invoke(Operation::ResumeRescueAdmitJson, &request).await
}

#[tauri::command]
pub async fn open_resume_rescue_document(
    app: AppHandle,
    vault: State<'_, ResumeDocumentVault>,
) -> Result<Value, DesktopError> {
    let vault = vault.inner().clone();
    tauri::async_runtime::spawn_blocking(move || open_resume_document_blocking(&app, &vault))
        .await
        .map_err(|_| {
            DesktopError::new(
                "BRIDGE_UNAVAILABLE",
                "The résumé document review worker stopped unexpectedly.",
            )
        })?
}

#[tauri::command]
pub async fn admit_resume_rescue_document(
    vault: State<'_, ResumeDocumentVault>,
    request: ResumeAdmitDocumentRequest,
) -> Result<Value, DesktopError> {
    validate_resume_document_admission(&request)?;
    let vault = vault.inner().clone();
    tauri::async_runtime::spawn_blocking(move || admit_resume_document_blocking(&vault, request))
        .await
        .map_err(|_| {
            DesktopError::new(
                "BRIDGE_UNAVAILABLE",
                "The résumé document admission worker stopped unexpectedly.",
            )
        })?
}

#[tauri::command]
pub fn discard_resume_rescue_document(
    vault: State<'_, ResumeDocumentVault>,
    request: ResumeDiscardDocumentRequest,
) -> Result<Value, DesktopError> {
    validate_resume_document_review_reference(
        &request.review_token,
        &request.expected_review_digest,
    )?;
    let discarded = vault.discard(&request.review_token, &request.expected_review_digest)?;
    Ok(serde_json::json!({
        "review_token": request.review_token,
        "discarded": discarded,
    }))
}

#[tauri::command]
pub async fn get_resume_rescue_json_export(
    request: ResumePreviewRequest,
) -> Result<Value, DesktopError> {
    validate_resume_identifier(&request.projection_id)?;
    invoke(Operation::ResumeRescueExportJson, &request).await
}

#[tauri::command]
pub async fn export_resume_rescue_json(
    app: AppHandle,
    request: ResumeExportJsonRequest,
) -> Result<Value, DesktopError> {
    validate_resume_identifier(&request.projection_id)?;
    validate_resume_digest(&request.expected_document_digest)?;
    tauri::async_runtime::spawn_blocking(move || export_resume_json_blocking(&app, request))
        .await
        .map_err(|_| {
            DesktopError::new(
                "BRIDGE_UNAVAILABLE",
                "The JSON Resume export worker stopped unexpectedly.",
            )
        })?
}

#[tauri::command]
pub async fn export_resume_rescue_docx(
    app: AppHandle,
    request: ResumeExportDocxRequest,
) -> Result<Value, DesktopError> {
    validate_resume_identifier(&request.projection_id)?;
    validate_resume_digest(&request.expected_artifact_digest)?;
    validate_resume_digest(&request.expected_preview_document_digest)?;
    tauri::async_runtime::spawn_blocking(move || export_resume_docx_blocking(&app, request))
        .await
        .map_err(|_| {
            DesktopError::new(
                "BRIDGE_UNAVAILABLE",
                "The Résumé Rescue DOCX export worker stopped unexpectedly.",
            )
        })?
}

#[tauri::command]
pub async fn export_resume_rescue_pdf(
    app: AppHandle,
    request: ResumeExportPdfRequest,
) -> Result<Value, DesktopError> {
    validate_resume_identifier(&request.projection_id)?;
    validate_resume_digest(&request.expected_artifact_digest)?;
    validate_resume_digest(&request.expected_preview_document_digest)?;
    tauri::async_runtime::spawn_blocking(move || export_resume_pdf_blocking(&app, request))
        .await
        .map_err(|_| {
            DesktopError::new(
                "BRIDGE_UNAVAILABLE",
                "The Résumé Rescue PDF export worker stopped unexpectedly.",
            )
        })?
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

fn export_resume_html_blocking(
    app: &AppHandle,
    request: ResumeExportHtmlRequest,
) -> Result<Value, DesktopError> {
    let params = serde_json::json!({"projection_id": request.projection_id});
    let value = bridge::call_blocking(Operation::ResumeRescuePreview, params)?;
    let response: ResumePreviewResponse = serde_json::from_value(value).map_err(|_| {
        DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The desktop bridge returned an invalid Résumé Rescue preview.",
        )
    })?;
    validate_resume_preview_for_export(&response.preview, &request)?;

    let default_name = format!(
        "resume-{}.html",
        request
            .projection_id
            .chars()
            .map(|character| {
                if character.is_ascii_alphanumeric() || matches!(character, '-' | '_' | '.') {
                    character
                } else {
                    '-'
                }
            })
            .collect::<String>()
    );
    let mut dialog = FileDialog::new()
        .add_filter("HTML document", &["html"])
        .set_can_create_directories(true)
        .set_file_name(default_name)
        .set_title("Export a new Résumé Rescue HTML file");
    if let Some(window) = app.get_webview_window("main") {
        dialog = dialog.set_parent(&window);
    }
    let path = dialog.save_file().ok_or_else(|| {
        DesktopError::new("USER_CANCELLED", "Résumé Rescue export was cancelled.")
    })?;
    validate_resume_export_path(&path)?;
    write_new_resume_export(&path, response.preview.html.as_bytes())?;
    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("resume.html");
    Ok(serde_json::json!({
        "schema_version": 1,
        "projection_id": request.projection_id,
        "document_digest": response.preview.document_digest,
        "file_name": file_name,
        "byte_count": response.preview.html.len(),
        "created": true,
        "action_capability": "none",
    }))
}

fn open_resume_json_blocking(app: &AppHandle) -> Result<Value, DesktopError> {
    let mut dialog = FileDialog::new()
        .add_filter("JSON Resume", &["json"])
        .set_title("Review a JSON Resume file");
    if let Some(window) = app.get_webview_window("main") {
        dialog = dialog.set_parent(&window);
    }
    let path = dialog
        .pick_file()
        .ok_or_else(|| DesktopError::new("USER_CANCELLED", "JSON Resume import was cancelled."))?;
    let source_text = read_json_resume_source(&path)?;
    let params = serde_json::json!({"source_text": source_text});
    let value = bridge::call_blocking(Operation::ResumeRescueReviewJson, params)?;
    let response: JsonResumeReviewResponse = serde_json::from_value(value).map_err(|_| {
        DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The desktop bridge returned an invalid JSON Resume review.",
        )
    })?;
    validate_json_resume_review(&response.review, &source_text)?;
    Ok(serde_json::json!({
        "source_text": source_text,
        "review": response.review,
    }))
}

fn open_resume_document_blocking(
    app: &AppHandle,
    vault: &ResumeDocumentVault,
) -> Result<Value, DesktopError> {
    let mut dialog = FileDialog::new()
        .add_filter("Résumé document", &["pdf", "docx"])
        .set_title("Review a PDF or DOCX résumé");
    if let Some(window) = app.get_webview_window("main") {
        dialog = dialog.set_parent(&window);
    }
    let path = dialog.pick_file().ok_or_else(|| {
        DesktopError::new("USER_CANCELLED", "Résumé document import was cancelled.")
    })?;
    let (source, source_format) = read_resume_document_source(&path)?;
    let source_digest = sha256_hex(&source);
    let params = serde_json::json!({
        "source_base64": base64::engine::general_purpose::STANDARD.encode(&source),
        "source_format": source_format,
    });
    let value = bridge::call_blocking(Operation::ResumeRescueReviewDocument, params)?;
    let response: ResumeDocumentReviewResponse = serde_json::from_value(value).map_err(|_| {
        DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The desktop bridge returned an invalid résumé document review.",
        )
    })?;
    validate_resume_document_review(
        &response.review,
        &source_format,
        &source_digest,
        source.len(),
    )?;
    let review_digest = response.review.review_digest.clone();
    let review_token = vault.insert(source, source_format, source_digest, review_digest)?;
    Ok(serde_json::json!({
        "review_token": review_token,
        "review": response.review,
    }))
}

fn admit_resume_document_blocking(
    vault: &ResumeDocumentVault,
    request: ResumeAdmitDocumentRequest,
) -> Result<Value, DesktopError> {
    let entry = vault.begin(&request.review_token, &request.expected_review_digest)?;
    let params = serde_json::json!({
        "source_base64": base64::engine::general_purpose::STANDARD.encode(&entry.source),
        "source_format": entry.source_format,
        "expected_review_digest": request.expected_review_digest,
        "profile_id": request.profile_id,
        "display_name": request.display_name,
        "locale": request.locale,
        "selections": request.selections,
        "expected_version": request.expected_version,
    });
    let result = bridge::call_blocking(Operation::ResumeRescueAdmitDocument, params);
    vault.finish(&request.review_token, &entry.source_digest, result.is_ok());
    result
}

fn export_resume_json_blocking(
    app: &AppHandle,
    request: ResumeExportJsonRequest,
) -> Result<Value, DesktopError> {
    export_resume_json_with_operation(app, request, Operation::ResumeRescueExportJson)
}

fn export_resume_rewrite_json_blocking(
    app: &AppHandle,
    request: ResumeExportJsonRequest,
) -> Result<Value, DesktopError> {
    export_resume_json_with_operation(app, request, Operation::ResumeRescueExportRewriteJson)
}

fn export_resume_json_with_operation(
    app: &AppHandle,
    request: ResumeExportJsonRequest,
    operation: Operation,
) -> Result<Value, DesktopError> {
    let params = if operation == Operation::ResumeRescueExportRewriteJson {
        serde_json::json!({"version_id": request.projection_id})
    } else {
        serde_json::json!({"projection_id": request.projection_id})
    };
    let value = bridge::call_blocking(operation, params)?;
    let response: JsonResumeExportResponse = serde_json::from_value(value).map_err(|_| {
        DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The desktop bridge returned an invalid JSON Resume export.",
        )
    })?;
    validate_json_resume_export(&response.export, &request)?;

    let default_name = format!(
        "resume-{}.json",
        request
            .projection_id
            .chars()
            .map(|character| {
                if character.is_ascii_alphanumeric() || matches!(character, '-' | '_' | '.') {
                    character
                } else {
                    '-'
                }
            })
            .collect::<String>()
    );
    let mut dialog = FileDialog::new()
        .add_filter("JSON Resume", &["json"])
        .set_can_create_directories(true)
        .set_file_name(default_name)
        .set_title("Export a new JSON Resume file");
    if let Some(window) = app.get_webview_window("main") {
        dialog = dialog.set_parent(&window);
    }
    let path = dialog
        .save_file()
        .ok_or_else(|| DesktopError::new("USER_CANCELLED", "JSON Resume export was cancelled."))?;
    validate_resume_json_path(&path)?;
    write_new_json_resume_export(&path, response.export.json_text.as_bytes())?;
    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("resume.json");
    Ok(serde_json::json!({
        "schema_version": 1,
        "projection_id": request.projection_id,
        "document_digest": response.export.document_digest,
        "file_name": file_name,
        "byte_count": response.export.json_text.len(),
        "created": true,
        "action_capability": "none",
    }))
}

fn export_resume_docx_blocking(
    app: &AppHandle,
    request: ResumeExportDocxRequest,
) -> Result<Value, DesktopError> {
    export_resume_docx_with_operation(app, request, Operation::ResumeRescueExportDocx)
}

fn export_resume_rewrite_docx_blocking(
    app: &AppHandle,
    request: ResumeExportDocxRequest,
) -> Result<Value, DesktopError> {
    export_resume_docx_with_operation(app, request, Operation::ResumeRescueExportRewriteDocx)
}

fn export_resume_docx_with_operation(
    app: &AppHandle,
    request: ResumeExportDocxRequest,
    operation: Operation,
) -> Result<Value, DesktopError> {
    let params = if operation == Operation::ResumeRescueExportRewriteDocx {
        serde_json::json!({
            "version_id": request.projection_id,
            "expected_preview_document_digest": request.expected_preview_document_digest,
        })
    } else {
        serde_json::json!({
            "projection_id": request.projection_id,
            "expected_preview_document_digest": request.expected_preview_document_digest,
        })
    };
    let value = bridge::call_blocking(operation, params)?;
    let response: ResumeNativeExportResponse = serde_json::from_value(value).map_err(|_| {
        DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The desktop bridge returned an invalid Résumé Rescue DOCX export.",
        )
    })?;
    let content = validate_resume_docx_export(&response.export, &request)?;

    let default_name = format!("resume-{}.docx", safe_export_stem(&request.projection_id));
    let mut dialog = FileDialog::new()
        .add_filter("Word document", &["docx"])
        .set_can_create_directories(true)
        .set_file_name(default_name)
        .set_title("Export a new Résumé Rescue DOCX file");
    if let Some(window) = app.get_webview_window("main") {
        dialog = dialog.set_parent(&window);
    }
    let path = dialog.save_file().ok_or_else(|| {
        DesktopError::new("USER_CANCELLED", "Résumé Rescue DOCX export was cancelled.")
    })?;
    validate_resume_docx_path(&path)?;
    write_new_docx_export(&path, &content)?;
    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("resume.docx");
    Ok(serde_json::json!({
        "schema_version": 1,
        "projection_id": request.projection_id,
        "artifact_digest": request.expected_artifact_digest,
        "preview_document_digest": request.expected_preview_document_digest,
        "content_digest": response.export.content_digest,
        "format": "docx",
        "file_name": file_name,
        "byte_count": content.len(),
        "created": true,
        "action_capability": "none",
    }))
}

fn export_resume_pdf_blocking(
    app: &AppHandle,
    request: ResumeExportPdfRequest,
) -> Result<Value, DesktopError> {
    export_resume_pdf_with_operation(app, request, Operation::ResumeRescueExportPdf)
}

fn export_resume_rewrite_pdf_blocking(
    app: &AppHandle,
    request: ResumeExportPdfRequest,
) -> Result<Value, DesktopError> {
    export_resume_pdf_with_operation(app, request, Operation::ResumeRescueExportRewritePdf)
}

fn export_resume_pdf_with_operation(
    app: &AppHandle,
    request: ResumeExportPdfRequest,
    operation: Operation,
) -> Result<Value, DesktopError> {
    let params = if operation == Operation::ResumeRescueExportRewritePdf {
        serde_json::json!({
            "version_id": request.projection_id,
            "expected_preview_document_digest": request.expected_preview_document_digest,
        })
    } else {
        serde_json::json!({
            "projection_id": request.projection_id,
            "expected_preview_document_digest": request.expected_preview_document_digest,
        })
    };
    let value = bridge::call_blocking(operation, params)?;
    let response: ResumeNativeExportResponse = serde_json::from_value(value).map_err(|_| {
        DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The desktop bridge returned an invalid Résumé Rescue PDF export.",
        )
    })?;
    let content = validate_resume_pdf_export(&response.export, &request)?;

    let default_name = format!("resume-{}.pdf", safe_export_stem(&request.projection_id));
    let mut dialog = FileDialog::new()
        .add_filter("PDF document", &["pdf"])
        .set_can_create_directories(true)
        .set_file_name(default_name)
        .set_title("Export a new Résumé Rescue PDF file");
    if let Some(window) = app.get_webview_window("main") {
        dialog = dialog.set_parent(&window);
    }
    let path = dialog.save_file().ok_or_else(|| {
        DesktopError::new("USER_CANCELLED", "Résumé Rescue PDF export was cancelled.")
    })?;
    validate_resume_pdf_path(&path)?;
    write_new_pdf_export(&path, &content)?;
    let file_name = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("resume.pdf");
    Ok(serde_json::json!({
        "schema_version": 1,
        "projection_id": request.projection_id,
        "artifact_digest": request.expected_artifact_digest,
        "preview_document_digest": request.expected_preview_document_digest,
        "content_digest": response.export.content_digest,
        "format": "pdf",
        "file_name": file_name,
        "byte_count": content.len(),
        "created": true,
        "action_capability": "none",
    }))
}

fn safe_export_stem(value: &str) -> String {
    value
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || matches!(character, '-' | '_' | '.') {
                character
            } else {
                '-'
            }
        })
        .collect()
}

fn validate_resume_preview_for_export(
    preview: &ResumePreviewPayload,
    request: &ResumeExportHtmlRequest,
) -> Result<(), DesktopError> {
    let prohibited = [
        "<script", "<iframe", "<object", "<embed", "<form", "<link", "<img", "<base",
    ];
    let lowercase_html = preview.html.to_ascii_lowercase();
    if preview.schema_version != 1
        || preview.projection_id != request.projection_id
        || preview.renderer_version != 1
        || preview.template_id != "openchronicle-classic-v1"
        || preview.action_capability != "none"
        || preview.plain_text.is_empty()
        || !preview.html.starts_with("<!doctype html>\n")
        || !preview.html.contains("default-src 'none'")
        || prohibited.iter().any(|tag| lowercase_html.contains(tag))
        || validate_resume_digest(&preview.artifact_digest).is_err()
        || validate_resume_digest(&preview.document_digest).is_err()
        || !constant_time_equal(
            preview.document_digest.as_bytes(),
            request.expected_document_digest.as_bytes(),
        )
    {
        return Err(DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The desktop bridge returned an invalid Résumé Rescue preview.",
        ));
    }
    Ok(())
}

fn validate_resume_export_path(path: &Path) -> Result<(), DesktopError> {
    if !path
        .extension()
        .and_then(|value| value.to_str())
        .is_some_and(|value| value.eq_ignore_ascii_case("html"))
    {
        return Err(DesktopError::new(
            "INVALID_EXPORT_PATH",
            "Résumé Rescue exports require a .html file name.",
        ));
    }
    Ok(())
}

fn write_new_resume_export(path: &Path, content: &[u8]) -> Result<(), DesktopError> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path).map_err(|error| {
        if error.kind() == ErrorKind::AlreadyExists {
            DesktopError::new(
                "EXPORT_EXISTS",
                "The selected export path already exists; choose a new file name.",
            )
        } else {
            DesktopError::new(
                "EXPORT_FAILED",
                "The Résumé Rescue HTML file could not be created.",
            )
        }
    })?;
    if file
        .write_all(content)
        .and_then(|_| file.sync_all())
        .is_err()
    {
        drop(file);
        let _ = std::fs::remove_file(path);
        return Err(DesktopError::new(
            "EXPORT_FAILED",
            "The Résumé Rescue HTML file could not be written completely.",
        ));
    }
    Ok(())
}

fn read_json_resume_source(path: &Path) -> Result<String, DesktopError> {
    validate_resume_json_path(path).map_err(|_| {
        DesktopError::new(
            "INVALID_IMPORT_PATH",
            "JSON Resume imports require a .json file.",
        )
    })?;
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    #[cfg(not(unix))]
    if std::fs::symlink_metadata(path).is_err_or(|metadata| metadata.file_type().is_symlink()) {
        return Err(DesktopError::new(
            "INVALID_IMPORT_PATH",
            "The selected JSON Resume source is not a regular file.",
        ));
    }
    let file = options.open(path).map_err(|_| {
        DesktopError::new(
            "IMPORT_FAILED",
            "The selected JSON Resume source could not be opened.",
        )
    })?;
    let metadata = file.metadata().map_err(|_| {
        DesktopError::new(
            "IMPORT_FAILED",
            "The selected JSON Resume source could not be inspected.",
        )
    })?;
    if !metadata.is_file() {
        return Err(DesktopError::new(
            "INVALID_IMPORT_PATH",
            "The selected JSON Resume source is not a regular file.",
        ));
    }
    if metadata.len() > MAX_JSON_RESUME_SOURCE_BYTES as u64 {
        return Err(DesktopError::new(
            "IMPORT_TOO_LARGE",
            "The selected JSON Resume source exceeds 500 KB.",
        ));
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    file.take((MAX_JSON_RESUME_SOURCE_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| {
            DesktopError::new(
                "IMPORT_FAILED",
                "The selected JSON Resume source could not be read completely.",
            )
        })?;
    if bytes.len() > MAX_JSON_RESUME_SOURCE_BYTES {
        return Err(DesktopError::new(
            "IMPORT_TOO_LARGE",
            "The selected JSON Resume source exceeds 500 KB.",
        ));
    }
    let source = String::from_utf8(bytes).map_err(|_| {
        DesktopError::new(
            "IMPORT_INVALID",
            "The selected JSON Resume source is not valid UTF-8.",
        )
    })?;
    validate_json_resume_source_text(&source)?;
    Ok(source)
}

fn read_resume_document_source(path: &Path) -> Result<(Vec<u8>, String), DesktopError> {
    let source_format = path
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase)
        .filter(|value| matches!(value.as_str(), "pdf" | "docx"))
        .ok_or_else(|| {
            DesktopError::new(
                "INVALID_IMPORT_PATH",
                "Résumé document imports require a .pdf or .docx file.",
            )
        })?;
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    #[cfg(not(unix))]
    if std::fs::symlink_metadata(path).is_err_or(|metadata| metadata.file_type().is_symlink()) {
        return Err(DesktopError::new(
            "INVALID_IMPORT_PATH",
            "The selected résumé document is not a regular file.",
        ));
    }
    let file = options.open(path).map_err(|_| {
        DesktopError::new(
            "IMPORT_FAILED",
            "The selected résumé document could not be opened.",
        )
    })?;
    let metadata = file.metadata().map_err(|_| {
        DesktopError::new(
            "IMPORT_FAILED",
            "The selected résumé document could not be inspected.",
        )
    })?;
    if !metadata.is_file() {
        return Err(DesktopError::new(
            "INVALID_IMPORT_PATH",
            "The selected résumé document is not a regular file.",
        ));
    }
    if metadata.len() == 0 || metadata.len() > MAX_RESUME_DOCUMENT_BYTES as u64 {
        return Err(DesktopError::new(
            "IMPORT_TOO_LARGE",
            "The selected résumé document is empty or exceeds 8 MB.",
        ));
    }
    let mut source = Vec::with_capacity(metadata.len() as usize);
    file.take((MAX_RESUME_DOCUMENT_BYTES + 1) as u64)
        .read_to_end(&mut source)
        .map_err(|_| {
            DesktopError::new(
                "IMPORT_FAILED",
                "The selected résumé document could not be read completely.",
            )
        })?;
    if source.len() > MAX_RESUME_DOCUMENT_BYTES {
        return Err(DesktopError::new(
            "IMPORT_TOO_LARGE",
            "The selected résumé document exceeds 8 MB.",
        ));
    }
    let valid_signature = match source_format.as_str() {
        "pdf" => source.starts_with(b"%PDF-"),
        "docx" => source.starts_with(b"PK"),
        _ => false,
    };
    if !valid_signature {
        return Err(DesktopError::new(
            "IMPORT_INVALID",
            "The selected résumé document does not match its file type.",
        ));
    }
    Ok((source, source_format))
}

fn validate_resume_json_path(path: &Path) -> Result<(), DesktopError> {
    if !path
        .extension()
        .and_then(|value| value.to_str())
        .is_some_and(|value| value.eq_ignore_ascii_case("json"))
    {
        return Err(DesktopError::new(
            "INVALID_EXPORT_PATH",
            "JSON Resume files require a .json file name.",
        ));
    }
    Ok(())
}

fn write_new_json_resume_export(path: &Path, content: &[u8]) -> Result<(), DesktopError> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path).map_err(|error| {
        if error.kind() == ErrorKind::AlreadyExists {
            DesktopError::new(
                "EXPORT_EXISTS",
                "The selected export path already exists; choose a new file name.",
            )
        } else {
            DesktopError::new(
                "EXPORT_FAILED",
                "The JSON Resume file could not be created.",
            )
        }
    })?;
    if file
        .write_all(content)
        .and_then(|_| file.sync_all())
        .is_err()
    {
        drop(file);
        let _ = std::fs::remove_file(path);
        return Err(DesktopError::new(
            "EXPORT_FAILED",
            "The JSON Resume file could not be written completely.",
        ));
    }
    Ok(())
}

fn validate_resume_docx_path(path: &Path) -> Result<(), DesktopError> {
    if !path
        .extension()
        .and_then(|value| value.to_str())
        .is_some_and(|value| value.eq_ignore_ascii_case("docx"))
    {
        return Err(DesktopError::new(
            "INVALID_EXPORT_PATH",
            "Résumé Rescue DOCX exports require a .docx file name.",
        ));
    }
    Ok(())
}

fn write_new_docx_export(path: &Path, content: &[u8]) -> Result<(), DesktopError> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path).map_err(|error| {
        if error.kind() == ErrorKind::AlreadyExists {
            DesktopError::new(
                "EXPORT_EXISTS",
                "The selected export path already exists; choose a new file name.",
            )
        } else {
            DesktopError::new(
                "EXPORT_FAILED",
                "The Résumé Rescue DOCX file could not be created.",
            )
        }
    })?;
    if file
        .write_all(content)
        .and_then(|_| file.sync_all())
        .is_err()
    {
        drop(file);
        let _ = std::fs::remove_file(path);
        return Err(DesktopError::new(
            "EXPORT_FAILED",
            "The Résumé Rescue DOCX file could not be written completely.",
        ));
    }
    Ok(())
}

fn validate_resume_pdf_path(path: &Path) -> Result<(), DesktopError> {
    if !path
        .extension()
        .and_then(|value| value.to_str())
        .is_some_and(|value| value.eq_ignore_ascii_case("pdf"))
    {
        return Err(DesktopError::new(
            "INVALID_EXPORT_PATH",
            "Résumé Rescue PDF exports require a .pdf file name.",
        ));
    }
    Ok(())
}

fn write_new_pdf_export(path: &Path, content: &[u8]) -> Result<(), DesktopError> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(path).map_err(|error| {
        if error.kind() == ErrorKind::AlreadyExists {
            DesktopError::new(
                "EXPORT_EXISTS",
                "The selected export path already exists; choose a new file name.",
            )
        } else {
            DesktopError::new(
                "EXPORT_FAILED",
                "The Résumé Rescue PDF file could not be created.",
            )
        }
    })?;
    if file
        .write_all(content)
        .and_then(|_| file.sync_all())
        .is_err()
    {
        drop(file);
        let _ = std::fs::remove_file(path);
        return Err(DesktopError::new(
            "EXPORT_FAILED",
            "The Résumé Rescue PDF file could not be written completely.",
        ));
    }
    Ok(())
}

fn validate_resume_json_admission(request: &ResumeAdmitJsonRequest) -> Result<(), DesktopError> {
    validate_json_resume_source_text(&request.source_text)?;
    validate_resume_digest(&request.expected_review_digest)?;
    validate_resume_identifier(&request.profile_id)?;
    validate_multiline_text(&request.display_name, 512, false)?;
    validate_bounded_text(&request.locale, 64, true)?;
    if request
        .expected_version
        .is_some_and(|version| version > 2_147_483_647)
        || request.selections.len() > MAX_JSON_RESUME_CANDIDATES
    {
        return Err(DesktopError::invalid_request(
            "The JSON Resume admission is too large or has an invalid version.",
        ));
    }
    let mut candidates = HashSet::new();
    let mut facts = HashSet::new();
    for selection in &request.selections {
        validate_resume_identifier(&selection.candidate_id)?;
        validate_resume_identifier(&selection.fact_id)?;
        if !candidates.insert(selection.candidate_id.as_str())
            || !facts.insert(selection.fact_id.as_str())
            || !valid_resume_section(&selection.section)
            || !matches!(
                selection.confidentiality.as_str(),
                "public" | "private" | "confidential"
            )
            || !matches!(
                selection.ownership_scope.as_str(),
                "individual" | "shared" | "organization" | "unspecified"
            )
        {
            return Err(DesktopError::invalid_request(
                "A JSON Resume selection is invalid.",
            ));
        }
    }
    Ok(())
}

fn validate_resume_document_admission(
    request: &ResumeAdmitDocumentRequest,
) -> Result<(), DesktopError> {
    validate_resume_document_review_reference(
        &request.review_token,
        &request.expected_review_digest,
    )?;
    validate_resume_identifier(&request.profile_id)?;
    validate_multiline_text(&request.display_name, 512, false)?;
    validate_bounded_text(&request.locale, 64, true)?;
    if request
        .expected_version
        .is_some_and(|version| version == 0 || version > 2_147_483_647)
        || request.selections.len() > MAX_JSON_RESUME_CANDIDATES
    {
        return Err(DesktopError::invalid_request(
            "The résumé document admission is too large or has an invalid version.",
        ));
    }
    let mut candidates = HashSet::new();
    let mut facts = HashSet::new();
    for selection in &request.selections {
        validate_resume_identifier(&selection.candidate_id)?;
        validate_resume_identifier(&selection.fact_id)?;
        if !candidates.insert(selection.candidate_id.as_str())
            || !facts.insert(selection.fact_id.as_str())
            || !valid_resume_section(&selection.section)
            || !matches!(
                selection.confidentiality.as_str(),
                "public" | "private" | "confidential"
            )
            || !matches!(
                selection.ownership_scope.as_str(),
                "individual" | "shared" | "organization" | "unspecified"
            )
        {
            return Err(DesktopError::invalid_request(
                "A résumé document selection is invalid.",
            ));
        }
    }
    Ok(())
}

fn validate_resume_document_review_reference(
    review_token: &str,
    expected_review_digest: &str,
) -> Result<(), DesktopError> {
    if review_token.len() != 32 || !review_token.bytes().all(|value| value.is_ascii_hexdigit()) {
        return Err(DesktopError::invalid_request(
            "The résumé document review token is invalid.",
        ));
    }
    validate_resume_digest(expected_review_digest)
}

fn validate_json_resume_source_text(source_text: &str) -> Result<(), DesktopError> {
    let bytes = source_text.as_bytes();
    if bytes.len() < 2 || bytes.len() > MAX_JSON_RESUME_SOURCE_BYTES || source_text.contains('\0') {
        return Err(DesktopError::invalid_request(
            "The JSON Resume source is empty, too large, or invalid.",
        ));
    }
    Ok(())
}

fn validate_json_resume_review(
    review: &JsonResumeImportReview,
    source_text: &str,
) -> Result<(), DesktopError> {
    validate_json_resume_upstream(&review.upstream_schema)?;
    let source_digest = sha256_hex(source_text.as_bytes());
    let expected_source_id = format!("json-resume-{}", &source_digest[..32]);
    if review.schema_version != 1
        || review.format != "json_resume_v1"
        || review.action_capability != "none"
        || review.source.id != expected_source_id
        || !constant_time_equal(review.source.digest.as_bytes(), source_digest.as_bytes())
        || review.source.byte_count != source_text.len() as u64
        || review.candidates.len() > MAX_JSON_RESUME_CANDIDATES
        || review.omissions.len() > MAX_JSON_RESUME_CANDIDATES
        || review.unknown_fields.len() > MAX_JSON_RESUME_CANDIDATES
        || review.warnings.len() > 100
        || review.candidates.iter().any(|item| !item.is_object())
        || review.omissions.iter().any(|item| !item.is_object())
        || review.display_name_candidate.chars().count() > 512
        || validate_resume_digest(&review.review_digest).is_err()
    {
        return Err(DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The desktop bridge returned an invalid JSON Resume review.",
        ));
    }
    Ok(())
}

fn validate_resume_document_review(
    review: &ResumeDocumentImportReview,
    source_format: &str,
    source_digest: &str,
    source_bytes: usize,
) -> Result<(), DesktopError> {
    let expected_source_id = format!("resume-document-{}", &source_digest[..32]);
    if review.schema_version != 1
        || review.format != source_format
        || review.extractor.version != 1
        || validate_bounded_text(&review.extractor.method, 128, false).is_err()
        || review.action_capability != "none"
        || review.source.id != expected_source_id
        || !constant_time_equal(review.source.digest.as_bytes(), source_digest.as_bytes())
        || review.source.byte_count != source_bytes as u64
        || review.candidates.len() > MAX_JSON_RESUME_CANDIDATES
        || review.omissions.len() > 100
        || review.warnings.len() > 20
        || review.candidates.iter().any(|item| !item.is_object())
        || review.omissions.iter().any(|item| !item.is_object())
        || review.warnings.iter().any(|item| !item.is_object())
        || validate_resume_digest(&review.review_digest).is_err()
    {
        return Err(DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The desktop bridge returned an invalid résumé document review.",
        ));
    }
    Ok(())
}

fn validate_resume_docx_export(
    export: &ResumeNativeExportPayload,
    request: &ResumeExportDocxRequest,
) -> Result<Vec<u8>, DesktopError> {
    let maximum_encoded = MAX_RESUME_DOCX_EXPORT_BYTES.div_ceil(3) * 4;
    if export.schema_version != 1
        || export.projection_id != request.projection_id
        || export.renderer_version != 1
        || export.native_export_version != 1
        || export.template_id != "openchronicle-classic-v1"
        || export.format != "docx"
        || export.media_type
            != "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        || export.extension != "docx"
        || export.action_capability != "none"
        || export.byte_count == 0
        || export.byte_count > MAX_RESUME_DOCX_EXPORT_BYTES as u64
        || export.content_base64.len() > maximum_encoded
        || !constant_time_equal(
            export.artifact_digest.as_bytes(),
            request.expected_artifact_digest.as_bytes(),
        )
        || !constant_time_equal(
            export.preview_document_digest.as_bytes(),
            request.expected_preview_document_digest.as_bytes(),
        )
        || validate_resume_digest(&export.content_digest).is_err()
    {
        return Err(invalid_docx_export());
    }
    let content = base64::engine::general_purpose::STANDARD
        .decode(export.content_base64.as_bytes())
        .map_err(|_| invalid_docx_export())?;
    let digest = sha256_hex(&content);
    if content.len() as u64 != export.byte_count
        || !content.starts_with(b"PK")
        || !constant_time_equal(export.content_digest.as_bytes(), digest.as_bytes())
    {
        return Err(invalid_docx_export());
    }
    validate_docx_archive(&content)?;
    Ok(content)
}

fn validate_resume_pdf_export(
    export: &ResumeNativeExportPayload,
    request: &ResumeExportPdfRequest,
) -> Result<Vec<u8>, DesktopError> {
    let maximum_encoded = MAX_RESUME_PDF_EXPORT_BYTES.div_ceil(3) * 4;
    if export.schema_version != 1
        || export.projection_id != request.projection_id
        || export.renderer_version != 1
        || export.native_export_version != 1
        || export.template_id != "openchronicle-classic-v1"
        || export.format != "pdf"
        || export.media_type != "application/pdf"
        || export.extension != "pdf"
        || export.action_capability != "none"
        || export.byte_count == 0
        || export.byte_count > MAX_RESUME_PDF_EXPORT_BYTES as u64
        || export.content_base64.len() > maximum_encoded
        || !constant_time_equal(
            export.artifact_digest.as_bytes(),
            request.expected_artifact_digest.as_bytes(),
        )
        || !constant_time_equal(
            export.preview_document_digest.as_bytes(),
            request.expected_preview_document_digest.as_bytes(),
        )
        || validate_resume_digest(&export.content_digest).is_err()
    {
        return Err(invalid_pdf_export());
    }
    let content = base64::engine::general_purpose::STANDARD
        .decode(export.content_base64.as_bytes())
        .map_err(|_| invalid_pdf_export())?;
    let digest = sha256_hex(&content);
    let trimmed = content
        .iter()
        .rposition(|byte| !byte.is_ascii_whitespace())
        .map_or(&content[..0], |index| &content[..=index]);
    if content.len() as u64 != export.byte_count
        || !content.starts_with(b"%PDF-")
        || !trimmed.ends_with(b"%%EOF")
        || [
            b"/AcroForm".as_slice(),
            b"/EmbeddedFile".as_slice(),
            b"/JavaScript".as_slice(),
            b"/Launch".as_slice(),
            b"/OpenAction".as_slice(),
            b"/RichMedia".as_slice(),
        ]
        .iter()
        .any(|marker| {
            content
                .windows(marker.len())
                .any(|window| window == *marker)
        })
        || !constant_time_equal(export.content_digest.as_bytes(), digest.as_bytes())
    {
        return Err(invalid_pdf_export());
    }
    Ok(content)
}

fn validate_docx_archive(content: &[u8]) -> Result<(), DesktopError> {
    let mut package =
        zip::ZipArchive::new(Cursor::new(content)).map_err(|_| invalid_docx_export())?;
    if package.is_empty() || package.len() > 100 {
        return Err(invalid_docx_export());
    }
    let mut names = Vec::with_capacity(package.len());
    let mut seen = HashSet::new();
    let mut expanded_bytes = 0_u64;
    for index in 0..package.len() {
        let mut member = package.by_index(index).map_err(|_| invalid_docx_export())?;
        let name = member.name().to_owned();
        let path = Path::new(&name);
        let lowered = name.to_ascii_lowercase();
        if name.is_empty()
            || name.contains('\\')
            || path.is_absolute()
            || path.components().any(|part| {
                matches!(
                    part,
                    Component::ParentDir | Component::RootDir | Component::Prefix(_)
                )
            })
            || member.is_dir()
            || member.encrypted()
            || !matches!(
                member.compression(),
                CompressionMethod::Stored | CompressionMethod::Deflated
            )
            || !seen.insert(name.clone())
            || ["vbaproject", "activex/", "embeddings/", "oleobject"]
                .iter()
                .any(|marker| lowered.contains(marker))
        {
            return Err(invalid_docx_export());
        }
        expanded_bytes = expanded_bytes.saturating_add(member.size());
        if expanded_bytes > 8 * 1024 * 1024 {
            return Err(invalid_docx_export());
        }
        let member_size = member.size();
        let mut value = Vec::with_capacity(member_size as usize);
        member
            .by_ref()
            .take(member_size.saturating_add(1))
            .read_to_end(&mut value)
            .map_err(|_| invalid_docx_export())?;
        if value.len() as u64 != member_size {
            return Err(invalid_docx_export());
        }
        if name.ends_with(".xml") || name.ends_with(".rels") {
            validate_docx_xml(&value, name.ends_with(".rels"))?;
        }
        names.push(name);
    }
    if names.windows(2).any(|pair| pair[0] >= pair[1])
        || !seen.contains("[Content_Types].xml")
        || !seen.contains("_rels/.rels")
        || !seen.contains("word/document.xml")
    {
        return Err(invalid_docx_export());
    }
    Ok(())
}

fn validate_docx_xml(value: &[u8], relationships: bool) -> Result<(), DesktopError> {
    let mut reader = XmlReader::from_reader(value);
    let mut buffer = Vec::new();
    loop {
        match reader.read_event_into(&mut buffer) {
            Ok(Event::Start(element) | Event::Empty(element)) => {
                if relationships {
                    validate_docx_relationship(&element)?;
                }
            }
            Ok(Event::DocType(_) | Event::PI(_)) => return Err(invalid_docx_export()),
            Ok(Event::Eof) => break,
            Ok(_) => {}
            Err(_) => return Err(invalid_docx_export()),
        }
        buffer.clear();
    }
    Ok(())
}

fn validate_docx_relationship(element: &BytesStart<'_>) -> Result<(), DesktopError> {
    if element.local_name().as_ref() != b"Relationship" {
        return Ok(());
    }
    for attribute in element.attributes().with_checks(true) {
        let attribute = attribute.map_err(|_| invalid_docx_export())?;
        if attribute.key.local_name().as_ref() == b"TargetMode"
            && attribute.value.as_ref() == b"External"
        {
            return Err(invalid_docx_export());
        }
    }
    Ok(())
}

fn invalid_docx_export() -> DesktopError {
    DesktopError::new(
        "BRIDGE_PROTOCOL_ERROR",
        "The desktop bridge returned an invalid Résumé Rescue DOCX export.",
    )
}

fn invalid_pdf_export() -> DesktopError {
    DesktopError::new(
        "BRIDGE_PROTOCOL_ERROR",
        "The desktop bridge returned an invalid Résumé Rescue PDF export.",
    )
}

fn validate_json_resume_export(
    export: &JsonResumeExportPayload,
    request: &ResumeExportJsonRequest,
) -> Result<(), DesktopError> {
    validate_json_resume_upstream(&export.upstream_schema)?;
    let parsed: Value = serde_json::from_str(&export.json_text).map_err(|_| {
        DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The desktop bridge returned malformed JSON Resume content.",
        )
    })?;
    let digest = sha256_hex(export.json_text.as_bytes());
    if export.schema_version != 1
        || export.format != "json_resume_v1"
        || export.action_capability != "none"
        || export.projection_binding.id != request.projection_id
        || export.profile_binding.version == 0
        || parsed != export.document
        || !export.document.is_object()
        || !constant_time_equal(
            export.document_digest.as_bytes(),
            request.expected_document_digest.as_bytes(),
        )
        || !constant_time_equal(export.document_digest.as_bytes(), digest.as_bytes())
        || validate_resume_digest(&export.projection_binding.artifact_digest).is_err()
        || validate_resume_identifier(&export.profile_binding.id).is_err()
        || validate_resume_digest(&export.profile_binding.digest).is_err()
        || export.interoperability_losses.len() > MAX_RESUME_FACTS
        || export
            .interoperability_losses
            .iter()
            .any(|item| !item.is_object())
        || export.warnings.len() > 100
    {
        return Err(DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The desktop bridge returned an invalid JSON Resume export.",
        ));
    }
    Ok(())
}

fn validate_json_resume_upstream(value: &JsonResumeUpstreamSchema) -> Result<(), DesktopError> {
    if value.version != "v1.0.0"
        || value.commit != "272929d51b450dbd5a0d242af24c60252904f405"
        || value.url
            != "https://raw.githubusercontent.com/jsonresume/jsonresume.org/272929d51b450dbd5a0d242af24c60252904f405/packages/schema/schema.json"
    {
        return Err(DesktopError::new(
            "BRIDGE_PROTOCOL_ERROR",
            "The desktop bridge returned an unsupported JSON Resume schema binding.",
        ));
    }
    Ok(())
}

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
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
        request.rewrite_limit,
        request.rewrite_version_limit,
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

fn validate_resume_rewrite_cas(request: &ResumeRewriteCasRequest) -> Result<(), DesktopError> {
    validate_resume_identifier(&request.job_id)?;
    if request.expected_version == 0 || request.expected_version > 2_147_483_647 {
        return Err(DesktopError::invalid_request(
            "The Résumé Rescue rewrite version is invalid.",
        ));
    }
    Ok(())
}

fn validate_resume_rewrite_decision(
    request: &ResumeRewriteDecisionRequest,
) -> Result<(), DesktopError> {
    validate_resume_identifier(&request.job_id)?;
    validate_resume_identifier(&request.proposal_id)?;
    validate_resume_digest(&request.expected_proposal_digest)?;
    validate_resume_digest(&request.expected_artifact_digest)?;
    if !request.expected_head_id.is_empty() {
        validate_resume_identifier(&request.expected_head_id)?;
    }
    if request.expected_job_version == 0
        || request.expected_job_version > 2_147_483_647
        || !matches!(request.decision.as_str(), "accepted" | "rejected")
    {
        return Err(DesktopError::invalid_request(
            "The Résumé Rescue rewrite decision is invalid.",
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
        ResumeProvenanceRequest::JsonResumeField {
            reviewed_at,
            source_id,
            source_digest,
            json_pointer,
            value_digest,
            mapping,
            upstream_schema_version,
        } => {
            validate_bounded_text(reviewed_at, 100, false)?;
            validate_resume_identifier(source_id)?;
            validate_resume_digest(source_digest)?;
            validate_multiline_text(json_pointer, 1_024, false)?;
            validate_resume_digest(value_digest)?;
            if !matches!(
                mapping.as_str(),
                "exact_field" | "deterministic_composite" | "openchronicle_extension_exact"
            ) || upstream_schema_version != "v1.0.0"
            {
                return Err(DesktopError::invalid_request(
                    "A JSON Resume provenance binding is invalid.",
                ));
            }
            Ok(())
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
            rewrite_limit: Some(50),
            rewrite_version_limit: Some(50),
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
    fn resume_rewrite_review_is_single_proposal_and_digest_bound() {
        let valid = ResumeRewriteDecisionRequest {
            job_id: "resume-rewrite-job".to_owned(),
            proposal_id: "proposal-one".to_owned(),
            expected_proposal_digest: "a".repeat(64),
            expected_job_version: 3,
            expected_head_id: String::new(),
            expected_artifact_digest: "b".repeat(64),
            decision: "accepted".to_owned(),
        };
        assert!(validate_resume_rewrite_decision(&valid).is_ok());
        assert!(
            validate_resume_rewrite_decision(&ResumeRewriteDecisionRequest {
                decision: "accept_all".to_owned(),
                ..valid
            })
            .is_err()
        );
    }

    #[test]
    fn resume_export_preview_is_digest_bound_and_rejects_active_content() {
        let request = ResumeExportHtmlRequest {
            projection_id: "resume-projection-1".to_owned(),
            expected_document_digest: "d".repeat(64),
        };
        let mut preview = ResumePreviewPayload {
            schema_version: 1,
            projection_id: request.projection_id.clone(),
            artifact_digest: "a".repeat(64),
            renderer_version: 1,
            template_id: "openchronicle-classic-v1".to_owned(),
            html: "<!doctype html>\n<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'\"><main>Reviewed fact</main>".to_owned(),
            plain_text: "Reviewed fact\n".to_owned(),
            document_digest: request.expected_document_digest.clone(),
            action_capability: "none".to_owned(),
        };
        assert!(validate_resume_preview_for_export(&preview, &request).is_ok());

        preview.html.push_str("<script>alert(1)</script>");
        assert_eq!(
            validate_resume_preview_for_export(&preview, &request)
                .expect_err("active content must fail")
                .code,
            "BRIDGE_PROTOCOL_ERROR"
        );
        preview.html = "<!doctype html>\ndefault-src 'none'<main>Reviewed fact</main>".to_owned();
        preview.document_digest = "e".repeat(64);
        assert!(validate_resume_preview_for_export(&preview, &request).is_err());
    }

    #[test]
    fn resume_export_creates_private_html_without_overwriting() {
        let directory = tempfile::tempdir().expect("temporary export directory");
        let path = directory.path().join("reviewed-resume.html");
        assert!(validate_resume_export_path(&path).is_ok());
        assert!(validate_resume_export_path(&directory.path().join("resume.pdf")).is_err());

        write_new_resume_export(&path, b"reviewed exact HTML").expect("new export");
        assert_eq!(
            std::fs::read(&path).expect("read export"),
            b"reviewed exact HTML"
        );
        assert_eq!(
            write_new_resume_export(&path, b"replacement")
                .expect_err("existing file must not be overwritten")
                .code,
            "EXPORT_EXISTS"
        );
        assert_eq!(
            std::fs::read(&path).expect("read preserved export"),
            b"reviewed exact HTML"
        );

        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            assert_eq!(
                std::fs::metadata(&path).expect("export metadata").mode() & 0o777,
                0o600
            );
        }
    }

    #[test]
    fn json_resume_native_reader_rejects_wrong_paths_links_and_oversize_files() {
        let directory = tempfile::tempdir().expect("temporary import directory");
        let source = directory.path().join("reviewed.json");
        std::fs::write(&source, b"{\"basics\":{\"name\":\"Ada\"}}").expect("write source fixture");
        assert_eq!(
            read_json_resume_source(&source).expect("read reviewed source"),
            "{\"basics\":{\"name\":\"Ada\"}}"
        );
        let wrong_extension = directory.path().join("reviewed.txt");
        std::fs::write(&wrong_extension, b"{}").expect("write extension fixture");
        assert_eq!(
            read_json_resume_source(&wrong_extension)
                .expect_err("wrong extension must fail")
                .code,
            "INVALID_IMPORT_PATH"
        );
        let oversized = directory.path().join("oversized.json");
        std::fs::write(&oversized, vec![b'x'; MAX_JSON_RESUME_SOURCE_BYTES + 1])
            .expect("write oversized fixture");
        assert_eq!(
            read_json_resume_source(&oversized)
                .expect_err("oversized source must fail")
                .code,
            "IMPORT_TOO_LARGE"
        );

        #[cfg(unix)]
        {
            use std::os::unix::fs::symlink;
            let link = directory.path().join("linked.json");
            symlink(&source, &link).expect("create source link");
            assert!(read_json_resume_source(&link).is_err());
        }
    }

    #[test]
    fn json_resume_review_is_bound_to_native_source_digest() {
        let source = "{\"basics\":{\"name\":\"Ada\"}}";
        let digest = sha256_hex(source.as_bytes());
        let review = JsonResumeImportReview {
            schema_version: 1,
            format: "json_resume_v1".to_owned(),
            upstream_schema: json_resume_upstream_fixture(),
            source: JsonResumeSourceBinding {
                id: format!("json-resume-{}", &digest[..32]),
                digest,
                byte_count: source.len() as u64,
            },
            display_name_candidate: "Ada".to_owned(),
            candidates: vec![serde_json::json!({"id": "candidate-1"})],
            omissions: vec![],
            unknown_fields: vec![],
            warnings: vec![],
            action_capability: "none".to_owned(),
            review_digest: "a".repeat(64),
        };
        assert!(validate_json_resume_review(&review, source).is_ok());
        assert_eq!(
            validate_json_resume_review(&review, "{\"basics\":{}}")
                .expect_err("changed source must fail")
                .code,
            "BRIDGE_PROTOCOL_ERROR"
        );
    }

    #[test]
    fn json_resume_admission_rejects_duplicate_or_unbounded_selection() {
        let selection = ResumeJsonSelectionRequest {
            candidate_id: "candidate-1".to_owned(),
            fact_id: "fact-1".to_owned(),
            section: "summary".to_owned(),
            confidentiality: "public".to_owned(),
            ownership_scope: "individual".to_owned(),
        };
        let valid = ResumeAdmitJsonRequest {
            source_text: "{}".to_owned(),
            expected_review_digest: "a".repeat(64),
            profile_id: "profile-1".to_owned(),
            display_name: "Ada".to_owned(),
            locale: "en-US".to_owned(),
            selections: vec![selection],
            expected_version: Some(1),
        };
        assert!(validate_resume_json_admission(&valid).is_ok());
        let duplicated = ResumeAdmitJsonRequest {
            selections: vec![
                ResumeJsonSelectionRequest {
                    candidate_id: "candidate-1".to_owned(),
                    fact_id: "fact-1".to_owned(),
                    section: "summary".to_owned(),
                    confidentiality: "public".to_owned(),
                    ownership_scope: "individual".to_owned(),
                },
                ResumeJsonSelectionRequest {
                    candidate_id: "candidate-1".to_owned(),
                    fact_id: "fact-2".to_owned(),
                    section: "skill".to_owned(),
                    confidentiality: "public".to_owned(),
                    ownership_scope: "individual".to_owned(),
                },
            ],
            ..valid
        };
        assert!(validate_resume_json_admission(&duplicated).is_err());
    }

    #[test]
    fn resume_document_reader_checks_extension_signature_size_and_links() {
        let directory = tempfile::tempdir().expect("temporary import directory");
        let pdf = directory.path().join("resume.PDF");
        std::fs::write(&pdf, b"%PDF-1.7\nfixture").expect("write PDF fixture");
        assert_eq!(
            read_resume_document_source(&pdf).expect("read PDF fixture"),
            (b"%PDF-1.7\nfixture".to_vec(), "pdf".to_owned())
        );

        let docx = directory.path().join("resume.docx");
        std::fs::write(&docx, b"PK\x03\x04fixture").expect("write DOCX fixture");
        assert_eq!(
            read_resume_document_source(&docx).expect("read DOCX fixture"),
            (b"PK\x03\x04fixture".to_vec(), "docx".to_owned())
        );

        let wrong_extension = directory.path().join("resume.txt");
        std::fs::write(&wrong_extension, b"%PDF-1.7").expect("write extension fixture");
        assert_eq!(
            read_resume_document_source(&wrong_extension)
                .expect_err("wrong extension must fail")
                .code,
            "INVALID_IMPORT_PATH"
        );

        let wrong_signature = directory.path().join("resume.pdf");
        std::fs::write(&wrong_signature, b"not a PDF").expect("write signature fixture");
        assert_eq!(
            read_resume_document_source(&wrong_signature)
                .expect_err("wrong signature must fail")
                .code,
            "IMPORT_INVALID"
        );

        let oversized = directory.path().join("oversized.docx");
        std::fs::write(&oversized, vec![b'x'; MAX_RESUME_DOCUMENT_BYTES + 1])
            .expect("write oversized fixture");
        assert_eq!(
            read_resume_document_source(&oversized)
                .expect_err("oversized document must fail")
                .code,
            "IMPORT_TOO_LARGE"
        );

        #[cfg(unix)]
        {
            use std::os::unix::fs::symlink;
            let link = directory.path().join("linked.pdf");
            symlink(&pdf, &link).expect("create source link");
            assert!(read_resume_document_source(&link).is_err());
        }
    }

    #[test]
    fn resume_document_admission_is_token_digest_and_selection_bounded() {
        let selection = ResumeJsonSelectionRequest {
            candidate_id: "candidate-1".to_owned(),
            fact_id: "fact-1".to_owned(),
            section: "experience".to_owned(),
            confidentiality: "private".to_owned(),
            ownership_scope: "individual".to_owned(),
        };
        let valid = ResumeAdmitDocumentRequest {
            review_token: "a".repeat(32),
            expected_review_digest: "b".repeat(64),
            profile_id: "profile-1".to_owned(),
            display_name: "Ada".to_owned(),
            locale: "en-US".to_owned(),
            selections: vec![selection],
            expected_version: Some(1),
        };
        assert!(validate_resume_document_admission(&valid).is_ok());

        let invalid_token = ResumeAdmitDocumentRequest {
            review_token: "not-a-token".to_owned(),
            selections: vec![],
            ..valid
        };
        assert!(validate_resume_document_admission(&invalid_token).is_err());

        let duplicated = ResumeAdmitDocumentRequest {
            review_token: "c".repeat(32),
            selections: vec![
                ResumeJsonSelectionRequest {
                    candidate_id: "candidate-1".to_owned(),
                    fact_id: "fact-1".to_owned(),
                    section: "summary".to_owned(),
                    confidentiality: "public".to_owned(),
                    ownership_scope: "individual".to_owned(),
                },
                ResumeJsonSelectionRequest {
                    candidate_id: "candidate-1".to_owned(),
                    fact_id: "fact-2".to_owned(),
                    section: "skill".to_owned(),
                    confidentiality: "public".to_owned(),
                    ownership_scope: "individual".to_owned(),
                },
            ],
            ..invalid_token
        };
        assert!(validate_resume_document_admission(&duplicated).is_err());
    }

    #[test]
    fn resume_document_vault_prevents_replay_concurrency_and_digest_swap() {
        let vault = ResumeDocumentVault::default();
        let source = b"%PDF-fixture".to_vec();
        let source_digest = sha256_hex(&source);
        let review_digest = "a".repeat(64);
        let token = vault
            .insert(
                source.clone(),
                "pdf".to_owned(),
                source_digest.clone(),
                review_digest.clone(),
            )
            .expect("store document review");
        assert_eq!(token.len(), 32);

        assert_eq!(
            vault
                .begin(&token, &"b".repeat(64))
                .err()
                .expect("changed digest must fail")
                .code,
            "DOCUMENT_REVIEW_CHANGED"
        );
        let lease = vault
            .begin(&token, &review_digest)
            .expect("lease reviewed source");
        assert_eq!(lease.source, source);
        assert_eq!(
            vault
                .begin(&token, &review_digest)
                .err()
                .expect("concurrent lease must fail")
                .code,
            "DOCUMENT_REVIEW_CHANGED"
        );

        vault.finish(&token, &source_digest, false);
        assert!(vault.begin(&token, &review_digest).is_ok());
        vault.finish(&token, &source_digest, false);
        assert!(vault
            .discard(&token, &"b".repeat(64))
            .expect_err("changed discard digest must fail")
            .code
            .eq("DOCUMENT_REVIEW_CHANGED"));
        assert!(vault
            .discard(&token, &review_digest)
            .expect("discard reviewed source"));
        assert!(!vault
            .discard(&token, &review_digest)
            .expect("discard is idempotent"));
        assert_eq!(
            vault
                .begin(&token, &review_digest)
                .err()
                .expect("consumed token must not replay")
                .code,
            "DOCUMENT_REVIEW_EXPIRED"
        );
    }

    #[test]
    fn resume_document_review_is_bound_to_native_source() {
        let source = b"%PDF-fixture";
        let digest = sha256_hex(source);
        let review = ResumeDocumentImportReview {
            schema_version: 1,
            format: "pdf".to_owned(),
            extractor: ResumeDocumentExtractorBinding {
                version: 1,
                method: "pdfplumber-lines-v1".to_owned(),
            },
            source: JsonResumeSourceBinding {
                id: format!("resume-document-{}", &digest[..32]),
                digest: digest.clone(),
                byte_count: source.len() as u64,
            },
            candidates: vec![serde_json::json!({"id": "candidate-1"})],
            omissions: vec![],
            warnings: vec![],
            action_capability: "none".to_owned(),
            review_digest: "a".repeat(64),
        };
        assert!(validate_resume_document_review(&review, "pdf", &digest, source.len()).is_ok());
        assert!(validate_resume_document_review(&review, "docx", &digest, source.len()).is_err());
        assert!(
            validate_resume_document_review(&review, "pdf", &"b".repeat(64), source.len()).is_err()
        );
    }

    #[test]
    fn json_resume_export_recomputes_digest_and_creates_private_file() {
        let json_text = "{\n  \"basics\": {\n    \"name\": \"Ada\"\n  }\n}\n";
        let digest = sha256_hex(json_text.as_bytes());
        let request = ResumeExportJsonRequest {
            projection_id: "projection-1".to_owned(),
            expected_document_digest: digest.clone(),
        };
        let export = JsonResumeExportPayload {
            schema_version: 1,
            format: "json_resume_v1".to_owned(),
            upstream_schema: json_resume_upstream_fixture(),
            projection_binding: JsonResumeProjectionBinding {
                id: request.projection_id.clone(),
                artifact_digest: "b".repeat(64),
            },
            profile_binding: JsonResumeProfileBinding {
                id: "profile-1".to_owned(),
                version: 1,
                digest: "c".repeat(64),
            },
            document: serde_json::json!({"basics": {"name": "Ada"}}),
            json_text: json_text.to_owned(),
            document_digest: digest,
            interoperability_losses: vec![],
            warnings: vec!["Review before sharing.".to_owned()],
            action_capability: "none".to_owned(),
        };
        assert!(validate_json_resume_export(&export, &request).is_ok());

        let directory = tempfile::tempdir().expect("temporary export directory");
        let path = directory.path().join("resume.json");
        write_new_json_resume_export(&path, json_text.as_bytes()).expect("new JSON export");
        assert_eq!(
            std::fs::read_to_string(&path).expect("read export"),
            json_text
        );
        assert_eq!(
            write_new_json_resume_export(&path, b"{}")
                .expect_err("existing file must not be overwritten")
                .code,
            "EXPORT_EXISTS"
        );
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            assert_eq!(
                std::fs::metadata(&path).expect("export metadata").mode() & 0o777,
                0o600
            );
        }
    }

    #[test]
    fn docx_export_is_identity_digest_and_archive_bound() {
        let content = docx_fixture(false, false);
        let digest = sha256_hex(&content);
        let request = ResumeExportDocxRequest {
            projection_id: "projection-1".to_owned(),
            expected_artifact_digest: "a".repeat(64),
            expected_preview_document_digest: "b".repeat(64),
        };
        let mut export = ResumeNativeExportPayload {
            schema_version: 1,
            projection_id: request.projection_id.clone(),
            artifact_digest: request.expected_artifact_digest.clone(),
            preview_document_digest: request.expected_preview_document_digest.clone(),
            renderer_version: 1,
            native_export_version: 1,
            template_id: "openchronicle-classic-v1".to_owned(),
            format: "docx".to_owned(),
            media_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                .to_owned(),
            extension: "docx".to_owned(),
            byte_count: content.len() as u64,
            content_digest: digest,
            action_capability: "none".to_owned(),
            content_base64: base64::engine::general_purpose::STANDARD.encode(&content),
        };
        assert_eq!(
            validate_resume_docx_export(&export, &request).expect("valid DOCX export"),
            content
        );

        export.projection_id = "projection-swap".to_owned();
        assert_eq!(
            validate_resume_docx_export(&export, &request)
                .expect_err("projection swap must fail")
                .code,
            "BRIDGE_PROTOCOL_ERROR"
        );
        export.projection_id = request.projection_id.clone();
        export.content_base64.push('A');
        assert!(validate_resume_docx_export(&export, &request).is_err());
    }

    #[test]
    fn docx_archive_rejects_external_relationships_and_active_content() {
        assert_eq!(
            validate_docx_archive(&docx_fixture(true, false))
                .expect_err("external target must fail")
                .code,
            "BRIDGE_PROTOCOL_ERROR"
        );
        assert_eq!(
            validate_docx_archive(&docx_fixture(false, true))
                .expect_err("macro payload must fail")
                .code,
            "BRIDGE_PROTOCOL_ERROR"
        );
    }

    #[test]
    fn docx_export_creates_private_file_without_overwriting() {
        let directory = tempfile::tempdir().expect("temporary export directory");
        let path = directory.path().join("reviewed-resume.docx");
        let content = docx_fixture(false, false);
        assert!(validate_resume_docx_path(&path).is_ok());
        assert!(validate_resume_docx_path(&directory.path().join("resume.pdf")).is_err());

        write_new_docx_export(&path, &content).expect("new DOCX export");
        assert_eq!(std::fs::read(&path).expect("read DOCX export"), content);
        assert_eq!(
            write_new_docx_export(&path, b"replacement")
                .expect_err("existing file must not be overwritten")
                .code,
            "EXPORT_EXISTS"
        );
        assert_eq!(
            std::fs::read(&path).expect("read preserved DOCX export"),
            content
        );

        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            assert_eq!(
                std::fs::metadata(&path).expect("export metadata").mode() & 0o777,
                0o600
            );
        }
    }

    #[test]
    fn pdf_export_is_identity_digest_and_passive_structure_bound() {
        let content = b"%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n".to_vec();
        let request = ResumeExportPdfRequest {
            projection_id: "projection-1".to_owned(),
            expected_artifact_digest: "a".repeat(64),
            expected_preview_document_digest: "b".repeat(64),
        };
        let mut export = pdf_export_fixture(&content, &request);
        assert_eq!(
            validate_resume_pdf_export(&export, &request).expect("valid PDF export"),
            content
        );

        export.artifact_digest = "c".repeat(64);
        assert_eq!(
            validate_resume_pdf_export(&export, &request)
                .expect_err("artifact swap must fail")
                .code,
            "BRIDGE_PROTOCOL_ERROR"
        );

        let active = b"%PDF-1.7\n1 0 obj<</OpenAction 2 0 R>>endobj\n%%EOF\n".to_vec();
        let active_export = pdf_export_fixture(&active, &request);
        assert_eq!(
            validate_resume_pdf_export(&active_export, &request)
                .expect_err("active PDF must fail")
                .code,
            "BRIDGE_PROTOCOL_ERROR"
        );
    }

    #[test]
    fn pdf_export_creates_private_file_without_overwriting() {
        let directory = tempfile::tempdir().expect("temporary export directory");
        let path = directory.path().join("reviewed-resume.pdf");
        let content = b"%PDF-1.7\n%%EOF\n";
        assert!(validate_resume_pdf_path(&path).is_ok());
        assert!(validate_resume_pdf_path(&directory.path().join("resume.docx")).is_err());

        write_new_pdf_export(&path, content).expect("new PDF export");
        assert_eq!(std::fs::read(&path).expect("read PDF export"), content);
        assert_eq!(
            write_new_pdf_export(&path, b"replacement")
                .expect_err("existing file must not be overwritten")
                .code,
            "EXPORT_EXISTS"
        );
        assert_eq!(std::fs::read(&path).expect("read preserved PDF"), content);

        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            assert_eq!(
                std::fs::metadata(&path).expect("export metadata").mode() & 0o777,
                0o600
            );
        }
    }

    fn pdf_export_fixture(
        content: &[u8],
        request: &ResumeExportPdfRequest,
    ) -> ResumeNativeExportPayload {
        ResumeNativeExportPayload {
            schema_version: 1,
            projection_id: request.projection_id.clone(),
            artifact_digest: request.expected_artifact_digest.clone(),
            preview_document_digest: request.expected_preview_document_digest.clone(),
            renderer_version: 1,
            native_export_version: 1,
            template_id: "openchronicle-classic-v1".to_owned(),
            format: "pdf".to_owned(),
            media_type: "application/pdf".to_owned(),
            extension: "pdf".to_owned(),
            byte_count: content.len() as u64,
            content_digest: sha256_hex(content),
            action_capability: "none".to_owned(),
            content_base64: base64::engine::general_purpose::STANDARD.encode(content),
        }
    }

    fn docx_fixture(external_relationship: bool, active_content: bool) -> Vec<u8> {
        use zip::write::SimpleFileOptions;

        let cursor = Cursor::new(Vec::new());
        let mut writer = zip::ZipWriter::new(cursor);
        let options = SimpleFileOptions::default()
            .compression_method(CompressionMethod::Deflated)
            .last_modified_time(zip::DateTime::default());
        writer
            .start_file("[Content_Types].xml", options)
            .expect("content types member");
        writer
            .write_all(
                br#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>"#,
            )
            .expect("content types XML");
        writer
            .start_file("_rels/.rels", options)
            .expect("relationships member");
        let target_mode = if external_relationship {
            r#" TargetMode="External""#
        } else {
            ""
        };
        writer
            .write_all(
                format!(
                    r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"{target_mode}/></Relationships>"#
                )
                .as_bytes(),
            )
            .expect("relationships XML");
        writer
            .start_file("word/document.xml", options)
            .expect("document member");
        writer
            .write_all(
                br#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Reviewed fact</w:t></w:r></w:p></w:body></w:document>"#,
            )
            .expect("document XML");
        if active_content {
            writer
                .start_file("word/vbaProject.bin", options)
                .expect("macro member");
            writer.write_all(b"macro").expect("macro fixture");
        }
        writer.finish().expect("finish DOCX fixture").into_inner()
    }

    fn json_resume_upstream_fixture() -> JsonResumeUpstreamSchema {
        JsonResumeUpstreamSchema {
            version: "v1.0.0".to_owned(),
            commit: "272929d51b450dbd5a0d242af24c60252904f405".to_owned(),
            url: "https://raw.githubusercontent.com/jsonresume/jsonresume.org/272929d51b450dbd5a0d242af24c60252904f405/packages/schema/schema.json".to_owned(),
        }
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
        let request = serde_json::json!({
            "content": "界".repeat(MAX_REQUEST_BYTES / "界".len() + 1)
        });
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
