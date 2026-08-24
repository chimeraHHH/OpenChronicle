"""Bounded, current reviewed-memory context for Prompt Rescue."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass

from ..config import Config
from ..provenance.models import EvidenceRef, content_digest
from ..services.current_facts import CurrentFact, list_current_facts
from ..services.memory_projection import canonical_entry_hits_locked
from ..store import files as files_store
from ..store import fts, semantic

_RECALL_LIMIT = 100
_MAX_ITEMS = 3
_MAX_QUERY_CHARS = 4_000
_MAX_TERMS = 32
_MAX_ITEM_CHARS = 4_000
_TERM_RE = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class ReviewedMemoryItem:
    id: str
    path: str
    content: str
    truncated: bool
    ref: EvidenceRef

    def to_prompt_dict(self) -> dict[str, object]:
        return {
            "memory_id": self.id,
            "path": self.path,
            "content": self.content,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class ReviewedMemorySelection:
    retrieval_mode: str
    items: tuple[ReviewedMemoryItem, ...]

    @property
    def refs(self) -> tuple[EvidenceRef, ...]:
        return tuple(item.ref for item in self.items)

    def to_prompt_dict(self) -> dict[str, object]:
        return {"items": [item.to_prompt_dict() for item in self.items]}


def prompt_rescue_query(
    *,
    rough_prompt: str,
    target: str,
    audience: str,
    constraints: tuple[str, ...],
    desired_format: str,
) -> str:
    """Build a bounded relevance query from the user's explicit request."""
    combined = "\n".join(
        value.strip()
        for value in (rough_prompt, target, audience, *constraints, desired_format)
        if value.strip()
    )
    return combined[:_MAX_QUERY_CHARS]


def select_reviewed_procedures(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    query: str,
) -> ReviewedMemorySelection:
    """Retrieve up to three current, authorized, text-only procedures.

    Markdown remains authoritative. SQLite BM25/vector rows are recall-only and
    every result is re-projected through the current fact and policy views.
    """
    if not query.strip():
        return ReviewedMemorySelection(retrieval_mode="empty_query", items=())

    with files_store.store_write_lock():
        if cfg.search.semantic_enabled:
            try:
                recall_hits = semantic.configured_hybrid_search(
                    conn,
                    search_config=cfg.search,
                    query=query,
                    path_patterns=["procedure-*"],
                    top_k=max(_MAX_ITEMS, min(_RECALL_LIMIT, cfg.search.hybrid_candidate_k)),
                    include_superseded=False,
                )
            except semantic.SemanticIndexUnavailable:
                # Do not silently substitute a different retrieval contract.
                return ReviewedMemorySelection(
                    retrieval_mode="hybrid_unavailable",
                    items=(),
                )
            retrieval_mode = "hybrid_rrf"
        else:
            lexical_query = _lexical_query(query)
            if not lexical_query:
                return ReviewedMemorySelection(retrieval_mode="bm25", items=())
            recall_hits = fts.search(
                conn,
                query=lexical_query,
                path_patterns=["procedure-*"],
                top_k=_RECALL_LIMIT,
                include_superseded=False,
                match_any_terms=True,
            )
            retrieval_mode = "bm25"

        canonical_hits = canonical_entry_hits_locked(conn, cfg, recall_hits)
        current = {
            (fact.path, fact.id): fact
            for fact in list_current_facts(conn, cfg, limit=10_000)
            if _is_reviewed_procedure(fact)
        }
        items: list[ReviewedMemoryItem] = []
        for hit in canonical_hits:
            fact = current.get((hit.path, hit.id))
            if fact is None or fact.content != hit.content:
                continue
            excerpt = fact.content[:_MAX_ITEM_CHARS]
            items.append(
                ReviewedMemoryItem(
                    id=fact.id,
                    path=fact.path,
                    content=excerpt,
                    truncated=len(excerpt) != len(fact.content),
                    ref=EvidenceRef(
                        kind="memory_entry",
                        id=fact.id,
                        path=fact.path,
                        timestamp=fact.recorded_at,
                        content_hash=content_digest(fact.content),
                    ),
                )
            )
            if len(items) >= _MAX_ITEMS:
                break
    return ReviewedMemorySelection(retrieval_mode=retrieval_mode, items=tuple(items))


def reviewed_procedure_refs_are_current(
    conn: sqlite3.Connection,
    cfg: Config,
    refs: tuple[EvidenceRef, ...],
) -> bool:
    """Re-authorize the exact procedure revisions bound to an artifact."""
    if len(refs) > _MAX_ITEMS or not _refs_have_unique_identities(refs):
        return False
    if not refs:
        return True
    with files_store.store_write_lock():
        current = {
            (fact.path, fact.id): fact
            for fact in list_current_facts(conn, cfg, limit=10_000)
            if _is_reviewed_procedure(fact)
        }
        return all(
            ref.kind == "memory_entry"
            and (fact := current.get((ref.path, ref.id))) is not None
            and ref.timestamp == fact.recorded_at
            and ref.content_hash == content_digest(fact.content)
            for ref in refs
        )


def _is_reviewed_procedure(fact: CurrentFact) -> bool:
    tags = {tag.casefold() for tag in fact.tags}
    return bool(
        fact.path.startswith("procedure-")
        and fact.path.endswith(".md")
        and {"procedure", "text-only"}.issubset(tags)
        and fact.assertion_kind in {"user_asserted", "observed", "inferred"}
    )


def _lexical_query(query: str) -> str:
    normalized = unicodedata.normalize("NFKC", query)
    terms: list[str] = []
    seen: set[str] = set()
    for raw in _TERM_RE.findall(normalized):
        term = raw.casefold()
        if len(term) < 2 or term in seen:
            continue
        seen.add(term)
        terms.append(raw)
        if len(terms) >= _MAX_TERMS:
            break
    return " ".join(terms)


def _refs_have_unique_identities(refs: tuple[EvidenceRef, ...]) -> bool:
    identities = {(ref.kind, ref.path, ref.id) for ref in refs}
    return len(identities) == len(refs)
