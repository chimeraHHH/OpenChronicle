# MemOps-50 adjacent operation inference: Sol v1

## Verdict

The pre-registered decision gate passes on clean OpenChronicle commit
`c94189597480934c9058b17e2110ca6753cf667b` with 50/50 successful
`codex_cli:gpt-5.6-sol` calls and no parse failure.

The result is strong evidence that the current no-tool classifier can recover
the operation and target structure from clean adjacent conversations. It is
not a full MemOps score, and the first scorer audit found a provenance-contract
mismatch that must be corrected before treating the exact provenance rate as a
model result.

## Reproduction identity

- provider/model: `codex_cli:gpt-5.6-sol`
- reasoning effort: `none`
- repository state at invocation: clean, zero status lines
- OpenChronicle commit:
  `c94189597480934c9058b17e2110ca6753cf667b`
- MemOps commit:
  `312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35`
- projected dataset digest:
  `2b57cab4decbf706ffc0166d2cd46afb534bb4a2f140f46bc001285d3839749f`
- decision manifest SHA-256:
  `732fb4382747111502f0254c3b275e4351398c344cb94283e9ebe5d4c3c96373`
- decision contract SHA-256:
  `3ab65c417f63ec002b567ea0f1001631c46c6308ea133b3d1d39a0e51dac746f`
- raw report SHA-256:
  `a2e0e5e7b8280c9757f7a84d71712735769cf76b1b879bdf5c31a5295c997990`
- action capability: none

## Metrics

| Metric | Result |
|---|---:|
| Parse success | 1.000000 |
| Operation precision | 0.978339 |
| Operation recall | 0.989051 |
| Operation F1 | 0.983666 |
| Target binding accuracy | 1.000000 |
| Exact provenance-set rate | 0.881919 |
| Exact full-string value rate | 0.453875 |
| Whole-case exact pass rate | 0.060000 |

Operation counts were 271 true positives, six false positives, and three false
negatives across 274 gold state-changing operations. Per type:

| Type | Precision | Recall | F1 |
|---|---:|---:|---:|
| Remember | 0.995434 | 0.995434 | 0.995434 |
| Update | 0.864865 | 0.969697 | 0.914286 |
| Forget | 1.000000 | 1.000000 | 1.000000 |
| Reflect | 1.000000 | 0.916667 | 0.956522 |

The exact value and whole-case rates are diagnostics, not semantic-quality
claims. The frozen gold uses the complete generated value as a single required
substring, while Sol usually emits a concise or expanded paraphrase. The
pre-registered contract correctly does not gate this field without a separate
semantic judge.

## Detection error analysis

Eight cases contained a detection error:

- five extra updates treated a later elaboration or future plan as a
  replacement (`lunch_meeting_timing`, `current_home_address`,
  `elaine_relationship_status`, `kevin_vehicle`, `chloe_city`);
- one cross-session `compensation_preference` pattern was emitted as remember
  instead of reflect, producing one FP plus one FN;
- one potential secondary-contact fact was omitted;
- one third weight update was omitted.

This points to two bounded prompt/model improvements: distinguish a value made
false from a value merely made more specific, and reserve pattern-shaped target
descriptions for reflect when independent sessions support them. These changes
must be evaluated as a development ablation and then validated on a separate
held-out selection.

## Provenance scorer audit

The v1 `0.881919` exact provenance-set rate is not yet an interpretable model
metric. The classifier prompt requires only the exact user turn that triggers
an operation and explicitly excludes prior values and later confirmations.
However, the adapter used upstream `evidence_spans` verbatim for every
operation. For some updates those spans contain both the old-value turn and the
new trigger turn, so a prompt-compliant prediction is counted wrong.

The correction is to use `trigger_span` for remember, update, and forget, while
retaining the complete independent support set for reflect. Because the raw
predictions are already immutable in this report, the corrected contract can be
re-scored deterministically without another model call. This v1 report remains
archived rather than overwritten.

## Remaining boundary

All 50 inputs are clean Stage 2 adjacent conversations. This result says
nothing about Stage 4 longitudinal distractor retrieval or paired question
answer accuracy. Those are the next external tiers after the scorer correction.
