import { useEffect, useState } from "react";

import type { DesktopApi } from "../api";
import type {
  ReplyRescueJob,
  ReplyRescueJobSummary,
  ReplyRescueSnapshot,
} from "../contracts";
import { StatusBadge } from "../components/StatusBadge";
import { UntrustedText } from "../components/UntrustedText";
import { displayError, formatDateTime } from "../format";

interface ReplyRescuePageProps {
  api: DesktopApi;
  daemonRunning: boolean;
  rescue: ReplyRescueSnapshot;
  onChanged: () => Promise<void>;
}

const errorLabels: Record<string, string> = {
  provider_failed: "The configured model provider did not complete this job.",
  invalid_output: "The model returned an unsafe or invalid prepared-reply shape.",
  input_changed: "The reviewed conversation source changed before generation completed.",
  cancelled: "The local worker released this job before completion.",
};

function statusTone(status: ReplyRescueJobSummary["status"]) {
  if (status === "ready") return "positive" as const;
  if (status === "failed") return "danger" as const;
  return "info" as const;
}

function lines(value: string): string[] {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function ReplyRescuePage({
  api,
  daemonRunning,
  rescue,
  onChanged,
}: ReplyRescuePageProps) {
  const [selectedId, setSelectedId] = useState(rescue.jobs[0]?.id ?? "");
  const [detail, setDetail] = useState<ReplyRescueJob | null>(null);
  const [conversationText, setConversationText] = useState("");
  const [participants, setParticipants] = useState("");
  const [recipients, setRecipients] = useState("");
  const [replyMode, setReplyMode] = useState<"reply" | "reply_all" | "unspecified">(
    "unspecified",
  );
  const [goal, setGoal] = useState("");
  const [tone, setTone] = useState("");
  const [styleInstructions, setStyleInstructions] = useState("");
  const [commitments, setCommitments] = useState("");
  const [replyDraft, setReplyDraft] = useState("");
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
      .getReplyRescue(selectedId)
      .then((job) => {
        if (!current) return;
        setDetail(job);
        setReplyDraft(job.output?.reply_body ?? "");
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
      const result = await api.queueReplyRescue({
        conversationText,
        participants: lines(participants),
        intendedRecipients: lines(recipients),
        replyMode,
        goal,
        tone,
        styleInstructions: lines(styleInstructions),
        commitments: lines(commitments),
      });
      setSelectedId(result.job.id);
      setDetail(result.job);
      setReplyDraft("");
      if (result.created) {
        setConversationText("");
        setParticipants("");
        setRecipients("");
        setReplyMode("unspecified");
        setGoal("");
        setTone("");
        setStyleInstructions("");
        setCommitments("");
      }
      setNotice(
        result.created
          ? "Reply queued for preparation. Nothing was sent."
          : "The matching local job already exists.",
      );
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
      const updated = await api.editReplyRescue(detail.id, detail.version, replyDraft);
      setDetail(updated);
      setReplyDraft(updated.output?.reply_body ?? "");
      setNotice("Reviewed reply saved locally. Its generated claim ledger was cleared.");
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
      const updated = await api.retryReplyRescue(detail.id, detail.version);
      setDetail(updated);
      setReplyDraft("");
      setNotice("Reply queued for another preparation attempt.");
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
      await api.deleteReplyRescue(detail.id, detail.version);
      setDetail(null);
      setSelectedId("");
      setReplyDraft("");
      setNotice("Conversation source and prepared reply deleted locally.");
      await onChanged();
    } catch (reason: unknown) {
      setError(displayError(reason));
    } finally {
      setBusy("");
    }
  }

  async function copyReply() {
    if (!replyDraft) return;
    setError("");
    setNotice("");
    try {
      await navigator.clipboard.writeText(replyDraft);
      setNotice("Reviewed reply copied. OpenChronicle did not paste, draft, or send it.");
    } catch {
      setError("The reviewed reply could not be copied to the clipboard.");
    }
  }

  const providerIsLocal = rescue.provider.location === "local";

  return (
    <main className="page prompt-rescue" id="main-content" tabIndex={-1}>
      <header className="page-header">
        <div>
          <p className="eyebrow">Prepared reply, never sent</p>
          <h1>Reply Rescue</h1>
          <p>
            Supply a conversation excerpt you have reviewed. OpenChronicle prepares a reply for
            local review and copy; it has no mailbox, provider-draft, paste, or send capability.
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
            ? "Reviewed source is configured to stay on this Mac, subject to the named local provider."
            : "Reviewed conversation text may leave this Mac when processed. Check configuration before enabling."}
        </p>
      </section>

      <section className="warning-panel">
        <h2>Manual source has no thread identity</h2>
        <p>
          This excerpt does not prove a mailbox account, thread, sender, or recipient. Verify the
          target and every commitment before copying. Reply Rescue cannot send anything.
        </p>
      </section>

      {!rescue.enabled ? (
        <section className="empty-panel">
          <h2>Reply Rescue is off</h2>
          <p>Enable `[reply_rescue]` only after reviewing the provider disclosure.</p>
        </section>
      ) : null}

      {error ? <div className="global-error" role="alert"><UntrustedText>{error}</UntrustedText></div> : null}
      {notice ? <div className="success-panel" role="status">{notice}</div> : null}

      <section className="prompt-rescue__composer" aria-labelledby="reply-rescue-compose">
        <div>
          <p className="eyebrow">Source: manual conversation</p>
          <h2 id="reply-rescue-compose">Prepare a reply</h2>
          <p className="muted">One participant, recipient, style instruction, or commitment per line.</p>
        </div>
        <label className="field field--wide">
          <span>Reviewed conversation excerpt</span>
          <textarea disabled={!rescue.enabled || busy === "queue"} maxLength={50_000} onChange={(event) => setConversationText(event.currentTarget.value)} placeholder="Paste only the conversation context you want to disclose" rows={10} value={conversationText} />
        </label>
        <div className="prompt-rescue__context-grid">
          <label className="field"><span>Participants</span><textarea maxLength={10_000} onChange={(event) => setParticipants(event.currentTarget.value)} rows={3} value={participants} /></label>
          <label className="field"><span>Intended recipients</span><textarea maxLength={10_000} onChange={(event) => setRecipients(event.currentTarget.value)} rows={3} value={recipients} /></label>
          <label className="field"><span>Reply mode</span><select onChange={(event) => setReplyMode(event.currentTarget.value as typeof replyMode)} value={replyMode}><option value="unspecified">Unspecified</option><option value="reply">Reply</option><option value="reply_all">Reply all</option></select></label>
          <label className="field"><span>Goal</span><textarea maxLength={1_000} onChange={(event) => setGoal(event.currentTarget.value)} rows={3} value={goal} /></label>
          <label className="field"><span>Tone</span><textarea maxLength={1_000} onChange={(event) => setTone(event.currentTarget.value)} rows={3} value={tone} /></label>
          <label className="field"><span>Reviewed style instructions</span><textarea maxLength={10_000} onChange={(event) => setStyleInstructions(event.currentTarget.value)} rows={3} value={styleInstructions} /></label>
          <label className="field"><span>Explicit commitments</span><textarea maxLength={10_000} onChange={(event) => setCommitments(event.currentTarget.value)} rows={3} value={commitments} /></label>
        </div>
        <div className="button-row">
          <button className="button button--primary" disabled={!rescue.enabled || !conversationText.trim() || busy === "queue"} onClick={() => void queue()} type="button">
            {busy === "queue" ? "Queueing…" : "Prepare reply"}
          </button>
          {!daemonRunning ? <p className="muted">Start the daemon to process queued jobs.</p> : null}
        </div>
      </section>

      <div className="prompt-rescue__workspace">
        <section aria-label="Reply Rescue history" className="prompt-rescue__history">
          <div className="section-heading-row"><div><p className="eyebrow">Local history</p><h2>Prepared replies</h2></div><button className="button button--ghost" onClick={() => void onChanged()} type="button">Refresh</button></div>
          {rescue.jobs.length === 0 ? <div className="empty-list">No Reply Rescue jobs yet.</div> : rescue.jobs.map((job) => (
            <button aria-pressed={selectedId === job.id} className="prompt-rescue__history-item" key={job.id} onClick={() => setSelectedId(job.id)} type="button">
              <span className="review-list__meta"><StatusBadge tone={statusTone(job.status)}>{job.status}</StatusBadge><span>{formatDateTime(job.updated_at)}</span></span>
              <strong><UntrustedText>{job.conversation_preview}</UntrustedText></strong>
              <small>Manual, unverified identity · attempt {job.attempt_count}</small>
            </button>
          ))}
        </section>

        <section aria-label="Reply Rescue detail" className="prompt-rescue__detail">
          {!selectedId ? <div className="empty-state"><h2>Select a prepared reply</h2><p>Conversation and proposed reply appear side by side.</p></div> : busy === "load" && !detail ? <p role="status">Loading local reply…</p> : detail ? (
            <>
              <div className="detail-header"><div><p className="eyebrow">Manual, unverified identity · {formatDateTime(detail.created_at)}</p><h2>Review prepared reply</h2></div><StatusBadge tone={statusTone(detail.status)}>{detail.status}</StatusBadge></div>
              <section className="info-panel"><h3>Declared target</h3><p>Mode: <strong>{detail.source.reply_mode}</strong> · Recipients: <UntrustedText>{detail.source.intended_recipients.join(", ") || "None declared"}</UntrustedText></p><p>Participants: <UntrustedText>{detail.source.participants.join(", ") || "None declared"}</UntrustedText></p></section>
              <div className="prompt-rescue__comparison">
                <label className="field"><span>Reviewed conversation</span><textarea readOnly rows={18} value={detail.source.conversation_text} /></label>
                <label className="field"><span>Prepared reply</span><textarea disabled={detail.status !== "ready"} maxLength={30_000} onChange={(event) => setReplyDraft(event.currentTarget.value)} placeholder={detail.status === "ready" ? "" : "Waiting for a valid prepared artifact"} rows={18} value={replyDraft} /></label>
              </div>
              {detail.status === "queued" || detail.status === "leased" ? <div className="info-panel" role="status">{detail.status === "queued" ? "Queued locally. The daemon will claim this job." : "The configured model is preparing a bounded reply without tools."}</div> : null}
              {detail.status === "failed" ? <div className="global-error" role="alert">{errorLabels[detail.error_code] ?? "This job failed with a sanitized local status."}</div> : null}
              {detail.output ? <div className="prompt-rescue__notes">
                {(["warnings", "unresolved_questions", "addressed_questions", "assumptions"] as const).map((name) => <section key={name}><h3>{name.replaceAll("_", " ")}</h3>{detail.output?.[name].length ? <ul>{detail.output[name].map((value, index) => <li key={`${name}-${index}`}><UntrustedText>{value}</UntrustedText></li>)}</ul> : <p className="muted">None declared.</p>}</section>)}
                <section><h3>claim ledger</h3>{detail.output.claims.length ? <ul>{detail.output.claims.map((claim, index) => <li key={`claim-${index}`}><UntrustedText>{claim.text}</UntrustedText> <small>({claim.support})</small></li>)}</ul> : <p className="muted">No generated claims retained.</p>}</section>
              </div> : null}
              <div className="button-row">
                <button className="button button--danger-outline" disabled={Boolean(busy)} onClick={() => void deleteJob()} type="button">Delete locally</button>
                {detail.status === "failed" ? <button className="button button--secondary" disabled={Boolean(busy)} onClick={() => void retry()} type="button">Retry preparation</button> : null}
                {detail.status === "ready" ? <><button className="button button--secondary" disabled={Boolean(busy) || !replyDraft.trim() || replyDraft === detail.output?.reply_body} onClick={() => void saveEdit()} type="button">Save reviewed edit</button><button className="button button--primary" disabled={!replyDraft || Boolean(busy)} onClick={() => void copyReply()} type="button">Copy reviewed reply</button></> : null}
              </div>
            </>
          ) : null}
        </section>
      </div>
    </main>
  );
}
