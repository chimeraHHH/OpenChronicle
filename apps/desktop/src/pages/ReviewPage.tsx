import { useEffect, useMemo, useState } from "react";

import { DesktopApiError, type DesktopApi } from "../api";
import type {
  Candidate,
  CandidateSummary,
  ForgetPreview,
  SourceSubject,
} from "../contracts";
import { displayError, formatDateTime, titleCase } from "../format";
import { StatusBadge } from "../components/StatusBadge";
import { UntrustedText } from "../components/UntrustedText";

interface ReviewPageProps {
  api: DesktopApi;
  candidates: CandidateSummary[];
  onChanged: () => Promise<void>;
  onForgotten: () => void;
  onOpenSource: (subject: SourceSubject) => void;
}

type ReviewFilter = "needs-review" | "accepted" | "rejected" | "all";

function statusTone(status: CandidateSummary["status"]) {
  if (status === "accepted") return "positive" as const;
  if (status === "conflict") return "warning" as const;
  if (status === "applying") return "info" as const;
  if (status === "rejected") return "neutral" as const;
  return "info" as const;
}

const staleCodes = new Set([
  "VERSION_CONFLICT",
  "CANDIDATE_VERSION_CONFLICT",
  "STALE_VERSION",
  "STALE_PURGE_PLAN",
]);

function normalizedErrorCode(error: unknown) {
  return error instanceof DesktopApiError ? error.code.trim().toUpperCase() : "";
}

function isStaleState(error: unknown) {
  return staleCodes.has(normalizedErrorCode(error));
}

export function ReviewPage({
  api,
  candidates,
  onChanged,
  onForgotten,
  onOpenSource,
}: ReviewPageProps) {
  const [filter, setFilter] = useState<ReviewFilter>("needs-review");
  const visibleCandidates = useMemo(() => {
    if (filter === "needs-review") {
      return candidates.filter((candidate) =>
        ["pending", "conflict", "applying"].includes(candidate.status),
      );
    }
    if (filter === "accepted") return candidates.filter((candidate) => candidate.status === "accepted");
    if (filter === "rejected") return candidates.filter((candidate) => candidate.status === "rejected");
    return candidates;
  }, [candidates, filter]);
  const [selectedId, setSelectedId] = useState<string | null>(visibleCandidates[0]?.id ?? null);
  const [candidate, setCandidate] = useState<Candidate | null>(null);
  const [content, setContent] = useState("");
  const [tags, setTags] = useState("");
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [cancellation, setCancellation] = useState("");
  const [forgetPreview, setForgetPreview] = useState<ForgetPreview | null>(null);

  useEffect(() => {
    if (!visibleCandidates.some((value) => value.id === selectedId)) {
      setSelectedId(visibleCandidates[0]?.id ?? null);
    }
  }, [selectedId, visibleCandidates]);

  useEffect(() => {
    if (!selectedId) {
      setCandidate(null);
      return;
    }
    let current = true;
    setCandidate(null);
    setBusy(true);
    setError("");
    setNotice("");
    setCancellation("");
    setForgetPreview(null);
    void api
      .getCandidate(selectedId)
      .then((value) => {
        if (!current) return;
        setCandidate(value);
        setContent(value.content);
        setTags(value.tags.join(", "));
        setEditing(false);
      })
      .catch((reason: unknown) => {
        if (current) setError(displayError(reason));
      })
      .finally(() => {
        if (current) setBusy(false);
      });
    return () => {
      current = false;
    };
  }, [api, selectedId]);

  async function reloadCandidate() {
    if (!candidate || candidate.id !== selectedId) return;
    const next = await api.getCandidate(candidate.id);
    setCandidate(next);
    setContent(next.content);
    setTags(next.tags.join(", "));
    setEditing(false);
  }

  async function runMutation(action: () => Promise<Candidate>, success: string) {
    setBusy(true);
    setError("");
    setNotice("");
    setForgetPreview(null);
    try {
      const next = await action();
      const hydrated =
        candidate && next.id === candidate.id && next.evidence.length === 0
          ? {
              ...next,
              evidence: candidate.evidence,
              evidence_count: candidate.evidence.length,
            }
          : next;
      setCandidate(hydrated);
      setContent(hydrated.content);
      setTags(hydrated.tags.join(", "));
      setEditing(false);
      setNotice(success);
      try {
        await onChanged();
      } catch {
        setError("The proposal change was applied, but the inbox totals could not be refreshed.");
      }
    } catch (reason: unknown) {
      if (isStaleState(reason)) {
        setError("This proposal changed since you opened it. No change was applied; review the latest version.");
        setCandidate(null);
        try {
          await reloadCandidate();
          await onChanged();
        } catch {
          // The original actionable error remains visible.
        }
      } else {
        setError(displayError(reason));
      }
    } finally {
      setBusy(false);
    }
  }

  async function saveEdit() {
    if (!candidate || candidate.id !== selectedId) return;
    await runMutation(
      () =>
        api.editCandidate({
          candidateId: candidate.id,
          expectedVersion: candidate.version,
          content,
          tags: tags
            .split(",")
            .map((tag) => tag.trim())
            .filter(Boolean),
        }),
      "Changes saved to the proposal. Nothing was added to durable memory.",
    );
  }

  async function previewForget() {
    if (!candidate || candidate.id !== selectedId) return;
    setBusy(true);
    setError("");
    setNotice("");
    setCancellation("");
    try {
      const preview = await api.previewForgetCandidate(candidate.id, candidate.version);
      setForgetPreview(preview);
    } catch (reason: unknown) {
      if (isStaleState(reason)) {
        setError("This proposal changed before the deletion preview was prepared. Review the latest version.");
        setCandidate(null);
        try {
          await reloadCandidate();
        } catch {
          // Keep the stale-preview message; the old proposal is no longer actionable.
        }
      } else {
        setError(displayError(reason));
      }
    } finally {
      setBusy(false);
    }
  }

  async function commitForget() {
    if (
      !forgetPreview ||
      !candidate ||
      candidate.id !== selectedId ||
      forgetPreview.candidate_id !== candidate.id ||
      forgetPreview.expected_version !== candidate.version
    ) {
      setForgetPreview(null);
      setError("The deletion preview no longer matches the selected proposal. Review a new preview.");
      return;
    }
    setBusy(true);
    setError("");
    setCancellation("");
    try {
      await api.forgetCandidate(forgetPreview);
      setCandidate(null);
      setSelectedId(null);
      setForgetPreview(null);
      onForgotten();
      setNotice("Permanent local purge was authorized and affected content is now hidden.");
      try {
        await onChanged();
      } catch {
        setError("Permanent local purge completed, but the inbox totals could not be refreshed.");
      }
    } catch (reason: unknown) {
      const code = normalizedErrorCode(reason);
      if (code === "USER_CANCELLED") {
        setCancellation("System confirmation was cancelled. Nothing was deleted.");
      } else if (code === "STALE_PURGE_PLAN" || code === "VERSION_CONFLICT") {
        setForgetPreview(null);
        setError("The deletion impact changed before confirmation. Nothing was deleted; review a new preview.");
        setCandidate(null);
        try {
          await reloadCandidate();
          await onChanged();
        } catch {
          // Keep the actionable stale-plan message visible.
        }
      } else {
        setError(displayError(reason));
      }
    } finally {
      setBusy(false);
    }
  }

  const selectionIsCurrent =
    candidate !== null &&
    candidate.id === selectedId &&
    visibleCandidates.some((summary) => summary.id === selectedId);
  const canReview =
    selectionIsCurrent && (candidate.status === "pending" || candidate.status === "conflict");
  const canApprove =
    selectionIsCurrent && (candidate.status === "pending" || candidate.status === "applying");
  const approveDisabled = busy || !canApprove;
  const currentForgetPreview =
    selectionIsCurrent &&
    forgetPreview?.candidate_id === candidate.id &&
    forgetPreview.expected_version === candidate.version
      ? forgetPreview
      : null;

  return (
    <main className="page page--split" id="main-content" tabIndex={-1}>
      <section className="collection-panel" aria-labelledby="review-heading">
        <header className="collection-panel__header">
          <p className="eyebrow">Human decision required</p>
          <h1 id="review-heading">Review Inbox</h1>
          <div className="segmented-control" aria-label="Filter proposals">
            {(["needs-review", "accepted", "rejected", "all"] as const).map((value) => (
              <button
                aria-pressed={filter === value}
                disabled={busy}
                key={value}
                onClick={() => setFilter(value)}
                type="button"
              >
                {value === "needs-review" ? "Needs review" : titleCase(value)}
              </button>
            ))}
          </div>
        </header>
        <ul className="collection-list">
          {visibleCandidates.map((summary) => (
            <li key={summary.id}>
              <button
                aria-current={selectedId === summary.id ? "true" : undefined}
                className="collection-list__button"
                disabled={busy}
                onClick={() => setSelectedId(summary.id)}
                type="button"
              >
                <span className="collection-list__topline">
                  <StatusBadge tone={statusTone(summary.status)}>{titleCase(summary.status)}</StatusBadge>
                  <small>v{summary.version}</small>
                </span>
                <UntrustedText className="line-clamp">{summary.content_preview}</UntrustedText>
                <small><UntrustedText>{summary.target_path}</UntrustedText></small>
              </button>
            </li>
          ))}
          {visibleCandidates.length === 0 ? (
            <li className="empty-list">No proposals in this view.</li>
          ) : null}
        </ul>
      </section>

      <section className="detail-panel" aria-busy={busy} aria-live="polite">
        {error ? <UntrustedText as="p" className="error-banner" role="alert">{error}</UntrustedText> : null}
        {notice ? <p className="success-panel" role="status">{notice}</p> : null}
        {cancellation ? <p className="trust-note" role="status">{cancellation}</p> : null}
        {busy && !candidate ? <p role="status">Loading proposal…</p> : null}
        {selectionIsCurrent && candidate ? (
          <>
            <header className="detail-header">
              <div>
                <p className="eyebrow"><bdi>{titleCase(candidate.kind)}</bdi> proposal</p>
                <h2>Review proposed memory</h2>
                <p>Created <bdi>{formatDateTime(candidate.created_at)}</bdi> · version {candidate.version}</p>
              </div>
              <StatusBadge tone={statusTone(candidate.status)}>{titleCase(candidate.status)}</StatusBadge>
            </header>

            {candidate.status === "conflict" ? (
              <section className="warning-panel" id="conflict-warning" aria-labelledby="conflict-title">
                <h3 id="conflict-title">Conflicting memory needs resolution</h3>
                <p>
                  Direct approval is disabled. Compare the conflicting facts, edit this proposal, or
                  reject it; OpenChronicle will not silently merge them.
                </p>
              </section>
            ) : null}
            {candidate.status === "applying" ? (
              <section className="trust-note" aria-labelledby="applying-title">
                <h3 id="applying-title">A previous save needs to resume</h3>
                <p>
                  OpenChronicle may have written part of this reviewed memory before it stopped.
                  Resume uses the same idempotent local entry and does not duplicate it.
                </p>
              </section>
            ) : null}

            <section aria-labelledby="proposal-content-heading">
              <div className="section-heading-row">
                <div>
                  <h3 id="proposal-content-heading">Proposed content</h3>
                  {editing ? <small>User edits remain linked to the cited context.</small> : null}
                </div>
                {canReview && !editing ? (
                  <button className="text-button" onClick={() => setEditing(true)} type="button">
                    Edit proposal
                  </button>
                ) : null}
              </div>
              {editing ? (
                <div className="edit-form">
                  <label>
                    <span>Memory text</span>
                    <textarea
                      onChange={(event) => setContent(event.currentTarget.value)}
                      rows={8}
                      value={content}
                    />
                  </label>
                  <label>
                    <span>Tags, separated by commas</span>
                    <input onChange={(event) => setTags(event.currentTarget.value)} value={tags} />
                  </label>
                  <div className="button-row">
                    <button className="button button--primary" disabled={busy} onClick={() => void saveEdit()} type="button">
                      Save changes
                    </button>
                    <button
                      className="button button--ghost"
                      disabled={busy}
                      onClick={() => {
                        setContent(candidate.content);
                        setTags(candidate.tags.join(", "));
                        setEditing(false);
                      }}
                      type="button"
                    >
                      Cancel editing
                    </button>
                  </div>
                  <p className="boundary-note">Save changes updates only this proposal; it does not write durable memory.</p>
                </div>
              ) : (
                <UntrustedText as="pre" className="proposal-text">{candidate.content}</UntrustedText>
              )}
            </section>

            <dl className="definition-grid">
              <dt>Target</dt>
              <dd><UntrustedText>{candidate.target_path}</UntrustedText></dd>
              <dt>Operation</dt>
              <dd><bdi>{titleCase(candidate.operation)}</bdi></dd>
              <dt>Tags</dt>
              <dd>
                {candidate.tags.length > 0
                  ? candidate.tags.map((tag, index) => (
                      <span key={`${tag}-${index}`}><bdi>{tag}</bdi>{index < candidate.tags.length - 1 ? ", " : ""}</span>
                    ))
                  : "None"}
              </dd>
              <dt>Evidence</dt>
              <dd>{candidate.evidence.length} direct source(s)</dd>
            </dl>

            <div className="button-row button-row--review">
              <button
                className="button button--secondary"
                onClick={() =>
                  onOpenSource({
                    kind: "memory_candidate",
                    id: candidate.id,
                    label: "Proposal sources",
                  })
                }
                type="button"
              >
                View sources
              </button>
              <button
                aria-describedby={candidate.status === "conflict" ? "conflict-warning" : undefined}
                className="button button--primary"
                disabled={approveDisabled}
                onClick={() =>
                  void runMutation(
                    () => api.approveCandidate(candidate.id, candidate.version),
                    "Reviewed memory saved locally.",
                  )
                }
                type="button"
              >
                {candidate.status === "applying" ? "Resume saving memory" : "Save reviewed memory"}
              </button>
              <button
                className="button button--secondary"
                disabled={busy || !canReview}
                onClick={() =>
                  void runMutation(
                    () => api.rejectCandidate(candidate.id, candidate.version),
                    "Proposal rejected. It remains in local review history and its sources were not deleted.",
                  )
                }
                type="button"
              >
                Reject proposal
              </button>
            </div>
            <p className="boundary-note">
              Rejecting keeps review history. Saving writes local memory only; neither action sends,
              publishes, or changes another application.
            </p>

            <details className="danger-zone" open={currentForgetPreview !== null}>
              <summary>Permanent local deletion</summary>
              <p>
                Forget removes this proposal, accepted memory derived from it, and affected Daily
                Wraps. Candidate-created containers are removed when empty or sanitized when they
                contain surviving local entries. Original captures and timeline sources may remain
                until their retention period.
              </p>
              {currentForgetPreview ? (
                <div className="purge-preview" aria-live="polite">
                  <h3>Deletion impact</h3>
                  <ul>
                    <li>{currentForgetPreview.counts.candidates} proposal record(s)</li>
                    <li>{currentForgetPreview.counts.memory_files} candidate-created memory file(s) cleaned or removed</li>
                    <li>{currentForgetPreview.counts.memory_entries} durable memory entry or entries</li>
                    <li>{currentForgetPreview.counts.daily_wraps} Daily Wrap(s), including revisions</li>
                  </ul>
                  <div className="purge-targets">
                    <h4>Proposal IDs</h4>
                    <ul>
                      {currentForgetPreview.candidate_ids.map((id) => <li key={id}><bdi>{id}</bdi></li>)}
                    </ul>
                    {currentForgetPreview.files.length > 0 ? (
                      <>
                        <h4>Affected candidate-created memory files</h4>
                        <ul>
                          {currentForgetPreview.files.map((file) => (
                            <li key={file.path}><UntrustedText>{file.path}</UntrustedText></li>
                          ))}
                        </ul>
                      </>
                    ) : null}
                    {currentForgetPreview.entries.length > 0 ? (
                      <>
                        <h4>Durable memory entries</h4>
                        <ul>
                          {currentForgetPreview.entries.map((entry) => (
                            <li key={`${entry.path}-${entry.id}`}>
                              <UntrustedText>{entry.path}</UntrustedText> · <bdi>{entry.id}</bdi>
                            </li>
                          ))}
                        </ul>
                      </>
                    ) : null}
                    {currentForgetPreview.wrap_ids.length > 0 ? (
                      <>
                        <h4>Daily Wrap IDs</h4>
                        <ul>
                          {currentForgetPreview.wrap_ids.map((id) => <li key={id}><bdi>{id}</bdi></li>)}
                        </ul>
                      </>
                    ) : null}
                  </div>
                  <p>No undo is available. Backups, filesystem snapshots, and provider copies are outside this purge.</p>
                  <div className="button-row">
                    <button
                      className="button button--danger"
                      disabled={busy}
                      onClick={() => void commitForget()}
                      type="button"
                    >
                      Continue to system confirmation
                    </button>
                    <button className="button button--ghost" onClick={() => setForgetPreview(null)} type="button">
                      Cancel deletion
                    </button>
                  </div>
                </div>
              ) : (
                <button className="button button--danger-outline" disabled={busy || !selectionIsCurrent} onClick={() => void previewForget()} type="button">
                  Review permanent forget…
                </button>
              )}
            </details>
          </>
        ) : !busy ? (
          <div className="empty-state">
            <h2>Select a proposal</h2>
            <p>Review its exact content and sources before deciding whether it becomes durable memory.</p>
          </div>
        ) : null}
      </section>
    </main>
  );
}
