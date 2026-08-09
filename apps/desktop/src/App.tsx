import { listen } from "@tauri-apps/api/event";
import { useCallback, useEffect, useRef, useState } from "react";

import { desktopApi, type DesktopApi } from "./api";
import { Sidebar } from "./components/Sidebar";
import { SourceDrawer } from "./components/SourceDrawer";
import { UntrustedText } from "./components/UntrustedText";
import type { DesktopSnapshot, PageId, SourceSubject } from "./contracts";
import { displayError } from "./format";
import { DailyWrapPage } from "./pages/DailyWrapPage";
import { OverviewPage } from "./pages/OverviewPage";
import { PrivacyPage } from "./pages/PrivacyPage";
import { PromptRescuePage } from "./pages/PromptRescuePage";
import { ReviewPage } from "./pages/ReviewPage";
import { ReplyRescuePage } from "./pages/ReplyRescuePage";
import { SuggestionsPage } from "./pages/SuggestionsPage";
import { TimelinePage } from "./pages/TimelinePage";

interface AppProps {
  api?: DesktopApi;
}

export function App({ api = desktopApi }: AppProps) {
  const [page, setPage] = useState<PageId>("overview");
  const [snapshot, setSnapshot] = useState<DesktopSnapshot | null>(null);
  const [sourceSubject, setSourceSubject] = useState<SourceSubject | null>(null);
  const [loading, setLoading] = useState(true);
  const [pauseBusy, setPauseBusy] = useState(false);
  const [error, setError] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const snapshotRequest = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++snapshotRequest.current;
    try {
      const next = await api.snapshot();
      if (requestId === snapshotRequest.current) setSnapshot(next);
    } catch (reason: unknown) {
      if (requestId === snapshotRequest.current) throw reason;
    }
  }, [api]);

  useEffect(() => {
    let current = true;
    const requestId = ++snapshotRequest.current;
    setLoading(true);
    void api
      .snapshot()
      .then((next) => {
        if (current && requestId === snapshotRequest.current) setSnapshot(next);
      })
      .catch((reason: unknown) => {
        if (current && requestId === snapshotRequest.current) setError(displayError(reason));
      })
      .finally(() => {
        if (current && requestId === snapshotRequest.current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [api]);

  useEffect(() => {
    let unlisten: (() => void) | undefined;
    void listen<string>("desktop:navigate", (event) => {
      if (
        event.payload === "permissions" ||
        event.payload === "privacy" ||
        event.payload === "prompt-rescue"
      ) {
        setPage(event.payload === "prompt-rescue" ? "prompt-rescue" : "privacy");
        setSourceSubject(null);
        window.requestAnimationFrame(() => document.getElementById("main-content")?.focus());
      }
    })
      .then((value) => {
        unlisten = value;
      })
      .catch(() => {
        // A normal browser preview has no Tauri event transport.
      });
    return () => unlisten?.();
  }, []);

  useEffect(() => {
    let unlisten: (() => void) | undefined;
    void listen("desktop:refresh", () => {
      void refresh().catch((reason: unknown) => setError(displayError(reason)));
    })
      .then((value) => {
        unlisten = value;
      })
      .catch(() => {
        // A normal browser preview has no Tauri event transport.
      });
    return () => unlisten?.();
  }, [refresh]);

  function navigate(next: PageId) {
    setPage(next);
    setSourceSubject(null);
    window.requestAnimationFrame(() => document.getElementById("main-content")?.focus());
  }

  async function setCapturePaused(paused: boolean) {
    if (!snapshot) return;
    setPauseBusy(true);
    setError("");
    try {
      const result = await api.setCapturePaused(snapshot.capture.paused, paused);
      setSnapshot((current) =>
        current
          ? {
              ...current,
              capture: {
                ...current.capture,
                paused: result.paused,
                state: result.paused ? "paused" : "active",
              },
            }
          : current,
      );
      setAnnouncement(paused ? "New desktop capture paused." : "New desktop capture resumed.");
      try {
        await refresh();
      } catch {
        setError("Capture state changed, but the latest local snapshot could not be refreshed.");
      }
    } catch (reason: unknown) {
      setError(displayError(reason));
    } finally {
      setPauseBusy(false);
    }
  }

  return (
    <>
      <a className="skip-link" href="#main-content">Skip to content</a>
      <div className="app-shell">
        <Sidebar
          current={page}
          onNavigate={navigate}
          reviewCount={snapshot
            ? snapshot.review_counts.pending +
              snapshot.review_counts.conflict +
              snapshot.review_counts.applying
            : 0}
          suggestionCount={snapshot?.suggestions.length ?? 0}
          promptRescueCount={snapshot?.prompt_rescue.jobs.length ?? 0}
          replyRescueCount={snapshot?.reply_rescue.jobs.length ?? 0}
        />
        <div className="workspace">
          <div aria-live="polite" className="sr-only" role="status">{announcement}</div>
          {error ? (
            <div className="global-error" role="alert">
              <UntrustedText>{error}</UntrustedText>
              <button
                className="button button--ghost"
                onClick={() => {
                  setError("");
                  setLoading(true);
                  void refresh()
                    .catch((reason: unknown) => setError(displayError(reason)))
                    .finally(() => setLoading(false));
                }}
                type="button"
              >
                Retry local connection
              </button>
            </div>
          ) : null}
          {loading && !snapshot ? (
            <main className="loading-state" id="main-content" tabIndex={-1}>
              <div className="loading-mark" aria-hidden="true">OC</div>
              <h1>Opening trusted local console</h1>
              <p role="status">Reading the local snapshot. No model is being contacted.</p>
            </main>
          ) : null}
          {snapshot && page === "overview" ? (
            <OverviewPage
              busy={pauseBusy}
              onOpenReview={() => navigate("review")}
              onOpenSuggestions={() => navigate("suggestions")}
              onOpenWrap={() => navigate("daily-wrap")}
              onSetPaused={(paused) => void setCapturePaused(paused)}
              snapshot={snapshot}
            />
          ) : null}
          {snapshot && page === "suggestions" ? (
            <SuggestionsPage
              api={api}
              enabled={snapshot.suggestions_enabled}
              onChanged={refresh}
              onOpenSource={setSourceSubject}
              suggestions={snapshot.suggestions}
            />
          ) : null}
          {snapshot && page === "prompt-rescue" ? (
            <PromptRescuePage
              api={api}
              daemonRunning={snapshot.daemon.state !== "stopped"}
              onChanged={refresh}
              rescue={snapshot.prompt_rescue}
            />
          ) : null}
          {snapshot && page === "reply-rescue" ? (
            <ReplyRescuePage
              api={api}
              daemonRunning={snapshot.daemon.state !== "stopped"}
              onChanged={refresh}
              rescue={snapshot.reply_rescue}
            />
          ) : null}
          {snapshot && page === "review" ? (
            <ReviewPage
              api={api}
              candidates={snapshot.candidates}
              onChanged={refresh}
              onForgotten={() => setSourceSubject(null)}
              onOpenSource={setSourceSubject}
            />
          ) : null}
          {snapshot && page === "daily-wrap" ? (
            <DailyWrapPage
              api={api}
              onOpenSource={setSourceSubject}
              summaries={snapshot.daily_wraps}
            />
          ) : null}
          {snapshot && page === "timeline" ? (
            <TimelinePage items={snapshot.timeline} onOpenSource={setSourceSubject} />
          ) : null}
          {snapshot && page === "privacy" ? <PrivacyPage snapshot={snapshot} /> : null}
        </div>
      </div>
      <SourceDrawer api={api} onClose={() => setSourceSubject(null)} subject={sourceSubject} />
    </>
  );
}
