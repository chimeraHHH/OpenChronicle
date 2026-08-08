"""Classifier stage: event-daily → user-/project-/topic-/tool-/person-/org-.

Runs after the S2 reducer successfully appends a session summary to
``event-YYYY-MM-DD.md``. Reads that entry plus a small window of the
preceding entries of the same day, calls the ``classifier`` LLM stage,
and lets it retrieve context and stage grounded candidates in a local review
inbox (read_memory / search_memory / propose_memory_candidate / commit).

The classifier has no Markdown mutation tools. Event-daily remains reducer
owned, and durable memories are materialized only after explicit review.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..capture import filenames as capture_filenames
from ..config import Config
from ..logger import get
from ..memory_candidates import store as candidate_store
from ..privacy.egress import model_egress_lock
from ..prompts import load as load_prompt
from ..provenance.models import EvidenceRef, content_digest, timeline_block_digest
from ..services.context import ContextService
from ..services.evidence import EvidenceResolver
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import fts
from . import classifier_jobs
from . import llm as llm_mod
from . import tools as tools_mod

logger = get("openchronicle.writer")

# How many trailing entries from yesterday's event-daily file to carry in as
# context. One day is deliberate: the classifier has retrieval tools
# (`search_memory` / `read_memory`) and should pull more on its own if a
# specific fact seems to need older grounding.
_PRIOR_DAY_ENTRIES = 8


@dataclass
class ClassifyResult:
    session_id: str
    committed: bool = False
    summary: str = ""
    written_ids: list[str] = field(default_factory=list)
    created_paths: list[str] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    iterations: int = 0
    skipped_reason: str = ""
    error: str = ""
    producer_run_key: str = ""
    input_digest: str = ""


def classify_window(
    cfg: Config,
    *,
    session_id: str,
    event_daily_path: str,
    start: datetime,
    end: datetime,
    include_prior_day: bool = False,
    delivery_job_id: str = "",
    delivery_lease_token: str = "",
    focus_entry_ids: list[str] | None = None,
    allow_empty_delivery: bool = False,
) -> ClassifyResult:
    """Classify event-daily entries for ``session_id`` within ``[start, end)``.

    Used by two callers:
      * the 30-min classifier tick during an active session — classifies the
        window ``[classified_end or session_start, now)`` and advances
        ``classified_end`` on success.
      * the terminal classifier after session-end reduce — classifies the
        trailing window ``[classified_end or session_start, session_end)``.

    Only entries in event-daily tagged ``sid:<session_id>`` with a
    timestamp in the window count as focus entries; if none match the
    window, the tick is a silent no-op.
    """
    if not cfg.reducer.enabled:
        return ClassifyResult(session_id=session_id, skipped_reason="reducer disabled")

    with fts.cursor() as conn:
        if delivery_job_id:
            classifier_jobs.assert_lease(
                conn,
                job_id=delivery_job_id,
                lease_token=delivery_lease_token,
            )
        else:
            entries_mod.write_preset_files(conn)
        if candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=event_daily_path):
            if delivery_job_id:
                raise classifier_jobs.ClassifierJobInputChanged(
                    "classifier source is pending permanent purge"
                )
            input_digest = _delivery_input_digest(
                event_daily_path=event_daily_path,
                session_id=session_id,
                start=start,
                end=end,
                evidence=[],
            )
            run_key = _classifier_run_key(
                session_id=session_id,
                event_daily_path=event_daily_path,
                evidence=[],
            )
            if delivery_job_id:
                run_key = classifier_jobs.make_producer_run_key(delivery_job_id, input_digest)
                bound = classifier_jobs.bind_input(
                    conn,
                    job_id=delivery_job_id,
                    lease_token=delivery_lease_token,
                    input_digest=input_digest,
                    producer_run_key=run_key,
                )
                run_key = bound.producer_run_key
            return ClassifyResult(
                session_id=session_id,
                skipped_reason="event memory file is pending permanent purge",
                producer_run_key=run_key,
                input_digest=input_digest,
            )

        focus_entries = _focus_entries_in_range(
            conn=conn,
            cfg=cfg,
            event_daily_path=event_daily_path,
            session_id=session_id,
            start=start,
            end=end,
            focus_entry_ids=focus_entry_ids,
            strict_coverage=bool(delivery_job_id),
        )
        delivery_run_key = _classifier_run_key(
            session_id=session_id,
            event_daily_path=event_daily_path,
            evidence=[],
        )
        if not focus_entries:
            if delivery_job_id and not allow_empty_delivery:
                raise classifier_jobs.ClassifierJobInputChanged(
                    "durably materialized classifier window has no reducer entry"
                )
            input_digest = _delivery_input_digest(
                event_daily_path=event_daily_path,
                session_id=session_id,
                start=start,
                end=end,
                evidence=[],
            )
            if delivery_job_id:
                delivery_run_key = classifier_jobs.make_producer_run_key(
                    delivery_job_id, input_digest
                )
                bound = classifier_jobs.bind_input(
                    conn,
                    job_id=delivery_job_id,
                    lease_token=delivery_lease_token,
                    input_digest=input_digest,
                    producer_run_key=delivery_run_key,
                )
                delivery_run_key = bound.producer_run_key
            return ClassifyResult(
                session_id=session_id,
                skipped_reason=(
                    classifier_jobs.EMPTY_TERMINAL_SKIP
                    if allow_empty_delivery
                    else "no session entries in window"
                ),
                producer_run_key=delivery_run_key,
                input_digest=input_digest,
            )

        timeline_text, timeline_evidence = _render_timeline_blocks(conn, cfg, start, end)
        prior_day_text, prior_day_evidence = (
            _render_prior_day(conn, cfg, start) if include_prior_day else ("", [])
        )

        context = _assemble_context(
            event_daily_path=event_daily_path,
            focus_entries=focus_entries,
            timeline_text=timeline_text,
            prior_day_text=prior_day_text,
        )

        initial_evidence = [
            *_entry_evidence(event_daily_path, focus_entries),
            *timeline_evidence,
            *prior_day_evidence,
        ]
        delivery_run_key = _classifier_run_key(
            session_id=session_id,
            event_daily_path=event_daily_path,
            evidence=initial_evidence,
        )
        input_digest = _delivery_input_digest(
            event_daily_path=event_daily_path,
            session_id=session_id,
            start=start,
            end=end,
            evidence=initial_evidence,
        )
        if delivery_job_id:
            delivery_run_key = classifier_jobs.make_producer_run_key(delivery_job_id, input_digest)
            bound = classifier_jobs.bind_input(
                conn,
                job_id=delivery_job_id,
                lease_token=delivery_lease_token,
                input_digest=input_digest,
                producer_run_key=delivery_run_key,
            )
            delivery_run_key = bound.producer_run_key

        def validate_delivery_input() -> None:
            if not delivery_job_id:
                return
            if candidate_store.is_tombstoned(
                conn, kind="memory_file", artifact_id=event_daily_path
            ):
                raise classifier_jobs.ClassifierJobInputChanged(
                    "classifier source became pending permanent purge"
                )
            current_focus = _focus_entries_in_range(
                conn=conn,
                cfg=cfg,
                event_daily_path=event_daily_path,
                session_id=session_id,
                start=start,
                end=end,
                focus_entry_ids=focus_entry_ids,
                strict_coverage=True,
            )
            _, current_timeline = _render_timeline_blocks(conn, cfg, start, end)
            _, current_prior_day = (
                _render_prior_day(conn, cfg, start) if include_prior_day else ("", [])
            )
            current_digest = _delivery_input_digest(
                event_daily_path=event_daily_path,
                session_id=session_id,
                start=start,
                end=end,
                evidence=[
                    *_entry_evidence(event_daily_path, current_focus),
                    *current_timeline,
                    *current_prior_day,
                ],
            )
            if current_digest != input_digest:
                raise classifier_jobs.ClassifierJobInputChanged(
                    f"classifier input changed for delivery {delivery_job_id}"
                )

        return _run_tool_loop(
            cfg,
            conn,
            session_id=session_id,
            event_daily_path=event_daily_path,
            context=context,
            initial_evidence=initial_evidence,
            delivery_job_id=delivery_job_id,
            delivery_lease_token=delivery_lease_token,
            producer_run_key=delivery_run_key,
            input_digest=input_digest,
            validate_delivery_input=validate_delivery_input,
        )


def classify_after_reduce(
    cfg: Config,
    *,
    session_id: str,
    event_daily_path: str,
    just_written_entry_id: str = "",
    session_start: datetime | None = None,
    session_end: datetime | None = None,
    window_start: datetime | None = None,
) -> ClassifyResult:
    """Terminal-reduce classifier entry point.

    If ``window_start`` is provided (e.g. ``classified_end`` from the
    sessions table), classify only the trailing window
    ``[window_start, session_end)`` — the 30-min tick has already handled
    everything earlier in the session. Otherwise fall back to the whole
    session (behaves like the legacy callsite).
    """
    if not cfg.reducer.enabled:
        return ClassifyResult(session_id=session_id, skipped_reason="reducer disabled")

    if session_start is None or session_end is None:
        # Legacy path: no time bounds available — best we can do is classify
        # every entry tagged with this session and hope for the best.
        return _classify_untimed(
            cfg,
            session_id=session_id,
            event_daily_path=event_daily_path,
            just_written_entry_id=just_written_entry_id,
        )

    effective_start = window_start or session_start
    # Event-daily entries are appended with wall-clock "now" timestamps
    # (not the session's nominal start/end), so the focus-entry filter
    # must end at the current moment — especially on the catch-up path
    # where the reducer runs long after session_end.
    now = datetime.now().astimezone()
    window_end = max(session_end, now)
    if effective_start >= window_end:
        return ClassifyResult(
            session_id=session_id,
            skipped_reason="terminal window empty (already classified)",
        )
    return classify_window(
        cfg,
        session_id=session_id,
        event_daily_path=event_daily_path,
        start=effective_start,
        end=window_end,
        include_prior_day=True,
    )


def _classify_untimed(
    cfg: Config,
    *,
    session_id: str,
    event_daily_path: str,
    just_written_entry_id: str,
) -> ClassifyResult:
    with fts.cursor() as conn:
        entries_mod.write_preset_files(conn)
        if candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=event_daily_path):
            return ClassifyResult(
                session_id=session_id,
                skipped_reason="event memory file is pending permanent purge",
            )
        focus_entries = _focus_entries(
            conn=conn,
            cfg=cfg,
            event_daily_path=event_daily_path,
            session_id=session_id,
            fallback_entry_id=just_written_entry_id,
        )
        if not focus_entries:
            return ClassifyResult(
                session_id=session_id,
                skipped_reason=f"no entries found in {event_daily_path}",
            )
        context = _assemble_context(
            event_daily_path=event_daily_path,
            focus_entries=focus_entries,
            timeline_text="",
            prior_day_text="",
        )
        return _run_tool_loop(
            cfg,
            conn,
            session_id=session_id,
            event_daily_path=event_daily_path,
            context=context,
            initial_evidence=_entry_evidence(event_daily_path, focus_entries),
        )


def _focus_entries_in_range(
    *,
    conn: sqlite3.Connection,
    cfg: Config,
    event_daily_path: str,
    session_id: str,
    start: datetime,
    end: datetime,
    focus_entry_ids: list[str] | None = None,
    strict_coverage: bool = False,
) -> list[files_mod.ParsedEntry]:
    path = files_mod.memory_path(event_daily_path)
    if not path.exists():
        return []
    try:
        parsed = files_mod.read_file(path)
    except Exception:  # noqa: BLE001
        return []
    sid_tag = f"sid:{session_id}"
    required_ids = set(focus_entry_ids or [])
    matches: list[files_mod.ParsedEntry] = []
    for e in parsed.entries:
        if sid_tag not in e.tags:
            continue
        if e.id in required_ids:
            matches.append(e)
            continue
        encoded_end = next(
            (
                tag.removeprefix("oc-window-end:")
                for tag in e.tags
                if tag.startswith("oc-window-end:")
            ),
            "",
        )
        covered_end = capture_filenames.parse_capture_stem(encoded_end)
        if covered_end is not None:
            covered_cmp = _align_tz(covered_end, start)
            if start < covered_cmp <= end:
                matches.append(e)
            continue
        if strict_coverage:
            # New reducer entries carry an explicit coverage boundary. Older
            # entries do not, so they cannot safely be assigned to a durable
            # periodic window. Ignore those ambiguous legacy entries here;
            # terminal upgrade recovery supplies its exact entry ID, which is
            # handled above, while a periodic window with no proven entry
            # fails closed in ``classify_window``.
            continue
        ts = _parse_entry_ts(e.timestamp)
        if ts is None:
            # Timestamp unparseable — keep it so the classifier sees it
            # rather than silently dropping a tagged entry.
            matches.append(e)
            continue
        ts_cmp = _align_tz(ts, start)
        start_cmp = start
        end_cmp = end
        if start_cmp <= ts_cmp < end_cmp:
            matches.append(e)
    matches = [
        entry
        for entry in matches
        if not candidate_store.is_tombstoned(
            conn,
            kind="memory_entry",
            artifact_id=entry.id,
            path=event_daily_path,
        )
        and tools_mod.memory_entry_allowed(
            conn,
            cfg,
            path=event_daily_path,
            entry=entry,
        )
    ]
    missing_required = required_ids - {entry.id for entry in matches}
    if missing_required:
        raise classifier_jobs.ClassifierJobInputChanged(
            "required terminal classifier entry is missing or changed: "
            + ", ".join(sorted(missing_required))
        )
    return matches


def _align_tz(ts: datetime, ref: datetime) -> datetime:
    """Make ``ts`` comparable with ``ref`` — if one is naive, make the other naive too."""
    if (ts.tzinfo is None) == (ref.tzinfo is None):
        return ts
    if ts.tzinfo is None and ref.tzinfo is not None:
        return ts.replace(tzinfo=ref.tzinfo)
    return ts.replace(tzinfo=None)


def _instant(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(UTC)


def _parse_entry_ts(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _focus_entries(
    *,
    conn: sqlite3.Connection,
    cfg: Config,
    event_daily_path: str,
    session_id: str,
    fallback_entry_id: str,
) -> list[files_mod.ParsedEntry]:
    """Return every entry in today's event-daily tagged with this session.

    Falls back to ``[fallback_entry_id]`` (the single last-written entry) if
    the session tag is missing — keeps behaviour sane even if the tag
    convention shifts.
    """
    path = files_mod.memory_path(event_daily_path)
    if not path.exists():
        return []
    try:
        parsed = files_mod.read_file(path)
    except Exception:  # noqa: BLE001
        return []
    sid_tag = f"sid:{session_id}"
    matches = [e for e in parsed.entries if sid_tag in e.tags]
    if not matches:
        matches = [e for e in parsed.entries if e.id == fallback_entry_id]
    if not matches and parsed.entries:
        matches = [parsed.entries[-1]]
    return [
        entry
        for entry in matches
        if not candidate_store.is_tombstoned(
            conn,
            kind="memory_entry",
            artifact_id=entry.id,
            path=event_daily_path,
        )
        and tools_mod.memory_entry_allowed(
            conn,
            cfg,
            path=event_daily_path,
            entry=entry,
        )
    ]


def _render_timeline_blocks(
    conn: sqlite3.Connection,
    cfg: Config,
    start: datetime,
    end: datetime,
) -> tuple[str, list[EvidenceRef]]:
    rows = conn.execute(
        """
        SELECT id, start_time, end_time, entries, apps_used
          FROM timeline_blocks
         WHERE julianday(end_time) > julianday(?) - 2
           AND julianday(start_time) < julianday(?) + 2
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    filtered: list[tuple[datetime, sqlite3.Row]] = []
    for row in rows:
        row_start = _parse_entry_ts(row["start_time"])
        row_end = _parse_entry_ts(row["end_time"])
        if (
            row_start is not None
            and row_end is not None
            and _instant(row_end) > _instant(start)
            and _instant(row_start) < _instant(end)
        ):
            filtered.append((_instant(row_start), row))
    rows = [row for _, row in sorted(filtered, key=lambda item: item[0])]
    if not rows:
        return "(no timeline blocks recorded for this session)", []
    out: list[str] = []
    evidence: list[EvidenceRef] = []
    authorizer = ContextService(conn, cfg)
    for r in rows:
        try:
            s = datetime.fromisoformat(r["start_time"]).strftime("%H:%M")
            e = datetime.fromisoformat(r["end_time"]).strftime("%H:%M")
        except (TypeError, ValueError):
            s, e = r["start_time"], r["end_time"]
        try:
            entries = json.loads(r["entries"] or "[]")
            apps = json.loads(r["apps_used"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(entries, list) or not isinstance(apps, list):
            continue
        ref = EvidenceRef(
            kind="timeline_block",
            id=r["id"],
            timestamp=r["start_time"],
            content_hash=timeline_block_digest(
                start=r["start_time"],
                end=r["end_time"],
                entries=entries,
                apps=apps,
            ),
        )
        if not authorizer.evidence_allowed(ref):
            continue
        evidence.append(ref)
        header = f"[{s}-{e}] [evidence:{ref.key}]"
        if not entries:
            out.append(f"{header} (no notable activity)")
            continue
        out.append(header)
        out.extend(f"  - {entry}" for entry in entries)
    if not out:
        return "(no policy-allowed timeline blocks recorded for this session)", []
    return "\n".join(out), evidence


def _render_prior_day(
    conn: sqlite3.Connection,
    cfg: Config,
    session_start: datetime,
) -> tuple[str, list[EvidenceRef]]:
    prior_date = (session_start - timedelta(days=1)).strftime("%Y-%m-%d")
    name = f"event-{prior_date}.md"
    if candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=name):
        return "", []
    path = files_mod.memory_path(name)
    if not path.exists():
        return "", []
    try:
        parsed = files_mod.read_file(path)
    except Exception:  # noqa: BLE001
        return "", []
    visible = [
        entry
        for entry in parsed.entries
        if not candidate_store.is_tombstoned(
            conn,
            kind="memory_entry",
            artifact_id=entry.id,
            path=name,
        )
        and tools_mod.memory_entry_allowed(
            conn,
            cfg,
            path=name,
            entry=entry,
        )
    ]
    tail = visible[-_PRIOR_DAY_ENTRIES:]
    if not tail:
        return "", []
    out: list[str] = [f"From {name} (last {len(tail)} entries):", ""]
    evidence: list[EvidenceRef] = []
    for e in tail:
        ref = EvidenceRef(
            kind="memory_entry",
            id=e.id,
            path=name,
            timestamp=e.timestamp,
            content_hash=content_digest(e.body),
        )
        evidence.append(ref)
        out.append(f"### [{e.timestamp}] {{id: {e.id}}} [evidence:{ref.key}]")
        body = e.body.strip()
        if body:
            out.append(body)
        out.append("")
    return "\n".join(out).strip(), evidence


def _assemble_context(
    *,
    event_daily_path: str,
    focus_entries: list[files_mod.ParsedEntry],
    timeline_text: str,
    prior_day_text: str,
) -> str:
    parts: list[str] = [f"Source file: {event_daily_path}", ""]
    parts.append("## Session entries (focus — classify these)")
    for e in focus_entries:
        ref = EvidenceRef(
            kind="memory_entry",
            id=e.id,
            path=event_daily_path,
            timestamp=e.timestamp,
            content_hash=content_digest(e.body),
        )
        parts.append(f"### [{e.timestamp}] {{id: {e.id}}} [evidence:{ref.key}]")
        body = e.body.strip()
        if body:
            parts.append(body)
        parts.append("")
    if timeline_text:
        parts.append("## Timeline blocks covering this session")
        parts.append(
            "These are the verbatim-preserving activity slices the reducer compressed. "
            "Use them to ground any durable fact you're considering writing — "
            "or to skip a fact that the compressed entry overstates."
        )
        parts.append("")
        parts.append(timeline_text)
        parts.append("")
    if prior_day_text:
        parts.append("## Preceding day (context, dedup anchor)")
        parts.append(prior_day_text)
        parts.append("")
    parts.append(
        "If you need earlier history or adjacent entity files, call "
        "`search_memory` or `read_memory` — don't guess."
    )
    return "\n".join(parts).strip()


def _render_index(conn: sqlite3.Connection) -> str:
    del conn
    # File-level index metadata has no entry-level provenance binding and can
    # itself contain model-derived private text.  Retrieval tools expose only
    # policy-authorized, hash-bound entries, so the classifier discovers
    # adjacent memory through those tools instead of receiving a raw index.
    return "(index metadata withheld; use search_memory or read_memory)"


def _run_tool_loop(
    cfg: Config,
    conn: sqlite3.Connection,
    *,
    session_id: str,
    event_daily_path: str,
    context: str,
    initial_evidence: list[EvidenceRef],
    delivery_job_id: str = "",
    delivery_lease_token: str = "",
    producer_run_key: str = "",
    input_digest: str = "",
    validate_delivery_input: Callable[[], None] | None = None,
) -> ClassifyResult:
    system = load_prompt("classifier.md")
    schema = load_prompt("schema.md")
    index = _render_index(conn)

    user_msg = (
        f"# Schema\n\n{schema}\n\n"
        f"# Memory index\n\n{index}\n\n"
        f"# Event-daily context\n\n{context}\n\n"
        f"Source file (do NOT write to it): {event_daily_path}\n"
        f"Session being classified: {session_id}"
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    state = tools_mod.CommitState(
        producer_run_key=producer_run_key
        or _classifier_run_key(
            session_id=session_id,
            event_daily_path=event_daily_path,
            evidence=initial_evidence,
        ),
    )
    for ref in initial_evidence:
        if not state.expose_evidence(ref):
            return ClassifyResult(
                session_id=session_id,
                error="classifier input contained conflicting evidence revisions",
                producer_run_key=state.producer_run_key,
                input_digest=input_digest,
            )
    if bool(delivery_job_id) != bool(delivery_lease_token):
        raise ValueError("classifier delivery job and lease token must be provided together")
    lease_seconds = max(
        30,
        int(getattr(cfg.classifier, "lease_seconds", 300)),
        math.ceil(llm_mod.call_budget_seconds(cfg, "classifier")) + 60,
    )
    if delivery_job_id and lease_seconds > 21_600:
        raise ValueError("classifier provider call budget exceeds the maximum safe lease")
    if delivery_job_id:

        def persist_commit(commit_state: tools_mod.CommitState) -> None:
            with files_mod.review_operation_lock():
                classifier_jobs.record_commit(
                    conn,
                    job_id=delivery_job_id,
                    lease_token=delivery_lease_token,
                    producer_run_key=commit_state.producer_run_key,
                    result={
                        "committed": True,
                        "summary": commit_state.summary,
                        "written_ids": list(commit_state.written_ids),
                        "created_paths": list(commit_state.created_paths),
                        "candidate_ids": list(commit_state.candidate_ids),
                        "skipped_reason": "",
                    },
                    transaction_guard=(
                        (lambda _connection: validate_delivery_input())
                        if validate_delivery_input is not None
                        else None
                    ),
                )

        state.commit_callback = persist_commit

        def guard_mutation(connection: sqlite3.Connection) -> None:
            classifier_jobs.assert_lease(
                connection,
                job_id=delivery_job_id,
                lease_token=delivery_lease_token,
            )
            if validate_delivery_input is not None:
                validate_delivery_input()

        state.mutation_guard = guard_mutation
    max_iter = cfg.writer.max_tool_iterations
    last_error = ""
    iterations = 0

    for iteration in range(max_iter):
        iterations = iteration + 1
        try:
            if delivery_job_id:
                classifier_jobs.renew(
                    conn,
                    job_id=delivery_job_id,
                    lease_token=delivery_lease_token,
                    lease_seconds=lease_seconds,
                )
            with model_egress_lock():
                if validate_delivery_input is not None:
                    validate_delivery_input()
                if state.evidence_conflicts or any(
                    EvidenceResolver(conn, cfg).resolve(ref)["status"] != "current"
                    for ref in state.allowed_evidence.values()
                ):
                    raise classifier_jobs.ClassifierJobInputChanged(
                        "classifier evidence changed before provider egress"
                    )
                resp = llm_mod.call_llm(
                    cfg,
                    "classifier",
                    messages=messages,
                    tools=tools_mod.CLASSIFIER_TOOL_SCHEMAS,
                )
            if delivery_job_id:
                # Renew after provider I/O as well so tool-side mutations have
                # a full fenced interval even when the call used most of its
                # configured timeout budget.
                classifier_jobs.renew(
                    conn,
                    job_id=delivery_job_id,
                    lease_token=delivery_lease_token,
                    lease_seconds=lease_seconds,
                )
        except (
            classifier_jobs.ClassifierJobInputChanged,
            classifier_jobs.ClassifierJobLostLease,
        ):
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "classifier %s: LLM call failed at iter %d: %s",
                session_id,
                iteration,
                exc,
            )
            break

        tool_calls = llm_mod.extract_tool_calls(resp)
        text = llm_mod.extract_text(resp)

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": c["id"] or f"call_{iteration}_{i}",
                    "type": "function",
                    "function": {
                        "name": c["name"],
                        "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                    },
                }
                for i, c in enumerate(tool_calls)
            ]
        messages.append(assistant_msg)

        if not tool_calls:
            last_error = "classifier ended without an explicit commit"
            logger.info("classifier %s: ended without commit at iter %d", session_id, iteration)
            break

        for i, call in enumerate(tool_calls):
            name = call["name"]
            args = call["arguments"] or {}
            if name not in tools_mod.CLASSIFIER_TOOL_NAMES:
                result = {"error": f"unknown tool: {name}"}
            else:
                try:
                    result = tools_mod.dispatch_classifier(
                        name,
                        args,
                        conn=conn,
                        cfg=cfg,
                        soft_limit_tokens=cfg.writer.soft_limit_tokens,
                        state=state,
                    )
                except (
                    classifier_jobs.ClassifierJobInputChanged,
                    classifier_jobs.ClassifierJobLostLease,
                ):
                    raise
                except Exception as exc:  # noqa: BLE001
                    result = {"error": f"tool crashed: {exc}"}
                    logger.exception("classifier tool %s failed", name)
            messages.append(_tool_response(assistant_msg, i, name, result))
            if state.committed:
                # Commit is terminal. Ignoring any later calls in the same
                # assistant batch prevents effects from landing after the
                # durable receipt was written.
                break

        if state.committed:
            return ClassifyResult(
                session_id=session_id,
                committed=True,
                summary=state.summary,
                written_ids=list(state.written_ids),
                created_paths=list(state.created_paths),
                candidate_ids=list(state.candidate_ids),
                iterations=iterations,
                producer_run_key=state.producer_run_key,
                input_digest=input_digest,
            )

    return ClassifyResult(
        session_id=session_id,
        committed=state.committed,
        summary=state.summary,
        written_ids=list(state.written_ids),
        created_paths=list(state.created_paths),
        candidate_ids=list(state.candidate_ids),
        iterations=iterations,
        error=last_error or "classifier exhausted tool iterations without commit",
        producer_run_key=state.producer_run_key,
        input_digest=input_digest,
    )


def _entry_evidence(path: str, entries: list[files_mod.ParsedEntry]) -> list[EvidenceRef]:
    return [
        EvidenceRef(
            kind="memory_entry",
            id=entry.id,
            path=path,
            timestamp=entry.timestamp,
            content_hash=content_digest(entry.body),
        )
        for entry in entries
    ]


def _classifier_run_key(
    *,
    session_id: str,
    event_daily_path: str,
    evidence: list[EvidenceRef],
) -> str:
    material = ["classifier-run-v1", session_id, event_daily_path]
    material.extend(
        sorted(f"{ref.kind}\0{ref.path}\0{ref.id}\0{ref.content_hash}" for ref in evidence)
    )
    return hashlib.sha256("\0".join(material).encode()).hexdigest()


def _delivery_input_digest(
    *,
    event_daily_path: str,
    session_id: str,
    start: datetime,
    end: datetime,
    evidence: list[EvidenceRef],
) -> str:
    material = [
        "classifier-input-v1",
        session_id,
        event_daily_path,
        start.isoformat(),
        end.isoformat(),
    ]
    material.extend(
        sorted(f"{ref.kind}\0{ref.path}\0{ref.id}\0{ref.content_hash}" for ref in evidence)
    )
    return hashlib.sha256("\0".join(material).encode()).hexdigest()


def _tool_response(
    assistant_msg: dict[str, Any], i: int, name: str, result: dict[str, Any]
) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": assistant_msg["tool_calls"][i]["id"],
        "name": name,
        "content": json.dumps(result, ensure_ascii=False),
    }
