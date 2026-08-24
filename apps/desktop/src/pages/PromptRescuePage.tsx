import { useEffect, useState } from "react";

import type { DesktopApi } from "../api";
import type {
  PromptRescueJob,
  PromptRescueJobSummary,
  PromptRescueSnapshot,
} from "../contracts";
import { StatusBadge } from "../components/StatusBadge";
import { UntrustedText } from "../components/UntrustedText";
import { displayError, formatDateTime } from "../format";

interface PromptRescuePageProps {
  api: DesktopApi;
  daemonRunning: boolean;
  rescue: PromptRescueSnapshot;
  onChanged: () => Promise<void>;
}

const errorLabels: Record<string, string> = {
  provider_failed: "The configured model provider did not complete this job.",
  invalid_output: "The model returned an unsafe or invalid prepared-artifact shape.",
  input_changed: "The reviewed input changed before generation completed.",
  cancelled: "The local worker released this job before completion.",
};

function statusTone(status: PromptRescueJobSummary["status"]) {
  if (status === "ready") return "positive" as const;
  if (status === "failed") return "danger" as const;
  return "info" as const;
}

export function PromptRescuePage({
  api,
  daemonRunning,
  rescue,
  onChanged,
}: PromptRescuePageProps) {
  const [selectedId, setSelectedId] = useState(rescue.jobs[0]?.id ?? "");
  const [detail, setDetail] = useState<PromptRescueJob | null>(null);
  const [roughPrompt, setRoughPrompt] = useState("");
  const [target, setTarget] = useState("");
  const [audience, setAudience] = useState("");
  const [desiredFormat, setDesiredFormat] = useState("");
  const [constraints, setConstraints] = useState("");
  const [improvedDraft, setImprovedDraft] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const selectedVersion = rescue.jobs.find((job) => job.id === selectedId)?.version ?? 0;

  useEffect(() => {
    if (selectedId && !rescue.jobs.some((job) => job.id === selectedId)) {
      setSelectedId(rescue.jobs[0]?.id ?? "");
      setDetail(null);
    } else if (!selectedId && rescue.jobs[0]) {
      setSelectedId(rescue.jobs[0].id);
    }
  }, [rescue.jobs, selectedId]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      return;
    }
    let current = true;
    setBusy("load");
    setError("");
    void api
      .getPromptRescue(selectedId)
      .then((job) => {
        if (!current) return;
        setDetail(job);
        setImprovedDraft(job.output?.improved_prompt ?? "");
      })
      .catch((reason: unknown) => {
        if (current) setError(displayError(reason));
      })
      .finally(() => {
        if (current) setBusy("");
      });
    return () => {
      current = false;
    };
  }, [api, selectedId, selectedVersion]);

  useEffect(() => {
    if (!rescue.jobs.some((job) => job.status === "queued" || job.status === "leased")) {
      return;
    }
    const timer = window.setInterval(() => {
      void onChanged().catch((reason: unknown) => setError(displayError(reason)));
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [onChanged, rescue.jobs]);

  async function queue() {
    setBusy("queue");
    setError("");
    setNotice("");
    try {
      const result = await api.queuePromptRescue({
        roughPrompt,
        target,
        audience,
        constraints: constraints
          .split("\n")
          .map((value) => value.trim())
          .filter(Boolean),
        desiredFormat,
      });
      setSelectedId(result.job.id);
      setDetail(result.job);
      setImprovedDraft("");
      if (result.created) {
        setRoughPrompt("");
        setTarget("");
        setAudience("");
        setDesiredFormat("");
        setConstraints("");
      }
      setNotice(result.created ? "Prompt queued for preparation." : "The matching job already exists.");
      await onChanged();
    } catch (reason: unknown) {
      setError(displayError(reason));
    } finally {
      setBusy("");
    }
  }

  async function saveEdit() {
    if (!detail) return;
    setBusy("edit");
    setError("");
    setNotice("");
    try {
      const updated = await api.editPromptRescue(
        detail.id,
        detail.version,
        improvedDraft,
      );
      setDetail(updated);
      setImprovedDraft(updated.output?.improved_prompt ?? "");
      setNotice("Reviewed prompt saved locally.");
      await onChanged();
    } catch (reason: unknown) {
      setError(displayError(reason));
    } finally {
      setBusy("");
    }
  }

  async function retry() {
    if (!detail) return;
    setBusy("retry");
    setError("");
    setNotice("");
    try {
      const updated = await api.retryPromptRescue(detail.id, detail.version);
      setDetail(updated);
      setImprovedDraft("");
      setNotice("Prompt queued for another preparation attempt.");
      await onChanged();
    } catch (reason: unknown) {
      setError(displayError(reason));
    } finally {
      setBusy("");
    }
  }

  async function deleteJob() {
    if (!detail) return;
    setBusy("delete");
    setError("");
    setNotice("");
    try {
      await api.deletePromptRescue(detail.id, detail.version);
      setDetail(null);
      setSelectedId("");
      setImprovedDraft("");
      setNotice("Prompt Rescue input and artifact deleted locally.");
      await onChanged();
    } catch (reason: unknown) {
      setError(displayError(reason));
    } finally {
      setBusy("");
    }
  }

  async function copyPrompt() {
    if (!improvedDraft) return;
    setError("");
    setNotice("");
    try {
      await navigator.clipboard.writeText(improvedDraft);
      setNotice("Reviewed prompt copied. OpenChronicle did not paste or submit it.");
    } catch {
      setError("The reviewed prompt could not be copied to the clipboard.");
    }
  }

  async function markUsed() {
    if (!detail?.output) return;
    setBusy("adopt");
    setError("");
    setNotice("");
    try {
      const result = await api.recordArtifactAdoption(
        "prompt_rescue",
        detail.id,
        detail.version,
        detail.output_digest,
      );
      setNotice(
        result.created
          ? "Recorded that you used this exact reviewed prompt. No workflow was learned automatically."
          : "This exact reviewed prompt was already marked as used.",
      );
    } catch (reason: unknown) {
      setError(displayError(reason));
    } finally {
      setBusy("");
    }
  }

  const providerIsLocal = rescue.provider.location === "local";

  return (
    <main className="page prompt-rescue" id="main-content" tabIndex={-1}>
      <header className="page-header">
        <div>
          <p className="eyebrow">Prepared prompt, never submitted</p>
          <h1>Prompt Rescue</h1>
          <p>
            Paste a rough prompt you have reviewed. OpenChronicle prepares a stronger draft for
            comparison; it cannot paste into another app, submit, or run tools.
          </p>
        </div>
        <StatusBadge tone={rescue.enabled ? "positive" : "neutral"}>
          {rescue.enabled ? "Opted in" : "Off"}
        </StatusBadge>
      </header>

      <section className={providerIsLocal ? "info-panel" : "warning-panel"}>
        <h2>Model disclosure</h2>
        <p>
          <strong>{providerIsLocal ? "Configured local provider" : "Remote or unknown provider"}</strong>
          {" · "}
          <UntrustedText>{rescue.provider.model}</UntrustedText>
        </p>
        <p>
          {providerIsLocal
            ? "Reviewed input is configured to stay on this Mac, subject to the named local provider."
            : "Reviewed input may leave this Mac when the queued job is processed. Check the model configuration before enabling."}
        </p>
      </section>

      {!rescue.enabled ? (
        <section className="empty-panel">
          <h2>Prompt Rescue is off</h2>
          <p>Enable `[prompt_rescue]` in config only after reviewing the provider disclosure.</p>
        </section>
      ) : null}

      <section className="info-panel">
        <h2>Import an exact macOS selection</h2>
        <p>
          In another app, select one text range and press <kbd>⌘</kbd> <kbd>⇧</kbd>{" "}
          <kbd>Space</kbd>. OpenChronicle reads only that stable AX selection, queues it, and
          opens this review. It never falls back to the clipboard or the whole text field.
        </p>
      </section>

      {error ? <div className="global-error" role="alert"><UntrustedText>{error}</UntrustedText></div> : null}
      {notice ? <div className="success-panel" role="status">{notice}</div> : null}

      <section className="prompt-rescue__composer" aria-labelledby="prompt-rescue-compose">
        <div>
          <p className="eyebrow">Source: manual paste</p>
          <h2 id="prompt-rescue-compose">Prepare a prompt</h2>
          <p className="muted">
            This source is not claimed to match an external macOS selection. Text remains quoted
            input even if it contains role markers or instructions.
          </p>
        </div>
        <label className="field field--wide">
          <span>Rough prompt</span>
          <textarea
            disabled={!rescue.enabled || busy === "queue"}
            maxLength={20_000}
            onChange={(event) => setRoughPrompt(event.currentTarget.value)}
            placeholder="Paste the prompt you want to improve"
            rows={8}
            value={roughPrompt}
          />
        </label>
        <div className="prompt-rescue__context-grid">
          <label className="field">
            <span>Target or goal (optional)</span>
            <input maxLength={500} onChange={(event) => setTarget(event.currentTarget.value)} value={target} />
          </label>
          <label className="field">
            <span>Audience (optional)</span>
            <input maxLength={500} onChange={(event) => setAudience(event.currentTarget.value)} value={audience} />
          </label>
          <label className="field">
            <span>Desired format (optional)</span>
            <input maxLength={500} onChange={(event) => setDesiredFormat(event.currentTarget.value)} value={desiredFormat} />
          </label>
          <label className="field">
            <span>Constraints (one per line)</span>
            <textarea maxLength={10_000} onChange={(event) => setConstraints(event.currentTarget.value)} rows={3} value={constraints} />
          </label>
        </div>
        <div className="button-row">
          <button
            className="button button--primary"
            disabled={!rescue.enabled || !roughPrompt.trim() || busy === "queue"}
            onClick={() => void queue()}
            type="button"
          >
            {busy === "queue" ? "Queueing…" : "Prepare prompt"}
          </button>
          {!daemonRunning ? <p className="muted">Start the daemon to process queued jobs.</p> : null}
        </div>
      </section>

      <div className="prompt-rescue__workspace">
        <section aria-label="Prompt Rescue history" className="prompt-rescue__history">
          <div className="section-heading-row">
            <div>
              <p className="eyebrow">Local history</p>
              <h2>Prepared jobs</h2>
            </div>
            <button className="button button--ghost" onClick={() => void onChanged()} type="button">
              Refresh
            </button>
          </div>
          {rescue.jobs.length === 0 ? (
            <div className="empty-list">No Prompt Rescue jobs yet.</div>
          ) : (
            rescue.jobs.map((job) => (
              <button
                aria-pressed={selectedId === job.id}
                className="prompt-rescue__history-item"
                key={job.id}
                onClick={() => setSelectedId(job.id)}
                type="button"
              >
                <span className="review-list__meta">
                  <StatusBadge tone={statusTone(job.status)}>{job.status}</StatusBadge>
                  <span>{formatDateTime(job.updated_at)}</span>
                </span>
                <strong><UntrustedText>{job.rough_prompt_preview}</UntrustedText></strong>
                <small>
                  {job.source_kind === "macos_selection" ? "Bound macOS selection" : "Manual paste"}
                  {" · "}attempt {job.attempt_count}
                </small>
              </button>
            ))
          )}
        </section>

        <section aria-label="Prompt Rescue detail" className="prompt-rescue__detail">
          {!selectedId ? (
            <div className="empty-state">
              <h2>Select a prepared job</h2>
              <p>Rough input and prepared output appear side by side for review.</p>
            </div>
          ) : busy === "load" && !detail ? (
            <p role="status">Loading local prompt…</p>
          ) : detail ? (
            <>
              <div className="detail-header">
                <div>
                  <p className="eyebrow">
                    {detail.source_kind === "macos_selection" ? "Bound macOS selection" : "Manual paste"}
                    {" · "}{formatDateTime(detail.created_at)}
                  </p>
                  <h2>Review prepared prompt</h2>
                </div>
                <StatusBadge tone={statusTone(detail.status)}>{detail.status}</StatusBadge>
              </div>

              {detail.source_binding ? (
                <section className="info-panel" aria-label="Exact selection source">
                  <h3>Exact selection receipt</h3>
                  <p>
                    <strong><UntrustedText>{detail.source_binding.app_name || "Unnamed app"}</UntrustedText></strong>
                    {" · "}<UntrustedText>{detail.source_binding.bundle_id}</UntrustedText>
                    {" · PID "}{detail.source_binding.pid}
                  </p>
                  <p>
                    Window: <UntrustedText>{detail.source_binding.window_title || "Untitled"}</UntrustedText>
                    {" · "}<UntrustedText>{detail.source_binding.element_role}</UntrustedText>
                    {" · range "}{detail.source_binding.selection_location}
                    {"+"}{detail.source_binding.selection_length}
                    {" · "}{formatDateTime(detail.source_binding.captured_at)}
                  </p>
                </section>
              ) : null}

              <div className="prompt-rescue__comparison">
                <label className="field">
                  <span>Reviewed rough input</span>
                  <textarea readOnly rows={16} value={detail.rough_prompt} />
                </label>
                <label className="field">
                  <span>Prepared prompt</span>
                  <textarea
                    disabled={detail.status !== "ready"}
                    maxLength={30_000}
                    onChange={(event) => setImprovedDraft(event.currentTarget.value)}
                    placeholder={detail.status === "ready" ? "" : "Waiting for a valid prepared artifact"}
                    rows={16}
                    value={improvedDraft}
                  />
                </label>
              </div>

              {detail.status === "queued" || detail.status === "leased" ? (
                <div className="info-panel" role="status">
                  {detail.status === "queued"
                    ? "Queued locally. The daemon will claim this job without blocking the desktop."
                    : "The configured model is preparing a bounded draft. No tools are available to it."}
                </div>
              ) : null}
              {detail.status === "failed" ? (
                <div className="global-error" role="alert">
                  {errorLabels[detail.error_code] ?? "This job failed with a sanitized local status."}
                </div>
              ) : null}

              {detail.output ? (
                <div className="prompt-rescue__notes">
                  {(["changes", "assumptions", "missing_context"] as const).map((name) => (
                    <section key={name}>
                      <h3>{name.replace("_", " ")}</h3>
                      {detail.output?.[name].length ? (
                        <ul>
                          {detail.output[name].map((value, index) => (
                            <li key={`${name}-${index}`}><UntrustedText>{value}</UntrustedText></li>
                          ))}
                        </ul>
                      ) : <p className="muted">None declared.</p>}
                    </section>
                  ))}
                </div>
              ) : null}

              <div className="button-row">
                <button
                  className="button button--danger-outline"
                  disabled={Boolean(busy)}
                  onClick={() => void deleteJob()}
                  type="button"
                >
                  Delete locally
                </button>
                {detail.status === "failed" ? (
                  <button className="button button--secondary" disabled={Boolean(busy)} onClick={() => void retry()} type="button">
                    Retry preparation
                  </button>
                ) : null}
                {detail.status === "ready" ? (
                  <>
                    <button
                      className="button button--secondary"
                      disabled={Boolean(busy) || !improvedDraft.trim() || improvedDraft === detail.output?.improved_prompt}
                      onClick={() => void saveEdit()}
                      type="button"
                    >
                      Save reviewed edit
                    </button>
                    <button className="button button--primary" disabled={!improvedDraft || Boolean(busy)} onClick={() => void copyPrompt()} type="button">
                      Copy reviewed prompt
                    </button>
                    <button className="button button--secondary" disabled={Boolean(busy) || improvedDraft !== detail.output?.improved_prompt} onClick={() => void markUsed()} type="button">
                      I used this
                    </button>
                  </>
                ) : null}
              </div>
            </>
          ) : null}
        </section>
      </div>
    </main>
  );
}
