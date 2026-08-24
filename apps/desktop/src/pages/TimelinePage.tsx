import { useEffect, useMemo, useState } from "react";

import type { SourceSubject, TimelineItem } from "../contracts";
import { formatDateTime } from "../format";
import { UntrustedText } from "../components/UntrustedText";

interface TimelinePageProps {
  items: TimelineItem[];
  onOpenSource: (subject: SourceSubject) => void;
}

export function TimelinePage({ items, onOpenSource }: TimelinePageProps) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    if (!normalized) return items;
    return items.filter((item) =>
      [...item.entries, ...item.apps].some((value) =>
        value.toLocaleLowerCase().includes(normalized),
      ),
    );
  }, [items, query]);
  const [selectedId, setSelectedId] = useState<string | null>(filtered[0]?.id ?? null);
  const selected = filtered.find((item) => item.id === selectedId) ?? filtered[0] ?? null;

  useEffect(() => {
    if (selected && selected.id !== selectedId) setSelectedId(selected.id);
  }, [selected, selectedId]);

  return (
    <main className="page page--split" id="main-content" tabIndex={-1}>
      <section className="collection-panel" aria-labelledby="timeline-heading">
        <header className="collection-panel__header">
          <p className="eyebrow">Read-only local activity</p>
          <h1 id="timeline-heading">Timeline</h1>
          <label className="search-field">
            <span>Filter loaded timeline</span>
            <input
              onChange={(event) => setQuery(event.currentTarget.value)}
              placeholder="Application or exact text"
              type="search"
              value={query}
            />
          </label>
        </header>
        <ul className="collection-list">
          {filtered.map((item) => (
            <li key={item.id}>
              <button
                aria-current={selected?.id === item.id ? "true" : undefined}
                className="collection-list__button"
                onClick={() => setSelectedId(item.id)}
                type="button"
              >
                <span className="collection-list__topline">
                  <strong><bdi>{formatDateTime(item.start_time, item.timezone)}</bdi></strong>
                  <small>{item.capture_count} captures</small>
                </span>
                <UntrustedText className="line-clamp">
                  {item.entries[0] ?? "No normalized text"}
                </UntrustedText>
              </button>
            </li>
          ))}
          {filtered.length === 0 ? <li className="empty-list">No loaded block matches this filter.</li> : null}
        </ul>
      </section>

      <section className="detail-panel" aria-live="polite">
        {selected ? (
          <>
            <header className="detail-header">
              <div>
                <p className="eyebrow">Timeline block</p>
                <h2><bdi>{formatDateTime(selected.start_time, selected.timezone)}</bdi></h2>
                <p>
                  <bdi>{formatDateTime(selected.start_time, selected.timezone)}</bdi> –{" "}
                  <bdi>{formatDateTime(selected.end_time, selected.timezone)}</bdi>
                </p>
                <p className="technical-label">Recorded timezone: <bdi>{selected.timezone}</bdi></p>
              </div>
              <button
                className="button button--secondary"
                onClick={() =>
                  onOpenSource({
                    kind: "timeline_block",
                    id: selected.id,
                    label: "Timeline source",
                  })
                }
                type="button"
              >
                View sources
              </button>
            </header>
            <section aria-labelledby="activity-text-heading">
              <h3 id="activity-text-heading">Normalized activity</h3>
              <ol className="activity-list">
                {selected.entries.map((entry, index) => (
                  <li key={`${selected.id}-${index}`}>
                    <UntrustedText>{entry}</UntrustedText>
                  </li>
                ))}
              </ol>
            </section>
            <section aria-labelledby="apps-heading">
              <h3 id="apps-heading">Applications observed</h3>
              <ul className="token-list">
                {selected.apps.map((app) => (
                  <li key={app}><UntrustedText>{app}</UntrustedText></li>
                ))}
              </ul>
            </section>
            <p className="trust-note">
              Timeline text is screen-derived evidence. It cannot authorize a desktop or external action.
            </p>
          </>
        ) : (
          <div className="empty-state">
            <h2>No timeline blocks</h2>
            <p>Nothing in the loaded local snapshot is available to inspect.</p>
          </div>
        )}
      </section>
    </main>
  );
}
