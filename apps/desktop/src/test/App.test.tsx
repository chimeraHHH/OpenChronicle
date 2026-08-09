import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const tauri = vi.hoisted(() => ({
  invoke: vi.fn(),
  listen: vi.fn(async () => () => undefined),
}));

vi.mock("@tauri-apps/api/core", () => ({ invoke: tauri.invoke }));
vi.mock("@tauri-apps/api/event", () => ({ listen: tauri.listen }));

import { App } from "../App";
import { desktopApi } from "../api";
import { SourceDrawer } from "../components/SourceDrawer";
import "../styles.css";
import {
  bridgeCandidateGet,
  bridgeCandidateMutation,
  bridgeResolvedEvidence,
  bridgePromptRescueJob,
  bridgePromptRescueQueue,
  bridgeReplyRescueJob,
  bridgeReplyRescueQueue,
  bridgeSnapshot,
  bridgeSuggestionMutation,
  bridgeWrapGet,
  candidateDetail,
  candidateSummary,
  forgetPreview,
  maliciousText,
  provenanceTrace,
  promptRescueJob,
  promptRescueSummary,
  replyRescueJob,
  replyRescueSummary,
  resumeOpportunity,
  resumeProfileVersion,
  resumeProjection,
  resumeRescueState,
  resolvedEvidence,
  snapshot,
  suggestion,
  wrapDetail,
  wrapSummary,
} from "./fixtures";

function commandResult(command: string) {
  if (command === "get_snapshot") return bridgeSnapshot();
  if (command === "get_candidate") return bridgeCandidateGet();
  if (command === "get_daily_wrap") return bridgeWrapGet();
  if (command === "trace_provenance") return provenanceTrace;
  if (command === "resolve_evidence") return bridgeResolvedEvidence();
  if (command === "preview_forget_candidate") return forgetPreview;
  if (command === "forget_candidate") return { candidate_id: "cand-1", removed_entry: true, removed_file_count: 1, invalidated_wrap_ids: ["daily-wrap-1"] };
  if (command === "set_capture_paused") return { paused: true, changed: true };
  if (command === "transition_suggestion") {
    return bridgeSuggestionMutation(suggestion({ status: "accepted", version: 2 }));
  }
  if (command === "get_prompt_rescue") return bridgePromptRescueJob();
  if (command === "queue_prompt_rescue") return bridgePromptRescueQueue();
  if (command === "edit_prompt_rescue") {
    return bridgePromptRescueJob(promptRescueJob({ version: 4, output_edited: true }));
  }
  if (command === "retry_prompt_rescue") {
    return bridgePromptRescueJob(
      promptRescueJob({ status: "queued", output: null, version: 4 }),
    );
  }
  if (command === "delete_prompt_rescue") {
    return { job_id: "prompt-rescue-1", deleted: true };
  }
  if (command === "get_reply_rescue") return bridgeReplyRescueJob();
  if (command === "queue_reply_rescue") return bridgeReplyRescueQueue();
  if (command === "edit_reply_rescue") {
    return bridgeReplyRescueJob(
      replyRescueJob({
        version: 4,
        output_edited: true,
        output: {
          ...replyRescueJob().output!,
          addressed_questions: [],
          claims: [],
        },
      }),
    );
  }
  if (command === "retry_reply_rescue") {
    return bridgeReplyRescueJob(
      replyRescueJob({ status: "queued", output: null, version: 4 }),
    );
  }
  if (command === "delete_reply_rescue") {
    return { job_id: "reply-rescue-1", deleted: true };
  }
  if (command === "get_resume_rescue_state") return resumeRescueState();
  if (command === "save_resume_rescue_profile") {
    return { profile: resumeProfileVersion(), created: true };
  }
  if (command === "save_resume_rescue_opportunity") {
    return { opportunity: resumeOpportunity(), created: true };
  }
  if (command === "replace_resume_rescue_opportunity") {
    return { opportunity: resumeOpportunity(), created: true };
  }
  if (command === "compose_resume_rescue_exact") {
    return { projection: resumeProjection(), created: true };
  }
  if (command === "edit_candidate" || command === "approve_candidate" || command === "reject_candidate") {
    return bridgeCandidateMutation();
  }
  throw new Error(`Unexpected command: ${command}`);
}

beforeEach(() => {
  tauri.invoke.mockReset();
  tauri.listen.mockClear();
  tauri.invoke.mockImplementation(async (command: string) => commandResult(command));
});

describe("trusted console", () => {
  it("reviews an exact résumé projection without ATS, upload, or application capability", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /Résumé Rescue/i }));

    expect(await screen.findByRole("heading", { name: "Résumé Rescue" })).toBeInTheDocument();
    expect(screen.getByText(/does not invent claims, score ATS compatibility, upload files, or apply/i)).toBeInTheDocument();
    expect(
      (await screen.findAllByText(
        "Reduced API p95 latency by 40% after profiling the query path.",
      )).length,
    ).toBeGreaterThan(0);
    expect(screen.getAllByText("Kubernetes is required.").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Missing evidence").length).toBeGreaterThan(0);
    expect(screen.getByText(/manual_mapping_unverified/i)).toBeInTheDocument();
    expect(screen.getByText(/Action capability: none/i)).toBeInTheDocument();
    expect(
      tauri.invoke.mock.calls.some(([command]) =>
        String(command).includes("apply") ||
        String(command).includes("upload") ||
        String(command).includes("submit"),
      ),
    ).toBe(false);
  });

  it("reviews and copies a manual reply without mailbox or send capability", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /Reply Rescue/i }));
    expect(await screen.findByRole("heading", { name: "Reply Rescue" })).toBeInTheDocument();
    expect(screen.getByText(/Excerpt sources have no thread identity/i)).toBeInTheDocument();
    expect(screen.getByText(/no mailbox, provider-draft, paste, or send capability/i)).toBeInTheDocument();
    expect(await screen.findByDisplayValue("Ana: Can you meet Tuesday at 10?")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Copy reviewed reply" }));
    expect(writeText).toHaveBeenCalledWith("Hi Ana, Tuesday at 10 works for me.");
    expect(screen.getByText(/did not paste, draft, or send it/i)).toBeInTheDocument();
    expect(
      tauri.invoke.mock.calls.some(([command]) =>
        String(command).includes("send") || String(command).includes("mailbox"),
      ),
    ).toBe(false);
  });

  it("prepares explicit manual input and copies without paste or submit capability", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /Prompt Rescue/i }));
    expect(await screen.findByRole("heading", { name: "Prompt Rescue" })).toBeInTheDocument();
    expect(screen.getByText(/Source: manual paste/i)).toBeInTheDocument();
    expect(screen.getByText(/cannot paste into another app, submit, or run tools/i)).toBeInTheDocument();
    expect(screen.getByText(/Configured local provider/i)).toBeInTheDocument();
    expect(await screen.findByDisplayValue("make a release note")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Copy reviewed prompt" }));
    expect(writeText).toHaveBeenCalledWith(
      "Write concise release notes using only reviewed facts.",
    );
    expect(screen.getByText(/did not paste or submit it/i)).toBeInTheDocument();
    expect(
      tauri.invoke.mock.calls.some(([command]) =>
        String(command).includes("paste") || String(command).includes("submit"),
      ),
    ).toBe(false);
  });

  it("shows the global shortcut and exact selection receipt without an import button", async () => {
    const user = userEvent.setup();
    const binding = {
      schema_version: 1 as const,
      captured_at: "2026-08-09T12:00:00Z",
      app_name: "Notes",
      bundle_id: "com.apple.Notes",
      pid: 123,
      window_title: "Launch notes",
      element_role: "AXTextArea",
      element_subrole: "",
      selection_location: 4,
      selection_length: 27,
    };
    const summary = promptRescueSummary({ source_kind: "macos_selection" });
    const detail = promptRescueJob({
      source_kind: "macos_selection",
      source_binding: binding,
    });
    tauri.invoke.mockImplementation(async (command: string) => {
      if (command === "get_snapshot") {
        return bridgeSnapshot(
          snapshot({
            prompt_rescue: {
              ...snapshot().prompt_rescue,
              jobs: [summary],
            },
          }),
        );
      }
      if (command === "get_prompt_rescue") return bridgePromptRescueJob(detail);
      return commandResult(command);
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /Prompt Rescue/i }));
    const selectionHelp = screen
      .getByRole("heading", { name: "Import an exact macOS selection" })
      .closest("section");
    expect(selectionHelp).not.toBeNull();
    expect(selectionHelp).toHaveTextContent("press ⌘ ⇧ Space");
    expect(screen.getByText(/never falls back to the clipboard/i)).toBeInTheDocument();
    const receipt = await screen.findByLabelText("Exact selection source");
    expect(within(receipt).getByText("Notes")).toBeInTheDocument();
    expect(within(receipt).getByText("com.apple.Notes")).toBeInTheDocument();
    expect(within(receipt).getByText(/Launch notes/)).toBeInTheDocument();
    expect(within(receipt).getByText(/range 4\+27/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /import selection/i })).not.toBeInTheDocument();
  });

  it("shows Reply Rescue's distinct shortcut and weaker exact-selection receipt", async () => {
    const user = userEvent.setup();
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
    const summary = replyRescueSummary({
      source_kind: "macos_selection",
      identity_assurance: "selected_excerpt_unverified",
    });
    const detail = replyRescueJob({
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
    tauri.invoke.mockImplementation(async (command: string) => {
      if (command === "get_snapshot") {
        return bridgeSnapshot(
          snapshot({
            reply_rescue: {
              ...snapshot().reply_rescue,
              jobs: [summary],
            },
          }),
        );
      }
      if (command === "get_reply_rescue") return bridgeReplyRescueJob(detail);
      return commandResult(command);
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /Reply Rescue/i }));
    const selectionHelp = screen
      .getByRole("heading", { name: "Import an exact macOS selection" })
      .closest("section");
    expect(selectionHelp).toHaveTextContent("press ⌘ ⇧ R");
    expect(selectionHelp).toHaveTextContent("recipients and reply mode left unspecified");
    const receipt = await screen.findByLabelText("Reply exact selection source");
    expect(within(receipt).getByText("Notes")).toBeInTheDocument();
    expect(within(receipt).getByText("com.apple.Notes")).toBeInTheDocument();
    expect(within(receipt).getByText(/range 7\+48/)).toBeInTheDocument();
    expect(receipt).toHaveTextContent("does not establish mail-thread or recipient identity");
    expect(screen.queryByRole("button", { name: /import selection/i })).not.toBeInTheDocument();
  });

  it("queues the exact reviewed rough prompt through the bounded desktop command", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: /Prompt Rescue/i }));
    const rough = screen.getByRole("textbox", { name: "Rough prompt" });
    await user.type(rough, "Turn these notes into a test plan");
    await user.click(screen.getByRole("button", { name: "Prepare prompt" }));

    await waitFor(() =>
      expect(tauri.invoke).toHaveBeenCalledWith("queue_prompt_rescue", {
        request: {
          rough_prompt: "Turn these notes into a test plan",
          target: "",
          audience: "",
          constraints: [],
          desired_format: "",
        },
      }),
    );
  });

  it("renders proactive evidence as inert text and acknowledges without acting", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Suggestions" }));
    expect(await screen.findByRole("heading", { name: "Resume your recent work" })).toBeInTheDocument();
    expect(screen.getByText(/cannot type, paste, send, or run tools/i)).toBeInTheDocument();
    expect(screen.getByText("Reviewed the trusted console implementation.").closest("bdi")).not.toBeNull();

    await user.click(screen.getByRole("button", { name: "Acknowledge only" }));
    await waitFor(() =>
      expect(tauri.invoke).toHaveBeenCalledWith("transition_suggestion", {
        request: {
          suggestion_id: "sg-1",
          expected_version: 1,
          status: "accepted",
          reason: "acknowledged_from_desktop",
        },
      }),
    );
    expect(
      tauri.invoke.mock.calls.some(([command]) =>
        ["approve_candidate", "set_capture_paused"].includes(command),
      ),
    ).toBe(false);
  });

  it("keeps programmatically focused page landmarks free of a full-page outline", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Review" }));
    const main = screen.getByRole("main");
    main.focus();

    expect(main).toHaveFocus();
    expect(getComputedStyle(main).outlineStyle).toBe("none");
  });

  it("states the narrow pause semantics and sends a request-wrapped CAS command", async () => {
    const user = userEvent.setup();
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Overview" })).toBeInTheDocument();
    expect(
      screen.getByText(/Pausing stops only new desktop captures.*already queued locally/is),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Pause new capture" }));

    await waitFor(() =>
      expect(tauri.invoke).toHaveBeenCalledWith("set_capture_paused", {
        request: { expected_state: false, paused: true },
      }),
    );
    expect(tauri.invoke.mock.calls.some(([command]) => command === "approve_candidate")).toBe(false);
  });

  it("allows capture to resume while the daemon health is degraded", async () => {
    tauri.invoke.mockImplementation(async (command: string) => {
      if (command === "get_snapshot") {
        return bridgeSnapshot(snapshot({
          daemon: { state: "degraded", health: "stale", pid: 1234, uptime: "2h" },
          capture: {
            paused: true,
            state: "paused",
            last_capture_at: "2026-08-08T09:00:00+08:00",
            last_app: "Code",
          },
        }));
      }
      return commandResult(command);
    });
    render(<App />);

    expect(await screen.findByRole("button", { name: "Resume new capture" })).toBeEnabled();
  });

  it("does not let an older inbox refresh overwrite a newer capture snapshot", async () => {
    const user = userEvent.setup();
    let snapshotReads = 0;
    let releaseOlderSnapshot: ((value: ReturnType<typeof bridgeSnapshot>) => void) | undefined;
    tauri.invoke.mockImplementation(async (command: string) => {
      if (command === "get_snapshot") {
        snapshotReads += 1;
        if (snapshotReads === 1) return bridgeSnapshot();
        if (snapshotReads === 2) {
          return new Promise<ReturnType<typeof bridgeSnapshot>>((resolve) => {
            releaseOlderSnapshot = resolve;
          });
        }
        return bridgeSnapshot(snapshot({
          capture: {
            paused: true,
            state: "paused",
            last_capture_at: "2026-08-08T09:59:30+08:00",
            last_app: "Code",
          },
        }));
      }
      return commandResult(command);
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Review" }));
    expect((await screen.findAllByText("Decided to keep the trusted boundary.")).length).toBeGreaterThanOrEqual(2);
    await user.click(screen.getByRole("button", { name: "Save reviewed memory" }));
    await waitFor(() => expect(snapshotReads).toBe(2));

    await user.click(screen.getByRole("button", { name: "Overview" }));
    await user.click(screen.getByRole("button", { name: "Pause new capture" }));
    expect(await screen.findByRole("button", { name: "Resume new capture" })).toBeInTheDocument();

    releaseOlderSnapshot?.(bridgeSnapshot());
    await waitFor(() => expect(snapshotReads).toBe(3));
    expect(screen.getByRole("button", { name: "Resume new capture" })).toBeInTheDocument();
  });

  it("renders prompt, HTML, and bidi source text inertly in the source drawer", async () => {
    const user = userEvent.setup();
    const summary = candidateSummary({ content_preview: maliciousText });
    const detail = candidateDetail({ content: maliciousText });
    tauri.invoke.mockImplementation(async (command: string) => {
      if (command === "get_snapshot") {
        return bridgeSnapshot(snapshot({ candidates: [summary] }));
      }
      if (command === "get_candidate") return bridgeCandidateGet(detail);
      return commandResult(command);
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Review" }));
    expect((await screen.findAllByText(maliciousText)).length).toBeGreaterThanOrEqual(2);
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("script")).toBeNull();
    expect(document.querySelector("a[href='x']")).toBeNull();

    await user.click(screen.getByRole("button", { name: "View sources" }));
    const drawer = await screen.findByRole("dialog", { name: "Proposal sources" });
    expect(await within(drawer).findByText(maliciousText)).toBeInTheDocument();
    expect(within(drawer).getAllByText(maliciousText)[0]?.closest("bdi")).not.toBeNull();
    expect((await within(drawer).findByText("current")).closest(".status-badge")).toHaveClass(
      "status-badge--positive",
    );
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("a")).toHaveClass("skip-link");
    expect(tauri.invoke.mock.calls.some(([command]) => command === "approve_candidate")).toBe(false);
  });

  it("disables direct approval for a conflicting proposal", async () => {
    const user = userEvent.setup();
    const summary = candidateSummary({ status: "conflict" });
    const detail = candidateDetail({ status: "conflict" });
    tauri.invoke.mockImplementation(async (command: string) => {
      if (command === "get_snapshot") {
        return bridgeSnapshot(snapshot({
          candidates: [summary],
          review_counts: { pending: 0, conflict: 1, applying: 0, accepted: 0, rejected: 0 },
        }));
      }
      if (command === "get_candidate") return bridgeCandidateGet(detail);
      return commandResult(command);
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Review" }));
    expect(await screen.findByRole("heading", { name: "Conflicting memory needs resolution" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save reviewed memory" })).toBeDisabled();
  });

  it("offers an idempotent resume action for an interrupted applying proposal", async () => {
    const user = userEvent.setup();
    const summary = candidateSummary({ status: "applying" });
    const detail = candidateDetail({ status: "applying" });
    tauri.invoke.mockImplementation(async (command: string) => {
      if (command === "get_snapshot") {
        return bridgeSnapshot(snapshot({
          candidates: [summary],
          review_counts: { pending: 0, conflict: 0, applying: 1, accepted: 0, rejected: 0 },
        }));
      }
      if (command === "get_candidate") return bridgeCandidateGet(detail);
      return commandResult(command);
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Review" }));
    expect(await screen.findByRole("heading", { name: "A previous save needs to resume" })).toBeInTheDocument();
    expect(screen.getByLabelText("1 need review")).toBeInTheDocument();
    const resume = screen.getByRole("button", { name: "Resume saving memory" });
    expect(resume).toBeEnabled();
    await user.click(resume);

    await waitFor(() =>
      expect(tauri.invoke).toHaveBeenCalledWith("approve_candidate", {
        request: { candidate_id: "cand-1", expected_version: 3 },
      }),
    );
  });

  it("clears the old proposal before a newly selected proposal fails to load", async () => {
    const user = userEvent.setup();
    const firstSummary = candidateSummary({ id: "cand-a", content_preview: "A preview" });
    const secondSummary = candidateSummary({ id: "cand-b", content_preview: "B preview" });
    tauri.invoke.mockImplementation(async (command: string, args?: { request?: { candidate_id?: string } }) => {
      if (command === "get_snapshot") {
        return bridgeSnapshot(snapshot({
          candidates: [firstSummary, secondSummary],
          review_counts: { pending: 2, conflict: 0, applying: 0, accepted: 0, rejected: 0 },
        }));
      }
      if (command === "get_candidate" && args?.request?.candidate_id === "cand-a") {
        return bridgeCandidateGet(candidateDetail({ id: "cand-a", content: "A detail only" }));
      }
      if (command === "get_candidate" && args?.request?.candidate_id === "cand-b") {
        throw { code: "NOT_FOUND", message: "B detail unavailable" };
      }
      return commandResult(command);
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Review" }));
    expect(await screen.findByText("A detail only")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /B preview/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("B detail unavailable");
    expect(screen.queryByText("A detail only")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save reviewed memory" })).not.toBeInTheDocument();
  });

  it("surfaces a CAS conflict, reloads, and never overwrites silently", async () => {
    const user = userEvent.setup();
    let detailReads = 0;
    tauri.invoke.mockImplementation(async (command: string) => {
      if (command === "get_snapshot") return bridgeSnapshot();
      if (command === "get_candidate") {
        detailReads += 1;
        return bridgeCandidateGet(candidateDetail({ version: detailReads === 1 ? 3 : 4, content: detailReads === 1 ? "Old proposal" : "Changed elsewhere" }));
      }
      if (command === "edit_candidate") {
        throw { code: "VERSION_CONFLICT", message: "candidate version changed" };
      }
      return commandResult(command);
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Review" }));
    await screen.findByText("Old proposal");
    await user.click(screen.getByRole("button", { name: "Edit proposal" }));
    const textarea = screen.getByRole("textbox", { name: "Memory text" });
    await user.clear(textarea);
    await user.type(textarea, "My reviewed text");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      await screen.findByText(/changed since you opened it.*No change was applied/is),
    ).toBeInTheDocument();
    expect(await screen.findByText("Changed elsewhere")).toBeInTheDocument();
    expect(tauri.invoke).toHaveBeenCalledWith("edit_candidate", {
      request: expect.objectContaining({ candidate_id: "cand-1", expected_version: 3 }),
    });
  });

  it("preserves direct evidence after a mutation response that omits evidence", async () => {
    const user = userEvent.setup();
    tauri.invoke.mockImplementation(async (command: string) => {
      if (command === "edit_candidate") {
        return bridgeCandidateMutation(candidateDetail({ content: "Reviewed proposal", version: 4 }));
      }
      return commandResult(command);
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Review" }));
    expect(await screen.findByText("1 direct source(s)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Edit proposal" }));
    const textarea = screen.getByRole("textbox", { name: "Memory text" });
    await user.clear(textarea);
    await user.type(textarea, "Reviewed proposal");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText("Changes saved to the proposal. Nothing was added to durable memory.")).toBeInTheDocument();
    expect(screen.getByText("1 direct source(s)")).toBeInTheDocument();
    expect(tauri.invoke.mock.calls.filter(([command]) => command === "get_candidate")).toHaveLength(1);
  });

  it("shows partial coverage without mutation actions", async () => {
    const user = userEvent.setup();
    const summary = wrapSummary({ coverage_status: "partial", revision: 2 });
    const detail = wrapDetail({
      coverage_status: "partial",
      revision: 2,
      output: {
        ...wrapDetail().output!,
        status: "partial",
        coverage_gaps: ["timeline_not_covered_through_day_end"],
      },
    });
    tauri.invoke.mockImplementation(async (command: string) => {
      if (command === "get_snapshot") return bridgeSnapshot(snapshot({ daily_wraps: [summary] }));
      if (command === "get_daily_wrap") return bridgeWrapGet(detail);
      return commandResult(command);
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Daily Wrap" }));
    expect(await screen.findByRole("heading", { name: "Partial coverage" })).toBeInTheDocument();
    expect(screen.getByText("Published revision 2")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /accept/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /ignore/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /send/i })).not.toBeInTheDocument();
  });

  it("does not expose mutable refresh state for a published revision", async () => {
    const user = userEvent.setup();
    const summary = wrapSummary({ revision: 1 });
    const detail = wrapDetail({ revision: 1 });
    tauri.invoke.mockImplementation(async (command: string) => {
      if (command === "get_snapshot") return bridgeSnapshot(snapshot({ daily_wraps: [summary] }));
      if (command === "get_daily_wrap") return bridgeWrapGet(detail);
      return commandResult(command);
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Daily Wrap" }));
    expect(await screen.findByText("Published revision 1")).toBeInTheDocument();
    expect(screen.queryByText(/generation is in progress/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/refresh failed/i)).not.toBeInTheDocument();
  });

  it("binds permanent forget to the reviewed plan digest and delegates final confirmation", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm");
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Review" }));
    expect((await screen.findAllByText("Decided to keep the trusted boundary.")).length).toBeGreaterThanOrEqual(2);
    await user.click(screen.getByRole("button", { name: "Review permanent forget…" }));

    expect(await screen.findByRole("heading", { name: "Deletion impact" })).toBeInTheDocument();
    expect(screen.getByText(/Original captures and timeline sources may remain/i)).toBeInTheDocument();
    expect(screen.getByText("candidate-created.md").closest("bdi")).not.toBeNull();
    expect(screen.getByText("entry-1").closest("bdi")).not.toBeNull();
    expect(screen.getByText("daily-wrap-1").closest("bdi")).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "Continue to system confirmation" }));

    await waitFor(() =>
      expect(tauri.invoke).toHaveBeenCalledWith("forget_candidate", {
        request: {
          candidate_id: "cand-1",
          expected_version: 3,
          plan_digest: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        },
      }),
    );
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("locks proposal navigation while a deletion preview is being prepared", async () => {
    const user = userEvent.setup();
    let releasePreview: ((value: typeof forgetPreview) => void) | undefined;
    tauri.invoke.mockImplementation(async (command: string) => {
      if (command === "preview_forget_candidate") {
        return new Promise<typeof forgetPreview>((resolve) => {
          releasePreview = resolve;
        });
      }
      return commandResult(command);
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Review" }));
    expect((await screen.findAllByText("Decided to keep the trusted boundary.")).length).toBeGreaterThanOrEqual(2);
    await user.click(screen.getByRole("button", { name: "Review permanent forget…" }));

    expect(screen.getByRole("button", { name: "Accepted" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "All" })).toBeDisabled();
    releasePreview?.(forgetPreview);
    expect(await screen.findByRole("heading", { name: "Deletion impact" })).toBeInTheDocument();
  });

  it("keeps the deletion preview after USER_CANCELLED without showing an error", async () => {
    const user = userEvent.setup();
    tauri.invoke.mockImplementation(async (command: string) => {
      if (command === "forget_candidate") {
        throw { code: "USER_CANCELLED", message: "cancelled in native confirmation" };
      }
      return commandResult(command);
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Review" }));
    expect((await screen.findAllByText("Decided to keep the trusted boundary.")).length).toBeGreaterThanOrEqual(2);
    await user.click(screen.getByRole("button", { name: "Review permanent forget…" }));
    await screen.findByRole("heading", { name: "Deletion impact" });
    await user.click(screen.getByRole("button", { name: "Continue to system confirmation" }));

    expect(await screen.findByText("System confirmation was cancelled. Nothing was deleted.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Deletion impact" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("invalidates a stale purge preview and requires a fresh impact review", async () => {
    const user = userEvent.setup();
    tauri.invoke.mockImplementation(async (command: string) => {
      if (command === "forget_candidate") {
        throw { code: "STALE_PURGE_PLAN", message: "purge closure changed" };
      }
      return commandResult(command);
    });
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Review" }));
    expect((await screen.findAllByText("Decided to keep the trusted boundary.")).length).toBeGreaterThanOrEqual(2);
    await user.click(screen.getByRole("button", { name: "Review permanent forget…" }));
    await screen.findByRole("heading", { name: "Deletion impact" });
    await user.click(screen.getByRole("button", { name: "Continue to system confirmation" }));

    expect(await screen.findByText(/deletion impact changed.*Nothing was deleted/is)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Deletion impact" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review permanent forget…" })).toBeInTheDocument();
  });

  it("closes the non-modal source drawer with Escape and restores focus", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.click(await screen.findByRole("button", { name: "Review" }));
    const sourceButton = await screen.findByRole("button", { name: "View sources" });
    sourceButton.focus();
    await user.click(sourceButton);
    expect(await screen.findByRole("dialog", { name: "Proposal sources" })).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Proposal sources" })).not.toBeInTheDocument();
    expect(sourceButton).toHaveFocus();
  });

  it("does not attribute a previous subject's sources when the next trace fails", async () => {
    tauri.invoke.mockImplementation(async (command: string, args?: { request?: { artifact_id?: string } }) => {
      if (command === "trace_provenance" && args?.request?.artifact_id === "subject-a") {
        return {
          subject: { kind: "memory_candidate", id: "subject-a" },
          direct_sources: [
            {
              kind: "timeline_block",
              id: "source-a",
              timestamp: "A-only source timestamp",
              availability: "available",
              integrity: "current",
            },
          ],
          trace: [],
        };
      }
      if (command === "trace_provenance" && args?.request?.artifact_id === "subject-b") {
        throw { code: "NOT_FOUND", message: "B trace unavailable" };
      }
      if (command === "resolve_evidence") return bridgeResolvedEvidence();
      return commandResult(command);
    });
    const view = render(
      <SourceDrawer
        api={desktopApi}
        onClose={() => undefined}
        subject={{ kind: "memory_candidate", id: "subject-a", label: "A sources" }}
      />,
    );

    expect(await screen.findByText("A-only source timestamp")).toBeInTheDocument();
    view.rerender(
      <SourceDrawer
        api={desktopApi}
        onClose={() => undefined}
        subject={{ kind: "memory_candidate", id: "subject-b", label: "B sources" }}
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("B trace unavailable");
    expect(screen.queryByText("A-only source timestamp")).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "B sources" })).toBeInTheDocument();
  });
});
