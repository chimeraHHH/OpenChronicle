import { beforeEach, describe, expect, it, vi } from "vitest";

const tauri = vi.hoisted(() => ({ invoke: vi.fn() }));

vi.mock("@tauri-apps/api/core", () => ({ invoke: tauri.invoke }));

import {
  DesktopApiError,
  desktopApi,
  normalizeEvidence,
  normalizeForgetPreview,
  normalizeMemoryHistory,
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
  bridgeResumeCueMutation,
  bridgeReplyRescueQueue,
  bridgeSuggestionMutation,
  bridgeWrapGet,
  candidateDetail,
  forgetPreview,
  jsonResumeExport,
  maliciousText,
  memoryHistory,
  memoryForgetPreview,
  memorySummary,
  openedJsonResumeReview,
  openedResumeDocumentReview,
  provenanceTrace,
  promptRescueJob,
  replyRescueJob,
  resumeOpportunity,
  resumeCue,
  resumePdfPreview,
  resumeProfileVersion,
  resumePreview,
  resumeProjection,
  resumeRescueState,
  resumeRewriteJob,
  snapshot,
  suggestion,
  wrapDetail,
} from "./fixtures";

beforeEach(() => {
  tauri.invoke.mockReset();
});

describe("desktop bridge adapters", () => {
  it("tracks on-demand Published Memory history as bridge protocol v23", () => {
    expect(DESKTOP_BRIDGE_PROTOCOL_VERSION).toBe(23);
  });

  it("loads one revision-bound Published Memory lineage", async () => {
    const memory = memorySummary();
    tauri.invoke.mockResolvedValue(memoryHistory());

    const history = await desktopApi.getPublishedMemoryHistory(memory);

    expect(history.versions.map((version) => version.state)).toEqual([
      "current",
      "superseded",
    ]);
    expect(tauri.invoke).toHaveBeenCalledWith("get_published_memory_history", {
      request: {
        path: memory.path,
        entry_id: memory.id,
        expected_revision: memory.revision,
      },
    });
  });

  it("rejects an open or broken Published Memory history projection", () => {
    expect(() => normalizeMemoryHistory({ ...memoryHistory(), unexpected: true })).toThrow(
      DesktopApiError,
    );
    const broken = memoryHistory();
    broken.versions[1]!.superseded_by = "not-the-current-version";
    expect(() => normalizeMemoryHistory(broken)).toThrow(DesktopApiError);
  });

  it("previews and commits revision-bound Published Memory forget", async () => {
    const memory = memorySummary();
    const preview = memoryForgetPreview();
    tauri.invoke.mockResolvedValueOnce(preview).mockResolvedValueOnce({
      path: preview.path,
      entry_id: preview.entry_id,
      removed_entry: true,
      removed_file_count: 0,
      invalidated_wrap_ids: preview.wrap_ids,
    });

    const normalized = await desktopApi.previewForgetPublishedMemory(memory);
    expect(normalized).toEqual(preview);
    expect(tauri.invoke).toHaveBeenLastCalledWith("preview_forget_published_memory", {
      request: {
        path: memory.path,
        entry_id: memory.id,
        expected_revision: memory.revision,
      },
    });

    const result = await desktopApi.forgetPublishedMemory(normalized);
    expect(result.removed_entry).toBe(true);
    expect(tauri.invoke).toHaveBeenLastCalledWith("forget_published_memory", {
      request: {
        path: preview.path,
        entry_id: preview.entry_id,
        expected_revision: preview.expected_revision,
        plan_digest: preview.plan_digest,
      },
    });
  });

  it("corrects one published memory with a revision precondition", async () => {
    tauri.invoke.mockResolvedValue({
      memory: {
        id: "me-corrected",
        path: "user-preferences.md",
        timestamp: "2026-08-23T12:00:00+08:00",
        content: "User prefers encrypted local-first tools.",
        tags: ["preference", "encrypted"],
        origin: "derived-v1",
        source_count: 1,
        revision: "f".repeat(64),
        subject_key: "user.tools.storage",
        assertion_kind: "user_asserted",
        valid_from: "2026-08-01",
        valid_to: "",
        state: "current",
      },
    });

    const result = await desktopApi.correctPublishedMemory({
      path: "user-preferences.md",
      entryId: "published-current",
      expectedRevision: "e".repeat(64),
      content: "User prefers encrypted local-first tools.",
      tags: ["preference", "encrypted"],
    });

    expect(tauri.invoke).toHaveBeenCalledWith("correct_published_memory", {
      request: {
        path: "user-preferences.md",
        entry_id: "published-current",
        expected_revision: "e".repeat(64),
        content: "User prefers encrypted local-first tools.",
        tags: ["preference", "encrypted"],
      },
    });
    expect(result.id).toBe("me-corrected");
    expect(result.revision).toBe("f".repeat(64));
  });

  it("exports current memory through an explicit local save", async () => {
    tauri.invoke.mockResolvedValue({
      schema_version: 1,
      format: "openchronicle_current_memory_markdown_v1",
      content_digest: "c".repeat(64),
      file_name: "openchronicle-memory-2026-08-23.md",
      byte_count: 2_048,
      fact_count: 3,
      created: true,
      action_capability: "none",
    });

    const result = await desktopApi.exportPublishedMemory("markdown");

    expect(tauri.invoke).toHaveBeenCalledWith("export_published_memory", {
      request: { format: "markdown" },
    });
    expect(result).toMatchObject({
      format: "openchronicle_current_memory_markdown_v1",
      fact_count: 3,
      created: true,
    });
  });

  it("requests a bounded snapshot and maps only canonical backend fields", async () => {
    tauri.invoke.mockResolvedValue(bridgeSnapshot());

    const result = await desktopApi.snapshot();

    expect(tauri.invoke).toHaveBeenCalledWith("get_snapshot", {
      request: {
        timeline_limit: 24,
        candidate_limit: 100,
        memory_limit: 250,
        wrap_limit: 30,
        suggestion_limit: 50,
        prompt_rescue_limit: 50,
        reply_rescue_limit: 50,
      },
    });
    expect(result.daemon).toMatchObject({ state: "running", health: "healthy", pid: 1234 });
    expect(result.capture).toMatchObject({ paused: false, state: "active", last_app: "Code" });
    expect(result.memories[0]).toMatchObject({
      id: "memory-entry-1",
      path: "user-preferences.md",
      source_count: 2,
      subject_key: "user.communication.report-style",
      assertion_kind: "user_asserted",
      state: "current",
    });
    expect(result.review_counts).toEqual({ pending: 1, conflict: 0, applying: 0, accepted: 0, rejected: 0 });
    expect(result.suggestion_feedback).toMatchObject({
      sample_size: 4,
      accepted: 1,
      dismissed: 3,
      acceptance_rate: 0.25,
      action_capability: "none",
    });
    expect(result.resume_cues).toEqual([resumeCue()]);
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
      request: {
        profile_limit: 20,
        opportunity_limit: 20,
        projection_limit: 20,
        rewrite_limit: 20,
        rewrite_version_limit: 20,
      },
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

  it("binds supervised résumé proposals to one reviewed decision at a time", async () => {
    const rewrite = resumeRewriteJob();
    const rewritePdfDigest = "7".repeat(64);
    tauri.invoke.mockResolvedValueOnce({
      ...resumeRescueState(),
      rewrite_enabled: true,
      rewrite_provider: { model: rewrite.model_identity, location: rewrite.provider_location },
      rewrites: [rewrite],
    });

    const state = await desktopApi.getResumeRescueState();
    expect(state.rewrites[0]).toMatchObject({
      id: rewrite.id,
      status: "ready",
      proposals: [
        {
          proposal_id: "rewrite-proposal-1",
          operation: "replace_text",
          fact_id: "fact-api",
        },
      ],
      head: {
        decision: "accepted",
        artifact: { generation_mode: "supervised_rewrite_projection" },
      },
    });

    const malformed = resumeRewriteJob() as unknown as Record<string, unknown>;
    const proposals = malformed.proposals as Array<Record<string, unknown>>;
    proposals[0]!.accept_all = true;
    tauri.invoke.mockResolvedValueOnce({
      ...resumeRescueState(),
      rewrite_enabled: true,
      rewrite_provider: { model: rewrite.model_identity, location: rewrite.provider_location },
      rewrites: [malformed],
    });
    await expect(desktopApi.getResumeRescueState()).rejects.toMatchObject({
      code: "BRIDGE_PROTOCOL_ERROR",
    });

    tauri.invoke.mockResolvedValueOnce({ rewrite, created: true });
    await desktopApi.queueResumeRewrite({
      projectionId: rewrite.projection_id,
      expectedArtifactDigest: rewrite.projection_artifact_digest,
      expectedModelIdentity: rewrite.model_identity,
      expectedProviderLocation: rewrite.provider_location,
      remoteEgressAuthorized: false,
    });
    expect(tauri.invoke).toHaveBeenLastCalledWith("queue_resume_rescue_rewrite", {
      request: {
        projection_id: rewrite.projection_id,
        expected_artifact_digest: rewrite.projection_artifact_digest,
        expected_model_identity: rewrite.model_identity,
        expected_provider_location: "local",
        remote_egress_authorized: false,
      },
    });

    const proposal = rewrite.proposals[0]!;
    const head = rewrite.head!;
    tauri.invoke.mockResolvedValueOnce({ version: head, created: true });
    await desktopApi.decideResumeRewrite({
      jobId: rewrite.id,
      proposalId: proposal.proposal_id,
      expectedProposalDigest: proposal.proposal_digest,
      expectedJobVersion: rewrite.version,
      expectedHeadId: "",
      expectedArtifactDigest: rewrite.projection_artifact_digest,
      decision: "accepted",
    });
    expect(tauri.invoke).toHaveBeenLastCalledWith("decide_resume_rescue_rewrite", {
      request: {
        job_id: rewrite.id,
        proposal_id: proposal.proposal_id,
        expected_proposal_digest: proposal.proposal_digest,
        expected_job_version: rewrite.version,
        expected_head_id: "",
        expected_artifact_digest: rewrite.projection_artifact_digest,
        decision: "accepted",
      },
    });

    const versionId = head.id;
    const artifactDigest = head.artifact_digest;
    const documentDigest = "9".repeat(64);
    tauri.invoke.mockResolvedValueOnce({
      schema_version: 1,
      projection_id: versionId,
      document_digest: documentDigest,
      file_name: "resume-reviewed.json",
      byte_count: 1_024,
      created: true,
      action_capability: "none",
    });
    await desktopApi.exportResumeRewriteJson(versionId, documentDigest);
    expect(tauri.invoke).toHaveBeenLastCalledWith("export_resume_rescue_rewrite_json", {
      request: { version_id: versionId, expected_document_digest: documentDigest },
    });

    tauri.invoke.mockResolvedValueOnce({
      schema_version: 1,
      projection_id: versionId,
      artifact_digest: artifactDigest,
      preview_document_digest: documentDigest,
      content_digest: "8".repeat(64),
      format: "docx",
      file_name: "resume-reviewed.docx",
      byte_count: 4_096,
      created: true,
      action_capability: "none",
    });
    await desktopApi.exportResumeRewriteDocx(versionId, artifactDigest, documentDigest);
    expect(tauri.invoke).toHaveBeenLastCalledWith("export_resume_rescue_rewrite_docx", {
      request: {
        version_id: versionId,
        expected_artifact_digest: artifactDigest,
        expected_preview_document_digest: documentDigest,
      },
    });

    const reviewedPdfPreview = resumePdfPreview({
      projection_id: versionId,
      artifact_digest: artifactDigest,
      preview_document_digest: documentDigest,
      pdf_content_digest: rewritePdfDigest,
    });
    tauri.invoke.mockResolvedValueOnce(reviewedPdfPreview);
    expect(
      await desktopApi.getResumeRewritePdfPreview(
        versionId,
        artifactDigest,
        documentDigest,
      ),
    ).toEqual(reviewedPdfPreview);
    expect(tauri.invoke).toHaveBeenLastCalledWith(
      "get_resume_rescue_rewrite_pdf_preview",
      {
        request: {
          projection_id: versionId,
          expected_artifact_digest: artifactDigest,
          expected_preview_document_digest: documentDigest,
        },
      },
    );

    tauri.invoke.mockResolvedValueOnce({
      schema_version: 1,
      projection_id: versionId,
      artifact_digest: artifactDigest,
      preview_document_digest: documentDigest,
      content_digest: rewritePdfDigest,
      format: "pdf",
      file_name: "resume-reviewed.pdf",
      byte_count: 56_000,
      created: true,
      action_capability: "none",
    });
    await desktopApi.exportResumeRewritePdf(
      versionId,
      artifactDigest,
      documentDigest,
      rewritePdfDigest,
    );
    expect(tauri.invoke).toHaveBeenLastCalledWith("export_resume_rescue_rewrite_pdf", {
      request: {
        version_id: versionId,
        expected_artifact_digest: artifactDigest,
        expected_preview_document_digest: documentDigest,
        expected_pdf_content_digest: rewritePdfDigest,
      },
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
      schema_version: 1,
      projection_id: projection.id,
      artifact_digest: projection.artifact_digest,
      preview_document_digest: preview.document_digest,
      content_digest: "e".repeat(64),
      format: "docx",
      file_name: "resume-projection-1.docx",
      byte_count: 4_096,
      created: true,
      action_capability: "none",
    });
    const exportedDocx = await desktopApi.exportResumeDocx(
      projection.id,
      projection.artifact_digest,
      preview.document_digest,
    );
    expect(exportedDocx).toMatchObject({
      file_name: "resume-projection-1.docx",
      content_digest: "e".repeat(64),
      action_capability: "none",
    });
    expect(exportedDocx).not.toHaveProperty("content_base64");
    expect(exportedDocx).not.toHaveProperty("path");
    expect(tauri.invoke).toHaveBeenLastCalledWith("export_resume_rescue_docx", {
      request: {
        projection_id: projection.id,
        expected_artifact_digest: projection.artifact_digest,
        expected_preview_document_digest: preview.document_digest,
      },
    });

    tauri.invoke.mockResolvedValueOnce({
      ...exportedDocx,
      content_base64: "UEsDBA==",
    });
    await expect(
      desktopApi.exportResumeDocx(
        projection.id,
        projection.artifact_digest,
        preview.document_digest,
      ),
    ).rejects.toMatchObject({ code: "BRIDGE_PROTOCOL_ERROR" });

    tauri.invoke.mockResolvedValueOnce({
      schema_version: 1,
      projection_id: projection.id,
      artifact_digest: projection.artifact_digest,
      preview_document_digest: preview.document_digest,
      content_digest: "f".repeat(64),
      format: "pdf",
      file_name: "resume-projection-1.pdf",
      byte_count: 56_864,
      created: true,
      action_capability: "none",
    });
    const exportedPdf = await desktopApi.exportResumePdf(
      projection.id,
      projection.artifact_digest,
      preview.document_digest,
      "f".repeat(64),
    );
    expect(exportedPdf).toMatchObject({
      file_name: "resume-projection-1.pdf",
      content_digest: "f".repeat(64),
      action_capability: "none",
    });
    expect(exportedPdf).not.toHaveProperty("content_base64");
    expect(exportedPdf).not.toHaveProperty("path");
    expect(tauri.invoke).toHaveBeenLastCalledWith("export_resume_rescue_pdf", {
      request: {
        projection_id: projection.id,
        expected_artifact_digest: projection.artifact_digest,
        expected_preview_document_digest: preview.document_digest,
        expected_pdf_content_digest: "f".repeat(64),
      },
    });

    const pdfPreview = resumePdfPreview();
    tauri.invoke.mockResolvedValueOnce(pdfPreview);
    expect(
      await desktopApi.getResumePdfPreview(
        projection.id,
        projection.artifact_digest,
        preview.document_digest,
      ),
    ).toEqual(pdfPreview);
    expect(tauri.invoke).toHaveBeenLastCalledWith("get_resume_rescue_pdf_preview", {
      request: {
        projection_id: projection.id,
        expected_artifact_digest: projection.artifact_digest,
        expected_preview_document_digest: preview.document_digest,
      },
    });

    tauri.invoke.mockResolvedValueOnce({
      preview: { ...resumePreview(), action_capability: "download" },
    });
    await expect(
      desktopApi.getResumePreview(projection.id, projection.artifact_digest),
    ).rejects.toMatchObject({ code: "BRIDGE_PROTOCOL_ERROR" });
  });

  it("reviews JSON Resume before admission and previews losses before native export", async () => {
    const opened = openedJsonResumeReview();
    tauri.invoke.mockResolvedValueOnce(opened);

    const review = await desktopApi.openResumeJson();

    expect(review.review.candidates[0]).toMatchObject({
      suggested_text: "Engineer.",
      review_status: "unreviewed",
    });
    expect(tauri.invoke).toHaveBeenLastCalledWith("open_resume_rescue_json");

    const profile = resumeProfileVersion();
    const admittedProfile = {
      ...profile,
      profile: {
        ...profile.profile,
        facts: [
          {
            id: "fact-json-summary",
            section: "summary",
            text: "Engineer.",
            confidentiality: "public",
            ownership_scope: "individual",
            provenance: [
              {
                kind: "json_resume_field",
                reviewed_at: "2026-08-09T12:00:00.000000+00:00",
                source_id: opened.review.source.id,
                source_digest: opened.review.source.digest,
                json_pointer: "/basics/summary",
                value_digest: "7".repeat(64),
                mapping: "exact_field",
                upstream_schema_version: "v1.0.0",
              },
            ],
          },
        ],
      },
    };
    tauri.invoke.mockResolvedValueOnce({ profile: admittedProfile, created: true });
    const selection = {
      candidate_id: opened.review.candidates[0]!.id,
      fact_id: "fact-json-summary",
      section: "summary" as const,
      confidentiality: "public" as const,
      ownership_scope: "individual" as const,
    };
    const admitted = await desktopApi.admitResumeJson(
      opened.source_text,
      opened.review.review_digest,
      profile.id,
      profile.profile.display_name,
      profile.profile.locale,
      [selection],
      profile.version,
    );
    expect(admitted.profile.profile.facts[0]?.provenance[0]?.kind).toBe(
      "json_resume_field",
    );
    expect(tauri.invoke).toHaveBeenLastCalledWith("admit_resume_rescue_json", {
      request: {
        source_text: opened.source_text,
        expected_review_digest: opened.review.review_digest,
        profile_id: profile.id,
        display_name: profile.profile.display_name,
        locale: profile.profile.locale,
        selections: [selection],
        expected_version: profile.version,
      },
    });

    const exported = jsonResumeExport();
    tauri.invoke.mockResolvedValueOnce({ export: exported });
    const preview = await desktopApi.getResumeJsonExport(
      exported.projection_binding.id,
      exported.projection_binding.artifact_digest,
    );
    expect(preview.interoperability_losses).toHaveLength(1);
    expect(preview.action_capability).toBe("none");

    tauri.invoke.mockResolvedValueOnce({
      schema_version: 1,
      projection_id: exported.projection_binding.id,
      document_digest: exported.document_digest,
      file_name: "resume-projection-1.json",
      byte_count: exported.json_text.length,
      created: true,
      action_capability: "none",
    });
    const saved = await desktopApi.exportResumeJson(
      exported.projection_binding.id,
      exported.document_digest,
    );
    expect(saved.file_name).toBe("resume-projection-1.json");
    expect(tauri.invoke).toHaveBeenLastCalledWith("export_resume_rescue_json", {
      request: {
        projection_id: exported.projection_binding.id,
        expected_document_digest: exported.document_digest,
      },
    });

    tauri.invoke.mockResolvedValueOnce({
      export: { ...exported, action_capability: "upload" },
    });
    await expect(
      desktopApi.getResumeJsonExport(
        exported.projection_binding.id,
        exported.projection_binding.artifact_digest,
      ),
    ).rejects.toMatchObject({ code: "BRIDGE_PROTOCOL_ERROR" });
  });

  it("reviews opaque-token document candidates before exact profile admission", async () => {
    const opened = openedResumeDocumentReview();
    tauri.invoke.mockResolvedValueOnce(opened);

    const review = await desktopApi.openResumeDocument();

    expect(review).toEqual(opened);
    expect(review).not.toHaveProperty("source_bytes");
    expect(review.review.candidates[0]?.locator).toMatchObject({
      kind: "page_bbox",
      page: 1,
    });
    expect(tauri.invoke).toHaveBeenLastCalledWith("open_resume_rescue_document");

    const profile = resumeProfileVersion();
    tauri.invoke.mockResolvedValueOnce({ profile, created: false });
    const selection = {
      candidate_id: opened.review.candidates[0]!.id,
      fact_id: "fact-document-1",
      section: "experience" as const,
      confidentiality: "private" as const,
      ownership_scope: "individual" as const,
    };
    await desktopApi.admitResumeDocument(
      opened.review_token,
      opened.review.review_digest,
      profile.id,
      profile.profile.display_name,
      profile.profile.locale,
      [selection],
      profile.version,
    );
    expect(tauri.invoke).toHaveBeenLastCalledWith("admit_resume_rescue_document", {
      request: {
        review_token: opened.review_token,
        expected_review_digest: opened.review.review_digest,
        profile_id: profile.id,
        display_name: profile.profile.display_name,
        locale: profile.profile.locale,
        selections: [selection],
        expected_version: profile.version,
      },
    });

    tauri.invoke.mockResolvedValueOnce({
      review_token: opened.review_token,
      discarded: true,
    });
    expect(
      await desktopApi.discardResumeDocument(
        opened.review_token,
        opened.review.review_digest,
      ),
    ).toEqual({ review_token: opened.review_token, discarded: true });

    tauri.invoke.mockResolvedValueOnce({
      ...opened,
      review: {
        ...opened.review,
        candidates: [
          {
            ...opened.review.candidates[0],
            locator: { ...opened.review.candidates[0]!.locator, page: 0 },
          },
        ],
      },
    });
    await expect(desktopApi.openResumeDocument()).rejects.toMatchObject({
      code: "BRIDGE_PROTOCOL_ERROR",
    });
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
      "helpful",
    );

    expect(tauri.invoke).toHaveBeenCalledWith("transition_suggestion", {
      request: {
        suggestion_id: "sg-1",
        expected_version: 1,
        status: "accepted",
        reason: "helpful",
      },
    });
    expect(result).toMatchObject({ status: "accepted", version: 2 });
  });

  it("creates and closes one exact user-authored resume cue", async () => {
    const parked = resumeCue();
    const resumed = resumeCue({ status: "resumed", version: 2 });
    tauri.invoke
      .mockResolvedValueOnce(bridgeResumeCueMutation(parked))
      .mockResolvedValueOnce(bridgeResumeCueMutation(resumed));

    expect(
      await desktopApi.createResumeCue(parked.task_label, parked.next_step),
    ).toEqual(parked);
    expect(tauri.invoke).toHaveBeenLastCalledWith("create_resume_cue", {
      request: {
        task_label: parked.task_label,
        next_step: parked.next_step,
      },
    });

    expect(
      await desktopApi.transitionResumeCue(parked.id, parked.version, "resumed"),
    ).toEqual(resumed);
    expect(tauri.invoke).toHaveBeenLastCalledWith("transition_resume_cue", {
      request: {
        cue_id: parked.id,
        expected_version: parked.version,
        status: "resumed",
      },
    });
  });

  it("accepts only the closed cue-bound Work Resumption artifact", () => {
    const base = suggestion();
    const cue = resumeCue();
    const cueBound = suggestion({
      title: `Resume: ${cue.task_label}`,
      artifact: {
        ...base.artifact,
        schema_version: 2,
        recommended_next_step: cue.next_step,
        parked_cue: {
          id: cue.id,
          task_label: cue.task_label,
          next_step: cue.next_step,
          parked_at: cue.created_at,
          user_authored: true,
        },
      },
    });
    const raw = bridgeSnapshot(snapshot({ suggestions: [cueBound] }));

    expect(normalizeSnapshot(raw).suggestions[0]?.artifact).toEqual(cueBound.artifact);

    const malformed = structuredClone(raw);
    const malformedArtifact = malformed.suggestions[0]?.artifact as unknown as Record<
      string,
      unknown
    >;
    malformedArtifact.unknown = true;
    expect(() => normalizeSnapshot(malformed)).toThrowError(DesktopApiError);
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
    expect(result.claim_evidence).toHaveLength(1);
    expect(result).toMatchObject({
      subject_key: "project-alpha-decision",
      assertion_kind: "user_asserted",
      valid_from: "2026-08-08T08:00:00+08:00",
    });
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
