import { useState } from "react";

import type { DesktopApi } from "../api";
import type { SourceSubject, Suggestion } from "../contracts";
import { displayError, formatDateTime } from "../format";
import { StatusBadge } from "../components/StatusBadge";
import { UntrustedText } from "../components/UntrustedText";

interface SuggestionsPageProps {
  api: DesktopApi;
  enabled: boolean;
  suggestions: Suggestion[];
  onChanged: () => Promise<void>;
  onOpenSource: (subject: SourceSubject) => void;
}

export function SuggestionsPage({
  api,
  enabled,
  suggestions,
  onChanged,
  onOpenSource,
}: SuggestionsPageProps) {
  const [busyId, setBusyId] = useState("");
  const [error, setError] = useState("");

  async function transition(
    suggestion: Suggestion,
    status: "accepted" | "dismissed",
  ) {
    setBusyId(suggestion.id);
    setError("");
    try {
      await api.transitionSuggestion(
        suggestion.id,
        suggestion.version,
        status,
        status === "dismissed" ? "dismissed_from_desktop" : "acknowledged_from_desktop",
      );
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
                    onClick={() => void transition(suggestion, "dismissed")}
                    type="button"
                  >
                    Dismiss
                  </button>
                  <button
                    className="button button--primary"
                    disabled={busyId === suggestion.id}
                    onClick={() => void transition(suggestion, "accepted")}
                    type="button"
                  >
                    Acknowledge only
                  </button>
                </div>
              </article>
            ))}
          </section>
        </div>
      )}
    </main>
  );
}
