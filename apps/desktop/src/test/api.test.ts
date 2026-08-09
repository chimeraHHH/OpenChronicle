import { beforeEach, describe, expect, it, vi } from "vitest";

const tauri = vi.hoisted(() => ({ invoke: vi.fn() }));

vi.mock("@tauri-apps/api/core", () => ({ invoke: tauri.invoke }));

import {
  DesktopApiError,
  desktopApi,
  normalizeEvidence,
  normalizeForgetPreview,
  normalizeProvenance,
  normalizeSnapshot,
} from "../api";
import { DESKTOP_BRIDGE_PROTOCOL_VERSION } from "../contracts";
import {
  bridgeCandidateGet,
  bridgeCandidateMutation,
  bridgeResolvedEvidence,
  bridgeSnapshot,
  bridgePromptRescueJob,
  bridgePromptRescueQueue,
  bridgeReplyRescueJob,
  bridgeReplyRescueQueue,
  bridgeSuggestionMutation,
  bridgeWrapGet,
  candidateDetail,
  forgetPreview,
  maliciousText,
  provenanceTrace,
  promptRescueJob,
  replyRescueJob,
  resumeOpportunity,
  resumeProfileVersion,
  resumePreview,
  resumeProjection,
  resumeRescueState,
  suggestion,
  wrapDetail,
} from "./fixtures";

beforeEach(() => {
  tauri.invoke.mockReset();
});

describe("desktop bridge adapters", () => {
  it("tracks deterministic Résumé Rescue preview as bridge protocol v9", () => {
    expect(DESKTOP_BRIDGE_PROTOCOL_VERSION).toBe(9);
  });

  it("requests a bounded snapshot and maps only canonical backend fields", async () => {
    tauri.invoke.mockResolvedValue(bridgeSnapshot());

    const result = await desktopApi.snapshot();

    expect(tauri.invoke).toHaveBeenCalledWith("get_snapshot", {
      request: {
        timeline_limit: 24,
        candidate_limit: 100,
        wrap_limit: 30,
        suggestion_limit: 50,
        prompt_rescue_limit: 50,
        reply_rescue_limit: 50,
      },
    });
    expect(result.daemon).toMatchObject({ state: "running", health: "healthy", pid: 1234 });
    expect(result.capture).toMatchObject({ paused: false, state: "active", last_app: "Code" });
    expect(result.review_counts).toEqual({ pending: 1, conflict: 0, applying: 0, accepted: 0, rejected: 0 });
    expect(result.timeline[0]).toMatchObject({
      id: "block-1",
      timezone: "Asia/Shanghai",
      apps: ["Code"],
      capture_count: 2,
    });
    expect(result.privacy).toMatchObject({
      buffer_retention_hours: 168,
      screenshot_retention_hours: 24,
      model_mode: "unknown",
    });
    expect(result.permissions).toEqual([]);
    expect(result.suggestions[0]).toMatchObject({
      id: "sg-1",
      workflow: "work_resumption",
      artifact: { action_capability: "none" },
    });
    expect(result.prompt_rescue).toMatchObject({
      enabled: true,
      provider: { model: "ollama/test-local", location: "local" },
    });
    expect(result.reply_rescue).toMatchObject({
      enabled: true,
      provider: { model: "ollama/test-local", location: "local" },
    });
  });

  it("queues a manual conversation and exposes only a no-action reply artifact", async () => {
    tauri.invoke.mockResolvedValueOnce(bridgeReplyRescueQueue());
    const queued = await desktopApi.queueReplyRescue({
      conversationText: "Ana: Can you meet Tuesday at 10?",
      participants: ["Ana", "Me"],
      intendedRecipients: ["Ana"],
      replyMode: "reply",
      goal: "Confirm Tuesday at 10.",
      tone: "Warm",
      styleInstructions: ["Use a greeting."],
      commitments: ["Tuesday at 10 works."],
    });
    expect(tauri.invoke).toHaveBeenCalledWith("queue_reply_rescue", {
      request: {
        conversation_text: "Ana: Can you meet Tuesday at 10?",
        participants: ["Ana", "Me"],
        intended_recipients: ["Ana"],
        reply_mode: "reply",
        goal: "Confirm Tuesday at 10.",
        tone: "Warm",
        style_instructions: ["Use a greeting."],
        commitments: ["Tuesday at 10 works."],
      },
    });
    expect(queued.job).toMatchObject({
      source: { identity_assurance: "manual_unverified" },
      output: { workflow: "reply_rescue", action_capability: "none" },
    });

    tauri.invoke.mockResolvedValueOnce(
      bridgeReplyRescueJob(
        replyRescueJob({
          version: 4,
          output_edited: true,
          output: {
            ...replyRescueJob().output!,
            reply_body: "Reviewed edit",
            addressed_questions: [],
            claims: [],
          },
        }),
      ),
    );
    const edited = await desktopApi.editReplyRescue("reply-rescue-1", 3, "Reviewed edit");
    expect(edited).toMatchObject({ version: 4, output_edited: true });
    expect(edited.output?.claims).toEqual([]);
    expect(tauri.invoke).toHaveBeenLastCalledWith("edit_reply_rescue", {
      request: {
        job_id: "reply-rescue-1",
        expected_version: 3,
        reply_body: "Reviewed edit",
      },
    });
  });

  it("decodes only the closed Résumé Rescue state and exact no-action artifact", async () => {
    tauri.invoke.mockResolvedValueOnce(resumeRescueState());

    const state = await desktopApi.getResumeRescueState();

    expect(tauri.invoke).toHaveBeenCalledWith("get_resume_rescue_state", {
      request: { profile_limit: 20, opportunity_limit: 20, projection_limit: 20 },
    });
    expect(state.profiles[0]?.profile.facts[0]).toMatchObject({
      id: "fact-api",
      provenance: [{ kind: "manual_reviewed" }],
    });
    expect(state.projections[0]?.artifact).toMatchObject({
      workflow: "resume_rescue",
      action_capability: "none",
      generation_mode: "deterministic_exact_projection",
      missing_evidence: [
        { requirement_id: "req-kubernetes", text: "Kubernetes is required." },
      ],
    });

    const malformed = resumeRescueState() as unknown as Record<string, unknown>;
    malformed.unknown = true;
    tauri.invoke.mockResolvedValueOnce(malformed);
    await expect(desktopApi.getResumeRescueState()).rejects.toMatchObject({
      code: "BRIDGE_PROTOCOL_ERROR",
    });

    const unsafe = resumeRescueState();
    unsafe.projections[0]!.artifact.action_capability = "submit" as never;
    tauri.invoke.mockResolvedValueOnce(unsafe);
    await expect(desktopApi.getResumeRescueState()).rejects.toMatchObject({
      code: "BRIDGE_PROTOCOL_ERROR",
    });
  });

  it("saves reviewed résumé sources and composes only caller-selected exact facts", async () => {
    const profile = resumeProfileVersion();
    tauri.invoke.mockResolvedValueOnce({ profile, created: true });
    const savedProfile = await desktopApi.saveResumeProfile(profile.profile);
    expect(savedProfile.profile).toEqual(profile);
    expect(tauri.invoke).toHaveBeenLastCalledWith("save_resume_rescue_profile", {
      request: {
        profile_id: profile.id,
        display_name: profile.profile.display_name,
        locale: profile.profile.locale,
        facts: profile.profile.facts,
        conflicts: [],
      },
    });

    const opportunity = resumeOpportunity();
    tauri.invoke.mockResolvedValueOnce({ opportunity, created: true });
    await desktopApi.saveResumeOpportunity(opportunity.snapshot);
    expect(tauri.invoke).toHaveBeenLastCalledWith("save_resume_rescue_opportunity", {
      request: {
        employer: opportunity.snapshot.employer,
        title: opportunity.snapshot.title,
        source_url: opportunity.snapshot.source_url,
        source_text: opportunity.snapshot.source_text,
        priorities: opportunity.snapshot.priorities,
        locale: opportunity.snapshot.locale,
        captured_at: opportunity.snapshot.captured_at,
      },
    });

    const projection = resumeProjection();
    tauri.invoke.mockResolvedValueOnce({ projection, created: true });
    const composed = await desktopApi.composeResumeExact(
      profile.id,
      opportunity.id,
      projection.request.sections,
      projection.request.requirements,
    );
    expect(composed.projection.artifact.sections[0]?.items[0]?.text).toBe(
      profile.profile.facts[0]?.text,
    );
    expect(tauri.invoke).toHaveBeenLastCalledWith("compose_resume_rescue_exact", {
      request: {
        profile_id: profile.id,
        opportunity_id: opportunity.id,
        sections: projection.request.sections,
        requirements: projection.request.requirements,
      },
    });

    tauri.invoke.mockResolvedValueOnce({ preview: resumePreview() });
    const preview = await desktopApi.getResumePreview(
      projection.id,
      projection.artifact_digest,
    );
    expect(preview).toMatchObject({
      template_id: "openchronicle-classic-v1",
      renderer_version: 1,
      action_capability: "none",
    });
    expect(preview.html).toContain("default-src 'none'");
    expect(tauri.invoke).toHaveBeenLastCalledWith("get_resume_rescue_preview", {
      request: { projection_id: projection.id },
    });

    tauri.invoke.mockResolvedValueOnce({
      schema_version: 1,
      projection_id: projection.id,
      document_digest: preview.document_digest,
      file_name: "resume-projection-1.html",
      byte_count: preview.html.length,
      created: true,
      action_capability: "none",
    });
    const exported = await desktopApi.exportResumeHtml(
      projection.id,
      preview.document_digest,
    );
    expect(exported).toMatchObject({
      file_name: "resume-projection-1.html",
      action_capability: "none",
    });
    expect(tauri.invoke).toHaveBeenLastCalledWith("export_resume_rescue_html", {
      request: {
        projection_id: projection.id,
        expected_document_digest: preview.document_digest,
      },
    });

    tauri.invoke.mockResolvedValueOnce({
      preview: { ...resumePreview(), action_capability: "download" },
    });
    await expect(
      desktopApi.getResumePreview(projection.id, projection.artifact_digest),
    ).rejects.toMatchObject({ code: "BRIDGE_PROTOCOL_ERROR" });
  });

  it("queues reviewed manual input and maps only a no-action prepared artifact", async () => {
    tauri.invoke.mockResolvedValueOnce(bridgePromptRescueQueue());

    const queued = await desktopApi.queuePromptRescue({
      roughPrompt: "make a release note",
      target: "Engineering",
      audience: "Reviewers",
      constraints: ["Use supplied facts only"],
      desiredFormat: "Markdown",
    });

    expect(tauri.invoke).toHaveBeenCalledWith("queue_prompt_rescue", {
      request: {
        rough_prompt: "make a release note",
        target: "Engineering",
        audience: "Reviewers",
        constraints: ["Use supplied facts only"],
        desired_format: "Markdown",
      },
    });
    expect(queued.job.output).toMatchObject({
      workflow: "prompt_rescue",
      action_capability: "none",
    });

    tauri.invoke.mockResolvedValueOnce(
      bridgePromptRescueJob(
        promptRescueJob({
          output: {
            ...promptRescueJob().output!,
            action_capability: "none",
            improved_prompt: "Reviewed edit",
          },
          version: 4,
          output_edited: true,
        }),
      ),
    );
    const edited = await desktopApi.editPromptRescue(
      "prompt-rescue-1",
      3,
      "Reviewed edit",
    );
    expect(edited).toMatchObject({ version: 4, output_edited: true });
    expect(tauri.invoke).toHaveBeenLastCalledWith("edit_prompt_rescue", {
      request: {
        job_id: "prompt-rescue-1",
        expected_version: 3,
        improved_prompt: "Reviewed edit",
      },
    });
  });

  it("accepts only a closed exact-selection receipt", async () => {
    const selected = promptRescueJob({
      source_kind: "macos_selection",
      source_binding: {
        schema_version: 1,
        captured_at: "2026-08-09T12:00:00Z",
        app_name: "Notes",
        bundle_id: "com.apple.Notes",
        pid: 123,
        window_title: "Release",
        element_role: "AXTextArea",
        element_subrole: "",
        selection_location: 7,
        selection_length: 28,
      },
    });
    tauri.invoke.mockResolvedValueOnce(bridgePromptRescueJob(selected));

    const result = await desktopApi.getPromptRescue(selected.id);

    expect(result.source_kind).toBe("macos_selection");
    expect(result.source_binding).toMatchObject({
      bundle_id: "com.apple.Notes",
      selection_location: 7,
      selection_length: 28,
    });

    const malformed = bridgePromptRescueJob(selected);
    (malformed.job.source_binding as Record<string, unknown>).unknown = true;
    tauri.invoke.mockResolvedValueOnce(malformed);
    await expect(desktopApi.getPromptRescue(selected.id)).rejects.toMatchObject({
      code: "BRIDGE_PROTOCOL_ERROR",
    });
  });

  it("keeps Reply Rescue selection identity weaker than mailbox identity", async () => {
    const binding = {
      schema_version: 1 as const,
      captured_at: "2026-08-09T12:00:00Z",
      app_name: "Notes",
      bundle_id: "com.apple.Notes",
      pid: 123,
      window_title: "Conversation excerpt",
      element_role: "AXTextArea",
      element_subrole: "",
      selection_location: 7,
      selection_length: 48,
    };
    const selected = replyRescueJob({
      source_kind: "macos_selection",
      source: {
        schema_version: 2,
        identity_assurance: "selected_excerpt_unverified",
        selection_binding: binding,
        conversation_text: "Ana: Can you confirm whether Tuesday still works?",
        participants: [],
        intended_recipients: [],
        reply_mode: "unspecified",
        goal: "Prepare a cautious reply to this selected excerpt for review.",
        tone: "",
        style_instructions: [],
        commitments: [],
      },
    });
    tauri.invoke.mockResolvedValueOnce(bridgeReplyRescueJob(selected));

    const result = await desktopApi.getReplyRescue(selected.id);

    expect(result.source_kind).toBe("macos_selection");
    expect(result.source.identity_assurance).toBe("selected_excerpt_unverified");
    if (result.source.identity_assurance !== "selected_excerpt_unverified") {
      throw new Error("expected selected excerpt");
    }
    expect(result.source.selection_binding).toMatchObject({
      bundle_id: "com.apple.Notes",
      selection_location: 7,
      selection_length: 48,
    });
    expect(result.source.intended_recipients).toEqual([]);

    const malformed = bridgeReplyRescueJob(selected);
    const malformedSource = malformed.job.source as unknown as Record<string, unknown>;
    (malformedSource.selection_binding as Record<string, unknown>).unknown = true;
    tauri.invoke.mockResolvedValueOnce(malformed);
    await expect(desktopApi.getReplyRescue(selected.id)).rejects.toMatchObject({
      code: "BRIDGE_PROTOCOL_ERROR",
    });
  });

  it("sends an exact suggestion CAS transition without any action command", async () => {
    tauri.invoke.mockResolvedValue(
      bridgeSuggestionMutation(suggestion({ status: "accepted", version: 2 })),
    );

    const result = await desktopApi.transitionSuggestion(
      "sg-1",
      1,
      "accepted",
      "acknowledged_from_desktop",
    );

    expect(tauri.invoke).toHaveBeenCalledWith("transition_suggestion", {
      request: {
        suggestion_id: "sg-1",
        expected_version: 1,
        status: "accepted",
        reason: "acknowledged_from_desktop",
      },
    });
    expect(result).toMatchObject({ status: "accepted", version: 2 });
  });

  it("unwraps candidate reads and keeps their direct evidence", async () => {
    const detail = candidateDetail({ content: maliciousText });
    tauri.invoke.mockResolvedValue(bridgeCandidateGet(detail));

    const result = await desktopApi.getCandidate("cand-1");

    expect(tauri.invoke).toHaveBeenCalledWith("get_candidate", {
      request: { candidate_id: "cand-1" },
    });
    expect(result.content).toBe(maliciousText);
    expect(result.evidence).toEqual(detail.evidence);
    expect(result.evidence_count).toBe(1);
  });

  it("unwraps CAS mutation responses and sends one request object", async () => {
    tauri.invoke.mockResolvedValue(
      bridgeCandidateMutation(candidateDetail({ content: "Reviewed", version: 4 })),
    );

    const result = await desktopApi.editCandidate({
      candidateId: "cand-1",
      expectedVersion: 3,
      content: "Reviewed",
      tags: ["architecture"],
    });

    expect(tauri.invoke).toHaveBeenCalledWith("edit_candidate", {
      request: {
        candidate_id: "cand-1",
        expected_version: 3,
        content: "Reviewed",
        tags: ["architecture"],
      },
    });
    expect(result).toMatchObject({ content: "Reviewed", version: 4, evidence: [] });
  });

  it("maps a read-only Daily Wrap while preserving the untrusted quote marker", async () => {
    tauri.invoke.mockResolvedValue({
      wrap: {
        ...wrapDetail(),
        attempt_count: 17,
        input_digest: "active-private-digest",
        lease_token: "private-lease",
        updated_at: "2026-08-08T12:00:00Z",
        last_error: "private provider failure",
      },
    });

    const result = await desktopApi.getDailyWrap("2026-08-07", "Asia/Shanghai");

    expect(tauri.invoke).toHaveBeenCalledWith("get_daily_wrap", {
      request: { local_date: "2026-08-07", timezone: "Asia/Shanghai", scope: "default" },
    });
    expect(result.output?.completed[0]).toMatchObject({
      kind: "completed",
      untrusted_activity_quote: true,
    });
    expect(result).not.toHaveProperty("attempt_count");
    expect(result).not.toHaveProperty("input_digest");
    expect(result).not.toHaveProperty("lease_token");
    expect(result).not.toHaveProperty("updated_at");
    expect(result).not.toHaveProperty("last_error");
  });

  it("passes provenance references explicitly and maps evidence into inert text", async () => {
    tauri.invoke
      .mockResolvedValueOnce({
        ...provenanceTrace,
        subject: { ...provenanceTrace.subject, path: "project-alpha.md" },
      })
      .mockResolvedValueOnce(bridgeResolvedEvidence());

    const traced = await desktopApi.traceProvenance({
      kind: "memory_candidate",
      id: "cand-1",
      path: "project-alpha.md",
    });
    const resolved = await desktopApi.resolveEvidence(traced.direct_sources[0]!);

    expect(tauri.invoke).toHaveBeenNthCalledWith(1, "trace_provenance", {
      request: {
        kind: "memory_candidate",
        artifact_id: "cand-1",
        path: "project-alpha.md",
        max_depth: 4,
      },
    });
    expect(tauri.invoke).toHaveBeenNthCalledWith(2, "resolve_evidence", {
      request: expect.objectContaining({ kind: "timeline_block", id: "block-1" }),
    });
    expect(resolved.excerpt).toBe(maliciousText);
    expect(resolved.availability).toBe("current");
  });

  it("treats changed integrity as changed even when the source row still exists", () => {
    const normalized = normalizeProvenance({
      ...provenanceTrace,
      direct_sources: provenanceTrace.direct_sources.map((source) => ({
        ...source,
        availability: "available",
        integrity: "changed",
      })),
    });

    expect(normalized.direct_sources[0]?.availability).toBe("changed");
  });

  it("validates forget previews before forwarding their digest to native confirmation", async () => {
    tauri.invoke
      .mockResolvedValueOnce(forgetPreview)
      .mockResolvedValueOnce({
        candidate_id: "cand-1",
        removed_entry: true,
        removed_file_count: 1,
        invalidated_wrap_ids: ["daily-wrap-1"],
      });

    const preview = await desktopApi.previewForgetCandidate("cand-1", 3);
    const result = await desktopApi.forgetCandidate(preview);

    expect(tauri.invoke).toHaveBeenNthCalledWith(2, "forget_candidate", {
      request: {
        candidate_id: "cand-1",
        expected_version: 3,
        plan_digest: forgetPreview.plan_digest,
      },
    });
    expect(result).toEqual({
      candidate_id: "cand-1",
      removed_entry: true,
      removed_file_count: 1,
      invalidated_wrap_ids: ["daily-wrap-1"],
    });
  });

  it("rejects malformed or mismatched responses as protocol errors", async () => {
    expect(() => normalizeSnapshot({ daemon: null })).toThrowError(DesktopApiError);
    expect(() =>
      normalizeForgetPreview({ ...forgetPreview, plan_digest: "not-a-digest" }),
    ).toThrowError(expect.objectContaining({ code: "BRIDGE_PROTOCOL_ERROR" }));
    expect(() =>
      normalizeForgetPreview({
        ...forgetPreview,
        counts: { ...forgetPreview.counts, memory_files: 0 },
      }),
    ).toThrowError(expect.objectContaining({ code: "BRIDGE_PROTOCOL_ERROR" }));
    expect(() =>
      normalizeEvidence({ reference: { kind: "x", id: "y" }, status: "invented", content: null }),
    ).toThrowError(expect.objectContaining({ code: "BRIDGE_PROTOCOL_ERROR" }));

    tauri.invoke.mockResolvedValue({ ...forgetPreview, candidate_id: "cand-other" });
    await expect(desktopApi.previewForgetCandidate("cand-1", 3)).rejects.toMatchObject({
      code: "BRIDGE_PROTOCOL_ERROR",
    });

    tauri.invoke.mockResolvedValue({
      candidate_id: "cand-other",
      removed_entry: true,
      removed_file_count: 0,
      invalidated_wrap_ids: [],
    });
    await expect(desktopApi.forgetCandidate(forgetPreview)).rejects.toMatchObject({
      code: "BRIDGE_PROTOCOL_ERROR",
    });

    tauri.invoke.mockResolvedValue({ paused: false, changed: true });
    await expect(desktopApi.setCapturePaused(false, true)).rejects.toMatchObject({
      code: "BRIDGE_PROTOCOL_ERROR",
    });

    tauri.invoke.mockResolvedValue(
      bridgeCandidateGet(candidateDetail({ id: "cand-other" })),
    );
    await expect(desktopApi.getCandidate("cand-1")).rejects.toMatchObject({
      code: "BRIDGE_PROTOCOL_ERROR",
    });
  });

  it("preserves structured bridge error codes for CAS handling", async () => {
    tauri.invoke.mockRejectedValue({
      error: { code: "VERSION_CONFLICT", message: "candidate version changed" },
    });

    await expect(desktopApi.approveCandidate("cand-1", 3)).rejects.toMatchObject({
      code: "VERSION_CONFLICT",
      message: "candidate version changed",
    });
  });
});
