# LECS-v1 local exact-turn selector

LECS-v1 is a new evidence-selection architecture, not another calibration of
the rejected generative selector. It keeps OpenChronicle's production local
SQLite FTS5/BM25 top-20 segment retrieval and replaces only the evidence
distillation call with a deterministic, local ONNX cross-encoder over exact
dialogue turns.

The design follows the extractive-compression direction of
[RECOMP](https://openreview.net/pdf?id=mlJLVigNHp) and the retrieval-noise
diagnosis measured by
[RAGChecker](https://github.com/amazon-science/RAGChecker). FastEmbed's
[official supported-model registry](https://qdrant.github.io/fastembed/examples/Supported_Models/)
lists `Xenova/ms-marco-MiniLM-L-6-v2` as an Apache-2.0, approximately 0.08 GB
cross-encoder and exposes its unnormalized relevance logits through
`TextCrossEncoder.rerank`.

## Frozen selection policy

For each benchmark question, production retrieval returns up to 20 dialogue
segments. LECS expands them into the same opaque exact-turn refs already used
by the cited answer evaluator. Every candidate turn is scored exactly once:

```text
left  = exact question
right = exact role + "\n" + exact turn content
```

Only a native raw float32 logit strictly greater than zero is eligible. The
selector sorts positive turns by score descending and then retrieval rank,
turn index, and opaque ref ascending, keeps at most seven, and restores the
chosen turns to retrieval/turn order for downstream presentation. Zero,
negative, non-finite, overlength, missing, or duplicate results never trigger a
fallback. A case with no positive turn emits only a structured insufficient
selection.

The zero threshold is the model's native single-logit decision boundary. It was
chosen from the model interface and official examples, not from any MemOps
score distribution. There is no threshold, model, cap, or top-k sweep.

## Local model boundary

[`model_artifact_manifest.json`](json/model_artifact_manifest.json) freezes the
immutable Hugging Face revision, every provisioned file's size and SHA-256,
the aggregate digest, FastEmbed/ONNX runtime versions, CPU provider, batch,
thread count, float32 logit extraction, and the 512-token no-truncation rule.
Formal evaluation loads only an explicit local snapshot with
`local_files_only=true`; it performs no model download, provider call, remote
I/O, or fallback.

## Data and promotion boundary

The selection contract is frozen before publishing the new development tier's
pair list. That tier must contain 50 source files disjoint from the original
development, already-observed qualification, and still-unobserved third
validation tiers. The validation tier contributes only a frozen source-ID
exclusion set; its questions, gold fields, retrieval, answers, and judges are
not read or run.

LECS first runs as a selection-only experiment. Both adjacent and longitudinal
settings must pass every hard execution and selection-quality gate in
[`selection_metric_contract.json`](json/selection_metric_contract.json) in one
run. Only then may the same frozen selector be connected to the existing
answer, per-part faithfulness, and lifecycle correctness stages under a
separate full-chain contract. Failure rejects LECS-v1; it does not authorize a
threshold search or consumption of the third validation tier.
