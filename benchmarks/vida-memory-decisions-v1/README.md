# Model memory-decision evaluation v1

This bounded development split asks the configured classifier model to emit an
inert JSON decision artifact for `remember`, `update`, `forget`, and `reflect`.
It does not stage candidates, edit Markdown, or delete anything. `forget` is
only a scored label; product deletion remains explicit, previewed, and
user-confirmed.

Run with the configured classifier provider (the default configuration is
`codex_cli:gpt-5.6-sol`):

```bash
uv run python scripts/run_vida_memory_decisions.py \
  --output reports/vida-memory-decisions.json
```

The adapter records operation precision/recall/F1, exact value accuracy,
provenance support, no-operation accuracy, and the first failure stage per
case. Current/old values and evidence identities are exact; generated new
values use frozen necessary phrase anchors so harmless wording changes do not
require a second judge model. It also exposes `adapt_memops_sample` for the official MemOps evidence
JSON shape, pinned during design at
`312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35`. No upstream code or generated
sample is copied into this repository.

This split is intentionally small and deterministically scored. Passing it is
a local regression result, not a claim about public MemOps performance or
real-user quality.

The first clean configured-model result is recorded in
[analysis/gpt-5.6-sol-2026-08-23.md](analysis/gpt-5.6-sol-2026-08-23.md).

## Fixed official MemOps smoke

The external-data adapter also supports a frozen adjacent four-file smoke with
27 confirmed operations (23 remember, two update, one forget, one reflect):

```bash
git clone https://github.com/MemTensor/MemOps.git /path/to/MemOps
git -C /path/to/MemOps checkout 312af65e2c7b6d1b70f062ffa8b4cde32aaf6f35
uv run python scripts/run_vida_memory_decisions.py \
  --memops-root /path/to/MemOps \
  --output reports/vida-memory-decisions-memops.json
```

The runner verifies the clone commit plus every selected file's SHA-256 before
model egress. The repository stores only the manifest, not the upstream
generated conversations. This smoke exercises the official evidence shape and
update chains, but is not the full MemOps question/judge pipeline.

The first clean official-data smoke result is recorded in
[analysis/official-memops-smoke-2026-08-23.md](analysis/official-memops-smoke-2026-08-23.md).

## Repeated-run stability

`official_memops_stability_contract.json` freezes a three-or-more-run gate for
the same clean repository commit, dataset/contract digests, provider/model, and
inert action capability. After producing repeated decision reports outside the
worktree, aggregate them with:

```bash
uv run python scripts/run_vida_memory_decision_stability.py \
  /tmp/memops-run-1.json /tmp/memops-run-2.json /tmp/memops-run-3.json
```

The aggregate records metric min/max/mean/stdev, provider/parse failures,
per-case exact decision signatures, and whole-run signature diversity. It does
not rerun or judge the model and cannot execute predicted operations.

The first three-run v1 result is intentionally retained as a failed gate in
[analysis/official-memops-stability-v1-2026-08-24.md](analysis/official-memops-stability-v1-2026-08-24.md):
operation metrics were perfectly stable, but byte-level free-text decisions
were not. Structural, evidence-set, and surface-text agreement need separate
follow-up metrics.

`official_memops_stability_contract_v2.json` is that separate follow-up. It
requires exact operation type/target/order agreement on every case and exact
evidence-set agreement on at least 75% of cases. Complete generated-value JSON
agreement remains visible as a stricter diagnostic but is not relabeled as a
structural decision failure. Pass `--contract ..._v2.json` to the same
aggregation command; the v1 contract and report remain unchanged.
