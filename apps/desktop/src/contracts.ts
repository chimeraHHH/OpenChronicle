export type PageId =
  | "overview"
  | "suggestions"
  | "prompt-rescue"
  | "review"
  | "daily-wrap"
  | "timeline"
  | "privacy";

// Rust owns the sidecar envelope, while these types own the corresponding
// WebView result projection. Version 4 adds the bounded Prompt Rescue review
// surface without adding paste, submit, or target-application capabilities.
export const DESKTOP_BRIDGE_PROTOCOL_VERSION = 4 as const;

export type PromptRescueStatus = "queued" | "leased" | "ready" | "failed";
export type PromptRescueProviderLocation = "local" | "remote_or_unknown";

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
  source_kind: "manual_paste";
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
