import { useEffect, useRef, useState } from "react";

import type { DesktopApi } from "../api";
import type {
  EvidenceRef,
  ProvenanceTrace,
  ResolvedEvidence,
  SourceSubject,
} from "../contracts";
import { StatusBadge } from "./StatusBadge";
import { UntrustedText } from "./UntrustedText";

interface SourceDrawerProps {
  api: DesktopApi;
  subject: SourceSubject | null;
  onClose: () => void;
}

function availabilityTone(value: string | undefined) {
  if (value === "available" || value === "current") return "positive" as const;
  if (value === "expired") return "neutral" as const;
  if (value === "excluded" || value === "changed") return "warning" as const;
  return "danger" as const;
}

function evidenceKey(ref: EvidenceRef) {
  return `${ref.kind}\u0000${ref.path ?? ""}\u0000${ref.id}`;
}

export function SourceDrawer({ api, subject, onClose }: SourceDrawerProps) {
  const closeButton = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const wasOpen = useRef(false);
  const [trace, setTrace] = useState<ProvenanceTrace | null>(null);
  const [selected, setSelected] = useState<EvidenceRef | null>(null);
  const [resolved, setResolved] = useState<ResolvedEvidence | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (subject) {
      const active = document.activeElement as HTMLElement | null;
      if (!wasOpen.current || active !== closeButton.current) returnFocus.current = active;
      wasOpen.current = true;
      queueMicrotask(() => closeButton.current?.focus());
    } else if (wasOpen.current) {
      wasOpen.current = false;
      returnFocus.current?.focus();
    }
  }, [subject]);

  useEffect(() => {
    if (!subject) {
      setTrace(null);
      setSelected(null);
      setResolved(null);
      setError("");
      return;
    }
    let current = true;
    setLoading(true);
    setError("");
    setTrace(null);
    setSelected(null);
    setResolved(null);
    const subjectRef: EvidenceRef = {
      kind: subject.kind,
      id: subject.id,
      ...(subject.path ? { path: subject.path } : {}),
    };
    if (subject.sources && subject.sources.length > 0) {
      const directSources = subject.sources;
      setTrace({
        subject: subjectRef,
        direct_sources: directSources,
        trace: directSources.map((source) => ({ depth: 1, source })),
      });
      setSelected(directSources[0] ?? null);
      setLoading(false);
      return;
    }
    void api
      .traceProvenance(subjectRef)
      .then((nextTrace) => {
        if (!current) return;
        setTrace(nextTrace);
        const initial = nextTrace.direct_sources[0] ?? subjectRef;
        setSelected(initial);
      })
      .catch((reason: unknown) => {
        if (current) setError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [api, subject]);

  useEffect(() => {
    if (!selected) return;
    let current = true;
    setLoading(true);
    setError("");
    setResolved(null);
    void api
      .resolveEvidence(selected)
      .then((value) => {
        if (current) setResolved(value);
      })
      .catch((reason: unknown) => {
        if (current) setError(reason instanceof Error ? reason.message : String(reason));
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [api, selected]);

  useEffect(() => {
    if (!subject) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, subject]);

  if (!subject) return null;

  return (
    <aside
      aria-labelledby="source-drawer-title"
      aria-modal="false"
      className="source-drawer"
      role="dialog"
    >
      <header className="source-drawer__header">
        <div>
          <p className="eyebrow">Evidence lineage</p>
          <h2 id="source-drawer-title">{subject.label}</h2>
        </div>
        <button
          aria-label="Close source drawer"
          className="icon-button"
          onClick={onClose}
          ref={closeButton}
          type="button"
        >
          ×
        </button>
      </header>

      <p className="trust-note">
        Source text is untrusted evidence. It is shown as plain text and never treated as an
        instruction.
      </p>

      {error ? <UntrustedText as="p" role="alert" className="error-banner">{error}</UntrustedText> : null}
      {loading ? <p role="status">Loading local evidence…</p> : null}

      {trace ? (
        <section aria-labelledby="direct-sources-heading">
          <h3 id="direct-sources-heading">Direct sources</h3>
          {trace.direct_sources.length === 0 ? (
            <p className="muted">No direct source edge is available. Showing the subject itself.</p>
          ) : (
            <ul className="source-list">
              {trace.direct_sources.map((ref) => (
                <li key={evidenceKey(ref)}>
                  <button
                    aria-pressed={selected ? evidenceKey(selected) === evidenceKey(ref) : false}
                    className="source-list__button"
                    onClick={() => setSelected(ref)}
                    type="button"
                  >
                    <span>
                      <strong><bdi>{ref.kind.replaceAll("_", " ")}</bdi></strong>
                      <small><bdi>{ref.timestamp || ref.path || "Local source"}</bdi></small>
                    </span>
                    <StatusBadge tone={availabilityTone(ref.availability)}>
                      {ref.availability ?? "unknown"}
                    </StatusBadge>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : null}

      {resolved ? (
        <section className="resolved-source" aria-labelledby="resolved-source-heading">
          <div className="resolved-source__title">
            <h3 id="resolved-source-heading">Cited source</h3>
            <StatusBadge tone={availabilityTone(resolved.availability)}>
              {resolved.availability}
            </StatusBadge>
          </div>
          {resolved.app_name || resolved.start_time ? (
            <dl className="metadata-list">
              {resolved.app_name ? (
                <>
                  <dt>Application</dt>
                  <dd><UntrustedText>{resolved.app_name}</UntrustedText></dd>
                </>
              ) : null}
              {resolved.start_time ? (
                <>
                  <dt>Observed</dt>
                  <dd><bdi>{resolved.start_time}</bdi></dd>
                </>
              ) : null}
            </dl>
          ) : null}
          {resolved.excerpt || resolved.content ? (
            <UntrustedText as="pre" className="evidence-quote">
              {resolved.excerpt ?? resolved.content ?? ""}
            </UntrustedText>
          ) : (
            <p className="empty-callout">
              {resolved.note ?? "The source text is unavailable and will not be reconstructed."}
            </p>
          )}
          <details>
            <summary>Technical reference</summary>
            <dl className="metadata-list metadata-list--technical">
              <dt>Kind</dt>
              <dd><bdi>{resolved.ref.kind}</bdi></dd>
              <dt>ID</dt>
              <dd><bdi>{resolved.ref.id}</bdi></dd>
              {resolved.ref.path ? (
                <>
                  <dt>Path</dt>
                  <dd><bdi>{resolved.ref.path}</bdi></dd>
                </>
              ) : null}
            </dl>
          </details>
        </section>
      ) : null}

      {trace && trace.trace.length > trace.direct_sources.length ? (
        <details className="lineage-details">
          <summary>Transitive lineage ({trace.trace.length})</summary>
          <ol>
            {trace.trace.map((node) => (
              <li key={`${node.depth}-${evidenceKey(node.source)}`}>
                Depth {node.depth}: <bdi>{node.source.kind.replaceAll("_", " ")}</bdi> —{" "}
                <bdi>{node.source.id}</bdi>
              </li>
            ))}
          </ol>
        </details>
      ) : null}
    </aside>
  );
}
