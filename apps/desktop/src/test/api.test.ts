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
  bridgeWrapGet,
  candidateDetail,
  forgetPreview,
  maliciousText,
  provenanceTrace,
  wrapDetail,
} from "./fixtures";

beforeEach(() => {
  tauri.invoke.mockReset();
});

describe("desktop bridge adapters", () => {
  it("tracks the immutable Daily Wrap projection as bridge protocol v2", () => {
    expect(DESKTOP_BRIDGE_PROTOCOL_VERSION).toBe(2);
  });

  it("requests a bounded snapshot and maps only canonical backend fields", async () => {
    tauri.invoke.mockResolvedValue(bridgeSnapshot());

    const result = await desktopApi.snapshot();

    expect(tauri.invoke).toHaveBeenCalledWith("get_snapshot", {
      request: { timeline_limit: 24, candidate_limit: 100, wrap_limit: 30 },
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
