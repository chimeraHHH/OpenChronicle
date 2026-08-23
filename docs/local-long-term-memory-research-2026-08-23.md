# Local long-term memory research and implementation decisions

Date: 2026-08-23

This report consolidates three parallel investigations: an audit of the current
OpenChronicle implementation, a paper survey, and a repository/competitor
survey. The product constraint is unchanged: local-first, text-product focused,
no direct computer-use feature, and no unnecessary external infrastructure.

## Executive decision

Keep canonical Markdown plus SQLite as the local authority. Build rebuildable
projections and reviewed lifecycle operations around it. Do not replace the
memory plane with a hosted vector service, graph database, or a second agent
runtime.

The target shape is:

```text
immutable local evidence
  -> minute normalization
  -> session/event evidence
  -> reviewed semantic facts and preferences
  -> current/historical fact versions
  -> minimal task-specific retrieval context
  -> suggestions, drafts, summaries, and organization plans
```

The highest-value principle repeated across the literature and repositories is
that memory-writing quality and fact lifecycle matter more than the brand of
vector database. Summaries and profiles must remain rebuildable derivatives;
they must not replace the evidence that supports them.

## Current OpenChronicle position

The repository already has a stronger evidence and deletion foundation than
most reference systems:

- canonical inspectable Markdown with SQLite FTS as a rebuildable projection;
- observation -> timeline -> session event -> candidate -> accepted-entry
  provenance;
- review-first durable-memory proposals rather than direct model mutation;
- permanent purge with transitive derivative cleanup;
- local BM25 + multilingual vector retrieval fused with RRF;
- a native memory regression fixture and a LongMemEval-V2 adapter;
- provenance-aware leaf compaction;
- deterministic reviewed supersession that preserves the old fact and restores
  it if the reviewed replacement is purged;
- a separate historical activity-evidence search for cross-session pattern
  confirmation.

The clean native hybrid baseline currently records Recall@5, MRR, semantic
recall, source identity, and abstention at 1.0, with zero forbidden hits and
about 3.04 ms p95 on the small deterministic fixture. This is a regression
baseline, not a claim of real-world memory quality. A real public
LongMemEval-V2 trajectory smoke also passes; the full benchmark has not yet
been claimed.

## Paper findings

| Work | Useful result or pattern | OpenChronicle decision |
|---|---|---|
| [CoALA](https://arxiv.org/abs/2309.02427) | Separates working, episodic, semantic, and procedural memory. | Use these as logical views in the existing local store, not separate services. |
| [Generative Agents](https://arxiv.org/abs/2304.03442) | Observation streams plus higher-level reflection improve long-horizon behavior. | Keep evidence and reflection separate; every reflection must link back to observations. |
| [EM-LLM](https://arxiv.org/abs/2407.09450) | Event boundaries and temporal adjacency improve long-context retrieval. | Keep minute blocks as observations, then segment by idle/app/window/topic change and retrieve adjacent events. |
| [SeCom](https://arxiv.org/abs/2502.05589) | Topic-consistent segments outperform raw turn/session chunks; compression can reduce noise. | Retrieve event/topic segments, but retain exact lower-level sources for names, numbers, and negation. |
| [TiMem](https://arxiv.org/abs/2601.02845) | Hierarchical temporal consolidation can reduce returned context. | Treat day/week/profile summaries as rebuildable trees whose nodes cite children. Do not make them new authority. |
| [Mem0](https://arxiv.org/abs/2504.19413) | Explicit add/update/delete/no-op decisions improve memory maintenance. | Use append/supersede/no-op under review. Reserve physical delete for explicit user forget. |
| [MemoryBank](https://arxiv.org/abs/2305.10250) | Recall strength and recency can influence ranking. | Apply decay to ranking only; never let time silently delete identity, constraints, or source evidence. |
| [MIRIX](https://arxiv.org/abs/2507.07957) | Multiple logical memory types help multimodal personal assistants. | Reuse the type distinctions; reject its heavier multi-agent/multi-store physical architecture here. |
| [A-MEM](https://arxiv.org/abs/2502.12110) | Atomic notes and neighbor links support evolving memory. | Atomic facts and lightweight relations are useful; models must not autonomously rewrite reviewed history. |

## Repository and competitor findings

| Repository | What to borrow | What not to import |
|---|---|---|
| [Hindsight](https://github.com/vectorize-io/hindsight) | Raw facts -> derived observations, BM25/vector/time/graph channels, RRF, retrieval traces. | Its Postgres-centered service when local SQLite already covers the product. |
| [Graphiti](https://github.com/getzep/graphiti) | `valid_at`/`invalid_at`-style temporal facts and episode-backed contradiction history. | Neo4j/FalkorDB and general graph traversal before evidence shows it is needed. |
| [Mem0](https://github.com/mem0ai/mem0) | Scoped facts, history, expiration for explicitly temporary memories. | Autonomous destructive updates and a parallel memory service. |
| [Letta](https://github.com/letta-ai/letta) | Small hot context plus searchable cold memory and inspectable change history. | A second complete agent runtime. |
| [LangMem](https://github.com/langchain-ai/langmem) | Semantic/episodic/procedural schemas and foreground/background extraction split. | A LangGraph dependency for primitives that fit the current services. |
| [OpenViking](https://github.com/volcengine/OpenViking) | Layered summaries, retrieval traces, typed memory templates. | AGPL code reuse and recursive whole-tree re-summarization on every write. |
| [Personal Model](https://github.com/Intuition-Lab/personal-model) | Owner-local evidence/interpretation separation and visible sparse/degraded states. | Unsupported high-level profiling when evidence is weak. |
| [HippoRAG 2](https://github.com/OSU-NLP-Group/HippoRAG) | A future option for measured multi-hop failures. | OpenIE and graph propagation in the default personal-fact path. |
| [screenpipe](https://github.com/screenpipe/screenpipe) | Competitor reference for local capture/search UX. | Its capture stack and current source-available code; OpenChronicle already has a narrower privacy pipeline. |

## Implemented decisions from this research

1. **No silent local-summary fallback.** Timeline/reducer model failures remain
   retryable; they do not produce heuristic facts.
2. **Local hybrid retrieval.** Approved durable entries use FastEmbed plus
   FTS5/BM25 and RRF. Event/capture corpora are not embedded by default.
3. **Evaluation before infrastructure.** The native fixture records retrieval,
   source, abstention, forbidden-hit, latency, and rebuild behavior. The
   LongMemEval-V2 adapter uses the production retrieval path.
4. **Reviewed fact evolution.** Supersede candidates bind the exact previous
   entry revision, revalidate it at approval, and preserve both old and new
   evidence.
5. **Real cross-session confirmation.** The classifier now has a separate
   authorized `search_activity_evidence` path. Multiple flushes in one session
   do not count as multiple independent observations.
6. **Claim support versus information flow.** Candidates separately bind the
   model's explicitly cited fact support and every input exposed before the
   proposal. Review stays focused without weakening privacy re-evaluation or
   transitive forget.
7. **Typed current facts.** New classifier proposals bind a canonical
   `subject_key`, `assertion_kind`, and optional valid-time interval. The
   metadata is stored in the canonical Markdown provenance frame, survives
   compaction, participates in candidate tamper/replay digests, and is projected
   into review and Published Memory. Global subject-slot conflicts prevent the
   same present-tense fact from silently diverging across files; supersede keeps
   one slot and current recall excludes scheduled or expired values.

## Remaining optimization sequence

### P0: published-memory product surface

The inspectable Memory/About Me page now supports search, source opening, typed
current-state metadata, and explicit local JSON/Markdown export through the
native save dialog. Remaining work is current/history switching,
edit-as-supersede, and entry-root forget.

### P0: typed fact history and state transitions

The current-value slice is implemented for new reviewed facts:

- `subject_key` or canonical fact slot;
- `assertion_kind`: user-asserted, observed, or inferred;
- `recorded_at` and `valid_from`/`valid_to`;
- current versus superseded and valid-time state.

Remaining work is a user-facing history view plus explicit disputed/retracted
states and `as_of` queries. Continue treating Markdown metadata as authority;
do not attempt a universal ontology.

### P0: complete longitudinal evaluation

Run the official LongMemEval-V2 small tier and report answer accuracy together
with evidence recall and query latency. Add native cases for current-value
updates, `as_of` history, repeated-session patterns, over-applied preferences,
and forget leakage.

### P1: event segmentation and adjacency retrieval

Keep minute timeline blocks, but materialize larger episodic segments using
existing session boundaries plus app/window/topic discontinuities. A hit should
optionally expand to neighboring segments. Do not introduce a new model call on
every minute; segmentation should be deterministic where possible and model-
assisted only for ambiguous topic shifts.

### P1: procedural memory from adopted outcomes

Only promote a workflow, template, or checklist after repeated evidence and/or
positive adoption. Procedural memories remain read-only context for generating
text artifacts; they never execute computer actions.

### P1: transparent ranking signals

Add current-state, explicit-temporary TTL, recency, and recall-use signals after
the existing BM25/vector fusion, with an explain-search view. Time affects rank,
not physical retention. Cross-encoder reranking is an experiment, not a default.

## Explicit non-goals

- No Neo4j, Qdrant, Postgres, or hosted vector dependency for the default path.
- No embeddings for all captures or every minute block.
- No automatic model deletion of remembered facts.
- No autonomous rewriting of reviewed history.
- No six-agent memory manager or second agent runtime.
- No direct computer-use feature; memory improves generated text products only.

## Evaluation sources

- [LongMemEval](https://github.com/xiaowu0162/LongMemEval) and
  [LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2): temporal,
  update, abstention, workflow, and environment-gotcha memory.
- [LoCoMo](https://github.com/snap-research/locomo): long conversational memory,
  temporal/causal and multi-hop questions.
- [MemoryAgentBench](https://github.com/HUST-AI-HYZ/MemoryAgentBench): retrieval,
  online learning, long-range understanding, and selective forgetting.
- [MemBench](https://github.com/import-myself/Membench): factual and reflective
  memory under participant/observer settings.
- [PrefEval](https://arxiv.org/abs/2502.09597): whether retrieved preferences are
  actually applied, and whether they are over-applied.
- [HaluMem](https://arxiv.org/abs/2511.03506): hallucination across extraction,
  update, and answering stages.

## Success criteria

The memory plane is ready for Vida-like use when it can demonstrate, on both
public and product-native evaluation:

- high current-fact precision and source coverage;
- low stale-value and forbidden-hit rates;
- correct temporal update and `as_of` history;
- explicit abstention when evidence is absent;
- zero surviving derivatives after explicit forget;
- bounded retrieval context and predictable local latency;
- a measurable lift in suggestion/draft adoption over a no-memory baseline.
