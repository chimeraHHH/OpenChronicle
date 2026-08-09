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
  PrivacySnapshot,
  PromptRescueJob,
  PromptRescueJobSummary,
  PromptRescueOutput,
  PromptRescueProviderLocation,
  PromptRescueStatus,
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
  if (stringValue(raw.source_kind, "Prompt Rescue source kind") !== "manual_paste") {
    return protocolError("Prompt Rescue source kind");
  }
  return {
    id: stringValue(raw.id, "Prompt Rescue id"),
    status: promptRescueStatus(raw.status),
    source_kind: "manual_paste",
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
  if (stringValue(raw.source_kind, "Prompt Rescue source kind") !== "manual_paste") {
    return protocolError("Prompt Rescue source kind");
  }
  const output = promptRescueOutput(raw.output);
  const status = promptRescueStatus(raw.status);
  if ((status === "ready") !== (output !== null)) {
    return protocolError("Prompt Rescue output state");
  }
  return {
    id: stringValue(raw.id, "Prompt Rescue id"),
    status,
    source_kind: "manual_paste",
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
