# OpenChronicle frozen-tier result — 2026-08-24

Source identity:

- OpenChronicle commit: `93fb60faf57bc2471cc09f7c7dcaf3a2e419615a`
- repository state during run: clean
- MemoryAgentBench code: `fe1735de8cf8b9908e1e3d3b5612afc815698062`
- dataset revision: `7ea066982b140a19337e17e60d45d4076e042faf`
- Parquet SHA-256: `24d5c3f09ce0ce15625cb9f8a98f44f0d864ca6c94d7b4ad04eb697ca3a5ff45`
- manifest SHA-256: `de9c8c00bbd5b3b4fa67cddaf6d44e689436db677cef7c0d47d973a45d90c629`
- metric contract SHA-256: `e576c9fda1ac259ac7053483ebb7f2bb820fa5e16d7c4fedccf268c36c53b27d`
- raw report SHA-256: `4a7c075e34d20061cc87dfd64d6ca6aa03a884052d035587ff4ef2fd794ed0ed`
- sample: `Conflict_Resolution / factconsolidation_sh_6k`
- scope: all 455 facts and all 100 frozen QA IDs

## Result

| Measure | Result |
|---|---:|
| Accepted review operations | 455 / 455 |
| Append / supersede | 294 / 161 |
| Current entries / unique typed slots | 294 / 294 |
| Duplicate current typed slots | 0 |
| Retained history entries | 455 |
| Current-slot consistency | 1.000 |
| Fact / question parser coverage | 1.000 / 1.000 |
| No-memory accuracy | 0.000 |
| Pure BM25 top-1 accuracy | 1.000 |
| BM25 top-20 + slot-oracle accuracy | 1.000 |
| Typed slot-oracle accuracy | 1.000 |
| Typed contradiction-free accuracy | 1.000 |
| Typed stale / contradiction rate | 0.000 / 0.000 |
| Pure BM25 query P50 / P95 | 0.705 / 0.951 ms |
| BM25 + oracle query P50 / P95 | 0.874 / 1.117 ms |
| Typed query P50 / P95 | 0.018 / 0.024 ms |
| Review-first ingest time | 74.052 s |
| Peak Python traced memory | 17.889 MB |

All 16 frozen gates passed. Before this clean run, an independent audit found
two product-level uniqueness failures: direct Published Memory correction and a
forged supersede marker could release a typed subject slot. Approval now checks
canonical current Markdown facts inside the publication fence and only releases
historical candidate ownership when the successor body, subject, exact
provenance reference, SQLite provenance edges, and deterministic candidate
entry identity all agree.

The earlier result used the label `bm25_current_only` for a top-20 retrieval
followed by exact subject-slot selection. That label was incorrect. This rerun
separates a pure top-1 BM25 baseline from the slot-oracle reranker; both happen
to score 1.000 on this corpus. The pure result still should not be generalized
to semantic personal-memory retrieval because the questions repeat entity and
relation terms almost verbatim. The typed result proves lifecycle/schema
correctness after 161 overwrites, not open-ended question understanding.

Stale detection is now independent of substring correctness. An answer that
contains both an old and current value is marked stale and contradictory even
if the official substring metric considers it correct. The no-memory arm is
retained only as a deterministic sanity check.
