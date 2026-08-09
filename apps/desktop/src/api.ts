import { invoke } from "@tauri-apps/api/core";

import type {
  Candidate,
  CandidateStatus,
  DailyWrap,
  DailyWrapOutput,
  DailyWrapSummary,
  DesktopSnapshot,
  EvidenceRef,
  ForgetPreview,
  JsonResumeExport,
  JsonResumeImportReview,
  JsonResumeSelection,
  JsonResumeUpstreamSchema,
  OpenedJsonResumeReview,
  OpenedResumeDocumentReview,
  PrivacySnapshot,
  PromptRescueJob,
  PromptRescueJobSummary,
  PromptRescueOutput,
  PromptRescueProviderLocation,
  PromptRescueSelectionBinding,
  PromptRescueSourceKind,
  PromptRescueStatus,
  ReplyRescueJob,
  ReplyRescueJobSummary,
  ReplyRescueOutput,
  ReplyRescueProviderLocation,
  ReplyRescueSource,
  ReplyRescueSourceKind,
  ReplyRescueStatus,
  ResumeConfidentiality,
  ResumeConflict,
  ResumeDocxExportResult,
  ResumeFact,
  ResumeHtmlExportResult,
  ResumeJsonExportResult,
  ResumeOpportunity,
  ResumeOpportunitySource,
  ResumeOwnership,
  ResumeProfile,
  ResumeProfileVersion,
  ResumePreview,
  ResumeProjection,
  ResumeProjectionRequest,
  ResumeProvenance,
  ResumeRequirementCoverage,
  ResumeRequirementRequest,
  ResumeRescueArtifact,
  ResumeRescueState,
  ResumeSectionKind,
  ResumeDocumentImportReview,
  ResumeDocumentLocator,
  ProvenanceTrace,
  ResolvedEvidence,
  Suggestion,
  SuggestionStatus,
  TimelineItem,
  WrapCategory,
  WrapItem,
} from "./contracts";

export class DesktopApiError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "DesktopApiError";
    this.code = code;
  }
}

type JsonRecord = Record<string, unknown>;
const candidateStatuses = new Set<CandidateStatus>([
  "pending",
  "conflict",
  "applying",
  "accepted",
  "rejected",
]);
const suggestionStatuses = new Set<SuggestionStatus>([
  "ready",
  "viewed",
  "accepted",
  "dismissed",
  "expired",
]);
const promptRescueStatuses = new Set<PromptRescueStatus>([
  "queued",
  "leased",
  "ready",
  "failed",
]);
const promptRescueProviderLocations = new Set<PromptRescueProviderLocation>([
  "local",
  "remote_or_unknown",
]);
const promptRescueSourceKinds = new Set<PromptRescueSourceKind>([
  "manual_paste",
  "macos_selection",
]);
const replyRescueStatuses = new Set<ReplyRescueStatus>([
  "queued",
  "leased",
  "ready",
  "failed",
]);
const replyRescueProviderLocations = new Set<ReplyRescueProviderLocation>([
  "local",
  "remote_or_unknown",
]);
const replyRescueSourceKinds = new Set(["manual_conversation", "macos_selection"] as const);
const replyRescueIdentityAssurances = new Set([
  "manual_unverified",
  "selected_excerpt_unverified",
] as const);
const resumeSections = new Set<ResumeSectionKind>([
  "summary",
  "experience",
  "education",
  "skill",
  "project",
  "certification",
  "language",
  "other",
]);
const resumeConfidentialities = new Set<ResumeConfidentiality>([
  "public",
  "private",
  "confidential",
]);
const resumeOwnerships = new Set<ResumeOwnership>([
  "individual",
  "shared",
  "organization",
  "unspecified",
]);
const jsonResumeMappings = new Set([
  "exact_field",
  "deterministic_composite",
  "openchronicle_extension_exact",
] as const);
const resumeDocumentWarningCodes = new Set([
  "docx_pagination_unavailable",
  "duplicate_candidate_text",
  "external_relationship_ignored",
  "no_extractable_text",
  "ocr_required",
  "reading_order_requires_review",
  "untrusted_document_text",
] as const);
const wrapCategories: WrapCategory[] = [
  "completed",
  "progressed",
  "open",
  "blocked",
  "needs_review",
];
const dailyWrapStatuses = new Set<DailyWrap["status"]>([
  "succeeded",
]);
const dailyWrapCoverageStatuses = new Set<DailyWrap["coverage_status"]>([
  "ready",
  "partial",
]);
const evidenceStatuses = new Set<ResolvedEvidence["availability"]>([
  "available",
  "current",
  "expired",
  "excluded",
  "changed",
  "missing",
  "purging",
  "unverifiable",
  "unsupported",
]);
const provenanceAvailabilityStatuses = new Set([
  "available",
  "expired",
  "missing",
  "unknown",
]);
const provenanceIntegrityStatuses = new Set(["current", "changed", "unverified"]);

function protocolError(detail: string): never {
  throw new DesktopApiError(
    "BRIDGE_PROTOCOL_ERROR",
    `The local service returned an invalid ${detail}.`,
  );
}

function objectValue(value: unknown, detail: string): JsonRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return protocolError(detail);
  }
  return value as JsonRecord;
}

function closedObject(value: unknown, fields: readonly string[], detail: string): JsonRecord {
  const raw = objectValue(value, detail);
  const allowed = new Set(fields);
  if (
    Object.keys(raw).length !== allowed.size ||
    Object.keys(raw).some((key) => !allowed.has(key))
  ) {
    return protocolError(detail);
  }
  return raw;
}

function arrayValue(value: unknown, detail: string): unknown[] {
  if (!Array.isArray(value)) return protocolError(detail);
  return value;
}

function stringValue(value: unknown, detail: string): string {
  if (typeof value !== "string") return protocolError(detail);
  return value;
}

function optionalString(value: unknown, detail: string): string | undefined {
  if (value === undefined || value === null) return undefined;
  return stringValue(value, detail);
}

function numberValue(value: unknown, detail: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return protocolError(detail);
  return value;
}

function booleanValue(value: unknown, detail: string): boolean {
  if (typeof value !== "boolean") return protocolError(detail);
  return value;
}

function allowedString<T extends string>(
  value: unknown,
  allowed: ReadonlySet<T>,
  detail: string,
): T {
  const result = stringValue(value, detail) as T;
  if (!allowed.has(result)) return protocolError(detail);
  return result;
}

function stringArray(value: unknown, detail: string): string[] {
  return arrayValue(value, detail).map((item, index) => stringValue(item, `${detail}[${index}]`));
}

function candidateStatus(value: unknown): CandidateStatus {
  const status = stringValue(value, "candidate status") as CandidateStatus;
  if (!candidateStatuses.has(status)) return protocolError("candidate status");
  return status;
}

function reference(value: unknown): EvidenceRef {
  const raw = objectValue(value, "evidence reference");
  const path = optionalString(raw.path, "evidence path");
  const timestamp = optionalString(raw.timestamp, "evidence timestamp");
  const contentHash = optionalString(raw.content_hash, "evidence content hash");
  return {
    kind: stringValue(raw.kind, "evidence kind"),
    id: stringValue(raw.id, "evidence id"),
    ...(path ? { path } : {}),
    ...(timestamp ? { timestamp } : {}),
    ...(contentHash ? { content_hash: contentHash } : {}),
  };
}

function sameReference(left: EvidenceRef, right: EvidenceRef): boolean {
  return (
    left.kind === right.kind &&
    left.id === right.id &&
    (left.path ?? "") === (right.path ?? "") &&
    (left.timestamp ?? "") === (right.timestamp ?? "") &&
    (left.content_hash ?? "") === (right.content_hash ?? "")
  );
}

function candidatePayload(value: unknown, evidenceValue: unknown = []): Candidate {
  const raw = objectValue(value, "candidate payload");
  const conflictKey = optionalString(raw.conflict_key, "candidate conflict key");
  const evidence = arrayValue(evidenceValue, "candidate evidence").map(reference);
  const confidence = raw.confidence === null ? null : raw.confidence === undefined ? undefined : numberValue(raw.confidence, "candidate confidence");
  const appliedEntryId = raw.applied_entry_id === null ? null : optionalString(raw.applied_entry_id, "candidate entry id");
  const reviewedAt = raw.reviewed_at === null ? null : optionalString(raw.reviewed_at, "candidate reviewed time");
  return {
    id: stringValue(raw.id, "candidate id"),
    status: candidateStatus(raw.status),
    kind: stringValue(raw.kind, "candidate kind"),
    operation: stringValue(raw.operation, "candidate operation"),
    target_path: stringValue(raw.target_path, "candidate target"),
    content: stringValue(raw.content, "candidate content"),
    version: numberValue(raw.version, "candidate version"),
    created_at: stringValue(raw.created_at, "candidate created time"),
    updated_at: stringValue(raw.updated_at, "candidate updated time"),
    tags: stringArray(raw.tags, "candidate tags"),
    ...(confidence === undefined ? {} : { confidence }),
    ...(conflictKey ? { conflict_key: conflictKey } : {}),
    ...(appliedEntryId === undefined ? {} : { applied_entry_id: appliedEntryId }),
    ...(reviewedAt === undefined ? {} : { reviewed_at: reviewedAt }),
    review_reason: optionalString(raw.review_reason, "candidate review reason") ?? "",
    last_error: optionalString(raw.last_error, "candidate error") ?? "",
    evidence,
    evidence_count: evidence.length,
  };
}

function wrapItem(value: unknown, category: WrapCategory): WrapItem {
  const raw = objectValue(value, "Daily Wrap item");
  const kind = allowedString(raw.kind, new Set(wrapCategories), "Daily Wrap item kind");
  if (kind !== category) return protocolError("Daily Wrap item category");
  const untrustedActivityQuote = booleanValue(
    raw.untrusted_activity_quote,
    "Daily Wrap trust marker",
  );
  if (!untrustedActivityQuote) return protocolError("Daily Wrap trust marker");
  return {
    id: stringValue(raw.id, "Daily Wrap item id"),
    kind,
    text: stringValue(raw.text, "Daily Wrap item text"),
    supporting_text: stringValue(raw.supporting_text, "Daily Wrap supporting text"),
    untrusted_activity_quote: true,
    evidence: arrayValue(raw.evidence, "Daily Wrap item evidence").map(reference),
  };
}

function wrapOutput(value: unknown): DailyWrapOutput | null {
  if (value === null || value === undefined) return null;
  const raw = objectValue(value, "Daily Wrap output");
  const status = stringValue(raw.status, "Daily Wrap output status");
  if (status !== "ready" && status !== "partial") return protocolError("Daily Wrap output status");
  const result = {
    schema_version: numberValue(raw.schema_version, "Daily Wrap schema version"),
    local_date: stringValue(raw.local_date, "Daily Wrap local date"),
    timezone: stringValue(raw.timezone, "Daily Wrap timezone"),
    status,
    summary: stringValue(raw.summary, "Daily Wrap summary"),
    coverage_gaps: stringArray(raw.coverage_gaps, "Daily Wrap coverage gaps"),
    generated_at: stringValue(raw.generated_at, "Daily Wrap generated time"),
  } as DailyWrapOutput;
  for (const category of wrapCategories) {
    result[category] = arrayValue(raw[category], `Daily Wrap ${category}`).map((item) =>
      wrapItem(item, category),
    );
  }
  return result;
}

function wrapPayload(value: unknown): DailyWrap {
  const raw = objectValue(value, "Daily Wrap payload");
  const status = allowedString(raw.status, dailyWrapStatuses, "Daily Wrap status");
  const coverage = allowedString(
    raw.coverage_status,
    dailyWrapCoverageStatuses,
    "Daily Wrap coverage status",
  );
  return {
    id: stringValue(raw.id, "Daily Wrap id"),
    local_date: stringValue(raw.local_date, "Daily Wrap local date"),
    timezone: stringValue(raw.timezone, "Daily Wrap timezone"),
    scope: stringValue(raw.scope, "Daily Wrap scope"),
    status,
    coverage_status: coverage,
    revision: numberValue(raw.revision, "Daily Wrap revision"),
    published_input_digest:
      optionalString(raw.published_input_digest, "Daily Wrap published digest") ?? "",
    output: wrapOutput(raw.output),
  };
}

function timelineItem(value: unknown): TimelineItem {
  const raw = objectValue(value, "timeline item");
  return {
    id: stringValue(raw.id, "timeline id"),
    start_time: stringValue(raw.start_time, "timeline start time"),
    end_time: stringValue(raw.end_time, "timeline end time"),
    timezone: stringValue(raw.timezone, "timeline timezone"),
    entries: stringArray(raw.entries, "timeline entries"),
    apps: stringArray(raw.apps_used, "timeline applications"),
    capture_count: numberValue(raw.capture_count, "timeline capture count"),
  };
}

function workResumptionSuggestion(value: unknown): Suggestion {
  const raw = objectValue(value, "suggestion payload");
  const artifact = objectValue(raw.artifact, "suggestion artifact");
  if (
    numberValue(artifact.schema_version, "suggestion artifact version") !== 1 ||
    stringValue(artifact.workflow, "suggestion artifact workflow") !== "work_resumption" ||
    stringValue(artifact.action_capability, "suggestion action capability") !== "none"
  ) {
    return protocolError("suggestion artifact contract");
  }
  const interruption = objectValue(artifact.interruption, "suggestion interruption");
  const previous = objectValue(
    artifact.last_verified_state,
    "suggestion last verified state",
  );
  const current = objectValue(artifact.resumption_signal, "suggestion resumption signal");
  if (
    booleanValue(previous.untrusted_activity_quote, "suggestion previous trust marker") !== true ||
    booleanValue(current.untrusted_activity_quote, "suggestion current trust marker") !== true
  ) {
    return protocolError("suggestion trust marker");
  }
  const workflow = stringValue(raw.workflow, "suggestion workflow");
  if (workflow !== "work_resumption") return protocolError("suggestion workflow");
  const feedbackReason = optionalString(raw.feedback_reason, "suggestion feedback");
  return {
    id: stringValue(raw.id, "suggestion id"),
    workflow,
    status: allowedString(raw.status, suggestionStatuses, "suggestion status"),
    title: stringValue(raw.title, "suggestion title"),
    summary: stringValue(raw.summary, "suggestion summary"),
    artifact: {
      schema_version: 1,
      workflow: "work_resumption",
      action_capability: "none",
      interruption: {
        previous_end: stringValue(interruption.previous_end, "suggestion previous end"),
        current_start: stringValue(interruption.current_start, "suggestion current start"),
        gap_minutes: numberValue(interruption.gap_minutes, "suggestion gap"),
      },
      last_verified_state: {
        untrusted_activity_quote: true,
        entries: stringArray(previous.entries, "suggestion previous entries"),
        apps: stringArray(previous.apps, "suggestion previous apps"),
      },
      resumption_signal: {
        untrusted_activity_quote: true,
        entries: stringArray(current.entries, "suggestion current entries"),
        apps: stringArray(current.apps, "suggestion current apps"),
      },
      recommended_next_step: stringValue(
        artifact.recommended_next_step,
        "suggestion next step",
      ),
    },
    score: numberValue(raw.score, "suggestion score"),
    version: numberValue(raw.version, "suggestion version"),
    detected_at: stringValue(raw.detected_at, "suggestion detected time"),
    expires_at: stringValue(raw.expires_at, "suggestion expiry"),
    ...(feedbackReason === undefined ? {} : { feedback_reason: feedbackReason }),
  };
}

function promptRescueStatus(value: unknown): PromptRescueStatus {
  return allowedString(value, promptRescueStatuses, "Prompt Rescue status");
}

function promptRescueProviderLocation(value: unknown): PromptRescueProviderLocation {
  return allowedString(
    value,
    promptRescueProviderLocations,
    "Prompt Rescue provider location",
  );
}

function promptRescueSourceKind(value: unknown): PromptRescueSourceKind {
  return allowedString(value, promptRescueSourceKinds, "Prompt Rescue source kind");
}

function promptRescueBinding(
  value: unknown,
  sourceKind: PromptRescueSourceKind,
): PromptRescueSelectionBinding | null {
  const raw = objectValue(value, "Prompt Rescue source binding");
  if (sourceKind === "manual_paste") {
    if (Object.keys(raw).length !== 0) return protocolError("Prompt Rescue source binding");
    return null;
  }
  const fields = new Set([
    "schema_version",
    "captured_at",
    "app_name",
    "bundle_id",
    "pid",
    "window_title",
    "element_role",
    "element_subrole",
    "selection_location",
    "selection_length",
  ]);
  if (Object.keys(raw).length !== fields.size || Object.keys(raw).some((key) => !fields.has(key))) {
    return protocolError("Prompt Rescue source binding");
  }
  if (numberValue(raw.schema_version, "Prompt Rescue binding schema") !== 1) {
    return protocolError("Prompt Rescue source binding");
  }
  const bundleId = stringValue(raw.bundle_id, "Prompt Rescue source bundle");
  const elementRole = stringValue(raw.element_role, "Prompt Rescue source role");
  const pid = numberValue(raw.pid, "Prompt Rescue source pid");
  const location = numberValue(raw.selection_location, "Prompt Rescue selection location");
  const length = numberValue(raw.selection_length, "Prompt Rescue selection length");
  if (!bundleId || !elementRole || !Number.isInteger(pid) || pid <= 0 ||
      !Number.isInteger(location) || location < 0 || !Number.isInteger(length) || length <= 0) {
    return protocolError("Prompt Rescue source binding");
  }
  return {
    schema_version: 1,
    captured_at: stringValue(raw.captured_at, "Prompt Rescue capture time"),
    app_name: stringValue(raw.app_name, "Prompt Rescue source app"),
    bundle_id: bundleId,
    pid,
    window_title: stringValue(raw.window_title, "Prompt Rescue source window"),
    element_role: elementRole,
    element_subrole: stringValue(raw.element_subrole, "Prompt Rescue source subrole"),
    selection_location: location,
    selection_length: length,
  };
}

function promptRescueOutput(value: unknown): PromptRescueOutput | null {
  if (value === null) return null;
  const raw = objectValue(value, "Prompt Rescue output");
  if (
    numberValue(raw.schema_version, "Prompt Rescue schema version") !== 1 ||
    stringValue(raw.workflow, "Prompt Rescue workflow") !== "prompt_rescue" ||
    stringValue(raw.action_capability, "Prompt Rescue action capability") !== "none"
  ) {
    return protocolError("Prompt Rescue prepared-artifact contract");
  }
  const improvedPrompt = stringValue(raw.improved_prompt, "improved prompt");
  if (!improvedPrompt.trim()) return protocolError("improved prompt");
  return {
    schema_version: 1,
    workflow: "prompt_rescue",
    action_capability: "none",
    improved_prompt: improvedPrompt,
    assumptions: stringArray(raw.assumptions, "Prompt Rescue assumptions"),
    missing_context: stringArray(raw.missing_context, "Prompt Rescue missing context"),
    changes: stringArray(raw.changes, "Prompt Rescue changes"),
  };
}

function promptRescueSummary(value: unknown): PromptRescueJobSummary {
  const raw = objectValue(value, "Prompt Rescue summary");
  const sourceKind = promptRescueSourceKind(raw.source_kind);
  return {
    id: stringValue(raw.id, "Prompt Rescue id"),
    status: promptRescueStatus(raw.status),
    source_kind: sourceKind,
    rough_prompt_preview: stringValue(raw.rough_prompt_preview, "Prompt Rescue preview"),
    model_identity: stringValue(raw.model_identity, "Prompt Rescue model"),
    provider_location: promptRescueProviderLocation(raw.provider_location),
    output_edited: booleanValue(raw.output_edited, "Prompt Rescue edited state"),
    error_code: stringValue(raw.error_code, "Prompt Rescue error code"),
    attempt_count: numberValue(raw.attempt_count, "Prompt Rescue attempt count"),
    created_at: stringValue(raw.created_at, "Prompt Rescue created time"),
    updated_at: stringValue(raw.updated_at, "Prompt Rescue updated time"),
    version: numberValue(raw.version, "Prompt Rescue version"),
  };
}

function promptRescueJob(value: unknown): PromptRescueJob {
  const raw = objectValue(value, "Prompt Rescue job");
  const sourceKind = promptRescueSourceKind(raw.source_kind);
  const output = promptRescueOutput(raw.output);
  const status = promptRescueStatus(raw.status);
  if ((status === "ready") !== (output !== null)) {
    return protocolError("Prompt Rescue output state");
  }
  return {
    id: stringValue(raw.id, "Prompt Rescue id"),
    status,
    source_kind: sourceKind,
    source_binding: promptRescueBinding(raw.source_binding, sourceKind),
    rough_prompt: stringValue(raw.rough_prompt, "rough prompt"),
    target: stringValue(raw.target, "Prompt Rescue target"),
    audience: stringValue(raw.audience, "Prompt Rescue audience"),
    constraints: stringArray(raw.constraints, "Prompt Rescue constraints"),
    desired_format: stringValue(raw.desired_format, "Prompt Rescue desired format"),
    model_identity: stringValue(raw.model_identity, "Prompt Rescue model"),
    provider_location: promptRescueProviderLocation(raw.provider_location),
    output,
    output_edited: booleanValue(raw.output_edited, "Prompt Rescue edited state"),
    error_code: stringValue(raw.error_code, "Prompt Rescue error code"),
    attempt_count: numberValue(raw.attempt_count, "Prompt Rescue attempt count"),
    created_at: stringValue(raw.created_at, "Prompt Rescue created time"),
    updated_at: stringValue(raw.updated_at, "Prompt Rescue updated time"),
    version: numberValue(raw.version, "Prompt Rescue version"),
  };
}

function replyRescueStatus(value: unknown): ReplyRescueStatus {
  return allowedString(value, replyRescueStatuses, "Reply Rescue status");
}

function replyRescueProviderLocation(value: unknown): ReplyRescueProviderLocation {
  return allowedString(
    value,
    replyRescueProviderLocations,
    "Reply Rescue provider location",
  );
}

function replyRescueSource(value: unknown, sourceKind: ReplyRescueSourceKind): ReplyRescueSource {
  const raw = objectValue(value, "Reply Rescue source");
  const assurance = allowedString(
    raw.identity_assurance,
    replyRescueIdentityAssurances,
    "Reply Rescue identity assurance",
  );
  const expectedFields = new Set([
    "schema_version",
    "identity_assurance",
    "conversation_text",
    "participants",
    "intended_recipients",
    "reply_mode",
    "goal",
    "tone",
    "style_instructions",
    "commitments",
    ...(sourceKind === "macos_selection" ? ["selection_binding"] : []),
  ]);
  if (
    Object.keys(raw).length !== expectedFields.size ||
    Object.keys(raw).some((key) => !expectedFields.has(key))
  ) {
    return protocolError("Reply Rescue source contract");
  }
  const replyMode = stringValue(raw.reply_mode, "Reply Rescue reply mode");
  if (replyMode !== "reply" && replyMode !== "reply_all" && replyMode !== "unspecified") {
    return protocolError("Reply Rescue reply mode");
  }
  const conversationText = stringValue(
    raw.conversation_text,
    "Reply Rescue conversation",
  );
  if (!conversationText.trim()) return protocolError("Reply Rescue conversation");
  const fields = {
    conversation_text: conversationText,
    participants: stringArray(raw.participants, "Reply Rescue participants"),
    intended_recipients: stringArray(
      raw.intended_recipients,
      "Reply Rescue intended recipients",
    ),
    reply_mode: replyMode as "reply" | "reply_all" | "unspecified",
    goal: stringValue(raw.goal, "Reply Rescue goal"),
    tone: stringValue(raw.tone, "Reply Rescue tone"),
    style_instructions: stringArray(
      raw.style_instructions,
      "Reply Rescue style instructions",
    ),
    commitments: stringArray(raw.commitments, "Reply Rescue commitments"),
  };
  if (sourceKind === "manual_conversation") {
    if (
      numberValue(raw.schema_version, "Reply Rescue source schema") !== 1 ||
      assurance !== "manual_unverified"
    ) {
      return protocolError("Reply Rescue manual source contract");
    }
    return { schema_version: 1, identity_assurance: "manual_unverified", ...fields };
  }
  if (
    numberValue(raw.schema_version, "Reply Rescue source schema") !== 2 ||
    assurance !== "selected_excerpt_unverified"
  ) {
    return protocolError("Reply Rescue selection source contract");
  }
  const binding = promptRescueBinding(raw.selection_binding, "macos_selection");
  if (!binding) return protocolError("Reply Rescue selection binding");
  return {
    schema_version: 2,
    identity_assurance: "selected_excerpt_unverified",
    selection_binding: binding,
    ...fields,
  };
}

function replyRescueOutput(value: unknown): ReplyRescueOutput | null {
  if (value === null) return null;
  const raw = objectValue(value, "Reply Rescue output");
  if (
    numberValue(raw.schema_version, "Reply Rescue schema version") !== 1 ||
    stringValue(raw.workflow, "Reply Rescue workflow") !== "reply_rescue" ||
    stringValue(raw.action_capability, "Reply Rescue action capability") !== "none"
  ) {
    return protocolError("Reply Rescue prepared-artifact contract");
  }
  const replyBody = stringValue(raw.reply_body, "Reply Rescue body");
  if (!replyBody.trim()) return protocolError("Reply Rescue body");
  const claims = arrayValue(raw.claims, "Reply Rescue claims").map((value) => {
    const claim = objectValue(value, "Reply Rescue claim");
    const rawSupport = stringValue(claim.support, "Reply Rescue claim support");
    if (rawSupport !== "conversation" && rawSupport !== "user_direction") {
      return protocolError("Reply Rescue claim support");
    }
    const support: "conversation" | "user_direction" = rawSupport;
    return {
      text: stringValue(claim.text, "Reply Rescue claim text"),
      support,
    };
  });
  return {
    schema_version: 1,
    workflow: "reply_rescue",
    action_capability: "none",
    reply_body: replyBody,
    addressed_questions: stringArray(
      raw.addressed_questions,
      "Reply Rescue addressed questions",
    ),
    unresolved_questions: stringArray(
      raw.unresolved_questions,
      "Reply Rescue unresolved questions",
    ),
    assumptions: stringArray(raw.assumptions, "Reply Rescue assumptions"),
    warnings: stringArray(raw.warnings, "Reply Rescue warnings"),
    claims,
  };
}

function replyRescueSummary(value: unknown): ReplyRescueJobSummary {
  const raw = objectValue(value, "Reply Rescue summary");
  const sourceKind = allowedString(
    raw.source_kind,
    replyRescueSourceKinds,
    "Reply Rescue source kind",
  );
  const assurance = allowedString(
    raw.identity_assurance,
    replyRescueIdentityAssurances,
    "Reply Rescue identity assurance",
  );
  if (
    (sourceKind === "manual_conversation" && assurance !== "manual_unverified") ||
    (sourceKind === "macos_selection" && assurance !== "selected_excerpt_unverified")
  ) {
    return protocolError("Reply Rescue source assurance");
  }
  return {
    id: stringValue(raw.id, "Reply Rescue id"),
    status: replyRescueStatus(raw.status),
    source_kind: sourceKind,
    conversation_preview: stringValue(raw.conversation_preview, "Reply Rescue preview"),
    identity_assurance: assurance,
    model_identity: stringValue(raw.model_identity, "Reply Rescue model"),
    provider_location: replyRescueProviderLocation(raw.provider_location),
    output_edited: booleanValue(raw.output_edited, "Reply Rescue edited state"),
    error_code: stringValue(raw.error_code, "Reply Rescue error code"),
    attempt_count: numberValue(raw.attempt_count, "Reply Rescue attempt count"),
    created_at: stringValue(raw.created_at, "Reply Rescue created time"),
    updated_at: stringValue(raw.updated_at, "Reply Rescue updated time"),
    version: numberValue(raw.version, "Reply Rescue version"),
  };
}

function replyRescueJob(value: unknown): ReplyRescueJob {
  const raw = objectValue(value, "Reply Rescue job");
  const sourceKind = allowedString(
    raw.source_kind,
    replyRescueSourceKinds,
    "Reply Rescue source kind",
  );
  const status = replyRescueStatus(raw.status);
  const output = replyRescueOutput(raw.output);
  if ((status === "ready") !== (output !== null)) {
    return protocolError("Reply Rescue output state");
  }
  return {
    id: stringValue(raw.id, "Reply Rescue id"),
    status,
    source_kind: sourceKind,
    source: replyRescueSource(raw.source, sourceKind),
    model_identity: stringValue(raw.model_identity, "Reply Rescue model"),
    provider_location: replyRescueProviderLocation(raw.provider_location),
    output,
    output_edited: booleanValue(raw.output_edited, "Reply Rescue edited state"),
    error_code: stringValue(raw.error_code, "Reply Rescue error code"),
    attempt_count: numberValue(raw.attempt_count, "Reply Rescue attempt count"),
    created_at: stringValue(raw.created_at, "Reply Rescue created time"),
    updated_at: stringValue(raw.updated_at, "Reply Rescue updated time"),
    version: numberValue(raw.version, "Reply Rescue version"),
  };
}

function resumeDigest(value: unknown, detail: string): string {
  const result = stringValue(value, detail);
  if (!/^[0-9a-f]{64}$/.test(result)) return protocolError(detail);
  return result;
}

function resumePositiveInteger(value: unknown, detail: string): number {
  const result = numberValue(value, detail);
  if (!Number.isSafeInteger(result) || result < 1) return protocolError(detail);
  return result;
}

function jsonResumeUpstream(value: unknown): JsonResumeUpstreamSchema {
  const raw = closedObject(
    value,
    ["version", "commit", "url"],
    "JSON Resume upstream schema",
  );
  const version = stringValue(raw.version, "JSON Resume upstream version");
  const commit = stringValue(raw.commit, "JSON Resume upstream commit");
  const url = stringValue(raw.url, "JSON Resume upstream URL");
  if (
    version !== "v1.0.0" ||
    commit !== "272929d51b450dbd5a0d242af24c60252904f405" ||
    url !==
      "https://raw.githubusercontent.com/jsonresume/jsonresume.org/272929d51b450dbd5a0d242af24c60252904f405/packages/schema/schema.json"
  ) {
    return protocolError("JSON Resume upstream schema binding");
  }
  return { version, commit, url };
}

function resumeSection(value: unknown): ResumeSectionKind {
  return allowedString(value, resumeSections, "Résumé Rescue section");
}

function resumeProvenance(value: unknown): ResumeProvenance {
  const base = objectValue(value, "Résumé Rescue provenance");
  const kind = stringValue(base.kind, "Résumé Rescue provenance kind");
  if (kind === "manual_reviewed") {
    const raw = closedObject(value, ["kind", "reviewed_at"], "manual résumé provenance");
    return {
      kind,
      reviewed_at: stringValue(raw.reviewed_at, "résumé review time"),
    };
  }
  if (kind === "document_excerpt") {
    const raw = closedObject(
      value,
      [
        "kind",
        "reviewed_at",
        "source_id",
        "source_digest",
        "page",
        "section",
        "start",
        "end",
        "extraction_method",
      ],
      "document résumé provenance",
    );
    const page = numberValue(raw.page, "résumé source page");
    const start = numberValue(raw.start, "résumé source start");
    const end = numberValue(raw.end, "résumé source end");
    if (![page, start, end].every(Number.isSafeInteger) || page < 0 || start < 0 || end <= start) {
      return protocolError("document résumé provenance span");
    }
    return {
      kind,
      reviewed_at: stringValue(raw.reviewed_at, "résumé review time"),
      source_id: stringValue(raw.source_id, "résumé source id"),
      source_digest: resumeDigest(raw.source_digest, "résumé source digest"),
      page,
      section: stringValue(raw.section, "résumé source section"),
      start,
      end,
      extraction_method: stringValue(raw.extraction_method, "résumé extraction method"),
    };
  }
  if (kind === "reviewed_memory") {
    const raw = closedObject(
      value,
      ["kind", "reviewed_at", "memory_id", "memory_path", "memory_digest"],
      "memory résumé provenance",
    );
    return {
      kind,
      reviewed_at: stringValue(raw.reviewed_at, "résumé review time"),
      memory_id: stringValue(raw.memory_id, "résumé memory id"),
      memory_path: stringValue(raw.memory_path, "résumé memory path"),
      memory_digest: resumeDigest(raw.memory_digest, "résumé memory digest"),
    };
  }
  if (kind === "json_resume_field") {
    const raw = closedObject(
      value,
      [
        "kind",
        "reviewed_at",
        "source_id",
        "source_digest",
        "json_pointer",
        "value_digest",
        "mapping",
        "upstream_schema_version",
      ],
      "JSON Resume provenance",
    );
    const mapping = allowedString(
      raw.mapping,
      jsonResumeMappings,
      "JSON Resume mapping",
    );
    if (stringValue(raw.upstream_schema_version, "JSON Resume schema version") !== "v1.0.0") {
      return protocolError("JSON Resume schema version");
    }
    return {
      kind,
      reviewed_at: stringValue(raw.reviewed_at, "résumé review time"),
      source_id: stringValue(raw.source_id, "JSON Resume source id"),
      source_digest: resumeDigest(raw.source_digest, "JSON Resume source digest"),
      json_pointer: stringValue(raw.json_pointer, "JSON Resume pointer"),
      value_digest: resumeDigest(raw.value_digest, "JSON Resume value digest"),
      mapping,
      upstream_schema_version: "v1.0.0",
    };
  }
  return protocolError("Résumé Rescue provenance kind");
}

function resumeConflict(value: unknown): ResumeConflict {
  const raw = closedObject(
    value,
    ["id", "fact_ids", "description"],
    "Résumé Rescue conflict",
  );
  return {
    id: stringValue(raw.id, "Résumé Rescue conflict id"),
    fact_ids: stringArray(raw.fact_ids, "Résumé Rescue conflict facts"),
    description: stringValue(raw.description, "Résumé Rescue conflict description"),
  };
}

function resumeFact(value: unknown): ResumeFact {
  const raw = closedObject(
    value,
    ["id", "section", "text", "confidentiality", "ownership_scope", "provenance"],
    "Résumé Rescue fact",
  );
  return {
    id: stringValue(raw.id, "Résumé Rescue fact id"),
    section: resumeSection(raw.section),
    text: stringValue(raw.text, "Résumé Rescue fact text"),
    confidentiality: allowedString(
      raw.confidentiality,
      resumeConfidentialities,
      "Résumé Rescue confidentiality",
    ),
    ownership_scope: allowedString(
      raw.ownership_scope,
      resumeOwnerships,
      "Résumé Rescue ownership",
    ),
    provenance: arrayValue(raw.provenance, "Résumé Rescue provenance list").map(
      resumeProvenance,
    ),
  };
}

function resumeProfile(value: unknown): ResumeProfile {
  const raw = closedObject(
    value,
    ["schema_version", "profile_id", "display_name", "locale", "facts", "conflicts"],
    "Résumé Rescue profile",
  );
  if (numberValue(raw.schema_version, "Résumé Rescue profile schema") !== 1) {
    return protocolError("Résumé Rescue profile schema");
  }
  return {
    schema_version: 1,
    profile_id: stringValue(raw.profile_id, "Résumé Rescue profile id"),
    display_name: stringValue(raw.display_name, "Résumé Rescue display name"),
    locale: stringValue(raw.locale, "Résumé Rescue locale"),
    facts: arrayValue(raw.facts, "Résumé Rescue facts").map(resumeFact),
    conflicts: arrayValue(raw.conflicts, "Résumé Rescue conflicts").map(resumeConflict),
  };
}

function resumeProfileVersion(value: unknown): ResumeProfileVersion {
  const raw = closedObject(
    value,
    ["id", "version", "digest", "created_at", "profile"],
    "Résumé Rescue profile version",
  );
  const profile = resumeProfile(raw.profile);
  const id = stringValue(raw.id, "Résumé Rescue profile version id");
  if (profile.profile_id !== id) return protocolError("Résumé Rescue profile identity");
  return {
    id,
    version: resumePositiveInteger(raw.version, "Résumé Rescue profile version"),
    digest: resumeDigest(raw.digest, "Résumé Rescue profile digest"),
    created_at: stringValue(raw.created_at, "Résumé Rescue profile created time"),
    profile,
  };
}

function resumeOpportunitySource(value: unknown): ResumeOpportunitySource {
  const raw = closedObject(
    value,
    [
      "schema_version",
      "employer",
      "title",
      "source_url",
      "source_text",
      "priorities",
      "locale",
      "captured_at",
    ],
    "Résumé Rescue opportunity source",
  );
  if (numberValue(raw.schema_version, "Résumé Rescue opportunity schema") !== 1) {
    return protocolError("Résumé Rescue opportunity schema");
  }
  return {
    schema_version: 1,
    employer: stringValue(raw.employer, "Résumé Rescue employer"),
    title: stringValue(raw.title, "Résumé Rescue title"),
    source_url: stringValue(raw.source_url, "Résumé Rescue source URL"),
    source_text: stringValue(raw.source_text, "Résumé Rescue source text"),
    priorities: stringArray(raw.priorities, "Résumé Rescue priorities"),
    locale: stringValue(raw.locale, "Résumé Rescue opportunity locale"),
    captured_at: stringValue(raw.captured_at, "Résumé Rescue capture time"),
  };
}

function resumeOpportunity(value: unknown): ResumeOpportunity {
  const raw = closedObject(
    value,
    ["id", "digest", "created_at", "snapshot"],
    "Résumé Rescue opportunity",
  );
  return {
    id: stringValue(raw.id, "Résumé Rescue opportunity id"),
    digest: resumeDigest(raw.digest, "Résumé Rescue opportunity digest"),
    created_at: stringValue(raw.created_at, "Résumé Rescue opportunity created time"),
    snapshot: resumeOpportunitySource(raw.snapshot),
  };
}

function resumeProjectionRequest(value: unknown): ResumeProjectionRequest {
  const raw = closedObject(
    value,
    ["schema_version", "sections", "requirements"],
    "Résumé Rescue projection request",
  );
  if (numberValue(raw.schema_version, "Résumé Rescue request schema") !== 1) {
    return protocolError("Résumé Rescue request schema");
  }
  const sections = arrayValue(raw.sections, "Résumé Rescue requested sections").map((value) => {
    const item = closedObject(value, ["kind", "fact_ids"], "Résumé Rescue requested section");
    return {
      kind: resumeSection(item.kind),
      fact_ids: stringArray(item.fact_ids, "Résumé Rescue selected facts"),
    };
  });
  const requirements: ResumeRequirementRequest[] = arrayValue(
    raw.requirements,
    "Résumé Rescue requirements",
  ).map((value) => {
    const item = closedObject(
      value,
      ["id", "text", "fact_ids"],
      "Résumé Rescue requirement",
    );
    return {
      id: stringValue(item.id, "Résumé Rescue requirement id"),
      text: stringValue(item.text, "Résumé Rescue requirement text"),
      fact_ids: stringArray(item.fact_ids, "Résumé Rescue mapped facts"),
    };
  });
  return { schema_version: 1, sections, requirements };
}

function resumeArtifact(value: unknown): ResumeRescueArtifact {
  const raw = closedObject(
    value,
    [
      "schema_version",
      "workflow",
      "action_capability",
      "generation_mode",
      "profile_binding",
      "opportunity_binding",
      "sections",
      "requirement_coverage",
      "conflicts",
      "missing_evidence",
      "excluded_fact_ids",
      "warnings",
    ],
    "Résumé Rescue artifact",
  );
  if (
    numberValue(raw.schema_version, "Résumé Rescue artifact schema") !== 1 ||
    stringValue(raw.workflow, "Résumé Rescue workflow") !== "resume_rescue" ||
    stringValue(raw.action_capability, "Résumé Rescue action capability") !== "none" ||
    stringValue(raw.generation_mode, "Résumé Rescue generation mode") !==
      "deterministic_exact_projection"
  ) {
    return protocolError("Résumé Rescue artifact identity");
  }
  const profileBinding = closedObject(
    raw.profile_binding,
    ["id", "version", "digest"],
    "Résumé Rescue profile binding",
  );
  const opportunityBinding = closedObject(
    raw.opportunity_binding,
    ["id", "digest", "employer", "title"],
    "Résumé Rescue opportunity binding",
  );
  const selectedIds = new Set<string>();
  const sections = arrayValue(raw.sections, "Résumé Rescue artifact sections").map((value) => {
    const section = closedObject(value, ["kind", "items"], "Résumé Rescue artifact section");
    const items = arrayValue(section.items, "Résumé Rescue artifact items").map((value) => {
      const item = closedObject(
        value,
        [
          "fact_id",
          "text",
          "transformation",
          "confidentiality",
          "ownership_scope",
          "provenance",
        ],
        "Résumé Rescue artifact item",
      );
      const factId = stringValue(item.fact_id, "Résumé Rescue artifact fact id");
      if (selectedIds.has(factId)) return protocolError("Résumé Rescue duplicate selected fact");
      selectedIds.add(factId);
      if (stringValue(item.transformation, "Résumé Rescue transformation") !== "selected_exact") {
        return protocolError("Résumé Rescue transformation");
      }
      return {
        fact_id: factId,
        text: stringValue(item.text, "Résumé Rescue artifact fact text"),
        transformation: "selected_exact" as const,
        confidentiality: allowedString(
          item.confidentiality,
          resumeConfidentialities,
          "Résumé Rescue artifact confidentiality",
        ),
        ownership_scope: allowedString(
          item.ownership_scope,
          resumeOwnerships,
          "Résumé Rescue artifact ownership",
        ),
        provenance: arrayValue(item.provenance, "Résumé Rescue artifact provenance").map(
          resumeProvenance,
        ),
      };
    });
    return { kind: resumeSection(section.kind), items };
  });
  const requirementCoverage: ResumeRequirementCoverage[] = arrayValue(
    raw.requirement_coverage,
    "Résumé Rescue requirement coverage",
  ).map((value) => {
    const item = closedObject(
      value,
      ["id", "text", "status", "fact_ids", "support_assurance"],
      "Résumé Rescue requirement coverage item",
    );
    const status = stringValue(item.status, "Résumé Rescue requirement status");
    const assurance = stringValue(item.support_assurance, "Résumé Rescue support assurance");
    const factIds = stringArray(item.fact_ids, "Résumé Rescue coverage facts");
    if (factIds.some((factId) => !selectedIds.has(factId))) {
      return protocolError("Résumé Rescue coverage fact binding");
    }
    if (
      (status === "candidate_supported" &&
        assurance === "manual_mapping_unverified" &&
        factIds.length > 0) ||
      (status === "missing_evidence" && assurance === "no_evidence" && factIds.length === 0)
    ) {
      return {
        id: stringValue(item.id, "Résumé Rescue coverage id"),
        text: stringValue(item.text, "Résumé Rescue coverage text"),
        status,
        fact_ids: factIds,
        support_assurance: assurance,
      } as ResumeRequirementCoverage;
    }
    return protocolError("Résumé Rescue coverage assurance");
  });
  const conflicts = arrayValue(raw.conflicts, "Résumé Rescue artifact conflicts").map(
    resumeConflict,
  );
  const missingEvidence = arrayValue(
    raw.missing_evidence,
    "Résumé Rescue missing evidence",
  ).map((value) => {
    const item = closedObject(
      value,
      ["requirement_id", "text"],
      "Résumé Rescue missing-evidence item",
    );
    return {
      requirement_id: stringValue(item.requirement_id, "Résumé Rescue missing requirement id"),
      text: stringValue(item.text, "Résumé Rescue missing requirement text"),
    };
  });
  const expectedMissing = requirementCoverage
    .filter((item) => item.status === "missing_evidence")
    .map((item) => ({ requirement_id: item.id, text: item.text }));
  if (JSON.stringify(missingEvidence) !== JSON.stringify(expectedMissing)) {
    return protocolError("Résumé Rescue missing-evidence ledger");
  }
  const excludedFactIds = stringArray(raw.excluded_fact_ids, "Résumé Rescue excluded facts");
  if (excludedFactIds.some((factId) => selectedIds.has(factId))) {
    return protocolError("Résumé Rescue selected/excluded fact binding");
  }
  return {
    schema_version: 1,
    workflow: "resume_rescue",
    action_capability: "none",
    generation_mode: "deterministic_exact_projection",
    profile_binding: {
      id: stringValue(profileBinding.id, "Résumé Rescue profile binding id"),
      version: resumePositiveInteger(profileBinding.version, "Résumé Rescue bound profile version"),
      digest: resumeDigest(profileBinding.digest, "Résumé Rescue bound profile digest"),
    },
    opportunity_binding: {
      id: stringValue(opportunityBinding.id, "Résumé Rescue bound opportunity id"),
      digest: resumeDigest(opportunityBinding.digest, "Résumé Rescue bound opportunity digest"),
      employer: stringValue(opportunityBinding.employer, "Résumé Rescue bound employer"),
      title: stringValue(opportunityBinding.title, "Résumé Rescue bound title"),
    },
    sections,
    requirement_coverage: requirementCoverage,
    conflicts,
    missing_evidence: missingEvidence,
    excluded_fact_ids: excludedFactIds,
    warnings: stringArray(raw.warnings, "Résumé Rescue warnings"),
  };
}

function resumeProjection(value: unknown): ResumeProjection {
  const raw = closedObject(
    value,
    [
      "id",
      "profile_id",
      "profile_version",
      "profile_digest",
      "opportunity_id",
      "opportunity_digest",
      "request",
      "artifact",
      "artifact_digest",
      "created_at",
    ],
    "Résumé Rescue projection",
  );
  const request = resumeProjectionRequest(raw.request);
  const artifact = resumeArtifact(raw.artifact);
  const profileId = stringValue(raw.profile_id, "Résumé Rescue projection profile id");
  const profileVersion = resumePositiveInteger(
    raw.profile_version,
    "Résumé Rescue projection profile version",
  );
  const profileDigest = resumeDigest(raw.profile_digest, "Résumé Rescue projection profile digest");
  const opportunityId = stringValue(raw.opportunity_id, "Résumé Rescue projection opportunity id");
  const opportunityDigest = resumeDigest(
    raw.opportunity_digest,
    "Résumé Rescue projection opportunity digest",
  );
  if (
    artifact.profile_binding.id !== profileId ||
    artifact.profile_binding.version !== profileVersion ||
    artifact.profile_binding.digest !== profileDigest ||
    artifact.opportunity_binding.id !== opportunityId ||
    artifact.opportunity_binding.digest !== opportunityDigest
  ) {
    return protocolError("Résumé Rescue projection binding");
  }
  if (
    JSON.stringify(request.sections) !==
      JSON.stringify(
        artifact.sections.map((section) => ({
          kind: section.kind,
          fact_ids: section.items.map((item) => item.fact_id),
        })),
      ) ||
    JSON.stringify(request.requirements) !==
      JSON.stringify(
        artifact.requirement_coverage.map((item) => ({
          id: item.id,
          text: item.text,
          fact_ids: item.fact_ids,
        })),
      )
  ) {
    return protocolError("Résumé Rescue request/artifact binding");
  }
  return {
    id: stringValue(raw.id, "Résumé Rescue projection id"),
    profile_id: profileId,
    profile_version: profileVersion,
    profile_digest: profileDigest,
    opportunity_id: opportunityId,
    opportunity_digest: opportunityDigest,
    request,
    artifact,
    artifact_digest: resumeDigest(raw.artifact_digest, "Résumé Rescue artifact digest"),
    created_at: stringValue(raw.created_at, "Résumé Rescue projection created time"),
  };
}

export function normalizeResumeRescueState(value: unknown): ResumeRescueState {
  const raw = closedObject(
    value,
    ["enabled", "profiles", "opportunities", "projections"],
    "Résumé Rescue state",
  );
  return {
    enabled: booleanValue(raw.enabled, "Résumé Rescue enabled state"),
    profiles: arrayValue(raw.profiles, "Résumé Rescue profiles").map(resumeProfileVersion),
    opportunities: arrayValue(raw.opportunities, "Résumé Rescue opportunities").map(
      resumeOpportunity,
    ),
    projections: arrayValue(raw.projections, "Résumé Rescue projections").map(resumeProjection),
  };
}

export function normalizeResumeProfileMutation(value: unknown): {
  profile: ResumeProfileVersion;
  created: boolean;
} {
  const raw = closedObject(value, ["profile", "created"], "Résumé Rescue profile response");
  return {
    profile: resumeProfileVersion(raw.profile),
    created: booleanValue(raw.created, "Résumé Rescue profile created state"),
  };
}

export function normalizeResumeOpportunityMutation(value: unknown): {
  opportunity: ResumeOpportunity;
  created: boolean;
} {
  const raw = closedObject(
    value,
    ["opportunity", "created"],
    "Résumé Rescue opportunity response",
  );
  return {
    opportunity: resumeOpportunity(raw.opportunity),
    created: booleanValue(raw.created, "Résumé Rescue opportunity created state"),
  };
}

export function normalizeResumeProjectionMutation(value: unknown): {
  projection: ResumeProjection;
  created: boolean;
} {
  const raw = closedObject(
    value,
    ["projection", "created"],
    "Résumé Rescue projection response",
  );
  return {
    projection: resumeProjection(raw.projection),
    created: booleanValue(raw.created, "Résumé Rescue projection created state"),
  };
}

export function normalizeResumePreview(value: unknown): ResumePreview {
  const response = closedObject(value, ["preview"], "Résumé Rescue preview response");
  const raw = closedObject(
    response.preview,
    [
      "schema_version",
      "projection_id",
      "artifact_digest",
      "renderer_version",
      "template_id",
      "html",
      "plain_text",
      "document_digest",
      "action_capability",
    ],
    "Résumé Rescue preview",
  );
  const html = stringValue(raw.html, "Résumé Rescue preview HTML");
  if (
    numberValue(raw.schema_version, "Résumé Rescue preview schema") !== 1 ||
    numberValue(raw.renderer_version, "Résumé Rescue renderer version") !== 1 ||
    stringValue(raw.template_id, "Résumé Rescue template") !== "openchronicle-classic-v1" ||
    stringValue(raw.action_capability, "Résumé Rescue preview action") !== "none" ||
    !html.startsWith("<!doctype html>\n") ||
    !html.includes("default-src 'none'") ||
    /<(?:script|iframe|object|embed|form|link|img|base)\b/i.test(html)
  ) {
    return protocolError("Résumé Rescue preview contract");
  }
  return {
    schema_version: 1,
    projection_id: stringValue(raw.projection_id, "Résumé Rescue preview projection id"),
    artifact_digest: resumeDigest(raw.artifact_digest, "Résumé Rescue preview artifact digest"),
    renderer_version: 1,
    template_id: "openchronicle-classic-v1",
    html,
    plain_text: stringValue(raw.plain_text, "Résumé Rescue preview plain text"),
    document_digest: resumeDigest(raw.document_digest, "Résumé Rescue document digest"),
    action_capability: "none",
  };
}

export function normalizeResumeHtmlExport(value: unknown): ResumeHtmlExportResult {
  const raw = closedObject(
    value,
    [
      "schema_version",
      "projection_id",
      "document_digest",
      "file_name",
      "byte_count",
      "created",
      "action_capability",
    ],
    "Résumé Rescue HTML export result",
  );
  const byteCount = numberValue(raw.byte_count, "Résumé Rescue export byte count");
  if (
    numberValue(raw.schema_version, "Résumé Rescue export schema") !== 1 ||
    booleanValue(raw.created, "Résumé Rescue export created state") !== true ||
    stringValue(raw.action_capability, "Résumé Rescue export action") !== "none" ||
    !Number.isSafeInteger(byteCount) ||
    byteCount < 1
  ) {
    return protocolError("Résumé Rescue HTML export contract");
  }
  return {
    schema_version: 1,
    projection_id: stringValue(raw.projection_id, "Résumé Rescue export projection id"),
    document_digest: resumeDigest(raw.document_digest, "Résumé Rescue export digest"),
    file_name: stringValue(raw.file_name, "Résumé Rescue export file name"),
    byte_count: byteCount,
    created: true,
    action_capability: "none",
  };
}

export function normalizeResumeDocxExport(value: unknown): ResumeDocxExportResult {
  const raw = closedObject(
    value,
    [
      "schema_version",
      "projection_id",
      "artifact_digest",
      "preview_document_digest",
      "content_digest",
      "format",
      "file_name",
      "byte_count",
      "created",
      "action_capability",
    ],
    "Résumé Rescue DOCX export result",
  );
  const byteCount = numberValue(raw.byte_count, "Résumé Rescue DOCX byte count");
  const fileName = stringValue(raw.file_name, "Résumé Rescue DOCX file name");
  if (
    numberValue(raw.schema_version, "Résumé Rescue DOCX schema") !== 1 ||
    stringValue(raw.format, "Résumé Rescue DOCX format") !== "docx" ||
    booleanValue(raw.created, "Résumé Rescue DOCX created state") !== true ||
    stringValue(raw.action_capability, "Résumé Rescue DOCX action") !== "none" ||
    !fileName.toLowerCase().endsWith(".docx") ||
    !Number.isSafeInteger(byteCount) ||
    byteCount < 1 ||
    byteCount > 4 * 1024 * 1024
  ) {
    return protocolError("Résumé Rescue DOCX export contract");
  }
  return {
    schema_version: 1,
    projection_id: stringValue(raw.projection_id, "Résumé Rescue DOCX projection id"),
    artifact_digest: resumeDigest(raw.artifact_digest, "Résumé Rescue DOCX artifact digest"),
    preview_document_digest: resumeDigest(
      raw.preview_document_digest,
      "Résumé Rescue DOCX preview digest",
    ),
    content_digest: resumeDigest(raw.content_digest, "Résumé Rescue DOCX content digest"),
    format: "docx",
    file_name: fileName,
    byte_count: byteCount,
    created: true,
    action_capability: "none",
  };
}

export function normalizeOpenedJsonResumeReview(value: unknown): OpenedJsonResumeReview {
  const response = closedObject(
    value,
    ["source_text", "review"],
    "opened JSON Resume review",
  );
  const sourceText = stringValue(response.source_text, "JSON Resume source text");
  const raw = closedObject(
    response.review,
    [
      "schema_version",
      "format",
      "upstream_schema",
      "source",
      "display_name_candidate",
      "candidates",
      "omissions",
      "unknown_fields",
      "warnings",
      "action_capability",
      "review_digest",
    ],
    "JSON Resume review",
  );
  const source = closedObject(
    raw.source,
    ["id", "digest", "byte_count"],
    "JSON Resume source binding",
  );
  const sourceByteCount = numberValue(source.byte_count, "JSON Resume source byte count");
  if (
    numberValue(raw.schema_version, "JSON Resume review schema") !== 1 ||
    stringValue(raw.format, "JSON Resume review format") !== "json_resume_v1" ||
    stringValue(raw.action_capability, "JSON Resume review action") !== "none" ||
    !Number.isSafeInteger(sourceByteCount) ||
    sourceByteCount !== new TextEncoder().encode(sourceText).length ||
    sourceByteCount > 500_000
  ) {
    return protocolError("JSON Resume review contract");
  }
  const candidates = arrayValue(raw.candidates, "JSON Resume candidates").map((value) => {
    const candidate = closedObject(
      value,
      [
        "id",
        "suggested_section",
        "suggested_text",
        "mapping",
        "source_fields",
        "review_status",
      ],
      "JSON Resume candidate",
    );
    const reviewStatus = stringValue(candidate.review_status, "JSON Resume review status");
    if (reviewStatus !== "unreviewed") return protocolError("JSON Resume review status");
    return {
      id: stringValue(candidate.id, "JSON Resume candidate id"),
      suggested_section: resumeSection(candidate.suggested_section),
      suggested_text: stringValue(candidate.suggested_text, "JSON Resume candidate text"),
      mapping: allowedString(candidate.mapping, jsonResumeMappings, "JSON Resume mapping"),
      source_fields: arrayValue(candidate.source_fields, "JSON Resume source fields").map(
        (value) => {
          const field = closedObject(
            value,
            ["pointer", "value"],
            "JSON Resume source field",
          );
          return {
            pointer: stringValue(field.pointer, "JSON Resume source pointer"),
            value: stringValue(field.value, "JSON Resume source value"),
          };
        },
      ),
      review_status: "unreviewed" as const,
    };
  });
  const omissions = arrayValue(raw.omissions, "JSON Resume omissions").map((value) => {
    const omission = closedObject(
      value,
      ["pointer", "reason", "value_digest"],
      "JSON Resume omission",
    );
    return {
      pointer: stringValue(omission.pointer, "JSON Resume omission pointer"),
      reason: stringValue(omission.reason, "JSON Resume omission reason"),
      value_digest: resumeDigest(omission.value_digest, "JSON Resume omission digest"),
    };
  });
  const review: JsonResumeImportReview = {
    schema_version: 1,
    format: "json_resume_v1",
    upstream_schema: jsonResumeUpstream(raw.upstream_schema),
    source: {
      id: stringValue(source.id, "JSON Resume source id"),
      digest: resumeDigest(source.digest, "JSON Resume source digest"),
      byte_count: sourceByteCount,
    },
    display_name_candidate: stringValue(
      raw.display_name_candidate,
      "JSON Resume display name candidate",
    ),
    candidates,
    omissions,
    unknown_fields: stringArray(raw.unknown_fields, "JSON Resume unknown fields"),
    warnings: stringArray(raw.warnings, "JSON Resume warnings"),
    action_capability: "none",
    review_digest: resumeDigest(raw.review_digest, "JSON Resume review digest"),
  };
  return { source_text: sourceText, review };
}

function boundedResumeDocumentString(
  value: unknown,
  maximum: number,
  detail: string,
): string {
  const result = stringValue(value, detail);
  if (!result || result.length > maximum || result.includes("\0")) {
    return protocolError(detail);
  }
  return result;
}

function resumeDocumentInteger(
  value: unknown,
  minimum: number,
  maximum: number,
  detail: string,
): number {
  const result = numberValue(value, detail);
  if (!Number.isSafeInteger(result) || result < minimum || result > maximum) {
    return protocolError(detail);
  }
  return result;
}

function resumeDocumentLocator(value: unknown, format: "pdf" | "docx"): ResumeDocumentLocator {
  if (format === "pdf") {
    const raw = closedObject(
      value,
      ["kind", "page", "section", "start", "end", "bbox"],
      "résumé PDF candidate locator",
    );
    if (stringValue(raw.kind, "résumé PDF locator kind") !== "page_bbox") {
      return protocolError("résumé PDF locator kind");
    }
    const page = resumeDocumentInteger(raw.page, 1, 50, "résumé PDF locator page");
    const start = resumeDocumentInteger(raw.start, 0, 9_999_999, "résumé locator start");
    const end = resumeDocumentInteger(raw.end, 1, 10_000_000, "résumé locator end");
    const values = arrayValue(raw.bbox, "résumé PDF locator bounds");
    if (end <= start || values.length !== 4) return protocolError("résumé PDF locator bounds");
    const bbox = values.map((item) => numberValue(item, "résumé PDF locator coordinate"));
    if (
      bbox.some((item) => item < 0 || item > 100_000) ||
      bbox[0]! > bbox[2]! ||
      bbox[1]! > bbox[3]!
    ) {
      return protocolError("résumé PDF locator bounds");
    }
    return {
      kind: "page_bbox",
      page,
      section: boundedResumeDocumentString(raw.section, 512, "résumé locator section"),
      start,
      end,
      bbox: [bbox[0]!, bbox[1]!, bbox[2]!, bbox[3]!],
    };
  }
  const raw = closedObject(
    value,
    ["kind", "page", "section", "start", "end", "part", "block", "block_kind"],
    "résumé DOCX candidate locator",
  );
  if (
    stringValue(raw.kind, "résumé DOCX locator kind") !== "part_block" ||
    numberValue(raw.page, "résumé DOCX locator page") !== 0
  ) {
    return protocolError("résumé DOCX locator kind");
  }
  const start = resumeDocumentInteger(raw.start, 0, 9_999_999, "résumé locator start");
  const end = resumeDocumentInteger(raw.end, 1, 10_000_000, "résumé locator end");
  if (end <= start) return protocolError("résumé DOCX locator span");
  const part = boundedResumeDocumentString(raw.part, 256, "résumé DOCX locator part");
  const blockKind = boundedResumeDocumentString(
    raw.block_kind,
    128,
    "résumé DOCX locator block kind",
  );
  if (
    !/^word\/(?:document|header\d+|footer\d+)\.xml$/.test(part) ||
    (blockKind !== "paragraph" && !/^table_row_\d+$/.test(blockKind))
  ) {
    return protocolError("résumé DOCX locator binding");
  }
  return {
    kind: "part_block",
    page: 0,
    section: boundedResumeDocumentString(raw.section, 512, "résumé locator section"),
    start,
    end,
    part,
    block: resumeDocumentInteger(raw.block, 0, 100_000, "résumé DOCX locator block"),
    block_kind: blockKind,
  };
}

export function normalizeOpenedResumeDocumentReview(
  value: unknown,
): OpenedResumeDocumentReview {
  const response = closedObject(
    value,
    ["review_token", "review"],
    "opened résumé document review",
  );
  const reviewToken = stringValue(response.review_token, "résumé document review token");
  if (!/^[a-f0-9]{32}$/.test(reviewToken)) {
    return protocolError("résumé document review token");
  }
  const raw = closedObject(
    response.review,
    [
      "schema_version",
      "format",
      "extractor",
      "source",
      "candidates",
      "omissions",
      "warnings",
      "action_capability",
      "review_digest",
    ],
    "résumé document review",
  );
  const format = stringValue(raw.format, "résumé document format");
  if (format !== "pdf" && format !== "docx") {
    return protocolError("résumé document format");
  }
  const extractor = closedObject(
    raw.extractor,
    ["version", "method"],
    "résumé document extractor",
  );
  const method = boundedResumeDocumentString(
    extractor.method,
    128,
    "résumé document extraction method",
  );
  if (
    numberValue(raw.schema_version, "résumé document review schema") !== 1 ||
    numberValue(extractor.version, "résumé document extractor version") !== 1 ||
    stringValue(raw.action_capability, "résumé document review action") !== "none" ||
    (format === "docx"
      ? method !== "ooxml-bounded-blocks-v1"
      : !/^pdfplumber-\d+(?:\.\d+){1,3}-geometry-v1$/.test(method))
  ) {
    return protocolError("résumé document review contract");
  }
  const source = closedObject(
    raw.source,
    ["id", "digest", "byte_count"],
    "résumé document source binding",
  );
  const sourceDigest = resumeDigest(source.digest, "résumé document source digest");
  const sourceId = stringValue(source.id, "résumé document source id");
  const sourceByteCount = resumeDocumentInteger(
    source.byte_count,
    1,
    8 * 1024 * 1024,
    "résumé document source byte count",
  );
  if (sourceId !== `resume-document-${sourceDigest.slice(0, 32)}`) {
    return protocolError("résumé document source binding");
  }
  const candidateValues = arrayValue(raw.candidates, "résumé document candidates");
  if (candidateValues.length > 2_000) return protocolError("résumé document candidates");
  const candidateIds = new Set<string>();
  let extractedCharacters = 0;
  const candidates = candidateValues.map((value) => {
    const candidate = closedObject(
      value,
      ["id", "text", "text_digest", "locator", "extraction_method", "candidate_digest"],
      "résumé document candidate",
    );
    const candidateDigest = resumeDigest(
      candidate.candidate_digest,
      "résumé document candidate digest",
    );
    const id = stringValue(candidate.id, "résumé document candidate id");
    const text = boundedResumeDocumentString(candidate.text, 8_000, "résumé document text");
    extractedCharacters += text.length;
    if (
      id !== `document-candidate-${candidateDigest.slice(0, 32)}` ||
      candidateIds.has(id) ||
      stringValue(candidate.extraction_method, "résumé document candidate extractor") !== method
    ) {
      return protocolError("résumé document candidate binding");
    }
    candidateIds.add(id);
    return {
      id,
      text,
      text_digest: resumeDigest(candidate.text_digest, "résumé document text digest"),
      locator: resumeDocumentLocator(candidate.locator, format),
      extraction_method: method,
      candidate_digest: candidateDigest,
    };
  });
  if (extractedCharacters > 500_000) {
    return protocolError("résumé document extracted text size");
  }
  const omissionValues = arrayValue(raw.omissions, "résumé document omissions");
  if (omissionValues.length > 100) return protocolError("résumé document omissions");
  const omissions: ResumeDocumentImportReview["omissions"] = omissionValues.map((value) => {
    const object = objectValue(value, "résumé document omission");
    const code = stringValue(object.code, "résumé document omission code");
    if (code === "images_not_extracted") {
      const omission = closedObject(
        object,
        ["code", "count"],
        "résumé document image omission",
      );
      return {
        code,
        count: resumeDocumentInteger(
          omission.count,
          1,
          100_000,
          "résumé document omitted image count",
        ),
      };
    }
    if (code === "supplementary_parts_not_extracted") {
      const omission = closedObject(
        object,
        ["code", "parts"],
        "résumé document part omission",
      );
      const parts = stringArray(omission.parts, "résumé document omitted parts");
      const allowed = new Set([
        "word/comments.xml",
        "word/footnotes.xml",
        "word/endnotes.xml",
      ]);
      if (
        !parts.length ||
        parts.length > allowed.size ||
        new Set(parts).size !== parts.length ||
        parts.some((part) => !allowed.has(part))
      ) {
        return protocolError("résumé document omitted parts");
      }
      return { code, parts };
    }
    return protocolError("résumé document omission code");
  });
  const warningValues = arrayValue(raw.warnings, "résumé document warnings");
  if (warningValues.length > 20) return protocolError("résumé document warnings");
  const warningCodes = new Set<string>();
  const warnings: ResumeDocumentImportReview["warnings"] = warningValues.map((value) => {
    const warning = closedObject(
      value,
      ["code", "message"],
      "résumé document warning",
    );
    const code = allowedString(
      warning.code,
      resumeDocumentWarningCodes,
      "résumé document warning code",
    );
    if (warningCodes.has(code)) return protocolError("résumé document warning duplication");
    warningCodes.add(code);
    return {
      code,
      message: boundedResumeDocumentString(
        warning.message,
        512,
        "résumé document warning message",
      ),
    };
  });
  const review: ResumeDocumentImportReview = {
    schema_version: 1,
    format,
    extractor: { version: 1, method },
    source: { id: sourceId, digest: sourceDigest, byte_count: sourceByteCount },
    candidates,
    omissions,
    warnings,
    action_capability: "none",
    review_digest: resumeDigest(raw.review_digest, "résumé document review digest"),
  };
  return { review_token: reviewToken, review };
}

export function normalizeJsonResumeExport(value: unknown): JsonResumeExport {
  const response = closedObject(value, ["export"], "JSON Resume export response");
  const raw = closedObject(
    response.export,
    [
      "schema_version",
      "format",
      "upstream_schema",
      "projection_binding",
      "profile_binding",
      "document",
      "json_text",
      "document_digest",
      "interoperability_losses",
      "warnings",
      "action_capability",
    ],
    "JSON Resume export",
  );
  if (
    numberValue(raw.schema_version, "JSON Resume export schema") !== 1 ||
    stringValue(raw.format, "JSON Resume export format") !== "json_resume_v1" ||
    stringValue(raw.action_capability, "JSON Resume export action") !== "none"
  ) {
    return protocolError("JSON Resume export contract");
  }
  const projectionBinding = closedObject(
    raw.projection_binding,
    ["id", "artifact_digest"],
    "JSON Resume projection binding",
  );
  const profileBinding = closedObject(
    raw.profile_binding,
    ["id", "version", "digest"],
    "JSON Resume profile binding",
  );
  const document = objectValue(raw.document, "JSON Resume document");
  const jsonText = stringValue(raw.json_text, "JSON Resume text");
  let parsed: unknown;
  try {
    parsed = JSON.parse(jsonText);
  } catch {
    return protocolError("JSON Resume text");
  }
  if (JSON.stringify(parsed) !== JSON.stringify(document)) {
    return protocolError("JSON Resume document binding");
  }
  return {
    schema_version: 1,
    format: "json_resume_v1",
    upstream_schema: jsonResumeUpstream(raw.upstream_schema),
    projection_binding: {
      id: stringValue(projectionBinding.id, "JSON Resume projection id"),
      artifact_digest: resumeDigest(
        projectionBinding.artifact_digest,
        "JSON Resume artifact digest",
      ),
    },
    profile_binding: {
      id: stringValue(profileBinding.id, "JSON Resume profile id"),
      version: resumePositiveInteger(profileBinding.version, "JSON Resume profile version"),
      digest: resumeDigest(profileBinding.digest, "JSON Resume profile digest"),
    },
    document,
    json_text: jsonText,
    document_digest: resumeDigest(raw.document_digest, "JSON Resume document digest"),
    interoperability_losses: arrayValue(
      raw.interoperability_losses,
      "JSON Resume interoperability losses",
    ).map((value) => {
      const loss = closedObject(
        value,
        ["fact_id", "section", "reason"],
        "JSON Resume interoperability loss",
      );
      return {
        fact_id: stringValue(loss.fact_id, "JSON Resume loss fact"),
        section: resumeSection(loss.section),
        reason: stringValue(loss.reason, "JSON Resume loss reason"),
      };
    }),
    warnings: stringArray(raw.warnings, "JSON Resume export warnings"),
    action_capability: "none",
  };
}

export function normalizeResumeJsonExportResult(value: unknown): ResumeJsonExportResult {
  return normalizeResumeHtmlExport(value);
}

function privacySnapshot(raw: JsonRecord, dailyWrap: JsonRecord): PrivacySnapshot {
  return {
    allowed_bundle_ids: stringArray(raw.allowed_bundle_ids, "allowed bundle IDs"),
    excluded_bundle_ids: stringArray(raw.excluded_bundle_ids, "excluded bundle IDs"),
    excluded_app_names: stringArray(raw.excluded_app_names, "excluded app names"),
    excluded_window_title_patterns: stringArray(
      raw.excluded_window_title_patterns,
      "excluded title patterns",
    ),
    deny_unknown_windows: booleanValue(raw.deny_unknown_windows, "unknown-window policy"),
    include_screenshot: booleanValue(raw.include_screenshot, "screenshot policy"),
    buffer_retention_hours: numberValue(
      raw.buffer_retention_hours,
      "capture buffer retention",
    ),
    screenshot_retention_hours: numberValue(
      raw.screenshot_retention_hours,
      "screenshot retention",
    ),
    model_mode: "unknown",
    daily_wrap_enabled: booleanValue(dailyWrap.enabled, "Daily Wrap enabled state"),
    daily_wrap_timezone: stringValue(dailyWrap.timezone, "Daily Wrap configured timezone"),
  };
}

export function normalizeSnapshot(value: unknown): DesktopSnapshot {
  const raw = objectValue(value, "snapshot");
  const daemon = objectValue(raw.daemon, "daemon snapshot");
  const capture = objectValue(raw.capture, "capture snapshot");
  const privacy = objectValue(raw.privacy, "privacy snapshot");
  const counts = objectValue(raw.counts, "snapshot counts");
  const candidateCounts = objectValue(counts.candidates, "candidate counts");
  const dailyWrap = objectValue(raw.daily_wrap, "Daily Wrap snapshot");
  const promptRescue = objectValue(raw.prompt_rescue, "Prompt Rescue snapshot");
  const promptRescueProvider = objectValue(
    promptRescue.provider,
    "Prompt Rescue provider",
  );
  const replyRescue = objectValue(raw.reply_rescue, "Reply Rescue snapshot");
  const replyRescueProvider = objectValue(
    replyRescue.provider,
    "Reply Rescue provider",
  );
  const suggestions = arrayValue(raw.suggestions, "suggestion summaries").map(
    workResumptionSuggestion,
  );
  const running = booleanValue(daemon.running, "daemon running state");
  const paused = booleanValue(capture.paused, "capture paused state");
  const health = stringValue(daemon.health, "daemon health");
  const last = capture.last === null ? null : objectValue(capture.last, "last capture");
  const candidates = arrayValue(raw.candidates, "candidate summaries").map((value) => {
    const item = objectValue(value, "candidate summary");
    const confidence = item.confidence === null ? null : item.confidence === undefined ? undefined : numberValue(item.confidence, "candidate confidence");
    return {
      id: stringValue(item.id, "candidate summary id"),
      status: candidateStatus(item.status),
      kind: stringValue(item.kind, "candidate summary kind"),
      target_path: stringValue(item.target_path, "candidate summary target"),
      content_preview: stringValue(item.content_preview, "candidate content preview"),
      version: numberValue(item.version, "candidate summary version"),
      updated_at: stringValue(item.updated_at, "candidate summary updated time"),
      tags: stringArray(item.tags, "candidate summary tags"),
      ...(confidence === undefined ? {} : { confidence }),
    };
  });
  const dailyWraps = arrayValue(dailyWrap.wraps, "Daily Wrap summaries").map((value) => {
    const item = objectValue(value, "Daily Wrap summary");
    const status = allowedString(
      item.status,
      dailyWrapStatuses,
      "Daily Wrap summary status",
    );
    const coverage = allowedString(
      item.coverage_status,
      dailyWrapCoverageStatuses,
      "Daily Wrap summary coverage",
    );
    const rawCounts = objectValue(item.item_counts, "Daily Wrap item counts");
    const itemCounts: Partial<Record<WrapCategory, number>> = {};
    for (const category of wrapCategories) itemCounts[category] = numberValue(rawCounts[category], `Daily Wrap ${category} count`);
    const revision = numberValue(item.revision, "Daily Wrap summary revision");
    return {
      id: stringValue(item.id, "Daily Wrap summary id"),
      local_date: stringValue(item.local_date, "Daily Wrap summary local date"),
      timezone: stringValue(item.timezone, "Daily Wrap summary timezone"),
      scope: stringValue(item.scope, "Daily Wrap summary scope"),
      status,
      coverage_status: coverage,
      revision,
      has_output: revision > 0,
      item_counts: itemCounts,
    };
  });
  return {
    generated_at: stringValue(raw.generated_at, "snapshot generation time"),
    daemon: {
      state: running ? (health.toLocaleLowerCase() === "healthy" ? "running" : "degraded") : "stopped",
      health,
      ...(daemon.pid === null || daemon.pid === undefined ? {} : { pid: numberValue(daemon.pid, "daemon pid") }),
      uptime: stringValue(daemon.uptime, "daemon uptime"),
    },
    capture: {
      paused,
      state: !running ? "stopped" : paused ? "paused" : "active",
      last_capture_at: last ? stringValue(last.timestamp, "last capture timestamp") : null,
      last_app: last ? stringValue(last.app_name, "last capture app") : null,
    },
    review_counts: {
      pending: numberValue(candidateCounts.pending, "pending candidate count"),
      conflict: numberValue(candidateCounts.conflict, "conflict candidate count"),
      applying: numberValue(candidateCounts.applying, "applying candidate count"),
      accepted: numberValue(candidateCounts.accepted, "accepted candidate count"),
      rejected: numberValue(candidateCounts.rejected, "rejected candidate count"),
    },
    purge_pending_count: 0,
    candidates,
    daily_wraps: dailyWraps,
    suggestions_enabled: booleanValue(
      raw.suggestions_enabled,
      "suggestions enabled state",
    ),
    suggestions,
    prompt_rescue: {
      enabled: booleanValue(promptRescue.enabled, "Prompt Rescue enabled state"),
      provider: {
        model: stringValue(promptRescueProvider.model, "Prompt Rescue configured model"),
        location: promptRescueProviderLocation(promptRescueProvider.location),
      },
      jobs: arrayValue(promptRescue.jobs, "Prompt Rescue summaries").map(
        promptRescueSummary,
      ),
    },
    reply_rescue: {
      enabled: booleanValue(replyRescue.enabled, "Reply Rescue enabled state"),
      provider: {
        model: stringValue(replyRescueProvider.model, "Reply Rescue configured model"),
        location: replyRescueProviderLocation(replyRescueProvider.location),
      },
      jobs: arrayValue(replyRescue.jobs, "Reply Rescue summaries").map(
        replyRescueSummary,
      ),
    },
    timeline: arrayValue(raw.timeline, "timeline snapshot").map(timelineItem),
    privacy: privacySnapshot(privacy, dailyWrap),
    permissions: [],
  };
}

export function normalizeCandidateGet(value: unknown): Candidate {
  const raw = objectValue(value, "candidate response");
  return candidatePayload(raw.candidate, raw.evidence);
}

export function normalizeCandidateMutation(value: unknown): Candidate {
  const raw = objectValue(value, "candidate mutation response");
  return candidatePayload(raw.candidate);
}

export function normalizeDailyWrap(value: unknown): DailyWrap {
  const raw = objectValue(value, "Daily Wrap response");
  return wrapPayload(raw.wrap);
}

export function normalizeProvenance(value: unknown): ProvenanceTrace {
  const raw = objectValue(value, "provenance response");
  const summary = (item: unknown) => {
    const record = objectValue(item, "provenance reference");
    const ref = reference(record);
    const availability = allowedString(
      record.availability,
      provenanceAvailabilityStatuses,
      "provenance availability",
    );
    const integrity = allowedString(
      record.integrity,
      provenanceIntegrityStatuses,
      "provenance integrity",
    );
    return {
      ...ref,
      availability: integrity === "changed" ? "changed" : availability,
    };
  };
  return {
    subject: reference(raw.subject),
    direct_sources: arrayValue(raw.direct_sources, "direct provenance sources").map(summary),
    trace: arrayValue(raw.trace, "provenance trace").map((item) => {
      const node = objectValue(item, "provenance node");
      return { depth: numberValue(node.depth, "provenance depth"), source: summary(node.source) };
    }),
  };
}

export function normalizeEvidence(value: unknown): ResolvedEvidence {
  const raw = objectValue(value, "evidence response");
  const ref = reference(raw.reference);
  const status = allowedString(raw.status, evidenceStatuses, "evidence status");
  const content = raw.content === null ? null : objectValue(raw.content, "evidence content");
  let excerpt = "";
  let appName: string | undefined;
  let windowTitle: string | undefined;
  let startTime: string | undefined;
  let endTime: string | undefined;
  if (content) {
    const type = stringValue(content.type, "evidence content type");
    if (type === "observation") {
      const focused = objectValue(content.focused_element, "focused element");
      excerpt = optionalString(content.visible_text, "visible text") || optionalString(focused.value, "focused value") || "";
      appName = optionalString(content.app_name, "evidence app");
      windowTitle = optionalString(content.window_title, "evidence window title");
      startTime = optionalString(content.timestamp, "evidence timestamp");
    } else if (type === "timeline_block") {
      excerpt = stringArray(content.entries, "timeline evidence entries").join("\n");
      startTime = optionalString(content.start_time, "timeline evidence start");
      endTime = optionalString(content.end_time, "timeline evidence end");
      appName = stringArray(content.apps_used, "timeline evidence apps")[0];
    } else if (type === "memory_entry") {
      excerpt = optionalString(content.body, "memory evidence body") ?? "";
      startTime = optionalString(content.timestamp, "memory evidence timestamp");
    } else if (type === "memory_candidate") {
      excerpt = optionalString(content.content, "candidate evidence content") ?? "";
    } else if (type === "daily_wrap_item") {
      const item = objectValue(content.item, "Daily Wrap evidence item");
      excerpt = optionalString(item.text, "Daily Wrap evidence text") ?? "";
    } else if (type === "daily_wrap") {
      const output = content.output === null ? null : objectValue(content.output, "Daily Wrap evidence output");
      excerpt = output ? optionalString(output.summary, "Daily Wrap evidence summary") ?? "" : "";
    } else if (type === "session") {
      excerpt = `Session status: ${optionalString(content.status, "session status") ?? "unknown"}`;
      startTime = optionalString(content.start_time, "session start");
      endTime = optionalString(content.end_time, "session end");
    } else {
      return protocolError("evidence content type");
    }
  }
  const noteByStatus: Partial<Record<ResolvedEvidence["availability"], string>> = {
    expired: "The source expired under retention and will not be reconstructed.",
    excluded: "The current privacy policy excludes this source.",
    changed: "The source no longer matches the cited content hash.",
    missing: "The cited local source is missing.",
    purging: "The source is hidden while permanent deletion finishes.",
    unverifiable: "The local source cannot be verified safely.",
    unsupported: "This source kind is not available in the trusted drawer.",
  };
  return {
    ref,
    availability: status,
    ...(excerpt ? { excerpt } : {}),
    ...(appName ? { app_name: appName } : {}),
    ...(windowTitle ? { window_title: windowTitle } : {}),
    ...(startTime ? { start_time: startTime } : {}),
    ...(endTime ? { end_time: endTime } : {}),
    ...(noteByStatus[status] ? { note: noteByStatus[status] } : {}),
  };
}

export function normalizeForgetPreview(value: unknown): ForgetPreview {
  const raw = objectValue(value, "forget preview");
  const counts = objectValue(raw.counts, "forget preview counts");
  const files = arrayValue(raw.files, "forget preview files").map((value) => {
    const file = objectValue(value, "forget preview file");
    return { path: stringValue(file.path, "forget preview file path") };
  });
  const entries = arrayValue(raw.entries, "forget preview entries").map((value) => {
    const entry = objectValue(value, "forget preview entry");
    return {
      id: stringValue(entry.id, "forget preview entry id"),
      path: stringValue(entry.path, "forget preview entry path"),
    };
  });
  const planDigest = stringValue(raw.plan_digest, "forget preview digest");
  if (!/^[0-9a-f]{64}$/.test(planDigest)) return protocolError("forget preview digest");
  const preview: ForgetPreview = {
    candidate_id: stringValue(raw.candidate_id, "forget preview candidate id"),
    expected_version: numberValue(raw.expected_version, "forget preview version"),
    candidate_ids: stringArray(raw.candidate_ids, "forget preview candidate ids"),
    files,
    entries,
    wrap_ids: stringArray(raw.wrap_ids, "forget preview wrap ids"),
    plan_digest: planDigest,
    counts: {
      candidates: numberValue(counts.candidates, "forget preview candidate count"),
      memory_files: numberValue(counts.memory_files, "forget preview memory file count"),
      memory_entries: numberValue(counts.memory_entries, "forget preview memory count"),
      daily_wraps: numberValue(counts.daily_wraps, "forget preview Daily Wrap count"),
    },
  };
  if (
    preview.counts.candidates !== preview.candidate_ids.length ||
    preview.counts.memory_files !== preview.files.length ||
    preview.counts.memory_entries !== preview.entries.length ||
    preview.counts.daily_wraps !== preview.wrap_ids.length
  ) {
    return protocolError("forget preview counts");
  }
  return preview;
}

export function normalizeForgetResult(value: unknown): {
  candidate_id: string;
  removed_entry: boolean;
  removed_file_count: number;
  invalidated_wrap_ids: string[];
} {
  const raw = objectValue(value, "forget result");
  return {
    candidate_id: stringValue(raw.candidate_id, "forgotten candidate id"),
    removed_entry: booleanValue(raw.removed_entry, "forget entry result"),
    removed_file_count: numberValue(raw.removed_file_count, "removed memory file count"),
    invalidated_wrap_ids: stringArray(raw.invalidated_wrap_ids, "invalidated Daily Wrap ids"),
  };
}

export function normalizePauseResult(value: unknown): { paused: boolean; changed: boolean } {
  const raw = objectValue(value, "capture pause result");
  return {
    paused: booleanValue(raw.paused, "capture paused state"),
    changed: booleanValue(raw.changed, "capture pause changed state"),
  };
}

export function normalizeSuggestionMutation(value: unknown): Suggestion {
  const raw = objectValue(value, "suggestion response");
  return workResumptionSuggestion(raw.suggestion);
}

export function normalizePromptRescueJob(value: unknown): PromptRescueJob {
  const raw = objectValue(value, "Prompt Rescue response");
  return promptRescueJob(raw.job);
}

export function normalizePromptRescueQueue(value: unknown): {
  job: PromptRescueJob;
  created: boolean;
} {
  const raw = objectValue(value, "Prompt Rescue queue response");
  return {
    job: promptRescueJob(raw.job),
    created: booleanValue(raw.created, "Prompt Rescue created state"),
  };
}

export function normalizePromptRescueDelete(value: unknown): {
  job_id: string;
  deleted: true;
} {
  const raw = objectValue(value, "Prompt Rescue delete response");
  if (booleanValue(raw.deleted, "Prompt Rescue deleted state") !== true) {
    return protocolError("Prompt Rescue deleted state");
  }
  return {
    job_id: stringValue(raw.job_id, "Prompt Rescue deleted id"),
    deleted: true,
  };
}

export function normalizeReplyRescueJob(value: unknown): ReplyRescueJob {
  const raw = objectValue(value, "Reply Rescue response");
  return replyRescueJob(raw.job);
}

export function normalizeReplyRescueQueue(value: unknown): {
  job: ReplyRescueJob;
  created: boolean;
} {
  const raw = objectValue(value, "Reply Rescue queue response");
  return {
    job: replyRescueJob(raw.job),
    created: booleanValue(raw.created, "Reply Rescue created state"),
  };
}

export function normalizeReplyRescueDelete(value: unknown): {
  job_id: string;
  deleted: true;
} {
  const raw = objectValue(value, "Reply Rescue delete response");
  if (booleanValue(raw.deleted, "Reply Rescue deleted state") !== true) {
    return protocolError("Reply Rescue deleted state");
  }
  return {
    job_id: stringValue(raw.job_id, "Reply Rescue deleted id"),
    deleted: true,
  };
}

async function request<T>(command: string, payload: object, normalize: (value: unknown) => T): Promise<T> {
  try {
    const value = await invoke<unknown>(command, { request: payload });
    return normalize(value);
  } catch (error: unknown) {
    if (error instanceof DesktopApiError) throw error;
    if (typeof error === "object" && error !== null) {
      const value = error as Record<string, unknown>;
      const nested = value.error;
      if (typeof nested === "object" && nested !== null) {
        const detail = nested as Record<string, unknown>;
        throw new DesktopApiError(
          String(detail.code ?? "desktop_error"),
          String(detail.message ?? "The local service rejected the request."),
        );
      }
      throw new DesktopApiError(
        String(value.code ?? "desktop_error"),
        String(value.message ?? "The local service rejected the request."),
      );
    }
    throw new DesktopApiError("desktop_error", String(error));
  }
}

async function requestWithoutPayload<T>(
  command: string,
  normalize: (value: unknown) => T,
): Promise<T> {
  try {
    return normalize(await invoke<unknown>(command));
  } catch (error: unknown) {
    if (error instanceof DesktopApiError) throw error;
    if (typeof error === "object" && error !== null) {
      const value = error as Record<string, unknown>;
      const nested = value.error;
      if (typeof nested === "object" && nested !== null) {
        const detail = nested as Record<string, unknown>;
        throw new DesktopApiError(
          String(detail.code ?? "desktop_error"),
          String(detail.message ?? "The local service rejected the request."),
        );
      }
      throw new DesktopApiError(
        String(value.code ?? "desktop_error"),
        String(value.message ?? "The local service rejected the request."),
      );
    }
    throw new DesktopApiError("desktop_error", String(error));
  }
}

export const desktopApi = {
  snapshot: () =>
    request(
      "get_snapshot",
      {
        timeline_limit: 24,
        candidate_limit: 100,
        wrap_limit: 30,
        suggestion_limit: 50,
        prompt_rescue_limit: 50,
        reply_rescue_limit: 50,
      },
      normalizeSnapshot,
    ),
  getCandidate: (candidateId: string) =>
    request("get_candidate", { candidate_id: candidateId }, (value) => {
      const candidate = normalizeCandidateGet(value);
      if (candidate.id !== candidateId) return protocolError("candidate response identity");
      return candidate;
    }),
  editCandidate: (input: {
    candidateId: string;
    expectedVersion: number;
    content: string;
    tags: string[];
  }) =>
    request(
      "edit_candidate",
      {
        candidate_id: input.candidateId,
        expected_version: input.expectedVersion,
        content: input.content,
        tags: input.tags,
      },
      (value) => {
        const candidate = normalizeCandidateMutation(value);
        if (candidate.id !== input.candidateId) return protocolError("candidate mutation identity");
        return candidate;
      },
    ),
  approveCandidate: (candidateId: string, expectedVersion: number) =>
    request(
      "approve_candidate",
      { candidate_id: candidateId, expected_version: expectedVersion },
      (value) => {
        const candidate = normalizeCandidateMutation(value);
        if (candidate.id !== candidateId) return protocolError("candidate mutation identity");
        return candidate;
      },
    ),
  rejectCandidate: (candidateId: string, expectedVersion: number, reason = "") =>
    request(
      "reject_candidate",
      { candidate_id: candidateId, expected_version: expectedVersion, reason },
      (value) => {
        const candidate = normalizeCandidateMutation(value);
        if (candidate.id !== candidateId) return protocolError("candidate mutation identity");
        return candidate;
      },
    ),
  previewForgetCandidate: (candidateId: string, expectedVersion: number) =>
    request(
      "preview_forget_candidate",
      { candidate_id: candidateId, expected_version: expectedVersion },
      (value) => {
        const preview = normalizeForgetPreview(value);
        if (
          preview.candidate_id !== candidateId ||
          preview.expected_version !== expectedVersion
        ) {
          return protocolError("forget preview identity");
        }
        return preview;
      },
    ),
  forgetCandidate: (preview: ForgetPreview) =>
    request(
      "forget_candidate",
      {
        candidate_id: preview.candidate_id,
        expected_version: preview.expected_version,
        plan_digest: preview.plan_digest,
      },
      (value) => {
        const result = normalizeForgetResult(value);
        if (result.candidate_id !== preview.candidate_id) {
          return protocolError("forget result identity");
        }
        return result;
      },
    ),
  getDailyWrap: (localDate: string, timezone: string, scope = "default") =>
    request(
      "get_daily_wrap",
      { local_date: localDate, timezone, scope },
      (value) => {
        const wrap = normalizeDailyWrap(value);
        if (
          wrap.local_date !== localDate ||
          wrap.timezone !== timezone ||
          wrap.scope !== scope
        ) {
          return protocolError("Daily Wrap response identity");
        }
        return wrap;
      },
    ),
  transitionSuggestion: (
    suggestionId: string,
    expectedVersion: number,
    status: "viewed" | "accepted" | "dismissed",
    reason = "",
  ) =>
    request(
      "transition_suggestion",
      {
        suggestion_id: suggestionId,
        expected_version: expectedVersion,
        status,
        reason,
      },
      (value) => {
        const suggestion = normalizeSuggestionMutation(value);
        if (suggestion.id !== suggestionId) {
          return protocolError("suggestion mutation identity");
        }
        return suggestion;
      },
    ),
  getPromptRescue: (jobId: string) =>
    request("get_prompt_rescue", { job_id: jobId }, (value) => {
      const job = normalizePromptRescueJob(value);
      if (job.id !== jobId) return protocolError("Prompt Rescue response identity");
      return job;
    }),
  queuePromptRescue: (input: {
    roughPrompt: string;
    target: string;
    audience: string;
    constraints: string[];
    desiredFormat: string;
  }) =>
    request(
      "queue_prompt_rescue",
      {
        rough_prompt: input.roughPrompt,
        target: input.target,
        audience: input.audience,
        constraints: input.constraints,
        desired_format: input.desiredFormat,
      },
      normalizePromptRescueQueue,
    ),
  editPromptRescue: (jobId: string, expectedVersion: number, improvedPrompt: string) =>
    request(
      "edit_prompt_rescue",
      {
        job_id: jobId,
        expected_version: expectedVersion,
        improved_prompt: improvedPrompt,
      },
      (value) => {
        const job = normalizePromptRescueJob(value);
        if (job.id !== jobId) return protocolError("Prompt Rescue mutation identity");
        return job;
      },
    ),
  retryPromptRescue: (jobId: string, expectedVersion: number) =>
    request(
      "retry_prompt_rescue",
      { job_id: jobId, expected_version: expectedVersion },
      (value) => {
        const job = normalizePromptRescueJob(value);
        if (job.id !== jobId) return protocolError("Prompt Rescue mutation identity");
        return job;
      },
    ),
  deletePromptRescue: (jobId: string, expectedVersion: number) =>
    request(
      "delete_prompt_rescue",
      { job_id: jobId, expected_version: expectedVersion },
      (value) => {
        const result = normalizePromptRescueDelete(value);
        if (result.job_id !== jobId) return protocolError("Prompt Rescue delete identity");
        return result;
      },
    ),
  getReplyRescue: (jobId: string) =>
    request("get_reply_rescue", { job_id: jobId }, (value) => {
      const job = normalizeReplyRescueJob(value);
      if (job.id !== jobId) return protocolError("Reply Rescue response identity");
      return job;
    }),
  queueReplyRescue: (input: {
    conversationText: string;
    participants: string[];
    intendedRecipients: string[];
    replyMode: "reply" | "reply_all" | "unspecified";
    goal: string;
    tone: string;
    styleInstructions: string[];
    commitments: string[];
  }) =>
    request(
      "queue_reply_rescue",
      {
        conversation_text: input.conversationText,
        participants: input.participants,
        intended_recipients: input.intendedRecipients,
        reply_mode: input.replyMode,
        goal: input.goal,
        tone: input.tone,
        style_instructions: input.styleInstructions,
        commitments: input.commitments,
      },
      normalizeReplyRescueQueue,
    ),
  editReplyRescue: (jobId: string, expectedVersion: number, replyBody: string) =>
    request(
      "edit_reply_rescue",
      { job_id: jobId, expected_version: expectedVersion, reply_body: replyBody },
      (value) => {
        const job = normalizeReplyRescueJob(value);
        if (job.id !== jobId) return protocolError("Reply Rescue mutation identity");
        return job;
      },
    ),
  retryReplyRescue: (jobId: string, expectedVersion: number) =>
    request(
      "retry_reply_rescue",
      { job_id: jobId, expected_version: expectedVersion },
      (value) => {
        const job = normalizeReplyRescueJob(value);
        if (job.id !== jobId) return protocolError("Reply Rescue mutation identity");
        return job;
      },
    ),
  deleteReplyRescue: (jobId: string, expectedVersion: number) =>
    request(
      "delete_reply_rescue",
      { job_id: jobId, expected_version: expectedVersion },
      (value) => {
        const result = normalizeReplyRescueDelete(value);
        if (result.job_id !== jobId) return protocolError("Reply Rescue delete identity");
        return result;
      },
    ),
  getResumeRescueState: () =>
    request(
      "get_resume_rescue_state",
      { profile_limit: 20, opportunity_limit: 20, projection_limit: 20 },
      normalizeResumeRescueState,
    ),
  saveResumeProfile: (profile: ResumeProfile, expectedVersion?: number) =>
    request(
      "save_resume_rescue_profile",
      {
        profile_id: profile.profile_id,
        display_name: profile.display_name,
        locale: profile.locale,
        facts: profile.facts,
        conflicts: profile.conflicts,
        ...(expectedVersion === undefined ? {} : { expected_version: expectedVersion }),
      },
      (value) => {
        const result = normalizeResumeProfileMutation(value);
        if (result.profile.id !== profile.profile_id) {
          return protocolError("Résumé Rescue profile response identity");
        }
        return result;
      },
    ),
  saveResumeOpportunity: (opportunity: ResumeOpportunitySource) =>
    request(
      "save_resume_rescue_opportunity",
      {
        employer: opportunity.employer,
        title: opportunity.title,
        source_url: opportunity.source_url,
        source_text: opportunity.source_text,
        priorities: opportunity.priorities,
        locale: opportunity.locale,
        captured_at: opportunity.captured_at,
      },
      normalizeResumeOpportunityMutation,
    ),
  replaceResumeOpportunity: (
    opportunityId: string,
    expectedDigest: string,
    opportunity: ResumeOpportunitySource,
  ) =>
    request(
      "replace_resume_rescue_opportunity",
      {
        opportunity_id: opportunityId,
        expected_digest: expectedDigest,
        employer: opportunity.employer,
        title: opportunity.title,
        source_url: opportunity.source_url,
        source_text: opportunity.source_text,
        priorities: opportunity.priorities,
        locale: opportunity.locale,
        captured_at: opportunity.captured_at,
      },
      normalizeResumeOpportunityMutation,
    ),
  composeResumeExact: (
    profileId: string,
    opportunityId: string,
    sections: ResumeProjectionRequest["sections"],
    requirements: ResumeProjectionRequest["requirements"],
  ) =>
    request(
      "compose_resume_rescue_exact",
      {
        profile_id: profileId,
        opportunity_id: opportunityId,
        sections,
        requirements,
      },
      (value) => {
        const result = normalizeResumeProjectionMutation(value);
        if (
          result.projection.profile_id !== profileId ||
          result.projection.opportunity_id !== opportunityId
        ) {
          return protocolError("Résumé Rescue projection response identity");
        }
        return result;
      },
    ),
  getResumePreview: (projectionId: string, expectedArtifactDigest: string) =>
    request("get_resume_rescue_preview", { projection_id: projectionId }, (value) => {
      const preview = normalizeResumePreview(value);
      if (
        preview.projection_id !== projectionId ||
        preview.artifact_digest !== expectedArtifactDigest
      ) {
        return protocolError("Résumé Rescue preview response identity");
      }
      return preview;
    }),
  exportResumeHtml: (projectionId: string, expectedDocumentDigest: string) =>
    request(
      "export_resume_rescue_html",
      {
        projection_id: projectionId,
        expected_document_digest: expectedDocumentDigest,
      },
      (value) => {
        const result = normalizeResumeHtmlExport(value);
        if (
          result.projection_id !== projectionId ||
          result.document_digest !== expectedDocumentDigest
        ) {
          return protocolError("Résumé Rescue HTML export response identity");
        }
        return result;
      },
    ),
  exportResumeDocx: (
    projectionId: string,
    expectedArtifactDigest: string,
    expectedPreviewDocumentDigest: string,
  ) =>
    request(
      "export_resume_rescue_docx",
      {
        projection_id: projectionId,
        expected_artifact_digest: expectedArtifactDigest,
        expected_preview_document_digest: expectedPreviewDocumentDigest,
      },
      (value) => {
        const result = normalizeResumeDocxExport(value);
        if (
          result.projection_id !== projectionId ||
          result.artifact_digest !== expectedArtifactDigest ||
          result.preview_document_digest !== expectedPreviewDocumentDigest
        ) {
          return protocolError("Résumé Rescue DOCX export response identity");
        }
        return result;
      },
    ),
  openResumeJson: () =>
    requestWithoutPayload("open_resume_rescue_json", normalizeOpenedJsonResumeReview),
  admitResumeJson: (
    sourceText: string,
    expectedReviewDigest: string,
    profileId: string,
    displayName: string,
    locale: string,
    selections: JsonResumeSelection[],
    expectedVersion?: number,
  ) =>
    request(
      "admit_resume_rescue_json",
      {
        source_text: sourceText,
        expected_review_digest: expectedReviewDigest,
        profile_id: profileId,
        display_name: displayName,
        locale,
        selections,
        ...(expectedVersion === undefined ? {} : { expected_version: expectedVersion }),
      },
      (value) => {
        const result = normalizeResumeProfileMutation(value);
        if (result.profile.id !== profileId) {
          return protocolError("JSON Resume admitted profile identity");
        }
        return result;
      },
    ),
  openResumeDocument: () =>
    requestWithoutPayload(
      "open_resume_rescue_document",
      normalizeOpenedResumeDocumentReview,
    ),
  admitResumeDocument: (
    reviewToken: string,
    expectedReviewDigest: string,
    profileId: string,
    displayName: string,
    locale: string,
    selections: JsonResumeSelection[],
    expectedVersion?: number,
  ) =>
    request(
      "admit_resume_rescue_document",
      {
        review_token: reviewToken,
        expected_review_digest: expectedReviewDigest,
        profile_id: profileId,
        display_name: displayName,
        locale,
        selections,
        ...(expectedVersion === undefined ? {} : { expected_version: expectedVersion }),
      },
      (value) => {
        const result = normalizeResumeProfileMutation(value);
        if (result.profile.id !== profileId) {
          return protocolError("résumé document admitted profile identity");
        }
        return result;
      },
    ),
  discardResumeDocument: (reviewToken: string, expectedReviewDigest: string) =>
    request(
      "discard_resume_rescue_document",
      {
        review_token: reviewToken,
        expected_review_digest: expectedReviewDigest,
      },
      (value) => {
        const result = closedObject(
          value,
          ["review_token", "discarded"],
          "discarded résumé document review",
        );
        if (stringValue(result.review_token, "résumé document review token") !== reviewToken) {
          return protocolError("discarded résumé document review identity");
        }
        return {
          review_token: reviewToken,
          discarded: booleanValue(result.discarded, "résumé document discard state"),
        };
      },
    ),
  getResumeJsonExport: (projectionId: string, expectedArtifactDigest: string) =>
    request("get_resume_rescue_json_export", { projection_id: projectionId }, (value) => {
      const result = normalizeJsonResumeExport(value);
      if (
        result.projection_binding.id !== projectionId ||
        result.projection_binding.artifact_digest !== expectedArtifactDigest
      ) {
        return protocolError("JSON Resume export identity");
      }
      return result;
    }),
  exportResumeJson: (projectionId: string, expectedDocumentDigest: string) =>
    request(
      "export_resume_rescue_json",
      {
        projection_id: projectionId,
        expected_document_digest: expectedDocumentDigest,
      },
      (value) => {
        const result = normalizeResumeJsonExportResult(value);
        if (
          result.projection_id !== projectionId ||
          result.document_digest !== expectedDocumentDigest
        ) {
          return protocolError("JSON Resume file export identity");
        }
        return result;
      },
    ),
  traceProvenance: (subject: EvidenceRef, maxDepth = 4) =>
    request(
      "trace_provenance",
      {
        kind: subject.kind,
        artifact_id: subject.id,
        ...(subject.path ? { path: subject.path } : {}),
        max_depth: maxDepth,
      },
      (value) => {
        const trace = normalizeProvenance(value);
        if (!sameReference(trace.subject, subject)) {
          return protocolError("provenance response identity");
        }
        return trace;
      },
    ),
  resolveEvidence: (ref: EvidenceRef) =>
    request(
      "resolve_evidence",
      {
        kind: ref.kind,
        id: ref.id,
        ...(ref.path ? { path: ref.path } : {}),
        ...(ref.timestamp ? { timestamp: ref.timestamp } : {}),
        ...(ref.content_hash ? { content_hash: ref.content_hash } : {}),
      },
      (value) => {
        const evidence = normalizeEvidence(value);
        if (!sameReference(evidence.ref, ref)) {
          return protocolError("evidence response identity");
        }
        return evidence;
      },
    ),
  setCapturePaused: (expectedState: boolean, paused: boolean) =>
    request(
      "set_capture_paused",
      { expected_state: expectedState, paused },
      (value) => {
        const result = normalizePauseResult(value);
        if (result.paused !== paused) return protocolError("capture pause result state");
        return result;
      },
    ),
};

export type DesktopApi = typeof desktopApi;
