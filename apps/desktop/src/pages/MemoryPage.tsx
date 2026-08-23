import { useEffect, useMemo, useState } from "react";

import type { MemorySummary, SourceSubject } from "../contracts";
import { formatDateTime, titleCase } from "../format";
import { StatusBadge } from "../components/StatusBadge";
import { UntrustedText } from "../components/UntrustedText";

interface MemoryPageProps {
  memories: MemorySummary[];
  onOpenSource: (subject: SourceSubject) => void;
}

type MemoryScope = "all" | "about-me" | "projects" | "other";

function scopeMatches(memory: MemorySummary, scope: MemoryScope) {
  if (scope === "about-me") return memory.path.startsWith("user-");
  if (scope === "projects") return memory.path.startsWith("project-");
  if (scope === "other") {
    return !memory.path.startsWith("user-") && !memory.path.startsWith("project-");
  }
  return true;
}

function memoryKind(path: string) {
  return titleCase(path.split("-", 1)[0] || "memory");
}

function memoryKey(memory: MemorySummary) {
  return `${memory.path}\u0000${memory.id}`;
}

export function MemoryPage({ memories, onOpenSource }: MemoryPageProps) {
  const [scope, setScope] = useState<MemoryScope>("all");
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visible = useMemo(
    () =>
      memories.filter((memory) => {
        if (!scopeMatches(memory, scope)) return false;
        if (!normalizedQuery) return true;
        return [memory.content, memory.path, memory.tags.join(" ")]
          .join(" ")
          .toLocaleLowerCase()
          .includes(normalizedQuery);
      }),
    [memories, normalizedQuery, scope],
  );
  const [selectedKey, setSelectedKey] = useState<string | null>(
    visible[0] ? memoryKey(visible[0]) : null,
  );

  useEffect(() => {
    if (!visible.some((memory) => memoryKey(memory) === selectedKey)) {
      setSelectedKey(visible[0] ? memoryKey(visible[0]) : null);
    }
  }, [selectedKey, visible]);

  const selected = visible.find((memory) => memoryKey(memory) === selectedKey) ?? null;

  return (
    <main className="page page--split" id="main-content" tabIndex={-1}>
      <section className="collection-panel" aria-labelledby="memory-heading">
        <header className="collection-panel__header">
          <p className="eyebrow">Current reviewed context</p>
          <h1 id="memory-heading">Memory / About Me</h1>
          <label className="search-field">
            <span>Search remembered facts</span>
            <input
              onChange={(event) => setQuery(event.currentTarget.value)}
              placeholder="Preference, project, person…"
              type="search"
              value={query}
            />
          </label>
          <div className="segmented-control" aria-label="Filter current memories">
            {(["all", "about-me", "projects", "other"] as const).map((value) => (
              <button
                aria-pressed={scope === value}
                key={value}
                onClick={() => setScope(value)}
                type="button"
              >
                {value === "about-me" ? "About me" : titleCase(value)}
              </button>
            ))}
          </div>
        </header>
        <ul className="collection-list">
          {visible.map((memory) => (
            <li key={`${memory.path}:${memory.id}`}>
              <button
                aria-current={selectedKey === memoryKey(memory) ? "true" : undefined}
                className="collection-list__button"
                onClick={() => setSelectedKey(memoryKey(memory))}
                type="button"
              >
                <span className="collection-list__topline">
                  <StatusBadge tone="positive">Current</StatusBadge>
                  <small>{memoryKind(memory.path)}</small>
                </span>
                <UntrustedText className="line-clamp">{memory.content}</UntrustedText>
                <small><UntrustedText>{memory.path}</UntrustedText></small>
              </button>
            </li>
          ))}
          {visible.length === 0 ? (
            <li className="empty-list">
              {memories.length === 0
                ? "No current reviewed memories yet. Approved proposals will appear here."
                : "No current memories match this filter."}
            </li>
          ) : null}
        </ul>
      </section>

      <section className="detail-panel" aria-live="polite">
        {selected ? (
          <>
            <header className="detail-header">
              <div>
                <p className="eyebrow">Published local memory</p>
                <h2>{memoryKind(selected.path)} context</h2>
                <p>Remembered <bdi>{formatDateTime(selected.timestamp)}</bdi></p>
              </div>
              <StatusBadge tone="positive">Current</StatusBadge>
            </header>

            <p className="trust-note">
              This is current, locally stored context available to text-generation workflows.
              Superseded versions are kept in history but are not shown in this view.
            </p>

            <section aria-labelledby="remembered-content-heading">
              <h3 id="remembered-content-heading">Remembered fact</h3>
              <UntrustedText as="pre" className="proposal-text">
                {selected.content}
              </UntrustedText>
            </section>

            <dl className="definition-grid">
              <dt>File</dt>
              <dd><UntrustedText>{selected.path}</UntrustedText></dd>
              <dt>Entry ID</dt>
              <dd><bdi>{selected.id}</bdi></dd>
              <dt>Tags</dt>
              <dd>
                {selected.tags.length > 0
                  ? selected.tags.map((tag, index) => (
                      <span key={`${tag}-${index}`}>
                        <bdi>{tag}</bdi>{index < selected.tags.length - 1 ? ", " : ""}
                      </span>
                    ))
                  : "None"}
              </dd>
              <dt>Origin</dt>
              <dd><bdi>{selected.origin}</bdi></dd>
              <dt>Direct sources</dt>
              <dd>{selected.source_count}</dd>
            </dl>

            <div className="button-row">
              <button
                className="button button--secondary"
                onClick={() =>
                  onOpenSource({
                    kind: "memory_entry",
                    id: selected.id,
                    path: selected.path,
                    label: "Memory sources",
                  })
                }
                type="button"
              >
                View sources
              </button>
            </div>
            <p className="boundary-note">
              This page is inspect-only. New facts and corrections still go through the Review
              Inbox; no external application is changed.
            </p>
          </>
        ) : (
          <section className="empty-callout">
            <h2>No current memory selected</h2>
            <p>Choose a remembered fact from the list, or approve one in the Review Inbox.</p>
          </section>
        )}
      </section>
    </main>
  );
}
