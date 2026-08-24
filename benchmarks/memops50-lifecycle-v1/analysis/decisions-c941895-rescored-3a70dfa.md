# MemOps-50 adjacent operation inference: corrected provenance rescore

## Verdict

The corrected decision gate passes on clean OpenChronicle commit
`3a70dfa470c1cc059c874608dda4e99ef768d480`. The rescore made zero model calls:
it reuses the immutable 50-case `codex_cli:gpt-5.6-sol` output from commit
`c94189597480934c9058b17e2110ca6753cf667b`, preserving every response hash,
response size, and latency.

Operation F1 remains `0.983666`. Exact provenance-set accuracy is corrected
from `0.881919` to `0.918819` after applying the prompt's declared rule:
remember, update, and forget use only the exact trigger turn, while reflect uses
its complete independent support set.

## Reproduction identity

- provider/model: `codex_cli:gpt-5.6-sol`
- reasoning effort: `none`
- source model report SHA-256:
  `a2e0e5e7b8280c9757f7a84d71712735769cf76b1b879bdf5c31a5295c997990`
- source model commit:
  `c94189597480934c9058b17e2110ca6753cf667b`
- rescorer commit:
  `3a70dfa470c1cc059c874608dda4e99ef768d480`
- MemOps commit:
  `312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35`
- projected dataset digest:
  `2b57cab4decbf706ffc0166d2cd46afb534bb4a2f140f46bc001285d3839749f`
- raw rescored report SHA-256:
  `40d9b71690a7044f91a447a594ae7e903df115301ed8496460a512e06fed47b9`
- repository state at rescore start: clean, zero status lines
- model calls made by rescore: 0

## Metrics

| Metric | Result |
|---|---:|
| Parse success | 1.000000 |
| Operation precision | 0.978339 |
| Operation recall | 0.989051 |
| Operation F1 | 0.983666 |
| Target binding accuracy | 1.000000 |
| Exact provenance-set rate | 0.918819 |
| Exact full-string value rate | 0.453875 |
| Whole-case exact pass rate | 0.060000 |

Per-operation detection metrics are unchanged from the original run:

| Type | Precision | Recall | F1 |
|---|---:|---:|---:|
| Remember | 0.995434 | 0.995434 | 0.995434 |
| Update | 0.864865 | 0.969697 | 0.914286 |
| Forget | 1.000000 | 1.000000 | 1.000000 |
| Reflect | 1.000000 | 0.916667 | 0.956522 |

The remaining 22 exact provenance mismatches are real strict-set differences,
mostly involving reflect support sets, plus a few non-reflect predictions that
include confirmation turns beyond the trigger. Value and whole-case exactness
remain diagnostics because the gold value is a generated full-string anchor,
not a semantic judge target.

## Optimization decision

The evidence does not justify automatic decay, consolidation, or confidence
ranking. Reviewed lifecycle execution is already perfect on this tier. The
next externally grounded work is therefore:

1. measure retrieval under the same 50 Stage 4 longitudinal distractor cases;
2. keep deterministic retrieval recall separate from answer-model quality;
3. improve update-versus-elaboration and reflect-versus-fact boundaries on a
   development set, then validate any prompt change on held-out cases.
