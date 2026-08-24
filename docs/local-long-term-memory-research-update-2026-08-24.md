# Local long-term memory research update — 2026-08-24

## Decision

OpenChronicle's current bottleneck is post-retrieval evidence purity, not raw
candidate recall. LECS-v1 therefore remains frozen as the next experiment:
production SQLite FTS5/BM25 retrieves top-20 segments, a pinned local ONNX
cross-encoder scores every exact turn once, and only positive logits enter a
seven-turn maximum context.

No temporal bundle, adaptive budget, summary tree, graph store, automatic
consolidation, or forgetting rule may change that selection-only run. Those
ideas are ordered follow-up experiments, not additions to the current gate.

## Ranked opportunities

| Rank | Increment | Expected value | Cost | Allowed before the LECS gate |
|---|---|---:|---:|---|
| 1 | Temporal version/conflict evidence bundle | Very high | Low-medium | Offline shadow analysis only |
| 2 | Query-adaptive evidence budget and abstention | High | Low | Counterfactual replay only |
| 3 | Memory-transition verifier | Medium-high | Low-medium | Independent read-only audit |
| 4 | Rebuildable multi-granularity temporal projection | Medium now, high at scale | High | No |
| 5 | Budget-triggered reversible consolidation | Low now | Medium | Statistics only |

### 1. Temporal version/conflict evidence bundle

For a recalled canonical fact, resolve its `subject_key` and present the
current version together with the immediately preceding version, validity
interval, supersede relation, timestamps, and original evidence refs. The
cross-encoder continues to score only exact, attributable text leaves; the
bundle merely keeps a state transition together and labels each item as
`current`, `superseded`, `scheduled`, or `expired`.

This fits the existing SQLite/Markdown model and does not require a graph
database. [APEX-MEM](https://aclanthology.org/2026.acl-long.749/) supports an
append-only history with retrieval-time conflict resolution, while the
[LongMemEval reference implementation](https://github.com/xiaowu0162/LongMemEval)
includes time-aware query expansion and temporal filtering.

The falsifiable experiment keeps retrieval, LECS, and the answerer fixed and
adds only the evidence bundle. It must improve Update/Forget/Reflect and
StateTransition accuracy without reducing evidence precision or increasing
distractor share.

### 2. Query-adaptive evidence budget and abstention

If frozen LECS-v1 still shows high recall but low precision, use its existing
logits for a separate, frozen router. Candidate features may include positive
count, top-score margin, score-distribution entropy, candidate length, and
query length. The output is either `insufficient` or a budget from one to seven.
Raw logits are not assumed to be calibrated across questions.

[RECOMP](https://arxiv.org/abs/2310.04408) demonstrates that a post-retrieval
compressor may return no augmentation when retrieved context is unhelpful; its
[official code](https://github.com/carriex/recomp) is available under MIT.
[MemGAS](https://openreview.net/forum?id=i2yIvZARnG) uses relevance-distribution
entropy for granularity selection. Its
[repository](https://github.com/Applied-Machine-Learning-Lab/ICLR2026_MemGAS)
is useful as an algorithm reference, but its generated summaries and unclear
repository licensing make direct reuse inappropriate here.

### 3. Memory-transition verifier

Add a read-only receipt for create, supersede, merge, and archive transitions:

- before/after state digest;
- coverage of facts that were preserved, superseded, or recoverable;
- preservation of state outside the requested operation;
- evidence-ref faithfulness for every new statement;
- input, output, and provenance hashes.

[TrustMem](https://arxiv.org/abs/2606.25161) motivates coverage, preservation,
and faithfulness checks. [Verifiable Memory](https://arxiv.org/abs/2608.03137)
and its [official repository](https://github.com/Sun-SYSU-24/VerMem) separate
operation-level verification from final evidence consistency. Their large-model
training stack is out of scope; the audit dimensions are reusable. Start with
reports only—no automatic retry, rewrite, or write rejection.

### 4. Rebuildable temporal projections

At larger scale, derive an inspectable hierarchy from immutable leaves:

```text
exact turn -> topic/session -> day -> week/persona
```

Every derived node must retain child hashes and evidence refs, expand to exact
turns, and be fully rebuildable. Only dirty paths should be recomputed.
[TiMem](https://aclanthology.org/2026.findings-acl.1091/) and its
[self-hostable implementation](https://github.com/TiMEM-AI/timem) support
hierarchical temporal recall. [SeCom](https://github.com/microsoft/SeCom)
supports topic segmentation plus post-retrieval compression. This is deferred
because current top-20 candidate recall is already saturated; the measured
problem is selection purity.

### 5. Budget-triggered reversible consolidation

Do not enable periodic decay or automatic deletion. Consolidation should start
only after measured storage or answer-context budgets are exceeded. Similarity
may nominate possible duplicates but must not decide deletion. Raw evidence
leaves remain the authority; summaries are derived and rebuildable; privacy
deletion remains a separate explicit tombstone/hard-delete path.

[LightMem](https://github.com/zjunlp/LightMem) reports major efficiency gains,
but [an independent reproduction](https://arxiv.org/abs/2607.29104) finds that
retriever choice and matched retrieval depth can erase or reverse the apparent
benefit. [Retain or Consolidate?](https://arxiv.org/abs/2607.17545) likewise
finds consolidation most useful under tight budgets. The product should first
record duplicate rate, store bytes, retrieved tokens, and evidence at risk.

## Frozen next sequence

1. Run LECS-v1 selection-only once on the new source-disjoint development tier.
2. Connect the same selector to answer, faithfulness, and correctness stages
   only if both adjacent and longitudinal selection gates pass.
3. Evaluate the temporal version/conflict bundle as the first independent
   increment.
4. Evaluate an adaptive evidence budget only if fixed-cap LECS remains noisy.
5. Add transition-verifier enforcement only after read-only receipts are useful.
6. Add hierarchy or consolidation only when measured scale/budget pressure
   justifies it.

The reserved third validation tier was neither read nor run in this work. Its
source IDs were used only through the frozen source-only exclusion artifact.
