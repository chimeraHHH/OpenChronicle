export type PageId =
  | "overview"
  | "suggestions"
  | "prompt-rescue"
  | "reply-rescue"
  | "resume-rescue"
  | "review"
  | "daily-wrap"
  | "timeline"
  | "privacy";

// Rust owns the sidecar envelope, while these types own the corresponding
// WebView result projection. Version 11 adds reviewed PDF/DOCX extraction without
// exposing source bytes or adding upload, application, submission, or send capabilities.
export const DESKTOP_BRIDGE_PROTOCOL_VERSION = 12 as const;

export type PromptRescueStatus = "queued" | "leased" | "ready" | "failed";
export type PromptRescueProviderLocation = "local" | "remote_or_unknown";
export type PromptRescueSourceKind = "manual_paste" | "macos_selection";

export interface PromptRescueSelectionBinding {
  schema_version: 1;
  captured_at: string;
  app_name: string;
  bundle_id: string;
  pid: number;
  window_title: string;
  element_role: string;
  element_subrole: string;
  selection_location: number;
  selection_length: number;
}

export interface PromptRescueOutput {
  schema_version: 1;
  workflow: "prompt_rescue";
  action_capability: "none";
  improved_prompt: string;
  assumptions: string[];
  missing_context: string[];
  changes: string[];
}

export interface PromptRescueJobSummary {
  id: string;
  status: PromptRescueStatus;
  source_kind: PromptRescueSourceKind;
  rough_prompt_preview: string;
  model_identity: string;
  provider_location: PromptRescueProviderLocation;
  output_edited: boolean;
  error_code: string;
  attempt_count: number;
  created_at: string;
  updated_at: string;
  version: number;
}

export interface PromptRescueJob
  extends Omit<PromptRescueJobSummary, "rough_prompt_preview"> {
  rough_prompt: string;
  source_binding: PromptRescueSelectionBinding | null;
  target: string;
  audience: string;
  constraints: string[];
  desired_format: string;
  output: PromptRescueOutput | null;
}

export interface PromptRescueSnapshot {
  enabled: boolean;
  provider: {
    model: string;
    location: PromptRescueProviderLocation;
  };
  jobs: PromptRescueJobSummary[];
}

export type ReplyRescueStatus = "queued" | "leased" | "ready" | "failed";
export type ReplyRescueProviderLocation = "local" | "remote_or_unknown";
export type ReplyRescueSourceKind = "manual_conversation" | "macos_selection";
export type ReplyRescueIdentityAssurance =
  | "manual_unverified"
  | "selected_excerpt_unverified";

interface ReplyRescueSourceFields {
  conversation_text: string;
  participants: string[];
  intended_recipients: string[];
  reply_mode: "reply" | "reply_all" | "unspecified";
  goal: string;
  tone: string;
  style_instructions: string[];
  commitments: string[];
}

export interface ReplyRescueManualSource extends ReplyRescueSourceFields {
  schema_version: 1;
  identity_assurance: "manual_unverified";
}

export interface ReplyRescueSelectionSource extends ReplyRescueSourceFields {
  schema_version: 2;
  identity_assurance: "selected_excerpt_unverified";
  selection_binding: PromptRescueSelectionBinding;
}

export type ReplyRescueSource = ReplyRescueManualSource | ReplyRescueSelectionSource;

export interface ReplyRescueClaim {
  text: string;
  support: "conversation" | "user_direction";
}

export interface ReplyRescueOutput {
  schema_version: 1;
  workflow: "reply_rescue";
  action_capability: "none";
  reply_body: string;
  addressed_questions: string[];
  unresolved_questions: string[];
  assumptions: string[];
  warnings: string[];
  claims: ReplyRescueClaim[];
}

export interface ReplyRescueJobSummary {
  id: string;
  status: ReplyRescueStatus;
  source_kind: ReplyRescueSourceKind;
  conversation_preview: string;
  identity_assurance: ReplyRescueIdentityAssurance;
  model_identity: string;
  provider_location: ReplyRescueProviderLocation;
  output_edited: boolean;
  error_code: string;
  attempt_count: number;
  created_at: string;
  updated_at: string;
  version: number;
}

export interface ReplyRescueJob
  extends Omit<ReplyRescueJobSummary, "conversation_preview" | "identity_assurance"> {
  source: ReplyRescueSource;
  output: ReplyRescueOutput | null;
}

export interface ReplyRescueSnapshot {
  enabled: boolean;
  provider: {
    model: string;
    location: ReplyRescueProviderLocation;
  };
  jobs: ReplyRescueJobSummary[];
}

export type ResumeSectionKind =
  | "summary"
  | "experience"
  | "education"
  | "skill"
  | "project"
  | "certification"
  | "language"
  | "other";
export type ResumeConfidentiality = "public" | "private" | "confidential";
export type ResumeOwnership =
  | "individual"
  | "shared"
  | "organization"
  | "unspecified";

export type ResumeProvenance =
  | { kind: "manual_reviewed"; reviewed_at: string }
  | {
      kind: "document_excerpt";
      reviewed_at: string;
      source_id: string;
      source_digest: string;
      page: number;
      section: string;
      start: number;
      end: number;
      extraction_method: string;
    }
  | {
      kind: "reviewed_memory";
      reviewed_at: string;
      memory_id: string;
      memory_path: string;
      memory_digest: string;
    }
  | {
      kind: "json_resume_field";
      reviewed_at: string;
      source_id: string;
      source_digest: string;
      json_pointer: string;
      value_digest: string;
      mapping: "exact_field" | "deterministic_composite" | "openchronicle_extension_exact";
      upstream_schema_version: "v1.0.0";
    };

export interface ResumeFact {
  id: string;
  section: ResumeSectionKind;
  text: string;
  confidentiality: ResumeConfidentiality;
  ownership_scope: ResumeOwnership;
  provenance: ResumeProvenance[];
}

export interface ResumeConflict {
  id: string;
  fact_ids: string[];
  description: string;
}

export interface ResumeProfile {
  schema_version: 1;
  profile_id: string;
  display_name: string;
  locale: string;
  facts: ResumeFact[];
  conflicts: ResumeConflict[];
}

export interface ResumeProfileVersion {
  id: string;
  version: number;
  digest: string;
  created_at: string;
  profile: ResumeProfile;
}

export interface ResumeOpportunitySource {
  schema_version: 1;
  employer: string;
  title: string;
  source_url: string;
  source_text: string;
  priorities: string[];
  locale: string;
  captured_at: string;
}

export interface ResumeOpportunity {
  id: string;
  digest: string;
  created_at: string;
  snapshot: ResumeOpportunitySource;
}

export interface ResumeProjectionSectionRequest {
  kind: ResumeSectionKind;
  fact_ids: string[];
}

export interface ResumeRequirementRequest {
  id: string;
  text: string;
  fact_ids: string[];
}

export interface ResumeProjectionRequest {
  schema_version: 1;
  sections: ResumeProjectionSectionRequest[];
  requirements: ResumeRequirementRequest[];
}

export interface ResumeArtifactItem {
  fact_id: string;
  text: string;
  transformation: "selected_exact";
  confidentiality: ResumeConfidentiality;
  ownership_scope: ResumeOwnership;
  provenance: ResumeProvenance[];
}

export interface ResumeRequirementCoverage {
  id: string;
  text: string;
  status: "candidate_supported" | "missing_evidence";
  fact_ids: string[];
  support_assurance: "manual_mapping_unverified" | "no_evidence";
}

export interface ResumeRescueArtifact {
  schema_version: 1;
  workflow: "resume_rescue";
  action_capability: "none";
  generation_mode: "deterministic_exact_projection";
  profile_binding: { id: string; version: number; digest: string };
  opportunity_binding: {
    id: string;
    digest: string;
    employer: string;
    title: string;
  };
  sections: Array<{ kind: ResumeSectionKind; items: ResumeArtifactItem[] }>;
  requirement_coverage: ResumeRequirementCoverage[];
  conflicts: ResumeConflict[];
  missing_evidence: Array<{ requirement_id: string; text: string }>;
  excluded_fact_ids: string[];
  warnings: string[];
}

export interface ResumeProjection {
  id: string;
  profile_id: string;
  profile_version: number;
  profile_digest: string;
  opportunity_id: string;
  opportunity_digest: string;
  request: ResumeProjectionRequest;
  artifact: ResumeRescueArtifact;
  artifact_digest: string;
  created_at: string;
}

export interface ResumeRescueState {
  enabled: boolean;
  profiles: ResumeProfileVersion[];
  opportunities: ResumeOpportunity[];
  projections: ResumeProjection[];
}

export interface ResumePreview {
  schema_version: 1;
  projection_id: string;
  artifact_digest: string;
  renderer_version: number;
  template_id: "openchronicle-classic-v1";
  html: string;
  plain_text: string;
  document_digest: string;
  action_capability: "none";
}

export interface ResumeHtmlExportResult {
  schema_version: 1;
  projection_id: string;
  document_digest: string;
  file_name: string;
  byte_count: number;
  created: true;
  action_capability: "none";
}

export interface ResumeDocxExportResult {
  schema_version: 1;
  projection_id: string;
  artifact_digest: string;
  preview_document_digest: string;
  content_digest: string;
  format: "docx";
  file_name: string;
  byte_count: number;
  created: true;
  action_capability: "none";
}

export interface JsonResumeUpstreamSchema {
  version: "v1.0.0";
  commit: "272929d51b450dbd5a0d242af24c60252904f405";
  url: string;
}

export interface JsonResumeImportCandidate {
  id: string;
  suggested_section: ResumeSectionKind;
  suggested_text: string;
  mapping: "exact_field" | "deterministic_composite" | "openchronicle_extension_exact";
  source_fields: Array<{ pointer: string; value: string }>;
  review_status: "unreviewed";
}

export interface JsonResumeImportOmission {
  pointer: string;
  reason: string;
  value_digest: string;
}

export interface JsonResumeImportReview {
  schema_version: 1;
  format: "json_resume_v1";
  upstream_schema: JsonResumeUpstreamSchema;
  source: { id: string; digest: string; byte_count: number };
  display_name_candidate: string;
  candidates: JsonResumeImportCandidate[];
  omissions: JsonResumeImportOmission[];
  unknown_fields: string[];
  warnings: string[];
  action_capability: "none";
  review_digest: string;
}

export interface OpenedJsonResumeReview {
  source_text: string;
  review: JsonResumeImportReview;
}

export interface JsonResumeSelection {
  candidate_id: string;
  fact_id: string;
  section: ResumeSectionKind;
  confidentiality: ResumeConfidentiality;
  ownership_scope: ResumeOwnership;
}

export interface ResumeDocumentPageLocator {
  kind: "page_bbox";
  page: number;
  section: string;
  start: number;
  end: number;
  bbox: [number, number, number, number];
}

export interface ResumeDocumentPartLocator {
  kind: "part_block";
  page: 0;
  section: string;
  start: number;
  end: number;
  part: string;
  block: number;
  block_kind: string;
}

export type ResumeDocumentLocator =
  | ResumeDocumentPageLocator
  | ResumeDocumentPartLocator;

export interface ResumeDocumentCandidate {
  id: string;
  text: string;
  text_digest: string;
  locator: ResumeDocumentLocator;
  extraction_method: string;
  candidate_digest: string;
}

export type ResumeDocumentOmission =
  | { code: "images_not_extracted"; count: number }
  | {
      code: "supplementary_parts_not_extracted";
      parts: string[];
    };

export interface ResumeDocumentWarning {
  code:
    | "docx_pagination_unavailable"
    | "duplicate_candidate_text"
    | "external_relationship_ignored"
    | "no_extractable_text"
    | "ocr_required"
    | "reading_order_requires_review"
    | "untrusted_document_text";
  message: string;
}

export interface ResumeDocumentImportReview {
  schema_version: 1;
  format: "pdf" | "docx";
  extractor: { version: 1; method: string };
  source: { id: string; digest: string; byte_count: number };
  candidates: ResumeDocumentCandidate[];
  omissions: ResumeDocumentOmission[];
  warnings: ResumeDocumentWarning[];
  action_capability: "none";
  review_digest: string;
}

export interface OpenedResumeDocumentReview {
  review_token: string;
  review: ResumeDocumentImportReview;
}

export interface JsonResumeExport {
  schema_version: 1;
  format: "json_resume_v1";
  upstream_schema: JsonResumeUpstreamSchema;
  projection_binding: { id: string; artifact_digest: string };
  profile_binding: { id: string; version: number; digest: string };
  document: Record<string, unknown>;
  json_text: string;
  document_digest: string;
  interoperability_losses: Array<{
    fact_id: string;
    section: ResumeSectionKind;
    reason: string;
  }>;
  warnings: string[];
  action_capability: "none";
}

export type ResumeJsonExportResult = ResumeHtmlExportResult;

export type SuggestionStatus =
  | "ready"
  | "viewed"
  | "accepted"
  | "dismissed"
  | "expired";

export interface WorkResumptionArtifact {
  schema_version: 1;
  workflow: "work_resumption";
  action_capability: "none";
  interruption: {
    previous_end: string;
    current_start: string;
    gap_minutes: number;
  };
  last_verified_state: {
    untrusted_activity_quote: true;
    entries: string[];
    apps: string[];
  };
  resumption_signal: {
    untrusted_activity_quote: true;
    entries: string[];
    apps: string[];
  };
  recommended_next_step: string;
}

export interface Suggestion {
  id: string;
  workflow: "work_resumption";
  status: SuggestionStatus;
  title: string;
  summary: string;
  artifact: WorkResumptionArtifact;
  score: number;
  version: number;
  detected_at: string;
  expires_at: string;
  feedback_reason?: string;
}

export type CandidateStatus =
  | "pending"
  | "conflict"
  | "applying"
  | "accepted"
  | "rejected";

export interface EvidenceRef {
  kind: string;
  id: string;
  path?: string;
  timestamp?: string;
  content_hash?: string;
}

export interface CandidateSummary {
  id: string;
  status: CandidateStatus;
  kind: string;
  target_path: string;
  content_preview: string;
  version: number;
  updated_at: string;
  tags?: string[];
  confidence?: number | null;
  evidence_count?: number;
  conflict_key?: string;
}

export interface Candidate {
  id: string;
  status: CandidateStatus;
  kind: string;
  operation: string;
  target_path: string;
  content: string;
  content_preview?: string;
  version: number;
  created_at: string;
  updated_at: string;
  evidence_count?: number;
  conflict_key?: string;
  tags: string[];
  confidence?: number | null;
  applied_entry_id?: string | null;
  reviewed_at?: string | null;
  review_reason?: string;
  last_error?: string;
  evidence: EvidenceRef[];
}

export interface ForgetPreview {
  candidate_id: string;
  expected_version: number;
  candidate_ids: string[];
  files: Array<{ path: string }>;
  entries: Array<{ id: string; path: string }>;
  wrap_ids: string[];
  plan_digest: string;
  counts: {
    candidates: number;
    memory_files: number;
    memory_entries: number;
    daily_wraps: number;
  };
}

export interface WrapItem {
  id: string;
  kind: WrapCategory;
  text: string;
  supporting_text: string;
  untrusted_activity_quote: true;
  evidence: EvidenceRef[];
}

export type WrapCategory =
  | "completed"
  | "progressed"
  | "open"
  | "blocked"
  | "needs_review";

export interface DailyWrapOutput {
  schema_version: number;
  local_date: string;
  timezone: string;
  status: "ready" | "partial";
  summary: string;
  completed: WrapItem[];
  progressed: WrapItem[];
  open: WrapItem[];
  blocked: WrapItem[];
  needs_review: WrapItem[];
  coverage_gaps: string[];
  generated_at: string;
}

export interface DailyWrap {
  id: string;
  local_date: string;
  timezone: string;
  scope: string;
  status: "succeeded";
  coverage_status: "ready" | "partial";
  revision: number;
  output?: DailyWrapOutput | null;
  published_input_digest?: string;
}

export interface DailyWrapSummary {
  id: string;
  local_date: string;
  timezone: string;
  scope: string;
  status: "succeeded";
  coverage_status: "ready" | "partial";
  revision: number;
  has_output: boolean;
  item_counts?: Partial<Record<WrapCategory, number>>;
}

export interface TimelineItem {
  id: string;
  start_time: string;
  end_time: string;
  timezone: string;
  entries: string[];
  apps: string[];
  capture_count: number;
  source_count?: number;
}

export interface PermissionState {
  kind: string;
  label: string;
  state: "granted" | "denied" | "not_determined" | "restricted" | "unknown";
  required: boolean;
}

export interface PrivacySnapshot {
  policy_version?: string;
  allowed_bundle_ids: string[];
  excluded_bundle_ids: string[];
  excluded_app_names: string[];
  excluded_window_title_patterns: string[];
  deny_unknown_windows: boolean;
  include_screenshot: boolean;
  buffer_retention_hours?: number;
  screenshot_retention_hours?: number;
  model_mode: "local-only" | "cloud-assisted" | "unknown";
  model_provider?: string;
  daily_wrap_enabled: boolean;
  daily_wrap_timezone?: string;
}

export interface DesktopSnapshot {
  generated_at: string;
  daemon: {
    state: "running" | "stopped" | "degraded" | "unknown";
    health?: string;
    pid?: number | null;
    uptime?: string;
  };
  capture: {
    paused: boolean;
    state: "active" | "paused" | "stopped" | "permission_required" | "unknown";
    last_capture_at?: string | null;
    last_app?: string | null;
  };
  review_counts: {
    pending: number;
    conflict: number;
    applying: number;
    accepted: number;
    rejected: number;
  };
  purge_pending_count: number;
  candidates: CandidateSummary[];
  daily_wraps: DailyWrapSummary[];
  suggestions_enabled: boolean;
  suggestions: Suggestion[];
  prompt_rescue: PromptRescueSnapshot;
  reply_rescue: ReplyRescueSnapshot;
  timeline: TimelineItem[];
  privacy: PrivacySnapshot;
  permissions: PermissionState[];
}

export interface ProvenanceNode {
  depth: number;
  source: EvidenceRef & {
    availability?: string;
    integrity?: "current" | "changed" | "unverified";
  };
}

export interface ProvenanceTrace {
  subject: EvidenceRef;
  direct_sources: Array<
    EvidenceRef & {
      availability?: string;
      integrity?: "current" | "changed" | "unverified";
    }
  >;
  trace: ProvenanceNode[];
}

export interface ResolvedEvidence {
  ref: EvidenceRef;
  availability:
    | "available"
    | "current"
    | "expired"
    | "excluded"
    | "changed"
    | "missing"
    | "purging"
    | "unverifiable"
    | "unsupported";
  excerpt?: string;
  content?: string;
  app_name?: string;
  window_title?: string;
  start_time?: string;
  end_time?: string;
  note?: string;
}

export interface SourceSubject {
  kind: string;
  id: string;
  path?: string;
  label: string;
}
