import type { DesktopSnapshot } from "../contracts";
import { formatDateTime, titleCase } from "../format";
import { StatusBadge } from "../components/StatusBadge";
import { UntrustedText } from "../components/UntrustedText";

interface OverviewPageProps {
  snapshot: DesktopSnapshot;
  busy: boolean;
  onSetPaused: (paused: boolean) => void;
  onOpenReview: () => void;
  onOpenWrap: () => void;
}

function captureTone(state: DesktopSnapshot["capture"]["state"]) {
  if (state === "active") return "positive" as const;
  if (state === "paused") return "warning" as const;
  if (state === "permission_required") return "danger" as const;
  return "neutral" as const;
}

export function OverviewPage({
  snapshot,
  busy,
  onSetPaused,
  onOpenReview,
  onOpenWrap,
}: OverviewPageProps) {
  const needsReview =
    snapshot.review_counts.pending +
    snapshot.review_counts.conflict +
    snapshot.review_counts.applying;
  const latestWrap = snapshot.daily_wraps[0] ?? null;
  const pauseDisabled =
    busy || snapshot.daemon.state === "stopped" || snapshot.daemon.state === "unknown";

  return (
    <main className="page" id="main-content" tabIndex={-1}>
      <header className="page-header">
        <div>
          <p className="eyebrow">Local memory plane</p>
          <h1>Overview</h1>
          <p>Review what OpenChronicle observed and decide what becomes durable memory.</p>
        </div>
        <StatusBadge tone={captureTone(snapshot.capture.state)}>
          {snapshot.capture.state === "active"
            ? "New capture active"
            : snapshot.capture.state === "paused"
              ? "New capture paused"
              : titleCase(snapshot.capture.state)}
        </StatusBadge>
      </header>

      <section className="hero-card" aria-labelledby="capture-control-title">
        <div>
          <p className="eyebrow">Capture control</p>
          <h2 id="capture-control-title">
            {snapshot.capture.paused ? "New desktop capture is paused" : "New desktop capture is active"}
          </h2>
          <p>
            Pausing stops only new desktop captures. Work already queued locally—and any configured
            model call already in progress—may finish.
          </p>
          <p className="muted">
            Last capture: <bdi>{formatDateTime(snapshot.capture.last_capture_at)}</bdi>
            {snapshot.capture.last_app ? <> · <UntrustedText>{snapshot.capture.last_app}</UntrustedText></> : ""}
          </p>
        </div>
        <button
          className={snapshot.capture.paused ? "button button--primary" : "button button--secondary"}
          disabled={pauseDisabled}
          onClick={() => onSetPaused(!snapshot.capture.paused)}
          type="button"
        >
          {busy
            ? "Updating capture state…"
            : snapshot.capture.paused
              ? "Resume new capture"
              : "Pause new capture"}
        </button>
      </section>

      <div className="card-grid">
        <section className="summary-card" aria-labelledby="review-summary-title">
          <div className="summary-card__metric" aria-hidden="true">{needsReview}</div>
          <h2 id="review-summary-title">Need review</h2>
          <p>
            {snapshot.review_counts.conflict} conflict
            {snapshot.review_counts.conflict === 1 ? "" : "s"} · {snapshot.review_counts.applying}{" "}
            interrupted save{snapshot.review_counts.applying === 1 ? "" : "s"}; no proposal is saved automatically.
          </p>
          <button className="text-button" onClick={onOpenReview} type="button">
            Open Review Inbox
          </button>
        </section>

        <section className="summary-card" aria-labelledby="wrap-summary-title">
          <div className="summary-card__metric summary-card__metric--small" aria-hidden="true">
            {latestWrap ? `r${latestWrap.revision}` : "—"}
          </div>
          <h2 id="wrap-summary-title">Latest Daily Wrap</h2>
          {latestWrap ? (
            <p>
              {latestWrap.local_date} · {titleCase(latestWrap.status)} /{" "}
              {titleCase(latestWrap.coverage_status)}
            </p>
          ) : (
            <p>No evidence-backed wrap is available yet.</p>
          )}
          <button className="text-button" onClick={onOpenWrap} type="button">
            Open Daily Wrap
          </button>
        </section>

        <section className="summary-card" aria-labelledby="service-summary-title">
          <div className="summary-card__metric summary-card__metric--small" aria-hidden="true">
            {snapshot.daemon.state === "stopped"
              ? "Off"
              : snapshot.daemon.state === "unknown"
                ? "—"
                : "On"}
          </div>
          <h2 id="service-summary-title">Background service</h2>
          <p>
            {titleCase(snapshot.daemon.state)}
            {snapshot.daemon.health ? ` · ${snapshot.daemon.health}` : ""}
          </p>
          <p className="muted">Opening this console never pings a model.</p>
        </section>
      </div>

      {snapshot.purge_pending_count > 0 ? (
        <section className="warning-panel" aria-labelledby="purge-pending-title">
          <h2 id="purge-pending-title">Permanent deletion is still finishing</h2>
          <p>
            {snapshot.purge_pending_count} authorized purge operation(s) remain. Affected content is
            hidden while crash-resumable cleanup completes.
          </p>
        </section>
      ) : null}
    </main>
  );
}
