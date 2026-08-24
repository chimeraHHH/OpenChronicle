import { useState } from "react";

import type { DesktopApi } from "../api";
import type {
  ResumeCue,
  SourceSubject,
  Suggestion,
  SuggestionDismissalReason,
  SuggestionFeedbackSummary,
} from "../contracts";
import { displayError, formatDateTime } from "../format";
import { StatusBadge } from "../components/StatusBadge";
import { UntrustedText } from "../components/UntrustedText";

interface SuggestionsPageProps {
  api: DesktopApi;
  enabled: boolean;
  feedback: SuggestionFeedbackSummary;
  resumeCues: ResumeCue[];
  suggestions: Suggestion[];
  onChanged: () => Promise<void>;
  onOpenSource: (subject: SourceSubject) => void;
}

const dismissalChoices: Array<{
  reason: Exclude<SuggestionDismissalReason, "legacy_or_unspecified">;
  label: string;
}> = [
  { reason: "not_relevant", label: "Not relevant" },
  { reason: "wrong_timing", label: "Wrong timing" },
  { reason: "already_resolved", label: "Already resolved" },
  { reason: "too_vague", label: "Too vague" },
  { reason: "other", label: "Other" },
];

function feedbackLabel(reason: SuggestionDismissalReason) {
  return dismissalChoices.find((choice) => choice.reason === reason)?.label ?? "Unspecified";
}

export function SuggestionsPage({
  api,
  enabled,
  feedback,
  resumeCues,
  suggestions,
  onChanged,
  onOpenSource,
}: SuggestionsPageProps) {
  const [busyId, setBusyId] = useState("");
  const [error, setError] = useState("");
  const [dismissId, setDismissId] = useState("");
  const [dismissReason, setDismissReason] = useState<
    Exclude<SuggestionDismissalReason, "legacy_or_unspecified">
  >("not_relevant");
  const [taskLabel, setTaskLabel] = useState("");
  const [nextStep, setNextStep] = useState("");

  async function transition(
    suggestion: Suggestion,
    status: "accepted" | "dismissed",
    reason: string,
  ) {
    setBusyId(suggestion.id);
    setError("");
    try {
      await api.transitionSuggestion(
        suggestion.id,
        suggestion.version,
        status,
        reason,
      );
      setDismissId("");
      await onChanged();
    } catch (reason: unknown) {
      setError(displayError(reason));
    } finally {
      setBusyId("");
    }
  }

  async function createResumeCue() {
    setBusyId("resume-cue-create");
    setError("");
    try {
      await api.createResumeCue(taskLabel, nextStep);
      setTaskLabel("");
      setNextStep("");
      await onChanged();
    } catch (reason: unknown) {
      setError(displayError(reason));
    } finally {
      setBusyId("");
    }
  }

  async function closeResumeCue(cue: ResumeCue, status: "resumed" | "dismissed") {
    setBusyId(cue.id);
    setError("");
    try {
      await api.transitionResumeCue(cue.id, cue.version, status);
      await onChanged();
    } catch (reason: unknown) {
      setError(displayError(reason));
    } finally {
      setBusyId("");
    }
  }

  return (
    <main className="page" id="main-content" tabIndex={-1}>
      <header className="page-header">
        <div>
          <p className="eyebrow">Local suggestion plane</p>
          <h1>Suggestions</h1>
          <p>
            Evidence-backed cards only. This Stage 2 surface cannot type, paste, send, or run tools.
          </p>
        </div>
        <StatusBadge tone={enabled ? "positive" : "neutral"}>
          {enabled ? "Opted in" : "Off"}
        </StatusBadge>
      </header>

      {error ? <div className="global-error" role="alert"><UntrustedText>{error}</UntrustedText></div> : null}

      <section className="summary-card" aria-labelledby="park-task-heading">
        <p className="eyebrow">Explicit resumption cue</p>
        <h2 id="park-task-heading">Park the current task</h2>
        {resumeCues[0] ? (
          <>
            <p><strong><UntrustedText>{resumeCues[0].task_label}</UntrustedText></strong></p>
            <p><UntrustedText>{resumeCues[0].next_step}</UntrustedText></p>
            <p className="muted">
              User-authored locally at {formatDateTime(resumeCues[0].created_at)}. It will be
              shown only after a later verified activity gap; no same-task match is claimed.
            </p>
            <div className="button-row">
              <button
                className="button button--primary"
                disabled={busyId === resumeCues[0].id}
                onClick={() => void closeResumeCue(resumeCues[0]!, "resumed")}
                type="button"
              >
                Mark resumed
              </button>
              <button
                className="button button--ghost"
                disabled={busyId === resumeCues[0].id}
                onClick={() => void closeResumeCue(resumeCues[0]!, "dismissed")}
                type="button"
              >
                Dismiss cue
              </button>
            </div>
          </>
        ) : (
          <form
            className="edit-form"
            onSubmit={(event) => {
              event.preventDefault();
              void createResumeCue();
            }}
          >
            <label>
              Task label
              <input
                maxLength={120}
                onChange={(event) => setTaskLabel(event.target.value)}
                required
                value={taskLabel}
              />
            </label>
            <label>
              Exact next step
              <textarea
                maxLength={1000}
                onChange={(event) => setNextStep(event.target.value)}
                required
                rows={3}
                value={nextStep}
              />
            </label>
            <p className="muted">Stored locally as written. No model or network is used.</p>
            <button
              className="button button--primary"
              disabled={busyId === "resume-cue-create"}
              type="submit"
            >
              Park task
            </button>
          </form>
        )}
      </section>

      {enabled ? (
        <section className="summary-card" aria-labelledby="suggestion-feedback-heading">
          <p className="eyebrow">Local outcome history</p>
          <h2 id="suggestion-feedback-heading">Suggestion feedback</h2>
          {feedback.sample_size > 0 ? (
            <>
              <p>
                {feedback.accepted} helpful · {feedback.dismissed} dismissed · {Math.round(feedback.acceptance_rate * 100)}% acknowledged
              </p>
              {feedback.dismissal_reasons.length > 0 ? (
                <ul>
                  {feedback.dismissal_reasons.map((item) => (
                    <li key={item.reason}>{feedbackLabel(item.reason)}: {item.count}</li>
                  ))}
                </ul>
              ) : null}
              <p className="muted">
                This is a content-free local outcome summary, not a quality score. No model or
                network is used.
              </p>
            </>
          ) : (
            <p>No accepted or dismissed suggestions yet.</p>
          )}
        </section>
      ) : null}

      {!enabled ? (
        <section className="empty-panel">
          <h2>Proactive cards are off</h2>
          <p>Enable the local suggestion detector in config when you want to try it.</p>
        </section>
      ) : suggestions.length === 0 ? (
        <section className="empty-panel">
          <h2>No current opportunities</h2>
          <p>Quiet hours, cooldown, evidence policy, and the daily interruption budget still apply.</p>
        </section>
      ) : (
        <div className="review-layout">
          <section className="review-list" aria-label="Current suggestions">
            {suggestions.map((suggestion) => (
              <article className="summary-card" key={suggestion.id}>
                <div className="review-list__meta">
                  <StatusBadge tone="positive">{suggestion.status}</StatusBadge>
                  <span>{Math.round(suggestion.score * 100)}% · expires {formatDateTime(suggestion.expires_at)}</span>
                </div>
                <h2>{suggestion.title}</h2>
                <p>{suggestion.summary}</p>
                <p className="muted">
                  Verified gap: {suggestion.artifact.interruption.gap_minutes} minutes
                </p>
                {suggestion.artifact.schema_version === 2 ? (
                  <div className="warning-panel">
                    <h3>User-authored parked cue</h3>
                    <p><strong><UntrustedText>{suggestion.artifact.parked_cue.task_label}</UntrustedText></strong></p>
                    <p><UntrustedText>{suggestion.artifact.parked_cue.next_step}</UntrustedText></p>
                  </div>
                ) : null}
                <div className="warning-panel">
                  <h3>Last verified activity (untrusted text)</h3>
                  {suggestion.artifact.last_verified_state.entries.map((entry, index) => (
                    <p key={`${suggestion.id}-previous-${index}`}><UntrustedText>{entry}</UntrustedText></p>
                  ))}
                </div>
                <p>{suggestion.artifact.recommended_next_step}</p>
                <div className="button-row">
                  <button
                    className="button button--secondary"
                    disabled={busyId === suggestion.id}
                    onClick={() => onOpenSource({
                      kind: "suggestion",
                      id: suggestion.id,
                      label: suggestion.title,
                    })}
                    type="button"
                  >
                    View sources
                  </button>
                  <button
                    className="button button--ghost"
                    disabled={busyId === suggestion.id}
                    onClick={() => {
                      setDismissReason("not_relevant");
                      setDismissId(dismissId === suggestion.id ? "" : suggestion.id);
                    }}
                    type="button"
                  >
                    Dismiss
                  </button>
                  <button
                    className="button button--primary"
                    disabled={busyId === suggestion.id}
                    onClick={() => void transition(suggestion, "accepted", "helpful")}
                    type="button"
                  >
                    Helpful — acknowledge
                  </button>
                </div>
                {dismissId === suggestion.id ? (
                  <fieldset className="edit-form">
                    <legend>Why dismiss this suggestion?</legend>
                    {dismissalChoices.map((choice) => (
                      <label className="feedback-choice" key={choice.reason}>
                        <input
                          checked={dismissReason === choice.reason}
                          name={`dismiss-${suggestion.id}`}
                          onChange={() => setDismissReason(choice.reason)}
                          type="radio"
                        />
                        {choice.label}
                      </label>
                    ))}
                    <div className="button-row">
                      <button
                        className="button button--danger-outline"
                        disabled={busyId === suggestion.id}
                        onClick={() => void transition(suggestion, "dismissed", dismissReason)}
                        type="button"
                      >
                        Confirm dismiss
                      </button>
                      <button
                        className="button button--ghost"
                        disabled={busyId === suggestion.id}
                        onClick={() => setDismissId("")}
                        type="button"
                      >
                        Cancel
                      </button>
                    </div>
                  </fieldset>
                ) : null}
              </article>
            ))}
          </section>
        </div>
      )}
    </main>
  );
}
