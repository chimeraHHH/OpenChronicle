import { useState } from "react";

import type { DesktopApi } from "../api";
import type {
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
                    Acknowledge only
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
