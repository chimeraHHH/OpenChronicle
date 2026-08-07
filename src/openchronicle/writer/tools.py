"""Writer tool implementations + JSON Schema declarations for the LLM."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..logger import get
from ..memory_candidates import store as candidate_store
from ..provenance.models import EvidenceRef, content_digest
from ..services.memory import MemoryService
from ..store import entries as entries_mod
from ..store import files as files_mod
from ..store import fts

logger = get("openchronicle.writer")


@dataclass
class CommitState:
    committed: bool = False
    summary: str = ""
    written_ids: list[str] = field(default_factory=list)
    created_paths: list[str] = field(default_factory=list)
    flagged_compact: list[str] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    allowed_evidence: dict[str, EvidenceRef] = field(default_factory=dict)
    producer_run_key: str = ""
    next_proposal_slot: int = 0
    commit_callback: Callable[[CommitState], None] | None = None
    mutation_guard: Callable[[sqlite3.Connection], None] | None = None


# ─── tool implementations ────────────────────────────────────────────────

def tool_read_memory(
    conn: sqlite3.Connection,
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
        if candidate_store.is_tombstoned(
            conn, kind="memory_file", artifact_id=p.name
        ):
            return {"error": f"file not found: {path}"}
        if not p.exists():
            return {"error": f"file not found: {path}"}
        parsed = files_mod.read_file(p)
        if candidate_store.is_tombstoned(
            conn, kind="memory_file", artifact_id=p.name
        ):
            return {"error": f"file not found: {path}"}
        if any(not entry.provenance_valid for entry in parsed.entries):
            return {"error": f"invalid provenance frame in {path}"}
        visible = [
            entry
            for entry in parsed.entries
            if not candidate_store.is_tombstoned(
                conn, kind="memory_entry", artifact_id=entry.id, path=p.name
            )
            and entries_mod.dependency_sources_are_live(
                conn, entry.evidence_refs
            )
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
                state.allowed_evidence[ref.key] = ref
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
        return {
            "path": p.name,
            "description": parsed.description,
            "tags": parsed.tags,
            "status": parsed.status,
            "entry_count": len(visible),
            "updated": parsed.updated,
            "entries": entries,
        }


def tool_search_memory(
    conn: sqlite3.Connection,
    *,
    query: str,
    top_k: int = 5,
    include_superseded: bool = False,
    state: CommitState | None = None,
) -> dict[str, Any]:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
        return {"error": "top_k must be an integer in [1, 20]"}
    with files_mod.store_write_lock():
        hits = fts.search(
            conn,
            query=query,
            path_patterns=[
                f"{prefix}*"
                for prefix in files_mod.VALID_PREFIXES
                if prefix != "event-"
            ],
            top_k=top_k,
            include_superseded=include_superseded,
        )
        results: list[dict[str, Any]] = []
        parsed_by_path: dict[str, files_mod.ParsedFile | None] = {}
        for hit in hits:
            if candidate_store.is_tombstoned(
                conn, kind="memory_file", artifact_id=hit.path
            ):
                continue
            if candidate_store.is_tombstoned(
                conn, kind="memory_entry", artifact_id=hit.id, path=hit.path
            ):
                continue
            if hit.path not in parsed_by_path:
                try:
                    parsed_by_path[hit.path] = files_mod.read_file(
                        files_mod.memory_path(hit.path)
                    )
                except (FileNotFoundError, ValueError):
                    parsed_by_path[hit.path] = None
            parsed = parsed_by_path[hit.path]
            if parsed is None:
                continue
            current = next(
                (entry for entry in parsed.entries if entry.id == hit.id), None
            )
            if (
                current is None
                or not current.provenance_valid
                or not entries_mod.dependency_sources_are_live(
                    conn, current.evidence_refs
                )
                or content_digest(current.body) != content_digest(hit.content)
            ):
                continue
            ref = EvidenceRef(
                kind="memory_entry",
                id=hit.id,
                path=hit.path,
                timestamp=hit.timestamp,
                content_hash=content_digest(current.body),
            )
            if state is not None:
                state.allowed_evidence[ref.key] = ref
            results.append(
                {
                    "id": hit.id,
                    "path": hit.path,
                    "timestamp": hit.timestamp,
                    "content": current.body,
                    "rank": hit.rank,
                    "evidence_token": ref.key,
                }
            )
    return {
        "query": query,
        "results": results,
    }


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
    soft_limit_tokens: int,
    state: CommitState,
) -> dict[str, Any]:
    proposal_slot = state.next_proposal_slot
    if path.strip().startswith("event-"):
        return {"error": "event-daily is reducer-owned and cannot receive candidates"}
    if not evidence_tokens:
        return {"error": "at least one evidence token is required"}
    evidence: list[EvidenceRef] = []
    for token in evidence_tokens:
        ref = state.allowed_evidence.get(str(token))
        if ref is None:
            return {"error": f"unknown or unobserved evidence token: {token}"}
        evidence.append(ref)
    try:
        candidate = MemoryService(
            conn, soft_limit_tokens=soft_limit_tokens
        ).propose_candidate(
            kind=kind,
            target_path=path,
            content=content,
            tags=tags,
            evidence=evidence,
            confidence=confidence,
            conflict_key=conflict_key,
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
            conn, name=path, content=content, tags=tags,
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
    with files_mod.store_write_lock(), files_mod.file_lock(p):
        if candidate_store.is_tombstoned(
            conn, kind="memory_file", artifact_id=p.name
        ):
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
            "description": "BM25 full-text search across all memory. Use to dedup before appending.",
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
                    "content": {"type": "string", "description": "1–3 sentence self-contained fact"},
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
                    "description": {"type": "string", "description": "One-line description; required"},
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
                    "summary": {"type": "string", "description": "One-line summary of what you wrote."},
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
                    "path": {"type": "string"},
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
                },
                "required": ["kind", "path", "content", "tags", "evidence_tokens"],
            },
        },
    },
    TOOL_SCHEMAS[-1],
]
CLASSIFIER_TOOL_NAMES = {
    tool["function"]["name"] for tool in CLASSIFIER_TOOL_SCHEMAS
}


def dispatch_classifier(
    name: str,
    args: dict[str, Any],
    *,
    conn: sqlite3.Connection,
    soft_limit_tokens: int,
    state: CommitState,
) -> dict[str, Any]:
    if name == "read_memory":
        return tool_read_memory(
            conn, path=args["path"], tail_n=args.get("tail_n", 10), state=state
        )
    if name == "search_memory":
        return tool_search_memory(
            conn,
            query=args["query"],
            top_k=args.get("top_k", 5),
            include_superseded=args.get("include_superseded", False),
            state=state,
        )
    if name == "propose_memory_candidate":
        return tool_propose_memory_candidate(
            conn,
            kind=str(args.get("kind") or "fact"),
            path=str(args.get("path") or ""),
            content=str(args.get("content") or ""),
            tags=list(args.get("tags") or []),
            evidence_tokens=[str(token) for token in args.get("evidence_tokens") or []],
            confidence=args.get("confidence"),
            conflict_key=str(args.get("conflict_key") or ""),
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
    soft_limit_tokens: int,
    state: CommitState,
) -> dict[str, Any]:
    if name == "read_memory":
        return tool_read_memory(
            conn, path=args["path"], tail_n=args.get("tail_n", 10), state=state
        )
    if name == "search_memory":
        return tool_search_memory(
            conn,
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
