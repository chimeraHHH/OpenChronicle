# Local long-term memory research and implementation decisions

Date: 2026-08-23

This report consolidates three parallel investigations: an audit of the current
OpenChronicle implementation, a paper survey, and a repository/competitor
survey. The product constraint is unchanged: local-first, text-product focused,
no direct computer-use feature, and no unnecessary external infrastructure.

The report was refreshed against `agent/vida-integration@c4459a3` after a second
multi-agent pass on 2026-08-23. That refresh matters: several apparent gaps in
the first audit had already been closed by intervening commits. The decisions
below distinguish the current implementation from genuinely remaining work
instead of turning stale findings into duplicate infrastructure.

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
  confirmation;
- typed current facts with stable subject slots, assertion basis, valid-time
  boundaries, local correction, export, and full revision-lineage forget;
- Published Memory/About Me desktop inspection with exact sources;
- no model-failure path that writes a heuristic timeline or session summary.

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

## 2026 refresh: operation-level evidence

The second pass inspected current upstream heads rather than relying only on
paper abstracts or benchmark leaderboards:

| Source | Pinned identity inspected | Net-new implication |
|---|---|---|
| [MemOps](https://github.com/MemTensor/MemOps) | `312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35` | Its gold traces cover remember, update, forget, reflect, and multi-step state trajectories. It separately measures stale-value use, forget leakage, over-forget, operation detection, and provenance support. This is a closer fit to OpenChronicle's reviewed lifecycle than another retrieval-only leaderboard. |
| [LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2) | `2cc8c540bdb87fe6761629b585e727e1c4704520` | The current 451-question harness tests static and dynamic state, workflow knowledge, environment gotchas, and premise awareness over long trajectory histories. Keep using only its memory/evidence contract; do not import computer-use behavior into the product. |
| [Hindsight](https://github.com/vectorize-io/hindsight) | `3295716cafcc593b6a2cdebd03dd71373b091859` | Its semantic, keyword, graph, and temporal channels plus RRF confirm the value of multiple recall signals, but its cross-encoder and graph stages should remain experiments until OpenChronicle error analysis proves they pay for their local footprint. |
| [Graphiti](https://github.com/getzep/graphiti) | `993e081a6d7948a0d8851c12a5fbdbeb49fed862` | Its episode/reference time and fact valid/invalid time reinforce the existing typed-fact direction. The remaining product gap is historical querying, not the absence of a graph database. |
| [MIRIX](https://github.com/Mirix-AI/MIRIX) | `8cb06a62bbb7c478beb33dd4f2815696a72df482` | Six logical memory types are useful vocabulary, but a dedicated agent and physical store per type would add cost and autonomous mutation without solving OpenChronicle's present evaluation gaps. |
| [Personal Model](https://github.com/Intuition-Lab/personal-model) | `b7a28ffadaed3d83d9522efc8c171d6d642fd91b` | Evidence receipts, observation/inference separation, and explicit `degraded`/`not_built` states support making recall quality and unavailable projections visible rather than fabricating a seamless profile. |

Two current papers sharpen the same conclusion. [HaluMem](https://arxiv.org/abs/2511.03506)
evaluates extraction, update, and question answering separately, showing why an
end-to-end answer score cannot localize memory corruption. The 2026
[TrustMem](https://arxiv.org/abs/2606.25161) preprint reports fewer omission,
corruption, and hallucination errors from constrained consolidation. Its
author-reported numbers still need independent replication, but the failure
taxonomy supports OpenChronicle's review-first, provenance-bound write path.

### Corrected gap matrix

| Capability | Current state after the research implementation | Remaining optimization |
|---|---|---|
| Minute normalization | Model-backed, Luna-ready, retryable failure; no local rule summary | Measure quality/cost/latency on real replay data rather than adding another fallback. |
| Session reduction | Model-backed and retryable; empty/malformed output is not materialized | Add stage-level extraction error fixtures and observable retry health. |
| Cross-session pattern evidence | Dedicated bounded event-history BM25 tool, independent-session requirement | Evaluate pattern precision and unsupported generalization; semantic activity search is optional only if BM25 misses are measured. |
| Durable fact lifecycle | Reviewed append/supersede, typed slot and valid time, current and historical projections | Add disputed/retracted states only when product cases require them. |
| Retrieval | Local BM25 + multilingual embedding + RRF, explicit unavailable state, explicit MCP `as_of` | Add adjacent event expansion and ranking explanations; defer cross-encoder/graph. |
| Published Memory | Current facts, source view, correction, export, complete revision-lineage forget, on-demand desktop history | Add desktop `as_of` query UX; current view intentionally hides superseded values. |
| Evaluation | Native retrieval and reviewed lifecycle fixtures, inert model-decision adapter, real LongMemEval-V2 trajectory smoke | Run a fixed public MemOps and LongMemEval-V2 tier before making quality claims. |
| Procedural memory | Suggestions and feedback exist, but no reviewed reusable workflow memory | Promote only repeated/adopted text workflows, templates, or checklists; never execute them. |

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
8. **Native lifecycle operation gate.** Three deterministic traces now execute
   seven gold remember/update/forget/reflect operations through production
   review services and inspect five intermediate states. The clean result has
   zero stale-value, forget-leakage, and over-forget failures with complete
   claim-support coverage. It is a local regression result, not a public MemOps
   score or a model-extraction claim.
9. **Explicit historical search.** MCP `search(as_of=...)` considers immutable
   superseded revisions but returns only the version recorded and not yet
   replaced at the requested instant, while applying typed valid-time metadata
   at the same instant. Default recall remains current-only.
10. **On-demand revision inspection.** Published Memory can fetch the selected
    current fact's clean, newest-first immutable lineage through a
    revision-bound desktop protocol. Each version retains its source-drawer
    identity, while superseded values remain absent from the default snapshot.
11. **Separated model-decision gate.** A tools-free JSON evaluator now scores
    remember/update/forget/reflect detection, target binding, frozen value
    anchors, exact evidence support, abstention, and first failure stage. It
    never stages, approves, edits, or deletes memory; `forget` is only an inert
    benchmark label. The first clean `codex_cli:gpt-5.6-sol` native development
    run passed all eight cases and six operations with 1.00 operation F1,
    binding, value, provenance, and no-operation accuracy. This is not an
    official MemOps score.

## Remaining optimization sequence

### P0: run the model-decision adapter on fixed public and native tiers

The native gold-operation fixture is implemented for:

- remember with exact source support;
- update without returning the superseded value as current;
- forget with zero target leakage and zero unrelated over-forget;
- bounded reflection without unsupported generalization;
- multi-step state trajectories with correct order and provenance.

The execution gate reports operation success, stale-value rate, forget leakage,
over-forget, provenance support, and trajectory order separately. The new
model-decision adapter adds operation precision/recall/F1 and explicit
provider/parse/detection/value/provenance failures without a second judge
model. Its native development split must now be followed by a fixed official
MemOps tier; neither result should be hidden behind one aggregate score. The
external-data runner now pins the official clone commit and four adjacent
sample digests, supports repeated update operations on one target, and keeps
all upstream generated conversations outside this repository. Its first clean
run recalled all 27 gold operations with 0.931 precision, 1.000 recall, 0.964
F1, and 0.963 exact provenance support. Two elaborations were still
misclassified as updates, and repeat stability remains unmeasured. This is an
official-data adapter smoke, not the full MemOps evaluation.

In parallel, finish a fixed official LongMemEval-V2 small-tier run and preserve
the adapter version, dataset revision, model, latency, and retrieved evidence.
The current real-trajectory smoke proves compatibility, not longitudinal
quality.

### P0: finish historical fact inspection UX

The current-value path, user correction, full-lineage forget, explicit MCP
`as_of` search, and on-demand desktop immutable revision history are complete.
Add a desktop `as_of` query without allowing superseded entries into ordinary
current recall.
Disputed/retracted states should be introduced only with concrete product cases;
do not build a universal ontology.

### P1: event segmentation and adjacency retrieval

Keep minute timeline blocks, but materialize larger episodic segments using
existing session boundaries plus app/window/topic discontinuities. A hit should
optionally expand to neighboring segments. Do not introduce a new model call on
every minute; segmentation should be deterministic where possible and model-
assisted only for ambiguous topic shifts.

The materialized segment must remain a rebuildable projection over exact
timeline/session evidence. Evaluate minute, session, and event/topic retrieval
units on the same cases before changing the default.

### P1: procedural memory from adopted outcomes

Only promote a workflow, template, or checklist after repeated evidence and/or
positive adoption. Procedural memories remain read-only context for generating
text artifacts; they never execute computer actions.

### P1: transparent ranking signals

Add current-state, explicit-temporary TTL, recency, and recall-use signals after
the existing BM25/vector fusion, with an explain-search view. Time affects rank,
not physical retention. Cross-encoder reranking is an experiment, not a default.

The explanation should expose channel ranks, filters, temporal interpretation,
projection/model identity, and an explicit unavailable/degraded reason. It does
not need production observability infrastructure.

### P2: only evidence-backed consolidation experiments

After lifecycle evaluation exists, compare candidate-only consolidation against
the current reviewed path. A consolidation pass may propose merge, supersede,
or procedural candidates, but cannot rewrite approved history. Test constrained
write prompts or a stronger writer model only as an ablation against omission,
corruption, hallucination, latency, and cost.

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
- [MemOps](https://github.com/MemTensor/MemOps): remember/update/forget/reflect
  operation traces, target binding, lifecycle state, and provenance metrics.

## Immediate decision

The native lifecycle harness and inert model-decision adapter are implemented.
The next evaluation slice is a fixed official MemOps tier; the next product
slice is desktop `as_of` inspection. Neither requires a graph store or another
autonomous memory agent. Together with the retrieval baselines, these results
will tell us whether failures begin at extraction or retrieval before event
segmentation or reranking is promoted on intuition.

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
