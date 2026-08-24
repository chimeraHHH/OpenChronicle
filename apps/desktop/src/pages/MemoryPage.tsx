import { useEffect, useMemo, useState } from "react";

import { DesktopApiError, type DesktopApi } from "../api";
import type {
  MemoryForgetPreview,
  MemoryHistory,
  MemorySummary,
  SourceSubject,
} from "../contracts";
import { displayError, formatDateTime, titleCase } from "../format";
import { StatusBadge } from "../components/StatusBadge";
import { UntrustedText } from "../components/UntrustedText";

interface MemoryPageProps {
  api: DesktopApi;
  memories: MemorySummary[];
  onChanged: () => Promise<void>;
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

export function MemoryPage({ api, memories, onChanged, onOpenSource }: MemoryPageProps) {
  const [scope, setScope] = useState<MemoryScope>("all");
  const [query, setQuery] = useState("");
  const [exporting, setExporting] = useState(false);
  const [exportNotice, setExportNotice] = useState("");
  const [exportError, setExportError] = useState("");
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState("");
  const [editTags, setEditTags] = useState("");
  const [saving, setSaving] = useState(false);
  const [editNotice, setEditNotice] = useState("");
  const [editError, setEditError] = useState("");
  const [forgetPreview, setForgetPreview] = useState<MemoryForgetPreview | null>(null);
  const [forgetBusy, setForgetBusy] = useState(false);
  const [forgetNotice, setForgetNotice] = useState("");
  const [forgetError, setForgetError] = useState("");
  const [history, setHistory] = useState<MemoryHistory | null>(null);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visible = useMemo(
    () =>
      memories.filter((memory) => {
        if (!scopeMatches(memory, scope)) return false;
        if (!normalizedQuery) return true;
        return [memory.content, memory.path, memory.subject_key ?? "", memory.tags.join(" ")]
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

  useEffect(() => {
    setEditing(false);
    setEditContent(selected?.content ?? "");
    setEditTags(selected?.tags.join(", ") ?? "");
    setEditError("");
    setForgetPreview(null);
    setForgetError("");
    setHistory(null);
    setHistoryError("");
  }, [selected?.id, selected?.path, selected?.revision]);

  async function exportMemory(format: "json" | "markdown") {
    setExporting(true);
    setExportNotice("");
    setExportError("");
    try {
      const result = await api.exportPublishedMemory(format);
      setExportNotice(`Saved ${result.fact_count} current fact(s) to ${result.file_name}.`);
    } catch (reason) {
      setExportError(displayError(reason));
    } finally {
      setExporting(false);
    }
  }

  async function saveCorrection() {
    if (!selected) return;
    setSaving(true);
    setEditNotice("");
    setEditError("");
    try {
      await api.correctPublishedMemory({
        path: selected.path,
        entryId: selected.id,
        expectedRevision: selected.revision,
        content: editContent,
        tags: editTags
          .split(",")
          .map((tag) => tag.trim())
          .filter(Boolean),
      });
      setEditing(false);
      try {
        await onChanged();
        setEditNotice("Correction published locally. The prior value remains in history.");
      } catch {
        setEditNotice(
          "Correction published locally, but the current-memory view could not be refreshed.",
        );
      }
    } catch (reason) {
      setEditError(displayError(reason));
    } finally {
      setSaving(false);
    }
  }

  async function previewForget() {
    if (!selected) return;
    setForgetBusy(true);
    setForgetNotice("");
    setForgetError("");
    try {
      setForgetPreview(await api.previewForgetPublishedMemory(selected));
    } catch (reason) {
      setForgetError(displayError(reason));
    } finally {
      setForgetBusy(false);
    }
  }

  async function loadHistory() {
    if (!selected) return;
    setHistoryBusy(true);
    setHistoryError("");
    try {
      setHistory(await api.getPublishedMemoryHistory(selected));
    } catch (reason) {
      setHistory(null);
      setHistoryError(displayError(reason));
    } finally {
      setHistoryBusy(false);
    }
  }

  async function commitForget() {
    if (!forgetPreview || !selected) return;
    if (
      forgetPreview.path !== selected.path ||
      forgetPreview.entry_id !== selected.id ||
      forgetPreview.expected_revision !== selected.revision
    ) {
      setForgetPreview(null);
      setForgetError("The deletion preview is stale. Review the current memory again.");
      return;
    }
    setForgetBusy(true);
    setForgetNotice("");
    setForgetError("");
    try {
      await api.forgetPublishedMemory(forgetPreview);
      setForgetPreview(null);
      try {
        await onChanged();
        setForgetNotice("Published memory and its complete local revision history were deleted.");
      } catch {
        setForgetNotice(
          "Published memory was deleted, but the current-memory view could not be refreshed.",
        );
      }
    } catch (reason) {
      if (reason instanceof DesktopApiError && reason.code === "USER_CANCELLED") {
        setForgetNotice("System confirmation was cancelled. Nothing was deleted.");
      } else if (
        reason instanceof DesktopApiError &&
        (reason.code === "STALE_PURGE_PLAN" || reason.code === "VERSION_CONFLICT")
      ) {
        setForgetPreview(null);
        setForgetError("The deletion impact changed. Nothing was deleted; review a new preview.");
      } else {
        setForgetError(displayError(reason));
      }
    } finally {
      setForgetBusy(false);
    }
  }

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
          <div className="button-row">
            <button
              className="button button--secondary"
              disabled={exporting}
              onClick={() => void exportMemory("json")}
              type="button"
            >
              Export JSON…
            </button>
            <button
              className="button button--secondary"
              disabled={exporting}
              onClick={() => void exportMemory("markdown")}
              type="button"
            >
              Export Markdown…
            </button>
          </div>
          {exportNotice ? <p className="success-banner" role="status">{exportNotice}</p> : null}
          {exportError ? <p className="error-banner" role="alert">{exportError}</p> : null}
        </header>
        <ul className="collection-list">
          {visible.map((memory) => (
            <li key={`${memory.path}:${memory.id}`}>
              <button
                aria-current={selectedKey === memoryKey(memory) ? "true" : undefined}
                className="collection-list__button"
                onClick={() => {
                  setEditNotice("");
                  setForgetNotice("");
                  setSelectedKey(memoryKey(memory));
                }}
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
              Superseded versions are kept locally and loaded only when you request the revision
              history below.
            </p>

            <section aria-labelledby="remembered-content-heading">
              <h3 id="remembered-content-heading">Remembered fact</h3>
              {editing ? (
                <div className="edit-form">
                  <label>
                    Corrected fact
                    <textarea
                      maxLength={20_000}
                      onChange={(event) => setEditContent(event.currentTarget.value)}
                      rows={7}
                      value={editContent}
                    />
                  </label>
                  <label>
                    Tags (comma separated)
                    <input
                      onChange={(event) => setEditTags(event.currentTarget.value)}
                      value={editTags}
                    />
                  </label>
                  <p className="trust-note">
                    Saving creates a new current version and keeps this version in local history.
                    No model or network is used.
                  </p>
                  <div className="button-row">
                    <button
                      className="button button--primary"
                      disabled={saving || editContent.trim().length === 0}
                      onClick={() => void saveCorrection()}
                      type="button"
                    >
                      {saving ? "Saving…" : "Save correction"}
                    </button>
                    <button
                      className="button button--ghost"
                      disabled={saving}
                      onClick={() => setEditing(false)}
                      type="button"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              ) : (
                <UntrustedText as="pre" className="proposal-text">
                  {selected.content}
                </UntrustedText>
              )}
            </section>

            {editNotice ? <p className="success-banner" role="status">{editNotice}</p> : null}
            {editError ? <p className="error-banner" role="alert">{editError}</p> : null}

            <dl className="definition-grid">
              <dt>File</dt>
              <dd><UntrustedText>{selected.path}</UntrustedText></dd>
              <dt>Entry ID</dt>
              <dd><bdi>{selected.id}</bdi></dd>
              <dt>Fact slot</dt>
              <dd>
                {selected.subject_key
                  ? <UntrustedText>{selected.subject_key}</UntrustedText>
                  : "Legacy / unspecified"}
              </dd>
              <dt>Assertion basis</dt>
              <dd>{selected.assertion_kind ? titleCase(selected.assertion_kind) : "Unspecified"}</dd>
              <dt>Valid time</dt>
              <dd>
                {selected.valid_from || selected.valid_to
                  ? `${selected.valid_from || "Open start"} → ${selected.valid_to || "Open end"}`
                  : "Open-ended"}
              </dd>
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
                className="button button--primary"
                disabled={editing}
                onClick={() => {
                  setEditNotice("");
                  setEditError("");
                  setForgetPreview(null);
                  setForgetNotice("");
                  setEditContent(selected.content);
                  setEditTags(selected.tags.join(", "));
                  setEditing(true);
                }}
                type="button"
              >
                Correct memory
              </button>
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
              <button
                className="button button--secondary"
                disabled={historyBusy}
                onClick={() => void loadHistory()}
                type="button"
              >
                {historyBusy
                  ? "Loading history…"
                  : history
                    ? "Refresh revision history"
                    : "View revision history"}
              </button>
            </div>
            {history ? (
              <section aria-labelledby="memory-history-heading" className="memory-history">
                <div className="section-heading-row">
                  <div>
                    <p className="eyebrow">Immutable local lineage</p>
                    <h3 id="memory-history-heading">Revision history ({history.versions.length})</h3>
                  </div>
                </div>
                <ol>
                  {history.versions.map((version) => (
                    <li key={version.id}>
                      <div className="memory-history__heading">
                        <StatusBadge tone={version.state === "current" ? "positive" : "neutral"}>
                          {version.state === "current" ? "Current" : "Superseded"}
                        </StatusBadge>
                        <small>
                          Recorded <bdi>{formatDateTime(version.recorded_at)}</bdi>
                          {version.superseded_at
                            ? <> · Superseded <bdi>{formatDateTime(version.superseded_at)}</bdi></>
                            : null}
                        </small>
                      </div>
                      <UntrustedText as="pre" className="proposal-text">
                        {version.content}
                      </UntrustedText>
                      <dl className="definition-grid">
                        <dt>Valid time</dt>
                        <dd>
                          {version.valid_from || version.valid_to
                            ? `${version.valid_from || "Open start"} → ${version.valid_to || "Open end"}`
                            : "Open-ended"}
                        </dd>
                        <dt>Direct sources</dt>
                        <dd>{version.source_count}</dd>
                      </dl>
                      <button
                        className="button button--ghost"
                        onClick={() =>
                          onOpenSource({
                            kind: "memory_entry",
                            id: version.id,
                            path: version.path,
                            label: "Memory version sources",
                          })
                        }
                        type="button"
                      >
                        View version sources
                      </button>
                    </li>
                  ))}
                </ol>
              </section>
            ) : null}
            {historyError ? <p className="error-banner" role="alert">{historyError}</p> : null}
            <details className="danger-zone" open={forgetPreview !== null}>
              <summary>Permanent local deletion</summary>
              <p>
                Forget removes this fact's complete local revision chain, related proposal records,
                dependent Daily Wraps, and an empty candidate-created container when applicable.
                Upstream captures and external backups are outside this purge.
              </p>
              {forgetPreview ? (
                <div className="purge-preview" aria-live="polite">
                  <h3>Deletion impact</h3>
                  <ul>
                    <li>{forgetPreview.counts.memory_entries} memory version(s)</li>
                    <li>{forgetPreview.counts.candidates} related proposal record(s)</li>
                    <li>{forgetPreview.counts.memory_files} generated memory file(s) cleaned or removed</li>
                    <li>{forgetPreview.counts.daily_wraps} dependent Daily Wrap(s)</li>
                  </ul>
                  <div className="purge-targets">
                    <h4>Memory entries</h4>
                    <ul>
                      {forgetPreview.entries.map((entry) => (
                        <li key={`${entry.path}-${entry.id}`}>
                          <UntrustedText>{entry.path}</UntrustedText> · <bdi>{entry.id}</bdi>
                        </li>
                      ))}
                    </ul>
                    {forgetPreview.candidate_ids.length > 0 ? (
                      <>
                        <h4>Proposal IDs</h4>
                        <ul>
                          {forgetPreview.candidate_ids.map((id) => <li key={id}><bdi>{id}</bdi></li>)}
                        </ul>
                      </>
                    ) : null}
                    {forgetPreview.wrap_ids.length > 0 ? (
                      <>
                        <h4>Daily Wrap IDs</h4>
                        <ul>
                          {forgetPreview.wrap_ids.map((id) => <li key={id}><bdi>{id}</bdi></li>)}
                        </ul>
                      </>
                    ) : null}
                  </div>
                  <p>No undo is available after system confirmation.</p>
                  <div className="button-row">
                    <button
                      className="button button--danger"
                      disabled={forgetBusy}
                      onClick={() => void commitForget()}
                      type="button"
                    >
                      Continue to system confirmation
                    </button>
                    <button
                      className="button button--ghost"
                      disabled={forgetBusy}
                      onClick={() => setForgetPreview(null)}
                      type="button"
                    >
                      Cancel deletion
                    </button>
                  </div>
                </div>
              ) : (
                <button
                  className="button button--danger-outline"
                  disabled={forgetBusy || editing}
                  onClick={() => void previewForget()}
                  type="button"
                >
                  Review permanent forget…
                </button>
              )}
            </details>
            {forgetNotice ? <p className="success-banner" role="status">{forgetNotice}</p> : null}
            {forgetError ? <p className="error-banner" role="alert">{forgetError}</p> : null}
            <p className="boundary-note">
              Corrections are direct user-authored local revisions. Model-generated new facts still
              go through the Review Inbox; no external application is changed.
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
