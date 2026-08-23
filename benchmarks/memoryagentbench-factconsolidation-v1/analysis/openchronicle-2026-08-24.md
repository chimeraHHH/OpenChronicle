# OpenChronicle frozen-tier result — 2026-08-24

Source identity:

- MemoryAgentBench code: `fe1735de8cf8b9908e1e3d3b5612afc815698062`
- dataset revision: `7ea066982b140a19337e17e60d45d4076e042faf`
- Parquet SHA-256: `24d5c3f09ce0ce15625cb9f8a98f44f0d864ca6c94d7b4ad04eb697ca3a5ff45`
- sample: `Conflict_Resolution / factconsolidation_sh_6k`
- scope: all 455 facts and all 100 frozen QA IDs

## Result

| Measure | Result |
|---|---:|
| Accepted review operations | 455 / 455 |
| Append / supersede | 294 / 161 |
| Current slots / retained history entries | 294 / 455 |
| Current-slot consistency | 1.000 |
| Fact / question parser coverage | 1.000 / 1.000 |
| No-memory accuracy | 0.000 |
| BM25 current-only accuracy | 1.000 |
| Typed current-fact accuracy | 1.000 |
| Typed stale-value rate | 0.000 |
| BM25 query P50 / P95 | 0.878 / 1.114 ms |
| Typed query P50 / P95 | 0.014 / 0.020 ms |
| Review-first ingest time | 41.636 s |
| Peak Python traced memory | 17.872 MB |

All frozen gates passed. The run exposed and motivated a product fix before the
benchmark could pass: accepted candidates whose applied entries had already
been superseded still occupied the active subject-conflict set. That prevented
a second update to the same typed fact. Historical accepted candidates now
remain auditable without blocking the current subject slot.

The 1.000 BM25 result should not be generalized to semantic personal-memory
retrieval. This corpus repeats entity and relation terms almost verbatim in its
questions. The typed variant is chiefly a lifecycle correctness diagnostic:
after 161 overwrites, it proves the current slot resolves to the final value and
never returns a retained historical value.
