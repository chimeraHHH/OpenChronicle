import { useEffect, useMemo, useState } from "react";

import type { DesktopApi } from "../api";
import type { DailyWrap, DailyWrapSummary, SourceSubject, WrapCategory } from "../contracts";
import { displayError, formatDateTime, titleCase } from "../format";
import { StatusBadge } from "../components/StatusBadge";
import { UntrustedText } from "../components/UntrustedText";

interface DailyWrapPageProps {
  api: DesktopApi;
  summaries: DailyWrapSummary[];
  onOpenSource: (subject: SourceSubject) => void;
}

const categories: Array<{ id: WrapCategory; label: string }> = [
  { id: "completed", label: "Completed" },
  { id: "progressed", label: "Progressed" },
  { id: "open", label: "Open" },
  { id: "blocked", label: "Blocked" },
  { id: "needs_review", label: "Needs review" },
];

function wrapTone(wrap: DailyWrap | DailyWrapSummary) {
  if (wrap.status === "failed") return "danger" as const;
  if (wrap.status === "running") return "info" as const;
  if (wrap.coverage_status === "partial") return "warning" as const;
  return "positive" as const;
}

export function DailyWrapPage({ api, summaries, onOpenSource }: DailyWrapPageProps) {
  const [selectedId, setSelectedId] = useState<string | null>(summaries[0]?.id ?? null);
  const selectedSummary = summaries.find((summary) => summary.id === selectedId) ?? summaries[0] ?? null;
  const [wrap, setWrap] = useState<DailyWrap | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!summaries.some((value) => value.id === selectedId)) setSelectedId(summaries[0]?.id ?? null);
  }, [selectedId, summaries]);

  useEffect(() => {
    if (!selectedSummary) {
      setWrap(null);
      return;
    }
    let current = true;
    setLoading(true);
    setError("");
    setWrap(null);
    void api
      .getDailyWrap(selectedSummary.local_date, selectedSummary.timezone, selectedSummary.scope)
      .then((value) => {
        if (current) setWrap(value);
      })
      .catch((reason: unknown) => {
        if (current) setError(displayError(reason));
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [api, selectedSummary]);

  const itemCount = useMemo(() => {
    if (!wrap?.output) return 0;
    return categories.reduce((total, category) => total + wrap.output![category.id].length, 0);
  }, [wrap]);

  return (
    <main className="page page--split" id="main-content" tabIndex={-1}>
      <section className="collection-panel" aria-labelledby="daily-wrap-heading">
        <header className="collection-panel__header">
          <p className="eyebrow">Evidence-backed, read-only</p>
          <h1 id="daily-wrap-heading">Daily Wrap</h1>
          <p>Canonical local days and their retained revisions.</p>
        </header>
        <ul className="collection-list">
          {summaries.map((summary) => (
            <li key={summary.id}>
              <button
                aria-current={selectedSummary?.id === summary.id ? "true" : undefined}
                className="collection-list__button"
                onClick={() => setSelectedId(summary.id)}
                type="button"
              >
                <span className="collection-list__topline">
                  <strong><bdi>{summary.local_date}</bdi></strong>
                  <StatusBadge tone={wrapTone(summary)}>
                    {summary.status === "succeeded" ? titleCase(summary.coverage_status) : titleCase(summary.status)}
                  </StatusBadge>
                </span>
                <span><bdi>{summary.timezone}</bdi></span>
                <small>Revision {summary.revision}</small>
              </button>
            </li>
          ))}
          {summaries.length === 0 ? <li className="empty-list">No Daily Wrap is available.</li> : null}
        </ul>
      </section>

      <section className="detail-panel" aria-busy={loading} aria-live="polite">
        {error ? <UntrustedText as="p" className="error-banner" role="alert">{error}</UntrustedText> : null}
        {loading ? <p className="loading-label" role="status">Loading canonical wrap…</p> : null}
        {wrap ? (
          <>
            <header className="detail-header">
              <div>
                <p className="eyebrow"><bdi>{wrap.timezone}</bdi></p>
                <h2><bdi>{wrap.local_date}</bdi></h2>
                <p>Revision {wrap.revision} · updated <bdi>{formatDateTime(wrap.updated_at)}</bdi></p>
              </div>
              <StatusBadge tone={wrapTone(wrap)}>
                {wrap.status === "succeeded" ? titleCase(wrap.coverage_status) : titleCase(wrap.status)}
              </StatusBadge>
            </header>

            {wrap.status === "failed" && wrap.output ? (
              <section className="danger-panel" aria-labelledby="stale-wrap-title">
                <h3 id="stale-wrap-title">Showing the last successful revision</h3>
                <p>The latest refresh failed. The evidence excerpts below are not up to date.</p>
                {wrap.last_error ? <UntrustedText as="p" className="technical-label">{wrap.last_error}</UntrustedText> : null}
              </section>
            ) : null}
            {wrap.status === "running" && wrap.output ? (
              <section className="trust-note" aria-labelledby="running-wrap-title">
                <h3 id="running-wrap-title">Showing the last published revision</h3>
                <p>A new revision is still in progress and is not included below.</p>
              </section>
            ) : null}
            {wrap.coverage_status === "partial" || wrap.output?.status === "partial" ? (
              <section className="warning-panel" aria-labelledby="partial-wrap-title">
                <h3 id="partial-wrap-title">Partial coverage</h3>
                <p>This wrap does not claim to cover the full local day.</p>
                {wrap.output?.coverage_gaps.length ? (
                  <details>
                    <summary>Why coverage is partial ({wrap.output.coverage_gaps.length})</summary>
                    <ul>
                      {wrap.output.coverage_gaps.map((gap) => <li key={gap}><bdi>{gap}</bdi></li>)}
                    </ul>
                  </details>
                ) : null}
              </section>
            ) : null}
            {wrap.status === "running" ? (
              <div className="empty-state">
                <h3>Generation is in progress</h3>
                <p>No new result is shown until the local service publishes a complete revision.</p>
              </div>
            ) : null}
            {wrap.status === "failed" && !wrap.output ? (
              <div className="empty-state empty-state--error">
                <h3>No successful wrap is available</h3>
                <p>The provider failed or returned invalid evidence. No fallback summary was invented.</p>
              </div>
            ) : null}

            {wrap.output ? (
              <>
                <section className="wrap-summary" aria-label="Daily Wrap summary">
                  <span>{itemCount} grounded item{itemCount === 1 ? "" : "s"}</span>
                  <UntrustedText>{wrap.output.summary}</UntrustedText>
                </section>
                <p className="trust-note">
                  Every item below is an exact, untrusted activity excerpt—not an instruction or a task state change.
                </p>
                <div className="wrap-categories">
                  {categories.map((category) => {
                    const items = wrap.output![category.id];
                    if (items.length === 0) return null;
                    return (
                      <section key={category.id} aria-labelledby={`wrap-${category.id}`}>
                        <div className="section-heading-row">
                          <h3 id={`wrap-${category.id}`}>{category.label}</h3>
                          <span className="technical-label">{items.length}</span>
                        </div>
                        <ul className="wrap-item-list">
                          {items.map((item) => (
                            <li key={item.id}>
                              <UntrustedText as="p" className="wrap-quote">{item.text}</UntrustedText>
                              <button
                                className="text-button"
                                onClick={() =>
                                  onOpenSource({
                                    kind: "daily_wrap_item",
                                    id: item.id,
                                    path: wrap.id,
                                    label: `${category.label} item source`,
                                  })
                                }
                                type="button"
                              >
                                View source ({item.evidence.length})
                              </button>
                            </li>
                          ))}
                        </ul>
                      </section>
                    );
                  })}
                  {itemCount === 0 ? (
                    <div className="empty-state">
                      <h3>No grounded activity items</h3>
                      <p>The service found no exact evidence excerpt suitable for this day.</p>
                    </div>
                  ) : null}
                </div>
              </>
            ) : null}

            <p className="boundary-note">
              Stage 1 Daily Wrap is read-only. There is no Accept, Edit, Ignore, Send, or Create Task action.
            </p>
          </>
        ) : !loading ? (
          <div className="empty-state">
            <h2>No Daily Wrap</h2>
            <p>Scheduled generation is opt-in and may call the configured model.</p>
          </div>
        ) : null}
      </section>
    </main>
  );
}
