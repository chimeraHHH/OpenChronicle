"""Writer tool implementations + JSON Schema declarations for the LLM."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..activity import store as activity_store
from ..config import Config
from ..logger import get
from ..memory_candidates import store as candidate_store
from ..privacy.egress import privacy_egress_fenced
from ..provenance.models import EvidenceRef, content_digest
from ..services.activity_projection import (
    CanonicalActivityEvent,
    canonical_activity_events_locked,
)
from ..services.context import ContextService
from ..services.memory import MemoryService
from ..services.memory_projection import canonical_entry_hits_locked
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import fts, semantic

logger = get("openchronicle.writer")

_POLICY_SEARCH_RECALL_LIMIT = 100


@dataclass
class CommitState:
    committed: bool = False
    summary: str = ""
    written_ids: list[str] = field(default_factory=list)
    created_paths: list[str] = field(default_factory=list)
    flagged_compact: list[str] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    # Every source whose content has been exposed to this classifier turn.
    # Candidate authorization is deliberately based on this conservative
    # closure, not only on the subset the model chooses to cite.
    allowed_evidence: dict[str, EvidenceRef] = field(default_factory=dict)
    evidence_conflicts: set[str] = field(default_factory=set)
    producer_run_key: str = ""
    next_proposal_slot: int = 0
    commit_callback: Callable[[CommitState], None] | None = None
    mutation_guard: Callable[[sqlite3.Connection], None] | None = None

    def expose_evidence(self, ref: EvidenceRef) -> bool:
        """Register one prompt-visible source without laundering revisions."""
        existing = self.allowed_evidence.get(ref.key)
        if existing is None:
            self.allowed_evidence[ref.key] = ref
            return True
        if existing == ref:
            return True
        # Evidence tokens intentionally hide hashes.  If an identity changes
        # while the same model turn is alive, retaining either revision would
        # let output derived from the other one acquire the wrong provenance.
        self.evidence_conflicts.add(ref.key)
        return False


# ─── tool implementations ────────────────────────────────────────────────


def memory_entry_allowed(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    path: str,
    entry: files_mod.ParsedEntry,
) -> bool:
    """Shared writer-facing adapter for the central egress authorizer."""
    return ContextService(conn, cfg).memory_entry_allowed(path=path, entry=entry)


@privacy_egress_fenced
def tool_read_memory(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    path: str,
    tail_n: int = 10,
    state: CommitState | None = None,
) -> dict[str, Any]:
    if isinstance(tail_n, bool) or not isinstance(tail_n, int) or not 1 <= tail_n <= 20:
        return {"error": "tail_n must be an integer in [1, 20]"}
    if path.strip().startswith("event-"):
        return {"error": "classifier retrieval cannot open event-daily files"}
    try:
        p = files_mod.memory_path(path)
    except ValueError:
        return {"error": f"file not found: {path}"}
    with files_mod.store_write_lock(), files_mod.file_lock(p):
        if candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=p.name):
            return {"error": f"file not found: {path}"}
        if not p.exists():
            return {"error": f"file not found: {path}"}
        parsed = files_mod.read_file(p)
        if candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=p.name):
            return {"error": f"file not found: {path}"}
        visible = [
            entry
            for entry in parsed.entries
            if not candidate_store.is_tombstoned(
                conn, kind="memory_entry", artifact_id=entry.id, path=p.name
            )
            and memory_entry_allowed(conn, cfg, path=p.name, entry=entry)
        ]
        tail = visible[-tail_n:]
        entries: list[dict[str, Any]] = []
        for entry in tail:
            ref = EvidenceRef(
                kind="memory_entry",
                id=entry.id,
                path=p.name,
                timestamp=entry.timestamp,
                content_hash=content_digest(entry.body),
            )
            if state is not None:
                state.expose_evidence(ref)
            entries.append(
                {
                    "id": entry.id,
                    "timestamp": entry.timestamp,
                    "tags": entry.tags,
                    "body": entry.body,
                    "superseded_by": entry.superseded_by,
                    "evidence_token": ref.key,
                }
            )
        if not entries:
            # Do not turn an otherwise hidden/empty container into a file
            # existence and metadata oracle for the remote classifier.
            return {"error": f"file not found: {path}"}
        return {"path": p.name, "entries": entries}


@privacy_egress_fenced
def tool_search_memory(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    query: str,
    top_k: int = 5,
    include_superseded: bool = False,
    state: CommitState | None = None,
) -> dict[str, Any]:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
        return {"error": "top_k must be an integer in [1, 20]"}
    with files_mod.store_write_lock():
        results: list[dict[str, Any]] = []
        retrieval_mode = "hybrid_rrf" if cfg.search.semantic_enabled else "bm25"
        if cfg.search.semantic_enabled:
            try:
                hits = semantic.configured_hybrid_search(
                    conn,
                    search_config=cfg.search,
                    query=query,
                    path_patterns=[
                        f"{prefix}*"
                        for prefix in files_mod.VALID_PREFIXES
                        if prefix != "event-"
                    ],
                    top_k=_POLICY_SEARCH_RECALL_LIMIT,
                    include_superseded=include_superseded,
                )
            except semantic.SemanticIndexUnavailable as exc:
                return {
                    "query": query,
                    "retrieval_mode": "hybrid_unavailable",
                    "error": str(exc),
                    "results": [],
                }
            for hit in canonical_entry_hits_locked(conn, cfg, hits)[:top_k]:
                ref = EvidenceRef(
                    kind="memory_entry",
                    id=hit.id,
                    path=hit.path,
                    timestamp=hit.timestamp,
                    content_hash=content_digest(hit.content),
                )
                if state is not None:
                    state.expose_evidence(ref)
                results.append(
                    {
                        "id": hit.id,
                        "path": hit.path,
                        "timestamp": hit.timestamp,
                        "content": hit.content,
                        "rank": hit.rank,
                        "evidence_token": ref.key,
                    }
                )
            return {
                "query": query,
                "retrieval_mode": retrieval_mode,
                "results": results,
            }
        offset = 0
        while len(results) < top_k:
            hits = fts.search(
                conn,
                query=query,
                path_patterns=[
                    f"{prefix}*" for prefix in files_mod.VALID_PREFIXES if prefix != "event-"
                ],
                # FTS is recall-only. Page before applying the caller's limit
                # so a long stale/tombstoned prefix cannot hide later current
                # canonical Markdown entries from the classifier.
                top_k=_POLICY_SEARCH_RECALL_LIMIT,
                offset=offset,
                include_superseded=include_superseded,
            )
            if not hits:
                break
            offset += len(hits)
            for hit in canonical_entry_hits_locked(conn, cfg, hits):
                ref = EvidenceRef(
                    kind="memory_entry",
                    id=hit.id,
                    path=hit.path,
                    timestamp=hit.timestamp,
                    content_hash=content_digest(hit.content),
                )
                if state is not None:
                    state.expose_evidence(ref)
                results.append(
                    {
                        "id": hit.id,
                        "path": hit.path,
                        "timestamp": hit.timestamp,
                        "content": hit.content,
                        "rank": hit.rank,
                        "evidence_token": ref.key,
                    }
                )
                if len(results) >= top_k:
                    break
            if len(hits) < _POLICY_SEARCH_RECALL_LIMIT:
                break
    return {
        "query": query,
        "retrieval_mode": retrieval_mode,
        "results": results,
    }


@privacy_egress_fenced
def tool_search_activity_evidence(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    query: str,
    top_k: int = 10,
    adjacent: int = 1,
    state: CommitState | None = None,
) -> dict[str, Any]:
    """Recall reducer-owned session evidence without treating it as memory.

    Event entries stay out of durable-memory semantic indexing. This bounded
    BM25 path exists only so the classifier can verify that an observed
    behavior recurs across independent sessions before proposing a pattern.
    """
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
        return {"error": "top_k must be an integer in [1, 20]"}
    if isinstance(adjacent, bool) or not isinstance(adjacent, int) or not 0 <= adjacent <= 3:
        return {"error": "adjacent must be an integer in [0, 3]"}
    with files_mod.store_write_lock():
        results: list[dict[str, Any]] = []
        offset = 0
        while len(results) < top_k:
            hits = activity_store.search(
                conn,
                query=query,
                top_k=_POLICY_SEARCH_RECALL_LIMIT,
                offset=offset,
            )
            if not hits:
                break
            offset += len(hits)
            for hit in canonical_activity_events_locked(conn, cfg, hits):
                item = _classifier_activity_payload(hit, state=state)
                neighbor_rows = activity_store.neighbors(conn, hit.id, radius=adjacent)
                visible_neighbors = {
                    event.id: event
                    for event in canonical_activity_events_locked(
                        conn,
                        cfg,
                        [event for _relation, _distance, event in neighbor_rows],
                    )
                }
                item["neighbors"] = [
                    {
                        "relation": relation,
                        "distance": distance,
                        **_classifier_activity_payload(
                            visible_neighbors[event.id],
                            state=state,
                        ),
                    }
                    for relation, distance, event in neighbor_rows
                    if event.id in visible_neighbors
                ]
                results.append(item)
                if len(results) >= top_k:
                    break
            if len(hits) < _POLICY_SEARCH_RECALL_LIMIT:
                break
    return {
        "query": query,
        "retrieval_mode": "bm25_activity_evidence",
        "adjacency_radius": adjacent,
        "results": results,
    }


def _classifier_activity_payload(
    event: CanonicalActivityEvent,
    *,
    state: CommitState | None,
) -> dict[str, Any]:
    ref = EvidenceRef(
        kind="memory_entry",
        id=event.source_entry_id,
        path=event.source_path,
        timestamp=event.source_entry_timestamp,
        content_hash=event.source_content_hash,
    )
    if state is not None:
        state.expose_evidence(ref)
    payload: dict[str, Any] = {
        # Preserve the former entry-level identifiers for classifier prompt
        # compatibility while exposing the finer event identity explicitly.
        "id": event.source_entry_id,
        "event_id": event.id,
        "path": event.source_path,
        "timestamp": event.source_entry_timestamp,
        "session_id": event.session_id,
        "start_time": event.start_time,
        "end_time": event.end_time,
        "app_name": event.app_name,
        "content": event.content,
        "summary": event.summary,
        "evidence_token": ref.key,
    }
    if event.rank is not None:
        payload["rank"] = event.rank
    if event.query_mode is not None:
        payload["query_mode"] = event.query_mode
    return payload


def tool_propose_memory_candidate(
    conn: sqlite3.Connection,
    *,
    kind: str,
    path: str,
    content: str,
    tags: list[str],
    evidence_tokens: list[str],
    confidence: float | None,
    conflict_key: str,
    subject_key: str,
    assertion_kind: str,
    soft_limit_tokens: int,
    state: CommitState,
    operation: str = "append",
    target_entry_id: str = "",
    valid_from: str = "",
    valid_to: str = "",
) -> dict[str, Any]:
    proposal_slot = state.next_proposal_slot
    if path.strip().startswith("event-"):
        return {"error": "event-daily is reducer-owned and cannot receive candidates"}
    if not evidence_tokens:
        return {"error": "at least one evidence token is required"}
    if state.evidence_conflicts:
        return {"error": "evidence changed during this classifier run; retry from fresh context"}
    cited_evidence: list[EvidenceRef] = []
    for token in evidence_tokens:
        ref = state.allowed_evidence.get(str(token))
        if ref is None:
            return {"error": f"unknown or unobserved evidence token: {token}"}
        cited_evidence.append(ref)
    clean_operation = operation.strip().lower()
    clean_target_entry_id = target_entry_id.strip()
    target_ref = None
    if clean_operation == "supersede":
        target_ref = next(
            (
                ref
                for ref in cited_evidence
                if ref.kind == "memory_entry"
                and ref.path == path.strip()
                and ref.id == clean_target_entry_id
            ),
            None,
        )
        if target_ref is None:
            return {
                "error": (
                    "supersede target must be read or searched and its evidence token "
                    "must be cited"
                )
            }
        if not any(ref.key != target_ref.key for ref in cited_evidence):
            return {"error": "supersede requires evidence for the replacement fact"}
    elif clean_operation != "append":
        return {"error": "operation must be append or supersede"}
    # ``evidence_tokens`` remain useful citations, but cannot be trusted as
    # an information-flow declaration from the model.  Persist every source
    # exposed before this proposal so a later policy change on *any* input
    # hides and blocks approval of the derived candidate.
    evidence = sorted(
        (
            ref
            for ref in state.allowed_evidence.values()
            if target_ref is None or ref.key != target_ref.key
        ),
        key=lambda ref: (ref.kind, ref.path, ref.id, ref.content_hash),
    )
    if not evidence:
        return {"error": "at least one replacement-fact evidence source is required"}
    claim_evidence = [
        ref
        for ref in cited_evidence
        if target_ref is None or ref.key != target_ref.key
    ]
    try:
        candidate = MemoryService(conn, soft_limit_tokens=soft_limit_tokens).propose_candidate(
            kind=kind,
            operation=clean_operation,
            target_path=path,
            target_entry_id=clean_target_entry_id,
            content=content,
            tags=tags,
            evidence=evidence,
            claim_evidence=claim_evidence,
            confidence=confidence,
            conflict_key=conflict_key,
            subject_key=subject_key,
            assertion_kind=assertion_kind,
            valid_from=valid_from,
            valid_to=valid_to,
            producer_run_key=state.producer_run_key,
            proposal_slot=proposal_slot,
            transaction_guard=state.mutation_guard,
        )
    except (ValueError, FileNotFoundError) as exc:
        return {"error": str(exc)}
    state.next_proposal_slot += 1
    if candidate.id not in state.candidate_ids:
        state.candidate_ids.append(candidate.id)
    return {
        "ok": True,
        "candidate_id": candidate.id,
        "status": candidate.status,
        "operation": candidate.operation,
        "target_entry_id": candidate.target_entry_id,
        "subject_key": candidate.subject_key,
        "assertion_kind": candidate.assertion_kind,
        "review_required": True,
        "proposal_slot": proposal_slot,
    }


def tool_append(
    conn: sqlite3.Connection,
    *,
    path: str,
    content: str,
    tags: list[str],
    soft_limit_tokens: int,
    state: CommitState,
) -> dict[str, Any]:
    try:
        entry_id = entries_mod.append_entry(
            conn,
            name=path,
            content=content,
            tags=tags,
            soft_limit_tokens=soft_limit_tokens,
        )
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}
    state.written_ids.append(entry_id)
    return {"ok": True, "id": entry_id, "path": path}


def tool_create(
    conn: sqlite3.Connection,
    *,
    path: str,
    description: str,
    tags: list[str],
    state: CommitState,
) -> dict[str, Any]:
    try:
        entries_mod.create_file(conn, name=path, description=description, tags=tags)
    except (FileExistsError, ValueError) as exc:
        return {"error": str(exc)}
    state.created_paths.append(path)
    return {"ok": True, "path": path}


def tool_supersede(
    conn: sqlite3.Connection,
    *,
    path: str,
    old_entry_id: str,
    new_content: str,
    reason: str,
    tags: list[str] | None,
    state: CommitState,
) -> dict[str, Any]:
    try:
        new_id = entries_mod.supersede_entry(
            conn,
            name=path,
            old_entry_id=old_entry_id,
            new_content=new_content,
            reason=reason,
            tags=tags,
        )
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}
    state.written_ids.append(new_id)
    return {"ok": True, "new_id": new_id}


def tool_flag_compact(
    conn: sqlite3.Connection, *, path: str, reason: str, state: CommitState
) -> dict[str, Any]:
    entries_mod.require_autocommit(conn)
    p = files_mod.memory_path(path)
    with (
        files_mod.review_operation_lock(),
        files_mod.store_write_lock(),
        files_mod.file_lock(p),
    ):
        if candidate_store.is_tombstoned(conn, kind="memory_file", artifact_id=p.name):
            return {"error": f"file not found: {path}"}
        if not p.exists():
            return {"error": f"file not found: {path}"}
        files_mod._update_frontmatter_unlocked(p, {"needs_compact": True})
        fts.set_needs_compact(conn, p.name, True)
    state.flagged_compact.append(path)
    logger.info("flag_compact: %s (%s)", path, reason)
    return {"ok": True}


def tool_commit(state: CommitState, *, summary: str) -> dict[str, Any]:
    state.summary = summary
    if state.commit_callback is not None:
        # The durable delivery path persists its receipt here, after all prior
        # proposal tools have committed but before callers may advance their
        # progress bookmark.
        state.commit_callback(state)
    state.committed = True
    return {"ok": True}


# ─── JSON Schema declarations (OpenAI tool format) ───────────────────────

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": "Read a memory file (frontmatter + last N entries).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "e.g. 'project-openchronicle.md'"},
                    "tail_n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 10,
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": "BM25 search across currently authorized memory. Use to dedup before appending.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5,
                    },
                    "include_superseded": {"type": "boolean", "default": False},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append",
            "description": "Append a new entry to a memory file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {
                        "type": "string",
                        "description": "1–3 sentence self-contained fact",
                    },
                    "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                },
                "required": ["path", "content", "tags"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create",
            "description": (
                "Create a new memory file. Filename prefix must be one of: "
                "user-, project-, tool-, topic-, person-, org-, event-."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "description": {
                        "type": "string",
                        "description": "One-line description; required",
                    },
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["path", "description", "tags"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "supersede",
            "description": "Mark an old entry as superseded and append the replacement.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_entry_id": {"type": "string"},
                    "new_content": {"type": "string"},
                    "reason": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["path", "old_entry_id", "new_content", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flag_compact",
            "description": "Flag a file for the next compaction pass.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["path", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "commit",
            "description": "Finish this round. Call exactly once at the end.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "One-line summary of what you wrote.",
                    },
                },
                "required": ["summary"],
            },
        },
    },
]


TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}


CLASSIFIER_TOOL_SCHEMAS: list[dict[str, Any]] = [
    TOOL_SCHEMAS[0],
    TOOL_SCHEMAS[1],
    {
        "type": "function",
        "function": {
            "name": "search_activity_evidence",
            "description": (
                "Search reducer-owned historical activity events to verify a "
                "behavior across independent sessions. Each match includes bounded "
                "previous/next context. Results are evidence, not accepted facts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 10,
                    },
                    "adjacent": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 3,
                        "default": 1,
                        "description": "Previous/next event hops returned around each match.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_memory_candidate",
            "description": (
                "Stage one grounded durable-memory proposal for human review. "
                "This never writes Markdown directly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "description": "fact, preference, decision, person, project, tool, or topic",
                    },
                    "operation": {
                        "type": "string",
                        "enum": ["append", "supersede"],
                        "default": "append",
                        "description": (
                            "Use supersede when a reviewed current fact is replaced by "
                            "new evidence. The old fact remains in history."
                        ),
                    },
                    "path": {"type": "string"},
                    "target_entry_id": {
                        "type": "string",
                        "description": "Required for supersede; id returned by read/search.",
                    },
                    "content": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                    "evidence_tokens": {
                        "type": "array",
                        "items": {"type": "string", "pattern": "^ev-[a-f0-9]+$"},
                        "minItems": 1,
                        "maxItems": 20,
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "conflict_key": {
                        "type": "string",
                        "description": "Stable subject/property key used to surface contradictions.",
                    },
                    "subject_key": {
                        "type": "string",
                        "description": (
                            "Canonical fact slot, for example user.editor.preference or "
                            "project.openchronicle.database. Reuse it when superseding."
                        ),
                    },
                    "assertion_kind": {
                        "type": "string",
                        "enum": ["user_asserted", "observed", "inferred"],
                        "description": "How directly the cited evidence supports the fact.",
                    },
                    "valid_from": {
                        "type": "string",
                        "description": "Optional ISO 8601 start of real-world validity.",
                    },
                    "valid_to": {
                        "type": "string",
                        "description": "Optional exclusive ISO 8601 end of real-world validity.",
                    },
                },
                "required": [
                    "kind",
                    "path",
                    "content",
                    "tags",
                    "evidence_tokens",
                    "subject_key",
                    "assertion_kind",
                ],
            },
        },
    },
    TOOL_SCHEMAS[-1],
]
CLASSIFIER_TOOL_NAMES = {tool["function"]["name"] for tool in CLASSIFIER_TOOL_SCHEMAS}


def dispatch_classifier(
    name: str,
    args: dict[str, Any],
    *,
    conn: sqlite3.Connection,
    cfg: Config,
    soft_limit_tokens: int,
    state: CommitState,
) -> dict[str, Any]:
    if name == "read_memory":
        return tool_read_memory(
            conn,
            cfg,
            path=args["path"],
            tail_n=args.get("tail_n", 10),
            state=state,
        )
    if name == "search_memory":
        return tool_search_memory(
            conn,
            cfg,
            query=args["query"],
            top_k=args.get("top_k", 5),
            include_superseded=args.get("include_superseded", False),
            state=state,
        )
    if name == "search_activity_evidence":
        return tool_search_activity_evidence(
            conn,
            cfg,
            query=args["query"],
            top_k=args.get("top_k", 10),
            adjacent=args.get("adjacent", 1),
            state=state,
        )
    if name == "propose_memory_candidate":
        return tool_propose_memory_candidate(
            conn,
            kind=str(args.get("kind") or "fact"),
            operation=str(args.get("operation") or "append"),
            path=str(args.get("path") or ""),
            target_entry_id=str(args.get("target_entry_id") or ""),
            content=str(args.get("content") or ""),
            tags=list(args.get("tags") or []),
            evidence_tokens=[str(token) for token in args.get("evidence_tokens") or []],
            confidence=args.get("confidence"),
            conflict_key=str(args.get("conflict_key") or ""),
            subject_key=str(args.get("subject_key") or ""),
            assertion_kind=str(args.get("assertion_kind") or ""),
            valid_from=str(args.get("valid_from") or ""),
            valid_to=str(args.get("valid_to") or ""),
            soft_limit_tokens=soft_limit_tokens,
            state=state,
        )
    if name == "commit":
        return tool_commit(state, summary=args.get("summary", ""))
    return {"error": f"unknown classifier tool: {name}"}


def dispatch(
    name: str,
    args: dict[str, Any],
    *,
    conn: sqlite3.Connection,
    cfg: Config,
    soft_limit_tokens: int,
    state: CommitState,
) -> dict[str, Any]:
    if name == "read_memory":
        return tool_read_memory(
            conn,
            cfg,
            path=args["path"],
            tail_n=args.get("tail_n", 10),
            state=state,
        )
    if name == "search_memory":
        return tool_search_memory(
            conn,
            cfg,
            query=args["query"],
            top_k=args.get("top_k", 5),
            include_superseded=args.get("include_superseded", False),
            state=state,
        )
    if name == "append":
        return tool_append(
            conn,
            path=args["path"],
            content=args["content"],
            tags=list(args.get("tags", []) or []),
            soft_limit_tokens=soft_limit_tokens,
            state=state,
        )
    if name == "create":
        return tool_create(
            conn,
            path=args["path"],
            description=args["description"],
            tags=list(args.get("tags", []) or []),
            state=state,
        )
    if name == "supersede":
        return tool_supersede(
            conn,
            path=args["path"],
            old_entry_id=args["old_entry_id"],
            new_content=args["new_content"],
            reason=args["reason"],
            tags=list(args.get("tags") or []) or None,
            state=state,
        )
    if name == "flag_compact":
        return tool_flag_compact(
            conn, path=args["path"], reason=args.get("reason", ""), state=state
        )
    if name == "commit":
        return tool_commit(state, summary=args.get("summary", ""))
    return {"error": f"unknown tool: {name}"}
